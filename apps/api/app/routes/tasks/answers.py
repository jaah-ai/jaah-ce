"""Answering a Task's questions: delivery to the live runner, Slack echo, rounds, ignore/revise, the answer lock."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Any, Awaitable, Callable, Literal, Optional, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

# `notify_text` is imported for `_has_glyph` alone: the answer gate and the Slack card must agree on which
# options render visibly.
from app import actors, edition_features, fleet_verdicts, live_runner, notify_targets, notify_text, slot_state
from app.advisory_lock import SessionLock
from app.auth import adopt_user
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.clock import utcnow
from app.db import release_connection
from app.db_async import DbDep, release
from app.events import emit_console_event
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser
from app.models.task_extras import ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.tasks import Task, TaskQuestion
from app.dispatch_compose.input_staging import _attachment_workdir
from app.persona_registry import DONE_CRITERION_OPERATOR, PERSONA_DONE_CRITERION
from app.task_artifacts import uploaded_clause, visible_artifact_clause, visible_artifact_clause_joined
from app.task_awaiting_response_parser import parse_response_options
from app.routes.tasks import checkout_gate
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, PAUSED, TRANSITION_BY_NAME
from app.task_lookup import get_task_or_404
from app.org_gate import require_org_active
from app.user_activity import record_task_note
from app.routes.tasks.policy import MAX_TASK_TEXT_LEN
from app.question_drafts import clear_round_drafts
from app.identities import operator_label
from app.routes.tasks.qa_render import (
    _answer_with_comment,
    _flatten,
    _load_task_questions_out,
    _neutralize_batch_delimiters,
    _normalized_comment,
    _question_options_out,
    _task_qa_entries,
)
from app.routes.tasks.task_wire import TaskOut, _task_attachments_meta, _task_out
from app.routes.tasks.awaiting import answer_delivery_in_flight, resume_dispatched_task_on_answer

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
log = logging.getLogger(__name__)
_T = TypeVar("_T")


# The advisory-lock class for "an `answer` submission for this task is in flight": `blocked` cannot be
# atomically claimed (it must stay `blocked` for the backstop), so the submission is serialized by a
# Postgres session-level advisory lock keyed `(ANSWER_LOCK_CLASSID, task_id)` — shared by every control-plane
# task, not a set in one process. Sibling of `cron_runtime.CRON_RUN_LOCK_CLASSID`; the two classes keep the
# two lock spaces apart.
ANSWER_LOCK_CLASSID = 0x616E7377  # 'answ'


class _AnswerLock(SessionLock):
    """The lock `_answer_guard` yields: `advisory_lock.SessionLock` on `(ANSWER_LOCK_CLASSID, task_id)`. A body
    releases early before a Slack mirror and the guard's `finally` releases again, which its `release` admits."""

    def __init__(self, task_id: int) -> None:
        super().__init__(ANSWER_LOCK_CLASSID, task_id, label=f"answer lock for task {task_id}")


@contextlib.contextmanager
def _answer_guard(task_id: int):
    """Hold the task's answer lock for the block, 409 if another submission already holds it (on any
    control-plane task). Yields the lock so a body can `release()` early (before a Slack mirror); the exit
    release is idempotent, so that stays correct."""
    lock = _AnswerLock(task_id)
    if not lock.acquire():
        raise _answer_in_flight_409(task_id)
    try:
        yield lock
    finally:
        lock.release()


def _answer_in_flight_409(task_id: int) -> HTTPException:
    return HTTPException(status_code=409, detail=f"an answer for task {task_id} is already being submitted")


@contextlib.asynccontextmanager
async def _answer_guard_async(task_id: int, steps: "_Steps"):
    """`_answer_guard` for an `async def` route: the lock's dedicated connection is opened and released
    on a worker thread, never on the event loop (a fresh TCP/TLS/auth handshake per submission). A block that
    raises (a racer losing the round: `_open_round_or_409`) has its steps' transaction aborted BEFORE that
    release hop, so the route session's connection, and the task row lock it took, are not held across it."""
    lock = _AnswerLock(task_id)
    loop = asyncio.get_running_loop()
    acquiring = loop.create_task(asyncio.to_thread(lock.acquire))
    try:
        held = await asyncio.shield(acquiring)
    except asyncio.CancelledError:
        # The request went away while the worker thread was still dialing: the thread finishes anyway and
        # may store the connection, so the `finally` below (never entered) cannot free it. Release once
        # the thread is done, off the loop — `release` is idempotent and a no-op when `acquire` returned
        # False, so it is safe to call unconditionally; a failed acquire holds nothing to release.
        def _release_when_done(done: "asyncio.Future[bool]") -> None:
            if done.cancelled() or done.exception() is not None:
                return
            loop.run_in_executor(None, lock.release)

        acquiring.add_done_callback(_release_when_done)
        raise
    if not held:
        raise _answer_in_flight_409(task_id)
    try:
        yield lock
    except BaseException:
        # Never let a failed rollback replace what is in flight (a 409, a cancellation): the endpoint's own
        # rollback (`db_async.release_after_async_endpoint`) still runs after this.
        with contextlib.suppress(Exception):
            await steps.abort()
        raise
    finally:
        await asyncio.to_thread(lock.release)


# Operator's free-text note beside their choice; stored in `activity_events`, so bounded like the answer.
# Not capped again on the wire. Absent/blank means "no comment", never a placeholder.
_COMMENT_FIELD = Field(default=None, max_length=MAX_TASK_TEXT_LEN)


# Ticked options of a multi-choice round, beside `answer`. `None` selects the single-answer path; `[]`
# is a recorded "ticked none". Legality single-homes in `_gate_answer_or_422`. 25 = Slack's
# `actions`-block ceiling (Slack's own limit, above the question card's `notify_text.OPTIONS_MAX_COUNT`
# button cap), mirrored by `GateForm` (a Pydantic `too_long`
# list `detail` cannot render in `api.ts`'s `apiDetail`).
_MAX_SELECTIONS = 25
_SelectionEntry = Annotated[str, Field(max_length=MAX_TASK_TEXT_LEN)]
# These caps bound what is walked, not the parse (FastAPI materializes the body first); a parse bound
# belongs in middleware.
_SELECTIONS_FIELD = Field(default=None, max_length=_MAX_SELECTIONS)
# Files the operator attached to an answer: ids of this task's own uploaded `attachment` rows, uploaded
# through the ordinary attachment routes first. Legality single-homes in `_answer_attachments_or_422`;
# the cap is mirrored by `GateForm`'s `MAX_ANSWER_ATTACHMENTS`.
_MAX_ANSWER_ATTACHMENTS = 20
_ATTACHMENT_IDS_FIELD = Field(default=None, max_length=_MAX_ANSWER_ATTACHMENTS)


class AnswerIn(BaseModel):
    # Optional so `selections` can replace it; with neither the route still 422s (`_gate_answer_or_422`).
    answer: Optional[str] = Field(default=None, max_length=MAX_TASK_TEXT_LEN)
    # The round the answerer was SHOWN. Resolved by `_open_round_or_409` UNDER its row lock, so the open
    # round cannot move between display and resolve. `None` resolves the oldest open.
    question_id: Optional[int] = None
    comment: Optional[str] = _COMMENT_FIELD
    # Accepted for the `task_questions` round branch only; the `awaiting_response_body` path refuses it.
    selections: Optional[list[_SelectionEntry]] = _SELECTIONS_FIELD
    attachment_ids: Optional[list[int]] = _ATTACHMENT_IDS_FIELD


class AnswerQuestionIn(BaseModel):
    # Typed resolution or a clicked option. Blank is a 422 unless `selections` carries the answer.
    answer: Optional[str] = Field(default=None, min_length=1, max_length=MAX_TASK_TEXT_LEN)
    # Omitted → the OLDEST open round. 409 if not open here.
    question_id: Optional[int] = None
    comment: Optional[str] = _COMMENT_FIELD
    selections: Optional[list[_SelectionEntry]] = _SELECTIONS_FIELD
    attachment_ids: Optional[list[int]] = _ATTACHMENT_IDS_FIELD


class IgnoreQuestionIn(BaseModel):
    """The `ignore-question` body: the round selector alone. `{}` targets the oldest open round."""

    question_id: Optional[int] = None


# Rounds one batch may resolve. Bounds what is walked; a gate rarely carries more than a handful.
_MAX_BATCH_ANSWERS = 50


class BatchAnswerEntryIn(AnswerQuestionIn):
    """One entry of `input-requests/answers`: an answer to a NAMED round (`AnswerQuestionIn`'s fields), or
    `ignore: true` to skip it. The round is required: a batch has no "oldest" to fall back to."""

    question_id: int
    ignore: bool = False


class BatchAnswersIn(BaseModel):
    answers: list[BatchAnswerEntryIn] = Field(min_length=1, max_length=_MAX_BATCH_ANSWERS)


class BatchAnswerResultOut(BaseModel):
    """What happened to one entry, in the order sent. `failed` carries the status and detail the entry's own
    single-round route would have answered; the round is left as it was."""

    question_id: int
    outcome: Literal["answered", "dismissed", "failed"]
    status_code: Optional[int] = None
    detail: Any = None


class BatchAnswersOut(BaseModel):
    task: TaskOut
    results: list[BatchAnswerResultOut]


class ReviseQuestionIn(BaseModel):
    """The `revise-question` body: the round to reopen. REQUIRED, unlike its siblings — the RESOLVED set
    has no "oldest" order, and a guess would silently reopen a different decision."""

    question_id: int


