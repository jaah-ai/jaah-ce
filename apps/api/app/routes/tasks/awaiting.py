"""Awaiting-response settle and expiry: parking on a runner question, resuming, timing it out."""
from __future__ import annotations

import logging
from datetime import datetime
from collections.abc import Iterable
from typing import Any, Optional

from sqlalchemy import and_, case, literal, or_, select, update
from sqlalchemy.orm import Session

from app import (
    actors, fleet_verdicts, merge_lane_state, runner_terminations, slot_destroy, slot_state, task_sections,
)
from app.clock import utcnow
from app.events import emit_console_event
from app.models.tasks import Task, TaskQuestion
from app.models.worktrees import SlotIntent, Worktree
from app.task_artifacts import visible_artifact_clause, visible_artifact_clause_joined
from app.task_awaiting_response_parser import parse_response_options
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, READY, TRANSITION_BY_NAME
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
    now = utcnow()
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
    emit_console_event(session, entity="task", id=task_id, action=AWAITING_RESPONSE, commit=True)
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
    now = utcnow()
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
        # The drain's own pending test: its index guard reads the few live rows (`fleet_verdicts.still_pending`).
        fleet_verdicts.still_pending(),
    )


def answer_delivery_in_flight(task: Task, session: Session, runner_session_id: str) -> bool:
    """True iff a `deliver_answer` for THIS park, addressed to the live runner RUNNER_SESSION_ID, is not retired
    yet (still re-served) — the legacy gate stays `awaiting_response` until the node's `done` ack, so a second
    answer would mint a second delivery the runner acts on again. Only a delivery minted since the park
    (`github_synced_at`) to that session counts: one left behind by a runner that has since gone must not shut
    the door the cold resume reopens."""
    is_deliver = SlotIntent.verb == fleet_verdicts.VERB_DELIVER_ANSWER
    clauses = [
        _pending_delivery_clause(task.id),
        case((is_deliver, SlotIntent.payload["session_id"].astext)) == runner_session_id,
    ]
    if task.github_synced_at is not None:
        clauses.append(SlotIntent.created_at >= task.github_synced_at)
    return session.scalar(select(SlotIntent.id).where(*clauses).limit(1)) is not None


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
    round_ids = _rearm_rounds(session, task_id, payloads)
    if payloads:
        log.warning(
            "task %s: runner asked a new question while %s answer delivery(ies) were unacknowledged — retired "
            "them; round(s) %s re-quoted with the next delivery", task_id, len(payloads), round_ids,
        )
    return round_ids


def _rearm_rounds(session: Session, task_id: int, payloads: Iterable[Any]) -> list[int]:
    """Put the answered rounds the `deliver_answer` PAYLOADS carried back in the hand-off batch (`delivered_at`
    cleared), so the next delivery re-quotes them. Returns the round ids. Does not commit."""
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
    return round_ids


def rearm_undelivered_answer(session: Session, payload: Any) -> None:
    """A `deliver_answer` the node refused until `fleet_verdicts.purge_unexecutable_intents` dropped it: its
    rounds were stamped delivered at the mint, so without this the answer is lost and the task waits forever on a
    runner that never got it. The rounds go back in the hand-off batch and the parked-task sweep
    (`task_pipeline_cp.expire_awaiting_response`) mints a fresh delivery, until one lands or the runner goes.

    Only a round hand-off (`question_ids`) on a task still `awaiting_response`; a task edit's dismissal is
    `live_task_edits.reopen_undelivered_dismissal`'s. Does not commit."""
    if not isinstance(payload, dict) or payload.get("dismiss") is True:
        return
    task_id = payload.get("task_id")
    if not isinstance(task_id, int) or not payload.get("question_ids"):
        return
    # Unlocked on purpose: the caller holds the intent row, and the answer path locks the task before intents, so a
    # lock here could deadlock. A re-arm racing a fresh answer only re-quotes an answer, never loses one.
    if session.scalar(select(Task.state).where(Task.id == task_id)) != AWAITING_RESPONSE:
        return
    round_ids = _rearm_rounds(session, task_id, [payload])
    if round_ids:
        log.warning("task %s: an answer delivery was dropped undelivered — round(s) %s re-armed for the next "
                    "delivery", task_id, round_ids)


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
    outcome_end: str = "abandoned", review_tier: Any = None,
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
        outcome_end=outcome_end,
        review_tier=review_tier,
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
# Reset soft-hides rounds. `before` narrows it to rounds created before that instant.
def _has_open_runner_question(task_id_column: Any, *, before: Optional[datetime] = None) -> Any:
    return select(literal(1)).where(
        TaskQuestion.task_id == task_id_column,
        TaskQuestion.status == "open",
        _not_a_cp_round(),
        visible_artifact_clause_joined(TaskQuestion.created_at),
        *(() if before is None else (TaskQuestion.created_at < before,)),
    ).exists()


