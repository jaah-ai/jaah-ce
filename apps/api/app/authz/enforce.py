"""Enforcing one route's declaration: `_enforce`, its denials, object params and the read-scope binding."""
from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from app import act_as, api_keys
from app.auth import (
    NODE_AUTH_UNAVAILABLE_DETAIL,
    SESSION_IDLE_TIMEOUT_DETAIL,
    NodeAuthUnavailable,
    _valid_node_token,
    hidden_route,
)
from app.db import SessionLocal
from app.models.orgs import Org
from app.tenancy import seam
from app.authz.catalog import AUTHOR_DRAFT_KEY, AUTHOR_DRAFT_PARAM, Access, Tenancy, permission_key
from app.authz.declarations import Declaration, declaration_of
from app.authz.principal import Principal, RevokedSession
from app.authz.visibility import (
    ACCOUNT_SPEC, CREDENTIAL_SPEC, OBJECT_SPEC_BY_NAME, ObjectSpec, PROJECT_SPEC, TASK_SPEC,
)
from app.authz.object_access import (
    _refused_write_is_readable,
    author_draft_admits,
    authored_draft,
    can_access,
    key_scope_satisfied,
    object_access_for,
    readable_write_denial,
)
from app.authz import principal as principal_module

log = logging.getLogger(__name__)


# ── The guard ─────────────────────────────────────────────────────────────────────────────────────


# WebSocket denial close codes. An `HTTPException` on a socket yields an opaque handshake failure the
# client retries; only `WebSocketException` produces a readable code. Values shared with
# `app/routes/console.py` and the frontend; `app/routes/node_bus.py` uses 4401 only.
_WS_UNAUTHORIZED = 4401
_WS_NOT_FOUND = 4404

_WS_CODE_FOR_STATUS = {401: _WS_UNAUTHORIZED, 403: _WS_UNAUTHORIZED, 404: _WS_NOT_FOUND}


class HintedDenial(HTTPException):
    """A denial carrying the access-request hint (`app/access_requests.py::request_hint`): rendered as
    `{detail, request: {scope, role}}` by `app/main.py`'s handler. Only ever raised with a hint — a denial
    without one stays a plain `HTTPException`, so its body is byte-identical to before."""

    def __init__(self, status_code: int, detail: str, request: dict) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.request = request


def _deny(conn: HTTPConnection, status: int, detail: str, *, request: dict | None = None) -> HTTPException:
    """The denial `_enforce` raises — always an `HTTPException`. `_enforce` runs in a worker thread and a
    socket close takes two awaits, so `authz_guard` converts it (`_deny_websocket`). `request` is the
    access-request hint, when the refusal earned one (`_request_hint`)."""
    if request is not None:
        return HintedDenial(status, detail, request)
    return HTTPException(status_code=status, detail=detail)


async def _deny_websocket(conn: HTTPConnection, status: int, detail: str) -> WebSocketException:
    """Turn a denial into a close code the client can actually read, and return it to be raised.

    Accept first: raising before accept is a rejected handshake (browser sees 1006, which `ConsolePane.tsx`
    retries forever); after accept the client gets 4401. No handler runs on this path, so accepting grants
    nothing. Codes are reused, never invented (an unknown code is retryable); an unmapped status falls back
    to unauthorized."""
    # `conn` is the live `WebSocket` here; cast, never reconstruct a second connection object.
    try:
        await cast(WebSocket, conn).accept()
    except Exception:  # noqa: BLE001 - see below; the denial must survive a dead peer
        # Best-effort: an exception here would replace the `WebSocketException` about to be raised.
        log.warning("authz: could not accept %s to deliver a close code", conn.scope.get("path"))
    return WebSocketException(code=_WS_CODE_FOR_STATUS.get(status, _WS_UNAUTHORIZED), reason=detail)