# ── Action route: answer ───────────────────────────────────────────────────────────────────────────
# Not `/respond`: `answer` and `answer-question` must stay distinguishable.
def _deliver_answer_to_live_runner(
    task: Task, question: str, answer: str, session: Session, *, legacy_gate: bool = False,
    answered_by: Optional[UUID] = None, question_ids: Optional[list[int]] = None, bare: bool = False,
    dismiss: bool = False, runner_view: Optional[dict] = None, dismissed_ids: Optional[list[int]] = None,
) -> Optional[str]:
    """Mint a `deliver_answer` slot intent for the LIVE runner blocked on this task's question. Returns the
    NODE ID the intent was minted on when one was minted, None when there is no live session to deliver to.

    The node id (truthy) keeps the log and the branch on one fact; `fleet_verdicts.issue_intents` pushes
    to it. A node id means warm: the runner is frozen in `AskUserQuestion`, the Task stays
    `awaiting_response` until `ack_slot_intent` flips it active, keeping context. None means cold: the
    caller takes `answer`'s `-> ready` and a fresh dispatch carries the answer. None is the ordinary case."""
    if task.state != AWAITING_RESPONSE:
        # Only a parked task has a waiting runner; an `active` triage runner that recorded a round without parking
        # reads the trail itself next turn.
        return None
    if task.run_agent_id is not None:
        # An SDK agent is not frozen in a pane: it polls its rounds (`GET .../question-batch`) and reads the
        # answer there, so nothing is pasted and no `deliver_answer` is minted. The resume the node's ack
        # would make for a built-in is made here, on the same edge (`resume_dispatched_task_on_answer`).
        # No live hold means no agent left to read it, and a roundless (`legacy_gate`) answer is one it cannot
        # read: either way the caller's cold path takes the row to `ready`, the answer in the trail.
        hold = slot_state.live_hold_for_task(session, task.id)
        if legacy_gate or hold is None or not hold["node_id"]:
            return None
        resume_dispatched_task_on_answer(task.id, session)
        return str(hold["node_id"])
    hold = live_runner.live_runner_hold(session, task.id)
    if hold is None:
        # No live hold, or an unknown session identity; delivering into an unidentified session is the injection
        # hazard `build_deliver_answer_intent` refuses, so resume cold.
        return None
    intent = fleet_verdicts.build_deliver_answer_intent(
        # Identity then display name — the id goes on the row, the name into the event id / log strings.
        hold["project_id"], hold["project"], hold["slot"],
        task_id=task.id,
        # Both halves are scrubbed here and nowhere else. The question half, agent-authored, is also defanged and
        # folded: `compose_answer_message` frames `Answer to your question '<q>': <a>`, so a quote or line break in
        # it could forge a second frame. Three layers, all kept: `_neutralize_batch_delimiters` (Cc/Cf/Zl/Zp plus
        # the NFKC delimiter table — what keeps line breaks out); `live_runner.scrub_control_chars`, the independent
        # terminal guarantee every pane write goes through (`_flatten` would carry a bare `ESC` through); `_flatten`,
        # collapsing leftover space runs.
        question=_flatten(live_runner.scrub_control_chars(_neutralize_batch_delimiters(question or ""))),
        answer=live_runner.scrub_control_chars(answer),
        session_id=hold["session_id"],
        reason=fleet_verdicts.deliver_answer_reason(task.id),
        # The task's whole input set, every delivery: the node writes only the files it lacks, so a file
        # attached to this answer is on disk at the path the answer names before the runner reads it.
        attachments=[meta.model_dump() for meta in _task_attachments_meta(session, task, None) or []],
        attachments_dir=str(_attachment_workdir(task.id)),
        # Who answered, for the task page's message list (`routes/task_control_messages.py`).
        sender_id=answered_by,
        # The rounds this hand-off carries, re-armed if a newer question supersedes it before the ack.
        question_ids=question_ids,
        bare=bare,
        # A task edit (`app/live_task_edits.py`): Escape the dialog, never its write-in row; the view it stamps.
        dismiss=dismiss,
        runner_view=runner_view,
        dismissed_ids=dismissed_ids,
    )
    # `issue_intents` commits and pushes, so the answer does not wait for the node's next pool tick.
    fleet_verdicts.issue_intents(session, hold["node_id"], [intent])
    log.info(
        "task %s: minted deliver_answer for %s#%s session %s (warm resume)",
        task.id, hold["project"], hold["slot"], hold["session_id"][:8],
    )
    # The node the intent was minted on — see this function's docstring for why the id rather than a bool.
    return str(hold["node_id"])


# ── Mirroring a resolved question back onto its Slack card ──────────────────────────────────────────
# `app/slack_answer_echo.py` owns the mirror. Both wrappers are best-effort, run after the resolving
# commit, and import locally to break the ring (it imports `slack_inbound`, which imports `answer_task`).


@contextlib.contextmanager
def _mirror_faults_swallowed(task_id: int):
    """Nothing the Slack mirror does may reach the operator as a failure of their answer.

    `echo_resolution` never raises, but the wrappers do their own work (the import, `operator_label` on an
    expired session, `_normalized_comment`) after the commit; an exception would report a landed answer
    as failed and invite a re-post. Same asymmetry as `task_notify`'s call site."""
    try:
        yield
    except Exception:
        log.exception(
            "task %s: mirroring the resolution to its Slack card failed — the resolution itself is "
            "recorded and the operator's request stands; only the card may still show its buttons",
            task_id,
        )


def _echo_answer_to_slack(
    task_id: int,
    *,
    question_id: Optional[int],
    answer: str,
    by: Optional[str],
    comment: Optional[str],
) -> None:
    """Tell the Slack mirror that an ANSWER was recorded here. Blocking; never raises.

    `question_id` is the resolved round, or `None` for the roundless legacy gate. `by` is captured before
    the commit (`operator_label` reads ORM attributes). The comment is normalized here.

    Most call sites release the answer lock first (the mutation is committed; holding it would 409 a
    retry). The warm legacy branch must keep holding it: it consumes no round, so a retry would mint a
    second `deliver_answer`."""
    if not edition_features.enabled("slack"):
        # No Slack in this edition, so no card to mirror onto.
        return
    with _mirror_faults_swallowed(task_id):
        from app import slack_answer_echo

        slack_answer_echo.echo_resolution(
            task_id=task_id,
            outcome=slack_answer_echo.OUTCOME_ANSWERED,
            question_id=question_id,
            answer=answer,
            by=by,
            comment=_normalized_comment(comment),
        )


def _echo_dismissal_to_slack(
    task_id: int, *, question_id: int, by: Optional[str]
) -> None:
    """Tell the Slack mirror that a round was DISMISSED here. Blocking; never raises.

    A separate wrapper, not an `outcome` argument, so no outcome literal is spelled outside
    `slack_answer_echo`."""
    if not edition_features.enabled("slack"):
        # No Slack in this edition, so no card to mirror onto.
        return
    with _mirror_faults_swallowed(task_id):
        from app import slack_answer_echo

        slack_answer_echo.echo_resolution(
            task_id=task_id,
            outcome=slack_answer_echo.OUTCOME_DISMISSED,
            question_id=question_id,
            by=by,
        )


# ── A chat has no answer screen: its answer channel is its own console ─────────────────────────────
def _refuse_chat_answer_door_or_409(task: Task, verb: str) -> None:
    """409 when `task` is an interactive chat. Guards all three operator answer doors; a no-op otherwise.

    All three end at `awaiting_response -> ready`, and a chat parks there every turn, so an Answer would
    launch a second runner into its occupied slot. The message says where to answer instead: the Web
    Console (as `apps/web/src/components/tasks/shared.tsx` already routes a chat's `?`), for callers
    like `app/slack_inbound.py` that bypass the console.

    It covers the per-round doors too: an advisor's hook posts `task_questions` rounds, resolved in the
    pane; an orphaned one is dropped by `runner_terminations.drop_undelivered_questions`. Keyed on the
    done-criterion, never an `advisor` literal."""
    if PERSONA_DONE_CRITERION.get(task.persona) != DONE_CRITERION_OPERATOR:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"task {task.id} is a chat, so it has no answer screen — {verb} would put it back in the "
            "dispatch queue and launch a second runner into the worktree slot the live conversation is "
            "already sitting in. Say it in the console instead: open the chat's Web Console and type "
            "your reply there."
        ),
    )


class _Steps:
    """Where an answer flow runs its database steps. A route runs them on its own async session
    (`_route_steps`); an in-process caller holding a sync session (`app/slack_inbound.py`) runs them on worker
    threads over it (`_thread_steps`). Either way no step spans the answer guard's dial or the Slack mirror:
    `let_go` hands a read-only transaction's connection back before the guard is taken."""

    __slots__ = ("run", "let_go", "abort")

    def __init__(
        self, run: Callable[..., Awaitable[Any]], let_go: Callable[[], Awaitable[Any]],
        abort: Callable[[], Awaitable[Any]],
    ) -> None:
        self.run = run  # `await run(fn, *args)` → `fn(session, *args)`
        self.let_go = let_go
        self.abort = abort  # roll back a step that raised inside the guard, before its release hop


async def _no_abort() -> None:
    """A thread-steps caller owns its sync session and its error handling; it holds no async-pool connection."""


def _route_steps(db: AsyncSession) -> _Steps:
    # The route session is rolled back on a raise anyway (`db_async.release_after_async_endpoint`); this does it
    # before the guard's release hop rather than after.
    return _Steps(db.run_sync, lambda: release(db), db.rollback)


