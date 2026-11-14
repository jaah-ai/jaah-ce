"""Gate runs and their results: the CP opens a run when it hands a gate list out (`open_run`), the node driver
reports each gate's state change (`pipeline.gate_event` → `apply_event`), and the console reads them back
(`GET /api/projects/{id}/gate-runs` → `runs_out`).

Only the task holding the work may report on a run (the `_held_release` rule, `app/releases/runs.py`): a lane batch
run is its project's `merge_lane` task holding that batch; a release run the executor run carrying that release; an
arrival run the project's executor. Anything else is the same 404. A canceled run answers 409: the driver stops.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import secret_redact
from app.clock import utcnow
from app.entity_shape import iso_or_none, optional_uuid
from app.models.merge_lane import MergeLaneRow
from app.models.projects import Project
from app.models.releases import (
    GATE_REPORT_MAX,
    GATE_RESULT_STATES,
    GateResult,
    GateRun,
    Release,
    ReleaseStage,
)
from app.models.tasks import Task

#: Result states that end a gate.
DONE = frozenset({"passed", "failed", "timed_out", "off", "skipped"})
FAILED = frozenset({"failed", "timed_out"})
#: What a failed run still records: the gates after the one that failed it.
TAIL_STATES = frozenset({"skipped", "off"})
LANE_PERSONA = "merge_lane"
RUNS_LISTED = 20


class Conflict(Exception):
    """The run was canceled or ended: the driver stops (409)."""


def open_run(session: Session, project: Project, stage: Optional[ReleaseStage], phase: str, subject_kind: str,
             subject_id: str, gates: Sequence[Mapping[str, Any]], *, sha: Optional[str] = None) -> GateRun:
    """A new run of `gates` (each a `GateWire`, `full_gate.wire`), frozen as handed out: every gate `waiting`, an
    off one `off`. Flushes."""
    frozen = [dict(g) for g in gates]
    run = GateRun(org_id=project.org_id, project_id=project.id, stage_id=stage.id if stage is not None else None,
                  phase=phase, subject_kind=subject_kind, subject_id=subject_id, sha=sha, state="running",
                  gates=frozen)
    session.add(run)
    session.flush()
    for position, gate in enumerate(frozen):
        session.add(GateResult(org_id=project.org_id, run_id=run.id, position=position,
                               gate_id=optional_uuid(gate.get("id")),
                               state="waiting" if gate.get("enabled", True) else "off"))
    if not frozen:
        run.state, run.finished_at = "passed", utcnow()
    session.flush()
    return run


def running_or_open(session: Session, project: Project, stage: Optional[ReleaseStage], phase: str,
                    subject_kind: str, subject_id: str, gates: Sequence[Mapping[str, Any]], *,
                    sha: Optional[str] = None) -> GateRun:
    """The subject's still running run of this very list (its work handed out again), else a new one
    (`open_run`); a running run of another list (a retry after an edit) is canceled first. Serialised per subject
    (a transaction-scoped advisory lock), so two takes of one batch never open two runs."""
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"gate_run:{subject_kind}:{subject_id}"))))
    frozen = [dict(g) for g in gates]
    for run in session.scalars(select(GateRun).where(
            GateRun.project_id == project.id, GateRun.subject_kind == subject_kind, GateRun.subject_id == subject_id,
            GateRun.state == "running").order_by(GateRun.created_at.desc())):
        if run.gates == frozen:
            return run
        run.state, run.finished_at = "canceled", utcnow()
    session.flush()
    return open_run(session, project, stage, phase, subject_kind, subject_id, frozen, sha=sha)


def stamp_sha(session: Session, project_id: int, subject_kind: str, subject_id: str, sha: str) -> None:
    """Record the commit a subject's runs judge on those opened before it was known (a release's runs open when
    its work is handed out; the executor pins the head after). Never overwrites a known sha."""
    for run in session.scalars(select(GateRun).where(
            GateRun.project_id == project_id, GateRun.subject_kind == subject_kind, GateRun.subject_id == subject_id,
            GateRun.sha.is_(None))):
        run.sha = sha
    session.flush()


def cancel_runs(session: Session, project_id: int, subject_kind: str, subject_id: str) -> None:
    """End the subject's still running runs as `canceled` (its work ended without them: a release that failed before
    its gates finished), each gate not yet done `skipped`, so none reads Running or Waiting forever."""
    now = utcnow()
    for run in session.scalars(select(GateRun).where(
            GateRun.project_id == project_id, GateRun.subject_kind == subject_kind, GateRun.subject_id == subject_id,
            GateRun.state == "running")):
        run.state, run.finished_at = "canceled", now
        for result in session.scalars(select(GateResult).where(
                GateResult.run_id == run.id, GateResult.state.not_in(DONE))):
            result.state, result.finished_at = "skipped", now
    session.flush()


#: The task states that still hold their work: an ended task reports on nothing.
_LIVE = ("active", "awaiting_response")


def _authorized(session: Session, task: Task, run: GateRun) -> bool:
    if run.project_id != task.project_id or task.state not in _LIVE:
        return False
    if run.subject_kind == "lane_batch":
        if task.persona != LANE_PERSONA:
            return False
        held = session.scalar(select(MergeLaneRow.id).where(
            MergeLaneRow.project_id == run.project_id, MergeLaneRow.lane_task_id == task.id,
            MergeLaneRow.batch_id == optional_uuid(run.subject_id)).limit(1))
        # A base-check run gates no rows: its subject is the lane task itself.
        return held is not None or run.subject_id == f"task:{task.id}"
    from app.releases import executor  # noqa: PLC0415 — the releases package imports this module's neighbours

    if task.persona != executor.PERSONA:
        return False
    if run.subject_kind == "release":
        release = session.get(Release, int(run.subject_id)) if run.subject_id.isdigit() else None
        return release is not None and release.project_id == task.project_id and release.task_id == task.id
    live = executor.open_task(session, task.project_id)  # an arrival: the project's open executor carries it
    return live is not None and live.id == task.id


def _link(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text[:2000] if text.startswith("https://") else None


def held_run(session: Session, task: Task, run_id: Any, *, lock: bool = False) -> GateRun:
    """The run `run_id` names when `task` holds its work, else LookupError (the same 404 as no such run)."""
    key = optional_uuid(run_id)
    stmt = select(GateRun).where(GateRun.id == key)
    run = session.scalar(stmt.with_for_update() if lock else stmt) if key else None
    if run is None or not _authorized(session, task, run):
        raise LookupError("no such gate run")
    return run


def apply_event(session: Session, task: Task, body: Mapping[str, Any]) -> dict[str, Any]:
    """`pipeline.gate_event`: one gate's state in a run. `{ok, run_state}`; LookupError 404, Conflict 409,
    ValueError 422. Flushes; the route commits."""
    run = held_run(session, task, body.get("run_id"), lock=True)
    state = str(body.get("state") or "")
    # A run that failed still takes the rest of its list as `skipped` (or `off`): the driver reports them after
    # the required gate that failed it (D2), and they must not read as still waiting.
    if run.state != "running" and not (run.state == "failed" and state in TAIL_STATES):
        raise Conflict(f"this gate run is {run.state}")
    if state not in GATE_RESULT_STATES:
        raise ValueError(f"unknown state {state!r}")
    try:
        position = int(body.get("position"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("position must be a gate's index in the run") from None
    result = session.get(GateResult, (run.id, position))
    if result is None:
        raise LookupError("no such gate in this run")
    now = utcnow()
    if state == "running" and result.started_at is None:
        result.started_at = now
    result.state = state
    result.attempts = max(0, min(int(body.get("attempts") or result.attempts or 0), 100))
    if "report" in body:
        report = secret_redact.redacted(str(body.get("report") or ""))
        result.report = report[-GATE_REPORT_MAX:] or None
    if "link" in body:
        result.link = _link(body.get("link"))
    if state in DONE:
        result.finished_at = now
    session.flush()
    _settle(session, run)
    return {"ok": True, "run_state": run.state}


def _settle(session: Session, run: GateRun) -> None:
    """The run's state off its results: failed at the first required gate that failed, passed once every gate
    ended without one."""
    results = list(session.scalars(select(GateResult).where(GateResult.run_id == run.id)
                                   .order_by(GateResult.position)))
    required = {i: bool(g.get("required", True)) for i, g in enumerate(run.gates or [])}
    if any(r.state in FAILED and required.get(r.position, True) for r in results):
        run.state, run.finished_at = "failed", run.finished_at or utcnow()
    elif all(r.state in DONE for r in results):
        run.state, run.finished_at = "passed", run.finished_at or utcnow()
    session.flush()


def task_runs_out(session: Session, project: Project, task_id: int) -> dict[str, Any]:
    """The runs of the merge lane batches that carried `task_id`'s pull request (the task page's merge gates, D15)."""
    batches = [str(b) for b in session.scalars(select(MergeLaneRow.batch_id).where(
        MergeLaneRow.project_id == project.id, MergeLaneRow.task_id == task_id,
        MergeLaneRow.batch_id.is_not(None)).distinct())]
    if not batches:
        return {"runs": []}
    return _runs(session, project, select(GateRun).where(GateRun.project_id == project.id,
                                                GateRun.subject_kind == "lane_batch",
                                                GateRun.subject_id.in_(batches)))


def runs_out(session: Session, project: Project, subject_kind: Optional[str],
             subject_id: Optional[str], stage_id: Optional[UUID] = None) -> dict[str, Any]:
    """The project's newest runs (of one subject, or one stage, when named), each with its results. Reports are
    plain text."""
    stmt = select(GateRun).where(GateRun.project_id == project.id)
    if stage_id is not None:
        stmt = stmt.where(GateRun.stage_id == stage_id)
    if subject_kind:
        stmt = stmt.where(GateRun.subject_kind == subject_kind)
    if subject_id:
        stmt = stmt.where(GateRun.subject_id == subject_id)
    return _runs(session, project, stmt)


def _runs(session: Session, project: Project, stmt: Any) -> dict[str, Any]:
    from app import forges  # noqa: PLC0415 — the summary's CI label names the project's forge
    from app.pipeline_gates.schema import summary  # noqa: PLC0415

    ref = forges.repo_ref_or_none(project)
    forge = ref.forge if ref is not None else None
    runs = list(session.scalars(stmt.order_by(GateRun.created_at.desc(), GateRun.id.desc()).limit(RUNS_LISTED)))
    results: dict[UUID, list[GateResult]] = {}
    if runs:
        for row in session.scalars(select(GateResult).where(GateResult.run_id.in_([r.id for r in runs]))
                                   .order_by(GateResult.position)):
            results.setdefault(row.run_id, []).append(row)

    def frozen(run: GateRun, position: int) -> dict[str, Any]:
        return dict((run.gates or [])[position]) if position < len(run.gates or []) else {}

    def result_out(run: GateRun, x: GateResult) -> dict[str, Any]:
        gate = frozen(run, x.position)
        return {
            "position": x.position, "gate_id": str(x.gate_id) if x.gate_id else None,
            "name": gate.get("name"), "kind": gate.get("kind"), "required": gate.get("required"),
            "summary": summary(str(gate.get("kind") or ""), gate.get("config") or {}, forge) if gate else None,
            "state": x.state, "attempts": x.attempts, "started_at": iso_or_none(x.started_at),
            "finished_at": iso_or_none(x.finished_at), "report": x.report, "link": x.link,
        }

    return {"runs": [{
        "run": {"id": str(r.id), "stage_id": str(r.stage_id) if r.stage_id else None, "phase": r.phase,
                "subject_kind": r.subject_kind, "subject_id": r.subject_id, "sha": r.sha, "state": r.state,
                "created_at": iso_or_none(r.created_at), "finished_at": iso_or_none(r.finished_at)},
        "results": [result_out(r, x) for x in results.get(r.id, [])],
    } for r in runs]}


def failed_gates(session: Session, project_id: int, subject_kind: str,
                 subject_ids: Sequence[str]) -> dict[str, str]:
    """`{subject_id: gate name}`: the required gate that failed each subject's newest run, when that run failed (the
    Release step mark's "Failed at gate › <name>", D15)."""
    if not subject_ids:
        return {}
    newest: dict[str, GateRun] = {}
    for run in session.scalars(select(GateRun).where(
            GateRun.project_id == project_id, GateRun.subject_kind == subject_kind,
            GateRun.subject_id.in_(list(subject_ids))).order_by(GateRun.created_at.desc(), GateRun.id.desc())):
        newest.setdefault(run.subject_id, run)
    failed = {r.id: r for r in newest.values() if r.state == "failed"}
    if not failed:
        return {}
    out: dict[str, str] = {}
    for row in session.scalars(select(GateResult).where(GateResult.run_id.in_(list(failed)),
                                                       GateResult.state.in_(tuple(FAILED)))
                               .order_by(GateResult.position)):
        run = failed[row.run_id]
        gate = (run.gates or [])[row.position] if row.position < len(run.gates or []) else {}
        if gate.get("required", True) and run.subject_id not in out:
            out[run.subject_id] = str(gate.get("name") or "a gate")
    return out