class OtherOrgDenial(HTTPException):
    """An operator's mutation of a by-id row that lives in ANOTHER org: rendered as
    `{detail, reason: "other_org", org}` by `app/main.py`'s handler. The operator reads every org's rows
    and changes another org's by acting as it (`X-Jaah-Act-As`); an unacting mutation
    binds their own org (R5.7), so the honest answer is "act as that org", never the 404 the own-org bind would
    otherwise produce. Raised by `_refuse_other_org_mutation` alone. The console sends every such write acting
    as the row's org for that one request (`apps/web/src/lib/actAs.ts::actAsOnce`), so this reaches raw API
    callers."""

    reason = "other_org"

    def __init__(self, org: str) -> None:
        super().__init__(
            status_code=403,
            detail=f"This belongs to {org}. Act as {org} to change it.",
        )
        self.org = org


def _forbidden(conn: HTTPConnection) -> Exception:
    return _deny(conn, 403, "forbidden")


def _not_found(conn: HTTPConnection, *, request: dict | None = None) -> Exception:
    return _deny(conn, 404, "not found", request=request)


def _decides_without_io(declaration: Declaration) -> bool:
    """Is this declaration answerable from the declaration alone — no principal, no session, no DB?

    Identity, never `==` (`Access` mixes in `str`). Decided before resolving the principal, or a stale
    cookie would 401 `POST /api/auth/login`; so such routes must disclose nothing about any user. Hoisted
    so `authz_guard` answers these on the event loop: otherwise every request, health probes included,
    needs a worker-pool thread and queues behind a saturated DB pool."""
    return declaration.access is Access.INFRA or declaration.access is Access.ALWAYS


#: The methods that only READ. The operator's fleet-wide read plane and the org filter apply to these alone.
_READ_METHODS = frozenset({"GET", "HEAD"})


def _is_read(conn: HTTPConnection) -> bool:
    """Is this an HTTP read (GET/HEAD)? A WebSocket scope has no method, so it is not."""
    return conn.scope.get("type") == "http" and conn.scope.get("method", "") in _READ_METHODS


#: The by-id path params an operator's mutation is checked against for another org's row, and the type each
#: names (`_refuse_other_org_mutation`). The types an operator sees fleet-wide and edits from a page: a detail page,
#: or Brains' enable/pause toggle (an account), whose refusal must name the org, not "unknown account <id>".
_OTHER_ORG_PARAMS: tuple[tuple[str, ObjectSpec], ...] = (
    ("project_id", PROJECT_SPEC),
    ("task_id", TASK_SPEC),
    ("credential_id", CREDENTIAL_SPEC),
    ("account_id", ACCOUNT_SPEC),
)


def _refuse_other_org_mutation(
    conn: HTTPConnection, declaration: Declaration, principal: Principal
) -> None:
    """Refuse an operator's mutation of another org's row with `OtherOrgDenial`, before the handler runs.

    An operator's GET of an `ORG_SCOPED` route binds the fleet (`_principal_scope`), but a mutation binds
    their own org, so another org's row would 404 inside the handler: a row the console just showed them,
    refused as if it did not exist. Probed fleet-wide for the row's org only; a row in the operator's own
    org, a row whose org names no `orgs` row, or no such row, falls through to the ordinary decision. Operators only, so a
    non-operator still gets the 404 that discloses nothing (no existence oracle). `OPERATOR` and
    `CALLER_PLANE` routes (the transfers among them) already write fleet-wide and are not touched."""
    if not (
        principal.is_operator
        and declaration.tenancy is Tenancy.ORG_SCOPED
        and conn.scope.get("type") == "http"
        and not _is_read(conn)
    ):
        return
    probes: list[tuple[ObjectSpec, Any]] = []
    for param, spec in _OTHER_ORG_PARAMS:
        raw = conn.path_params.get(param)
        if raw is None:
            continue
        try:
            probes.append((spec, spec.id_type(raw)))  # type: ignore[arg-type]
        except (TypeError, ValueError, AttributeError):
            # Not an id: the ordinary decision answers it.
            continue
    if not probes:
        return
    with seam.operator_scope(), SessionLocal() as session:
        for spec, object_id in probes:
            org_id = session.scalar(select(spec.entity.org_id).where(spec.entity.id == object_id))
            if org_id is None or org_id == principal.org_id:
                continue
            name = session.scalar(select(Org.name).where(Org.id == org_id))
            if name is None:
                # A dangling `org_id` (no FK onto `orgs`) names no org to send the operator to: such a row
                # stays unreachable, the 404 below.
                continue
            log.info(
                "authz: operator %s refused %s %s — %s %s is in org %s",
                principal.user_id,
                conn.scope.get("method", ""),
                conn.scope.get("path"),
                spec.name,
                object_id,
                org_id,
            )
            raise OtherOrgDenial(name)