def _thread_steps(session: Session) -> _Steps:
    return _Steps(
        lambda fn, *args: asyncio.to_thread(fn, session, *args),
        lambda: asyncio.to_thread(release_connection, session),
        _no_abort,
    )


@dataclass(frozen=True)
class _Answered:
    """What `answer`'s guarded half leaves the route to do: the Slack mirror's arguments, whether the guard is let
    go before it (`_echo_answer_to_slack`), and whether the response carries the task's rounds."""

    echo: dict[str, Any]
    release_first: bool
    with_rounds: bool


@router.post("/{task_id}/answer", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
async def answer_task(
    db: DbDep,
    task_id: int,
    body: AnswerIn,
    caller: AsyncCaller = Depends(current_caller),
) -> TaskOut:
    """Post a human's answer to an `awaiting_response` Task's question and resume it (DB-direct); any other
    state is a 409. Records the `answered` Q&A note and resumes `awaiting_response → ready` in one commit.

    With open `task_questions` rounds it resolves the oldest instead, and the outstanding-round gate
    decides whether anything resumes (`app/slack_inbound.py` calls this route, `answer_in_process`). Source,
    target and attempt reset come from `TRANSITIONS`' `answer` row."""
    return await _answer_task_flow(_route_steps(db), task_id, body, caller.actor, caller.principal)


async def answer_in_process(
    session: Session, *, task_id: int, body: AnswerIn, user: AdminUser, principal: Principal | None,
) -> TaskOut:
    """`answer_task` for an in-process caller holding its own sync session (`app/slack_inbound.py`): the same
    flow, its database steps on worker threads over that session."""
    return await _answer_task_flow(_thread_steps(session), task_id, body, user, principal)


async def _answer_task_flow(
    steps: _Steps, task_id: int, body: AnswerIn, user: AdminUser, principal: Principal | None,
) -> TaskOut:
    # The answer's shape is validated per branch (whether `selections` is legal depends on the gate), so a blank
    # answer on a wrong-state task reads as the 409.
    await steps.run(_answer_task_door_or_refuse, task_id)
    await steps.let_go()
    # Serialize concurrent submissions (the answer lock), checked before any mutation; always released.
    async with _answer_guard_async(task_id, steps) as guard:
        answered = await steps.run(_answer_task_in_guard, task_id, body, user, principal)
        if answered.release_first:
            await asyncio.to_thread(guard.release)  # before the mirror — see `_echo_answer_to_slack`
        await asyncio.to_thread(_echo_answer_to_slack, task_id, **answered.echo)
    # The answer is read once the guard is let go: its read would otherwise hold a connection across the release.
    if answered.with_rounds:
        return await steps.run(_resolved_task_out, task_id, principal, False)
    return await steps.run(_answered_task_out, task_id, principal)


def _answer_task_door_or_refuse(session: Session, task_id: int) -> None:
    """`answer`'s checks before the guard: the task exists, is not a chat, its org is active, and it is parked."""
    resume = TRANSITION_BY_NAME["answer"]
    task = get_task_or_404(task_id, session)
    # Before the state gate: a parked chat would otherwise pass it and resume into a second dispatch.
    _refuse_chat_answer_door_or_409(task, "answering here")
    require_org_active(session, task.org_id)  # resuming is dispatching (`app/org_gate.py`)
    if task.state not in resume.sources:
        # Names the table's own source set, so the 409 cannot describe a stale guard.
        legal = "' or '".join(sorted(resume.sources))
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is '{task.state}', not '{legal}' — already resolved",
        )


def _answered_task_out(session: Session, task_id: int, principal: Principal | None) -> TaskOut:
    return _task_out(get_task_or_404(task_id, session), session=session, principal=principal)


def _answer_task_in_guard(
    session: Session, task_id: int, body: AnswerIn, user: AdminUser, principal: Principal | None,
) -> _Answered:
    """`answer`'s guarded half: resolve the oldest open round, or the legacy gate (warm hand-off or cold resume),
    and commit."""
    user = adopt_user(session, user)
    resume = TRANSITION_BY_NAME["answer"]
    task = get_task_or_404(task_id, session)
    # ── The outstanding-round gate applies here too ────────────────────────────────────────────
    # Otherwise `/answer` would resume a Task with open rounds still open; `app/slack_inbound.py::_submit_answer`
    # calls it in-process. An answer here resolves the oldest open round through the same machinery.
    if _open_question_count(session, task_id):
        round_row = _open_round_or_409(session, task_id, body.question_id, "answer")
        attachments = _answer_attachments_or_422(session, task_id, body.attachment_ids)
        answer = _gate_answer_or_422(
            _answer_or_file_only(body.answer, body.selections, attachments), body.selections, round_row)
        comment = _comment_with_attachments(body.comment, task_id, attachments)
        if edition_features.enabled("compliance"):
            from app.governance import guard_escalation

            answer = guard_escalation.answer_for(session, task, round_row, answer, user, principal)
        answer = checkout_gate.answer_for(session, task, round_row, answer, comment, user, principal)
        _record_answered_round(session, task_id, round_row, answer, user, comment)
        _settle_gate_if_nothing_outstanding(task, session)
        round_id, by = round_row.id, operator_label(user)
        session.commit()
        session.expire_all()
        return _Answered(
            echo={"question_id": round_id, "answer": answer, "by": by, "comment": comment},
            release_first=True, with_rounds=True,
        )

    # The legacy gate has no round to close, and a warm hand-off leaves the row `awaiting_response` until the
    # node acks: the guard is released by then, so without this a late second answer mints a second delivery.
    # Keyed on the LIVE hold: with no runner left the cold resume below must stay open.
    hold = live_runner.live_runner_hold(session, task_id)
    if hold is not None and answer_delivery_in_flight(task, session, hold["session_id"]):
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is still delivering a message to its runner — try again once it lands",
        )
    # `round_row=None` refuses `selections`: multi-choice lives on `task_questions` rounds only.
    attachments = _answer_attachments_or_422(session, task_id, body.attachment_ids)
    answer = _gate_answer_or_422(
        _answer_or_file_only(body.answer, body.selections, attachments), body.selections, None)
    question = task.awaiting_response_body
    options = parse_response_options(task.awaiting_response_body) or []
    # The legacy gate has no per-round column, so the note is the only record; the note keeps the halves
    # apart and the wire joins them.
    comment = _comment_with_attachments(body.comment, task_id, attachments)
    record_task_note(
        # Deliberately empty: the label says "Question answered", and question, options, answer and comment ride
        # `data` for the Q&A trail and re-dispatch context. The Timeline shows when; the Q&A subtab shows what.
        session, task_id, "answered", "",
        {"question": question, "options": options, "answer": answer, "comment": comment},
        user=user,  # R4.9: attribute the resolution — a security-gate answer must not be anonymous
    )
    # The round path's classification: `options` here came verbatim from a runner-authored
    # `awaiting_response_body` (`task_pipeline_cp.block_task`), so a clicked option is machine text that must
    # be capped and defanged (`_classified_for_delivery`).
    wire = _classified_for_delivery(answer, options, comment)
    by = operator_label(user)  # while `user` is unexpired — see `_echo_answer_to_slack`
    echo = {"question_id": None, "answer": answer, "by": by, "comment": comment}
    # Warm first: if the asking runner is alive and frozen, hand it the answer and stay `awaiting_response`;
    # otherwise resume cold below.
    warm_node = _deliver_answer_to_live_runner(
        task, question or "", wire, session, legacy_gate=True, answered_by=user.id if user else None)
    if warm_node:
        session.commit()
        session.expire_all()
        # The guard is not released early here: a warm delivery consumes no round, and a retry during the mirror
        # would mint a second `deliver_answer`. A 409 on retry is the cheaper failure.
        return _Answered(echo=echo, release_first=False, with_rounds=False)
    # `dispatch_attempts=0` is `resets_attempts`. No `started_by_id`: answering a gate ends a pause within one
    # run (see `_settle_gate_if_nothing_outstanding`).
    session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state.in_(resume.sources))
        .values(
            state=resume.target,
            **({"dispatch_attempts": 0} if resume.resets_attempts else {}),
            **actors.task_actor_values(session),
        )
    )
    emit_console_event(session, entity="task", id=task_id, action=resume.target, commit=True)
    session.expire_all()
    return _Answered(echo=echo, release_first=True, with_rounds=False)


# ── The outstanding-round gate: what an open `task_questions` row holds up, and how it clears ───────
# The unit of resumption is the task, not the round: one `AskUserQuestion` call can carry several
# questions and any delivery unfreezes the session, so the gate clears on "nothing outstanding".
# An ignored (`dismissed`) round is never delivered — as if it was never asked. When nothing else is left,
# the runner still needs a message: the node's `Esc` ends its turn, and an idle session waits for input.
# This word wakes it and names nothing (runner charter §Autonomous mode, "Answer recovery").
_IGNORED_WAKE_TEXT = "Continue."
# Heads the rounds a superseded delivery carried (`awaiting.supersede_pending_answer_delivery`), re-quoted
# ahead of the new answers. Server text: no `_BATCH_DELIMITER_SUBSTITUTIONS` delimiter.
_EARLIER_ANSWERS_HEADER = "Earlier answers you may not have received:"
# Heads the rounds the operator revised AFTER their earlier resolution had reached the runner
# (`_revised_round_ids`): in a parked hand-off, after the rest of the batch; to an `active` runner, as the whole
# message (`_deliver_revisions_to_active_runner`). Server text: no delimiter.
_REVISED_ANSWERS_HEADER = "Revised answers, each replacing the answer you were given earlier:"
# What a revised round that was re-dismissed says in place of a resolution. Server text: no delimiter.
_WITHDRAWN_TEXT = "withdrawn, proceed as if it was never asked"
#: The `sender` a revised-answer `deliver_message` names on the task page's message list, with the round's id.
_REVISED_ANSWER_SENDER = "Revised answer"

