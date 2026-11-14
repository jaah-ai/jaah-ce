"""The console's chat view and live control of one slot's ACP run.

  GET  /api/console/{project_id}/{number}/runtime   what the slot runs and which controls the caller has, each
                                                    with the reason it is off
  POST /api/console/{project_id}/{number}/runtime   send one control (steer/interrupt/answer/close) to that run

The run is the slot's own: its `state='task'` hold and the session the node said it LAUNCHED
(`Worktree.launch_session_id`, the `sessions` row's `agent_session_id`). What a provider can do is read off its
catalogue row (`agent_providers.capabilities_of`), never branched on its value. A control becomes one
`runtime_control` slot intent (`fleet_verdicts.build_runtime_control_intent`), which the node queues on the
host's control lane; the events it causes come back through `routes/task_session_events.py`.

Slot visibility and editability are the console's (`visible_worktree_clause`, `worktree_editable_clause`, node
routing by `console._resolve_console_node`), so a slot this route answers for is one the terminal would open.
A steer or answer is an operator's words: never logged here.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import agent_providers, fleet_verdicts, project_identity, slot_state
from app.agent_sessions import RUNTIME_ACP, RUNTIME_LEGACY, STATUS_RUNNING
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, Principal, current_caller, current_principal
from app.authz.visibility import visible_session_clause, visible_worktree_clause, worktree_editable_clause
from app.db_async import on_session
from app.events import emit_console_event
from app.live_runner import scrub_control_chars
from app.models.agent_sessions import AgentSession
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.worktrees import Worktree
from app.node_identity import node_ref
from app.ratelimit import SharedRateLimiter
from app.routes.console import _NO_SUCH_SLOT, _easy_mode_view_only, _resolve_console_node
from app.user_activity import record_task_note
from app.entity_shape import or_404

router = APIRouter(tags=["console"])
log = logging.getLogger("cockpit-console-runtime")

# The longest steer/answer accepted, in characters (a prompt, not a file).
MAX_CONTROL_TEXT = 16000

# Counted in the database like the console's other budgets, keyed on the user.
_CONTROL_RATE_LIMIT_PER_MINUTE = 30
_control_limiter = SharedRateLimiter(limit=_CONTROL_RATE_LIMIT_PER_MINUTE, bucket="console-runtime")

# The copy each refusal and disabled control shows.
NO_TASK = "No task is running here."
NOT_STARTED = "The session hasn't started yet."
SUBSCRIPTION_TERMINAL = "Subscription accounts run in the terminal."
TERMINAL_ONLY = "This session runs in the terminal only."
NO_SESSION_READ = "Your role can't read agent sessions."
NO_STEER_ROLE = "Your role can't steer this task."
SESSION_ENDED = "The session has ended."
SESSION_MOVED = "This slot runs a different session now."
NOT_ACP = "This session doesn't take messages from the console."
UNKNOWN_PROVIDER = "The agent's provider is unknown."
UNKNOWN_LABEL = "The agent"
NOT_FROM_CONSOLE = "Send it from the console itself."

# The control reasons that are the caller's role, answered 403; every other reason is the run's state, 409.
_ROLE_REASONS = frozenset({NO_SESSION_READ, NO_STEER_ROLE})
# The reasons that still refuse a POST but are not worth a caption: the GET sends them as "".
_UNSHOWN_REASONS = frozenset({SUBSCRIPTION_TERMINAL})


class ControlOut(BaseModel):
    available: bool
    #: Why the control is off; "" whenever `available` is true, or when the reason goes without saying.
    reason: str = ""


class ConsoleRuntimeOut(BaseModel):
    task_id: Optional[int] = None
    session_id: Optional[str] = None
    runtime: Optional[str] = None
    status: Optional[str] = None
    provider: Optional[str] = None
    provider_label: Optional[str] = None
    chat: ControlOut
    steer: ControlOut
    interrupt: ControlOut
    #: May send steer/interrupt/answer at all.
    control: ControlOut


class ConsoleRuntimeIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    type: Literal["steer", "interrupt", "answer", "close"]
    #: Required for steer/answer; ignored for interrupt/close.
    text: Optional[str] = Field(default=None, max_length=MAX_CONTROL_TEXT)


class ConsoleRuntimeSentOut(BaseModel):
    intent_id: str
    type: str


@dataclass(frozen=True)
class _Slot:
    """The slot row as this route reads it, with the caller's tier on it."""

    node_id: str
    project_id: int
    number: int
    project: str
    task_id: Optional[int]
    session_id: Optional[str]
    may_edit: bool


@dataclass(frozen=True)
class _Runtime:
    """Everything both routes decide from: the slot, its session row, the provider row, the caller's keys."""

    slot: _Slot
    row: Optional[AgentSession]
    provider: Optional[str]
    label: str
    caps: Any
    auth_kind: Optional[str]
    may_read_sessions: bool
    may_steer: bool