def _principal_scope(
    conn: HTTPConnection, declaration: Declaration, principal: Principal
) -> seam.Scope:
    """The scope a resolved principal binds: `seam.OPERATOR` for an operator on a `CALLER_PLANE` route,
    and for an operator's READ of an `ORG_SCOPED` route ("Operator can see everything", R5.5:
    the plane the org filter narrows); the principal's own org everywhere else (`OPERATOR` routes are
    widened by `_bind_read_scope`). A mutation stays on the operator's own org, so no create or write
    path changes plane (R5.7)."""
    if not principal.is_operator:
        return principal.org_id
    if declaration.tenancy is Tenancy.CALLER_PLANE:
        return seam.OPERATOR
    if declaration.tenancy is Tenancy.ORG_SCOPED and _is_read(conn):
        return seam.OPERATOR
    return principal.org_id


def _audit_key_refusal(conn: HTTPConnection, principal: Principal, detail: str) -> None:
    """An API key refused for a permission it does not carry: one throttled `api_key_refused` row (`app/api_keys.py`)."""
    if principal.api_key_id is not None:
        api_keys.refuse_scope(conn, principal, detail)


def _enforce(conn: HTTPConnection, declaration: Declaration | None = None) -> seam.Scope:
    """The guard's whole decision, as a blocking call. `authz_guard` is the async wrapper.

    Returns the scope to bind: the principal's org, `seam.OPERATOR` for the cookie-less agent caller an
    `agent_allowed` declaration admits, or `None` for no-I/O classes. It must not bind itself: it runs in a
    worker thread on a copied context, so a bind would silently vanish (`app/tenancy/seam.py::bind`;
    pinned by the scope-sites census test). `declaration` is passed in by `authz_guard` so there is one
    resolution; optional for tests."""
    if declaration is None:
        declaration = declaration_of(conn.scope.get("endpoint"))
    if declaration is None:
        # Fail closed: an undeclared route is denied.
        log.warning("authz: denying undeclared route %s", conn.scope.get("path"))
        raise _forbidden(conn)

    if _decides_without_io(declaration):
        # No principal resolved; INFRA binds at `routes/agent/auth.py::_check_token`, ALWAYS has no org.
        return None

    try:
        principal = principal_module.current_principal(conn)
    except RevokedSession as revoked:
        # A cookie was presented and did not verify; not the agent principal. The HIPAA idle refusal keeps
        # its own detail, which the console answers with its timed-out sign-in page.
        detail = SESSION_IDLE_TIMEOUT_DETAIL if revoked.detail == SESSION_IDLE_TIMEOUT_DETAIL else "unauthorized"
        raise _deny(conn, 401, detail) from revoked

    if principal is None:
        # No cookie: the agent principal, reaching only `agent_allowed` declarations here.
        if declaration.agent_allowed:
            # Verify the token here: WebSocket scopes skip `SessionAuthMiddleware`, and public paths pass
            # it unauthenticated (`app.auth._valid_node_token`).
            try:
                agent_ok = _valid_node_token(conn)  # type: ignore[arg-type]
            except NodeAuthUnavailable as unavailable:
                raise _deny(conn, 503, NODE_AUTH_UNAVAILABLE_DETAIL) from unavailable
            if agent_ok:
                # The agent principal reads on the fleet plane: the clauses already read `None` as
                # allow-all, so the seam must agree. The one route reaching here is `routes/portfolio.py`'s
                # projects collection (jaahd's clone-path resolver; unbound it answered `200 []` —
                # `tests/test_authz_project_scoping.py`). It is not narrowed per arm (`_read_scope_for`):
                # `_valid_node_token` returns only a bool, not the resolved caller.
                return seam.OPERATOR
            log.warning(
                "authz: denying cookie-less caller on agent-allowed route %s — no valid agent token",
                conn.scope.get("path"),
            )
        raise _deny(conn, 401, "unauthorized")

    _refuse_other_org_mutation(conn, declaration, principal)

    if declaration.access is Access.SUPERADMIN:
        # No feature key reaches this branch. On the fleet plane the flag alone is not enough: a tenant
        # superadmin would read every org's rows (`Principal.is_operator`).
        # Refused as a path the router does not serve (`hidden_route`): a superadmin door is not advertised.
        if not principal.is_superadmin:
            raise hidden_route()
        if declaration.tenancy is Tenancy.OPERATOR and not principal.is_operator:
            raise hidden_route()
        return _principal_scope(conn, declaration, principal)

    if declaration.access is Access.ORG_ADMIN:
        # An operator, or an Org Owner/Admin — whose org is the one `_principal_scope` binds.
        if not principal.is_org_admin:
            _audit_key_refusal(conn, principal, "org_admin")
            raise _forbidden(conn)
        return _principal_scope(conn, declaration, principal)

    if declaration.access is Access.FEATURE:
        key = permission_key(declaration.feature or "", declaration.action or "")
        if not principal.has(key) and principal.api_key_id is not None:
            # An API key never takes a fallback: it carries the declared key or it is refused (R4).
            _audit_key_refusal(conn, principal, key)
            raise _forbidden(conn)
        if not principal.has(key):
            # Fallbacks, consulted only after the declared key was refused. 403, not 404: the caller
            # holds neither key, so no existence oracle. Own-credential first (cheap, no query; it
            # admits every session and the handler's `scoped_credential_select_for` narrows), then
            # author-draft with its DB probe. Mutually exclusive in practice (`declare`).
            if declaration.own_credential:
                # Logged only for a mutation: reads are the steady state (every `/profile` poll). A socket
                # scope has no method, so `""` falls to the mutation side.
                if conn.scope.get("method", "") not in ("GET", "HEAD"):
                    log.info(
                        "authz: %s admitted on %s %s under the own-credential fallback",
                        principal.user_id,
                        conn.scope.get("method", ""),
                        conn.scope.get("path"),
                    )
            elif not (
                declaration.author_draft
                and principal.has(AUTHOR_DRAFT_KEY)
                and _authored_draft_subject(conn, declaration, principal)
            ):
                raise _forbidden(conn)
            else:
                # Admission logged: this is the guard's one novel path, and `record_write` never records
                # that a `tasks:create` principal reached a write route. Unconditional (this arm has no
                # reads, unlike own-credential).
                log.info(
                    "authz: %s admitted on %s under the author-draft fallback",
                    principal.user_id,
                    conn.scope.get("path"),
                )
        # Object scoping runs either way: the fallback chose the key, never the rows. Both candidate
        # keys are passed so the widening is monotonic — holding `tasks:edit` elsewhere must not cost a
        # caller their own draft here.
        if declaration.object_params:
            _enforce_object_params(
                conn,
                declaration,
                principal,
                key=key if principal.has(key) else None,
                author_draft_key_held=(
                    declaration.author_draft and principal.has(AUTHOR_DRAFT_KEY)
                ),
            )
        return _principal_scope(conn, declaration, principal)

    if declaration.access is Access.FEATURE_ANY:
        # Any declared key admits (`has` covers superadmin). No object scoping: `declare` rejects it on
        # non-FEATURE access. An empty key set denies.
        if not any(principal.has(key) for key in declaration.keys):
            _audit_key_refusal(conn, principal, " | ".join(declaration.keys))
            raise _forbidden(conn)
        return _principal_scope(conn, declaration, principal)

    # Explicit terminal: an unhandled `Access` member must deny, never fall through.
    log.error("authz: denying unhandled access kind %r", declaration.access)
    raise _forbidden(conn)