# What a multi-choice round records when nothing is ticked: a real answer ("none of these"), not a
# dismissal, so it never trips the blank-answer 422 or leaves `chosen_resolution` NULL. Server text
# reaches the wire untouched, so it must contain no delimiter above and not `_COMMENT_LABEL_RE`'s phrase
# (pinned by a test).
_NO_SELECTION_TEXT = "none of the offered options"


def _joined_selection_text(picked: list[str]) -> str:
    """How a list of TICKED LABELS reads as one string — `", "`-joined, or `_NO_SELECTION_TEXT` when empty.

    The one home, also used by `app/slack_inbound.py`'s confirmation and card line, so both match the
    recorded `chosen_resolution`. `", "` follows `AskUserQuestion`'s documented multi-select shape, and
    the runner receives prose. The empty case belongs here: `""` would be neither dismissal nor answer."""
    return ", ".join(picked) if picked else _NO_SELECTION_TEXT


def _open_question_count(session: Session, task_id: int) -> int:
    """How many `open` `TaskQuestion` rounds this task still carries — the outstanding count the gate reads.
    A count for the caller's log line; 0, never None.

    Reset-soft-deleted rounds are not outstanding (`app/task_artifacts.py`); counting one would hold the
    gate against a question nobody can see or resolve (`_open_round_or_409` excludes it too)."""
    return session.scalar(
        select(func.count())
        .select_from(TaskQuestion)
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(
            TaskQuestion.task_id == task_id,
            TaskQuestion.status == "open",
            visible_artifact_clause_joined(TaskQuestion.created_at),
        )
    ) or 0


def _classified_for_delivery(
    answer: str, options: list[str], comment: Optional[str], *, machine_authored: bool = False
) -> str:
    """One resolution as it goes on the WIRE: the answer half classified by provenance and, when it is
    machine-authored, flattened and defanged — then joined to the operator's own (trusted, verbatim) note.

    Neither half is truncated; the trusted half keeps its newlines (the wire is a bracketed paste). The
    single home for both gates (the round path and the legacy `awaiting_response_body` path, whose
    options `task_pipeline_cp.block_task` wrote verbatim), so one fix covers both doors.

    `machine_authored` marks provenance the options cannot show, e.g. a joined multi-select answer."""
    # Defang whenever the round offered options, not on membership: a transport can reshape a label (Slack's
    # button value is `_clip(sanitize(option), _BUTTON_VALUE_MAX)`, `app/slack_blocks.py`), and a
    # misclassified option could forge a resolved round. Unprovable-human is treated as runner text; cost:
    # typed text on an option round is defanged and flattened. Deferred fix: record the answering shape.
    if machine_authored or options:
        head = _neutralize_batch_delimiters(_flatten(answer))
    else:
        head = answer
    return _answer_with_comment(head, comment or "")


def _resolution_for_delivery(row: TaskQuestion) -> str:
    """A resolved round's resolution, WHOLE, and — when the operator did not TYPE it — flattened and stripped
    of the enumeration's delimiters like any other machine-authored text.

    On an option round the clicked string is runner-authored text, so provenance is derived from
    `row.options` (a typed answer equal to an option counts as machine — the safe direction). The comment
    is classified separately and appended after, which is why it is its own column; both go over whole.

    Multi-select rounds pass `machine_authored=True` (see `_classified_for_delivery`); a zero-tick
    `_NO_SELECTION_TEXT` is defanged harmlessly (its wording is pinned delimiter-free). ANSWERED rounds only:
    a dismissed one has no resolution (a revised one reads `_WITHDRAWN_TEXT`, `_compose_batch_answer`)."""
    # A property of the round, not of how this answer was composed: typed prose on a multi round is also
    # defanged and flattened. Deliberate (errs safe, loses no words; pinned by a test).
    return _classified_for_delivery(
        row.chosen_resolution or "",
        _question_options_out(row.options) or [],
        row.comment,
        machine_authored=row.multi_select,
    )


def _open_round_or_409(
    session: Session, task_id: int, question_id: Optional[int], action: str
) -> TaskQuestion:
    """The open round a resolve-route targets — the one named by `question_id`, else the OLDEST open — or a
    409 naming what could not be done. One rule for both resolve routes.

    Takes the task row lock first; that, not the answer lock, serializes the gate: these `def` routes
    run on worker threads with their own transactions, and two requests could each resolve one round
    reading the other as open, leaving a gate nothing can clear."""
    session.execute(select(Task.id).where(Task.id == task_id).with_for_update())
    stmt = notify_targets.open_rounds(task_id)
    if question_id is not None:
        stmt = stmt.where(TaskQuestion.id == question_id)
    row = session.scalars(stmt).first()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} has no open question"
                + (f" with id {question_id}" if question_id is not None else "")
                + f" — nothing to {action}"
            ),
        )
    return row


def _resolved_round_or_409(session: Session, task_id: int, question_id: int) -> TaskQuestion:
    """The RESOLVED round `revise-question` targets — the named one, answered or dismissed — or a 409 naming
    what could not be done. Separate from `_open_round_or_409`: the two want opposite halves of the
    status vocabulary.

    Same task row lock first: a revise races the gate's settle arm, which could otherwise stamp
    `delivered_at` over a row just reopened. `answered` and `dismissed` are revisable; an `open` round is
    refused (nothing to revise)."""
    session.execute(select(Task.id).where(Task.id == task_id).with_for_update())
    # Soft-deleted rounds are excluded: reopening one would put an abandoned attempt's question in front of
    # its replacement (`app/task_artifacts.py`).
    row = session.scalars(
        select(TaskQuestion)
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(
            TaskQuestion.task_id == task_id,
            TaskQuestion.id == question_id,
            TaskQuestion.status != "open",
            visible_artifact_clause_joined(TaskQuestion.created_at),
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} has no resolved question with id {question_id} — nothing to revise"
            ),
        )
    return row


def _gate_answer_or_422(
    answer: Optional[str], selections: Optional[list[str]], round_row: Optional[TaskQuestion]
) -> str:
    """The text to STORE as this round's `chosen_resolution`, from whichever of the two answer shapes the
    caller sent — or a 422 naming exactly what is wrong. Single home for every rule about `selections`.

    Shared by `answer_question` and both branches of `answer_task` (which `app/slack_inbound.py` drives).
    Called after `_open_round_or_409`, since multi-select is a property of the picked round; `round_row`
    `None` is the legacy gate, where `selections` is refused (no `multiSelect` source there).

    `selections is None` is the typed-answer path; `[]` is an answer, "ticked none"
    (`_NO_SELECTION_TEXT`). Membership against the stored options is enforced (a foreign entry is a bug
    or forgery handed back to the runner as a human decision); duplicates collapse, first-seen order."""
    if selections is None:
        # The historical path, byte for byte, including storing the answer untrimmed.
        if not (answer or "").strip():
            raise HTTPException(status_code=422, detail="answer is required")
        return answer or ""

    # Exclusivity is about presence, not blankness: `.strip()` misses U+200B, so blankness tests would
    # answer one body two ways. Any `answer` sent beside `selections` conflicts; `answer: null` is absence.
    if answer is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "answer and selections are mutually exclusive — send the typed answer or the ticked "
                "options, not both"
            ),
        )
    if round_row is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "selections is only valid on a task_questions round — this task's legacy question gate "
                "has no multi-select flag, so answer it with a single answer"
            ),
        )
    if not round_row.multi_select:
        raise HTTPException(
            status_code=422,
            detail=(
                f"question {round_row.id} is not a multi-select round — it accepts one answer, "
                "not ticked options"
            ),
        )

    # A blank-glyph option is not a legal selection (it would store an empty `chosen_resolution` past the
    # blank-answer 422). `notify_text._has_glyph` is the shared test, so card and gate agree.
    offered = [
        opt for opt in (_question_options_out(round_row.options) or []) if notify_text._has_glyph(opt)
    ]
    picked: list[str] = []
    for entry in selections:
        if entry not in offered:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"selection {entry!r} is not one of question {round_row.id}'s offered options"
                ),
            )
        if entry not in picked:
            picked.append(entry)
    joined = _joined_selection_text(picked)
    # The joined value takes the typed answer's bound: `_MAX_SELECTIONS` bounds the count, nothing bounds
    # option length. Refused, not truncated: a cut resolution misstates the decision.
    if len(joined) > MAX_TASK_TEXT_LEN:
        raise HTTPException(
            status_code=422,
            detail=(
                f"the {len(picked)} ticked options of question {round_row.id} join to {len(joined)} "
                f"characters, past the {MAX_TASK_TEXT_LEN} an answer may be"
            ),
        )
    return joined


# What an answer records when the operator sent files and no words: a real answer, so it clears the
# blank-answer 422. Server text, so it carries no `_BATCH_DELIMITER_SUBSTITUTIONS` delimiter.
_FILE_ONLY_ANSWER_TEXT = "See the attached files."


