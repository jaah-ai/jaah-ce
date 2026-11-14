"""The release executor's two verbs, CP side: what to carry out next (`release.next`) and what happened
(`release.event`), plus the merge lane's hooks into a release PR (`lane_admits`, `on_lane_merged`, `on_lane_failed`)
and the forge CLI's question whether a release PR is the caller's to open or merge (`authorize`).

The caller is `tools/release.py` on a node, through the daemon's TASK-scoped verbs (`routes/agent_release.py`), never
the model. `next` hands out, oldest first: a `merging` release no live run carries (its executor ended, or this run
asks again — retry is idempotent node-side), then a `queued` one, claiming it (`queued → merging`), then an open
executor job (`survey`) this run has not claimed. A release whose target is frozen, or that waits behind another into
its stage (`judge.ahead_of`: one merging or deploying, or under "In order" an older open one), is never handed out (it
stays queued); one that would put an older version over a newer one fails `older_than_live` at its claim. A release PR
the merge lane holds is the lane's, so it is never handed out again.

Events advance the release through `lifecycle.check`; one on a release that moved on (another run's, or no longer
`merging`) is a `Conflict` (409): the executor drops it. A merged hotfix files the back-merge (Production → Dev) at
once, and a merged pick its own (target → source), so the source holds the target's merge commit and the next promote
merges cleanly.

Whether a merged release waits for its deploy (`deploying`) or is done (`shipped`) is `deploy_tracked`: deploy events
connected (or Easy Mode) and the target is Production or names its deploy environment.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, object_session

from app import forges, merge_lane_state, secret_redact
from app.clock import utcnow
from app.models.merge_lane import MergeLaneRow
from app.models.projects import Project
from app.models.releases import Release, ReleaseChange, ReleaseContained, ReleaseJob, ReleaseStage
from app.models.tasks import Task
from app.pipeline_gates import defaults as gate_defaults
from app.releases import arrivals, executor, jobs, lifecycle, snapshot, stages as stages_mod, waiting as waiting_mod
from app.releases import judge, override as override_mod
from app.releases import service
from app.releases.service import _move as move

log = logging.getLogger(__name__)

#: `failed.kind` → the one action a person can take (`lifecycle.FAILURE_ACTIONS`).
FAILURE_ACTION: Mapping[str, str] = {
    "conflict": "open_pr", "checks_red": "open_pr", "forge_refused": "open_pr",
    "branch_missing": "retry", "no_login": "retry", "node": "retry", "not_started": "retry",
    "older_than_live": "open_run",
}
#: A failure message as stored: one short paragraph, secret-redacted.
MESSAGE_MAX = 500
#: The merge lane's row states that hold a PR (it is the lane's to merge, not the executor's).
_LANE_HOLDING = ("queued", "testing")
#: The executor's personas allowed to open or merge a release PR (`authorize`): the executor, and the lane merging it.
_RELEASE_WRITERS = (executor.PERSONA, "merge_lane")
#: `release.authorize`'s base-less question: may this run start a CI run on the pinned branch?
DISPATCH = "dispatch"


class Conflict(Exception):
    """The release or job moved on: the executor drops it (409)."""


def release_branch(project: Project, release_id: int) -> str:
    """The pinned branch a release PR merges from — under the project's branch prefix, so the push guard admits it."""
    return f"{project.branch_prefix or ''}release/{release_id}"


# ── release.next ─────────────────────────────────────────────────────────────────────────────────────


def _project(session: Session, task: Task) -> Project:
    project = session.get(Project, task.project_id)
    if project is None or not stages_mod.has_repo(project):
        raise LookupError("no repository for this release run")
    return project


def _lane_holds(session: Session, project_id: int, release: Release) -> bool:
    if release.pr_number is None:
        return False
    return session.scalar(select(MergeLaneRow.id).where(
        MergeLaneRow.project_id == project_id, MergeLaneRow.pr_number == release.pr_number,
        MergeLaneRow.state.in_(_LANE_HOLDING)).limit(1)) is not None