def enforce_in_process(conn: HTTPConnection, endpoint: Any) -> None:
    """The guard's decision for `endpoint`'s OWN declaration, applied to this connection — for a route that
    composes other routes' reads in-process (`routes/task_page.py`), so each composed read is admitted
    exactly when its own GET would be. Raises the refusal that GET would answer. Blocking, like `_enforce`.

    Fails closed twice: an undeclared `endpoint` is refused (never judged by the composing route's own
    declaration, which `_enforce` falls back to), and so is a decision resolving another scope than the one
    the composing route's guard bound — the composed read would otherwise run on a plane its route never
    binds."""
    declaration = declaration_of(endpoint)
    if declaration is None:
        log.error("authz: refusing the in-process call to undeclared %r", getattr(endpoint, "__name__", endpoint))
        raise _forbidden(conn)
    resolved = _enforce(conn, declaration)
    if resolved != seam.current_scope():
        log.error(
            "authz: refusing the in-process call to %s on %s — it resolves %s, the request is bound to %r",
            endpoint.__name__, conn.scope.get("path"), _resolved_label(resolved), seam.current_scope(),
        )
        raise _forbidden(conn)


def _authored_draft_subject(
    conn: HTTPConnection, declaration: Declaration, principal: Principal
) -> bool:
    """Does this request's `AUTHOR_DRAFT_PARAM` name a still-`draft` task `principal` authored?

    Only that one param: `remove_task_dependency` addresses two tasks, and widening to the second would let
    an author detach their draft from someone else's task. Probes under `seam.bound_to(principal.org_id)`,
    the scope the guard is about to bind: this runs before `_bind_read_scope`, and unbound the enforcing
    seam reads nothing, silently disabling the fallback. A region, not `bind()`, since `_enforce` runs in
    a worker thread."""
    spec = OBJECT_SPEC_BY_NAME.get(declaration.object_type or "")
    if spec is None:
        # Unreachable through `declare`; a hand-built `Declaration` gets no widening.
        log.error(
            "authz: not widening %s — unknown object_type %r",
            conn.scope.get("path"),
            declaration.object_type,
        )
        return False
    task_id = _author_draft_id(conn, spec)
    if task_id is None:
        return False
    with seam.bound_to(principal.org_id), SessionLocal() as session:
        return authored_draft(session, principal, task_id)


