"""Task state transitions: priority, reset, stop, wait-on-customer, set-state, force-start, complete."""
from __future__ import annotations

import logging
from typing import Any, NoReturn, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, case, func, literal, or_, select, update
from sqlalchemy.orm import Session

from app import actors, fleet_verdicts, merge_lane_state, runner_terminations, slot_state, task_priority
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.object_access import can_access, key_scope_satisfied
from app.authz.principal import Principal, current_principal
from app.authz.visibility import TASK_SPEC, ObjectAccess
from app.db import get_session
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.tasks import Task
from app import task_sections
from app.task_lifecycle import (
    ACTIVE,
    AWAITING_RESPONSE,
    DONE,
    DRAFT,
    PAUSED,
    READY,
    STATES,
    SYSTEM_FAILED,
    TRANSITION_BY_NAME,
    _RUNNER_RUNNING_STATES,
)
from app.task_lookup import get_task_or_404
from app.url_validation import validated_http_url
from app.org_gate import require_org_active
from app.routes.tasks import dispatch
from app.routes.tasks.policy import (
    EPIC_BUILD_PERSONAS,
    PERSONA_EPIC,
    _OPERATOR_HAND_CLOSE_EVIDENCE,
    _OPERATOR_HAND_CLOSE_REASON,
    _PRIORITY_ABS_MAX,
)
from app.routes.tasks.task_wire import TaskOut, _subtree_task_ids, _task_out, _task_out_with_children
from app.routes.tasks.slots import (
    _release_task_slots,
    abandon_task_slots,
    kill_runner_keeping_hold,
    send_minted_intents,
)
from app.routes.tasks.dispatch import (
    _RESET_FRESH_COLUMNS,
    _RUNNER_BINDING_CLEARED,
    _dispatch_subtask_ready,
    _push_identity_or_422,
    _refuse_starterless_start,
    _save_unsaved_draft_before_dispatch,
    _start,
    _terminates_a_running_runner,
)
from app.routes.tasks.epics import _maybe_roll_up_parent, _reevaluate_epic_child_failure
from app.routes.tasks.epic_finish import (
    _drop_epic_report_published_by,
    _publish_epic_final_report_at_completion,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
log = logging.getLogger(__name__)


# ── Action route: set-priority (the operator's dispatch-order lever) ────────────────────────────────


class TaskPriorityIn(BaseModel):
    """Set a Task's `priority`: higher = sooner in the dispatch worklist. Negative de-prioritizes below
    normal (0). Bounded to ±`_PRIORITY_ABS_MAX`, a 422 rather than an int4-overflow 500."""

    priority: int = Field(ge=-_PRIORITY_ABS_MAX, le=_PRIORITY_ABS_MAX)


def _priority_change(before: int | None, after: int | None) -> dict[str, Any] | None:
    """The `prioritized` row's `changes`, shaped like an edit's (`update.py`) so the Timeline shows
    "Normal → High". Recorded as level names, as `assigned_to_id` records names; None when nothing moved."""
    if before == after:
        return None
    return {"changes": {"priority": {
        "before": task_priority.priority_label(before), "after": task_priority.priority_label(after),
    }}}


@router.patch("/{task_id}", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",), author_draft=True)
def set_task_priority(
    task_id: int,
    body: TaskPriorityIn,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Set a Task's dispatch-order priority. Settable in ANY state (most useful on a queued task; no
    transition semantics). Idempotent. It orders the dispatch worklist, not the console list."""
    task = get_task_or_404(task_id, session)
    before = task.priority
    task.priority = body.priority
    session.flush()
    emit_console_event(
        session, entity="task", id=task.id, action="prioritized",
        transition_extra=_priority_change(before, task.priority),
    )
    session.commit()
    return _task_out(task, session=session, principal=principal)


# ── Action route: reset (back to a clean, re-runnable draft) ────────────────────────────────────────
# What a Reset clears beyond `_RESET_FRESH_COLUMNS`: Start re-runs a task; Reset abandons the attempt,
# so the columns Start keeps are cleared here.

# `recon_map` is this task's own artifact (dependents read the prerequisite row), so leaving it would
# thread an abandoned "Start here" into the next prompt. `phase_index`/`phase_name` are the planner's
# placement, not attempt output.
_RESET_TO_DRAFT_COLUMNS: dict[str, Any] = {
    **_RESET_FRESH_COLUMNS,
    "recon_map": None,
    # An SDK agent run's claim stamps (`agent_resolution.stamp_run`): a reset task has run nothing. Start keeps
    # them, as it keeps `bound_provider`; the next lease re-stamps or clears them.
    "run_agent_id": None,
    "run_agent_commit": None,
    # Settle refusals (`app/models/tasks.py::Task.settle_refusals`) belong to one attempt and go with it. A
    # re-dispatch continues the attempt and keeps them, or retrying would make the waiver unreachable.
    "settle_refusals": 0,
    # The soft-delete watermark, in the same guarded UPDATE, so a Reset that loses the race hides nothing.
    # `statement_timestamp()`, not `now()` (transaction start), so a row the dying runner committed after
    # this request began is still hidden.
    "artifacts_hidden_before": func.statement_timestamp(),
}

# Clearing columns alone would leave the task showing and feeding its prior interview; those rows are
# kept and hidden by the watermark (`app/task_artifacts.py` single-homes the predicate and tables).

# Notably `task_note:answered` (folded into a cold runner's prompt by `_task_qa_trail`) and
# `task_attachments` by kind: a `report` is the runner's; an `ATTACHMENT_KIND_FILE` is the human's and is
# never hidden.

# The watermark also hides the task's activity history (`task:%` transitions and notes) on the task's own
# Activity Log and Run metrics (`_row_predates_its_reset`, `app/task_run_metrics.py`). The rule is "the
# task's own surfaces", not a reader list. The system audit log keeps the narrower content rule (a Reset
# does not erase audit history), and `_epic_time_metrics` still counts a reset child's phases (the epic's
# elapsed time is real).


def _reset_row(task_id: int, session: Session) -> tuple[bool, list[fleet_verdicts.IssuedIntents]]:
    """One row's whole Reset, in the caller's transaction: the guarded `→ draft` UPDATE and, when it lands,
    the slot half (end the runner, wipe the tree, free the hold). Returns
    `(landed, issued)`; never commits and never rolls back, so the caller owes both and the
    `send_minted_intents` after its commit. A row outside `reset.sources` (a clean draft) writes nothing."""
    # Read before anything moves, under a row lock (see `_terminates_a_running_runner`).
    reset_terminated_runner = _terminates_a_running_runner(task_id, session)
    reset = TRANSITION_BY_NAME["reset"]
    # For the log line only: `_release_task_slots` owns the release (and its generation fence), and after it
    # the row reads `free`.
    hold = slot_state.task_hold(session, task_id)
    landed = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state.in_(reset.sources))
        .values(
            state=reset.target,
            dispatch_attempts=0,
            run_requested_at=None,
            # `_RESET_FRESH_COLUMNS` carries the staleness and binding clears, so Start and Reset clear the same set.
            **_RESET_TO_DRAFT_COLUMNS,
            **actors.task_actor_values(session),
        )
    )
    if landed.rowcount == 0:
        return False, []
    merge_lane_state.lane_task_left_active(session, task_id)
    _drop_epic_report_published_by(task_id, session)
    # The Reset is still recorded as a kill, now for `runner_terminations.record`'s side effect: its
    # `_QUESTIONS_RETIRED_ELSEWHERE` entry for `CAUSE_OPERATOR_RESET` stops `drop_undelivered_questions` from
    # hard-deleting rounds the watermark soft-deletes. The row survives in `activity_events` and the audit
    # log. Written in the same transaction as the `draft` write, so it sorts above `task:reset` by `id`.
    if reset_terminated_runner:
        runner_terminations.record(session, task_id, runner_terminations.CAUSE_OPERATOR_RESET)

    # ── The slot half of the reset: end the runner, wipe the tree, free the hold ────────────────────
    # All in one commit: no later drain frees a `draft` row's hold. Reset is a release edge (the operator
    # abandons the attempt), unlike a retryable `end_attempt`. `abandon_task_slots`, not
    # `kill_runner_keeping_hold` (see its docstring). Before `_release_task_slots`, whose release clears the
    # binding the mint reads.
    _, issued = abandon_task_slots(
        task_id, session,
        reason=f"task {task_id} was reset to draft, abandoning this attempt's work",
    )
    _release_task_slots(
        task_id, session,
        reason=f"released: task {task_id} was reset to draft, abandoning this attempt's work",
    )
    if hold is not None:
        log.info(
            "task %s reset — freed its %r hold on %s#%s; the tree is deliberately not preserved, because "
            "a Reset is the operator declaring this attempt's work abandoned",
            task_id, hold["state"], hold["project"], hold["slot"],
        )
    emit_console_event(session, entity="task", id=task_id, action="reset")
    return True, issued


def _raise_not_resettable(task_id: int, session: Session) -> NoReturn:
    """The 409 for a Reset that landed nothing: re-read to explain why (an already-clean row, or a state
    outside the vocabulary). Rolls back first, so no half-written cascade survives."""
    session.rollback()
    session.expire_all()
    current = get_task_or_404(task_id, session)
    hint = (
        "it is already a clean draft" if current.state == DRAFT
        else f"'{current.state}' is not a state this Task may be reset from"
    )
    raise HTTPException(
        status_code=409,
        detail=f"task {task_id} is '{current.state}' — {hint}",
    )


@router.post("/{task_id}/reset", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def reset_task(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Reset a Task back to a clean, re-runnable `draft` from ANY state but `draft` itself — WITHOUT
    dispatching, so the operator can reconfigure it and Start it fresh.

    It ends the runner, wipes the tree and releases the slot from every source state still holding
    one (from `done`/`system_failed` it mints nothing; see `abandon_task_slots`). Two writes: the slot
    row freed, plus the one `reset_to_base` intent with `abandon` (a `kill_runner` here could never fire).
    Clearing the binding is load-bearing: `runner_session_id` fences late reports from the killed session.

    An EPIC resets its WHOLE TREE (owner decision): every live descendant gets the per-row Reset
    and the container lands `draft`, so its next Start re-runs the pipeline from the top as a fresh epic
    would (a re-run Plan archives the old build children it supersedes). Archived rows keep their state;
    only a worktree one still holds is abandoned.

    It also hides what the attempt produced: Q&A rounds and answer notes, settled stage output and
    `recon_map`, the runner report, `outcome_*`/`merged_at`, and the task's activity history — a soft
    delete (owner decision). The operator's inputs (body, attachments, tags, dependencies, ratings,
    body versions) are untouched. Columns are NULLed via `_RESET_TO_DRAFT_COLUMNS`, rows hidden by its
    `artifacts_hidden_before` watermark; `_RESET_FRESH_COLUMNS` also clears `awaiting_response_body`,
    the cold gate's row-free question."""
    task = get_task_or_404(task_id, session)
    if task.persona == PERSONA_EPIC:
        return _reset_epic(task_id, session, principal)
    # Read before the commits below refresh the row: resetting a spent child stops it holding its epic
    # failed (owner decision).
    recovered_a_failed_child = task.state == SYSTEM_FAILED
    recovered_childs_parent_id = task.parent_task_id
    # Its epic is locked first (epic-then-child), only when this reset recovers a failure.
    if recovered_a_failed_child and recovered_childs_parent_id is not None:
        # `select(...).with_for_update()`, not `session.get(...)`: a lock without the row, and `session.get` would
        # be a registered tenancy-seam bypass.
        session.execute(
            select(Task.id).where(Task.id == recovered_childs_parent_id).with_for_update()
        )
    landed, issued = _reset_row(task_id, session)
    if not landed:
        _raise_not_resettable(task_id, session)
    # Before the commit: a crash after it would leave the epic `system_failed` with nothing failed under
    # it, and the rollup self-heal scans only `active` epics.
    if recovered_a_failed_child:
        _reevaluate_epic_child_failure(recovered_childs_parent_id, session)
    session.commit()
    send_minted_intents(issued)  # AFTER the commit — see `send_minted_intents`.
    session.expire_all()
    return _task_out_with_children(task_id, session, principal)


def _reset_epic(epic_id: int, session: Session, principal: Principal | None) -> TaskOut:
    """`reset_task` for an epic: every live descendant, then the container, in ONE commit, so no reader
    sees a draft epic over running children. The epic is locked first (epic-then-child, the order every
    writer here keeps); each child's own lock is `_reset_row`'s. 409 only when nothing at all was reset."""
    session.execute(select(Task.id).where(Task.id == epic_id).with_for_update())
    descendants = session.execute(
        select(Task.id, Task.archived_at)
        .where(Task.id.in_(_subtree_task_ids(epic_id, session)), Task.id != epic_id)
        .order_by(Task.id)
    ).all()
    issued: list[fleet_verdicts.IssuedIntents] = []
    any_landed = False
    for row in descendants:
        if row.archived_at is not None:
            # Kept evidence keeps its state, but a re-plan archives any non-`active` child, so one may still
            # hold a worktree (a stopped or asking child); the whole-tree Reset abandons it all the same.
            _, archived_issued = abandon_task_slots(
                row.id, session, reason=f"epic {epic_id} was reset, abandoning archived task {row.id}'s worktree",
            )
            issued.extend(archived_issued)
            _release_task_slots(row.id, session, reason=f"released: epic {epic_id} was reset")
            continue
        landed, child_issued = _reset_row(row.id, session)
        any_landed = any_landed or landed
        issued.extend(child_issued)
    # The container last: no runner and no slot, but the same columns (a failed epic's child list in
    # `awaiting_response_body`, a finished one's outcome) and watermark clear.
    landed, epic_issued = _reset_row(epic_id, session)
    issued.extend(epic_issued)
    if not (landed or any_landed):
        _raise_not_resettable(epic_id, session)
    session.commit()
    send_minted_intents(issued)  # AFTER the commit — see `send_minted_intents`.
    session.expire_all()
    return _task_out_with_children(epic_id, session, principal)


# ── Action route: stop — the cockpit-side Stop ──────────────────────────────────────────────────────
# The `stop` row's `sources` in `TRANSITIONS` (`app/task_lifecycle.py`) is the guard; no separate constant.


@router.post("/{task_id}/stop", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def stop_task(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Park a stoppable in-flight Task into `paused` — the cockpit-side Stop, the table's `stop` transition.
    It mints the kill itself rather than relying on the ~5s settled-hold sweep, whose re-mint cooldown
    could swallow it (the sweep remains the backstop); the node enacts it.

    The runner dies, the worktree stays (the difference from Reset): no release, so the slot keeps its
    binding for the Start that resumes it. Idempotent: one conditional UPDATE whose `rowcount` is the
    guard; an already stopped or terminal Task is a 200 no-op. An epic Stop cascades to its unsettled
    children on the winning branch only; a stopped child may collapse its parent epic. The row it stops
    records an `operator` stop (`app/task_sections.py`); cascaded children record none."""
    _stop_task_core(task_id, session, _user)
    session.expire_all()
    return _task_out_with_children(task_id, session, principal)


def _stop_task_core(
    task_id: int,
    session: Session,
    user: AdminUser,
    *,
    stop_reason: str = task_sections.STOP_REASON_OPERATOR,
    note: Optional[str] = None,
) -> bool:
    """`stop_task`'s whole effect, returning whether the stop landed (False: the state was not a `stop`
    source, nothing written, the session rolled back). The stop record — `stop_reason`, `note`, who and
    when — rides the SAME conditional UPDATE as `state=paused`, so no reader sees one without the other."""
    task = get_task_or_404(task_id, session)
    # Read before the UPDATE, under a row lock (see `_terminates_a_running_runner`).
    stopped_a_running_runner = _terminates_a_running_runner(task_id, session)
    stop = TRANSITION_BY_NAME["stop"]
    landed = session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            or_(
                Task.state.in_(stop.sources),
                # A person's Stop takes over a SYSTEM pause: the row keeps `paused` but its record
                # becomes theirs, so a top-up never resumes what an operator stopped (AS4). Any other paused row
                # is the table's idempotent no-op.
                and_(Task.state == stop.target, Task.stop_reason.in_(task_sections.SYSTEM_STOP_REASONS)),
            ),
        )
        .values(
            state=stop.target,
            stop_reason=stop_reason,
            stop_note=note,
            stopped_by_id=user.id,
            stopped_at=func.now(),
            **actors.task_actor_values(session),
        )
    )
    if landed.rowcount == 0:
        # Idempotent no-op. Roll back the no-op UPDATE; the caller re-reads.
        session.rollback()
        session.expire_all()
        return False
    merge_lane_state.lane_task_left_active(session, task_id)
    # A Stop from `ready` kills nothing and records nothing. The row lock, not the UPDATE's `sources` guard
    # (which admits `ready`), proves the row had not moved on.
    issued: list[fleet_verdicts.IssuedIntents] = []
    if stopped_a_running_runner:
        runner_terminations.record(
            session, task_id, runner_terminations.CAUSE_OPERATOR_STOP, user=user,
        )
        # Kill the runner here: the settled-hold sweep was the only path and its re-mint cooldown could swallow a
        # Stop for ten minutes. `fresh_decision=True` skips only the recency arm (a pending kill still
        # suppresses). The hold is kept (no `_release_task_slots`): the tree stays for the resume.
        _, issued = kill_runner_keeping_hold(
            task_id, session,
            reason=fleet_verdicts.task_stopped_reason(task_id),
            fresh_decision=True,
        )
    emit_console_event(session, entity="task", id=task_id, action="stop")
    if task.persona == PERSONA_EPIC:
        # The cascade shares the epic's park transaction, so stopped-epic-with-dispatching-children is never
        # observable. Archived children are untouched. Which children have a runner is read before the UPDATE
        # (`RETURNING` yields new states), locked for `_roll_up_epic`'s reason; parent then children.
        running_children = session.scalars(
            select(Task.id).where(
                Task.parent_task_id == task_id, Task.state.in_(_RUNNER_RUNNING_STATES),
                Task.archived_at.is_(None),
            ).with_for_update()
        ).all()
        stopped_children = session.execute(
            update(Task)
            .where(
                Task.parent_task_id == task_id, Task.state.in_(stop.sources),
                Task.archived_at.is_(None),
            )
            .values(state=stop.target, **actors.task_actor_values(session))
            .returning(Task.id)
        )
        stopped_ids = list(stopped_children.scalars())
        # Intersected: a child that left the running states in between must not get a termination row.
        was_running = set(running_children)
        stopped_running_children = [cid for cid in stopped_ids if cid in was_running]
        runner_terminations.record_many(
            session, stopped_running_children, runner_terminations.CAUSE_OPERATOR_STOP, user=user,
        )
        # And kill their runners, with the same intersection; recording alone would leave them working. Holds
        # kept, so the epic's Start resumes them in place.
        for child_id in stopped_running_children:
            _, child_issued = kill_runner_keeping_hold(
                child_id, session,
                reason=fleet_verdicts.epic_stopped_reason(child_id, task_id),
                fresh_decision=True,
            )
            issued.extend(child_issued)
        for child_id in stopped_ids:
            emit_console_event(session, entity="task", id=child_id, action="stop")
    session.commit()
    # After the commit, never before (`send_minted_intents`); a dropped frame only costs latency.
    send_minted_intents(issued)
    if task.persona != PERSONA_EPIC:
        # Pause-collapse check for a stopped child — after the commit, so it reads a fresh sibling count.
        _maybe_roll_up_parent(task_id, session)
    return True


# ── Action route: wait-on-customer — the "Waiting on customer" pause reason (D-waiting) ─────────────
class WaitOnCustomerIn(BaseModel):
    """An optional note shown with the reason on the row and the task page."""

    note: Optional[str] = Field(default=None, max_length=task_sections.STOP_NOTE_MAX)


@router.post("/{task_id}/wait-on-customer", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def wait_on_customer(
    task_id: int,
    body: WaitOnCustomerIn = WaitOnCustomerIn(),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Put a not-done task into Stopped with the reason "Waiting on customer" and an optional note — a
    stop RECORD, never a state (`app/task_sections.py`). A running or queued row (a `stop` source) is
    stopped exactly as `stop_task` stops it; a draft, paused or failed row keeps its state and gains the
    record alone. Starting it again clears the record (the clearing trigger).

    400 for a `done` row and for a stage task (the reason belongs to the epic); 409 if it reached `done`
    meanwhile."""
    task = get_task_or_404(task_id, session)
    if task.parent_task_id is not None:
        raise HTTPException(status_code=400, detail="a stage task cannot wait on the customer; set it on the epic")
    if task.state == DONE:
        raise HTTPException(status_code=400, detail=f"task {task_id} is done; nothing to wait for")
    note = (body.note or "").strip() or None
    if task.state in TRANSITION_BY_NAME["stop"].sources:
        if _stop_task_core(task_id, session, _user, stop_reason=task_sections.STOP_REASON_WAITING, note=note):
            session.expire_all()
            return _task_out_with_children(task_id, session, principal)
        # The state moved under us: re-read, then take the record-only path unless it finished.
        task = get_task_or_404(task_id, session)
    read_state = task.state
    if read_state == DONE or read_state in TRANSITION_BY_NAME["stop"].sources:
        raise HTTPException(status_code=409, detail=f"task {task_id} moved to {read_state!r}; try again")
    # Record only, state untouched; guarded on the state just read so a concurrent move is not overwritten.
    landed = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state == read_state)
        .values(
            stop_reason=task_sections.STOP_REASON_WAITING,
            stop_note=note,
            stopped_by_id=_user.id,
            stopped_at=func.now(),
            **actors.task_actor_values(session),
        )
    )
    if landed.rowcount == 0:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"task {task_id} moved; try again")
    # Not a lifecycle transition (the state is unchanged), so no transition row; the board still refreshes.
    emit_console_event(session, entity="task", id=task_id, action="wait_on_customer", record_transition=False)
    session.commit()
    session.expire_all()
    return _task_out_with_children(task_id, session, principal)


