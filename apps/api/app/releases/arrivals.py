"""A change arriving on a stage (architecture §4.3, §4.4): its deploy through the stage's target, its verify after
arrival, the guard on the step out of the stage, and Roll back. One row per landing (`stage_arrivals`).

LANDING (`land`) — the control plane writes an arrival when code lands on a stage's branch: a release's merge
(`runs.mark_merged`), and a task PR merged into the integration branch (the merge lane's `merged` event, a runner's
self-merge through the outcome ledger). A new arrival supersedes the stage's older one still moving (its executor
gets 409 and stops). By target (`pipeline_gates/defaults.target_kind`):
  * `none` → `arrived` at once;
  * `forge_env` → `deploying` while the stage's deploys are recorded (a deploy-tracked release; the integration stage
    with deploy events connected), settled by the ledger (`on_deploy`, from `anchoring`); else `arrived`;
  * `argocd`, `webhook`, `script` → `deploying`, and a `deploy` work item for the release executor, which drives the
    target and reports `pipeline.arrival_event` (`apply_event`).
A release into a driven target stays `deploying` until its arrival does: `arrived` makes it `live`, `deploy_failed`
fails it with the target's reason.

VERIFY — on `arrived`: a stage with no enabled After-arrival gate is `verified` at once; else `verifying`, a gate run
opened and a `verify` work item for the executor, whose verdict makes it `verified` or `verify_failed`.

THE STEP OUT (`step_open`) — a step out of a stage with After-arrival gates opens only once the stage's arrival of
the head being promoted is `verified`; a stage without them never holds a step (today's behaviour).

SWEEP (`sweep`, the release tick) — `deploying` past `DEPLOY_WITHIN` → `deploy_failed`; `verifying` past its gates'
summed timeouts plus `VERIFY_SLACK` → `verify_failed`; work still owed a node gets its executor back.

ROLL BACK (`rollback`) — offered on a `verify_failed` or `deploy_failed` arrival when the stage has an earlier
`arrived`/`verified` one, to whoever may press the step into the stage: a `rollback` release reverting the failed
merge on the stage's branch through the executor (and the merge lane where it is on). Its merge marks the failed
arrival `rolled_back`, and its own arrival deploys the previous version through the same target. What it reverted
waits again before the step (`rolled_back_shas`), and the next promote re-applies it (`reapply_shas`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import secret_redact
from app.authz.principal import Principal
from app.clock import utcnow
from app.models.projects import Project
from app.models.releases import (
    ARRIVAL_OPEN,
    GateResult,
    GateRun,
    Release,
    ReleaseChange,
    ReleaseContained,
    ReleaseStage,
    StageArrival,
)
from app.models.tasks import Task
from app.pipeline_gates import defaults as gate_defaults, full_gate
from app.releases import executor, lifecycle, service, snapshot, stages as stages_mod

log = logging.getLogger(__name__)

#: A deploy that has not reached its stage this long after landing failed (the release's own rule, `tick.py`).
DEPLOY_WITHIN = timedelta(hours=2)
#: A verify gets its gates' summed timeouts plus this before it is called failed.
VERIFY_SLACK = timedelta(minutes=10)
#: The targets the release executor drives on a node.
DRIVEN = ("argocd", "webhook", "script")
#: An arrival a roll back returns to.
GOOD = ("arrived", "verified")
#: The arrivals that offer Roll back.
FAILED = ("verify_failed", "deploy_failed")
#: The reports kept on an arrival (plain text).
REPORT_MAX = 1000
#: The release states a roll back still counts in (a failed or canceled one undid nothing).
_ROLLBACK_LIVE = tuple(s for s in lifecycle.STATES if s not in (lifecycle.FAILED, lifecycle.CANCELED))


class Conflict(Exception):
    """The arrival moved on (superseded, settled): the executor drops it (409)."""


def _report(value: Any) -> Optional[str]:
    text = secret_redact.redacted(str(value or "").strip())
    return text[-REPORT_MAX:] or None


def _https(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text[:2000] if text.startswith("https://") else None


# ── What a stage verifies ─────────────────────────────────────────────────────────────────────────────


def after_wire(session: Session, project: Project, stage: ReleaseStage) -> list[dict[str, Any]]:
    """The stage's After-arrival list as the node driver reads it (`GateWire`), off ones included (reported `off`)."""
    gates = gate_defaults.gates_of(session, project, [stage]).get((stage.id, gate_defaults.AFTER), [])
    return [full_gate.wire(g, project=project, stage=stage.name, step=f"Verify on {stage.name}") for g in gates]