def _answer_attachments_or_422(
    session: Session, task_id: int, attachment_ids: Optional[list[int]]
) -> list[TaskAttachment]:
    """The files an answer carries — this task's own uploaded `attachment` rows, in the order sent — or a
    422 naming the ids that are not.

    Only the task's OWN rows: `_attachment_paths_line` names each file in this task's attachment workdir,
    which `materialize_attachments` fills from `_task_attachments_meta`. A pending row (presigned PUT never
    confirmed) has no bytes to stage, and a runner-authored kind is not the operator's input."""
    wanted = list(dict.fromkeys(attachment_ids or []))
    if not wanted:
        return []
    rows = {
        row.id: row
        for row in session.scalars(
            select(TaskAttachment).where(
                TaskAttachment.id.in_(wanted),
                TaskAttachment.task_id == task_id,
                TaskAttachment.kind == ATTACHMENT_KIND_FILE,
                uploaded_clause(),
            )
        ).all()
    }
    missing = [att_id for att_id in wanted if att_id not in rows]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                f"attachment(s) {', '.join(map(str, missing))} are not uploaded files of task {task_id} — "
                "attach the file again and resend the answer"
            ),
        )
    return [rows[att_id] for att_id in wanted]


def _attachment_paths_line(task_id: int, attachments: list[TaskAttachment]) -> str:
    """Where each attached file sits on the runner's disk, as one block the runner can act on.

    The basename rule MIRRORS `apps/jaahd/fleet_dispatch.py::materialize_attachments` (a separate
    deployable): `<id>_<basename>`, falling back to `attachment-<id>`. A drift names a path that is not there."""
    workdir = _attachment_workdir(task_id)
    paths = []
    for att in attachments:
        name = PurePosixPath(att.filename or "").name or f"attachment-{att.id}"
        paths.append(f"- {workdir / f'{att.id}_{name}'}")
    return "Attached files (open each with Read):\n" + "\n".join(paths)


def _comment_with_attachments(
    comment: Optional[str], task_id: int, attachments: list[TaskAttachment]
) -> Optional[str]:
    """The comment as STORED and delivered: the operator's own words, then the attached files' paths.

    Folded into the comment so every surface that already carries it — warm delivery, the batch, the
    cold Q&A trail, the Slack echo, the console history — names the files with no second channel."""
    text = _normalized_comment(comment)
    if not attachments:
        return text
    line = _attachment_paths_line(task_id, attachments)
    return f"{text}\n\n{line}" if text else line


def _answer_or_file_only(
    answer: Optional[str], selections: Optional[list[str]], attachments: list[TaskAttachment]
) -> Optional[str]:
    """`answer`, or `_FILE_ONLY_ANSWER_TEXT` when the operator sent files and nothing else — so
    `_gate_answer_or_422`'s blank-answer rule never refuses a file-only answer."""
    if attachments and selections is None and not (answer or "").strip():
        return _FILE_ONLY_ANSWER_TEXT
    return answer


def _compose_batch_answer(rows: list[TaskQuestion], rounds: dict[int, int]) -> str:
    """The `1) <question> → <resolution>` enumeration handed to the runner when a gate clears with SEVERAL
    undelivered resolutions — one line per round, every round, nothing elided, with the agent-authored
    half flattened and defanged. A dismissed round reaches here only as a revision (`_revised_round_ids`) and
    reads `_WITHDRAWN_TEXT`.

    Lossless by construction. Rounds are newline-joined because a resolution may be multi-line; the
    leading `N)` is the delimiter, and only the operator's half can contain a newline (see the header
    above `_BATCH_DELIMITER_SUBSTITUTIONS`)."""
    parts: list[str] = []
    for row in rows:
        # The trail's round number, not this subset's position, matching every other surface.
        idx = rounds[row.id]
        # Agent-authored: flattened and defanged (see `_BATCH_DELIMITER_SUBSTITUTIONS`), not truncated.
        question = _neutralize_batch_delimiters(_flatten(row.question))
        resolution = _WITHDRAWN_TEXT if row.status == "dismissed" else _resolution_for_delivery(row)
        parts.append(f"{idx}) {question} → {resolution}")
    return "\n".join(parts)


def _round_ordinals(session: Session, task: Task) -> dict[int, int]:
    """Round id → its ordinal in the full visible trail (see `_compose_batch_answer`, `_load_task_questions_out`)."""
    return {
        row_id: idx
        for idx, row_id in enumerate(session.scalars(
            select(TaskQuestion.id)
            .where(
                TaskQuestion.task_id == task.id,
                visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
            )
            .order_by(TaskQuestion.created_at, TaskQuestion.id)
        ).all(), start=1)
    }


def _revised_round_ids(session: Session, task: Task, rows: list[TaskQuestion]) -> set[int]:
    """The ROWS whose earlier resolution had already reached the runner when `revise-question` reopened them —
    the `previous_delivered` flag on a `task_note:answer_revised` note. A round revised before its first hand-off
    is not one: the runner never saw the answer it would replace."""
    if not rows:
        return set()
    wanted = {str(row.id): row.id for row in rows}
    hits = session.scalars(
        select(ActivityEvent.data["question_id"].astext).where(
            ActivityEvent.kind == "task_note:answer_revised",
            ActivityEvent.data["id"].astext == str(task.id),
            ActivityEvent.data["previous_delivered"].astext == "true",
            ActivityEvent.data["question_id"].astext.in_(list(wanted)),
            visible_artifact_clause(ActivityEvent.created_at, task.artifacts_hidden_before),
        )
    ).all()
    return {wanted[hit] for hit in hits if hit in wanted}


def _deliver_revisions_to_active_runner(session: Session, task: Task, undelivered: list[TaskQuestion]) -> None:
    """An `active` runner has no dialog to answer into, so the revised rounds of UNDELIVERED reach it as a
    `deliver_message` — `_REVISED_ANSWERS_HEADER` and their enumeration, the way a description edit reaches it
    (`app/live_task_edits.py`). Rounds answered for the first time are left to the trail, as before. With nothing
    minted (no live, identified runner; an SDK agent, which polls its rounds) the revised rows go back to
    pending, so a later hand-off carries them. Stamps already set by the caller; `issue_intents` commits them."""
    revised_ids = _revised_round_ids(session, task, undelivered)
    revised = [row for row in undelivered if row.id in revised_ids]
    if not revised:
        return
    rounds = _round_ordinals(session, task)
    # One message per round, its sender naming the round by id: `issue_intents` supersedes a pending message of
    # the same sender, so a newer revision of THIS round replaces an undelivered older one, never another round's.
    messages = [
        (f"{_REVISED_ANSWERS_HEADER}\n{_compose_batch_answer([row], rounds)}", f"{_REVISED_ANSWER_SENDER} #{row.id}")
        for row in revised
    ]
    if task.run_agent_id is None and live_runner.deliver_messages_to_live_runner(session, task.id, messages):
        return
    for row in revised:
        row.delivered_at = None
    log.info("task %s: %s revised answer(s) found no live runner to message — pending the next hand-off",
             task.id, len(revised))


def _delivery_wire(
    session: Session, task: Task, undelivered: list[TaskQuestion]
) -> tuple[str, str, bool]:
    """`(question, answer, bare)` for the hand-off of `undelivered` — the runner's whole message.

    Dismissed rounds are left out, as if never asked. Rounds resolved before the newest round was asked are
    the ones a superseded delivery carried (`awaiting.supersede_pending_answer_delivery`): re-quoted under
    `_EARLIER_ANSWERS_HEADER`. Revised rounds (`_revised_round_ids`) follow under `_REVISED_ANSWERS_HEADER`,
    a re-dismissed one as withdrawn. One answered round and nothing else keeps the single-round frame; anything
    else is the enumeration with no question. Nothing to say is `_IGNORED_WAKE_TEXT`, `bare` so the node
    sends it without the `Answer: ` frame."""
    revised_ids = _revised_round_ids(session, task, undelivered)
    revised = [row for row in undelivered if row.id in revised_ids]
    answered = [row for row in undelivered if row.status != "dismissed" and row.id not in revised_ids]
    if not answered and not revised:
        return "", _IGNORED_WAKE_TEXT, True
    newest = max(row.created_at for row in undelivered)
    earlier = [row for row in answered if row.resolved_at is not None and row.resolved_at < newest]
    current = [row for row in answered if row not in earlier]
    if len(current) == 1 and not earlier and not revised:
        # The single-round path is agent-authored too, and common. It renders as a quoted frame, so a question
        # closing the quote or ending the line could forge a frame; hence `'` in the table and the flattening.
        row = current[0]
        return _neutralize_batch_delimiters(_flatten(row.question)), _resolution_for_delivery(row), False
    rounds = _round_ordinals(session, task)
    parts = [_EARLIER_ANSWERS_HEADER, _compose_batch_answer(earlier, rounds)] if earlier else []
    if current:
        parts.append(_compose_batch_answer(current, rounds))
    if revised:
        parts += [_REVISED_ANSWERS_HEADER, _compose_batch_answer(revised, rounds)]
    return "", "\n".join(parts), False


def _record_answered_round(
    session: Session, task_id: int, round_row: TaskQuestion, answer: str, user: AdminUser,
    comment: Optional[str] = None,
) -> None:
    """Resolve ONE open round as answered: the durable per-round decision plus its Q&A note. Shared by
    `answer_question` and `answer_task`. Delivery and resume live in
    `_settle_gate_if_nothing_outstanding`. `comment` is its own column and note key (see
    `_resolution_for_delivery`)."""
    round_row.status = "answered"
    round_row.chosen_resolution = answer
    clear_round_drafts(session, round_row.id)
    round_row.comment = _normalized_comment(comment)
    # Who decided and when, on the round itself, so the console can print a byline beside this resolution
    # (see `models/tasks.py::TaskQuestion.resolved_by_id`).
    round_row.resolved_by_id = user.id
    round_row.resolved_at = utcnow()
    record_task_note(
        # Empty, as above — the round's content rides `data`.
        session, task_id, "answered", "",
        {
            # Which round this note resolved: the question text is not a key. Consumed by `QAEntryOut.question_id`;
            # no backfill possible.
            "question_id": round_row.id,
            "question": round_row.question,
            # The round's short name, snapshotted: this note is the only record `_task_qa_entries` reads.
            "header": round_row.header,
            "options": round_row.options or [],
            "answer": answer,
            "comment": round_row.comment,
            # The provenance the prompt reader needs (`_task_qa_entries` reads notes, and a joined multi answer
            # defeats membership). No backfill owed: no round before migration 0117 was multi-select.
            "multi_select": round_row.multi_select,
        },
        # Attribute the resolution — a security-gate answer must not be anonymous.
        user=user,
    )


