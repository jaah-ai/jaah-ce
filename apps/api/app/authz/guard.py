"""The global FastAPI dependency `authz_guard` and the operator's org-filter header."""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection

from app.auth import COOKIE_NAME, hidden_route, require_operator, require_operator_acting, require_superadmin
from app.db import SessionLocal
from app.entity_shape import optional_uuid
from app.models.orgs import Org
from app.tenancy import exclusion_log, seam
from app.authz.catalog import Access, Tenancy
from app import api_keys
from app.authz.declarations import declaration_of, is_session_only
from app.authz.principal import Principal, RevokedSession, _PRINCIPAL_CACHE_ATTR
from app.authz.enforce import (
    _bind_read_scope,
    _decides_without_io,
    _deny_websocket,
    _enforce,
    _forbidden,
    _is_read,
)
from app.authz import principal as principal_module

log = logging.getLogger(__name__)


#: The operator's global org filter: `<uuid>[,<uuid>…]`, GET/HEAD only, operator only;
#: absent or empty is All. The web client sends it (`apps/web/src/lib/orgScope.ts`).
ORG_SCOPE_HEADER = "x-jaah-org-scope"

#: The most org ids one filter honours; the rest are ignored.
MAX_ORG_SCOPE_IDS = 50


def _requested_org_filter(conn: HTTPConnection, principal: Principal | None) -> frozenset[UUID] | None:
    """The org ids an operator's read asked to narrow to, or `None` when the header must be IGNORED: not
    a read, not an operator, absent, or naming nothing parseable. Malformed ids are dropped. Pure: the
    ids are reduced to orgs that exist by `_existing_orgs`, off the loop."""
    if principal is None or not principal.is_operator or not _is_read(conn):
        return None
    raw = conn.headers.get(ORG_SCOPE_HEADER, "")
    ids: set[UUID] = set()
    # Capped: a picker sends a handful, and every ORM select of the request carries the `IN` list.
    for part in raw.split(",", MAX_ORG_SCOPE_IDS)[:MAX_ORG_SCOPE_IDS]:
        parsed = optional_uuid(part.strip())
        if parsed is not None:
            ids.add(parsed)
    return frozenset(ids) or None


def _existing_orgs(ids: frozenset[UUID]) -> frozenset[UUID]:
    """`ids` reduced to orgs that exist (unknown ids are dropped; none left is All). Blocking: run in a
    worker thread. `orgs` is not tenant-owned, so the read needs no scope."""
    with SessionLocal() as session:
        return frozenset(session.scalars(select(Org.id).where(Org.id.in_(sorted(ids)))))


def _always_route_org_filter(conn: HTTPConnection) -> frozenset[UUID] | None:
    """`_requested_org_filter` for an `ALWAYS` route, whose guard resolves no principal: resolved here
    (and cached for the handler's `Depends(current_principal)`). A cookie that does not verify is no
    operator, so no filter; the handler answers it as it always has. Blocking: run in a worker thread."""
    try:
        principal = principal_module.current_principal(conn)
    except RevokedSession:
        return None
    org_filter = _requested_org_filter(conn, principal)
    return _existing_orgs(org_filter) if org_filter is not None else None


#: The handler gates that make a whole route the operator's (or a superadmin's): `app/auth.py`, which answers
#: their refusal with `hidden_route`.
_HIDDEN_DOOR_GATES = (require_superadmin, require_operator, require_operator_acting)

#: The refusals a hidden door answers as a missing one; a 401 stays, an unknown path answers it too.
_HIDDEN_REFUSALS = frozenset({403, 404})


def _dependant_calls(dependant: Any) -> list[Any]:
    calls = [dependant.call]
    for sub in dependant.dependencies:
        calls.extend(_dependant_calls(sub))
    return calls


def _is_hidden_door(route: Any) -> bool:
    """Is `route` a door no one off the operator plane may learn exists? Declared fleet-wide, or behind a
    `_HIDDEN_DOOR_GATES` handler gate. Uncached (an `APIRoute` is unhashable): it runs on a refusal only."""
    declaration = declaration_of(getattr(route, "endpoint", None))
    if declaration is not None and (
        declaration.access is Access.SUPERADMIN or declaration.tenancy is Tenancy.OPERATOR
    ):
        return True
    dependant = getattr(route, "dependant", None)
    return dependant is not None and any(gate in _dependant_calls(dependant) for gate in _HIDDEN_DOOR_GATES)


