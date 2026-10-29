"""What a request names about its caller beyond its actor: the browser, the address the ALB saw, the console
session (one sign-in) and the agentic session a runner's browser declared — so a console write can be traced
without reconstructing it from ALB logs.

Evidence only, never a decision: every value but `console_session` and `client_ip` is client-chosen text.

`UserActivityMiddleware` binds these facts for each mutating request (`bound`). Its captured request row (one
per mutating request) carries them, and so does every `task:%` transition the request causes
(`record_task_transition` merges `current()`), so Audit can filter one console session's writes
(`GET /api/audit?console_session=`). A hand-written `record_write` row does not repeat them: the captured row
of the same request holds them."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional
from uuid import UUID

from starlette.requests import Request

from app import act_as

#: The header a runner's browser sends to name its own agentic session.
AGENT_SESSION_HEADER = "X-Jaah-Session"

# Header text an attacker chose: bounded, so a request cannot write an arbitrarily large row or log line.
_CLIENT_HEADER_MAX = 256
# A session id is an identifier, not prose: anything else is dropped rather than stored.
_AGENT_SESSION = re.compile(r"[A-Za-z0-9._:-]{1,128}")
#: `console_session`'s shape, which the Audit filter validates against.
CONSOLE_SESSION = re.compile(r"[0-9a-f]{16}")
#: Every `data` key this module writes: `audit_visibility.tenant_worded` withholds them from a tenant reading an
#: operator's acting-as row (staff addresses and sessions are not the tenant's).
CALLER_KEYS = (
    "user_agent", "client_ip", "forwarded_for", "console_session", "agent_session", "api_key_id", "api_key_name",
)

_current: ContextVar[Optional[dict[str, str]]] = ContextVar("request_caller", default=None)
# Whether the bound request sent `X-Jaah-Act-As` at all: what `current` falls back on when no guard resolved it.
_act_as_sent: ContextVar[bool] = ContextVar("request_caller_act_as_sent", default=False)


def client_headers(request: Request) -> dict[str, Optional[str]]:
    """The user agent (a script, a runner's browser, a person), the address the ALB saw (`client_ip`) and the
    `X-Forwarded-For` chain, when one came.

    The ALB appends the address it saw as the chain's RIGHT-most hop; nginx forwards the header unchanged
    (`apps/api/entrypoint.sh`). So that hop is ours, and kept in its own field, while every hop left of it is
    client-declared text: the chain keeps its TAIL when bounded, so padding cannot push the ALB's hop out. A
    right-most hop that is not an IP address (no ALB in front, as on a local stack) is no `client_ip`. Every header
    line is joined first, as uvicorn joins them for `request.client.host` (`console_relay.server.client_address`),
    so a second line cannot make the two disagree."""
    user_agent = request.headers.get("user-agent")
    forwarded_for = ", ".join(request.headers.getlist("x-forwarded-for"))
    return {
        "user_agent": user_agent[:_CLIENT_HEADER_MAX] if user_agent else None,
        "client_ip": _right_most_hop(forwarded_for) if forwarded_for else None,
        "forwarded_for": forwarded_for[-_CLIENT_HEADER_MAX:] if forwarded_for else None,
    }


def _right_most_hop(forwarded_for: str) -> Optional[str]:
    """An IPv6 zone (`fe80::1%<text>`) is text, never an ALB's hop."""
    hop = forwarded_for.rsplit(",", 1)[-1].strip()
    try:
        address = ipaddress.ip_address(hop)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        return None
    return str(address)


def console_session_id(admin_id: UUID, iat: int) -> str:
    """One browser sign-in's stable id: the cookie's `iat` (kept across re-mints) and its user, keyed under the
    session secret, so it names the session without revealing when it signed in or carrying anything that could
    replay it. Limits: `iat` is whole seconds, so one user's two sign-ins in the same second share an id; an org
    switch re-mints for another membership row, so the id changes with it; rotating the secret changes every id."""
    from app.auth import resolve_session_secret  # `app.auth` imports this module.

    key = hmac.new(resolve_session_secret().encode(), b"console-session-id/v1", hashlib.sha256).digest()
    return hmac.new(key, f"{admin_id}:{iat}".encode(), hashlib.sha256).hexdigest()[:16]


def facts(request: Request) -> dict[str, str]:
    """This request's caller facts, absent keys omitted. `console_session` and `api_key_*` are set by
    `SessionAuthMiddleware` from the verified cookie or key (`request.state`)."""
    found: dict[str, Optional[str]] = {
        **client_headers(request),
        "console_session": getattr(request.state, "console_session", None),
    }
    # A request made with an API key (`app/api_keys.py`): the Audit reads it as "<owner> via API key <name>".
    key = getattr(request.state, "api_key", None)
    if key is not None:
        found["api_key_id"] = str(key.key_id)
        found["api_key_name"] = key.name
    declared = request.headers.get(AGENT_SESSION_HEADER)
    if declared and _AGENT_SESSION.fullmatch(declared):
        found["agent_session"] = declared
    return {key: value for key, value in found.items() if value}


@contextmanager
def bound(request: Request, caller: dict[str, str]) -> Iterator[None]:
    """Bind `caller` (`facts(request)`) for everything serving `request` (its endpoint runs in a task that copies
    this context)."""
    token = _current.set(caller)
    # An API key never acts as an org (`auth._acting_seat`), so its header must not withhold the key's own facts.
    sent = _act_as_sent.set(act_as.requested(request.headers) is not None and "api_key_id" not in caller)
    try:
        yield
    finally:
        _act_as_sent.reset(sent)
        _current.reset(token)


def current() -> dict[str, str]:
    """The bound request's caller facts, or `{}` off a request (a cron, a worker).

    `{}` too while an operator's tab acts as an org — as the guard resolved it (`act_as.reader_acting`), not
    from the header, which a tenant may send and which is ignored for one: its `task:%` rows file in the tenant's
    org, which must not read staff addresses or sessions. Its captured row still holds them, for the operator
    plane only. A request the guard never resolved (an `@always` route) that sent the header is withheld too:
    fail closed."""
    if act_as.reader_acting() or (_act_as_sent.get() and not act_as.reader_resolved()):
        return {}
    return _current.get() or {}


def merged(data: dict[str, Any], row_org: Optional[UUID]) -> dict[str, Any]:
    """`data` with the bound caller facts added under keys it does not already hold, or `data` unchanged off a
    request — and unchanged when the row files in a tenant org the caller is not bound to: an operator-plane
    write to a tenant's task (not acting, so `current` keeps the facts) must not hand that tenant's Audit the
    staff member's address, browser or sessions. A row of no known org gets none (fail closed)."""
    from app.tenancy import seam
    from app.tenancy.ids import is_tenant_org

    caller = current()
    if not caller or row_org is None or (is_tenant_org(row_org) and seam.bound_org() != row_org):
        return data
    return {**caller, **data}
