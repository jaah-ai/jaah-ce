"""The console's chat view and live control of one slot's ACP run.

  GET  /api/console/{project_id}/{number}/runtime   what the slot runs and which controls the caller has, each
                                                    with the reason it is off
  POST /api/console/{project_id}/{number}/runtime   send one control (steer/interrupt/answer/close) to that run
  POST /api/console/{project_id}/{number}/runtime/handoff   move that run between the chat and the terminal

The run is the slot's own: its `state='task'` hold and the session the node said it LAUNCHED
(`Worktree.launch_session_id`, the `sessions` row's `agent_session_id`). What a provider can do is read off its
catalogue row (`agent_providers.capabilities_of`), never branched on its value. A control becomes one
`runtime_control` slot intent (`fleet_verdicts.build_runtime_control_intent`), which the node queues on the
host's control lane; the events it causes come back through `routes/task_session_events.py`.

Slot visibility and editability are the console's (`visible_worktree_clause`, `worktree_editable_clause`, node
routing by `console._resolve_console_node`), so a slot this route answers for is one the terminal would open.
A steer or answer is an operator's words: never logged here.

The hand-off (D-CON-1) is a resume lease of the slot's own task onto the other front: `redispatch`
fences the hold, and a `lease_slot` intent copied from the slot's current one carries `resume`, the other
`runtime` and a `handoff` stanza naming the engine's own session id. A lease, not a node-local relaunch: a launch
needs the whole dispatch context (account, prompt bundle, ports, guard) the lease carries and the node does not
keep. On the node the old front closes before the new one starts, so one engine session never has two fronts;
each front is its own `sessions` row (the old one ends `superseded`), so usage folds per row once.
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

from app import agent_providers, fleet_verdicts, jaah_scheduler, project_identity, slot_state
from app.agent_sessions import RUNTIME_ACP, RUNTIME_LEGACY, STATUS_RUNNING
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import visible_session_clause, visible_worktree_clause, worktree_editable_clause
from app.db import get_session
from app.entity_shape import optional_uuid
from app.events import emit_console_event
from app.live_runner import scrub_control_chars
from app.models.agent_sessions import AgentSession
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.worktrees import SlotIntent, Worktree
from app.node_identity import node_ref
from app.ratelimit import SharedRateLimiter
from app.routes.console import _NO_SUCH_SLOT, _resolve_console_node
from app.user_activity import record_task_note

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
TOGGLE_UNAVAILABLE = "Switching a live session between chat and terminal isn't available yet."
NO_NATIVE_REF = "No conversation to hand over yet."
CANNOT_SWITCH = "This session can't switch views."
ACCOUNT_DELETED = "Its account was deleted."
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
    toggle: ControlOut


class ConsoleRuntimeIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    type: Literal["steer", "interrupt", "answer", "close"]
    #: Required for steer/answer; ignored for interrupt/close.
    text: Optional[str] = Field(default=None, max_length=MAX_CONTROL_TEXT)


class ConsoleRuntimeSentOut(BaseModel):
    intent_id: str
    type: str


class ConsoleHandoffIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    to: Literal["chat", "terminal"]


class ConsoleHandoffOut(BaseModel):
    intent_id: str
    to: str


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
    node_id = _resolve_console_node(project_id, number, node, session)
    project = project_identity.names_by_id(session, [project_id]).get(project_id)
    if not node_id or project is None:
        raise HTTPException(status_code=404, detail=_NO_SUCH_SLOT)
    row = session.execute(
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
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=_NO_SUCH_SLOT)
    # A run is addressable only while the slot holds its task with a launched session (`live_hold_for_task`).
    held = row.state == slot_state.TASK and row.task_id is not None and bool(row.launch_session_id)
    return _Slot(
        node_id=node_id, project_id=project_id, number=number, project=project,
        task_id=row.task_id if held else None, session_id=row.launch_session_id if held else None,
        may_edit=bool(row.may_edit),
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


def _current_lease(session: Session, rt: _Runtime) -> Optional[dict[str, Any]]:
    """The payload of the `lease_slot` order that started the slot's current occupancy (its `dispatch_id`), or
    None: every decision a hand-off's launch repeats, and whether that launch was itself a hand-off."""
    if rt.slot.task_id is None or rt.slot.session_id is None:
        return None
    node = node_ref(rt.slot.node_id)
    dispatch_id = (
        select(Worktree.dispatch_id)
        .where(Worktree.node_id == node, Worktree.project_id == rt.slot.project_id, Worktree.number == rt.slot.number)
        .scalar_subquery()
    )
    return session.scalar(
        select(SlotIntent.payload)
        .where(
            SlotIntent.node_id == node,
            SlotIntent.project_id == rt.slot.project_id,
            SlotIntent.number == rt.slot.number,
            SlotIntent.verb == fleet_verdicts.VERB_LEASE_SLOT,
            SlotIntent.payload["dispatch_id"].astext == dispatch_id,
        )
        .order_by(SlotIntent.created_at.desc())
        .limit(1)
    )