def retry_answer_delivery_after_launch(
    session: Session, *, worktree_id: UUID, generation: int, dispatch_id: str,
    task_id: int, session_id: str,
) -> None:
    """Retry pending resolutions against a fenced launched session, from its ack or the parked-task sweep."""
    from app.models.worktrees import Worktree

    lock = _AnswerLock(task_id)
    # An operator's in-flight submission wins; the recurring parked-task sweep retries next pass.
    if not lock.acquire():
        return
    try:
        task = session.scalar(
            select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True)
        )
        if task is None or task.state != AWAITING_RESPONSE:
            return
        if _open_question_count(session, task_id):
            return
        hold = session.scalar(select(Worktree.id).where(
            Worktree.id == worktree_id,
            Worktree.task_id == task_id,
            Worktree.state == slot_state.TASK,
            Worktree.generation == generation,
            Worktree.dispatch_id == dispatch_id,
            Worktree.launch_session_id == session_id,
        ).with_for_update())
        if hold is None:
            return
        _settle_gate_if_nothing_outstanding(task, session)
        session.commit()
    finally:
        # Release row locks even on a no-op before allowing another answer submission.
        session.rollback()
        lock.release()


def _settle_gate_if_nothing_outstanding(task: Task, session: Session) -> None:
    """Hand every UNDELIVERED resolution to the runner and resume the Task — but ONLY once no round is still
    open. A no-op while anything is outstanding.

    The shared tail of both resolve routes: the runner is woken exactly once, with everything. Resolved
    rows wait with `delivered_at IS NULL`; on clearing, one row keeps the single-round wire shape and
    several become the enumeration. Without an addressable runner they remain pending so its launch
    acknowledgement can retry the hand-off.

    No `started_by_id` stamp (rule above `_start`): answering is not starting. A gate is a
    pause inside one run whose starter already answers for its pushes; a colleague who unblocks it must
    not inherit them. Start/Run/Retry re-attribute explicitly."""
    outstanding = _open_question_count(session, task.id)
    if outstanding:
        # The gate holds but the trail changed: emit, or a second console tab keeps showing resolved rounds as
        # outstanding. State unchanged, so the event names the current state.
        emit_console_event(session, entity="task", id=task.id, action=task.state)
        log.info(
            "task %s: %s question round(s) still open — holding the gate, nothing delivered yet",
            task.id, outstanding,
        )
        return

    # Soft-deleted rounds are excluded from the hand-off: delivering one would answer a question the next
    # runner never asked.
    undelivered = list(session.scalars(
        select(TaskQuestion)
        .where(
            TaskQuestion.task_id == task.id,
            TaskQuestion.status != "open",
            TaskQuestion.delivered_at.is_(None),
            visible_artifact_clause(
                TaskQuestion.created_at, task.artifacts_hidden_before),
        )
        .order_by(TaskQuestion.created_at, TaskQuestion.id)
    ).all())
    if not undelivered:
        # Nothing to hand over (all already delivered); resuming on an empty batch would re-dispatch for nothing.
        return

    question, answer, bare = _delivery_wire(session, task, undelivered)

    # ── The stamp goes on before the intent is minted ──────────────────────────────────────────────
    # `issue_intents` ends in `session.commit()`; stamping afterwards splits the hand-off across two
    # transactions, and a rollback of the second would leak the batch.
    delivered_at = utcnow()
    for row in undelivered:
        row.delivered_at = delivered_at

    # Warm only: a live runner frozen on this round gets the answer in its pane and the row stays
    # `awaiting_response` until the ack. No cold resume (owner decision): rounds exist only while a runner is
    # frozen in `AskUserQuestion`. During a transport hand-off the replacement may not have reported its
    # session yet; leave that batch pending until its launch acknowledgement retries delivery.
    # Re-read under a row lock: the unanswered-question sweep may have stopped the runner since the load, and
    # the lock orders this against its UPDATE (see `stop_runner_on_unanswered_question`'s post-check).
    # A control-plane round has no runner to hand it to: the control plane enacts it (`checkout_gate.settle`).
    if checkout_gate.settle(session, task, undelivered):
        return
    if task.state == ACTIVE:
        # The batch this round rode was delivered and the runner is working: a revision is messaged, the rest
        # stays the trail's (see `_deliver_answer_to_live_runner`'s `active` note).
        _deliver_revisions_to_active_runner(session, task, undelivered)
        emit_console_event(session, entity="task", id=task.id, action=task.state)
        return
    if task.state == PAUSED:
        # No runner: an operator's Stop kept this round (`runner_terminations._QUESTIONS_KEPT_ANSWERABLE`) and
        # the work waits for its Start. Nothing to deliver into and nothing to resume; the prompt is composed
        # after the Start, so the next runner reads the answer from its Q&A trail, and the stamp keeps it out of
        # that runner's own warm batches. Not `ready`: its prompt may already list the round as NOT ANSWERED, so
        # that answer stays pending delivery (below) rather than stamped and lost.
        emit_console_event(session, entity="task", id=task.id, action=task.state)
        return
    runner_stopped = session.scalar(
        select(Task.question_runner_stopped_at).where(Task.id == task.id).with_for_update()
    )
    if task.state == AWAITING_RESPONSE and runner_stopped is not None:
        # The one cold resume left: the CP stopped this runner on purpose while its question waited
        # (`stop_runner_on_unanswered_question`), so a fresh runner is the plan; it gets the answers via
        # `_task_qa_trail`, and the stamp keeps them out of later warm batches.
        _resume_after_stopped_question_runner(task, session)
        return
    # One answerer names the delivery; a batch answered by several people has no single sender.
    answerers = {row.resolved_by_id for row in undelivered}
    warm = _deliver_answer_to_live_runner(
        task, question, answer, session,
        answered_by=next(iter(answerers)) if len(answerers) == 1 else None,
        question_ids=[row.id for row in undelivered], bare=bare,
    )
    # The gate cleared: emit, or the trail-finishing resolution would be the only one publishing nothing.
    # State unchanged (a warm hand-off stays parked until the ack), as in the still-open branch.
    emit_console_event(session, entity="task", id=task.id, action=task.state)
    if not (warm or task.state == ACTIVE):
        # No intent was minted (and therefore no delivery commit occurred). Keep the durable resolutions
        # pending rather than claiming a hand-off to a runner that had no addressable session.
        for row in undelivered:
            row.delivered_at = None
        log.info(
            "task %s: gate cleared with no live runner to deliver to (state %s) — %s resolution(s) "
            "recorded and pending delivery after launch",
            task.id, task.state, len(undelivered),
        )