def frozen(session: Session, release: Release, by_id: dict[Any, ReleaseStage], now: Optional[datetime] = None, *,
           project: Optional[Project] = None) -> bool:
    """Whether a freeze holds `release` at its claim, or holds its merging PR in the lane (`service.release_freeze`:
    one queued before the occurrence began passes a `let_queued` freeze), or its soak or allowed hours hold it before
    its claim (`judge.first_blocking`). A deploying release always finishes, a back-merge only writes Dev, and a roll
    back returns a stage to the version it held (`arrivals.rollback`)."""
    if release.kind in (lifecycle.KIND_BACKMERGE, lifecycle.KIND_ROLLBACK) or release.state not in (
            lifecycle.QUEUED, lifecycle.SCHEDULED, lifecycle.MERGING):
        return False
    target = by_id.get(release.to_stage_id)
    if target is None:
        return False
    project = project or session.scalar(select(Project).where(Project.id == release.project_id))
    return project is not None and judge.first_blocking(session, project, release, target, now) is not None


def _carried_by_live_run(session: Session, task: Task, release: Release) -> bool:
    """Whether another, still open executor run carries `release` (it is not this run's to take)."""
    if release.task_id is None or release.task_id == task.id:
        return False
    owner = session.get(Task, release.task_id)
    return owner is not None and owner.state not in ("done", "system_failed")


#: How many earlier arrived promotes into the stage a promote re-checks (`_listed_promote_rows`): enough to correct the
#: last few false claims, bounded so the wire and the executor's git work do not grow with the project's history.
_RECHECKED_PROMOTES = 10
_SHA = re.compile(r"[0-9a-f]{7,64}")


def _listed_promote_rows(session: Session, release: Release) -> Any:
    """SQL: the change rows of `release` and of the last `_RECHECKED_PROMOTES` promotes that arrived on its target
    (live or shipped, not rolled back) — the changes the board counts as on the target once `release` merges. A
    change any hotfix or pick carried is never judged: it travels as a cherry-pick, so its sha is in no branch's
    history downstream of the pick, and a later promote carrying the picked content would read it as missing."""
    promotes = list(session.scalars(select(Release.id).where(
        Release.project_id == release.project_id, Release.to_stage_id == release.to_stage_id,
        Release.kind == lifecycle.KIND_PROMOTE, Release.state.in_(tuple(lifecycle.ARRIVED)),
        Release.id.not_in(arrivals.rolled_back_release_ids())).order_by(Release.id.desc()).limit(_RECHECKED_PROMOTES)))
    picked = select(ReleaseChange.merge_sha).join(Release, Release.id == ReleaseChange.release_id).where(
        Release.project_id == release.project_id, Release.kind.in_((lifecycle.KIND_HOTFIX, lifecycle.KIND_PICK)),
        Release.state.in_(tuple(lifecycle.ARRIVED)))
    return and_(ReleaseChange.release_id.in_([release.id, *promotes]), ReleaseChange.merge_sha.not_in(picked))


def _listed_shas(session: Session, release: Release) -> set[str]:
    return set(session.scalars(select(ReleaseChange.merge_sha).where(_listed_promote_rows(session, release))))


def _drop_uncarried(session: Session, release: Release, reported: Any) -> None:
    """A promote pinned short of a change it listed (a certified pin older than the source's tip), or an earlier
    promote recorded one it never shipped: those rows go, so the change waits for the next release instead of reading
    as on the target (`tools/release.py` `uncarried`). Only rows `_listed_promote_rows` names are touched."""
    if release.kind != lifecycle.KIND_PROMOTE or not isinstance(reported, list):
        return
    shas = {s.lower() for s in reported if isinstance(s, str) and _SHA.fullmatch(s.lower())}
    if not shas:
        return
    rows = _listed_promote_rows(session, release)
    for release_id, sha in session.execute(select(ReleaseChange.release_id, ReleaseChange.merge_sha).where(
            rows, ReleaseChange.merge_sha.in_(shas))).all():
        log.info("release %s: change %s is not in promote %s's pin; it waits again", release_id, sha, release.id)
    session.execute(delete(ReleaseChange).where(rows, ReleaseChange.merge_sha.in_(shas)))