def _slot(session: Session, principal: Principal, project_id: int, number: int, node: Optional[str]) -> _Slot:
    """The visible slot, or the console's one 404. Visibility is the WHERE, editability a selected column
    (`console._console_slot_editable`'s shape, on the request's session)."""
    node_id = or_404(_resolve_console_node(project_id, number, node, session), detail=_NO_SUCH_SLOT)
    project = or_404(project_identity.names_by_id(session, [project_id]).get(project_id), detail=_NO_SUCH_SLOT)
    row = or_404(
        session.execute(
            select(
                Worktree.state, Worktree.task_id, Worktree.launch_session_id,
                worktree_editable_clause(principal).label("may_edit"),
            )
            .select_from(Worktree)
            .where(
                Worktree.node_id == node_ref(node_id),
                Worktree.project_id == project_id,
                Worktree.number == number,
            )
            .where(visible_worktree_clause(principal))
        ).first(),
        detail=_NO_SUCH_SLOT,
    )
    # A run is addressable only while the slot holds its task with a launched session (`live_hold_for_task`).
    held = row.state == slot_state.TASK and row.task_id is not None and bool(row.launch_session_id)
    return _Slot(
        node_id=node_id, project_id=project_id, number=number, project=project,
        task_id=row.task_id if held else None, session_id=row.launch_session_id if held else None,
        may_edit=bool(row.may_edit) and not _easy_mode_view_only(session, principal, project_id),
    )


def _runtime(session: Session, principal: Principal, slot: _Slot) -> _Runtime:
    """The slot's session row and provider, and what the caller may do with them."""
    row = None
    if slot.session_id is not None:
        row = session.scalars(
            select(AgentSession).where(
                AgentSession.agent_session_id == slot.session_id, AgentSession.task_id == slot.task_id,
            )
        ).first()
    account = (
        session.scalars(select(Account).where(Account.id == row.account_id)).first()
        if row is not None and row.account_id is not None else None
    )
    provider_row = agent_providers.by_value(account.provider) if account is not None else None
    # The events route's own admission (`routes/task_session_events.py`): the floor key plus a visible session.
    may_read = (
        row is not None
        and principal.has(permission_key("sessions", "edit"))
        and session.scalar(
            select(AgentSession.id).where(AgentSession.id == row.id, visible_session_clause(principal))
        ) is not None
    )
    return _Runtime(
        slot=slot,
        row=row,
        provider=provider_row.value if provider_row is not None else None,
        label=provider_row.label if provider_row is not None else UNKNOWN_LABEL,
        caps=provider_row.capabilities if provider_row is not None else None,
        auth_kind=account.auth_kind if account is not None else None,
        may_read_sessions=may_read,
        may_steer=slot.may_edit and principal.has(permission_key("tasks", "edit")),
    )


def _chat_reason(rt: _Runtime) -> str:
    """Why the chat view is off, or ""."""
    if rt.slot.task_id is None:
        return NO_TASK
    if rt.row is None:
        return NOT_STARTED
    if rt.row.runtime == RUNTIME_LEGACY:
        runtimes = dict(rt.caps.runtime) if rt.caps is not None else {}
        if rt.auth_kind == "oauth" and runtimes.get(rt.auth_kind) == "legacy_only":
            return SUBSCRIPTION_TERMINAL
        return TERMINAL_ONLY
    if not rt.may_read_sessions:
        return NO_SESSION_READ
    return ""


def _steer_reason(rt: _Runtime) -> str:
    if rt.caps is None:
        return UNKNOWN_PROVIDER
    return "" if rt.caps.turns.steer else f"{rt.label} takes a new message only between turns."


def _interrupt_reason(rt: _Runtime) -> str:
    if rt.caps is None:
        return UNKNOWN_PROVIDER
    return "" if rt.caps.turns.interrupt else f"{rt.label} can't be interrupted."


def _control_reason(rt: _Runtime, chat_reason: str) -> str:
    # No task first: a free slot is driveable by nobody but a superadmin, and "your role" would mislead.
    if rt.slot.task_id is None:
        return NO_TASK
    if not rt.may_steer:
        return NO_STEER_ROLE
    if chat_reason:
        return chat_reason
    if rt.row is None or rt.row.status != STATUS_RUNNING:
        return SESSION_ENDED
    return ""


def _control(reason: str) -> ControlOut:
    return ControlOut(available=not reason, reason="" if reason in _UNSHOWN_REASONS else reason)


def _principal(principal: Principal | None) -> Principal:
    # The cookie-less agent identity drives no console.
    return or_404(principal, detail=_NO_SUCH_SLOT)