def _resume_after_stopped_question_runner(task: Task, session: Session) -> None:
    """`awaiting_response -> ready` for a row whose question's runner was stopped, once its last round
    has cleared. Does NOT commit — `_settle_gate_if_nothing_outstanding`'s caller owns the transaction.

    `TRANSITIONS`' `answer` row is the authority (same target, `resets_attempts`). The runner lands in
    the slot the task still holds, or a freshly leased one if the clean tree was released. Guarded on the
    column as well as the state; the claim clears the column."""
    resume = TRANSITION_BY_NAME["answer"]
    moved = session.execute(
        update(Task)
        .where(
            Task.id == task.id,
            Task.state == AWAITING_RESPONSE,
            Task.question_runner_stopped_at.isnot(None),
        )
        .values(
            state=resume.target,
            github_synced_at=utcnow(),
            **({"dispatch_attempts": 0} if resume.resets_attempts else {}),
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    emit_console_event(session, entity="task", id=task.id, action=resume.target if moved else task.state,
                       record_transition=bool(moved))
    if moved:
        log.info(
            "task %s: every question answered after its runner was stopped — resumed %s for a fresh "
            "runner carrying the Q&A trail", task.id, resume.target,
        )


def _answer_open_round(
    session: Session, task: Task, body: AnswerQuestionIn, user: AdminUser, principal: Optional[Principal],
) -> tuple[int, str, Optional[str]]:
    """Resolve ONE open round as answered — `(round id, recorded answer, stored comment)` — or raise the
    409/422/403 naming why it was left open. Delivery and resume are the caller's
    (`_settle_gate_if_nothing_outstanding`), so `answer-question` and the batch route share every rule.

    Must run inside the answer guard: a select-then-lock order would let two requests pick the same row."""
    question = _open_round_or_409(session, task.id, body.question_id, "answer")
    attachments = _answer_attachments_or_422(session, task.id, body.attachment_ids)
    answer = _gate_answer_or_422(
        _answer_or_file_only(body.answer, body.selections, attachments), body.selections, question)
    comment = _comment_with_attachments(body.comment, task.id, attachments)
    # A guard stuck-limit round's answer may file a waiver and becomes what the runner is told (§7). An edition
    # without compliance has no guard round.
    if edition_features.enabled("compliance"):
        from app.governance import guard_escalation

        answer = guard_escalation.answer_for(session, task, question, answer, user, principal)
    # A checkout round's branch change is applied before the round is recorded (`checkout_gate`).
    answer = checkout_gate.answer_for(session, task, question, answer, comment, user, principal)
    # A branch change commits, which lets the task row go: take it again so the round is still written under it
    # (the lock order every writer keeps — `_open_round_or_409`). A no-op when the lock is still held.
    session.execute(select(Task.id).where(Task.id == task.id).with_for_update())
    _record_answered_round(session, task.id, question, answer, user, comment)
    return question.id, answer, comment


def _dismiss_open_round(session: Session, task_id: int, question_id: Optional[int], user: AdminUser) -> int:
    """Resolve ONE open round as `dismissed` (see `ignore_question`) and return its id, or 409. Delivery and
    resume are the caller's. Must run inside the answer guard, like `_answer_open_round`."""
    question = _open_round_or_409(session, task_id, question_id, "ignore")
    question.status = "dismissed"
    clear_round_drafts(session, question.id)
    # Attributed exactly like an answer (declining is a decision), so the console prints "Dismissed by …";
    # NULL `chosen_resolution` says which decision (see `models/tasks.py::TaskQuestion.resolved_by_id`).
    question.resolved_by_id = user.id
    question.resolved_at = utcnow()
    # `chosen_resolution` is NOT written — see `ignore_question`. The absence is the record.
    record_task_note(
        # Empty, as above.
        session, task_id, "ignored", "",
        # `question_id` and `header` for `_record_answered_round`'s reasons: `_task_qa_entries` reads this note
        # (dismissals can be revised from the trail).
        {
            "question_id": question.id,
            "question": question.question,
            "header": question.header,
            "options": question.options or [],
        },
        # Declining to answer a gate IS a decision, and it must not be anonymous either.
        user=user,
    )
    return question.id


# ── Action route: answer-question (multi-round Q&A) ─────────────────────────────────────────────────
@router.post("/{task_id}/answer-question", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
async def answer_question(
    db: DbDep,
    task_id: int,
    body: AnswerQuestionIn,
    caller: AsyncCaller = Depends(current_caller),
) -> TaskOut:
    """Answer the current open `TaskQuestion` round on a triage/blocked Task and resume the runner — the
    multi-round successor to the single-shot `answer`. Resolves the target round (named, else oldest),
    flips it `open → answered`, records the note, and hands the trail over only when no round remains
    open, since a partial delivery unfreezes the runner with the rest unanswered.

    No task-state guard: an open round is answerable on an `active` triage runner too
    (`_open_round_or_409` is the guard); on a terminal task the answer is recorded with nobody to tell.
    The shape is validated after the round is picked (`_gate_answer_or_422`). A chat is refused
    (`_refuse_chat_answer_door_or_409`)."""
    steps = _route_steps(db)
    # Refused BEFORE the in-flight guard is taken, so a refusal never has to release one.
    await steps.run(_resolve_door_or_refuse, task_id, "answering a round here", True)
    await steps.let_go()
    # The same in-flight guard as `answer`, so two racing requests cannot both answer and resume.
    async with _answer_guard_async(task_id, steps):
        round_id, answer, comment, by = await steps.run(_answer_question_in_guard, task_id, body, caller.actor, caller.principal)
    # Outside the in-flight guard — see `_echo_answer_to_slack`.
    await asyncio.to_thread(_echo_answer_to_slack, task_id, question_id=round_id, answer=answer, by=by, comment=comment)
    # The trail comes back with it: this is the verb clicked from the Q&A trail, and the console replaces its task
    # object with this response, so omitting `qa_entries` would empty that panel until an SSE refresh.
    return await steps.run(_resolved_task_out, task_id, caller.principal, True)


def _resolve_door_or_refuse(session: Session, task_id: int, verb: str, resumes: bool) -> None:
    """What every resolve door checks before taking the answer guard: the task exists, is not a chat, and — when
    settling the round may resume it (`resumes`) — its org is active (`app/org_gate.py`)."""
    task = get_task_or_404(task_id, session)
    _refuse_chat_answer_door_or_409(task, verb)
    if resumes:
        require_org_active(session, task.org_id)


def _answer_question_in_guard(
    session: Session, task_id: int, body: AnswerQuestionIn, user: AdminUser, principal: Principal | None,
) -> tuple[int, str, Optional[str], Optional[str]]:
    """`answer-question`'s guarded half: resolve the round, settle the gate, commit. Returns
    `(round id, answer, comment, by)` for the Slack mirror."""
    user = adopt_user(session, user)
    task = get_task_or_404(task_id, session)
    round_id, answer, comment = _answer_open_round(session, task, body, user, principal)
    # Delivery and resume live in the shared gate helper: waking the runner depends on the whole trail.
    _settle_gate_if_nothing_outstanding(task, session)
    # Read while the row is still live: `expire_all` below turns the read into a refresh SELECT.
    by = operator_label(user)
    session.commit()
    session.expire_all()
    return round_id, answer, comment, by


def _resolved_task_out(session: Session, task_id: int, principal: Principal | None, with_trail: bool) -> TaskOut:
    """The task a resolve door answers with: its rounds, and its Q&A trail when the door is clicked from it."""
    refreshed = get_task_or_404(task_id, session)
    return _task_out(
        refreshed,
        questions=_load_task_questions_out(session, task_id),
        qa_entries=_task_qa_entries(session, refreshed) if with_trail else None,
        session=session,
        principal=principal,
    )


# ── Action route: answer one named input request (the round id in the path) ────────────────────────
# The path-addressed successor of `answer-question`; `answer-question` stays as its alias.
@router.post("/{task_id}/input-requests/{question_id}/answer", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
async def answer_input_request(
    db: DbDep,
    task_id: int,
    question_id: int,
    body: AnswerQuestionIn,
    caller: AsyncCaller = Depends(current_caller),
) -> TaskOut:
    """`answer-question` with the round named by the path. A body naming a different round is a 422."""
    if body.question_id is not None and body.question_id != question_id:
        raise HTTPException(
            status_code=422,
            detail=f"body question_id {body.question_id} does not match the path's {question_id}",
        )
    return await answer_question(db, task_id, body.model_copy(update={"question_id": question_id}), caller)


# ── Action route: answer a whole round of input requests in one call ───────────────────────────────
@router.post("/{task_id}/input-requests/answers", response_model=BatchAnswersOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
async def answer_input_requests(
    db: DbDep,
    task_id: int,
    body: BatchAnswersIn,
    caller: AsyncCaller = Depends(current_caller),
) -> BatchAnswersOut:
    """Answer or skip SEVERAL open rounds in one call — the batch form of `input-requests/{qid}/answer` and
    `ignore-question`, so a client never loops over one POST per question.

    Best-effort (operator decision): each entry is resolved and committed on its own by the same helpers as
    the single-round routes; one that fails (stale id, bad selection, ...) is rolled back alone and reported in
    `results` with the status and detail its own route would have answered, while the rest are saved. Any
    subset of the open rounds may be sent; the outstanding-round gate then decides, ONCE after the batch,
    whether to hand everything to the runner. Entries resolve in the order sent, so a round named twice
    fails the second time (409, no longer open)."""
    steps = _route_steps(db)
    await steps.run(_resolve_door_or_refuse, task_id, "answering rounds here", True)
    await steps.let_go()
    async with _answer_guard_async(task_id, steps):
        results, echoes, by = await steps.run(_answer_input_requests_in_guard, task_id, body, caller.actor, caller.principal)
    # Outside the in-flight guard — see `_echo_answer_to_slack`.
    for round_id, answer, comment in echoes:
        if answer is None:
            await asyncio.to_thread(_echo_dismissal_to_slack, task_id, question_id=round_id, by=by)
        else:
            await asyncio.to_thread(
                _echo_answer_to_slack, task_id, question_id=round_id, answer=answer, by=by, comment=comment)
    task = await steps.run(_resolved_task_out, task_id, caller.principal, True)
    return BatchAnswersOut(task=task, results=results)


def _answer_input_requests_in_guard(
    session: Session, task_id: int, body: BatchAnswersIn, user: AdminUser, principal: Principal | None,
) -> tuple[list[BatchAnswerResultOut], list[tuple[int, Optional[str], Optional[str]]], Optional[str]]:
    """The batch's guarded half: each entry in its own transaction, then one settle. Returns
    `(results, echoes, by)`; an echo is `(round, answer or None = dismissed, comment)`."""
    user = adopt_user(session, user)
    task = get_task_or_404(task_id, session)
    results: list[BatchAnswerResultOut] = []
    echoes: list[tuple[int, Optional[str], Optional[str]]] = []
    for entry in body.answers:
        # One transaction per entry, committed before the next: a checkout round's branch change commits (or,
        # on a conflict, rolls back) the session itself, so a savepoint could neither contain it nor protect
        # the entries before it. Committed entries are durable whatever happens to a later one.
        try:
            round_id, answer, comment = _retry_once_on_lock_clash(
                session, task_id, entry.question_id,
                functools.partial(_resolve_batch_entry, session, task, entry, user, principal),
            )
        except HTTPException as exc:
            session.rollback()
            results.append(BatchAnswerResultOut(
                question_id=entry.question_id, outcome="failed",
                status_code=exc.status_code, detail=exc.detail,
            ))
            continue
        except Exception:  # noqa: BLE001 — best-effort: the entries already saved must still be handed over
            session.rollback()
            log.exception("task %s: answering round %s in a batch failed", task_id, entry.question_id)
            results.append(BatchAnswerResultOut(
                question_id=entry.question_id, outcome="failed", status_code=500,
                detail="Not saved. Press Send again.",
            ))
            continue
        results.append(BatchAnswerResultOut(
            question_id=round_id, outcome="dismissed" if entry.ignore else "answered"))
        echoes.append((round_id, answer, comment))
    if echoes:
        # One settle for the whole batch, under the task row lock the per-entry commits released: the runner
        # is woken once, with everything.
        # Re-read, not reused: a checkout entry may have moved the row since it was loaded.
        task = session.scalars(
            select(Task).where(Task.id == task_id).with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        _settle_gate_if_nothing_outstanding(task, session)
    by = operator_label(user)
    session.commit()
    session.expire_all()
    return results, echoes, by


def _resolve_batch_entry(
    session: Session, task: Task, entry: BatchAnswerEntryIn, user: AdminUser, principal: Principal | None,
) -> tuple[int, Optional[str], Optional[str]]:
    """Answer or skip one batch entry and commit it: `(round, answer or None = dismissed, comment)`."""
    answer: Optional[str] = None
    comment: Optional[str] = None
    if entry.ignore:
        if entry.answer is not None or entry.selections is not None or entry.comment or entry.attachment_ids:
            raise HTTPException(
                status_code=422,
                detail="a skipped round takes no answer, selections, comment or attachments",
            )
        round_id = _dismiss_open_round(session, task.id, entry.question_id, user)
    else:
        round_id, answer, comment = _answer_open_round(session, task, entry, user, principal)
    session.commit()
    return round_id, answer, comment


# Postgres's two "your transaction lost a lock race, run it again" answers: deadlock_detected and
# serialization_failure. The rollback undoes the entry; the one part a rerun can find already committed is a
# checkout round's branch change, which `checkout_gate` applies idempotently.
_LOCK_CLASH_SQLSTATES = frozenset({"40P01", "40001"})


def _retry_once_on_lock_clash(
    session: Session, task_id: int, question_id: Optional[int], attempt: Callable[[], _T],
) -> _T:
    """`attempt()`, run once more after a rollback when Postgres aborted it as a lock clash (a Send
    against the draft autosave). Every writer takes the task row before its rounds, so a clash is rare; a second
    one in a row is raised."""
    try:
        return attempt()
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) not in _LOCK_CLASH_SQLSTATES:
            raise
        session.rollback()
        log.warning("task %s: round %s lost a lock race (%s) — running it again", task_id, question_id,
                    getattr(exc.orig, "sqlstate", None))
        return attempt()


# ── Action route: ignore-question (silently drop one round without answering it) ────────────────────
@router.post("/{task_id}/ignore-question", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
async def ignore_question(
    db: DbDep,
    task_id: int,
    body: IgnoreQuestionIn,
    caller: AsyncCaller = Depends(current_caller),
) -> TaskOut:
    """Resolve one open `TaskQuestion` round by DECLINING to answer it — the sibling of `answer-question`.
    No body at all is valid and targets the oldest round.

    Otherwise the only way past a round with no opinion was inventing an answer the runner treats as a
    decision. Recorded `dismissed` with `chosen_resolution` NULL; the runner is still told. No task-state
    guard, and the same chat refusal (it ends in the same resume arm)."""
    return await _ignore_question_flow(_route_steps(db), task_id, body, caller.actor, caller.principal)


async def ignore_question_in_process(
    session: Session, *, task_id: int, body: IgnoreQuestionIn, user: AdminUser, principal: Principal | None,
) -> TaskOut:
    """`ignore_question` for an in-process caller holding its own sync session (`app/slack_inbound.py`): the same
    flow, its database steps on worker threads over that session."""
    return await _ignore_question_flow(_thread_steps(session), task_id, body, user, principal)


async def _ignore_question_flow(
    steps: "_Steps", task_id: int, body: IgnoreQuestionIn, user: AdminUser, principal: Principal | None,
) -> TaskOut:
    await steps.run(_resolve_door_or_refuse, task_id, "dismissing a round here", True)
    await steps.let_go()
    # The same guard as `answer-question`: the task's gate is what is serialized.
    async with _answer_guard_async(task_id, steps):
        round_id, by = await steps.run(_ignore_question_in_guard, task_id, body, user)
    # A DISMISSAL, never an answer, and outside the in-flight guard — see `_echo_answer_to_slack`.
    await asyncio.to_thread(_echo_dismissal_to_slack, task_id, question_id=round_id, by=by)
    return await steps.run(_resolved_task_out, task_id, principal, False)


def _ignore_question_in_guard(
    session: Session, task_id: int, body: IgnoreQuestionIn, user: AdminUser,
) -> tuple[int, Optional[str]]:
    """`ignore-question`'s guarded half: dismiss the round, settle the gate, commit. Returns `(round id, by)`."""
    user = adopt_user(session, user)
    task = get_task_or_404(task_id, session)
    round_id = _dismiss_open_round(session, task_id, body.question_id, user)
    _settle_gate_if_nothing_outstanding(task, session)
    by = operator_label(user)
    session.commit()
    session.expire_all()
    return round_id, by


# ── Action route: revise-question (change your mind about a round you already resolved) ────────────
@router.post("/{task_id}/revise-question", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
async def revise_question(
    db: DbDep,
    task_id: int,
    body: ReviseQuestionIn,
    caller: AsyncCaller = Depends(current_caller),
) -> TaskOut:
    """REOPEN one resolved `TaskQuestion` round so the operator can answer it again — the third thing
    that can happen to a round, after answering and dismissing it, and the one that runs BACKWARDS.

    It reopens rather than rewrites (operator's call): the round returns to `open` with its resolution
    cleared, so `GateForm`, `answer-question` and `_settle_gate_if_nothing_outstanding` work unchanged.
    `delivered_at` is cleared too, which is what puts the re-answer back in the hand-off batch.

    No task-state guard ("always available"): on a task with no live runner the record is corrected and
    nothing is delivered. The previous answer survives in a `task_note:answer_revised` note, with whether it
    had reached the runner (`previous_delivered`): the re-answer of such a round reaches it labelled as a
    revision (`_revised_round_ids`) — typed into an `active` runner at once, or carried in a parked one's next
    hand-off. A chat is
    refused, as by both resolve doors: a reopened chat round would have no door that can answer it."""
    steps = _route_steps(db)
    # Refused BEFORE the in-flight guard is taken, so a refusal never has to release one.
    await steps.run(_resolve_door_or_refuse, task_id, "revising a round here", False)
    await steps.let_go()
    # The same guard as the resolve routes: a revise racing an answer on the last round could otherwise
    # stamp `delivered_at` over the round just reopened.
    async with _answer_guard_async(task_id, steps):
        previous_status, by = await steps.run(_revise_question_in_guard, task_id, body, caller.actor)
    log.info(
        "task %s: question round %s reopened by %s (was %s) — awaiting a new answer",
        task_id, body.question_id, by, previous_status,
    )
    # No Slack echo: `notify_targets.open_round` re-arms the original card while its round is open, so a new
    # message would announce a question the operator just reopened.
    return await steps.run(_resolved_task_out, task_id, caller.principal, False)


def _revise_question_in_guard(
    session: Session, task_id: int, body: ReviseQuestionIn, user: AdminUser,
) -> tuple[str, Optional[str]]:
    """`revise-question`'s guarded half: reopen the round, note what it replaced, commit. Returns
    `(previous status, by)`."""
    user = adopt_user(session, user)
    task = get_task_or_404(task_id, session)
    # Selected INSIDE the guard, under the task row lock — see `_resolved_round_or_409`.
    question = _resolved_round_or_409(session, task_id, body.question_id)
    # Read before the reset, so the note records the decision replaced.
    previous_status = question.status
    previous_resolution = question.chosen_resolution
    previous_comment = question.comment
    # A dismissal is never on the wire (`_delivery_wire`), so a delivered dismissal gave the runner no answer.
    previous_delivered = question.delivered_at is not None and previous_status != "dismissed"
    question.status = "open"
    # A reopened round starts from no draft: one that slipped in after it resolved would pose as current.
    clear_round_drafts(session, question.id)
    # Every resolution column together: a reopened round must not read as answered or name a resolver.
    question.chosen_resolution = None
    question.comment = None
    question.resolved_by_id = None
    question.resolved_at = None
    # Not tidiness: this puts the re-answer back in the hand-off batch, re-arming delivery.
    question.delivered_at = None
    record_task_note(
        # Empty summary, like every note beside it — the content rides `data`.
        session, task_id, "answer_revised", "",
        {
            "question_id": question.id,
            "question": question.question,
            "header": question.header,
            "options": question.options or [],
            # Keys that cannot be mistaken for a current answer: `_task_qa_entries` reads `answer`/`comment`.
            "previous_status": previous_status,
            "previous_answer": previous_resolution,
            "previous_comment": previous_comment,
            # Whether the runner was given the replaced resolution (`_revised_round_ids`).
            "previous_delivered": previous_delivered,
        },
        # Reopening a settled decision is a decision — it must not be anonymous either.
        user=user,
    )
    # The trail changed without the state: emit, naming the current state. `record_transition=False` is
    # mandatory: `_epic_time_metrics` and `_epic_children_finished_at` read `kind LIKE 'task:%'` with no action
    # filter (`app/events.py`), so a revise on a `done` task would write a fresh `task:done`, inflating
    # durations and resetting `scan_stuck_epics`' anchor. The durable record is the `answer_revised` note.
    emit_console_event(
        session, entity="task", id=task.id, action=task.state, record_transition=False,
    )
    by = operator_label(user)
    session.commit()
    session.expire_all()
    return previous_status, by
