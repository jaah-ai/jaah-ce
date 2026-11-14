"""The `outcome-ledger-tick` cron (every 15 minutes, operator plane, fleet-wide). Idempotent; each part commits.

(a) a `merged` row past its survival window with no LIVE link becomes `survived`;
(b) analysis rows under an epic inherit their build siblings' result (D5);
(c) the catch-all: a row whose attempt no edge closed, on a task that can no longer end it, becomes `abandoned`
    with reason `reconciled` and a warning — the guarantee that "a settle without a row" cannot persist;
(d) a build row settled at its merge but still `pending` once its task is done and no lane row holds it is
    judged from the task's PR records: `merged` if one merged, else `failed` (`reconciled`);
(e) the cost of rows ended in the last two days is re-priced, since a session's final usage can land after the
    settle;
(f) a hidden `outcome_scan` task is filed for each due project, and a scan open past its interval raises the
    stale-scan alarm (`outcome_ledger/scan.py::file_due`);
(g) benchmark mode: open duels move on and opted-in projects are sampled (`outcome_ledger/benchmark.py::run`) —
    a no-op wherever the org's switch or the project's opt-in is off.

Each row is judged in its own savepoint: one bad row is logged and skipped, never the whole pass.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import edition_features
from app.clock import utcnow as _now
from app.models.merge_lane import MergeLaneRow
from app.models.outcomes import TaskOutcome
from app.models.task_extras import TaskPr
from app.models.tasks import Task
from app.outcome_ledger import states, writer

log = logging.getLogger("outcome-ledger")

#: Task states that still own an attempt: an open row on one of these is waiting on its edge, not lost. A `ready`
#: task is between its claim and its launch's announce (`end_attempt` charges an unannounced one).
_LIVE_TASK_STATES = ("active", "awaiting_response", "paused", "ready")
#: How far back a settled row's cost is re-priced.
_REPRICE_WINDOW = timedelta(days=2)
_BATCH = 500
#: How long a settled build row may wait `pending` on a done task before (d) judges it.
_PENDING_GRACE = timedelta(hours=6)
#: How far back (b) looks for an analysis row still waiting on its epic.
_INHERIT_HORIZON = timedelta(days=400)


def _each(session: Session, rows: list[Any], judge: Callable[[Any], bool]) -> int:
    """Judge each row in its own savepoint; a row that raises is logged and left for the next pass."""
    moved = 0
    for row in rows:
        try:
            with session.begin_nested():
                moved += bool(judge(row))
        except Exception:  # noqa: BLE001 — one bad row never stops the pass
            log.exception("outcome-ledger: tick could not judge outcome %s", getattr(row, "id", row))
    session.commit()
    return moved


def survive(session: Session) -> int:
    """(a) Judge every landed row whose window is over."""
    now = _now()
    rows = list(session.scalars(
        select(TaskOutcome).where(
            TaskOutcome.excluded.is_(False),
            TaskOutcome.outcome_state.in_((states.MERGED, states.DEPLOYED)),
            TaskOutcome.survival_ends_at <= now,
        ).limit(_BATCH)
    ))

    def judge(row: TaskOutcome) -> bool:
        before = row.outcome_state
        writer._recompute(session, row, now)
        return row.outcome_state != before

    return _each(session, rows, judge)


def inherit(session: Session) -> int:
    """(b) Every epic with a `settled` analysis row, once its build children are all final."""
    now = _now()
    epics = set(session.scalars(
        select(Task.parent_task_id).join(TaskOutcome, TaskOutcome.task_id == Task.id).where(
            Task.parent_task_id.is_not(None), TaskOutcome.excluded.is_(False),
            TaskOutcome.outcome_state == states.SETTLED, TaskOutcome.persona.not_in(writer.BUILD_PERSONAS),
            TaskOutcome.ended_at >= now - _INHERIT_HORIZON,
        )
    ))
    return _each(session, sorted(e for e in epics if e is not None),
                 lambda epic: writer.inherit_epic(session, epic, now) > 0)


def reconcile(session: Session) -> int:
    """(c) Close the open rows no edge will close."""
    now = _now()
    rows = list(session.scalars(
        select(TaskOutcome).outerjoin(Task, Task.id == TaskOutcome.task_id).where(
            TaskOutcome.ended_at.is_(None), TaskOutcome.excluded.is_(False),
            or_(Task.id.is_(None), Task.state.not_in(_LIVE_TASK_STATES)),
        ).limit(_BATCH)
    ))

    def judge(row: TaskOutcome) -> bool:
        log.warning(
            "outcome-ledger: task %s dispatch %s ended with no recorded edge; closed as abandoned (reconciled)",
            row.task_id, row.dispatch_seq,
        )
        task = session.get(Task, row.task_id) if row.task_id is not None else None
        writer._end(session, row, task, writer.END_ABANDONED, reason="reconciled", now=now)
        return True

    return _each(session, rows, judge)


def settle_stuck(session: Session) -> int:
    """(d) A settled build row still `pending` on a done task no lane row holds: the merge edge was lost."""
    now = _now()
    lane_holds = select(MergeLaneRow.id).where(
        MergeLaneRow.task_id == TaskOutcome.task_id, MergeLaneRow.state.in_(("queued", "testing")),
    ).exists()
    rows = list(session.scalars(
        select(TaskOutcome).join(Task, Task.id == TaskOutcome.task_id).where(
            TaskOutcome.excluded.is_(False), TaskOutcome.outcome_state == states.PENDING,
            TaskOutcome.ended_at <= now - _PENDING_GRACE, Task.state == "done", ~lane_holds,
        ).limit(_BATCH)
    ))

    def judge(row: TaskOutcome) -> bool:
        task = session.get(Task, row.task_id)
        merged = session.scalars(
            select(TaskPr).where(TaskPr.task_id == row.task_id, TaskPr.state == "merged").order_by(TaskPr.merged_at)
        ).first()
        log.warning("outcome-ledger: task %s dispatch %s settled but its merge edge never landed; reconciled",
                    row.task_id, row.dispatch_seq)
        if merged is not None:
            writer.on_merged(session, task, pr_url=merged.pr_url, merge_sha=None, merged_at=merged.merged_at)
        else:
            writer._move(row, states.EDGE_FAIL, states.FAILED, now)
            row.end_kind, row.end_reason = writer.END_FAILED, "reconciled"
        return True

    return _each(session, rows, judge)


def reprice(session: Session) -> int:
    """(d) Late usage: re-price the recently ended rows from their own session's entry."""
    rows = list(session.scalars(
        select(TaskOutcome).where(
            TaskOutcome.ended_at >= _now() - _REPRICE_WINDOW, TaskOutcome.agent_session_id.is_not(None),
            TaskOutcome.excluded.is_(False),
        ).order_by(TaskOutcome.ended_at.desc()).limit(_BATCH)
    ))

    def judge(row: TaskOutcome) -> bool:
        task = session.get(Task, row.task_id) if row.task_id is not None else None
        if task is not None:
            writer._price(session, task, row)
        return False

    _each(session, rows, judge)
    return len(rows)


def run(session: Session) -> int:
    """One pass; returns how many rows changed state (a pass that only re-priced is a noop)."""
    moved = survive(session) + inherit(session) + reconcile(session) + settle_stuck(session)
    reprice(session)
    from app.outcome_ledger import scan  # deferred: scan reads the merge lane, which imports the writer

    scan.file_due(session)
    if edition_features.enabled("exec_dashboard"):  # benchmark mode's ratings are the Command center's
        from app.outcome_ledger import benchmark

        benchmark.run(session)
    return moved