def _release_wire(session: Session, project: Project, release: Release, stages: list[ReleaseStage]) -> dict[str, Any]:
    by_id = {s.id: s for s in stages}
    source, target = by_id.get(release.from_stage_id), by_id.get(release.to_stage_id)
    # The branches it was requested with (`snapshot`): a save since never moves a running release.
    ends = snapshot.branches(project, release, source, target)
    if ends is None:
        raise Conflict(f"release {release.id}: a stage it names was removed")
    settings = release.settings or {}
    to_is_production = bool(settings["to_is_production"]) if "to_is_production" in settings else (
        target is not None and target.id == stages[-1].id)
    hotfix_sha = change_pr = None
    pick_shas: Optional[list[str]] = None
    if release.kind in (lifecycle.KIND_HOTFIX, lifecycle.KIND_PICK):
        # In the order it was requested: a pick's oldest merged first.
        if release.kind == lifecycle.KIND_PICK:
            # Its own rows, not their owners: a change whose task went since still ships, never silently drops.
            pick_shas = list(session.scalars(select(ReleaseChange.merge_sha).where(
                ReleaseChange.release_id == release.id).order_by(ReleaseChange.id)))
        elif carried := waiting_mod.carried(session, project, [release.id]).get(release.id) or []:
            hotfix_sha, change_pr = carried[0].merge_sha, carried[0].pr_url
    reverts: Optional[dict[str, Any]] = None
    reapply_shas: list[str] = []
    if release.kind == lifecycle.KIND_ROLLBACK:
        reverts = {"merge_sha": settings.get("reverts"), "version": release.version}
    elif release.kind == lifecycle.KIND_PROMOTE and target is not None:
        # A change a roll back took off the target comes back with the next promote (risk R1): the pin reverts the
        # roll back, and its merge records itself on it (`arrivals.on_merged`).
        undone = arrivals.reapply(session, project, target)
        reapply_shas = [sha for _, sha in undone]
        if [i for i, _ in undone] != settings.get("reapply", []):
            release.settings = {**settings, "reapply": [i for i, _ in undone]}
    ref = forges.repo_ref_or_none(project)
    lane = merge_lane_state.lane_state(session, project).active
    certified_sha = None
    # An untested release (`service.deploy`'s `untested_reason`) ships the source's head, tested or not.
    if lane and release.kind == lifecycle.KIND_PROMOTE and ends[0] == project.integration_branch \
            and not override_mod.untested_of(release):
        certified_sha = merge_lane_state.latest_certified_sha(session, project)
    listed: list[str] = []
    if release.kind == lifecycle.KIND_PROMOTE and release.head_sha is None:
        listed = sorted(_listed_shas(session, release))
    return {
        "release_id": release.id, "job_id": None, "mode": release.kind,
        "repo_url": project.repo_url, "forge": ref.forge if ref is not None else None,
        "from_stage_id": str(release.from_stage_id) if release.from_stage_id else None,
        "to_stage_id": str(release.to_stage_id) if release.to_stage_id else None,
        "from_branch": ends[0], "to_branch": ends[1],
        # Only Production's branch may be corrected to `master` (`branch_detected`), and only while it exists.
        "to_is_production": to_is_production and target is not None,
        "hotfix_merge_sha": hotfix_sha, "change_pr_url": change_pr, "pick_merge_shas": pick_shas,
        "lane": lane,
        # A promote out of the integration branch with the lane on pins the newest commit the lane certified, not the
        # branch's raw tip: its tree is already proven green, so neither the lane nor CI's `test-stack` runs the
        # whole suite on it again (`tools/release.py` `pin`).
        "certified_sha": certified_sha,
        # The changes counted as shipping with this promote or already on its target: the pin names those it and the
        # target lack (`uncarried_shas`), which wait for the next release (`_drop_uncarried`). Empty once pinned.
        "listed_shas": listed,
        "release_branch": release_branch(project, release.id), "merge_method": "merge",
        "head_sha": release.head_sha, "pr_number": release.pr_number,
        "survey": None, "reverts": reverts, "reapply_shas": reapply_shas,
    }


def _job_wire(session: Session, project: Project, job: ReleaseJob, stages: list[ReleaseStage]) -> dict[str, Any]:
    ref = forges.repo_ref_or_none(project)
    out: dict[str, Any] = {
        "release_id": None, "job_id": str(job.id), "mode": job.mode,
        "repo_url": project.repo_url, "forge": ref.forge if ref is not None else None,
        "release_branch": None, "merge_method": "merge", "survey": None,
    }
    if job.mode == jobs.SURVEY:
        known = waiting_mod.merged_changes(session, project)
        out["survey"] = {
            "stages": [{"stage_id": str(s.id), "branch": stages_mod.branch_of(project, s)} for s in stages],
            "shas": [c.merge_sha for c in known],
            "release_merge_shas": [sha for sha in session.scalars(select(Release.merge_sha).where(
                Release.project_id == project.id, Release.merge_sha.is_not(None)))],
            # Every release's pin: a merge of one into the source is Jaah's own (a back-merge), not an extra commit.
            "release_pins": [sha for sha in session.scalars(select(Release.head_sha).where(
                Release.project_id == project.id, Release.head_sha.is_not(None)))],
        }
    return out