def _answered_runner_park(task_id_column: Any) -> Any:
    """The row is parked on a RUNNER's question that a human has since resolved, with nothing still open: the
    answer found no live session to deliver into (`_settle_gate_if_nothing_outstanding` keeps it pending), so
    only a fresh runner reading the Q&A trail moves the task on.

    Positive evidence, as `_has_open_runner_question` is: the park's own text (`awaiting_response_body`, written
    verbatim by the park `record_question_round` makes) is a resolved runner round. The CP's wedged-dispatch gate
    parks on a text no runner round carries, and its checkout round is a CP round, so neither matches."""
    any_open = select(literal(1)).where(
        TaskQuestion.task_id == task_id_column,
        TaskQuestion.status == "open",
        visible_artifact_clause_joined(TaskQuestion.created_at),
    ).exists()
    answered = select(literal(1)).where(
        TaskQuestion.task_id == task_id_column,
        TaskQuestion.status != "open",
        TaskQuestion.question == Task.awaiting_response_body,
        _not_a_cp_round(),
        visible_artifact_clause_joined(TaskQuestion.created_at),
    ).exists()
    return and_(~any_open, answered)


def reclaim_awaiting_response_task(task_id: int, session: Session, *, node_fault: bool = False) -> bool:
    """Crash reclaim of a task parked on a RUNNER-RAISED question whose runner is gone — the
    `awaiting_response` analogue of the two `active` reclaimers above, and the state neither of them sees.

    With its session dead the row is unanswerable: answers are delivered into that session. `node_fault`
    means what it does in `reclaim_active_task`; the refund uses this door's source state.

    Guarded by `_has_open_runner_question` or `_answered_runner_park`, not the state. An answered (or
    dismissed: declining is a decision) park lands `ready` with its attempts reset, as `answer` would. A chat is reclaimed too (owner: a fresh session
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
    # Not a row whose runner the unanswered-question sweep stopped on purpose: its rounds are kept for the
    # operator and the answer relaunches it (`stop_runner_on_unanswered_question`).
    not_stopped = Task.question_runner_stopped_at.is_(None)
    # Answered after its runner died: the answer is the restart (`TRANSITIONS`' `answer` row resets attempts),
    # so the vanished runner must not spend the budget and land `system_failed` at the ceiling. Same transaction
    # as `end_attempt`, whose identical guard then lands it `ready`.
    answered = session.execute(
        update(Task)
        .where(
            Task.id == task_id, Task.state == AWAITING_RESPONSE, not_stopped,
            Task.persona.notin_(OPERATOR_CRITERION_PERSONAS), _answered_runner_park(Task.id),
        )
        .values(
            **({"dispatch_attempts": 0} if TRANSITION_BY_NAME["answer"].resets_attempts else {}),
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first() is not None
    if answered:
        # The fresh runner reads these answers from its Q&A trail; stamped, as `_resume_after_stopped_question_runner`
        # stamps them, so they do not ride that runner's own warm batches. Before `end_attempt`, which commits.
        session.execute(
            update(TaskQuestion)
            .where(
                TaskQuestion.task_id == task_id, TaskQuestion.status != "open", TaskQuestion.delivered_at.is_(None),
                visible_artifact_clause(TaskQuestion.created_at, session.scalar(
                    select(Task.artifacts_hidden_before).where(Task.id == task_id))),
            )
            .values(delivered_at=utcnow())
        )
    # As `reclaim_active_task`: the note names the node and VM the parked runner was on.
    trace = _vanished_trace(session, task_id)
    landed = end_attempt(
        task_id, session,
        reason=_vanished_reason(trace, parked=True),
        sources=(AWAITING_RESPONSE,),
        node=trace.node, note_detail=trace.detail, note_vm=trace.vm,
        guards=(
            not_stopped,
            or_(
                # A runner frozen inside its own `AskUserQuestion`, or one whose question was answered after it
                # died, for every non-interactive persona.
                and_(
                    Task.persona.notin_(OPERATOR_CRITERION_PERSONAS),
                    or_(_has_open_runner_question(Task.id), _answered_runner_park(Task.id)),
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
        # The attempts reset and stamp above are as orphaned as a refund when the reclaim declines: discard them.
        _discard_unused_refund(task_id, session, refunded=refunded or answered)
        return False
    log.info(
        "task %s parked on %s (its runner vanished) reclaimed by the reaper -> %s",
        task_id, "an answered question" if answered else "an unanswerable question", landed,
    )
    return True


def _stage_one_clause(cutoff: datetime) -> Any:
    """Stage one's guard (`stop_runner_on_unanswered_question`) on a correlated `Task`: not a chat, not already
    stopped, and an OPEN, VISIBLE runner round created before `cutoff`. Built here once for the write and for
    `unanswered_question_may_be_due`.

    The clock is the round's `created_at`, not `tasks.updated_at`, which moves for unrelated reasons. A runner
    round only (`_has_open_runner_question`), so the CP's own wedged-dispatch gate never matches."""
    return and_(
        Task.persona.notin_(OPERATOR_CRITERION_PERSONAS),
        Task.question_runner_stopped_at.is_(None),
        _has_open_runner_question(Task.id, before=cutoff),
    )