def _native_ref(rt: _Runtime, lease: Optional[dict[str, Any]]) -> Optional[str]:
    """The engine's own session id to resume: the row's, else the one the hand-off that started this front
    reopened. The engine keeps one id across both fronts (its resume reopens the same id), and a terminal front
    (a legacy row) reports none of its own. Never another attempt's id: a front no hand-off started resumes
    nothing. Only a canonical UUID counts: the id comes off the session's own event stream, which the agent can
    write, and lands on a resume argv (`apps/jaahd/acp/launch.py::NATIVE_REF`, which the node checks again)."""
    if rt.row is not None and rt.row.native_session_ref:
        return _uuid_or_none(rt.row.native_session_ref)
    handoff = (lease or {}).get("handoff")
    return _uuid_or_none(handoff.get("native_session_ref")) if isinstance(handoff, dict) else None


def _front(rt: _Runtime, lease: Optional[dict[str, Any]]) -> str:
    """"chat" or "terminal": the row's runtime, or the lease's runtime fact while a fresh ACP row has not
    streamed its first event (it reads `legacy` until then)."""
    acp = (rt.row is not None and rt.row.runtime == RUNTIME_ACP) or (lease or {}).get("runtime") == RUNTIME_ACP
    return "chat" if acp else "terminal"


def _uuid_or_none(ref: Optional[str]) -> Optional[str]:
    """`ref` when it is a canonical lowercase UUID, else None."""
    parsed = optional_uuid(ref)
    return ref if parsed is not None and str(parsed) == ref else None


def _account_deleted(rt: _Runtime, lease: Optional[dict[str, Any]]) -> bool:
    """The run's lease named an account (`config_dir` or `api_key_account`) that no longer exists: deleting it
    set the session's `account_id` NULL (SET NULL), so there is nothing left to carry the run on."""
    return (rt.row is not None and rt.row.account_id is None and lease is not None
            and bool(lease.get("config_dir") or lease.get("api_key_account")))


def _toggle_reason(rt: _Runtime, native_ref: Optional[str], lease: Optional[dict[str, Any]] = None) -> str:
    """Why the run can't switch between the chat and the terminal, or "". Read off the catalogue row."""
    if rt.slot.task_id is None:
        return NO_TASK
    if not rt.may_steer:
        return NO_STEER_ROLE
    if rt.row is None:
        return NOT_STARTED
    if rt.caps is None:
        return ACCOUNT_DELETED if _account_deleted(rt, lease) else UNKNOWN_PROVIDER
    if not rt.caps.console.toggle:
        return rt.caps.console.reason or TOGGLE_UNAVAILABLE
    if rt.caps.sessions.resume == "none":
        return f"{rt.label} can't resume a session."
    # Both fronts exist only where the auth kind has the operator's switch ("acp").
    runtime = dict(rt.caps.runtime).get(rt.auth_kind)
    if runtime == "legacy_only":
        return f"{rt.label} runs this account in the terminal only."
    if runtime != "acp":
        return f"{rt.label} has no terminal."
    if rt.row.status != STATUS_RUNNING:
        return SESSION_ENDED
    if not native_ref:
        return NO_NATIVE_REF
    return ""