#: The capability a toolbox announces when it runs a release step's gate list itself (the node gate driver).
CAP_GATES = "gates"
#: ... and when it carries a `rollback` release (a revert of the failed merge, `arrivals.rollback`). Its `deploy` and
#: `verify` caps take an arrival's work (`arrivals.work_item`).
CAP_ROLLBACK = "rollback"
#: Why a release waits for a toolbox that can run its gates (architecture §3.4, risk R4).
HOLD_TOO_OLD = "This node's Jaah is too old for this project's gates. Update the node."
#: `failure.kind` while a release waits on that (it stays queued; a capable run claims it and clears it).
FAILURE_NODE_TOO_OLD = "node_too_old"


def _legacy_release_gates(gates: Any) -> bool:
    """Whether a toolbox that predates gates runs the release's list faithfully: no list (a release requested before
    gates existed) or exactly the D14 default — one enabled, required CI gate waiting for every check, today's
    `pr checks --watch`."""
    if gates is None:
        return True
    if not isinstance(gates, list) or len(gates) != 1:
        return False
    gate = gates[0]
    config = gate.get("config") or {}
    return (gate.get("kind") == "ci" and bool(gate.get("enabled")) and bool(gate.get("required"))
            and config.get("mode") == "wait" and bool(config.get("all_checks")) and not config.get("workflow"))


def _needs_rollback_cap(session: Session, project: Project, release: Release,
                        by_id: dict[Any, ReleaseStage]) -> bool:
    """A roll back, or a promote that must re-apply one (`arrivals.reapply`): only a toolbox announcing `rollback`
    carries it faithfully."""
    if release.kind == lifecycle.KIND_ROLLBACK:
        return True
    target = by_id.get(release.to_stage_id)
    return release.kind == lifecycle.KIND_PROMOTE and target is not None \
        and bool(arrivals.reapply(session, project, target))


def _hold_too_old(release: Release) -> None:
    failure = release.failure or {}
    if failure.get("kind") != FAILURE_NODE_TOO_OLD:
        release.failure = {"kind": FAILURE_NODE_TOO_OLD, "message": HOLD_TOO_OLD, "action": "retry"}


def _gates_wire(session: Session, project: Project, release: Release,
                stages: list[ReleaseStage]) -> dict[str, Any]:
    """The release step's gates as requested (`snapshot`), the run their results go to, and what was asked when
    the step was pressed; nothing for a release requested before gates existed (it runs today's path)."""
    gates = (release.settings or {}).get("gates")
    if gates is None:
        return {"gates": None, "gate_run_id": None, "inputs": release.inputs}
    from app.pipeline_gates import results as gate_results  # noqa: PLC0415

    target = next((s for s in stages if s.id == release.to_stage_id), None)
    run = gate_results.running_or_open(session, project, target, "before", "release", str(release.id), gates,
                                          sha=release.head_sha) if gates else None
    # `stage_url`: a probe gate's base, the address of the stage this step releases into.
    return {"gates": gates, "gate_run_id": str(run.id) if run is not None else None, "inputs": release.inputs,
            "stage_url": (target.url if target is not None else None) or None}