def _cached_principal(conn: HTTPConnection) -> Principal | None:
    """The principal `_enforce` resolved for this request, from its per-request cache, or `None`.
    No I/O: safe on the loop."""
    cached = getattr(conn.state, _PRINCIPAL_CACHE_ATTR, None)
    return cached if isinstance(cached, Principal) else None


async def authz_guard(conn: HTTPConnection) -> None:
    """The route-aware global dependency: one authorization decision, in one place, for every route.

    `HTTPConnection`, never `Request` (a hard error on WebSocket routes). Owns its own short-lived session,
    never `Depends(get_session)`, which would live as long as a socket or stream. Runs the decision in a
    worker thread only when it needs I/O. Registered in `app/main.py::create_app` as a global dependency,
    so it decides on the already-resolved route and cannot disagree with the router."""
    # Attribute this request's reads first (`exclusion_log.label_for_connection`, the route template),
    # above the no-I/O hoist so `@always` routes are attributed too. Cannot raise, so it is safe outside
    # the WebSocket `try`. Then answer the no-I/O classes on the loop (`_decides_without_io`); an
    # undeclared route still falls through to `_enforce`, which denies it.
    exclusion_log.bind_caller(exclusion_log.label_for_connection(conn.scope))

    # Nothing fallible may join these three lines: they sit outside the WebSocket `try`, and an exception
    # here would close 1006 instead of a readable 4401. `declaration_of` cannot raise.
    declaration = declaration_of(conn.scope.get("endpoint"))
    if is_session_only(conn.scope.get("endpoint")) and api_keys.of(conn) is not None:
        # Ahead of the no-I/O hoist, so an `@always` route is covered too. A key never reaches a WebSocket.
        raise api_keys.ApiKeyNotAllowed()
    if declaration is not None and _decides_without_io(declaration):
        # An `ALWAYS` route binds its own plane in the handler, so the operator's org filter is set here,
        # for the residue sites that re-apply it (`seam.request_org_filter_applied`). Only when the header
        # is present on a read, so health probes and every other caller stay off the worker pool.
        if (
            declaration.access is Access.ALWAYS
            and _is_read(conn)
            and conn.headers.get(ORG_SCOPE_HEADER)
            and conn.cookies.get(COOKIE_NAME)
        ):
            org_filter = await run_in_threadpool(_always_route_org_filter, conn)
            if org_filter:
                seam.set_org_filter(org_filter)
        return

    if conn.scope.get("type") != "websocket":
        # HTTP: `_enforce` already raises the right shape — but on an operator door a key or object refusal would
        # still tell a tenant the door exists, so it reads as the router's unknown path. The operator,
        # who knows the door, keeps the real refusal (an `OtherOrgDenial`'s "act as" names the way in).
        try:
            org_id = await run_in_threadpool(_enforce, conn, declaration)
        except HTTPException as denial:
            refused = _cached_principal(conn)
            if (
                denial.status_code in _HIDDEN_REFUSALS
                and not (refused is not None and refused.is_operator)
                and _is_hidden_door(conn.scope.get("route"))
            ):
                raise hidden_route() from denial
            raise
        # Never None here (`_enforce` raised), but `raise`, not `assert`, since `-O` strips asserts.
        if declaration is None:  # pragma: no cover — `_enforce` raised above if it were None
            raise _forbidden(conn)
        principal = _cached_principal(conn)
        org_filter = _requested_org_filter(conn, principal)
        if org_filter is not None:
            org_filter = await run_in_threadpool(_existing_orgs, org_filter)
        await _bind_read_scope(conn, declaration, org_id, principal, org_filter)
        return

    # One decision, two transports. Every exception is reshaped: an unhandled one would close 1006,
    # which `NON_RETRYABLE_CLOSE_CODES` omits, sending every open console into the reconnect ladder.
    try:
        org_id = await run_in_threadpool(_enforce, conn, declaration)
    except HTTPException as denial:
        raise await _deny_websocket(conn, denial.status_code, str(denial.detail)) from denial
    except Exception as unexpected:
        log.exception("authz: denying %s — the decision itself failed", conn.scope.get("path"))
        raise await _deny_websocket(conn, 401, "unauthorized") from unexpected
    # Outside the `try`: the bind is not part of the decision; `_bind_read_scope` reshapes its own
    # refusal. `raise`, not `assert`: a stripped assert would raise outside the reshaping and close 1006.
    if declaration is None:  # pragma: no cover — `_enforce` raised above; see the HTTP arm.
        raise await _deny_websocket(conn, 403, "forbidden")
    await _bind_read_scope(conn, declaration, org_id, _cached_principal(conn))