@router.get("/api/console/{project_id}/{number}/runtime", response_model=ConsoleRuntimeOut)
# `view`, as the console open: what the slot runs is visible to whoever sees the slot; each control says why
# it is off for this caller.
@feature("worktrees", "view")
@on_session
def get_console_runtime(
    session: Session, project_id: int, number: int, node: Optional[str] = None,
    caller: AsyncCaller = Depends(current_caller),
) -> ConsoleRuntimeOut:
    """The slot's run and the caller's controls on it. 404 when the slot is not visible."""
    who = _principal(caller.principal)
    rt = _runtime(session, who, _slot(session, who, project_id, number, node))
    chat_reason = _chat_reason(rt)
    return ConsoleRuntimeOut(
        task_id=rt.slot.task_id,
        session_id=rt.slot.session_id,
        runtime=rt.row.runtime if rt.row is not None else None,
        status=rt.row.status if rt.row is not None else None,
        provider=rt.provider,
        provider_label=rt.label if rt.provider is not None else None,
        chat=_control(chat_reason),
        steer=_control(_steer_reason(rt)),
        interrupt=_control(_interrupt_reason(rt)),
        control=_control(_control_reason(rt, chat_reason)),
    )


def _check_control_rate_limit(user: AdminUser = Depends(require_user)) -> AdminUser:
    """Bound one user's controls, returning the user so `require_user` runs once."""
    if not _control_limiter.allow(str(user.id)):
        raise HTTPException(status_code=429, detail="too many messages — wait a minute")
    return user


@router.post("/api/console/{project_id}/{number}/runtime", response_model=ConsoleRuntimeSentOut)
# `tasks:edit` is the floor; the GET's `control` decision narrows it below.
@feature("tasks", "edit")
@on_session
def post_console_runtime(
    session: Session, request: Request, project_id: int, number: int, body: ConsoleRuntimeIn,
    node: Optional[str] = None, _user: AdminUser = Depends(_check_control_rate_limit),
    principal: Principal | None = Depends(current_principal),
) -> ConsoleRuntimeSentOut:
    """Send one control to the slot's live ACP run: one `runtime_control` intent, pushed to the node at once.

    Admitted by the GET's own `control` decision (`_control_reason`): steering needs a driveable slot AND a readable
    session. 404 slot not visible; 403 a cross-site request, or the caller's role (the reason as detail); 409 (the
    reason as detail) when the slot runs no live ACP session, the body names a session the slot no longer runs, or
    an interrupt its provider cannot take; 422 a steer/answer with no text. The node queues steer/answer as the next
    prompt, after the turn in flight."""
    # Only the console's own page may send one (`console.console_open`'s rule): the `SameSite=lax` cookie rides
    # any same-site POST. An absent header is a non-browser client.
    if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
        raise HTTPException(status_code=403, detail=NOT_FROM_CONSOLE)
    who = _principal(principal)
    # One transaction on the loop's session; the node push after its commit is a scheduled task, never awaited.
    slot = _slot(session, who, project_id, number, node)
    rt = _runtime(session, who, slot)
    reason = _control_reason(rt, _chat_reason(rt))
    if reason:
        raise HTTPException(status_code=403 if reason in _ROLE_REASONS else 409, detail=reason)
    assert rt.row is not None and slot.task_id is not None and slot.session_id is not None  # `reason` is "" only then
    if body.session_id != slot.session_id:
        raise HTTPException(status_code=409, detail=SESSION_MOVED)
    if rt.row.runtime != RUNTIME_ACP:
        raise HTTPException(status_code=409, detail=NOT_ACP)
    if body.type == "interrupt" and _interrupt_reason(rt):
        raise HTTPException(status_code=409, detail=_interrupt_reason(rt))
    payload: dict[str, Any] = {}
    if body.type in ("steer", "answer"):
        # Newlines and tabs kept; every other control character goes (`live_runner.scrub_control_chars`).
        text = scrub_control_chars(body.text or "")
        if not text.strip():
            raise HTTPException(status_code=422, detail="Write a message first.")
        payload = {"text": text}
    control_id = uuid.uuid4().hex
    intent = fleet_verdicts.build_runtime_control_intent(
        project_id, slot.project, number,
        task_id=slot.task_id, session_id=slot.session_id,
        control_id=control_id, control_type=body.type, control_payload=payload,
    )
    # Who sent which control, on the task's Activity trail: the write capture skips `/api/console/`
    # (`user_activity.WRITE_EXCLUDED_PREFIXES`). The type only, never the text. Committed with the intent below.
    record_task_note(
        session, slot.task_id, "runtime_control", body.type,
        {"control_type": body.type, "control_id": control_id, "session_id": slot.session_id},
        user=_user,
    )
    # The task page's live signal, as a pane message's (`live_runner.py`); not a transition.
    emit_console_event(session, entity="task", id=slot.task_id, action="control_message", record_transition=False)
    fleet_verdicts.issue_intents(session, slot.node_id, [intent])
    log.info(
        "task %s: minted runtime_control %s for %s#%s session %s",
        slot.task_id, body.type, slot.project, number, slot.session_id[:8],
    )
    return ConsoleRuntimeSentOut(intent_id=control_id, type=body.type)