def next_work(session: Session, task: Task, caps: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The run's next release or job (claimed), or `{"mode": None}` when nothing is due. Flushes; the route commits.

    `caps`: what the calling toolbox supports. With `gates` a release carries its step's gate list (`gates`,
    `gate_run_id`, `inputs`). Without it a release whose list is not the D14 default is not handed out: it stays
    where it is, its `failure` saying why (`HOLD_TOO_OLD`)."""
    project = _project(session, task)
    stages = stages_mod.branch_stages(stages_mod.materialise(session, project))
    by_id = {s.id: s for s in stages}
    candidates = session.scalars(
        select(Release).where(
            Release.project_id == project.id, Release.state.in_((lifecycle.MERGING, lifecycle.QUEUED)),
        ).order_by(Release.state != lifecycle.MERGING, Release.id).with_for_update(skip_locked=True)
    ).all()
    for release in candidates:
        if frozen(session, release, by_id, project=project) or _carried_by_live_run(session, task, release):
            continue
        if release.state == lifecycle.MERGING and _lane_holds(session, project.id, release):
            continue
        if (CAP_GATES not in caps and not _legacy_release_gates((release.settings or {}).get("gates"))) or (
                CAP_ROLLBACK not in caps and _needs_rollback_cap(session, project, release, by_id)):
            _hold_too_old(release)
            session.flush()
            continue
        if (release.failure or {}).get("kind") == FAILURE_NODE_TOO_OLD:
            release.failure = None
        if release.state == lifecycle.QUEUED:
            # A request into the stage holds it: claim on a later call. Never two into one stage at once; "In order"
            # waits for the older ones (a back-merge too).
            if release.to_stage_id is not None and not service.lock_target(session, release.to_stage_id,
                                                                           skip_locked=True):
                continue
            if judge.ahead_of(session, release) is not None:
                continue
            if judge.release_downgrade(session, project, release):
                target = by_id.get(release.to_stage_id)
                mark_failed(release, "older_than_live",
                            f"{target.name if target is not None else 'The stage'} already has a newer version")
                session.flush()
                continue
            move(release, "claim", lifecycle.MERGING)
        release.task_id = task.id
        session.flush()
        wire = _release_wire(session, project, release, stages)
        return {**wire, **_gates_wire(session, project, release, stages)} if CAP_GATES in caps else wire
    item = arrivals.work_item(session, project, caps)
    if item is not None:
        return item
    now = utcnow()
    for job in session.scalars(
        select(ReleaseJob).where(ReleaseJob.project_id == project.id, ReleaseJob.mode == jobs.SURVEY,
                                 ReleaseJob.done_at.is_(None))
        .order_by(ReleaseJob.requested_at).with_for_update(skip_locked=True)
    ):
        if not jobs.claimable(job, now, task.id):
            continue
        job.claimed_at = now
        job.payload = {**(job.payload or {}), "claimed_by": task.id}
        session.flush()
        return _job_wire(session, project, job, stages)
    return {"mode": None}


# ── release.event ────────────────────────────────────────────────────────────────────────────────────


def _message(value: Any) -> str:
    return secret_redact.redacted(str(value or "").strip())[:MESSAGE_MAX]


def deploy_tracked(session: Session, project: Project, stage: ReleaseStage, stages: list[ReleaseStage]) -> bool:
    """A merged release to `stage` waits for its deploy (`deploying`) rather than ending `shipped`: its ledger anchor,
    or its arrival on a target the executor drives."""
    if stages_mod.is_integration(stage):
        return False
    if gate_defaults.target_kind(stage) in arrivals.DRIVEN:
        return True  # the executor drives the target: live once its arrival reports (`arrivals`)
    from app.releases.views import deploy_events

    if deploy_events(session, project) == "not_connected":
        return False
    return stage.id == stages[-1].id or bool(stage.deploy_environment)


def _file_backmerge(session: Session, project: Project, stages: list[ReleaseStage], merged: Release) -> None:
    """After a hotfix merges into Production, or a pick into its target: a system back-merge of that branch into the
    one the changes came from (Production → Dev; a pick's target → its source; a back-merge re-checks its own pair),
    queued at once (no approval, no freeze — it only writes the source, which already holds every change it carries:
    it moves the merge base, so the next promote does not conflict with the cherry-picks). One open release per
    target: an open one to that stage already carries it."""
    by_id = {s.id: s for s in stages}
    if merged.kind == lifecycle.KIND_HOTFIX:
        dev, production = stages[0], stages[-1]
    else:
        source, target = by_id.get(merged.from_stage_id), by_id.get(merged.to_stage_id)
        if merged.kind == lifecycle.KIND_PICK:
            source, target = target, source
        if source is None or target is None:
            return
        production, dev = source, target
    service.lock_target(session, dev.id)  # beside a concurrent request into the same stage (R6)
    open_state = session.scalar(select(Release.state).where(
        Release.project_id == project.id, Release.to_stage_id == dev.id,
        Release.state.in_(lifecycle.OPEN)).limit(1))
    if open_state is not None:
        # A queued back-merge pins later, so it carries this merge too. One already pinned may not: when it merges,
        # `mark_merged` files the next one (a back-merge that merged something always re-checks once).
        return
    lifecycle.check("request", lifecycle.REQUESTED, lifecycle.QUEUED)
    backmerge = Release(
        org_id=project.org_id, project_id=project.id, kind=lifecycle.KIND_BACKMERGE,
        from_stage_id=production.id, to_stage_id=dev.id, state=lifecycle.QUEUED,
        requested_by_id=None, requested_at=utcnow(), queued_at=utcnow(), task_id=merged.task_id,
        settings=snapshot.take(project, production, dev, stages, session),
    )
    session.add(backmerge)
    session.flush()
    task_id = executor.ensure_executor(session, project, merged.requested_by_id)
    if task_id is not None:
        backmerge.task_id = task_id


_VERSION = re.compile(r"\d{1,9}(?:\.\d{1,9}){1,3}")


def stamp_version(session: Session, release: Release, reported: Any = None) -> None:
    """`release.version`: the version the executor read off the pinned head's tag (at the pin, or by the merge when
    the tag landed in between), else — once — `#N`, the project's Nth release into this stage. A number never
    replaces a version."""
    if isinstance(reported, str) and _VERSION.fullmatch(reported):
        release.version = reported
    elif release.version is None:
        nth = session.scalar(select(func.count()).select_from(Release).where(
            Release.project_id == release.project_id, Release.id <= release.id,
            Release.to_stage_id.is_not_distinct_from(release.to_stage_id)))
        release.version = f"#{nth}"


def mark_merged(session: Session, project: Project, release: Release, merge_sha: str, *, noop: bool = False,
                version: Any = None) -> None:
    """`merging → deploying|shipped` with its merge commit; a hotfix or a pick files its back-merge. `noop`: the target
    already held everything (nothing merged, so no deploy follows): `shipped`."""
    stages = stages_mod.branch_stages(stages_mod.stages_of(session, project.id))
    target = next((s for s in stages if s.id == release.to_stage_id), None)
    tracked = not noop and target is not None and deploy_tracked(session, project, target, stages)
    state = lifecycle.DEPLOYING if tracked else lifecycle.SHIPPED
    move(release, "merged", state)
    release.merge_sha = merge_sha
    release.failure = None
    stamp_version(session, release, version)
    session.flush()
    arrivals.on_merged(session, project, release, noop=noop)
    if target is not None and not noop:
        arrivals.land(session, project, target, merge_sha, release=release, tracked=tracked,
                      version=release.version)
    if len(stages) >= 2 and (release.kind == lifecycle.KIND_HOTFIX
                             or (release.kind in (lifecycle.KIND_PICK, lifecycle.KIND_BACKMERGE) and not noop)):
        _file_backmerge(session, project, stages, release)


def mark_failed(release: Release, kind: str, message: Any, url: Optional[str] = None) -> None:
    """`merging → failed`, with the one action that answers `kind`."""
    if kind not in FAILURE_ACTION:
        raise ValueError(f"unknown failure kind {kind!r}")
    move(release, "fail", lifecycle.FAILED)
    failure: dict[str, Any] = {"kind": kind, "message": _message(message), "action": FAILURE_ACTION[kind]}
    if url:
        failure["url"] = url
    release.failure = failure
    # Its gate run ends with it (one that failed before its PR never ran its gates).
    session = object_session(release)
    if session is not None:
        from app.pipeline_gates import results as gate_results  # noqa: PLC0415

        gate_results.cancel_runs(session, release.project_id, "release", str(release.id))


def _held_release(session: Session, task: Task, release_id: Any) -> Release:
    release = session.scalar(select(Release).where(
        Release.id == int(release_id), Release.project_id == task.project_id).with_for_update())
    if release is None:
        raise LookupError("no such release")
    if release.state != lifecycle.MERGING or release.task_id != task.id:
        raise Conflict(f"release {release.id} is {release.state}, not this run's to carry")
    return release


def _detect_branch(session: Session, project: Project, body: Mapping[str, Any]) -> None:
    """`branch_detected`: the stage's default branch does not exist and `master` does — the stage takes it."""
    stages = stages_mod.branch_stages(stages_mod.stages_of(session, project.id))
    stage = next((s for s in stages if str(s.id) == str(body["stage_id"])), None)
    branch = str(body.get("branch") or "").strip()
    if stage is None or stage.id != stages[-1].id or stages_mod.is_integration(stage) or not branch:
        raise ValueError("branch_detected names the Production stage and a branch")
    stage.branch = branch


