"""Runner liveness: breaks, heartbeats, and console exit/resume."""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import (
    account_fold,
    agent_sessions,
    agent_session_lifecycle,
    fleet_verdicts,
    project_identity,
    runner_terminations,
    slot_destroy,
    slot_state,
)
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db_async import on_session
from app.task_lookup import get_task_or_404
# Every PERSONA fact comes from the one registry record per persona (`app/persona_registry.py`).
from app.persona_registry import DONE_CRITERION_OPERATOR, PERSONA_DONE_CRITERION
from app.models.tasks import Task
# Task transitions go through the watermark-gated helpers; the state machine single-homes in routes/tasks/.
from app.routes.tasks.transitions import apply_complete
from app.routes.tasks.runner_settle import end_attempt, reassert_active, resume_from_console
from app.routes.tasks.slots import kill_runner_keeping_hold, send_minted_intents
# State vocabulary and guard sets single-home in `app/task_lifecycle.py`.
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, PAUSED, SYSTEM_FAILED, _BREAK_RELAUNCH_STATES
from app.runner_causes import CAUSES, TERMINAL_CAUSES
from app.routes.agent.auth import AgentDoor, _check_token_for_task, require_caller_task

router = APIRouter(prefix="/api/agent", tags=["agent"])
log = logging.getLogger("agent-routes")


# Every CP kill is a `kill_runner` slot intent on the one lease channel: minted by
# `tasks._mint_kill_runner_intents`, pushed and served on `GET /api/agent/pool-tick`, enacted by
# `apps/jaahd/reconcile/`.


# ── Broken sessions: a runner that broke rather than finished ─────────────────
# The cause rides onto the session row (Audit › Sessions), rests the account when it is a verdict on the
# credential, and ends the attempt. Causes single-home in `app/runner_causes.py`.
class RunnerBreakIn(BaseModel):
    # The task whose runner broke.
    task_id: int
    # One of `runner_causes.CAUSES` (422 otherwise). Validated rather than free-text so the session's
    # `end_reason` stays in its domain (`agent_sessions.end_reasons`).
    cause: str
    # The evidence line the classifier matched, so a human can check the verdict rather than trust it.
    detail: str = Field(default="", max_length=1000)
    session_id: str = Field(default="", max_length=200)
    # The name, all the way down: `account_fold.record_account_verdict` resolves it.
    node_id: str = Field(default="", max_length=200)
    account: str = Field(default="", max_length=200)
    transcript: str = Field(default="", max_length=1000)
    # Who observed the break: the runner's settle hook or a CP sweep, which see different evidence.
    observed_by: str = Field(default="", max_length=200)
    # The runner asserts from its StopFailure hook that it died mid-turn and wants replacing. It only ends the
    # attempt (task → `ready`): no dispatch, no slot pick. The worktree stays the task's (TRACKER-2335 rule 2),
    # so the relaunch resumes into the same slot (rule 6).
    request_replace: bool = False


class RunnerBreakOut(BaseModel):
    # The state `request_replace` left the Task in (`ready`, or `system_failed` with the retry budget spent),
    # or None when no attempt was ended; returned so the hook can log what its break caused.
    task_state: Optional[str] = None
    # True when the break was also recorded against the runner's account (`_ACCOUNT_BREAK_VERDICTS`).
    account_blocked: bool = False


# ── A break as evidence about the ACCOUNT, not only about the session ─────────
# Causes that are a verdict on the credential: a runner walling on its usage window has the freshest
# reading of the account (discarding it caused re-death loops). A mapping because
# `runner_causes.CAUSES` describes a session and `quota_status` an account.
_ACCOUNT_BREAK_VERDICTS = {
    "usage_limit": "usage_limit",
    "credit_exhausted": "credit_exhausted",
    "auth_expired": "auth_rejected",
}


def _kill_is_fenced(body: "RunnerBreakIn", what: str) -> bool:
    """May this report stop a process? True only when it names the session it is reporting about.

    The one place this route does not fall open on an absent fence: `_broken_runners_hold` omits the session
    predicate for older nodes, but a kill's worst case is someone else's live runner destroyed
    (`coding-standards.md` §In a guard, absent must DENY). Logged, so a node that stopped sending
    `session_id` is visible.
    """
    if body.session_id:
        return True
    log.warning(
        "runner break on task %s named no session_id — recording the break and NOT minting a kill to "
        "%s. A report that does not say which attempt it is about cannot be told from a report about "
        "somebody else's live runner, and a kill is not a decision that can be taken back",
        body.task_id, what,
    )
    return False


