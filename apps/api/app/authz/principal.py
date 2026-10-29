"""The principal: who is calling (`Principal`, `current_principal`, `Caller`), resolved once per request."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app import access, act_as, api_keys
from app.auth import (
    COOKIE_NAME,
    SESSION_IDLE_TIMEOUT_DETAIL,
    TERMS_PENDING_PATHS,
    TERMS_REQUIRED,
    require_user,
    read_session,
    session_idled_out,
    session_row_is_live,
)
from app.db import IdentitySessionLocal, get_session
from app.models.access import UserGroupMembership
from app.models.identity import AdminUser
from app.tenancy import seam
from app.tenancy.copy import administrator_changes
from app.tenancy.ids import is_operator_plane, is_tenant_org
from app.clock import utcnow as _now
from app.authz.catalog import OWNER_KEYS, PERMISSION_KEYS


# ── The principal ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Principal:
    """A resolved session user, flattened to exactly what an authorization decision needs.

    A frozen value, never the ORM `AdminUser`: it is resolved in a short-lived session and cached for a
    scope (a request, or a whole socket connection — see `current_principal`). Built from the user's
    effective role bindings (`app/access.py`), inheritance already applied."""

    user_id: UUID
    is_superadmin: bool
    permissions: frozenset[str]
    """THE ROUTE-LEVEL FLOOR: every key held ANYWHERE — `org_keys` ∪ every triple's key. Additive, no
    deny set, so this frozenset is the complete answer to "do you hold this key somewhere".

    IT IS THE FLOOR AND NOTHING MORE. A key a project binding confers is in here, so a route that then
    acts on a project must narrow by `scope_permissions`; and a route whose key WIDENS reach beyond a
    resource (`access_catalog.WIDENING_KEYS`) asks `has_org` instead, never `has`."""
    group_ids: tuple[UUID, ...]
    scope_permissions: frozenset[tuple[str, int | UUID, str]] = frozenset()
    """THE (scope_type, scope_id, key) TRIPLES A SCOPED BINDING CONFERS — `project` and `account` scopes (`app/access.py::expand` is the single home for which triples each binding emits).

    Org-wide keys are NOT expanded into triples (a portfolio's every row would be); they live in
    `org_keys`, and every scope reader here consults both (`has_on_scope`, `scope_key_clause`).

    A FROZENSET OF TRIPLES, not a mapping, because `Principal` is frozen and therefore hashable.
    NEVER CONSULTED FOR A SUPERADMIN, whose allow-all comes off the flag. DEFAULTS TO DENY."""

    org_id: UUID | None = None
    """THE ORG THIS PRINCIPAL BELONGS TO — the value `authz_guard` binds as the request's read scope.

    It rides on the principal because the bind happens on the event loop, after `_enforce` has returned
    from its worker thread. DEFAULTS TO `None` — UNBOUND, WHICH IS DENY (`app/tenancy/seam.py::
    criteria_for`). A superadmin is bound to their own org on an `ORG_SCOPED` route and fleet-wide only
    where a route SAYS it is fleet-wide (`_bind_read_scope` is the single home for that argument)."""

    org_keys: frozenset[str] = frozenset()
    """Keys held ORG-WIDE: an org binding of any role (an Org Owner/Admin holds every key). Bounded to the
    principal's own org by the seam, never by this field. Read through `has_org` and the org arm of every
    scope reader. DEFAULTS TO DENY."""

    org_role: str | None = None
    """`access.ORG_OWNER`, `access.ORG_ADMIN`, or None — the tenant-admin question `Access.ORG_ADMIN`
    and the org-credential arm of `usable_credential_clause` ask."""

    system_project_ids: frozenset[int] = frozenset()
    """THE SYSTEM PROJECTS THIS PRINCIPAL MAY NOT SEE (`Project.system_kind`, `app/node_targets.py`) — its org's,
    resolved once for a principal that is neither `is_org_admin` nor a superadmin, and empty for one who is. The
    one input of the O1 rule: `scope_key_clause` excludes them from every arm, `has_scope_key` refuses them. A
    system project created later is seen on the next resolution, as a binding edit is. DEFAULTS TO EMPTY."""

    bindings: tuple[access.EffectiveBinding, ...] = ()
    """Provenance: the live bindings this principal was built from. Never a READ decision's input; the one
    write decision that reads it is delegated admin (`app/access.py::may_manage` asks which scopes the
    principal holds Project Maintainer on)."""

    valid_until: datetime | None = None
    """The earliest expiry among those bindings, or None. `current_principal` re-resolves a cached
    principal once it passes, so an expiring binding stops conferring on a long-lived socket too."""

    api_key_id: UUID | None = None
    """The API key this request presents (`app/api_keys.py`), or None for a console session. A key's principal is
    already narrowed (`api_keys.narrow`); the guard reads this to skip the widening fallbacks (`own_credential`,
    `author_draft`) and to audit a refusal."""

    acting: bool = False
    """True when this is an operator's tab acting as `org_id`'s Owner seat (`owner_seat`, `app/act_as.py`):
    `user_id` is still the operator, everything else is the seat. Read for attribution, never to admit — with four
    exceptions: naming a stand-in starter (`routes/tasks/dispatch.py::_push_identity_or_422`), since the operator is no
    member to be stamped; `authz/visibility.py::edits_any_credential`, so an acting operator edits any of the
    org's credentials, personal and system rows included; `authz/visibility.py::_hidden_gate`, so an acting
    operator reads and writes the org's hidden tasks, as the operator itself does; and
    `governance/pack_admin.py::is_pack_operator`, so an acting operator's console tab edits the managed compliance
    packs (Compliance exists only inside a tenant org)."""

    @property
    def is_operator(self) -> bool:
        """`is_superadmin` and a member of the operator plane (`app/tenancy/ids.py::is_operator_plane`).

        This, not `is_superadmin`, admits a caller to a `Tenancy.OPERATOR` route: a superadmin created
        inside a tenant org would otherwise read every org's rows. Readers: `authz_guard`,
        `routes/stream.py::_may_see`."""
        return is_operator_plane(self.org_id, self.is_superadmin)

    @property
    def is_org_admin(self) -> bool:
        """The tenant-admin question (G4): an operator, or an Org Owner/Admin of a TENANT org — the one rule
        `Access.ORG_ADMIN` and every handler-level "may administer this org" check ask. On the operator
        plane an org role administers nothing: the fleet's own users, nodes and crons stay the operator's
        (`org_admin_writer`'s operator arm is the same rule)."""
        if self.is_operator:
            return True
        return is_tenant_org(self.org_id) and self.org_role in (access.ORG_OWNER, access.ORG_ADMIN)

    def has(self, key: str) -> bool:
        """The FLOOR: superadmin is allow-all; everyone else holds `key` somewhere.

        No scope considered; use `has_on_scope`/`scopes_with` wherever the decision names one, and
        `has_org` wherever the key widens (the census: `test_authz_policy.py`)."""
        return self.is_superadmin or key in self.permissions

    def has_org(self, key: str) -> bool:
        """Does this principal hold `key` ORG-WIDE — superadmin, or an org binding carrying it?

        THE ONLY QUESTION A WIDENING SITE MAY ASK: a key held on one project never reaches another
        project's rows or the org's settings."""
        return self.is_superadmin or key in self.org_keys

    def has_on_scope(self, scope_type: str, scope_id: int | UUID, key: str) -> bool:
        """Does this principal hold `key` on the `scope_type` scope `scope_id` — org-wide, or bound there
        (inheritance already applied)?"""
        return (
            self.is_superadmin
            or key in self.org_keys
            or (scope_type, scope_id, key) in self.scope_permissions
        )

    def scopes_with(self, scope_type: str, key: str) -> frozenset[int | UUID]:
        """Every `scope_type` scope a SCOPED binding confers `key` on.

        The org arm is not in here (it is every row): callers short-circuit on `has_org(key)` first, as
        `scope_key_clause` does."""
        return frozenset(
            sid for stype, sid, held in self.scope_permissions if stype == scope_type and held == key
        )

_PRINCIPAL_CACHE_ATTR = "authz_principal"
_UNRESOLVED = object()


class RevokedSession(HTTPException):
    """A cookie was presented and did not verify — bad signature, expired, unknown user, or a
    `session_epoch` retired by a logout or a password change.

    Exists so this cannot collapse into `None` (the allow-all agent principal): `SessionAuthMiddleware`
    checks only the signature, so revoking a session would otherwise upgrade it. A 401 `HTTPException`,
    so handlers using `current_principal` directly also answer 401, not 500."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=401, detail=detail)


def effective_access(
    session: Session, user_id: UUID, *, now: datetime | None = None
) -> tuple[tuple[access.EffectiveBinding, ...], access.Expansion]:
    """The user's live bindings and what they confer — the one resolution every reader derives from."""
    bindings = access.effective_bindings(session, user_id, now=now or _now())
    return bindings, access.expand(bindings, vocabulary=PERMISSION_KEYS)


def effective_permissions(session: Session, user_id: UUID) -> list[str]:
    """The route-level floor for `user_id`, sorted — `GET /api/auth/me`, `routes/users.py::_user_detail`.

    Derived from `effective_access`, so it cannot disagree with the principal."""
    return sorted(effective_access(session, user_id)[1].permissions)


def _resolve_principal(conn: HTTPConnection) -> Principal | None:
    """Verify this connection's session cookie and build its `Principal`, or None when there is no
    session user (the agent/infrastructure principal).

    Verifies the cookie directly and never reads `conn.state.admin_id`: `SessionAuthMiddleware` sets it only
    on http scopes, so WebSockets would resolve None. The `session_epoch` comparison is load-bearing —
    without it a logged-out or password-rotated cookie is accepted."""
    raw = conn.cookies.get(COOKIE_NAME)
    if raw is None:
        key = api_keys.of(conn)
        if key is not None:
            return key_principal(key)
        # No cookie and no key — the only genuine agent/infrastructure principal.
        return None
    claims = read_session(raw)
    if claims is None:
        raise RevokedSession("session cookie did not verify")
    # The identity lane (`app/db.py`): a guarded route already holds its route session's connection here, so
    # a second checkout from the request pool is the hold-and-wait that starved it.
    with IdentitySessionLocal() as session:
        # This read discovers the org, so it cannot be filtered by one (`seam.identity_scope`).
        with seam.identity_scope():
            user = session.get(AdminUser, claims.admin_id)
            if not session_row_is_live(user, claims.epoch, claims.iepoch):
                # A signature-valid cookie for a deleted or disabled user, or a retired epoch: deny.
                raise RevokedSession("session has been revoked")
        # The Terms gate on every scope: a WebSocket never met `SessionAuthMiddleware`, and a cookie read under a
        # `PUBLIC_PREFIXES` path (the SSO test) skipped it.
        if claims.terms is not None and (conn.scope["type"] == "websocket" or conn.url.path not in TERMS_PENDING_PATHS):
            raise RevokedSession(TERMS_REQUIRED["code"])
        if conn.scope["type"] == "websocket":
            # The org's IP allowlist; an http request met it in `SessionAuthMiddleware` already.
            from app import ip_allowlist

            org_id = None if ip_allowlist.exempt(user) else user.org_id
            refused = ip_allowlist.refusal(session, org_id, conn.client.host if conn.client else None)
            if refused is not None:
                raise RevokedSession(refused)
        # Outside the escape: `orgs` is not tenant-owned, and the escape declares exactly what it reads.
        if session_idled_out(conn, session, user, claims.issued_at):
            raise RevokedSession(SESSION_IDLE_TIMEOUT_DETAIL)
        target = act_as.target_org(session, user, act_as.requested(conn.headers))
        with seam.identity_scope():
            principal = principal_for_user(session, user)
        return owner_seat(principal, target) if target is not None else principal


def key_principal(key: "api_keys.ApiKeyContext") -> Principal:
    """The owner's principal — an acting key's: the org's Owner seat (`owner_seat`) — narrowed to an API key
    (`api_keys.narrow`). The middleware verified the key moments ago; a disabled owner is refused here too (the cookie
    arm's `session_row_is_live`)."""
    with IdentitySessionLocal() as session:
        with seam.identity_scope():
            user = session.scalar(select(AdminUser).where(AdminUser.id == key.owner_id))
            if user is None or user.disabled_at is not None:
                raise RevokedSession("api key owner is disabled")
            principal = principal_for_user(session, user)
    if key.acting:
        principal = owner_seat(principal, key.org_id)
    return api_keys.narrow(principal, key)


def owner_seat(principal: Principal, org_id: UUID) -> Principal:
    """`principal` (an operator) as `org_id`'s Owner seat (`app/act_as.py`): the operator's `user_id`, that org,
    every key an Org Owner holds, no superadmin flag — so the guard binds that org on every route and
    `is_operator` is false, exactly as for the org's own Owner."""
    return Principal(
        user_id=principal.user_id,
        is_superadmin=False,
        permissions=OWNER_KEYS,
        group_ids=(),
        org_id=org_id,
        org_keys=OWNER_KEYS,
        org_role=access.ORG_OWNER,
        acting=True,
    )


def principal_for_user(session: Session, user: AdminUser) -> Principal:
    """The `Principal` for an already-loaded user row — the one way to build one outside a request.

    An in-process call (e.g. `app/slack_inbound.py` calling `routes/tasks.answer_task`) is enforced by no
    guard, so the caller must ask this module itself; hand-rolled lookups would be a second definition of
    the principal. `session` is the caller's, neither committed nor closed; the returned value holds no ORM
    instance.

    A disabled row (`AdminUser.disabled_at`) gets a principal holding NOTHING: no cookie reaches it
    (`auth.session_row_is_live`), and this keeps the in-process paths — a Slack answer, a cron the user
    owns — from acting with the authority the disable withdrew."""
    seat_of = act_as.operator_org(user)
    if seat_of is not None and user.org_id is not None:
        # A seat copy (`act_as.seat_user`) holds no bindings of its own: it is the org's Owner seat.
        return owner_seat(
            Principal(user_id=user.id, is_superadmin=False, permissions=frozenset(), group_ids=()), user.org_id
        )
    # Entered here too: the Slack path passes a caller's org-bound session, under which these queries
    # would resolve a different principal. Re-entry is a no-op.
    with seam.identity_scope():
        if user.disabled_at is not None:
            return Principal(
                user_id=user.id, is_superadmin=False, permissions=frozenset(), group_ids=(), org_id=user.org_id
            )
        bindings, expansion = effective_access(session, user.id)
        group_ids = tuple(
            session.scalars(
                select(UserGroupMembership.group_id)
                .where(UserGroupMembership.user_id == user.id)
                .distinct()
            ).all()
        )
        # Read inside the escape: on the Slack path an expired `user` would refresh under the caller's org.
        org_id = user.org_id
    principal = Principal(
        user_id=user.id,
        is_superadmin=bool(user.is_superadmin),
        permissions=expansion.permissions,
        # Resolved once: the clauses are pure functions built without a `Session`. A binding changed
        # mid-request is seen on the next one.
        scope_permissions=expansion.scope_permissions,
        group_ids=group_ids,
        org_id=org_id,
        org_keys=expansion.org_keys,
        org_role=expansion.org_role,
        bindings=bindings,
        valid_until=expansion.valid_until,
    )
    if principal.is_org_admin or principal.is_superadmin or org_id is None:
        return principal
    hidden = _system_project_ids(session, org_id)
    return replace(principal, system_project_ids=hidden) if hidden else principal


#: Raw SQL, as `app/access.py`'s binding read: the principal is being RESOLVED, under whatever scope its caller
#: holds — `seam.identity_scope()` on a request, whose open set `projects` is not in, or another org's on an
#: in-process path. It names one org and restates the seam's predicate, so it reads exactly what an ORM read
#: bound to that org would (`test_tenancy_bypass_sites.py`, `principal-own-system-projects`).
_SYSTEM_PROJECTS_OF_ORG = text("SELECT id FROM projects WHERE org_id = :org_id AND system_kind IS NOT NULL")


def _system_project_ids(session: Session, org_id: UUID) -> frozenset[int]:
    """The system projects (`Project.system_kind`) of the principal's own org — `Principal.system_project_ids`."""
    return frozenset(session.scalars(_SYSTEM_PROJECTS_OF_ORG, {"org_id": org_id}).all())


def current_principal(conn: HTTPConnection) -> Principal | None:
    """The request's principal — `Depends(current_principal)` in a handler, or called directly.

    Resolves the principal itself on a cold cache, never returning None for a real cookie (None is
    unfiltered). The memo lives as long as the scope: per http request, but per socket for a WebSocket, so a
    long-lived dial keeps its handshake principal until reconnect — or until `Principal.valid_until`, when
    an expiring binding makes it re-resolve.

    None only when no cookie was presented — the agent principal, or on an unguarded surface possibly an
    anonymous caller, which that handler must gate itself. A rejected cookie raises `RevokedSession`; only
    resolved outcomes are cached."""
    cached = getattr(conn.state, _PRINCIPAL_CACHE_ATTR, _UNRESOLVED)
    if cached is not _UNRESOLVED and not _expired(cached):
        return cached  # type: ignore[return-value]
    principal = _resolve_principal(conn)
    setattr(conn.state, _PRINCIPAL_CACHE_ATTR, principal)
    return principal


def _expired(principal: Any) -> bool:
    """Has a cached principal outlived the earliest binding it was built from? Then it is re-resolved,
    so a socket-long cache never keeps conferring an expired binding."""
    valid_until = getattr(principal, "valid_until", None)
    return valid_until is not None and _now() >= valid_until


@dataclass(frozen=True)
class Caller:
    """What a guarded handler needs of its caller, resolved once: the acting seat (`require_user`), the
    principal — never None, the route's guard resolved one before the handler ran — and the request
    session. `caller: Caller = Depends(current_caller)` is the one-line form of that triple."""

    actor: AdminUser
    principal: Principal
    session: Session


def current_caller(
    actor: AdminUser = Depends(require_user),
    principal: "Principal | None" = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Caller:
    if principal is None:  # pragma: no cover - a guarded route always resolves one
        raise HTTPException(status_code=403, detail="no principal")
    return Caller(actor=actor, principal=principal, session=session)


def org_admin_writer(subject: str) -> Callable[..., AdminUser]:
    """A dependency for a write only a tenant ADMINISTRATOR makes, on a route whose key a project role may
    also carry: in a tenant org, `Principal.is_org_admin` (anyone else reads who can act,
    `administrator_changes(subject)`); on the operator plane, a superadmin (403 otherwise: the door is shared). The one home
    for that bar (`routes/accounts.py`, `routes/pool_slots.py`, `routes/port_grid.py`)."""

    def _require(
        user: AdminUser = Depends(require_user),
        principal: "Principal | None" = Depends(current_principal),
    ) -> AdminUser:
        if is_tenant_org(user.org_id):
            if principal is not None and principal.is_org_admin:
                return user
            raise HTTPException(status_code=403, detail=administrator_changes(subject))
        # A 403, not `require_superadmin`'s hidden 404: the door is shared, the console shows it to staff.
        if not user.is_superadmin:
            raise HTTPException(status_code=403, detail="superadmin required")
        return user

    return _require


def creates_org_rows(principal: Principal | None) -> bool:
    """May `principal` create an ORG credential or account (`owner_id IS NULL`)? An Org Owner/Admin or an
    operator — the bar `stamp_owner_on_create` defaults by. The agent principal (`None`) owns nothing and
    is answered True: it has no user to make a row personal to."""
    return principal is None or principal.org_role is not None or principal.is_operator