def _job_event(session: Session, task: Task, project: Project, body: Mapping[str, Any]) -> dict[str, Any]:
    job = session.scalar(select(ReleaseJob).where(
        ReleaseJob.id == UUID(str(body["job_id"])), ReleaseJob.project_id == project.id).with_for_update())
    if job is None:
        raise LookupError("no such job")
    if job.done_at is not None or (job.payload or {}).get("claimed_by") != task.id:
        raise Conflict(f"job {job.id} is not this run's")
    event = body.get("event")
    payload = dict(job.payload or {})
    if event == "survey" and job.mode == "survey":
        stages = stages_mod.branch_stages(stages_mod.stages_of(session, project.id))
        index = next((i for i, s in enumerate(stages) if str(s.id) == str(body.get("stage_id"))), None)
        if index is None or index == 0:
            raise ValueError("survey names a stage after Dev")
        # A change a roll back took off the stage is still in its branch's history: it waits again all the same.
        undone = arrivals.rolled_back_shas(session, project.id, stages[index].id)
        shas = [sha for sha in (str(s).lower() for s in body.get("contained") or [] if isinstance(s, str)
                                and len(s) <= 64) if sha not in undone]
        if shas:
            session.execute(pg_insert(ReleaseContained).values([
                {"org_id": project.org_id, "project_id": project.id, "stage_id": stages[index].id, "merge_sha": sha}
                for sha in dict.fromkeys(shas)
            ]).on_conflict_do_nothing())
        source = str(stages[index - 1].id)
        payload["commits_ahead"] = {**(payload.get("commits_ahead") or {}), source: int(body.get("commits_ahead") or 0)}
        extra = body.get("extra_commits")
        payload["extra_commits"] = {**(payload.get("extra_commits") or {}),
                                    source: int(extra if extra is not None else body.get("commits_ahead") or 0)}
        if body.get("last"):
            job.done_at = utcnow()
    elif event == "branch_detected":
        _detect_branch(session, project, body)
    else:
        raise ValueError(f"event {event!r} does not apply to a {job.mode} job")
    job.payload = payload
    session.flush()
    return {"applied": True, "job_id": str(job.id), "done": job.done_at is not None}


