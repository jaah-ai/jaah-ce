"""Awaiting-response settle and expiry: parking on a runner question, resuming, timing it out."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, case, literal, or_, select, update
from sqlalchemy.orm import Session

from app import actors, fleet_verdicts, merge_lane_state, runner_terminations, slot_destroy, task_sections
from app.events import emit_console_event
from app.models.tasks import Task, TaskQuestion
from app.models.worktrees import SlotIntent
from app.task_artifacts import visible_artifact_clause, visible_artifact_clause_joined
from app.task_awaiting_response_parser import parse_response_options
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, READY
from app.user_activity import record_task_note
from app.routes.tasks.policy import (
    OPERATOR_CRITERION_PERSONAS,
    unanswered_question_release_after,
    unanswered_question_timeout,
)
from app.routes.tasks.slots import kill_runner_keeping_hold, send_minted_intents
from app.routes.tasks.dispatch import _RUNNER_BINDING_CLEARED
from app.routes.tasks.runner_settle import (
    _discard_unused_refund, _refund_node_fault_attempt, _vanished_reason, _vanished_trace, end_attempt,
)

log = logging.getLogger(__name__)


def _park(
    task_id: int, question: str, session: Session, *, from_states: tuple[str, ...], why: str
) -> Optional[str]:
    """The `→ awaiting_response` park both gates share: guarded on `state IN from_states`, else None. Stores
    `question` verbatim on `awaiting_response_body` (read back via `parse_response_options`), stamps
    `github_synced_at` as the re-drive fence, and records the `question_raised` note. An empty `from_states`
    is refused: an unguarded park is never a gate."""
    if not from_states:
        raise ValueError("_park needs the gate's own source states")
    now = datetime.now(timezone.utc)
    row = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state.in_(from_states))
        .values(
            state=AWAITING_RESPONSE, awaiting_response_body=question, github_synced_at=now,
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if row is None:
        return None
    merge_lane_state.lane_task_left_active(session, task_id)
    record_task_note(
        session,
        task_id,
        "question_raised",
        # The question only — the label already says "Question raised".
        question[:120],
        extra={"question": question, "options": parse_response_options(question)},
    )
    emit_console_event(session, entity="task", id=task_id, action=AWAITING_RESPONSE)
    session.commit()
    log.info("task %s ->awaiting_response (%s; github_synced_at=%s)", task_id, why, now)
    return AWAITING_RESPONSE


def await_response_on_dispatched_task(task_id: int, question: str, session: Session) -> Optional[str]:
    """UNIVERSAL `active → awaiting_response` transition for the CP's own escalation park
    (`task_pipeline_cp.block_task`); persona-agnostic, guarded on `state == ACTIVE`, else None (`_park`)."""
    return _park(task_id, question, session, from_states=(ACTIVE,), why="runner question, DB-direct")


def await_response_on_wedged_dispatch(task_id: int, question: str, session: Session) -> Optional[str]:
    """`ready|active → awaiting_response` for a task whose DISPATCH is wedged — the CP's own gate, raised
    when a slot refuses the same way to the end of its retry budget.

    Not `await_response_on_dispatched_task`, which guards on `ACTIVE`: `ready → active` is written by the
    node's `hooks/runner-status.py` after the runner is up, so a dispatch refused before the spawn (e.g.
    `reset_to_base` gate 3 on unpushed commits) leaves the task `ready`. `ACTIVE` covers a later refusal.
    A system transition, so no `task_lifecycle.TRANSITIONS` row; recovery is `answer`'s
    `awaiting_response → ready`."""
    return _park(task_id, question, session, from_states=(READY, ACTIVE), why="CP wedged-dispatch gate")


def resume_dispatched_task_on_answer(task_id: int, session: Session) -> Optional[str]:
    """`awaiting_response → active` for a task whose LIVE runner has just been handed the human's answer —
    the mirror of `await_response_on_dispatched_task`, which parked it to ask.

    A system follow-up, not an operator verb. Only called after the node acks the delivery. No attempt
    reset, no re-stamped binding: the same session runs the same attempt. A moved row, or one with a visible
    open round, returns None."""
    now = datetime.now(timezone.utc)
    # Lock first, in its own statement: a capture holding the lock commits its round before the guard below
    # takes its snapshot (an UPDATE's own re-check would reuse a snapshot that cannot see it).
    session.execute(select(Task.id).where(Task.id == task_id).with_for_update())
    # A visible open round means the runner is asking again (`supersede_pending_answer_delivery`): a delivery
    # that lands now answered the OLD question, and resuming would hide the new one.
    open_round = select(literal(1)).select_from(TaskQuestion).where(
        TaskQuestion.task_id == Task.id,
        TaskQuestion.status == "open",
        or_(Task.artifacts_hidden_before.is_(None), TaskQuestion.created_at >= Task.artifacts_hidden_before),
    ).exists()
    row = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state == AWAITING_RESPONSE, ~open_round)
        # The acked warm delivery proves a LIVE runner — `_active_dispatch_values`' reason for the claim.
        .values(state=ACTIVE, github_synced_at=now, question_runner_stopped_at=None,
                **actors.task_actor_values(session))
        .returning(Task.id)
    ).first()
    if row is None:
        return None
    emit_console_event(session, entity="task", id=task_id, action=ACTIVE)
    log.info("task %s awaiting_response->active (answer delivered to the live runner)", task_id)
    return ACTIVE


def _pending_delivery_clause(task_id: int):
    """This task's `deliver_answer` intents the node has not retired. The key is read under the verb test, as
    `answer_delivering_keys` does (a `lease_slot` payload is large)."""
    is_deliver = SlotIntent.verb == fleet_verdicts.VERB_DELIVER_ANSWER
    return and_(
        is_deliver,
        case((is_deliver, SlotIntent.payload["task_id"].astext)) == str(task_id),
        or_(
            SlotIntent.outcome.is_(None),
            SlotIntent.outcome.notin_(fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES),
        ),
    )


def supersede_pending_answer_delivery(task_id: int, session: Session) -> list[int]:
    """Retire this task's in-flight `deliver_answer` intents `superseded` because its runner just asked a NEW
    question, and put the rounds they carried back in the hand-off batch. Returns the re-armed round ids.

    A new question proves the runner is past the old one: it got that answer (from the pane, or a delivery
    whose ack never came) or it never will. Left pending, the old intent keeps the row "Delivering" over the new
    question, and the node's `Esc` before each re-try dismisses the NEW picker. Its rounds lose `delivered_at`,
    so the next delivery re-quotes them as earlier answers (`_settle_gate_if_nothing_outstanding`). A delivery
    that names no `question_ids` (the legacy gate, or minted before they rode the payload) is left alone.
    Does not commit."""
    is_deliver = SlotIntent.verb == fleet_verdicts.VERB_DELIVER_ANSWER
    payloads = session.execute(
        update(SlotIntent)
        # Only a round hand-off (it names its `question_ids`): a legacy-gate answer has no round to re-quote, so
        # retiring it would lose the answer; it keeps its own retry.
        .where(_pending_delivery_clause(task_id),
               case((is_deliver, SlotIntent.payload.has_key("question_ids")), else_=False))
        .values(
            outcome=fleet_verdicts.OUTCOME_SUPERSEDED, next_attempt_at=None,
            reason="superseded: the runner asked a new question before this answer was acknowledged",
        )
        .returning(SlotIntent.payload)
        .execution_options(synchronize_session=False)
    ).scalars().all()
    round_ids = sorted({
        int(qid) for payload in payloads if isinstance(payload, dict)
        for qid in payload.get("question_ids") or [] if isinstance(qid, int)
    })
    if round_ids:
        session.execute(
            update(TaskQuestion)
            .where(TaskQuestion.task_id == task_id, TaskQuestion.id.in_(round_ids),
                   TaskQuestion.status != "open")
            .values(delivered_at=None)
            .execution_options(synchronize_session=False)
        )
    if payloads:
        log.warning(
            "task %s: runner asked a new question while %s answer delivery(ies) were unacknowledged — retired "
            "them; round(s) %s re-quoted with the next delivery", task_id, len(payloads), round_ids,
        )
    return round_ids


def resume_past_pending_answer_delivery(task_id: int, session: Session, *, session_id: str = "") -> bool:
    """`awaiting_response → active` for a runner ending its own task while the row is parked ONLY on an
    answered round's delivery still in flight (`task_sections.answer_delivering_keys`, no visible open round).

    A runner calling settle or fail is not frozen in `AskUserQuestion`, so a stuck delivery must not refuse
    it: the task's pending `deliver_answer` intents retire `superseded` (they stop re-serving) and the row
    resumes as the node's `done` ack would have (`resume_dispatched_task_on_answer`). A system follow-up like
    that one, so no `TRANSITIONS` row. `session_id`, when given, must be the row's runner. Does not commit —
    the caller owns the transaction. False, nothing written, for any other row.

    Only a delivery minted for THIS park counts: one created at or after `github_synced_at` (every park and
    resume stamps it) and addressed to the row's runner. An older one left pending by an earlier round must
    not let a runner settle past a later gate that records no round (the wedged-dispatch menu, `block_task`)."""
    task = session.scalar(
        select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True)
    )
    if task is None or task.state != AWAITING_RESPONSE:
        return False
    if session_id and task.runner_session_id != session_id:
        return False
    open_round = session.scalar(select(TaskQuestion.id).where(
        TaskQuestion.task_id == task_id,
        TaskQuestion.status == "open",
        visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
    ).limit(1))
    if open_round is not None or session.execute(task_sections.answer_delivering_keys([task_id])).first() is None:
        return False
    is_deliver = SlotIntent.verb == fleet_verdicts.VERB_DELIVER_ANSWER
    pending = _pending_delivery_clause(task_id)
    this_park = [pending]
    if task.github_synced_at is not None:
        this_park.append(SlotIntent.created_at >= task.github_synced_at)
    if task.runner_session_id:
        this_park.append(case((is_deliver, SlotIntent.payload["session_id"].astext)) == task.runner_session_id)
    if session.scalar(select(SlotIntent.id).where(*this_park).limit(1)) is None:
        return False
    retired = session.execute(
        update(SlotIntent)
        .where(pending)
        .values(
            outcome=fleet_verdicts.OUTCOME_SUPERSEDED, next_attempt_at=None,
            reason="superseded: the runner ended its task before the answer landed",
        )
        .returning(SlotIntent.id)
        .execution_options(synchronize_session=False)
    ).scalars().all()
    resume_dispatched_task_on_answer(task_id, session)
    log.warning(
        "task %s: runner ended its task while its answer was still in flight — retired deliver_answer "
        "intent(s) %s, awaiting_response->active so the settle is not refused",
        task_id, list(retired),
    )
    return True


def system_fail_dispatched_task(
    task_id: int, reason: str, session: Session, *, final: bool = False, session_id: str = "",
    cause: Optional[str] = None, node: Optional[str] = None, note_detail: Optional[str] = None,
) -> Optional[str]:
    """UNIVERSAL runner-reported FAILURE driven DB-DIRECT when a runner cannot proceed. Not
    unconditionally terminal: `end_attempt` returns the Task to `ready` while attempts remain.
    `final=True` is passed by the one caller with no retry budget.

    `session_id` fences the report to its attempt: `state == ACTIVE` names a task, and a reaped
    session's late `/fail` could otherwise end a new attempt in the same worktree.

    `cause` is not defaulted: the runner's `/fail` passes `reported_failure`; the compliance-group
    cascade passes nothing, since no runner ever ran on a mirror. `node` (a node uuid) rides the note, for a
    `node_fault` cause (`app/launch_failures.py`); `note_detail` too (`runner_terminations.record`)."""
    # No persona predicate: every row carries a persona, so one could not exclude anything.
    guards: tuple[Any, ...] = ()
    if session_id:
        guards += (Task.runner_session_id == session_id,)
    return end_attempt(
        task_id, session,
        reason=reason,
        final=final,
        guards=guards,
        cause=cause,
        node=node,
        note_detail=note_detail,
    )


# The key a round the CONTROL PLANE raised carries in `guard_context` (`checkout_gate.raise_round`); a runner's
# round never does (`record_question_round` keeps that column for guard rounds only).
CP_GATE_KEY = "cp_gate"


def _not_a_cp_round() -> Any:
    return or_(TaskQuestion.guard_context.is_(None), ~TaskQuestion.guard_context.has_key(CP_GATE_KEY))


# The whole safety of the `awaiting_response` reclaim. Three things park a row there: a runner in
# `AskUserQuestion`, `reassert_active`'s turn-boundary park (the persona guard), and the CP's own
# `await_response_on_wedged_dispatch` gate, which has no runner and must not be reclaimed in a loop. An
# open round is positive evidence (its one writer requires a running state, and `AskUserQuestion` is the
# only human channel) — except one the CP raised itself on its gate (`CP_GATE_KEY`). Visible rounds only: a
# Reset soft-hides rounds.
def _has_open_runner_question(task_id_column: Any) -> Any:
    return select(literal(1)).where(
        TaskQuestion.task_id == task_id_column,
        TaskQuestion.status == "open",
        _not_a_cp_round(),
        visible_artifact_clause_joined(TaskQuestion.created_at),
    ).exists()


def reclaim_awaiting_response_task(task_id: int, session: Session, *, node_fault: bool = False) -> bool:
    """Crash reclaim of a task parked on a RUNNER-RAISED question whose runner is gone — the
    `awaiting_response` analogue of the two `active` reclaimers above, and the state neither of them sees.

    With its session dead the row is unanswerable: answers are delivered into that session. `node_fault`
    means what it does in `reclaim_active_task`; the refund uses this door's source state.

    Guarded by `_has_open_runner_question`, not the state. A chat is reclaimed too (owner: a fresh session
    beats a row parked for ever), but a chat parks with no round, so its signature is
    `awaiting_response_body IS NULL` (the CP gate always writes one) AND `runner_session_id IS NOT NULL`
    (the gate's archetype never had a session), keyed on `DONE_CRITERION_OPERATOR`. `resume_from_console`
    clears the body so a stale one cannot block this.

    `_BREAK_RELAUNCH_STATES` already sanctions the edge. Landing is `end_attempt`'s (`ready` keeping the
    slot, or `system_failed` at the ceiling); `runner_terminations.record` drops the open round so the
    fresh runner can ask again."""
    if slot_destroy.destroy_pending(session, task_id):
        # As `reclaim_active_task`: the destroy's ack requeues it.
        log.info("task %s not reclaimed: its slot is being destroyed", task_id)
        return False
    # Before `end_attempt`, for `reclaim_active_task`'s reason.
    refunded = node_fault and _refund_node_fault_attempt(
        task_id, session, sources=(AWAITING_RESPONSE,))
    # As `reclaim_active_task`: the note names the node and VM the parked runner was on.
    trace = _vanished_trace(session, task_id)
    landed = end_attempt(
        task_id, session,
        reason=_vanished_reason(trace, parked=True),
        sources=(AWAITING_RESPONSE,),
        node=trace.node, note_detail=trace.detail, note_vm=trace.vm,
        guards=(
            # Not a row whose runner the unanswered-question sweep stopped on purpose: its rounds are kept for the
            # operator and the answer relaunches it (`stop_runner_on_unanswered_question`).
            Task.question_runner_stopped_at.is_(None),
            or_(
                # A runner frozen inside its own `AskUserQuestion`, for every non-interactive persona.
                and_(
                    Task.persona.notin_(OPERATOR_CRITERION_PERSONAS),
                    _has_open_runner_question(Task.id),
                ),
                # A chat parked at a turn boundary (no round); see the docstring for why both columns.
                and_(
                    Task.persona.in_(OPERATOR_CRITERION_PERSONAS),
                    Task.awaiting_response_body.is_(None),
                    Task.runner_session_id.isnot(None),
                ),
            ),
        ),
        # `vanished`, for its sibling reclaimers' reason.
        cause=runner_terminations.CAUSE_VANISHED,
    )
    if landed is None:
        _discard_unused_refund(task_id, session, refunded=refunded)
        return False
    log.info(
        "task %s parked on an unanswerable question (its runner vanished) reclaimed by the reaper -> %s",
        task_id, landed,
    )
    return True


def _aged_open_round(task_id: int, cutoff: datetime) -> Any:
    """An OPEN, VISIBLE round on this task created before `cutoff` — the age guard both unanswered-question
    doors ride, spelled as an EXISTS so it sits inside their one guarded write.

    Joined to `tasks` because `visible_artifact_clause_joined` requires it (else a silent cartesian
    product). Implies `_has_open_runner_question`, so it keeps both doors off the CP's gate. The clock is
    the round's `created_at`, not `tasks.updated_at`, which moves for unrelated reasons."""
    return (
        select(literal(1))
        .select_from(TaskQuestion)
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(
            TaskQuestion.task_id == task_id,
            TaskQuestion.status == "open",
            TaskQuestion.created_at < cutoff,
            _not_a_cp_round(),
            visible_artifact_clause_joined(TaskQuestion.created_at),
        )
        .exists()
    )