def _stage_two_clause(cutoff: datetime) -> Any:
    """Stage two's guard (`expire_timed_out_awaiting_response_task`) on a correlated `Task`: not a chat, and
    either an open runner round older than `cutoff` or a row stage one stopped before `cutoff` (with nothing
    open it must not hold its tree for ever). Built here once for the write and for
    `unanswered_question_may_be_due`."""
    return and_(
        Task.persona.notin_(OPERATOR_CRITERION_PERSONAS),
        or_(
            _has_open_runner_question(Task.id, before=cutoff),
            Task.question_runner_stopped_at < cutoff,
        ),
    )


def addressable_hold_filter(task_id_column: Any) -> tuple[Any, ...]:
    """The `Worktree` conditions for the hold `task_pipeline_cp.expire_awaiting_response` retries the answer
    hand-off against: the task's own `TASK` slot, launched and dispatched. Shared with
    `unanswered_question_may_be_due`."""
    return (
        Worktree.task_id == task_id_column,
        Worktree.state == slot_state.TASK,
        Worktree.launch_session_id.isnot(None),
        Worktree.dispatch_id.isnot(None),
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
    now = utcnow()
    hours = int(unanswered_question_timeout().total_seconds() // 3600)
    row = session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            Task.state == AWAITING_RESPONSE,
            _stage_one_clause(now - unanswered_question_timeout()),
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
                       record_transition=False, commit=True)
    send_minted_intents(issued)  # AFTER the commit — see `send_minted_intents`.
    log.warning(
        "task %s: its question went unanswered for over %sh — runner stopped, the row stays "
        "awaiting_response and answerable", task_id, hours,
    )
    return True


def unanswered_question_may_be_due(now: Optional[datetime] = None) -> Any:
    """True for an `awaiting_response` row (correlated to `Task`) that the parked-task sweep
    (`task_pipeline_cp.expire_awaiting_response`) could act on. A read-time hint that only skips rows; each
    stage's own guarded write stays the decision.

    Composed from the same clause builders as the writes (`_stage_two_clause`, `_stage_one_clause`,
    `addressable_hold_filter`), so it is never narrower than them. Left out, because they need the row at write
    time: the state (the caller's CASE applies it), `end_attempt`'s own guards (destroy pending), stage one's
    post-check, and the hand-off's lock and `_open_question_count`; `undelivered` stands in for the hand-off's
    own pending-answer read. A row that comes due after this read is acted on by the next pass."""
    now = now or utcnow()
    undelivered = select(literal(1)).where(
        TaskQuestion.task_id == Task.id,
        TaskQuestion.status != "open",
        TaskQuestion.delivered_at.is_(None),
    ).exists()
    return or_(
        _stage_two_clause(now - unanswered_question_release_after()),
        _stage_one_clause(now - unanswered_question_timeout()),
        and_(select(literal(1)).where(*addressable_hold_filter(Task.id)).exists(), undelivered),
    )


def expire_timed_out_awaiting_response_task(task_id: int, session: Session) -> bool:
    """Stage TWO of an unanswered question: FAIL the task and give its worktree back, work in progress
    included, once the question is older than `unanswered_question_release_after()`. True iff this call
    ended it; False is the ordinary answer and never an error.

    Per-task and atomic: every racing decision is taken inside the write. Stage one freed the runner;
    what remains is a tree with work in progress. It is failed, and `lease_drain._release_finished`
    returns the slot to be reset over — the WIP is wiped, by the operator's choice. Admits both a
    stage-one-stopped row and a still-live park.

    `final=True` lands `system_failed`: a re-dispatch would re-ask the same absent human. The question is
    quoted into `awaiting_response_body`. Excluded: the CP's own wedged-dispatch gate (no runner round;
    `_stage_two_clause`), and an `advisor` at any age. The age test is a guard on the write, so an answer
    in between cannot be overtaken."""
    days = unanswered_question_release_after().days
    cutoff = utcnow() - unanswered_question_release_after()
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
        guards=(_stage_two_clause(cutoff),),
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