def _clear_wait_record(task_id: int, session: Session) -> bool:
    """Drop a "Waiting on customer" stop record, leaving the state and any other record alone. Returns
    whether one was dropped; uncommitted."""
    landed = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.stop_reason == task_sections.STOP_REASON_WAITING)
        .values(
            stop_reason=None, stop_note=None, stopped_by_id=None, stopped_at=None,
            **actors.task_actor_values(session),
        )
    )
    return landed.rowcount > 0


@router.delete("/{task_id}/wait-on-customer", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def clear_wait_on_customer(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Dismiss "Waiting on customer" — the record goes, the state stays. Idempotent: no record is a 200."""
    get_task_or_404(task_id, session)
    if _clear_wait_record(task_id, session):
        emit_console_event(session, entity="task", id=task_id, action="wait_cleared", record_transition=False)
        session.commit()
    session.expire_all()
    return _task_out_with_children(task_id, session, principal)


# ── Action route: set-state — the Details tab's unrestricted State control ─────────────────────────────
# Any state from any state, beyond the operator-verb table. Each target keeps its real side effects by
# composing the verb that means it (Stop ends a runner, Reset releases the slot, Start queues, Complete
# closes); `active`/`awaiting_response`, and a target no verb reaches from here, are written directly.
# `waiting_on_customer` is the pause reason, offered beside the states. Every other move drops that reason.
SET_STATE_TARGETS: tuple[str, ...] = (*STATES, task_sections.STOP_REASON_WAITING)


class SetStateIn(BaseModel):
    """The state to put the task in, or `waiting_on_customer` with its optional note."""

    state: str
    note: Optional[str] = Field(default=None, max_length=task_sections.STOP_NOTE_MAX)

    @field_validator("state")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in SET_STATE_TARGETS:
            raise ValueError(f"state must be one of {', '.join(SET_STATE_TARGETS)}")
        return value


def _force_state(task_id: int, target: str, session: Session) -> None:
    """Write `target` directly, guarded on the state just read, then roll the parent up — the path for a
    move no operator verb makes. Never `done`: that is `complete_task`'s, whose event wakes the done-actions.
    Commits.

    A stage task put back to work (or failed) under a parked or finished epic reactivates the epic, as
    `_start` does, so the rollup sees it; the epic is locked first (epic-then-child, like Reset)."""
    if target == DONE:
        raise ValueError("set-state completes through complete_task, never a direct write")
    task = get_task_or_404(task_id, session)
    read_state, parent_id = task.state, task.parent_task_id
    if read_state == target:
        return
    if parent_id is not None:
        session.execute(select(Task.id).where(Task.id == parent_id).with_for_update())
    landed = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state == read_state)
        .values(
            state=target,
            # Leaving `system_failed` drops its failure text, as `_start` does.
            **({"awaiting_response_body": None} if read_state == SYSTEM_FAILED else {}),
            **actors.task_actor_values(session),
        )
    )
    if landed.rowcount == 0:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"task {task_id} moved; try again")
    if read_state == ACTIVE:
        merge_lane_state.lane_task_left_active(session, task_id)
    if parent_id is not None and target in (ACTIVE, AWAITING_RESPONSE, SYSTEM_FAILED):
        reactivated = session.execute(
            update(Task)
            .where(Task.id == parent_id, Task.persona == PERSONA_EPIC, Task.state.in_((PAUSED, DONE)))
            .values(state=ACTIVE, **actors.task_actor_values(session))
        )
        if reactivated.rowcount:
            emit_console_event(session, entity="task", id=parent_id, action="set_state")
    if read_state == SYSTEM_FAILED and parent_id is not None:
        _reevaluate_epic_child_failure(parent_id, session)
    emit_console_event(session, entity="task", id=task_id, action="set_state")
    session.commit()
    session.expire_all()
    if parent_id is not None:
        _maybe_roll_up_parent(task_id, session)


@router.post("/{task_id}/set-state", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def set_task_state(
    task_id: int,
    body: SetStateIn,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Put a task in any state (or Waiting on customer) from any state. Choosing the current state only
    drops a waiting reason. A refusal from a composed verb (the dispatch switch, the org gate, a stage
    task's wait) passes through unchanged."""
    task = get_task_or_404(task_id, session)
    target, current, is_epic = body.state, task.state, task.persona == PERSONA_EPIC
    stoppable = TRANSITION_BY_NAME["stop"].sources

    def stop_first() -> None:
        # End a live runner (the hold is kept) before a move that must not leave one working.
        if get_task_or_404(task_id, session).state in stoppable:
            _stop_task_core(task_id, session, _user)
            session.expire_all()

    if target == task_sections.STOP_REASON_WAITING:
        if task.parent_task_id is not None:
            raise HTTPException(status_code=400, detail="a stage task cannot wait on the customer; set it on the epic")
        if current == DONE:
            _force_state(task_id, PAUSED, session)
        return wait_on_customer(task_id, WaitOnCustomerIn(note=body.note), _user, principal, session)

    if target == current:
        pass
    elif target == DONE:
        complete_task(task_id, None, _user, principal, session)
    elif target == DRAFT:
        # An epic too: its Reset cascades to the whole tree (a forced draft would strand paused children).
        reset_task(task_id, _user, principal, session)
    elif target == READY and is_epic:
        # An epic is never queued (the dispatcher would take a `ready` epic as a unit): Ready starts it.
        dispatch.start_task(task_id, None, _user, principal, session)
    elif target == READY:
        if current in (ACTIVE, AWAITING_RESPONSE):
            # Refused before the stop, so a Start that would leave nobody to push as leaves the runner working.
            _refuse_starterless_start(session, task, principal.user_id if principal is not None else None)
            stop_first()
        dispatch.start_task(task_id, None, _user, principal, session)
    elif target == PAUSED:
        stop_first()
        _force_state(task_id, PAUSED, session)
    elif target in (SYSTEM_FAILED, DRAFT):
        stop_first()
        _force_state(task_id, target, session)
    else:  # ACTIVE / AWAITING_RESPONSE: a direct override, no runner started
        _force_state(task_id, target, session)

    session.expire_all()
    if _clear_wait_record(task_id, session):
        emit_console_event(session, entity="task", id=task_id, action="wait_cleared", record_transition=False)
        session.commit()
    session.expire_all()
    return _task_out_with_children(task_id, session, principal)


# ── Action route: force-start — preempt a running task to start this one now ──
class ForceStartIn(BaseModel):
    """What a force-start names: the RUNNING task whose place this one takes, and the same one-shot
    `run_requested` intent `TaskStartIn` carries (same conservative default) — a Start that first
    makes room, not a Run that bypasses the target's gates."""

    stop_task_id: int
    run_requested: bool = False
    # The same stand-in starter `TaskStartIn.push_as_id` names, vetted by the same `_push_identity_or_422`.
    push_as_id: Optional[UUID] = None


@router.post("/{task_id}/force-start", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def force_start_task(
    task_id: int,
    body: ForceStartIn,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Start THIS task almost immediately by stopping a running one to make room — PREEMPTION, for when
    every slot is taken. In one request:

      1. **Stop the victim** as `stop_task` does: park to `paused`, a termination row
         (`operator_preempt`), and a kill with the hold kept.
      2. **Re-queue the victim** at once (owner decision) via `_start` out of `paused`. The pending kill
         holds off any release of its slot (`fleet_verdicts.slots_awaiting_a_kill`) until enacted.
      3. **Make the target sort ahead**: dispatch orders `priority DESC, id`, so the target is bumped to
         the next named level above the victim (`task_priority.level_above`) unless already ahead.
      4. **Start the target** via `_dispatch_subtask_ready`.

    "Almost immediately": the kill is an order the node enacts next pool tick. Two commits
    (three if an unsaved draft is finalized first): the first carries the victim's park, termination,
    kill, re-queue and the target's bump, so a failed re-queue rolls everything back and a failed target
    start still leaves the target ahead. Intents are pushed after that commit. The victim is body-borne,
    so write access is checked here, with one 422 for missing and unauthorized alike."""
    task = get_task_or_404(task_id, session)
    require_org_active(session, task.org_id)  # before the victim is touched (`app/org_gate.py`)
    # A body-borne object identity, treated like `add_task_dependency`'s: a scoped read, the guard's two
    # halves restated (write clause plus the route key on its scope; no author-draft arm), and one
    # 422 for missing and not-yours, so no id is confirmed.
    victim = session.scalar(select(Task).where(Task.id == body.stop_task_id))
    if victim is None or not (
        can_access(session, principal, "task", victim.id, mode=ObjectAccess.WRITE)
        and key_scope_satisfied(
            session, principal, TASK_SPEC, victim.id,
            key=permission_key("tasks", "edit"), mode=ObjectAccess.WRITE,
        )
    ):
        raise HTTPException(status_code=422, detail=f"task {body.stop_task_id} not found")

    if task.id == victim.id:
        raise HTTPException(
            status_code=409, detail=f"task {task_id} cannot make room for itself — name another task"
        )
    if task.persona == PERSONA_EPIC:
        raise HTTPException(
            status_code=409, detail="force-start applies to a dispatched task, not an epic"
        )
    if task.state in TRANSITION_BY_NAME["start"].idempotent_noop:
        raise HTTPException(
            status_code=409, detail=f"task {task_id} is already {task.state!r} — nothing to force"
        )
    # An epic is `active` as a rollup, never a runner, so nothing to stop.
    if victim.persona == PERSONA_EPIC or victim.state not in _RUNNER_RUNNING_STATES:
        raise HTTPException(
            status_code=409, detail=f"task {victim.id} is not running, nothing to stop"
        )
    # Vetted before anything is stopped, so a bad stand-in refuses with the victim untouched; after the 409s above,
    # which name the plainer problem.
    push_as_id = _push_identity_or_422(session, task, body.push_as_id, principal, _user)
    # A task a monthly budget holds would never take the room it made: refuse before stopping anything.
    from app import budgets

    over_budget = budgets.hold_reasons(session, [task]).get(task.id)
    if over_budget is not None:
        raise HTTPException(status_code=409, detail=over_budget)
    project = session.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=422, detail=f"project {task.project_id} not found")
    # The same finalize `start_task` does, before the preemption's transaction (it commits).
    _save_unsaved_draft_before_dispatch(task, session, principal)

    # ── (1) Stop the victim — `stop_task`'s non-epic arm ────────────────────────────
    # Epic-then-child: the victim's `_start` locks its epic, so holding the child lock first would invert
    # the order against `stop_task`'s cascade.
    if victim.parent_task_id is not None:
        session.execute(
            select(Task.id)
            .where(Task.id == victim.parent_task_id, Task.persona == PERSONA_EPIC)
            .with_for_update()
        ).first()
    # Under a row lock: this is the guard; the plain read above was for the error message.
    if not _terminates_a_running_runner(victim.id, session):
        session.rollback()
        raise HTTPException(
            status_code=409, detail=f"task {victim.id} is not running, nothing to stop"
        )
    stop = TRANSITION_BY_NAME["stop"]
    landed = session.execute(
        update(Task)
        .where(Task.id == victim.id, Task.state.in_(stop.sources))
        .values(state=stop.target, **actors.task_actor_values(session))
    )
    if landed.rowcount == 0:
        session.rollback()
        raise HTTPException(
            status_code=409, detail=f"task {victim.id} moved before it could be stopped — retry"
        )
    merge_lane_state.lane_task_left_active(session, victim.id)
    # Before the `stop`/`ready` emits and in this transaction, so the row sorts above them.
    runner_terminations.record(session, victim.id, runner_terminations.CAUSE_OPERATOR_PREEMPT)
    # The hold is kept for the victim's resume; `fresh_decision=True` as in `stop_task`.
    _, issued = kill_runner_keeping_hold(
        victim.id, session,
        reason=fleet_verdicts.task_stopped_reason(victim.id),
        fresh_decision=True,
    )
    emit_console_event(session, entity="task", id=victim.id, action="stop")

    # ── (3) The target sorts ahead of the victim, or is bumped until it does ───────────────────────
    # Flushed so it rides the victim's commit (see the docstring).
    if not task_priority.sorts_ahead(task.priority, task.id, victim.priority, victim.id):
        bumped = task_priority.level_above(victim.priority)
        # Bounded like the patch route: above Urgent the bump is `+1`, and repeated force-starts could exceed
        # `_PRIORITY_ABS_MAX`. Refused rather than clamped: a clamp could leave the victim ahead.
        if bumped > _PRIORITY_ABS_MAX:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"task {task_id} cannot outrank task {victim.id} — lower that task's priority first",
            )
        before = task.priority
        task.priority = bumped
        session.flush()
        emit_console_event(
            session, entity="task", id=task.id, action="prioritized",
            transition_extra=_priority_change(before, bumped),
        )

    # ── (2) Re-queue the victim via the one `* -> ready` edge — commits everything above ───────
    # `sources=(PAUSED,)`, the state just written under our lock. No `started_by`: the preempting operator
    # does not answer for the victim's pushes; its existing starter survives (absence is a no-op in `_start`).
    if not _start(
        victim.id, session,
        sources=(PAUSED,),
        run_requested=False,
        started_by=None,
        values=dict(_RESET_FRESH_COLUMNS),
        events=(READY,),
    ):
        # `_start` has already rolled back: the victim was never ordered stopped.
        raise HTTPException(
            status_code=409,
            detail=f"task {victim.id} could not be re-queued after its stop — nothing was changed",
        )
    # After the commit, never before (`send_minted_intents`).
    send_minted_intents(issued)
    session.expire_all()

    # ── (4) Start the target — the shared non-epic flip; it commits and owns its own 409 ────────────
    return _dispatch_subtask_ready(
        task, session, principal, run_requested=body.run_requested, push_as_id=push_as_id,
    )


# ── Action route: complete ─────────────────────────────────────────────────────────────────────────
class CompleteTaskIn(BaseModel):
    """What an operator closes a row out WITH — both fields optional, so a bodyless Complete is exactly
    the verb it always was. See `apply_complete`'s `delivered` paragraph.

    The URL is shape-checked, not repo-checked, unlike the runner's settle: a runner attesting another
    repo's PR is the spoof that floor stops, while an operator may legitimately point at a sibling
    repo's fix."""

    # Bounded for what the board can render; the column is Text.
    outcome_summary: Optional[str] = Field(default=None, max_length=100_000)
    outcome_pr_url: Optional[str] = None

    _check_pr_url = field_validator("outcome_pr_url")(validated_http_url)


def apply_complete(
    task_id: int,
    session: Session,
    *,
    expect_session_id: str | None = None,
    allow_sources: frozenset[str] | None = None,
    delivered: "CompleteTaskIn | None" = None,
    cause: str = runner_terminations.CAUSE_OPERATOR_COMPLETE,
) -> bool:
    """Execute the table's `complete` transition on ONE row, and COMMIT. True when the row landed `done`;
    False when nothing matched (the caller answers: the operator route 409s, the agent door returns
    `completed=false` with the state found).

    Shared by the operator's Complete and an advisor's `/exit` (`POST /api/agent/tasks/{id}/console-exit`,
    routes/agent/runner_liveness.py): the same human act, so one body. `expect_session_id` fences the console door to
    its own session (a late SessionEnd must not complete a replacement's row or undo a Stop's `paused`);
    the operator passes none. `allow_sources` narrows the console door to the states a chat was live in,
    since `complete.sources` admits everything but `done`; not redundant, because `answer_task`'s cold
    gate can leave `ready` with a live binding. Both are restated in the UPDATE. `cause` is the termination
    row's, when a live runner is cut off: the operator's by default, `superseded` when the control plane ends
    a stale merge-lane base check (`merge_lane.end_stale_base_checks`, under the system actor).

    Does: the row-locked `_terminates_a_running_runner` read, the guarded UPDATE with attempt reset and
    binding clear, the `CAUSE_OPERATOR_COMPLETE` row, both console events, the commit. Does not: the epic
    rollup and final-report tail (`complete_task`'s).

    `delivered` is the outcome an operator closes a row out with, e.g. a PR merged after its runner was
    declared dead; a `pr_url` suppresses the "no reproduction was run" hand-close marker. Not a loosening
    of the anti-spoof floor (that stops runners; this is the operator's own verb). `merged_at` is not
    written: a late stamp would skew every dwell metric."""
    # Read before the UPDATE under a row lock: only two of `complete.sources` have a runner, and without the
    # lock a row landing in another between read and write would collect a termination row.
    completed_a_running_runner = _terminates_a_running_runner(task_id, session)
    complete = TRANSITION_BY_NAME["complete"]
    sources = complete.sources if allow_sources is None else allow_sources
    fence = (
        () if expect_session_id is None
        else (Task.runner_session_id == expect_session_id,)
    )
    # The hand-close marker, which stops this verb wedging an epic: `_child_rollup_ready` needs a `done` coder
    # child to carry `outcome_pr_url` or `outcome_no_pr_reason`, which only its own settle writes, so a
    # hand-closed coder would hold its epic `active` for ever (owner decision: stamp the marker). Only on the
    # operator's verb, only on `coder`, only where both are absent, inside the UPDATE via `case`.
    # The operator's own outcome, when supplied: a summary re-words; a `pr_url` says a delivery happened.
    # Absent, everything falls back to the column, so the no-body call is unchanged.
    recorded_pr_url = (delivered.outcome_pr_url or None) if delivered else None
    recorded_summary = (delivered.outcome_summary or None) if delivered else None
    hand_closed_with_no_terminal_reference = and_(
        Task.persona.in_(EPIC_BUILD_PERSONAS),
        Task.outcome_pr_url.is_(None),
        Task.outcome_no_pr_reason.is_(None),
        # A named PR says something was delivered, so the marker cannot also be written. A conjunct, keeping the
        # whole predicate in one place.
        literal(recorded_pr_url is None),
    )
    landed = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state.in_(sources), *fence)
        .values(
            state=complete.target, dispatch_attempts=0, **_RUNNER_BINDING_CLEARED,
            # Opposite coalesce directions per `models.py`: `outcome_summary` is not write-once (operator wins);
            # `outcome_pr_url` is write-once (column wins), and an `awaiting_response` row may already carry one.
            outcome_summary=func.coalesce(literal(recorded_summary), Task.outcome_summary),
            outcome_pr_url=func.coalesce(Task.outcome_pr_url, literal(recorded_pr_url)),
            outcome_no_pr_reason=case(
                (hand_closed_with_no_terminal_reference, _OPERATOR_HAND_CLOSE_REASON),
                else_=Task.outcome_no_pr_reason,
            ),
            outcome_no_pr_evidence=case(
                (hand_closed_with_no_terminal_reference, _OPERATOR_HAND_CLOSE_EVIDENCE),
                else_=Task.outcome_no_pr_evidence,
            ),
            **actors.task_actor_values(session),
        )
    )
    if landed.rowcount == 0:
        # Rolled back here: the read's lock lasts until the transaction ends. The miss reasons (source, fence,
        # narrowed `allow_sources`) are not distinguished; callers re-read to say what they found.
        session.rollback()
        return False
    # A hand-close cuts off a live runner, and "Runner ended (done)" would misstate it; recorded uniformly,
    # `advisor` included (owner decision; `runner_terminations.CAUSES`). Above the transition.
    if completed_a_running_runner:
        runner_terminations.record(session, task_id, cause)
    # Two events, not a redundant double-emit: `complete` is the operator verb the activity log and epic
    # metrics read, while `done` is what enqueues the done-actions (`app/durable/task_done.py`: Jira comment, re-tag,
    # epic report), with no sweep behind it. Guarded by the write-site drift scanner.
    merge_lane_state.lane_task_left_active(session, task_id)
    emit_console_event(session, entity="task", id=task_id, action="complete")
    emit_console_event(session, entity="task", id=task_id, action=DONE)
    session.commit()
    return True


@router.post("/{task_id}/complete", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def complete_task(
    task_id: int,
    body: Optional[CompleteTaskIn] = None,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Close a Task out as `done` by operator decision — the table's `complete` transition executed. Any task
    but a `done` one, `draft` and `ready` included (the table row explains); `done` 409s, since a second
    Complete is an operator error.

    An epic may be completed by hand; nothing cascades (Stop does), and it stays completed since every
    mover is guarded on `state == ACTIVE`. Outcomes are not verified.

    The transition is `apply_complete`, shared with the chat's `/exit`; this route adds the 409, the epic
    rollup and the final-report tail. The optional body carries the outcome (`CompleteTaskIn`)."""
    task = get_task_or_404(task_id, session)
    # Read before the commit refreshes `task.state`. A hand-completed spent child stops holding its epic
    # failed (owner decision).
    recovered_a_failed_child = task.state == SYSTEM_FAILED
    recovered_childs_parent_id = task.parent_task_id
    if not apply_complete(task_id, session, delivered=body):
        # `apply_complete` rolled back, so this reads the real row. The 409 is the route's own wording.
        session.expire_all()
        current = get_task_or_404(task_id, session)
        if current.state == DONE:
            hint = "it is already done"
        else:
            # Unreachable for any state in the vocabulary; kept for legacy/orphaned rows, failing closed like
            # `transitions_from`.
            hint = "that state is outside the task lifecycle vocabulary, so no operator verb applies to it"
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is '{current.state}' — {hint}",
        )
    # Before the rollup: a still-failed epic would fail `state == ACTIVE` and never reach `done`.
    if recovered_a_failed_child:
        _reevaluate_epic_child_failure(recovered_childs_parent_id, session)
        session.commit()
    # A completed child may complete its epic — after the commit, so the rollup reads a fresh count.
    _maybe_roll_up_parent(task_id, session)
    # A hand-completed epic bypasses `_try_complete_epic`'s backstop, so publish here past pending follow-ups
    # (`ignore_pending`); a follow-up that later dispatches replaces it on its higher id.
    if task.persona == PERSONA_EPIC:
        # Conditional so nothing commits when nothing was published.
        if _publish_epic_final_report_at_completion(task, session) is not None:
            session.commit()
    session.expire_all()
    return _task_out_with_children(task_id, session, principal)