def _account_refusal(session: Session, node_id: str, account_id: uuid.UUID) -> str:
    """Why the slot's own account may not carry its run on, or "". The scheduler's roster and verdict facts
    (`jaah_scheduler.account_exclusion_reason`); the candidate set holds enabled accounts only, so a missing
    one is disabled. POST-only: the candidate read is the whole roster plus the fold, too dear for the GET."""
    from app.routes.fleet import _account_candidates

    candidate = next((c for c in _account_candidates(session, node_id) if c["account_id"] == account_id), None)
    reason = jaah_scheduler.account_exclusion_reason(candidate if candidate is not None else {})
    return f"Its account can't run: {reason}." if reason else ""


def _control(reason: str) -> ControlOut:
    return ControlOut(available=not reason, reason="" if reason in _UNSHOWN_REASONS else reason)


def _principal(principal: Principal | None) -> Principal:
    # The cookie-less agent identity drives no console.
    if principal is None:
        raise HTTPException(status_code=404, detail=_NO_SUCH_SLOT)
    return principal


@router.get("/api/console/{project_id}/{number}/runtime", response_model=ConsoleRuntimeOut)
# `view`, as the console open: what the slot runs is visible to whoever sees the slot; each control says why
# it is off for this caller.
@feature("worktrees", "view")
def get_console_runtime(
    project_id: int,
    number: int,
    node: Optional[str] = None,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ConsoleRuntimeOut:
    """The slot's run and the caller's controls on it. 404 when the slot is not visible."""
    who = _principal(principal)
    rt = _runtime(session, who, _slot(session, who, project_id, number, node))
    chat_reason = _chat_reason(rt)
    lease = _current_lease(session, rt)
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
        toggle=_control(_toggle_reason(rt, _native_ref(rt, lease), lease)),
    )


def _check_control_rate_limit(user: AdminUser = Depends(require_user)) -> AdminUser:
    """Bound one user's controls, returning the user so `require_user` runs once."""
    if not _control_limiter.allow(str(user.id)):
        raise HTTPException(status_code=429, detail="too many messages — wait a minute")
    return user