def _broken_runners_hold(
    task_id: int, session: Session, *, session_id: str = ""
) -> Optional[dict[str, Any]]:
    """This task's own bound pool slot, or None when it holds none or the report names a different session.
    This lookup is the whole kill decision for a break that neither relaunched nor released.

    Fenced on the reporting session: a late break may arrive after the slot was re-armed for a live
    session. `slot_state.task_hold` needs no state narrowing: `worktrees.task_id` is set only in
    `state = 'task'`, so any hold found is the CP's own dispatch.
    """
    hold = slot_state.task_hold(session, task_id)
    if hold is None:
        return None
    if session_id and hold["session_id"] != session_id:
        return None
    return hold


@router.post("/runner-breaks", response_model=RunnerBreakOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def record_runner_break(
    session: Session,
    body: RunnerBreakIn,
    door: AgentDoor,
) -> RunnerBreakOut:
    """Record one broken session. Called by the runner's settle hook (`apps/jaahd/hooks/runner-status.py
    settle`) when it finds its task `active` with no evidence, and by its `StopFailure`/heartbeat arms.
    404 unknown task; 422 cause outside the vocabulary. Callers treat any failure as a logged no-op.

    `request_replace` is the deterministic failure signal (else a quota-walled runner idles with its task
    `active` forever): the account verdict (`_ACCOUNT_BREAK_VERDICTS`) is recorded first, then the
    attempt is ended (`end_attempt`) only from `_BREAK_RELAUNCH_STATES`.

    No park: the worktree stays the task's until done, deleted, reset or operator-released.
    `end_attempt` kills the runner on every branch and releases the slot only on `system_failed`. When it
    cannot land (settled or stale report), the fallback stops whatever this same session still holds,
    fenced against a live replacement.
    """
    caller, x_agent_task_id = door
    require_caller_task(session, caller, x_agent_task_id, body.task_id)
    if body.cause not in CAUSES:
        raise HTTPException(
            status_code=422,
            detail=f"cause {body.cause!r} is not a break cause — must be one of "
                   f"{list(CAUSES)}",
        )
    task = get_task_or_404(body.task_id, session)
    # Its slot is being destroyed: the destroy's ack, not this route, returns the task to `ready`.
    destroying = slot_destroy.destroy_pending(session, task.id)
    if destroying and body.cause in slot_destroy.KILL_SIGNATURE_CAUSES:
        # The CP's own kill reads as a death with no verdict: not a break at all.
        log.info("runner break on task %s (%s) ignored: its slot is being destroyed", task.id, body.cause)
        return RunnerBreakOut()
    if body.session_id and body.detail.strip():
        # The runner's own words onto its session row (Audit › Sessions), wherever the attempt's end left it: a
        # break often lands after the row already ended with only a bare reason or the attempt's summary, which
        # this report replaces (`replace`). Fenced to the caller's task and, for a node, its own rows. Redacted
        # and capped by the writer.
        agent_session_lifecycle.stamp_end_detail(
            session, agent_session_id=body.session_id, detail=f"{body.cause}: {body.detail.strip()}",
            task_id=body.task_id, node_id=caller.node_id, replace=True,
        )
    account_blocked = False
    task_state: Optional[str] = None
    # Kill mints ride this transaction; pushed once after the commit (`tasks.send_minted_intents`).
    issued: list[fleet_verdicts.IssuedIntents] = []
    if body.request_replace:
        verdict = _ACCOUNT_BREAK_VERDICTS.get(body.cause)
        if verdict and body.account:
            # `detail` is the matched transcript line; a `usage_limit` naming a far reset keeps the account out that
            # long (`account_fold.verdict_ttl_seconds`).
            account_blocked = account_fold.record_account_verdict(
                session, config_dir=body.account, node_id=body.node_id, status=verdict,
                detail=body.detail,
            )
    # A cause no relaunch clears ends the attempt even unasked: the settle path reports it without
    # `request_replace` and then posts a `/fail` that would only re-queue it.
    terminal = body.cause in TERMINAL_CAUSES
    if (body.request_replace or terminal) and not destroying:
        # The relaunch decision: `end_attempt` returns the row to `ready` (or `system_failed` on its last
        # attempt, or at once for a cause no relaunch clears) and mints its own kill (TRACKER-2335 S4).
        task_state = end_attempt(
            body.task_id, session,
            reason=(
                f"{agent_sessions.end_reason_label(body.cause)}: {body.detail or 'no detail'}" if terminal
                else f"runner session broke: {body.cause} ({body.detail or 'no detail'})"
            ),
            final=terminal,
            sources=tuple(_BREAK_RELAUNCH_STATES),
            guards=(
                (Task.runner_session_id == body.session_id,) if body.session_id else ()
            ) + (
                # Not a row whose runner the CP stopped while its question waits: that death is planned
                # (`tasks.stop_runner_on_unanswered_question`), and a relaunch would re-ask the question.
                Task.question_runner_stopped_at.is_(None),
            ),
            # The runner's self-reported cause, onto its own task's Timeline.
            # `runner_terminations.CAUSES` is derived from `runner_causes.CAUSES` (a superset), so `label_for` renders it.
            cause=body.cause,
        )
        if task_state is None:
            # Not an error: the row already settled (it keeps its slot, TRACKER-2335 rule 2) or this report is stale.
            # Only stopping this session, if it still holds the slot, is owed.
            log.warning(
                "runner break on task %s asked to be replaced, but the task is %r rather than one of %s — "
                "no attempt ended and nothing will be re-dispatched automatically",
                body.task_id, task.state, sorted(_BREAK_RELAUNCH_STATES),
            )
            # Re-asking for the hold is the fence: a superseded session names no current hold. Required
            # (`_kill_is_fenced`).
            stale = (
                _broken_runners_hold(body.task_id, session, session_id=body.session_id)
                if _kill_is_fenced(body, "stop its runner") else None
            )
            if stale is not None:
                killed, minted = kill_runner_keeping_hold(
                    body.task_id, session,
                    reason=(
                        f"task {body.task_id} is '{task.state}' and its runner reported breaking "
                        f"({body.cause}) — stop that session; the slot stays bound to the task"
                    ),
                )
                issued += minted
                if killed:
                    stale_project = project_identity.label(session, stale["project_id"])
                    log.warning(
                        "runner break on task %s (%r) — minted a kill for %s#%s: the attempt did not "
                        "relaunch, so nothing else would otherwise stop this session",
                        body.task_id, task.state, stale_project, stale["slot"],
                    )
    # The arm `end_attempt` did not cover: record the kill here, only here, or retryable breaks double-count.
    if body.request_replace and task_state is None and issued and not destroying:
        runner_terminations.record(session, body.task_id, body.cause)
    session.commit()
    send_minted_intents(issued)  # AFTER the commit — see `tasks.send_minted_intents`.
    log.info(
        "runner break recorded: task=%s cause=%s observed_by=%s "
        "request_replace=%s -> task_state=%s account_blocked=%s",
        body.task_id, body.cause, body.observed_by,
        body.request_replace, task_state, account_blocked,
    )
    return RunnerBreakOut(task_state=task_state, account_blocked=account_blocked)


class HeartbeatIn(BaseModel):
    # `CONTROL_RUNNER_SESSION_ID` (`runner-status.py::_runner_session_id`), the fence value every
    # identity-sensitive report on this session uses.
    session_id: str
    # The turn ended with background work in flight (the Stop payload's `background_tasks`/`session_crons`) that
    # starts the next turn itself, so a chat does not park on the operator (`reassert_active`).
    self_waking: bool = False


class HeartbeatOut(BaseModel):
    # One of `reassert_active`'s outcomes (`routes/tasks/runner_settle.py`); `not-found` is answered as a 404.
    outcome: str


@router.post("/tasks/{task_id}/heartbeat", response_model=HeartbeatOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def task_heartbeat(
    session: Session,
    task_id: int,
    body: HeartbeatIn,
    _: None = Depends(_check_token_for_task),
) -> HeartbeatOut:
    """The runner's own "I am still working" signal, fired on every `Stop` (a turn boundary;
    `apps/jaahd/hooks/runner-status.py rs_heartbeat`).

    The decision is `reassert_active`'s (`routes/tasks.py`). An empty `session_id` is 422: nothing is safe to
    fence without one. The hook swallows errors; this route raises normally.
    """
    if not body.session_id:
        raise HTTPException(status_code=422, detail="session_id is required")
    outcome = reassert_active(task_id, session, session_id=body.session_id, self_waking=body.self_waking)
    if outcome == "not-found":
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return HeartbeatOut(outcome=outcome)


# ── The Web Console doors: what the OPERATOR did at the keyboard ──────────────
# Two node-hook reports (`apps/jaahd/hooks/runner-status.py`) about rows whose surface is a human in a
# tmux pane (today the `advisor` chat). They carry the node's observation of what the human did, not the
# session's word about itself, so they alone admit operator-criterion personas and nothing else.


def _refuse_a_persona_with_no_operator_at_the_console(
    task_id: int, session: Session, *, gesture: str
) -> None:
    """404 an unknown id; 422 a persona whose done-criterion is not `operator`.

    The inverse of `_refuse_a_persona_with_no_terminal_call`, which refuses operator-criterion personas at
    `/fail`, `/complete` and `task_pipeline_cp.block_task` because only the operator moves such rows. These
    doors report exactly that operator gesture. Non-interactive personas are refused because they have no
    console conversation (completing a `coder` on session exit would settle unshipped work). Keyed on
    `PERSONA_DONE_CRITERION`, never an `advisor` literal.
    """
    task = get_task_or_404(task_id, session)
    if PERSONA_DONE_CRITERION.get(task.persona) != DONE_CRITERION_OPERATOR:
        raise HTTPException(
            status_code=422,
            detail=(
                f"task {task_id} (persona={task.persona!r}) has no operator at a console, so a "
                f"{gesture} report is not about it — this door carries what a human did at the keyboard, "
                f"and only a task whose one ending is the operator's own Complete has such a keyboard"
            ),
        )


# The source states this door accepts — narrower than `complete.sources`, which admits every state but
# `done` so an operator can close out work that never ran. `/exit` means "this pane ended this
# conversation", which only a chat that was live can do; otherwise a late `SessionEnd` could complete a
# queued row. The session fence does not cover this: a re-queued `ready` row can still name the dead
# pane's session (`apply_complete`'s `allow_sources` note).
_CONSOLE_EXIT_SOURCES: frozenset[str] = frozenset({ACTIVE, AWAITING_RESPONSE, PAUSED, SYSTEM_FAILED})


class ConsoleExitIn(BaseModel):
    # The fence value (`HeartbeatIn.session_id`): without it a superseded pane's late `SessionEnd` could
    # complete a row a replacement owns, or undo an operator Stop (`complete.sources` admits `paused`).
    session_id: str


class ConsoleExitOut(BaseModel):
    # True when this call landed the row `done`; False is a 200 (see the route).
    completed: bool
    # The row's state after the attempt, re-read.
    state: str


@router.post("/tasks/{task_id}/console-exit", response_model=ConsoleExitOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def task_console_exit(
    session: Session,
    task_id: int,
    body: ConsoleExitIn,
    _: None = Depends(_check_token_for_task),
) -> ConsoleExitOut:
    """The operator typed `/exit` in their chat's Web Console — a `SessionEnd` hook reporting it
    (`apps/jaahd/hooks/runner-status.py console_exit`, acting only on `prompt_input_exit`;
    `_CONSOLE_EXIT_REASON` owns that vocabulary).

    That gesture is the Complete button's decision, so this completes the row through the same writer
    (`routes/tasks.apply_complete`). Completing also frees the worktree via the settled-hold reap.

    404 an unknown id; 422 any non-`operator` persona (`_refuse_a_persona_with_no_operator_at_the_console`)
    and an empty `session_id`. Fenced on the reporting session (`apply_complete(expect_session_id=…)`).

    `completed=false` is a 200: the row was already `done` (operator pressed Complete first), not live
    (`_CONSOLE_EXIT_SOURCES`), or the fence missed (superseded pane). All expected races.
    """
    if not body.session_id:
        raise HTTPException(status_code=422, detail="session_id is required")
    _refuse_a_persona_with_no_operator_at_the_console(task_id, session, gesture="console /exit")
    completed = apply_complete(
        task_id, session,
        expect_session_id=body.session_id,
        allow_sources=_CONSOLE_EXIT_SOURCES,
    )
    # Re-read: `apply_complete` commits, so the loaded row is stale.
    session.expire_all()
    # Deleted meanwhile; a delete frees the worktree anyway.
    row = get_task_or_404(task_id, session)
    return ConsoleExitOut(completed=completed, state=row.state)


class ConsoleResumeIn(BaseModel):
    # The fence value (`HeartbeatIn.session_id`). Bare `str` so the route refuses empty with its own message.
    session_id: str


class ConsoleResumeOut(BaseModel):
    # One of `resume_from_console`'s outcomes (`routes/tasks/runner_settle.py`); `not-found` is answered as a 404.
    outcome: str


@router.post("/tasks/{task_id}/console-resume", response_model=ConsoleResumeOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def task_console_resume(
    session: Session,
    task_id: int,
    body: ConsoleResumeIn,
    _: None = Depends(_check_token_for_task),
) -> ConsoleResumeOut:
    """The operator sent their chat another message — the runner's `UserPromptSubmit` hook reporting it
    (`apps/jaahd/hooks/runner-status.py console_resume`).

    A chat parks `awaiting_response` at every turn boundary, and the operator's reply goes straight into the
    pane, never through the CP; this hook is the only edge that un-parks it. The decision is
    `resume_from_console`'s (`routes/tasks.py`). 422 on an empty `session_id` or non-`operator` persona
    (so `not-a-chat` never returns over this door); 404 on an unknown id.
    """
    if not body.session_id:
        raise HTTPException(status_code=422, detail="session_id is required")
    _refuse_a_persona_with_no_operator_at_the_console(task_id, session, gesture="console prompt")
    outcome = resume_from_console(task_id, session, session_id=body.session_id)
    if outcome == "not-found":
        # Deleted between the guard and the writer's read; 404 like the heartbeat.
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return ConsoleResumeOut(outcome=outcome)