def apply_event(session: Session, task: Task, body: Mapping[str, Any]) -> dict[str, Any]:
    """One executor event (module docstring). `ValueError` for a shape no event means (422), `LookupError` for a
    release or job this project does not hold (404), `Conflict` for one that moved on (409). Flushes."""
    project = _project(session, task)
    if body.get("job_id"):
        return _job_event(session, task, project, body)
    if body.get("release_id") is None:
        raise ValueError("an event names its release_id or job_id")
    release = _held_release(session, task, body["release_id"])
    event = body.get("event")
    if event == "pinned":
        release.head_sha = str(body["head_sha"]).lower()
        stamp_version(session, release, body.get("version"))
        _drop_uncarried(session, release, body.get("uncarried_shas"))
        from app.pipeline_gates import results as gate_results  # noqa: PLC0415

        # The release's gate runs opened at hand-out, before the head was known: they judge this commit.
        gate_results.stamp_sha(session, project.id, "release", str(release.id), release.head_sha)
    elif event == "pr":
        release.pr_number = int(body["pr_number"])
        release.pr_url = str(body["pr_url"])
    elif event == "queued_to_lane":
        pass
    elif event == "merged":
        mark_merged(session, project, release, str(body["merge_sha"]).lower(), noop=bool(body.get("noop")),
                    version=body.get("version"))
    elif event == "failed":
        mark_failed(release, str(body.get("kind")), body.get("message"), body.get("url") or release.pr_url)
    elif event == "branch_detected":
        _detect_branch(session, project, body)
    else:
        raise ValueError(f"unknown release event {event!r}")
    session.flush()
    return {"applied": True, "release_id": release.id, "state": release.state}


# ── The merge lane's side ────────────────────────────────────────────────────────────────────────────


def _open_release_for_pr(session: Session, project: Project, pr_number: int, base_branch: str) -> Optional[Release]:
    release = session.scalar(select(Release).where(
        Release.project_id == project.id, Release.pr_number == pr_number, Release.state == lifecycle.MERGING,
    ).order_by(Release.id.desc()).limit(1))
    if release is None:
        return None
    target = session.get(ReleaseStage, release.to_stage_id) if release.to_stage_id else None
    if snapshot.to_branch(project, release, target) != base_branch:
        return None
    return release


def lane_admits(session: Session, task: Task, project: Project, pr_number: int, base_branch: str) -> bool:
    """`merge_lane.report_pr`: a PR into a branch other than the integration branch is the lane's only when the
    release executor reports it and an open release names it (its base is that release's target branch)."""
    return task.persona == executor.PERSONA and _open_release_for_pr(session, project, pr_number, base_branch) \
        is not None


def is_release_row(session: Session, row: MergeLaneRow) -> bool:
    """A lane row the release executor reported: a release PR into a stage branch, or a back-merge into Dev."""
    return session.scalar(select(Task.persona).where(Task.id == row.task_id)) == executor.PERSONA


