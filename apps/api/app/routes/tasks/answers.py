"""Answering a Task's questions: delivery to the live runner, Slack echo, rounds, ignore/revise, the answer lock."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

# `notify_text` is imported for `_has_glyph` alone: the answer gate and the Slack card must agree on which
# options render visibly.
from app import actors, edition_features, fleet_verdicts, live_runner, notify_text, slot_state
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.task_extras import ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.tasks import Task, TaskQuestion
from app.dispatch_compose.input_staging import _attachment_workdir
from app.persona_registry import DONE_CRITERION_OPERATOR, PERSONA_DONE_CRITERION
from app.task_artifacts import uploaded_clause, visible_artifact_clause, visible_artifact_clause_joined
from app.task_awaiting_response_parser import parse_response_options
from app.routes.tasks import checkout_gate
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, TRANSITION_BY_NAME
from app.task_lookup import get_task_or_404
from app.org_gate import require_org_active
from app.user_activity import record_task_note
from app.routes.tasks.policy import MAX_TASK_TEXT_LEN
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
from app.routes.tasks.awaiting import resume_dispatched_task_on_answer

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
log = logging.getLogger(__name__)


# The advisory-lock class for "an `answer` submission for this task is in flight": `blocked` cannot be
# atomically claimed (it must stay `blocked` for the backstop), so the submission is serialized by a
# Postgres session-level advisory lock keyed `(ANSWER_LOCK_CLASSID, task_id)` — shared by every control-plane
# task, not a set in one process. Sibling of `cron_runtime.CRON_RUN_LOCK_CLASSID`; the two classes keep the
# two lock spaces apart.
ANSWER_LOCK_CLASSID = 0x616E7377  # 'answ'


class _AnswerLock:
    """The lock `_answer_guard` yields: session-level, on its own dedicated connection.

    Dedicated because the request session commits mid-block, which would end a transaction-level lock
    before the mirror, and because a pooled connection carrying a session lock would hand it to the next
    checkout (the same reason `cron_runtime.RunLock` dials its own). `release` is idempotent and never
    raises: a body releases early before a Slack mirror, and the guard's `finally` releases again."""

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        self._conn: Optional[Any] = None
        self._guard = threading.Lock()

    def acquire(self) -> bool:
        """True iff this process now holds the lock; False means another submission for the task is live."""
        from app import db_credentials

        conn = db_credentials.psycopg_connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)", (ANSWER_LOCK_CLASSID, self.task_id)
                )
                (locked,) = cur.fetchone()
        except Exception:
            conn.close()
            raise
        if not locked:
            conn.close()
            return False
        self._conn = conn
        return True

    def release(self) -> None:
        with self._guard:
            conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", (ANSWER_LOCK_CLASSID, self.task_id))
        except Exception as exc:  # noqa: BLE001 — closing the connection frees the lock regardless
            log.warning("answer lock for task %s: unlock failed (%s); closing the connection", self.task_id, exc)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


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
async def _answer_guard_async(task_id: int):
    """`_answer_guard` for an `async def` route: the lock's dedicated connection is opened and released
    on a worker thread, never on the event loop (a fresh TCP/TLS/auth handshake per submission)."""
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
    finally:
        await asyncio.to_thread(lock.release)


# Operator's free-text note beside their choice; stored in `activity_events`, so bounded like the answer.
# Not capped again on the wire. Absent/blank means "no comment", never a placeholder.
_COMMENT_FIELD = Field(default=None, max_length=MAX_TASK_TEXT_LEN)


# Ticked options of a multi-choice round, beside `answer`. `None` selects the single-answer path; `[]`
# is a recorded "ticked none". Legality single-homes in `_gate_answer_or_422`. 25 = Slack's
# `actions`-block ceiling (`slack_blocks._MAX_BUTTONS`), mirrored by `GateForm` (a Pydantic `too_long`
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


class ReviseQuestionIn(BaseModel):
    """The `revise-question` body: the round to reopen. REQUIRED, unlike its siblings — the RESOLVED set
    has no "oldest" order, and a guess would silently reopen a different decision."""

    question_id: int


# ── Action route: answer ───────────────────────────────────────────────────────────────────────────
# Not `/respond`: `answer` and `answer-question` must stay distinguishable. The terminal-safe scrub every pane
# write goes through is single-homed in `app/live_runner.py`; this name is kept for this module's callers.
_scrub_control_chars = live_runner.scrub_control_chars