def _enabled(gates: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [g for g in gates or [] if g.get("enabled", True)]


def verifies(session: Session, project: Project, stage: ReleaseStage) -> bool:
    """Whether `stage` has an enabled After-arrival gate: only then does it hold the step out of it."""
    return bool(_enabled(after_wire(session, project, stage)))


# ── Landing ──────────────────────────────────────────────────────────────────────────────────────────


def latest(session: Session, stage_id: UUID, *, sha: Optional[str] = None) -> Optional[StageArrival]:
    """The stage's newest arrival (of `sha`, when named)."""
    stmt = select(StageArrival).where(StageArrival.stage_id == stage_id)
    if sha is not None:
        stmt = stmt.where(StageArrival.sha == sha.lower())
    return session.scalar(stmt.order_by(StageArrival.created_at.desc(), StageArrival.id.desc()).limit(1))


def _supersede(session: Session, stage_id: UUID) -> None:
    for old in session.scalars(select(StageArrival).where(
            StageArrival.stage_id == stage_id, StageArrival.state.in_(ARRIVAL_OPEN)).with_for_update()):
        old.state = "superseded"
        _cancel_run(session, old)
    session.flush()


def _cancel_run(session: Session, arrival: StageArrival) -> None:
    if arrival.gate_run_id is None:
        return
    run = session.get(GateRun, arrival.gate_run_id)
    if run is not None and run.state == "running":
        run.state, run.finished_at = "canceled", utcnow()


def land(session: Session, project: Project, stage: ReleaseStage, sha: str, *, release: Optional[Release] = None,
         lane_row_id: Optional[UUID] = None, version: Optional[str] = None,
         tracked: bool = False) -> StageArrival:
    """`sha` landed on `stage`'s branch: its arrival (the existing one when the stage's newest already is of `sha`).
    `tracked`: a `forge_env` stage's deploy of it will be recorded (a deploy-tracked release). Flushes."""
    sha = sha.lower()
    service.lock_target(session, stage.id)  # one landing per stage at a time
    newest = latest(session, stage.id)
    if newest is not None and newest.sha == sha and newest.state != "superseded":
        if release is not None and newest.release_id is None:
            newest.release_id = release.id
        return newest
    _supersede(session, stage.id)
    kind = gate_defaults.target_kind(stage)
    after = after_wire(session, project, stage)
    if kind == "forge_env" and not tracked and stages_mod.is_integration(stage) and _enabled(after):
        # Only a stage that verifies waits for its deploy: one without After-arrival gates holds nothing, and a
        # deploy workflow that skips a push (path filters) must not read as a failed deploy there.
        from app.releases.views import deploy_events  # noqa: PLC0415 — views imports the releases package

        tracked = deploy_events(session, project) != "not_connected"
    arrival = StageArrival(
        org_id=project.org_id, project_id=project.id, stage_id=stage.id, sha=sha,
        release_id=release.id if release is not None else None, merge_lane_row_id=lane_row_id,
        version=version or (release.version if release is not None else None), target_kind=kind,
        state="deploying", after_gates=after,
    )
    session.add(arrival)
    session.flush()
    if kind in DRIVEN:
        executor.ensure_executor(session, project, release.requested_by_id if release is not None else None)
    elif not (kind == "forge_env" and tracked):
        _arrived(session, project, stage, arrival, None)
    service.wake_tick(session)
    return arrival


def land_integration(session: Session, project_id: int, sha: Optional[str], *,
                     lane_row_id: Optional[UUID] = None,
                     merged_at: Optional[datetime] = None) -> Optional[StageArrival]:
    """A task PR merged into the integration branch at `sha` (`merged_at`, when it merged): the integration stage's
    arrival, when the project has a release pipeline (none on a project without one). A merge the stage already
    holds, or one older than the stage's newest arrival (a late reconcile), lands nothing: it would redeploy an old
    head. In a savepoint, logged and swallowed on a fault: the merge it records never fails for it (a missed arrival
    only leaves the stage's verify unrun)."""
    project = session.get(Project, project_id)
    if project is None or not sha or not stages_mod.has_repo(project):
        return None
    try:
        with session.begin_nested():
            stages = stages_mod.branch_stages(stages_mod.stages_of(session, project.id))
            if not stages or not stages_mod.is_integration(stages[0]):
                return None
            stage = stages[0]
            if session.scalar(select(StageArrival.id).where(StageArrival.stage_id == stage.id,
                                                            StageArrival.sha == sha.lower()).limit(1)) is not None:
                return None
            newest = latest(session, stage.id)
            if newest is not None and merged_at is not None and newest.created_at > merged_at:
                return None
            return land(session, project, stage, sha, lane_row_id=lane_row_id)
    except Exception:  # noqa: BLE001 — see the docstring
        log.exception("arrivals: landing %s on project %s failed", sha, project_id)
        return None


def _release_of(session: Session, arrival: StageArrival) -> Optional[Release]:
    if arrival.release_id is None:
        return None
    return session.scalar(select(Release).where(Release.id == arrival.release_id).with_for_update())


def _arrived(session: Session, project: Project, stage: ReleaseStage, arrival: StageArrival,
             url: Optional[str]) -> None:
    """The change is on the stage: its address, its release live, and its verify started."""
    arrival.state = "arrived"
    if url:
        arrival.url = url
        # The target owns the address: an earlier typed one gives way (Settings shows it read-only); Easy Mode's
        # own stays.
        if arrival.target_kind in DRIVEN and stage.url_source != "easy_mode":
            stage.url, stage.url_source = url, arrival.target_kind
    release = _release_of(session, arrival)
    if release is not None and release.state == lifecycle.DEPLOYING and arrival.target_kind in DRIVEN:
        service._move(release, "anchor", lifecycle.LIVE)
        release.failure = None
    gates = arrival.after_gates or []
    if not _enabled(gates):
        arrival.state = "verified"
        session.flush()
        return
    from app.pipeline_gates import results as gate_results  # noqa: PLC0415

    run = gate_results.open_run(session, project, stage, gate_defaults.AFTER, "arrival", str(arrival.id), gates,
                                sha=arrival.sha)
    arrival.state, arrival.gate_run_id = "verifying", run.id
    session.flush()
    executor.ensure_executor(session, project, release.requested_by_id if release is not None else None)


def _deploy_failed(session: Session, arrival: StageArrival, report: Optional[str]) -> None:
    arrival.state, arrival.report = "deploy_failed", report
    release = _release_of(session, arrival)
    if release is not None and release.state == lifecycle.DEPLOYING:
        service._move(release, "fail", lifecycle.FAILED)
        release.failure = {"kind": "deploy_failed", "message": report, "action": "retry"}
    session.flush()


def on_deploy(session: Session, project: Project, stage: ReleaseStage, carries: Callable[[str], bool], ok: bool,
              *, url: Optional[str] = None, report: Optional[str] = None) -> bool:
    """The ledger saw a deploy of `stage`'s environment conclude (`anchoring`): the stage's `deploying` `forge_env`
    arrival it carries is `arrived` (or `deploy_failed`). Whether one moved."""
    moving = (StageArrival.stage_id == stage.id, StageArrival.state == "deploying",
              StageArrival.target_kind == "forge_env")
    sha = session.scalar(select(StageArrival.sha).where(*moving))
    # Compare first, unlocked (it may read the forge); then lock what is still moving.
    if sha is None or not carries(sha):
        return False
    arrival = session.scalar(select(StageArrival).where(*moving, StageArrival.sha == sha).with_for_update()
                             .execution_options(populate_existing=True))
    if arrival is None:
        return False
    if ok:
        _arrived(session, project, stage, arrival, _https(url))
    else:
        _deploy_failed(session, arrival, _report(report) or f"The deploy to {stage.name} failed.")
    return True


# ── The executor's side ──────────────────────────────────────────────────────────────────────────────


def _held(session: Session, task: Task, arrival_id: Any) -> StageArrival:
    try:
        key = UUID(str(arrival_id))
    except ValueError:
        raise LookupError("no such arrival") from None
    arrival = session.scalar(select(StageArrival).where(
        StageArrival.id == key, StageArrival.project_id == task.project_id).with_for_update())
    live = executor.open_task(session, task.project_id)
    if arrival is None or task.persona != executor.PERSONA or live is None or live.id != task.id:
        raise LookupError("no such arrival")
    return arrival


#: `pipeline.arrival_event` states, by the arrival state each settles.
_SETTLES: Mapping[str, str] = {"arrived": "deploying", "deploy_failed": "deploying",
                               "verified": "verifying", "verify_failed": "verifying"}


def apply_event(session: Session, task: Task, body: Mapping[str, Any]) -> dict[str, Any]:
    """`pipeline.arrival_event`: the executor's deploy or verify outcome. LookupError 404 (not this executor's),
    Conflict 409 (moved on), ValueError 422. Flushes; the route commits."""
    state = str(body.get("state") or "")
    if state not in _SETTLES:
        raise ValueError(f"unknown arrival state {state!r}")
    arrival = _held(session, task, body.get("arrival_id"))
    if arrival.state != _SETTLES[state]:
        raise Conflict(f"this arrival is {arrival.state}")
    project = session.get(Project, arrival.project_id)
    stage = session.get(ReleaseStage, arrival.stage_id)
    if project is None or stage is None:
        raise LookupError("no such arrival")
    report = _report(body.get("report"))
    if state == "arrived":
        _arrived(session, project, stage, arrival, _https(body.get("url")))
    elif state == "deploy_failed":
        _deploy_failed(session, arrival, report or f"The deploy to {stage.name} failed.")
    else:
        if state == "verified" and not _required_passed(session, arrival):
            # The verdict is the run's results, never the caller's word: a report alone opens no step.
            raise Conflict("not every required gate of this verify passed")
        run = session.get(GateRun, arrival.gate_run_id) if arrival.gate_run_id is not None else None
        if run is not None and run.state == "running":
            run.state, run.finished_at = ("passed" if state == "verified" else "failed"), utcnow()
        arrival.state, arrival.report = state, (report if state == "verify_failed" else None)
    session.flush()
    service.wake_tick(session)
    return {"ok": True, "state": arrival.state}


def _required_passed(session: Session, arrival: StageArrival) -> bool:
    """Whether every enabled, required gate of the arrival's verify has a `passed` result in its run."""
    required = {i for i, g in enumerate(arrival.after_gates or [])
                if g.get("enabled", True) and g.get("required", True)}
    if not required:
        return True
    if arrival.gate_run_id is None:
        return False
    passed = set(session.scalars(select(GateResult.position).where(
        GateResult.run_id == arrival.gate_run_id, GateResult.state == "passed")))
    return required <= passed


def work_item(session: Session, project: Project, caps: frozenset[str]) -> Optional[dict[str, Any]]:
    """The oldest arrival owed node work this toolbox can do: a driven deploy (`deploy` cap) or a verify (`verify`
    and `gates`). None when none is."""
    kinds = []
    if "deploy" in caps:
        kinds.append("deploying")
    if "verify" in caps and "gates" in caps:
        kinds.append("verifying")
    if not kinds:
        return None
    for arrival in session.scalars(select(StageArrival).where(
            StageArrival.project_id == project.id, StageArrival.state.in_(kinds)).order_by(StageArrival.created_at)):
        if arrival.state == "deploying" and arrival.target_kind not in DRIVEN:
            continue
        stage = session.get(ReleaseStage, arrival.stage_id)
        if stage is None:
            continue
        return _wire(session, project, stage, arrival)
    return None


def _wire(session: Session, project: Project, stage: ReleaseStage, arrival: StageArrival) -> dict[str, Any]:
    from app import forges  # noqa: PLC0415

    ref = forges.repo_ref_or_none(project)
    out: dict[str, Any] = {
        "mode": "deploy" if arrival.state == "deploying" else "verify", "release_id": None, "job_id": None,
        "arrival_id": str(arrival.id), "repo_url": project.repo_url, "forge": ref.forge if ref is not None else None,
        "project": project.name, "stage": stage.name, "stage_id": str(stage.id), "sha": arrival.sha,
        "branch": stages_mod.branch_of(project, stage), "version": arrival.version,
    }
    if arrival.state == "deploying":
        out["target"] = {"kind": arrival.target_kind, "config": stage.target_config or {},
                         "credential": stage.target_credential}
    else:
        run = session.get(GateRun, arrival.gate_run_id) if arrival.gate_run_id is not None else None
        if run is not None and run.state == "running":
            # The verify's time runs from when a node takes it, not from the arrival (`sweep`).
            run.created_at = utcnow()
        out.update({"url": arrival.url or stage.url or None, "environment": stage.deploy_environment,
                    "gates": arrival.after_gates or [],
                    "gate_run_id": str(arrival.gate_run_id) if arrival.gate_run_id is not None else None})
    return out


# ── The release tick's sweep ─────────────────────────────────────────────────────────────────────────


def _verify_budget(gates: Optional[list[dict[str, Any]]]) -> timedelta:
    seconds = sum(int(g.get("timeout_s") or 0) * (1 + int(g.get("retries") or 0)) for g in _enabled(gates))
    return timedelta(seconds=seconds) + VERIFY_SLACK


def sweep(session: Session, project: Project, now: datetime, *, may_file: bool = True) -> list[str]:
    """Fail what overran (`DEPLOY_WITHIN`, the verify budget); refile the executor for work still owed a node.
    `may_file`: False while the project's executors are cooling off (`tick._cooling`)."""
    acted: list[str] = []
    owed = False
    for arrival in session.scalars(select(StageArrival).where(
            StageArrival.project_id == project.id, StageArrival.state.in_(ARRIVAL_OPEN))
            .order_by(StageArrival.created_at).with_for_update()):
        stage = session.get(ReleaseStage, arrival.stage_id)
        name = stage.name if stage is not None else "the stage"
        if arrival.state == "deploying" and now - arrival.created_at >= DEPLOY_WITHIN:
            _deploy_failed(session, arrival, f"No deploy reached {name} in 2 hours.")
            acted.append(f"deploy_failed@{arrival.id}")
            continue
        if arrival.state == "verifying":
            run = session.get(GateRun, arrival.gate_run_id) if arrival.gate_run_id is not None else None
            began = run.created_at if run is not None else arrival.updated_at
            if began is not None and now - began >= _verify_budget(arrival.after_gates):
                _cancel_run(session, arrival)
                arrival.state, arrival.report = "verify_failed", f"Verify on {name} did not finish in time."
                acted.append(f"verify_failed@{arrival.id}")
                continue
        owed = owed or arrival.state == "verifying" or arrival.target_kind in DRIVEN
    if owed and may_file:
        executor.ensure_executor(session, project)
    session.flush()
    return acted


# ── The step out of a stage ──────────────────────────────────────────────────────────────────────────


def _passed(session: Session, arrival: StageArrival) -> int:
    if arrival.gate_run_id is None:
        return 0
    return int(session.scalar(select(func.count()).select_from(GateResult).where(
        GateResult.run_id == arrival.gate_run_id, GateResult.state == "passed")) or 0)


def step_open(session: Session, project: Project, source: ReleaseStage,
              sha: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Whether the step out of `source` may run: always without After-arrival gates; else only once `source`'s
    arrival of the head being promoted (`sha`; its newest, the head the CP last saw land) is `verified`. The reason
    a person reads when it may not."""
    closed = step_closed(session, project, source, sha)
    return (True, None) if closed is None else (False, closed[1])


def step_closed(session: Session, project: Project, source: ReleaseStage,
                sha: Optional[str] = None) -> Optional[tuple[str, str]]:
    """Why the step out of `source` may not run (`step_open`): its chip's one or two words and the full sentence;
    None while it may."""
    if not verifies(session, project, source):
        return None
    arrival = latest(session, source.id, sha=sha)
    state = arrival.state if arrival is not None else None
    if state == "verified":
        return None
    if arrival is not None and state == "verifying":
        total = len(_enabled(arrival.after_gates))
        return "Verifying", f"Verifying {source.name} — {_passed(session, arrival)} of {total} gates passed"
    if state == "verify_failed":
        return "Verify failed", f"Verify failed on {source.name}"
    if state == "deploy_failed":
        return "Deploy failed", f"Deploy failed on {source.name}"
    if state == "rolled_back":
        return "Rolling back", f"Rolling back {source.name}"
    return "Not verified", f"Waiting for {source.name} to verify"


# ── Roll back ────────────────────────────────────────────────────────────────────────────────────────


def _previous(session: Session, arrival: StageArrival) -> Optional[StageArrival]:
    """The stage's newest arrival before `arrival` that reached it: the version a roll back returns to."""
    return session.scalar(select(StageArrival).where(
        StageArrival.stage_id == arrival.stage_id, StageArrival.state.in_(GOOD),
        StageArrival.created_at < arrival.created_at,
    ).order_by(StageArrival.created_at.desc(), StageArrival.id.desc()).limit(1))


def _return_to(session: Session, arrival: StageArrival) -> Optional[tuple[Optional[str], str]]:
    """`(the version a roll back records, the words the board names it by)`, or None when the stage had no earlier
    version. The newest earlier arrival that reached the stage; else, for a stage whose earlier versions went live
    before arrivals were recorded, the newest earlier release that went live there with no arrival of its own (one
    with an arrival that failed is not a version to return to)."""
    previous = _previous(session, arrival)
    if previous is not None:
        return previous.version, previous.version or previous.sha[:10]
    earlier = session.scalar(select(Release).where(
        Release.to_stage_id == arrival.stage_id, Release.state.in_(tuple(lifecycle.ARRIVED)),
        Release.requested_at < arrival.created_at,
        ~select(StageArrival.id).where(StageArrival.release_id == Release.id).exists(),
    ).order_by(Release.requested_at.desc(), Release.id.desc()).limit(1))
    if earlier is None:
        return None
    return earlier.version, earlier.version or (earlier.merge_sha or "")[:10] or "the previous version"


def _is_rollback(session: Session, arrival: StageArrival) -> bool:
    """A roll back's own arrival: rolling it back would re-apply the change it undid."""
    return arrival.release_id is not None and session.scalar(select(Release.kind).where(
        Release.id == arrival.release_id)) == lifecycle.KIND_ROLLBACK


def _step_into(session: Session, project: Project, stage: ReleaseStage) -> Any:
    from app.pipeline import gate as gate_mod  # noqa: PLC0415 — the pipeline package imports this one

    return gate_mod.step_into(gate_mod.stages_of(session, project), stage.id)


def may_roll_back(principal: Optional[Principal], project: Project, step: Any) -> bool:
    """Who may press the step into the stage may roll it back (`access`, unchanged)."""
    from app.pipeline import gate as gate_mod  # noqa: PLC0415
    from app.releases import access  # noqa: PLC0415

    if step is None:
        return False
    writer = access.as_writer(principal, project.org_id) if principal is not None else None
    return writer is not None and gate_mod.may_act(writer, project.id, step)


def offer(session: Session, project: Project, stage: ReleaseStage, arrival: StageArrival,
          principal: Optional[Principal]) -> tuple[bool, Optional[str]]:
    """`(offered, the version it returns to)` on the board for `principal`."""
    if arrival.state not in FAILED or _is_rollback(session, arrival):
        return False, None
    to = _return_to(session, arrival)
    if to is None or not may_roll_back(principal, project, _step_into(session, project, stage)):
        return False, None
    return True, to[1]


def rollback(session: Session, project: Project, principal: Optional[Principal], stage_id: Any) -> Release:
    """Roll `stage` back to its previous version (module docstring). `service.Refused` 404/403/409. Flushes."""
    stages = stages_mod.branch_stages(stages_mod.materialise(session, project))
    stage = next((s for s in stages if str(s.id) == str(stage_id)), None)
    if stage is None:
        raise service.Refused(404)
    if not may_roll_back(principal, project, _step_into(session, project, stage)):
        raise service.Refused(403)
    service.lock_target(session, stage.id)
    arrival = latest(session, stage.id)
    to = _return_to(session, arrival) if arrival is not None and arrival.state in FAILED \
        and not _is_rollback(session, arrival) else None
    if arrival is None or to is None:
        raise service.Refused(409, {"code": "not_offered"})
    if (open_ := service.open_releases_into(session, project.id, stage.id)):
        raise service.Refused(409, {"code": "open", "release_id": open_[-1].id})
    now = utcnow()
    settings = {**snapshot.take(project, stage, stage, stages, session), "gates": [],
                "reverts": arrival.sha, "arrival_id": str(arrival.id)}
    lifecycle.check("request", lifecycle.REQUESTED, lifecycle.QUEUED)
    release = Release(
        org_id=project.org_id, project_id=project.id, kind=lifecycle.KIND_ROLLBACK, from_stage_id=stage.id,
        to_stage_id=stage.id, state=lifecycle.QUEUED, requested_by_id=principal.user_id if principal else None,
        requested_at=now, queued_at=now, settings=settings, rolls_back_id=arrival.release_id,
        version=to[0],
    )
    session.add(release)
    session.flush()
    shas = rolled_back_of(session, release)
    if shas:
        # A survey saw them on the branch; after the revert they wait again (`rolled_back_shas`).
        session.execute(delete(ReleaseContained).where(
            ReleaseContained.project_id == project.id, ReleaseContained.stage_id == stage.id,
            ReleaseContained.merge_sha.in_(shas)))
    service._queue(session, project, release, release.requested_by_id)
    service.wake_tick(session)
    session.flush()
    return release


def rolled_back_of(session: Session, rollback_release: Release) -> list[str]:
    """The change shas the release a roll back undoes carried."""
    if rollback_release.rolls_back_id is None:
        return []
    return list(session.scalars(select(ReleaseChange.merge_sha).where(
        ReleaseChange.release_id == rollback_release.rolls_back_id)))


def rolled_back_release_ids() -> Any:
    """SQL: the ids of releases a roll back still undoes (not failed or canceled, not yet re-applied)."""
    return select(Release.rolls_back_id).where(
        Release.kind == lifecycle.KIND_ROLLBACK, Release.rolls_back_id.is_not(None),
        Release.state.in_(_ROLLBACK_LIVE), Release.reapplied_by_id.is_(None))


def rolled_back_shas(session: Session, project_id: int, stage_id: UUID) -> set[str]:
    """The change shas a roll back took off `stage_id`: a survey's sighting of them on the branch does not count."""
    return set(session.scalars(select(ReleaseChange.merge_sha).join(Release, Release.id == ReleaseChange.release_id)
                               .where(Release.project_id == project_id, Release.to_stage_id == stage_id,
                                      Release.id.in_(rolled_back_release_ids()))))


def reapply(session: Session, project: Project, target: ReleaseStage) -> list[tuple[int, str]]:
    """`(rollback id, its merge commit)` of each roll back on `target` a promote must re-apply (risk R1), oldest
    first."""
    return [(r.id, r.merge_sha) for r in session.scalars(select(Release).where(
        Release.project_id == project.id, Release.to_stage_id == target.id, Release.kind == lifecycle.KIND_ROLLBACK,
        Release.state.in_(tuple(lifecycle.ARRIVED) + (lifecycle.DEPLOYING,)), Release.reapplied_by_id.is_(None),
        Release.merge_sha.is_not(None)).order_by(Release.id)) if r.merge_sha]


def on_merged(session: Session, project: Project, release: Release, *, noop: bool = False) -> None:
    """A release merged: a roll back marks the arrival it undid `rolled_back`; a promote that re-applied roll backs
    records itself on them (`settings.reapply`)."""
    if release.kind == lifecycle.KIND_ROLLBACK:
        arrival_id = (release.settings or {}).get("arrival_id")
        undone = session.get(StageArrival, UUID(str(arrival_id))) if arrival_id else None
        if undone is not None and undone.state in FAILED:
            undone.state = "rolled_back"
    for rollback_id in [] if noop else (release.settings or {}).get("reapply") or []:
        undone_by = session.get(Release, int(rollback_id))
        if undone_by is not None and undone_by.reapplied_by_id is None:
            undone_by.reapplied_by_id = release.id
    session.flush()


def holds_auto(session: Session, project: Project, target: ReleaseStage, newest_sha: str) -> bool:
    """The loop guard (risk R5): the newest waiting change was rolled back off `target`, so an auto step does not
    promote it again until a newer change arrives or a person presses."""
    return session.scalar(select(func.count()).select_from(ReleaseChange).where(
        ReleaseChange.merge_sha == newest_sha,
        ReleaseChange.release_id.in_(select(Release.rolls_back_id).where(
            Release.project_id == project.id, Release.to_stage_id == target.id,
            Release.kind == lifecycle.KIND_ROLLBACK, Release.state.in_(_ROLLBACK_LIVE),
            Release.reapplied_by_id.is_(None))))) > 0


# ── The board ────────────────────────────────────────────────────────────────────────────────────────


def board_out(session: Session, project: Project, stages: list[ReleaseStage],
              principal: Optional[Principal]) -> dict[UUID, dict[str, Any]]:
    """Each stage's newest arrival as the board shows it (architecture §3.3); a stage with none has no key."""
    by_id = {s.id: s for s in stages}
    if not by_id:
        return {}
    rows = list(session.scalars(select(StageArrival).where(StageArrival.stage_id.in_(list(by_id)))
                                .distinct(StageArrival.stage_id)
                                .order_by(StageArrival.stage_id, StageArrival.created_at.desc(),
                                          StageArrival.id.desc())))
    out: dict[UUID, dict[str, Any]] = {}
    for row in rows:
        allowed, to_version = offer(session, project, by_id[row.stage_id], row, principal)
        out[row.stage_id] = {
            "state": row.state, "version": row.version, "sha": row.sha, "passed": _passed(session, row),
            "total": len(_enabled(row.after_gates)), "report": row.report,
            "rollback_allowed": allowed, "rollback_to_version": to_version,
        }
    return out