@router.post("/api/console/{project_id}/{number}/runtime", response_model=ConsoleRuntimeSentOut)
# `tasks:edit` is the floor; the GET's `control` decision narrows it below.
@feature("tasks", "edit")
def post_console_runtime(
    request: Request,
    project_id: int,
    number: int,
    body: ConsoleRuntimeIn,
    node: Optional[str] = None,
    _user: AdminUser = Depends(_check_control_rate_limit),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
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


@router.post("/api/console/{project_id}/{number}/runtime/handoff", response_model=ConsoleHandoffOut)
# `tasks:edit` is the floor; the GET's `toggle` decision narrows it below.
@feature("tasks", "edit")
def post_console_handoff(
    request: Request,
    project_id: int,
    number: int,
    body: ConsoleHandoffIn,
    node: Optional[str] = None,
    _user: AdminUser = Depends(_check_control_rate_limit),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ConsoleHandoffOut:
    """Move the slot's live run to the other front: re-arm the slot and issue a resume lease onto `to`.

    Admitted by the GET's own `toggle` decision (`_toggle_reason`). 404 slot not visible; 403 a cross-site request
    or the caller's role; 409 (the reason as detail) when the run can't switch, is already on `to`, the body names
    a session the slot no longer runs, its account can't run or was deleted, or the slot's current lease can't be
    resumed as is."""
    # The control POST's rule: only the console's own page.
    if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
        raise HTTPException(status_code=403, detail=NOT_FROM_CONSOLE)
    who = _principal(principal)
    slot = _slot(session, who, project_id, number, node)
    rt = _runtime(session, who, slot)
    lease = _current_lease(session, rt)
    native_ref = _native_ref(rt, lease)
    reason = _toggle_reason(rt, native_ref, lease)
    if reason:
        raise HTTPException(status_code=403 if reason in _ROLE_REASONS else 409, detail=reason)
    assert rt.row is not None and slot.task_id is not None and slot.session_id is not None  # `reason` is "" only then
    if body.session_id != slot.session_id:
        raise HTTPException(status_code=409, detail=SESSION_MOVED)
    if body.to == _front(rt, lease):
        raise HTTPException(status_code=409, detail=f"The session is already in the {body.to}.")
    worktree = session.execute(
        select(Worktree.id, Worktree.node_id, Worktree.generation, Worktree.dispatch_id, Worktree.leased_account_id)
        .where(
            Worktree.node_id == node_ref(slot.node_id),
            Worktree.project_id == project_id,
            Worktree.number == number,
            Worktree.state == slot_state.TASK,
            Worktree.task_id == slot.task_id,
            Worktree.launch_session_id == slot.session_id,
        )
    ).first()
    if worktree is None:
        raise HTTPException(status_code=409, detail=SESSION_MOVED)
    # The same account carries the run on, so it must still be allowed to; checked before `redispatch` so a
    # refusal leaves the live run untouched.
    if worktree.leased_account_id is not None:
        refusal = _account_refusal(session, str(worktree.node_id), worktree.leased_account_id)
        if refusal:
            raise HTTPException(status_code=409, detail=refusal)
    elif rt.row.account_id is not None:
        # The session ran on an account whose bind went NULL since the session row was read: deleted in between.
        # Never re-armed onto no account.
        raise HTTPException(status_code=409, detail=ACCOUNT_DELETED)
    # The slot's current lease: every decision the new launch repeats. A Jaah-account or SDK-agent lease is not
    # re-issued from here (its block is minted per dispatch), nor is one with no prompt.
    prior = lease
    if not prior or not prior.get("prompt_bundle") or prior.get("bedrock") or prior.get("launch"):
        raise HTTPException(status_code=409, detail=CANNOT_SWITCH)
    # `redispatch` commits by itself and ends the old session row `superseded`. Anything failing after it leaves
    # a re-armed `task` hold with no lease of its own, which the drain re-dispatches as a resume in place.
    re_armed = slot_state.redispatch(
        session, worktree_id=worktree.id, generation=worktree.generation,
        reason=f"hand task {slot.task_id} to the {body.to}",
    )
    if re_armed is None:
        raise HTTPException(status_code=409, detail=SESSION_MOVED)
    # `redispatch` blanks the account bind; the key route needs it before the runner's first report.
    if worktree.leased_account_id is not None:
        slot_state.bind_account(
            session, worktree_id=worktree.id, generation=re_armed["generation"],
            account_id=worktree.leased_account_id,
        )
    intent = fleet_verdicts.build_lease_intent(
        project_id, slot.project, number,
        task_id=prior.get("task_id", slot.task_id),
        base=prior.get("base"),
        holder=prior.get("holder") or "fleet",
        generation=re_armed["generation"],
        dispatch_id=re_armed["dispatch_id"],
        config_dir=prior.get("config_dir"),
        securestorage_dir=prior.get("securestorage_dir"),
        api_key_account=prior.get("api_key_account") or "",
        provider=prior.get("provider") or agent_providers.DEFAULT_PROVIDER,
        git_author=prior.get("git_author"),
        ports=prior.get("ports"),
        prompt=prior["prompt_bundle"],
        memory=prior.get("memory"),
        guard=prior.get("guard"),
        has_repo=prior.get("has_repo"),
        resume=True,
        runtime="acp" if body.to == "chat" else "legacy",
        reason=f"hand task {slot.task_id} to the {body.to}",
        handoff={"to": body.to, "native_session_ref": native_ref, "from_session_id": slot.session_id},
    )
    record_task_note(
        session, slot.task_id, "runtime_control", "handoff",
        {"control_type": "handoff", "to": body.to, "session_id": slot.session_id},
        user=_user,
    )
    emit_console_event(session, entity="task", id=slot.task_id, action="control_message", record_transition=False)
    fleet_verdicts.issue_intents(session, slot.node_id, [intent])
    log.info(
        "task %s: handing %s#%s session %s to the %s at generation %s",
        slot.task_id, slot.project, number, slot.session_id[:8], body.to, re_armed["generation"],
    )
    # The new dispatch id names the hand-off: it fences the lease's ack and the slot's next session.
    return ConsoleHandoffOut(intent_id=re_armed["dispatch_id"], to=body.to)