def stop_runner_on_unanswered_question(task_id: int, session: Session) -> bool:
    """Stage ONE of an unanswered question: stop the runner, KEEP THE QUESTION. True iff this call stopped
    it; False is the ordinary answer (too young, a chat, the CP's own gate, already stopped, moved).

    The timeout bounds the runner (an account and pane held for an absent human), not the question: the
    row stays `awaiting_response`, rounds stay answerable, and the answers reach a fresh runner via its
    Q&A trail (`_resume_after_stopped_question_runner`). `question_runner_stopped_at`, set here and
    cleared on the next claim, tells the other readers the runner is gone on purpose.

    The worktree stays bound; a clean one is released a drain pass later
    (`lease_drain._release_stopped_question_holds`) after the kill — releasing now would cancel the kill
    (see `end_attempt`). Work in progress keeps its slot until the answer or stage two. No attempt is
    charged. An `advisor` and the CP's gate are excluded, as in stage two."""
    now = datetime.now(timezone.utc)
    hours = int(unanswered_question_timeout().total_seconds() // 3600)
    row = session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            Task.state == AWAITING_RESPONSE,
            Task.persona.notin_(OPERATOR_CRITERION_PERSONAS),
            Task.question_runner_stopped_at.is_(None),
            _aged_open_round(task_id, now - unanswered_question_timeout()),
        )
        .values(
            question_runner_stopped_at=now,
            # The session this row named is being killed; a late break report from it is fenced on this column.
            **_RUNNER_BINDING_CLEARED,
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if row is None:
        return False
    # Post-check closing the answer race: an answer committed while this UPDATE waited on the lock is
    # invisible to its snapshot; a fresh statement sees it, and then the stop must not happen.
    if not session.scalar(select(_has_open_runner_question(Task.id)).where(Task.id == task_id)):
        session.rollback()
        return False
    _, issued = kill_runner_keeping_hold(
        task_id, session,
        # A one-off decision, not suppressed by the re-mint cooldown: the clean-slot release waits on this kill.
        fresh_decision=True,
        reason=(
            f"task {task_id}'s question went unanswered for over {hours}h — stop that runner. The task "
            "stays awaiting its answer, which starts a fresh runner"
        ),
    )
    # Keeps the open rounds (see `runner_terminations._QUESTIONS_KEPT_ANSWERABLE`).
    runner_terminations.record(session, task_id, runner_terminations.CAUSE_QUESTION_RUNNER_STOPPED)
    # No transition row: the state did not change, and a same-state `task:` row skews epic time metrics.
    # The event still refreshes the board.
    emit_console_event(session, entity="task", id=task_id, action=AWAITING_RESPONSE,
                       record_transition=False)
    session.commit()
    send_minted_intents(issued)  # AFTER the commit — see `send_minted_intents`.
    log.warning(
        "task %s: its question went unanswered for over %sh — runner stopped, the row stays "
        "awaiting_response and answerable", task_id, hours,
    )
    return True


def expire_timed_out_awaiting_response_task(task_id: int, session: Session) -> bool:
    """Stage TWO of an unanswered question: FAIL the task and give its worktree back, work in progress
    included, once the question is older than `unanswered_question_release_after()`. True iff this call
    ended it; False is the ordinary answer and never an error.

    Per-task and atomic: every racing decision is taken inside the write. Stage one freed the runner;
    what remains is a tree with work in progress. It is failed, and `lease_drain._release_finished`
    returns the slot to be reset over — the WIP is wiped, by the operator's choice. Admits both a
    stage-one-stopped row and a still-live park.

    `final=True` lands `system_failed`: a re-dispatch would re-ask the same absent human. The question is
    quoted into `awaiting_response_body`. Excluded: the CP's own wedged-dispatch gate (no open round;
    `_aged_open_round`), and an `advisor` at any age. The age test is a guard on the write, so an answer
    in between cannot be overtaken."""
    days = unanswered_question_release_after().days
    cutoff = datetime.now(timezone.utc) - unanswered_question_release_after()
    # Read before the write only to quote it into the reason, never to branch on.
    question = session.scalar(
        select(TaskQuestion.question)
        .where(TaskQuestion.task_id == task_id, TaskQuestion.status == "open")
        .order_by(TaskQuestion.id)
        .limit(1)
    )
    landed = end_attempt(
        task_id, session,
        # Addressed to whoever opens the row: what happened, the cost, what to do, and the question (the
        # round is dropped on the way). Names no actor: tenant administrators read it too (`app/tenancy/copy.py`).
        reason=(
            f"nobody answered this runner's question within {days} days, so the task was stopped and its "
            f"worktree cleared and handed back rather than held any longer. The question was — "
            f"{question or '(the round is no longer readable)'} — answer it here and press Start to run "
            f"this task again."
        ),
        sources=(AWAITING_RESPONSE,),
        guards=(
            # NOT a chat — the docstring is the single home for that exclusion.
            Task.persona.notin_(OPERATOR_CRITERION_PERSONAS),
            or_(
                _aged_open_round(task_id, cutoff),
                # Or a row stage one stopped that long ago: with nothing open it must not hold its tree for ever.
                Task.question_runner_stopped_at < cutoff,
            ),
        ),
        # TERMINAL BY CONSTRUCTION — see the docstring.
        final=True,
        cause=runner_terminations.CAUSE_QUESTION_UNANSWERED,
    )
    if landed is None:
        return False
    log.warning(
        "task %s was parked on an unanswered question for over %s days -> %s (slot released once any "
        "kill has been enacted)",
        task_id, days, landed,
    )
    return True