def _author_draft_id(conn: HTTPConnection, spec: ObjectSpec) -> Any | None:
    """This request's `AUTHOR_DRAFT_PARAM`, parsed in the spec's own domain — or `None` when it will not
    parse.

    Split out so `_enforce_object_params` can ask on its existing session (a nested connection per request
    risks pool exhaustion). Parses the raw path segment (dependencies run before coercion); any divergence
    fails closed."""
    try:
        return spec.id_type(conn.path_params.get(AUTHOR_DRAFT_PARAM))  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError):
        return None


def _request_hint(
    session: Session,
    principal: Principal,
    declaration: Declaration,
    object_type: str,
    object_id: int | UUID,
    key: str | None,
) -> dict | None:
    """The access-request hint a refusal of this object carries — `{scope, role}` for a project or
    account of the caller's own org, else None (`app/access_requests.py::request_hint`, the
    one home of the rule). `key` is the key refused: the admitted one, else the declared one. Never
    raises: a hint that cannot be computed must not turn a refusal into a 500."""
    from app import access_requests

    refused = key or permission_key(declaration.feature or "", declaration.action or "")
    try:
        return access_requests.request_hint(
            session, principal, OBJECT_SPEC_BY_NAME.get(object_type), object_id, refused
        )
    except Exception:  # noqa: BLE001 - the refusal is what matters; the hint is a courtesy
        log.exception("authz: could not compute the access-request hint for %s %s", object_type, object_id)
        return None


