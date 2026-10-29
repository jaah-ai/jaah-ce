"""Browser-facing SSE relay — GET /api/stream.

Session-authenticated with `require_user`; a second consumer of the one `events.broadcaster`
(app/events.py). The `cockpit_events` payload is a live object-id + transition feed, so relaying it to
every tab would expose the id space `require_object_access`'s 404-not-403 rule withholds. For
`entity in PROBED_ENTITIES` and for `id=0` collection sentinels, access is checked per subscriber
before yielding; other entities and the keepalive pass through. A single feature-key gate was rejected:
it would kill live updates for every partially-granted user.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import literal, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection

from app.auth import COOKIE_NAME, read_session, require_user
from app import api_keys, auth, edition_features, hipaa, ip_allowlist
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app.authz.object_access import can_access
from app.authz.principal import Principal, RevokedSession, current_principal
from app.authz.visibility import (
    NODE_SPEC,
    OBJECT_SPEC_BY_NAME,
    OBJECT_TYPES,
    ObjectAccess,
    ObjectSpec,
    visible_node_ref_clause,
)
from app.db import SessionLocal, get_session
from app.entity_shape import optional_uuid
from app.events import SESSION_SWITCHED, broadcaster
from app.models.audit import CommandAudit
from app.models.identity import AdminUser
from app.tenancy import seam

router = APIRouter(prefix="/api", tags=["stream"])
log = logging.getLogger("cockpit-stream")

# Keepalive cadence: holds the connection through proxy idle timeouts and lets EventSource notice a
# dead peer within this window.
STREAM_KEEPALIVE_SECONDS = 15
# SSE `retry:` (ms) for the browser's next auto-reconnect, sent first so it applies even if the first
# connection drops; otherwise EventSource uses its slower implementation default.
STREAM_RETRY_MS = 1000

# The entities naming an access-controlled object: `authz.OBJECT_TYPES` itself, so a new type cannot be
# relayed unfiltered; the sole enforcement here (no feature gate). `_may_see` handles
# `ObjectSpec.unscoped_read_key`, string uuid ids (`_probe_id`) and uuid-type `id=0` sentinels. The
# `project` and `credential` arms have no emitter today (`models.Credential`) and are wired anyway.
OBJECT_FILTERED_ENTITIES = OBJECT_TYPES

# `task` is filtered though not grantable: `can_access(..., "task", ...)` answers per row through
# `visible_task_clause` (`tasks:view` on the row's project, creator-only when hidden).
#
# `command` is filtered by a derived node answer, not a grant, and must never join the tuple above;
# see `_may_see_command`.
COMMAND_ENTITY = "command"

# A flat superadmin arm: `cron` rows are ungrantable and every cron route is `Access.SUPERADMIN`, so
# without an arm every zero-grant session would see every cron's existence and cadence. The emitter spells
# the entity as a literal (the `emit_console_event` scan refuses non-literals), so the coupling is held by
# `test_crons_routes.py::test_the_cron_event_entity_the_routes_emit_is_the_one_the_relay_filters`.
CRON_ENTITY = "cron"

# A flat superadmin arm for the tenancy axis (`Axis` in `app/tenancy/manifest.py`): unfiltered it would
# leak every tenant's existence and uuid. Coupled to its literal emitter by
# `tests/test_orgs_routes.py::test_the_org_event_entity_the_routes_emit_is_the_one_the_relay_filters`.
ORG_ENTITY = "org"

# A flat superadmin arm: node groups are the operator's EC2 capacity instructions and every
# `/api/node-groups` route is `Access.SUPERADMIN` (`routes/node_groups.py`). Coupled to its literal emitter
# by `tests/test_node_groups_routes.py::test_the_node_group_event_entity_the_routes_emit_is_the_one_the_relay_filters`.
NODE_GROUP_ENTITY = "node_group"

# A flat operator arm: `port_grid_vars` are the operator's host port ranges and every `routes/port_grid.py`
# route requires `require_operator`. Coupled to its literal emitter by
# `tests/test_port_grid.py::test_the_port_grid_var_event_entity_the_routes_emit_is_the_one_the_relay_filters`.
PORT_GRID_VAR_ENTITY = "port_grid_var"

# An Add-node order moved a stage (`app/node_orders.py::advance`). Every order route is org-admin
# `Tenancy.CALLER_PLANE`, so its arm is the org-admin one; the emit tags the order's org. Coupled to its literal
# emitter by `tests/test_node_orders.py::test_the_order_event_entity_is_the_one_the_relay_filters`.
NODE_ORDER_ENTITY = "node_order"

# A tenant's Jaah AI balance changed (`app/token_billing/ledger.py::emit_wallet_event`): the event names the
# org, so it reaches that org's own members and the operator plane only — another tenant learning when a
# balance moves would be a covert cross-tenant channel. Coupled to its literal emitter by
# `tests/token_billing/test_routes.py::test_the_wallet_event_entity_the_ledger_emits_is_the_one_the_relay_filters`.
TOKEN_WALLET_ENTITY = "token_wallet"

# A project's Live Cast settings or an override changed (`routes/live_cast.py`). Every Live Cast route is org-admin
# `Tenancy.CALLER_PLANE`, so its arm is the org-admin one; the emit tags the project's org. Coupled to its literal
# emitter by `tests/test_live_cast_admin.py::test_the_live_cast_event_entity_is_the_one_the_relay_filters`.
LIVE_CAST_ENTITY = "live_cast"

# Every entity this relay must probe before yielding. The generator branches on this, never on
# `OBJECT_FILTERED_ENTITIES` alone, or a derived arm in `_may_see` would be dead code while the entity
# relayed unfiltered.
PROBED_ENTITIES = OBJECT_FILTERED_ENTITIES + (
    COMMAND_ENTITY, CRON_ENTITY, ORG_ENTITY, NODE_GROUP_ENTITY, PORT_GRID_VAR_ENTITY, TOKEN_WALLET_ENTITY,
    NODE_ORDER_ENTITY, LIVE_CAST_ENTITY,
)

# How stale this connection's subject set (superadmin flag + group memberships) may get, since
# `current_principal` memoizes per connection. Re-resolved lazily on the next event of any entity, which
# also re-verifies the cookie. Direct object grants are never cached; group-derived grants and task
# visibility (`tasks:view` on the scope) lag up to this long, the accepted residual. HTTP is unaffected.
SUBJECT_REFRESH_SECONDS = 60
# Retry delay after a failed re-resolve: short so a DB blip does not extend a revoked session's
# visibility, non-zero so a refusing DB is not re-dialled once per event.
SUBJECT_REFRESH_RETRY_SECONDS = 5


def _resolve_subject(conn: HTTPConnection) -> Principal | None:
    """Re-resolve this subscriber's `Principal` from the DB, bypassing the connection-lifetime memo.

    A fresh `state` makes the public `current_principal` re-run its cookie verification and subject
    query, so a logged-out session stops the stream. Uses its own short-lived session, never the
    request-scoped one (see `browser_stream`)."""
    return current_principal(HTTPConnection({**conn.scope, "state": {}}))


def _probe_id(spec: ObjectSpec, object_id: Any) -> int | UUID | None:
    """The id this event NAMES, in `spec`'s own domain — or None when the payload names none.

    A uuid id arrives as a string (`events.emit_console_event` stringifies it; the payload is
    `json.loads`ed), so judging it by the column type would silence every node or account event for every
    non-superadmin. A `UUID` object is refused: it is not a wire shape, and `json.dumps` would raise on
    it inside the generator, killing the tab's stream.

    No loose coercion: an int-keyed type takes an `int` only (`int("4_7")` is a different row).
    `UUID(str)` is safe because every spelling it accepts denotes the same value."""
    if isinstance(object_id, bool):
        # `isinstance(True, int)` is True in Python, so bools are refused before either arm.
        return None
    if spec.id_type is int:
        return object_id if isinstance(object_id, int) else None
    return optional_uuid(object_id) if isinstance(object_id, str) else None


def _may_see_command(principal: Principal | None, object_id: Any) -> bool:
    """May this subscriber see the `command` event naming `CommandAudit` row `object_id`?

    The one node-derived arm, and not a map: `worktree`/`runner` events are all emitted with `id=0`, so
    only `command` names a real row. Kept out of `OBJECT_FILTERED_ENTITIES` because `command` is not a
    grantable type; the answer is derived from the row's node at probe time.

    A row bound to no node is not withheld, matching `routes/portfolio.py::command_log`
    (`authz.visible_node_ref_clause`). A non-int id or a vanished row is dropped."""
    # `is_operator`, not the bare flag: a superadmin inside a tenant org is scoped to that org.
    if principal is None or principal.is_operator:
        return True
    if not isinstance(object_id, int) or isinstance(object_id, bool):
        log.warning("cockpit-stream: dropping command event with a non-int id: %r", object_id)
        return False
    with SessionLocal() as session:
        return session.scalar(
            select(literal(1))
            .select_from(CommandAudit)
            .where(CommandAudit.id == object_id)
            # The enclosing `select_from` is required: without `CommandAudit` in the FROM the correlated
            # EXISTS cross-joins and reads TRUE whenever the subject holds any node grant.
            .where(visible_node_ref_clause(principal, CommandAudit.node_id))
        ) is not None


def _is_collection_sentinel(entity: str, object_id: Any) -> bool:
    """Is this the `id=0` collection sentinel ("refetch this collection") rather than an event naming a row?

    Restricted to types whose id domain cannot hold a 0: unfiltered entities and uuid-keyed types. For an
    int-keyed filtered type `0` falls through to the row probe. `bool` is excluded (`False == 0`)."""
    if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id != 0:
        return False
    spec = OBJECT_SPEC_BY_NAME.get(entity)
    return spec is None or spec.id_type is not int


def _may_see_collection_sentinel(principal: Principal | None, event_org: Any) -> bool:
    """May this subscriber be told that SOME row of a collection changed — the sentinel's question.

    Scoped by the org it happened in: a fleet-wide wake would be a covert readout of other orgs' activity.
    Served to subscribers of the tagged org (`events.emit_console_event(org_id=...)`),
    and the operator plane (`is_operator`) sees every org's. An untagged sentinel reaches the operator
    plane only: fail closed."""
    if principal is None or principal.is_operator:
        return True
    if event_org is None or principal.org_id is None:
        return False
    return str(principal.org_id) == str(event_org)


def _may_see(
    principal: Principal | None, entity: str, object_id: Any, event_org: Any = None
) -> bool:
    """May this subscriber see the object this event names?

    `can_access` is the only sanctioned single-object probe: `visible_task_clause` et al. outside an
    enclosing `select` compile to a cross join (TRUE for any grant), and it carries the `hidden`/`unsaved`
    row gates.

    The session is opened per check, never inherited: the handler closes the request-scoped session
    before this generator runs, and reusing a closed Session would hold a new pooled connection
    `idle in transaction` for the tab's life, exhausting the pool.

    A READ probe: it must admit exactly who the list and detail routes admit; WRITE would silently drop
    updates for scope readers. It filters only the `cockpit_events` id feed; console access is gated
    separately (`routes/console.py`).

    `None` (agent/infrastructure) and the operator plane are allow-all, so the common path costs no query."""
    if entity == COMMAND_ENTITY:
        # Before the unfiltered fall-through: `command` is not an `OBJECT_FILTERED_ENTITIES` member.
        return _may_see_command(principal, object_id)
    if entity == TOKEN_WALLET_ENTITY:
        # The org's own members and the operator: the id IS the org (fail closed on any other shape).
        if principal is None or principal.is_operator:
            return True
        return principal.org_id is not None and str(principal.org_id) == str(object_id)
    if entity == ORG_ENTITY:
        # The flat operator arm: the org routes are operator-only, so the answer needs no probe.
        return principal is None or principal.is_operator
    if entity in (CRON_ENTITY, NODE_GROUP_ENTITY, PORT_GRID_VAR_ENTITY, NODE_ORDER_ENTITY, LIVE_CAST_ENTITY):
        # Tenant-admin `Tenancy.CALLER_PLANE` routes: the operator sees every org's rows, an Org Owner/Admin
        # (`Principal.is_org_admin`) their own org's. The emit tags the row's org; an untagged event reaches
        # the operator only (fail closed).
        if principal is None or principal.is_operator:
            return True
        return principal.is_org_admin and _may_see_collection_sentinel(principal, event_org)
    if _is_collection_sentinel(entity, object_id):
        # Before the unfiltered fall-through, so `worktree`/`runner`/`session` sentinels are org-scoped.
        return _may_see_collection_sentinel(principal, event_org)
    if entity not in OBJECT_FILTERED_ENTITIES:
        return True
    if entity == NODE_SPEC.name and principal is not None and not principal.is_operator:
        # A Multi-Node Host and another tenant's vnode never reach a tenant's stream, not even a tenant
        # superadmin's; `vnodes.withheld_from` is False for every ordinary node. The community edition has neither.
        if edition_features.enabled("multi_node_hosts"):
            from app import vnodes

            if vnodes.withheld_from(object_id, principal.org_id):
                return False
    # `is_operator`, not the bare flag: a superadmin inside a tenant org takes the org-bound probe.
    if principal is None or principal.is_operator:
        return True
    spec = OBJECT_SPEC_BY_NAME[entity]
    # The `id=0` sentinel never reaches here for uuid-keyed types (`_is_collection_sentinel`).
    # Payload hygiene first, for every filtered type: an id the relay could not yield is dropped whoever
    # is asking, since an escaping `json.dumps` error would kill the stream.
    probe_id = _probe_id(spec, object_id)
    if probe_id is None:
        log.warning("cockpit-stream: dropping %s event with an id of no known domain: %r",
                    entity, object_id)
        return False
    if spec.unscoped_read_key is not None:
        # The collection read is not grant-scoped yet, so answer on that read's key
        # (`ObjectSpec.unscoped_read_key`).
        return principal.has(spec.unscoped_read_key)
    with SessionLocal() as session:
        return can_access(session, principal, entity, probe_id, mode=ObjectAccess.READ)


@router.get("/stream")
@always(Tenancy.ORG_SCOPED)
async def browser_stream(
    request: Request,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Long-lived SSE stream of the cockpit_events notifications THIS subscriber may see.

    Filtering is the per-subscriber object check only (module docstring); `stack` and `org_id` are
    stripped at the yield. The principal is captured in the handler body and the generator never touches
    the request-scoped session (see `_may_see`).

    `session` is `require_user`'s own (dependencies are cached per request), closed HERE: since FastAPI
    0.118 a `yield` dependency tears down only once the response has finished, which for this route is
    the tab's life. Left open, its `admin_users` read held one pooled connection `idle in transaction`
    per tab, and ~40 tabs 500'd every route on `QueuePool limit … reached`
    (`test_an_open_stream_returns_its_request_session_connection_to_the_pool`).

    The stream is THIS browser session's — `(identity_id, iat)` of the cookie it opened with — and closes on
    that session's `session_switched` (`app/events.py`, an org switch): its reconnect then carries the old
    org's `?org=` and is refused `org_switched` (`app/stale_tab_guard.py`). Another device's session of the
    same person (another `iat`) keeps its stream."""
    if api_keys.of(request) is not None:
        # REST only: a stream is a browser tab's, and a key is never taught one (`app/api_keys.py`).
        raise HTTPException(status_code=401, detail=api_keys.INVALID_DETAIL)
    claims = read_session(request.cookies.get(COOKIE_NAME))
    # Read before the close below detaches the row.
    identity_id = _user.identity_id if _user is not None else None
    handshake = (str(identity_id), claims.iat) if claims is not None and identity_id is not None else None
    # The cookie this stream opened with is the one it holds for its life, so its HIPAA idle window (C5) is
    # re-read at every keepalive below: an idle tab's stream ends and its reconnect is refused. An active
    # tab's stream ends too, at most every 15 minutes, and reconnects on the cookie its requests refreshed.
    hipaa_issued_at = (
        claims.issued_at
        if claims is not None and _user is not None
        and await run_in_threadpool(hipaa.org_is_on, session, _user.org_id)
        else None
    )
    # The org's IP allowlist, re-read at every keepalive and subject refresh (`app/ip_allowlist.py`): a list
    # saved without this address ends the stream, and its reconnect is refused `ip_not_allowed` by the
    # middleware. The cookie's own row, as the middleware judges it (an operator acting as a member stays exempt).
    admin_id = claims.admin_id if claims is not None else None
    host = request.client.host if request.client else None
    session.close()
    queue = broadcaster.subscribe()

    async def _bind_subject_scope(subject: Principal | None) -> bool:
        """Put THIS subscriber's org in force for the probes below — the tenancy bind this route's
        `Tenancy.ORG_SCOPED` declaration promises, made in the only place it can be made.

        Not in the guard (it answers `Access.ALWAYS` without resolving a principal; this route
        authorizes per event) and not in the handler body (`seam.bind` writes a `ContextVar`, and the
        subject is re-bound mid-stream). `async def` with nothing to await so it can never
        run on a worker thread, where the bind would die with the thread
        (`test_tenancy_scope_sites.py`); threadpool probes inherit the context.

        A subject with no `org_id` binds nothing (unbound renders `false()`: fail closed). Returns
        whether the stream may continue: `seam` has no unbind, so a subject whose org was cleared
        mid-connection would keep the previous org in force; the caller closes and the tab reconnects."""
        bound = seam.current_scope()
        if subject is None or subject.org_id is None:
            # `bound is None`, not `not isinstance(bound, UUID)`: `seam.OPERATOR` is a non-UUID sentinel
            # wider than any org, and anything bound is stale for a subject with no org.
            return bound is None
        seam.bind(subject.org_id)
        return True

    async def _address_refused() -> bool:
        """Whether the org's IP allowlist now leaves out the address this stream opened from."""
        if admin_id is None:
            return False
        return await run_in_threadpool(ip_allowlist.session_refusal, admin_id, host) is not None

    async def _events() -> AsyncIterator[bytes]:
        subject = principal
        resolved_at = time.monotonic()
        # A deadline, not a per-wait timeout: a dropped event restarting the timeout would let the
        # keepalive rhythm reveal hidden transitions. Only a byte actually sent moves the deadline.
        next_keepalive = time.monotonic() + STREAM_KEEPALIVE_SECONDS
        try:
            # Honoured at open too, though unreachable in production today (the guard binds nothing);
            # `test_closing_at_OPEN_on_a_foreign_scope_still_unsubscribes_from_the_broadcaster` drives it.
            # Inside the `try` so `finally` unsubscribes the queue; otherwise it would leak in
            # `EventBroadcaster._subscribers` for the life of the process.
            if not await _bind_subject_scope(subject):
                return
            yield f"retry: {STREAM_RETRY_MS}\n\n".encode()
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=max(0.0, next_keepalive - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    if hipaa_issued_at is not None and hipaa.idled_out(auth._now() - hipaa_issued_at):
                        return
                    if await _address_refused():
                        return
                    next_keepalive = time.monotonic() + STREAM_KEEPALIVE_SECONDS
                    yield b": keepalive\n\n"   # SSE comment — holds the connection, carries no event
                    continue
                entity = event.get("entity")
                if entity == SESSION_SWITCHED:
                    # Never relayed: it names a person and a session, not an object.
                    if handshake is not None and (event.get("identity_id"), event.get("iat")) == handshake:
                        return
                    continue
                now = time.monotonic()
                if now - resolved_at >= SUBJECT_REFRESH_SECONDS:
                    # On an event of any entity, so a revoked session is noticed on any stream; still
                    # capped at one query per interval.
                    try:
                        subject = await run_in_threadpool(_resolve_subject, request)
                    except RevokedSession:
                        # Logged out, deleted, or password-rotated mid-connection. Close the stream:
                        # the tab's EventSource reconnects and gets a clean 401.
                        return
                    except Exception as exc:   # pragma: no cover — a DB blip, not a code path
                        log.warning("cockpit-stream: subject re-resolve failed (%s) — keeping the "
                                    "previous subject set, retrying in %ss",
                                    exc, SUBJECT_REFRESH_RETRY_SECONDS)
                        resolved_at = now - SUBJECT_REFRESH_SECONDS + SUBJECT_REFRESH_RETRY_SECONDS
                    else:
                        resolved_at = now
                        # A busy stream never reaches the keepalive: the allowlist is re-read here too.
                        if await _address_refused():
                            return
                        # Re-bound on every refresh: the subject can change org mid-stream. `False`
                        # means it lost its org while one is bound, which `seam` cannot undo: close.
                        if not await _bind_subject_scope(subject):
                            log.warning(
                                "cockpit-stream: subject lost its org mid-stream — closing so the "
                                "reconnect re-resolves rather than reading the previous org"
                            )
                            return
                if entity in PROBED_ENTITIES or _is_collection_sentinel(entity, event.get("id")):
                    # Any entity's sentinel is checked (`_may_see_collection_sentinel`). Off the loop
                    # via starlette's threadpool, sharing the anyio limiter every sync handler uses;
                    # `asyncio.to_thread` would add a second, independently sized pool of DB consumers.
                    try:
                        visible = await run_in_threadpool(
                            _may_see, subject, entity, event.get("id"), event.get("org_id")
                        )
                    except Exception as exc:   # pragma: no cover — a DB blip, not a code path
                        # Fail closed. A dropped wake is covered by the poll/sweep backstop
                        # (app/events.py); a wrongly-relayed one is an id leak.
                        log.warning("cockpit-stream: access check failed (%s) — dropping the event", exc)
                        visible = False
                    if not visible:
                        continue
                next_keepalive = time.monotonic() + STREAM_KEEPALIVE_SECONDS
                # `org_id` and `stack` stay server-side: a tenant must not learn another org, and the
                # stack is the operator's host layout (`app/events.py::_STACK`). A copy, not a `pop`:
                # `EventBroadcaster.publish` hands the same dict to every subscriber.
                wire = {k: v for k, v in event.items() if k not in ("org_id", "stack")}
                yield f"data: {json.dumps(wire)}\n\n".encode()
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Defense in depth alongside nginx's `proxy_buffering off`.
            "X-Accel-Buffering": "no",
        },
    )