def owns_release_pr(session: Session, task: Task, project: Project, pr_number: int) -> bool:
    """Whether `task` may report `pr_number` when an open release names that PR: only the release executor may."""
    named = session.scalar(select(Release.id).where(
        Release.project_id == project.id, Release.pr_number == pr_number, Release.state == lifecycle.MERGING).limit(1))
    return named is None or task.persona == executor.PERSONA


def frozen_release_prs(session: Session, project: Project) -> list[int]:
    """The PRs of merging releases whose target is frozen now: the lane holds them until the freeze lifts."""
    stages = stages_mod.branch_stages(stages_mod.stages_of(session, project.id))
    by_id = {s.id: s for s in stages}
    return [r.pr_number for r in session.scalars(select(Release).where(
        Release.project_id == project.id, Release.state == lifecycle.MERGING, Release.pr_number.is_not(None)))
        if frozen(session, r, by_id, project=project)]


def on_lane_merged(session: Session, project: Project, row: MergeLaneRow) -> None:
    """The lane merged a release PR: the release advances as if the executor reported `merged`."""
    release = _open_release_for_pr(session, project, row.pr_number, row.base_branch)
    if release is not None and row.merge_sha:
        mark_merged(session, project, release, row.merge_sha)


#: The lane's failure kind (or `closed`/`superseded`) → the release's failure kind and its message.
_LANE_FAILURE: Mapping[str, tuple[str, str]] = {
    "test": ("checks_red", "The release did not pass the project's full gate."),
    "conflict": ("conflict", "The release conflicts with its target branch."),
    "closed": ("forge_refused", "The release PR was closed before it merged."),
    "superseded": ("forge_refused", "Someone pushed to the release branch; release again."),
    "no_git_person": ("no_login", "Nobody with a git connection can merge the release; reconnect in Git Access."),
}


def on_lane_failed(session: Session, project: Project, row: MergeLaneRow, kind: str) -> None:
    """The lane failed, closed or lost (a push to the pinned branch) a release PR: the release fails."""
    release = _open_release_for_pr(session, project, row.pr_number, row.base_branch)
    if release is None:
        return
    failure, message = _LANE_FAILURE.get(kind, ("node", "The merge lane could not finish the release; retry."))
    mark_failed(release, failure, message, row.pr_url)
    session.flush()


# ── jaah-forge's question ────────────────────────────────────────────────────────────────────────────


def authorize(session: Session, task_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
    """`release.authorize`: may this run open or merge a PR from `head` into `base`, a branch other than the
    integration branch? Only the release executor or the merge lane, only for an open release of its own project
    whose pinned branch is `head` and whose target branch is `base`. `purpose: "dispatch"` with no `base` asks about
    `head` alone: may this run start a CI run on the pinned branch (`jaah-forge run dispatch`, a pipeline gate's
    "start a run"). Anything else is the same `LookupError`."""
    task = session.get(Task, task_id)
    head, base = str(body.get("head") or ""), str(body.get("base") or "")
    # The base-less question must say so: an empty `base` on a PR question (a forge naming no target) never passes.
    if not base and body.get("purpose") != DISPATCH:
        raise LookupError("no base named")
    if task is None or task.persona not in _RELEASE_WRITERS or task.state not in ("active", "awaiting_response"):
        raise LookupError("not a release run")
    project = session.get(Project, task.project_id)
    if project is None:
        raise LookupError("not a release run")
    prefix = f"{project.branch_prefix or ''}release/"
    if not head.startswith(prefix) or not head[len(prefix):].isdigit():
        raise LookupError("not a release branch")
    release = session.scalar(select(Release).where(
        Release.id == int(head[len(prefix):]), Release.project_id == project.id, Release.state == lifecycle.MERGING))
    target = session.get(ReleaseStage, release.to_stage_id) if release is not None and release.to_stage_id else None
    if release is None or (base and snapshot.to_branch(project, release, target) != base):
        raise LookupError("no open release from this branch into that base")
    if task.persona == executor.PERSONA:
        if release.task_id != task.id:
            raise LookupError("not this run's release")
    elif session.scalar(select(MergeLaneRow.id).where(
            MergeLaneRow.lane_task_id == task.id, MergeLaneRow.state == "testing",
            MergeLaneRow.pr_number == release.pr_number,
            MergeLaneRow.base_branch == (base or snapshot.to_branch(project, release, target))).limit(1)) is None:
        # The lane merges a release PR only from the batch it took it in, at the head the executor reported.
        raise LookupError("not in this lane run's batch")
    return {"ok": True, "release_id": release.id}