def _enforce_object_params(
    conn: HTTPConnection,
    declaration: Declaration,
    principal: Principal,
    *,
    key: str | None,
    author_draft_key_held: bool,
) -> None:
    """Enforce every path param the declaration names, in the guard, before the handler runs.

    All of them: a route addressing two objects is authorized for two. `key` is the declared key, or `None`
    when the caller does not hold it (admitted by the author-draft fallback); `author_draft_key_held` says
    the other candidate is available. The mode comes from the declaration's action, never the handler; all
    params share it."""
    object_type = declaration.object_type or ""
    spec = OBJECT_SPEC_BY_NAME.get(object_type)
    if spec is None:
        # Fail closed on an unknown type (reachable only via a hand-built `Declaration`), not a 500.
        log.error("authz: denying %s — unknown object_type %r", conn.scope.get("path"), object_type)
        raise _not_found(conn)
    mode = object_access_for(declaration)
    # `key` is the key the caller was admitted under (declared, or `AUTHOR_DRAFT_KEY`); always a catalog
    # key. The probe reads tenant rows before `_bind_read_scope`, so it runs under the org the guard is
    # about to bind — unbound, the enforcing seam reads nothing and every object route 404s. A region,
    # not `seam.bind`, since this runs in a worker thread. `read_key` is the route's view key, so the
    # readable exception's "may read" means what the caller's GET means.
    read_key = permission_key(declaration.feature, "view") if declaration.feature else None
    # An operator's read binds the fleet plane (`_principal_scope`), so its probe reads there too: a row
    # in another org is the operator's to see, never a 404.
    if principal is not None and _principal_scope(conn, declaration, principal) is seam.OPERATOR:
        plane = seam.operator_scope()
    else:
        plane = seam.bound_to(principal.org_id if principal is not None else None)
    with plane, SessionLocal() as session:
        for param in declaration.object_params:
            raw = conn.path_params.get(param)
            try:
                # The spec's own domain (uuid or int), never `int()` on both.
                object_id = spec.id_type(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError, AttributeError):
                # Not an id → cannot name a reachable object. `UUID` raises `AttributeError` for non-str.
                raise _not_found(conn)
            # The guard parses the raw segment (dependencies run before coercion); every divergence from
            # pydantic fails closed. 404, unless a write on a readable row, which is a 403 with
            # `readable_write_denial` (as `require_object_access`).
            if not can_access(session, principal, object_type, object_id, mode=mode):
                # The author's own draft: the one row the write clause refuses that the guard admits,
                # judged on this param's own row
                # (`test_the_dependency_delete_still_enforces_its_second_object_param`).
                if author_draft_key_held and author_draft_admits(
                    session, principal, spec, object_id, mode=mode
                ):
                    continue
                hint = _request_hint(session, principal, declaration, object_type, object_id, key)
                if _refused_write_is_readable(
                    session, principal, object_type, object_id, mode, read_key=read_key
                ):
                    raise _deny(conn, 403, readable_write_denial(object_type), request=hint)
                raise _not_found(conn, request=hint)
            # For a key-scoped type, the admitting key must be held on this row's own scope
            # (`key_scope_column`/`key_scope_type`), not merely somewhere. Denial as its neighbour.
            if key is not None and key_scope_satisfied(
                session, principal, spec, object_id, key=key, mode=mode
            ):
                continue
            # The author-draft key, tried last: widens the key alone; `can_access` already
            # passed. Both params of a two-object route are widened together, bound by `authored_draft`.
            draft_id = _author_draft_id(conn, spec) if author_draft_key_held else None
            if (
                draft_id is not None
                and key_scope_satisfied(
                    session, principal, spec, object_id, key=AUTHOR_DRAFT_KEY, mode=mode
                )
                # This session, not a nested one (`_author_draft_id`).
                and authored_draft(session, principal, draft_id)
            ):
                continue
            # The key half's denial: 403 naming the key if the row is readable (the admitted key, else the
            # declared one), else 404.
            hint = _request_hint(session, principal, declaration, object_type, object_id, key)
            if _refused_write_is_readable(
                session, principal, object_type, object_id, mode, read_key=read_key
            ):
                raise _deny(
                    conn,
                    403,
                    readable_write_denial(
                        object_type,
                        key=key
                        or permission_key(declaration.feature or "", declaration.action or ""),
                    ),
                    request=hint,
                )
            raise _not_found(conn, request=hint)