def _deliver_answer_to_live_runner(
    task: Task, question: str, answer: str, session: Session, *, legacy_gate: bool = False,
    answered_by: Optional[UUID] = None, question_ids: Optional[list[int]] = None, bare: bool = False,
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
        # the NFKC delimiter table — what keeps line breaks out); `_scrub_control_chars`, the independent terminal
        # guarantee (`_flatten` would carry a bare `ESC` through); `_flatten`, collapsing leftover space runs.
        question=_flatten(_scrub_control_chars(_neutralize_batch_delimiters(question or ""))),
        answer=_scrub_control_chars(answer),
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


@router.post("/{task_id}/answer", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
async def answer_task(
    task_id: int,
    body: AnswerIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Post a human's answer to an `awaiting_response` Task's question and resume it (DB-direct); any other
    state is a 409. Records the `answered` Q&A note and resumes `awaiting_response → ready` in one commit.

    With open `task_questions` rounds it resolves the oldest instead, and the outstanding-round gate
    decides whether anything resumes (`app/slack_inbound.py` calls this route). Source, target and attempt
    reset come from `TRANSITIONS`' `answer` row."""
    # The answer's shape is validated per branch below (whether `selections` is legal depends on the gate),
    # so a blank answer on a wrong-state task reads as the 409.
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

    # Serialize concurrent submissions (the answer lock), checked before any mutation; always released.
    async with _answer_guard_async(task_id) as guard:
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
            await asyncio.to_thread(guard.release)  # before the mirror — see `_echo_answer_to_slack`
            await asyncio.to_thread(
                _echo_answer_to_slack,
                task_id, question_id=round_id, answer=answer, by=by, comment=comment,
            )
            refreshed = get_task_or_404(task_id, session)
            return _task_out(
                refreshed,
                questions=_load_task_questions_out(session, task_id),
                session=session,
                principal=principal,
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
        # Warm first: if the asking runner is alive and frozen, hand it the answer and stay `awaiting_response`;
        # otherwise resume cold below.
        warm_node = _deliver_answer_to_live_runner(
            task, question or "", wire, session, legacy_gate=True, answered_by=user.id if user else None)
        if warm_node:
            session.commit()
            session.expire_all()
            # The guard is not released early here: a warm delivery consumes no round, and a retry during the mirror
            # would mint a second `deliver_answer`. A 409 on retry is the cheaper failure.
            await asyncio.to_thread(
                _echo_answer_to_slack,
                task_id, question_id=None, answer=answer, by=by, comment=comment,
            )
            return _task_out(get_task_or_404(task_id, session), session=session, principal=principal)
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
        emit_console_event(session, entity="task", id=task_id, action=resume.target)
        session.commit()
        session.expire_all()
        await asyncio.to_thread(guard.release)  # before the mirror — see `_echo_answer_to_slack`
        await asyncio.to_thread(
            _echo_answer_to_slack,
            task_id, question_id=None, answer=answer, by=by, comment=comment,
        )
        return _task_out(get_task_or_404(task_id, session), session=session, principal=principal)


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
    a dismissed one is never delivered (`_delivery_wire`)."""
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
    # Visible open rounds in the order `notify_targets.oldest_open_round` mirrors, so Slack and the console
    # target the same question. Soft-deleted rounds are excluded (see `_open_question_count`).
    stmt = (
        select(TaskQuestion)
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(
            TaskQuestion.task_id == task_id,
            TaskQuestion.status == "open",
            visible_artifact_clause_joined(TaskQuestion.created_at),
        )
        .order_by(TaskQuestion.created_at, TaskQuestion.id)
    )
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
    half flattened and defanged.

    Lossless by construction. Rounds are newline-joined because a resolution may be multi-line; the
    leading `N)` is the delimiter, and only the operator's half can contain a newline (see the header
    above `_BATCH_DELIMITER_SUBSTITUTIONS`)."""
    parts: list[str] = []
    for row in rows:
        # The trail's round number, not this subset's position, matching every other surface.
        idx = rounds[row.id]
        # Agent-authored: flattened and defanged (see `_BATCH_DELIMITER_SUBSTITUTIONS`), not truncated.
        question = _neutralize_batch_delimiters(_flatten(row.question))
        parts.append(f"{idx}) {question} → {_resolution_for_delivery(row)}")
    return "\n".join(parts)


def _delivery_wire(
    session: Session, task: Task, undelivered: list[TaskQuestion]
) -> tuple[str, str, bool]:
    """`(question, answer, bare)` for the hand-off of `undelivered` — the runner's whole message.

    Dismissed rounds are left out, as if never asked. Rounds resolved before the newest round was asked are
    the ones a superseded delivery carried (`awaiting.supersede_pending_answer_delivery`): re-quoted under
    `_EARLIER_ANSWERS_HEADER`. One answered round and nothing earlier keeps the single-round frame; anything
    else is the enumeration with no question. Nothing answered is `_IGNORED_WAKE_TEXT`, `bare` so the node
    sends it without the `Answer: ` frame."""
    answered = [row for row in undelivered if row.status != "dismissed"]
    if not answered:
        return "", _IGNORED_WAKE_TEXT, True
    newest = max(row.created_at for row in undelivered)
    earlier = [row for row in answered if row.resolved_at is not None and row.resolved_at < newest]
    current = [row for row in answered if row not in earlier]
    if len(current) == 1 and not earlier:
        # The single-round path is agent-authored too, and common. It renders as a quoted frame, so a question
        # closing the quote or ending the line could forge a frame; hence `'` in the table and the flattening.
        row = current[0]
        return _neutralize_batch_delimiters(_flatten(row.question)), _resolution_for_delivery(row), False
    # Ordinals from the full visible trail (see `_compose_batch_answer`, `_load_task_questions_out`).
    rounds = {
        row.id: idx
        for idx, row in enumerate(session.scalars(
            select(TaskQuestion)
            .where(
                TaskQuestion.task_id == task.id,
                visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
            )
            .order_by(TaskQuestion.created_at, TaskQuestion.id)
        ).all(), start=1)
    }
    parts = [_EARLIER_ANSWERS_HEADER, _compose_batch_answer(earlier, rounds)] if earlier else []
    if current:
        parts.append(_compose_batch_answer(current, rounds))
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
    round_row.comment = _normalized_comment(comment)
    # Who decided and when, on the round itself, so the console can print a byline beside this resolution
    # (see `models/tasks.py::TaskQuestion.resolved_by_id`).
    round_row.resolved_by_id = user.id
    round_row.resolved_at = datetime.now(timezone.utc)
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
    delivered_at = datetime.now(timezone.utc)
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
            github_synced_at=datetime.now(timezone.utc),
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


# ── Action route: answer-question (multi-round Q&A) ─────────────────────────────────────────────────
@router.post("/{task_id}/answer-question", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def answer_question(
    task_id: int,
    body: AnswerQuestionIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Answer the current open `TaskQuestion` round on a triage/blocked Task and resume the runner — the
    multi-round successor to the single-shot `answer`. Resolves the target round (named, else oldest),
    flips it `open → answered`, records the note, and hands the trail over only when no round remains
    open, since a partial delivery unfreezes the runner with the rest unanswered.

    No task-state guard: an open round is answerable on an `active` triage runner too
    (`_open_round_or_409` is the guard); on a terminal task the answer is recorded with nobody to tell.
    The shape is validated after the round is picked (`_gate_answer_or_422`). A chat is refused
    (`_refuse_chat_answer_door_or_409`)."""
    task = get_task_or_404(task_id, session)
    # Refused BEFORE the in-flight guard is taken, so a refusal never has to release one.
    _refuse_chat_answer_door_or_409(task, "answering a round here")
    require_org_active(session, task.org_id)  # settling the round may resume it (`app/org_gate.py`)

    # The same in-flight guard as `answer`, so two racing requests cannot both answer and resume.
    with _answer_guard(task_id):
        # Select inside the guard: a select-then-lock order would let two requests pick the same row.
        question = _open_round_or_409(session, task_id, body.question_id, "answer")
        attachments = _answer_attachments_or_422(session, task_id, body.attachment_ids)
        answer = _gate_answer_or_422(
            _answer_or_file_only(body.answer, body.selections, attachments), body.selections, question)
        comment = _comment_with_attachments(body.comment, task_id, attachments)
        # A guard stuck-limit round's answer may file a waiver and becomes what the runner is told (§7). An edition
        # without compliance has no guard round.
        if edition_features.enabled("compliance"):
            from app.governance import guard_escalation

            answer = guard_escalation.answer_for(session, task, question, answer, user, principal)
        # A checkout round's branch change is applied before the round is recorded (`checkout_gate`).
        answer = checkout_gate.answer_for(session, task, question, answer, comment, user, principal)
        _record_answered_round(session, task_id, question, answer, user, comment)
        # Delivery and resume live in the shared gate helper: waking the runner depends on the whole trail.
        _settle_gate_if_nothing_outstanding(task, session)
        # Read while the row is still live: `expire_all` below turns either read into a refresh SELECT.
        round_id, by = question.id, operator_label(user)
        session.commit()
        session.expire_all()

    # Outside the in-flight guard — see `_echo_answer_to_slack`.
    _echo_answer_to_slack(
        task_id, question_id=round_id, answer=answer, by=by, comment=comment
    )
    refreshed = get_task_or_404(task_id, session)
    return _task_out(
        refreshed,
        questions=_load_task_questions_out(session, task_id),
        # The trail comes back with it: this is the verb clicked from the Q&A trail, and the console replaces
        # its task object with this response, so omitting `qa_entries` would empty that panel until an SSE refresh.
        qa_entries=_task_qa_entries(session, refreshed),
        session=session,
        principal=principal,
    )


# ── Action route: answer one named input request (the round id in the path) ────────────────────────
# The path-addressed successor of `answer-question`; `answer-question` stays as its alias.
@router.post("/{task_id}/input-requests/{question_id}/answer", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def answer_input_request(
    task_id: int,
    question_id: int,
    body: AnswerQuestionIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """`answer-question` with the round named by the path. A body naming a different round is a 422."""
    if body.question_id is not None and body.question_id != question_id:
        raise HTTPException(
            status_code=422,
            detail=f"body question_id {body.question_id} does not match the path's {question_id}",
        )
    return answer_question(
        task_id, body.model_copy(update={"question_id": question_id}), user, principal, session,
    )


# ── Action route: ignore-question (silently drop one round without answering it) ────────────────────
@router.post("/{task_id}/ignore-question", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def ignore_question(
    task_id: int,
    body: IgnoreQuestionIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Resolve one open `TaskQuestion` round by DECLINING to answer it — the sibling of `answer-question`.
    No body at all is valid and targets the oldest round.

    Otherwise the only way past a round with no opinion was inventing an answer the runner treats as a
    decision. Recorded `dismissed` with `chosen_resolution` NULL; the runner is still told. No task-state
    guard, and the same chat refusal (it ends in the same resume arm)."""
    task = get_task_or_404(task_id, session)
    _refuse_chat_answer_door_or_409(task, "dismissing a round here")
    require_org_active(session, task.org_id)  # settling the round may resume it (`app/org_gate.py`)

    # The same guard as `answer-question`: the task's gate is what is serialized.
    with _answer_guard(task_id):
        # Selected INSIDE the guard — see `answer_question` for the interleaving a select-then-lock allows.
        question = _open_round_or_409(session, task_id, body.question_id, "ignore")
        question.status = "dismissed"
        # Attributed exactly like an answer (declining is a decision), so the console prints "Dismissed by …";
        # NULL `chosen_resolution` says which decision (see `models/tasks.py::TaskQuestion.resolved_by_id`).
        question.resolved_by_id = user.id
        question.resolved_at = datetime.now(timezone.utc)
        # `chosen_resolution` is NOT written — see the docstring. The absence is the record.
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
        _settle_gate_if_nothing_outstanding(task, session)
        round_id, by = question.id, operator_label(user)
        session.commit()
        session.expire_all()

    # A DISMISSAL, never an answer, and outside the in-flight guard — see `_echo_answer_to_slack`.
    _echo_dismissal_to_slack(task_id, question_id=round_id, by=by)
    refreshed = get_task_or_404(task_id, session)
    return _task_out(
        refreshed,
        questions=_load_task_questions_out(session, task_id),
        session=session,
        principal=principal,
    )


# ── Action route: revise-question (change your mind about a round you already resolved) ────────────
@router.post("/{task_id}/revise-question", response_model=TaskOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def revise_question(
    task_id: int,
    body: ReviseQuestionIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """REOPEN one resolved `TaskQuestion` round so the operator can answer it again — the third thing
    that can happen to a round, after answering and dismissing it, and the one that runs BACKWARDS.

    It reopens rather than rewrites (operator's call): the round returns to `open` with its resolution
    cleared, so `GateForm`, `answer-question` and `_settle_gate_if_nothing_outstanding` work unchanged.
    `delivered_at` is cleared too, which is what puts the re-answer back in the hand-off batch.

    No task-state guard ("always available"): on a task with no live runner the record is corrected and
    nothing is delivered. The previous answer survives in a `task_note:answer_revised` note. A chat is
    refused, as by both resolve doors: a reopened chat round would have no door that can answer it."""
    task = get_task_or_404(task_id, session)
    # Refused BEFORE the in-flight guard is taken, so a refusal never has to release one.
    _refuse_chat_answer_door_or_409(task, "revising a round here")

    # The same guard as the resolve routes: a revise racing an answer on the last round could otherwise
    # stamp `delivered_at` over the round just reopened.
    with _answer_guard(task_id):
        # Selected INSIDE the guard, under the task row lock — see `_resolved_round_or_409`.
        question = _resolved_round_or_409(session, task_id, body.question_id)
        # Read before the reset, so the note records the decision replaced.
        previous_status = question.status
        previous_resolution = question.chosen_resolution
        previous_comment = question.comment
        question.status = "open"
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
        session.commit()
        session.expire_all()

    log.info(
        "task %s: question round %s reopened by %s (was %s) — awaiting a new answer",
        task_id, body.question_id, operator_label(user), previous_status,
    )
    # No Slack echo: `notify_targets.open_round` re-arms the original card while its round is open, so a new
    # message would announce a question the operator just reopened.
    refreshed = get_task_or_404(task_id, session)
    return _task_out(
        refreshed,
        questions=_load_task_questions_out(session, task_id),
        session=session,
        principal=principal,
    )