def _resolved_label(resolved: seam.Scope) -> str:
    """What the decision resolved, in words, for the refusals below — one spelling for four messages, so
    `seam.OPERATOR` never renders as "org OPERATOR"."""
    if resolved is seam.OPERATOR:
        return "the operator plane (the cookie-less agent caller)"
    if resolved is None:
        return "NO org"
    if isinstance(resolved, UUID):
        return f"org {resolved}"
    # Unreachable through `_enforce`; named so a new scope kind reads as unhandled.
    return f"the unrecognized scope {resolved!r}"


def _tenancy_disagreement(
    declaration: Declaration, resolved: seam.Scope, scope: seam.Scope
) -> str | None:
    """The route's declared tenancy plane against the one actually in force — the disagreement the guard
    refuses, or `None` when the two agree.

    A string, so the log names the wrong declaration. Checked against the scope about to be bound
    (`resolved`, a `seam.Scope`) and whatever is already bound (`scope`); it never changes a bind. Runs only
    on the principal-resolving classes; `INFRA`/`ALWAYS` terms are enforced at import and by the census.

    Two contradictions it cannot see, both live now that the seam enforces: `routes/agent/auth.py::_read_scope_for`
    binds `seam.OPERATOR` for `ARM_CP_SELF`/`ARM_INSECURE`, widening any `ORG_SCOPED` route reached on them;
    and `ALWAYS` routes that bind later (`GET /api/stream`, node WebSockets) fail closed when unbound. The
    `BOOTSTRAP` arm is defence in depth against a forged `Declaration`."""
    if declaration.tenancy is Tenancy.BOOTSTRAP:
        # Reached only after a principal resolved, so the term is false of this route.
        if resolved is not None:
            return (
                f"declared {Tenancy.BOOTSTRAP.value} but the decision resolved "
                f"{_resolved_label(resolved)}: a route that runs before anything can be bound "
                "cannot be one that resolves a principal"
            )
        if scope is not None:
            return (
                f"declared {Tenancy.BOOTSTRAP.value} but scope {scope!r} is already in force: its "
                "handler reads inside identity_scope(), which a bound scope would suspend the wrong way"
            )
        return None
    if declaration.tenancy is Tenancy.OPERATOR or (
        declaration.tenancy is Tenancy.CALLER_PLANE and resolved is seam.OPERATOR
    ):
        # Fleet-wide by design: a per-org scope already in force would silently narrow it. This runs
        # before the bind; `_bind_read_scope` binds `seam.OPERATOR` for these routes.
        if isinstance(scope, UUID):
            return (
                f"declared {Tenancy.OPERATOR.value} (fleet-wide) but org {scope} is already in force, "
                "which would narrow a route whose subject is the fleet to a single tenant"
            )
        if scope is not None and scope is not seam.OPERATOR:
            # Exhaustive: `seam` has a second sentinel (see the ORG_SCOPED tail).
            return (
                f"declared {Tenancy.OPERATOR.value} (fleet-wide) but scope {scope!r} is already in "
                "force, and it is neither the operator plane nor an org this arm can reason about"
            )
        return None
    # ORG_SCOPED: one org, about to be bound; two scopes for one request is the contradiction (the
    # request-level twin of `seam.stamp_scope`). Neither arm is gated on `resolved`: with nothing
    # resolved no bind happens, so a foreign scope would stay in force for the handler.
    if isinstance(scope, UUID) and scope != resolved:
        return (
            f"declared {Tenancy.ORG_SCOPED.value} and the decision resolved "
            f"{_resolved_label(resolved)}, but org {scope} is already "
            "in force: one request cannot read two orgs"
        )
    # Exempt when `resolved` is `OPERATOR` (the cookie-less agent caller): then the scope in force is the
    # one about to be bound. Defence in depth today.
    if scope is seam.OPERATOR and resolved is not seam.OPERATOR:
        return (
            f"declared {Tenancy.ORG_SCOPED.value} and the decision resolved "
            f"{_resolved_label(resolved)}, but the operator plane is "
            "already in force: an org-scoped route may not read fleet-wide"
        )
    if scope is not None and not isinstance(scope, UUID):
        # The tail refuses: `seam.IDENTITY` renders as no filter at all for `seam.IDENTITY_CLASSES`,
        # so falling through would permit the widest read there is. Same rule as `seam.criteria_for`.
        return (
            f"declared {Tenancy.ORG_SCOPED.value} and the decision resolved "
            f"{_resolved_label(resolved)}, but scope {scope!r} is "
            "already in force: an org-scoped route may read one org, and that is not one"
        )
    return None


async def _bind_read_scope(
    conn: HTTPConnection,
    declaration: Declaration,
    resolved: seam.Scope,
    principal: Principal | None = None,
    org_filter: frozenset[UUID] | None = None,
) -> None:
    """Refuse a declaration that disagrees with the bound context, then bind the request's read scope
    (or leave it unbound when there is no org to bind), then the operator's org filter.

    `org_filter` is `_requested_org_filter`'s answer, already reduced to orgs that exist. It is applied
    only when the bound scope is `OPERATOR`, the request a GET/HEAD and `principal` an operator; in every
    other case it is ignored (R5.7, R5.8), so a crafted header never widens nor narrows anyone else.

    `async def` with nothing to await, so `seam.bind` can never be reached from a worker thread, where the
    `ContextVar` set would silently vanish (pinned by the scope-sites census test). One home for both
    transports. Unbound is a real answer: `criteria_for` renders it `false()`. The refusal is reshaped per
    transport here and logged at ERROR, since it always means a wrong declaration."""
    contradiction = _tenancy_disagreement(declaration, resolved, seam.current_scope())
    if contradiction is not None:
        log.error(
            "authz: denying %s — tenancy declaration and bound context disagree: %s",
            conn.scope.get("path"),
            contradiction,
        )
        if conn.scope.get("type") == "websocket":
            raise await _deny_websocket(conn, 403, "forbidden")
        raise _forbidden(conn)
    # Mark the plane for every caller that is a person, even unbound (`seam.mark_request_plane`): B28
    # reads it to tell a superadmin's operator-plane create, which must name its org, from a cron's. Not
    # for the cookie-less agent caller, who resolves `OPERATOR` and has nobody to ask for an org — the
    # mark would arm `write_seam._refuses_unstamped_create` against it.
    if resolved is not seam.OPERATOR or principal is not None:
        # A person who resolves `OPERATOR` (an operator on a `CALLER_PLANE` route or reading an
        # `ORG_SCOPED` one) is marked; the cookie-less agent caller has no principal.
        seam.mark_request_plane()
    # A route declared fleet-wide is bound fleet-wide; otherwise e.g. `routes/crons.py`'s org-zero system
    # crons 404 for a caller bound to their own org. Keyed on the declaration, not `is_superadmin`, which
    # would also widen a superadmin's `ORG_SCOPED` requests; only `@superadmin_only` routes reach here
    # with `OPERATOR`. No per-read escape instead: `stamp_scope` pins one session to one scope, so it
    # must be right before the first read. `bind()`, not a region: an `async def` on the loop.
    act_as.mark_reader(principal is not None and principal.acting)
    if declaration.tenancy is Tenancy.OPERATOR:
        seam.bind(seam.OPERATOR)
    elif resolved is not None:
        seam.bind(resolved)
    if (
        org_filter
        and seam.current_scope() is seam.OPERATOR
        and _is_read(conn)
        and principal is not None
        and principal.is_operator
    ):
        seam.set_org_filter(org_filter)
