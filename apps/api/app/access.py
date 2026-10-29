"""Effective role bindings — the one home for "what does this user hold, and where" (access-model C3).

A user's access is the role bindings naming them directly, plus the bindings of every group they belong
to, minus the expired ones. `effective_bindings` reads them in one query; `expand` turns them into what
`app/authz/principal.py::Principal` carries. Nothing else reads `role_bindings` for an authorization decision.

INHERITANCE IS A READ RULE, NOT ROWS (`app/models/access.py::RoleBinding`), and it runs here, per principal
resolution, so an object created after a binding is reached by it on the next request:
- an org binding of an org role (Owner/Admin) → every key, org-wide (`org_keys`), and `org_role`;
- an org binding of a project role → that role's keys, org-wide (`org_keys`);
- a project binding → the role's keys on that project, which is also where its features and tasks live;
- an account binding, of ANY role → `accounts:view` on that account (may dispatch on it; D5/D10).
Org isolation is not decided here: every read below runs under the request's bound org
(`app/tenancy/seam.py`), which is what keeps an org-wide key inside its own org.

ADDITIVE ONLY: there is no deny binding, so the union below is the whole answer, and removing one binding
never removes what another confers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access_catalog import (
    LEVEL_ORG,
    LEVEL_PROJECT,
    ROLES_BY_SLUG,
    SCOPE_ACCOUNT,
    SCOPE_ORG,
    SCOPE_PROJECT,
    SCOPES,
    expand as expand_role,
)
from app.models.access import Group, RoleBinding, UserGroupMembership
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.models.projects import Project
from app.entity_shape import SYSTEM_ACTOR_ID, SYSTEM_ACTOR_NAME, iso_or_none
from app.identities import display_name
from app.tenancy.seam import OPERATOR, current_scope
from app.clock import utcnow as _now

#: The org role a principal holds, strongest first. `Principal.org_role` is one of these or None.
ORG_OWNER = "owner"
ORG_ADMIN = "admin"
_ORG_ROLE_BY_SLUG = {"org_owner": ORG_OWNER, "org_admin": ORG_ADMIN}

#: The key that shows a project (and names it in a member list or an explainer).
PROJECTS_VIEW = "projects:view"
#: The one key an account binding confers on its account, whatever the role.
ACCOUNTS_VIEW = "accounts:view"


@dataclass(frozen=True)
class EffectiveBinding:
    """One live binding a user holds, directly (`via_group_id is None`) or through a group — provenance
    for the explainer and `GET /api/auth/me`, never itself an authorization input beyond `expand`."""

    binding_id: UUID
    role_slug: str
    via_group_id: UUID | None
    scope_type: str
    scope_id: UUID | int
    expires_at: datetime | None


@dataclass(frozen=True)
class Expansion:
    """What a set of bindings confers — the fields `Principal` is built from."""

    org_keys: frozenset[str]
    scope_permissions: frozenset[tuple[str, int | UUID, str]]
    org_role: str | None
    valid_until: datetime | None

    @property
    def permissions(self) -> frozenset[str]:
        """The route-level floor: every key held anywhere, derived so it cannot disagree."""
        return self.org_keys | {key for _type, _id, key in self.scope_permissions}


def _scope_of(row) -> tuple[str | None, UUID | int | None]:
    """Which scope column this binding (or request) names (`ck_role_bindings_one_scope`). A row naming
    none of the three names only a retired scope column: `(None, None)`, which confers nothing, resolves
    to no scope and is never served as a real one."""
    if row.scope_org_id is not None:
        return SCOPE_ORG, row.scope_org_id
    if row.project_id is not None:
        return SCOPE_PROJECT, int(row.project_id)
    if row.account_id is not None:
        return SCOPE_ACCOUNT, row.account_id
    return None, None


#: The one read of `role_bindings` an authorization decision makes. RAW SQL, deliberately: it runs while
#: the principal is being RESOLVED, inside `seam.identity_scope()` (whose open set, `IDENTITY_CLASSES`,
#: `role_bindings` is not in — and `app/tenancy/seam.py` is frozen for this change), and from in-process
#: callers already bound to an org or the operator plane. Keyed on the user, with the seam's own predicate
#: RESTATED (`rb.org_id` is the user's org), so it reads exactly the rows an org-bound ORM read would.
#: Registered in `test_tenancy_bypass_sites.py`'s arm C.
_EFFECTIVE_BINDINGS_SQL = text(
    """
    WITH me AS (SELECT id, org_id FROM admin_users WHERE id = :user_id)
    SELECT rb.id, r.slug, rb.scope_org_id, rb.project_id, rb.account_id,
           rb.expires_at, NULL::uuid AS via_group_id
      FROM role_bindings rb
      JOIN roles r ON r.id = rb.role_id
      JOIN me ON rb.user_id = me.id AND rb.org_id = me.org_id
     WHERE CAST(:include_expired AS boolean) OR rb.expires_at IS NULL OR rb.expires_at > :now
    UNION ALL
    SELECT DISTINCT rb.id, r.slug, rb.scope_org_id, rb.project_id, rb.account_id,
           rb.expires_at, m.group_id AS via_group_id
      FROM role_bindings rb
      JOIN roles r ON r.id = rb.role_id
      JOIN user_group_memberships m ON m.group_id = rb.group_id
      JOIN me ON m.user_id = me.id AND rb.org_id = me.org_id
     WHERE CAST(:include_expired AS boolean) OR rb.expires_at IS NULL OR rb.expires_at > :now
    """
)


def effective_bindings(
    session: Session, user_id: UUID, *, now: datetime, include_expired: bool = False
) -> tuple[EffectiveBinding, ...]:
    """Every binding naming `user_id` or a group they belong to, that has not expired at `now`.

    One statement: the direct arm and the via-group arm, UNION ALL (`_EFFECTIVE_BINDINGS_SQL`).
    `expires_at <= now` confers nothing (AC-2.5); a binding with no expiry never expires.
    `include_expired=True` keeps the expired rows too — ONLY for `explain`, which reports them as
    `expired` and never as a path; no authorization decision may pass it."""
    rows = session.execute(
        _EFFECTIVE_BINDINGS_SQL, {"user_id": user_id, "now": now, "include_expired": include_expired}
    ).all()
    out = []
    for row in rows:
        scope_type, scope_id = _scope_of(row)
        if scope_type is None:
            continue
        out.append(
            EffectiveBinding(
                binding_id=row.id, role_slug=row.slug, via_group_id=row.via_group_id,
                scope_type=scope_type, scope_id=scope_id, expires_at=row.expires_at,
            )
        )
    return tuple(sorted(out, key=lambda b: (str(b.binding_id), str(b.via_group_id))))


def expand(bindings: Iterable[EffectiveBinding], *, vocabulary: frozenset[str]) -> Expansion:
    """Turn bindings into org-wide keys, scope triples, the org role and the instant this answer expires.

    `vocabulary` is `app/authz/catalog.py::PERMISSION_KEYS` (passed in: this module is imported by it). No query:
    a project binding's triples name the project alone, and every task and feature is keyed on its
    `project_id`."""
    bindings = tuple(bindings)
    org_keys: set[str] = set()
    triples: set[tuple[str, int | UUID, str]] = set()
    org_roles: set[str] = set()
    expiries = [b.expires_at for b in bindings if b.expires_at is not None]

    for binding in bindings:
        role = ROLES_BY_SLUG.get(binding.role_slug)
        if role is None:
            # A role row the catalog does not know confers nothing (custom roles are out of scope).
            continue
        keys = expand_role(role, vocabulary)
        if binding.scope_type == SCOPE_ORG:
            org_keys |= keys
            if role.level == LEVEL_ORG and role.slug in _ORG_ROLE_BY_SLUG:
                org_roles.add(_ORG_ROLE_BY_SLUG[role.slug])
        elif binding.scope_type == SCOPE_PROJECT:
            pid = int(binding.scope_id)
            triples.update((SCOPE_PROJECT, pid, key) for key in keys)
        elif binding.scope_type == SCOPE_ACCOUNT:
            triples.add((SCOPE_ACCOUNT, binding.scope_id, ACCOUNTS_VIEW))

    org_role = ORG_OWNER if ORG_OWNER in org_roles else (ORG_ADMIN if org_roles else None)
    return Expansion(
        org_keys=frozenset(org_keys),
        scope_permissions=frozenset(triples),
        org_role=org_role,
        valid_until=min(expiries) if expiries else None,
    )


# ── The write half (access-model C4): delegated admin, per-binding writes, the explainer ───────────
#
# WHO MAY MANAGE WHAT (`may_manage`, the one gate every binding write asks): an operator anything; an
# Org Owner anything in their org; an Org Admin anything in their org but an Org Owner grant; a Project
# Maintainer the project roles on the project they maintain. Org and account scopes, and groups, are
# the org roles' alone.
# Writes are per binding — add one, remove one — never a collection replace.

OWNER_ROLE = "org_owner"
MAINTAINER_ROLE = "project_maintainer"
ROLES_BY_ID = {role.id: role for role in ROLES_BY_SLUG.values()}

#: The one column each scope type names on `role_bindings`.
SCOPE_COLUMNS = {
    SCOPE_ORG: "scope_org_id",
    SCOPE_PROJECT: "project_id",
    SCOPE_ACCOUNT: "account_id",
}

NOT_A_MANAGER_DETAIL = "You cannot manage access here."
EVERYONE_DETAIL = "Only an Org Owner or Org Admin can grant a role to Everyone."
OWNER_TO_GROUP_DETAIL = "Org Owner is held by a person, never by a group."
ORG_SCOPE_EXPIRY_DETAIL = "An organization-wide role never expires."
PAST_EXPIRY_DETAIL = "The expiry must be in the future."
PERSONAL_ACCOUNT_DETAIL = "A personal account stays its owner's; it cannot be shared through a role."
OTHER_ORG_DETAIL = "The person or group must belong to the scope's organization."
LAST_OWNER_DETAIL = "An organization must keep at least one owner. Make someone else an owner first."
OWNER_SUBJECT_DETAIL = "Only an owner of your organization can change another owner."


@dataclass(frozen=True)
class Scope:
    """One resolved binding scope (or explainer resource): its type, id, org and display name."""

    type: str
    id: UUID | int
    org_id: UUID
    name: str
    #: An account's owner — set means a PERSONAL account, its owner's alone.
    owner_id: UUID | None = None

    @property
    def personal(self) -> bool:
        return self.owner_id is not None

    @property
    def wire(self) -> dict:
        return {"type": self.type, "id": str(self.id), "name": self.name}


@dataclass(frozen=True)
class Holder:
    """The principal a binding names: a user or a group of the scope's org."""

    kind: str
    id: UUID
    name: str
    org_id: UUID
    is_everyone: bool = False


def _one(session: Session, entity: type, row_id: object):
    """One row by id through a `select`, never `session.get`: an identity-map hit would skip the seam's
    criteria (`test_tenancy_bypass_sites.py`), so another org's row must be read the way it is refused."""
    return session.scalar(select(entity).where(entity.id == row_id))


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown {what}")


def resolve_scope(session: Session, scope_type: str, raw_id: object) -> Scope:
    """The scope `scope_type`/`raw_id` names, or a 404 — also for one in an org this request cannot read:
    the seam hides another org's rows, and an org scope is checked against the bound org here (`orgs` is
    not org-scoped), so another org's scope is indistinguishable from none."""
    try:
        if scope_type == SCOPE_PROJECT:
            scope_id: UUID | int = int(raw_id)  # type: ignore[arg-type]
        elif scope_type in SCOPES:
            scope_id = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
        else:
            raise ValueError(scope_type)
    except (TypeError, ValueError):
        raise _not_found("scope")
    if scope_type == SCOPE_ORG:
        bound = current_scope()
        if bound is not OPERATOR and bound != scope_id:
            raise _not_found("scope")
        org = _one(session, Org, scope_id)
        if org is None:
            raise _not_found("scope")
        return Scope(SCOPE_ORG, org.id, org.id, org.name)
    if scope_type == SCOPE_PROJECT:
        project = _one(session, Project, scope_id)
        if project is None:
            raise _not_found("scope")
        return Scope(SCOPE_PROJECT, int(project.id), project.org_id, project.name)
    account = _one(session, Account, scope_id)
    if account is None:
        raise _not_found("scope")
    return Scope(SCOPE_ACCOUNT, account.id, account.org_id, account.name, owner_id=account.owner_id)


def resolve_holder(session: Session, kind: str, raw_id: object) -> Holder:
    """The user or group `kind`/`raw_id` names, or a 404 (the system actor is not a user here)."""
    try:
        holder_id = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
    except (TypeError, ValueError):
        raise _not_found(kind if kind in ("user", "group") else "principal")
    if kind == "user":
        user = _one(session, AdminUser, holder_id)
        if user is None or user.is_system:
            raise _not_found("user")
        return Holder("user", user.id, display_name(user), user.org_id)
    if kind == "group":
        group = _one(session, Group, holder_id)
        if group is None:
            raise _not_found("group")
        return Holder("group", group.id, group.name, group.org_id, is_everyone=bool(group.is_everyone))
    raise HTTPException(status_code=422, detail="a principal is a user or a group")


def _maintains(principal, scope: Scope) -> bool:
    """Does `principal` hold a live Project Maintainer binding that covers `scope`? Org-wide (an org
    binding of the role) or on that project."""
    return any(
        binding.role_slug == MAINTAINER_ROLE and (
            binding.scope_type == SCOPE_ORG
            or (binding.scope_type == SCOPE_PROJECT and binding.scope_id == scope.id)
        )
        for binding in principal.bindings
    )


def may_manage(principal, scope: Scope, role_slug: str) -> bool:
    """May `principal` add or remove a `role_slug` binding at `scope`? THE ONE GATE every binding write
    asks (and `members` asks for `editable`).

    Operator → yes. Org Owner (same org) → yes. Org Admin (same org) → any role but Org Owner. Project
    Maintainer (effective) on project P → a project role on P. Org and account scopes → the org roles
    only. `principal` is an `app/authz/principal.py::Principal`; None (the agent principal) manages nothing."""
    role = ROLES_BY_SLUG.get(role_slug)
    if principal is None or role is None:
        return False
    if principal.is_operator:
        return True
    if principal.org_id != scope.org_id:
        return False
    if principal.is_org_admin:
        return principal.org_role == ORG_OWNER or role_slug != OWNER_ROLE
    if role.level != LEVEL_PROJECT or scope.type != SCOPE_PROJECT:
        return False
    return _maintains(principal, scope)


def may_manage_any(principal, scope: Scope) -> bool:
    """May `principal` manage SOME role at `scope`? The gate of the Add dialog's picker."""
    return any(may_manage(principal, scope, role.slug) for role in ROLES_BY_SLUG.values() if scope.type in role.valid_scopes)


def may_read_scope(principal, scope: Scope) -> bool:
    """Does `principal` see `scope` — the read half of `GET /api/access/members`'s gate?"""
    if principal is None or (principal.org_id != scope.org_id and not principal.is_operator):
        return False
    if scope.type == SCOPE_ORG:
        return principal.has_org("users:view")
    key = ACCOUNTS_VIEW if scope.type == SCOPE_ACCOUNT else PROJECTS_VIEW
    return principal.has_on_scope(scope.type, scope.id, key)


def org_role_of(session: Session, user_id: UUID) -> str | None:
    """`ORG_OWNER`, `ORG_ADMIN` or None — the org role `user_id`'s live org bindings confer."""
    roles = {
        _ORG_ROLE_BY_SLUG[b.role_slug]
        for b in effective_bindings(session, user_id, now=_now())
        if b.scope_type == SCOPE_ORG and b.role_slug in _ORG_ROLE_BY_SLUG
    }
    return ORG_OWNER if ORG_OWNER in roles else (ORG_ADMIN if roles else None)


def owners_and_admins(session: Session, org_id: UUID) -> list[AdminUser]:
    """The org's live people holding the Owner or Admin role — everyone an org-wide notice (billing, an alert, an
    automation's failure) is for."""
    users = session.scalars(
        select(AdminUser).where(
            AdminUser.org_id == org_id, AdminUser.is_system.is_(False), AdminUser.disabled_at.is_(None)
        ).order_by(AdminUser.email)
    ).all()
    return [u for u in users if org_role_of(session, u.id) in (ORG_OWNER, ORG_ADMIN)]


def live_operators(session: Session) -> list[AdminUser]:
    """Every live operator: a superadmin on the operator plane (`app/tenancy/ids.py::is_operator_plane`), the verdict
    `Principal.is_operator` gives; the system actor and a disabled user never are. Who hears of what the operator
    plane owns (a support ticket, a failing system schedule)."""
    from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

    return list(session.scalars(
        select(AdminUser).where(
            AdminUser.is_superadmin.is_(True), AdminUser.is_system.is_(False), AdminUser.disabled_at.is_(None),
            AdminUser.org_id.in_(OPERATOR_PLANE_ORG_IDS),
        ).order_by(AdminUser.email)
    ))


def may_edit_user(session: Session, principal, target_id: UUID) -> bool:
    """May `principal` edit or delete the user record `target_id` (email, name, password, delete)? Only
    an Org Owner (or an operator) touches an Org Owner's record: Owner outranks Admin."""
    if principal is not None and (principal.is_operator or principal.org_role == ORG_OWNER):
        return True
    return org_role_of(session, target_id) != ORG_OWNER


def last_owner_guard(session: Session, org_id: UUID, *, dropping: set[UUID]) -> None:
    """409 if the org's DIRECT `org_owner` users would be none once `dropping` goes — binding ids and/or
    user ids (both are uuids, never colliding). An org with no owner today is not refused (it has none to
    lose). THE ONE HOME, called by `remove_binding` and the user delete; the org-move mapper has its own
    SQL rule. The owner rows are locked, so two concurrent removals cannot each leave the other last."""
    rows = session.execute(
        select(RoleBinding.id, RoleBinding.user_id)
        .join(AdminUser, AdminUser.id == RoleBinding.user_id)
        .where(
            RoleBinding.org_id == org_id,
            RoleBinding.scope_org_id == org_id,
            RoleBinding.role_id == ROLES_BY_SLUG[OWNER_ROLE].id,
            or_(RoleBinding.expires_at.is_(None), RoleBinding.expires_at > _now()),
            AdminUser.is_system.is_(False),
            # A disabled Owner administers nothing, so is no Owner for this floor.
            AdminUser.disabled_at.is_(None),
        )
        .with_for_update(of=RoleBinding)
    ).all()
    before = {user_id for _bid, user_id in rows}
    after = {user_id for bid, user_id in rows if bid not in dropping and user_id not in dropping}
    if before and not after:
        raise HTTPException(status_code=409, detail=LAST_OWNER_DETAIL)


def _binding_query(holder: Holder, role_id: UUID, scope: Scope):
    column = getattr(RoleBinding, SCOPE_COLUMNS[scope.type])
    principal_column = RoleBinding.user_id if holder.kind == "user" else RoleBinding.group_id
    return select(RoleBinding).where(
        principal_column == holder.id, RoleBinding.role_id == role_id, column == scope.id
    )


def add_binding(
    session: Session,
    actor: AdminUser,
    actor_principal,
    *,
    principal: Holder,
    role_slug: str,
    scope: Scope,
    expires_at: datetime | None = None,
) -> tuple[RoleBinding, bool]:
    """Bind `principal` to `role_slug` at `scope`, as `actor` — `(row, created)`. Idempotent on the
    unique key: an existing binding is returned (`created=False`), taking the requested expiry.

    Refuses, before any write: an unknown role or one the scope cannot carry (422); a caller `may_manage`
    refuses (403); a binding to Everyone by anyone below Org Admin (403); an org-scope expiry, a past one,
    a personal account, an Org Owner grant to a group, a principal of another org (422). The caller
    commits; `record_write` stages the audit row in the same transaction. A LAPSED row on the unique
    key is renewed (to `expires_at`) and reported `created=True`, since it conferred nothing."""
    from app.actors import stamp_actor

    role = ROLES_BY_SLUG.get(role_slug)
    if role is None:
        raise HTTPException(status_code=422, detail=f"unknown role {role_slug!r}")
    if not may_manage(actor_principal, scope, role_slug):
        raise HTTPException(status_code=403, detail=NOT_A_MANAGER_DETAIL)
    if principal.is_everyone and not actor_principal.is_org_admin:
        raise HTTPException(status_code=403, detail=EVERYONE_DETAIL)
    if scope.type not in role.valid_scopes:
        raise HTTPException(status_code=422, detail=f"{role.name} is granted on the organization only.")
    if role_slug == OWNER_ROLE and principal.kind != "user":
        raise HTTPException(status_code=422, detail=OWNER_TO_GROUP_DETAIL)
    if expires_at is not None:
        if scope.type == SCOPE_ORG:
            raise HTTPException(status_code=422, detail=ORG_SCOPE_EXPIRY_DETAIL)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= _now():
            raise HTTPException(status_code=422, detail=PAST_EXPIRY_DETAIL)
    if scope.personal:
        raise HTTPException(status_code=422, detail=PERSONAL_ACCOUNT_DETAIL)
    if principal.org_id != scope.org_id:
        raise HTTPException(status_code=422, detail=OTHER_ORG_DETAIL)

    existing = session.scalar(_binding_query(principal, role.id, scope))
    if existing is not None:
        lapsed = existing.expires_at is not None and existing.expires_at <= _now()
        if not lapsed and existing.expires_at == expires_at:
            return existing, False
        # A re-add is an explicit grant: a LAPSED row is renewed (else the answer would report a grant
        # that confers nothing), a live one takes the requested expiry — `never` included.
        existing.expires_at = expires_at
        stamp_actor(existing, actor)
        session.flush()
        _audit_add(session, actor, existing, role, scope, principal, expires_at)
        return existing, lapsed
    row = RoleBinding(
        org_id=scope.org_id,
        user_id=principal.id if principal.kind == "user" else None,
        group_id=principal.id if principal.kind == "group" else None,
        role_id=role.id,
        expires_at=expires_at,
        **{SCOPE_COLUMNS[scope.type]: scope.id},
    )
    stamp_actor(row, actor, created=True)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        # A concurrent identical add won the unique key: the same answer as finding it above.
        existing = session.scalar(_binding_query(principal, role.id, scope))
        if existing is None:
            raise
        return existing, False
    _audit_add(session, actor, row, role, scope, principal, expires_at)
    return row, True


def _audit_add(session: Session, actor: AdminUser, row: RoleBinding, role, scope: Scope, principal: Holder,
               expires_at: datetime | None) -> None:
    """`role_binding_add`: the role, scope and principal NAMES, which the ids on the wire cannot supply."""
    from app.user_activity import record_write

    record_write(
        session, actor, "role_binding_add",
        f"Granted {role.name} on {scope.type} {scope.name} to {principal.kind} {principal.name}",
        data={
            "binding_id": row.id, "role": role.slug, "scope_type": scope.type, "scope_id": str(scope.id),
            "principal_kind": principal.kind, "principal_id": principal.id,
            "expires_at": iso_or_none(expires_at),
        },
    )


def binding_scope(session: Session, row: RoleBinding) -> Scope:
    """The resolved scope an existing binding (or request) names; 404 for one naming a retired scope."""
    scope_type, scope_id = _scope_of(row)
    if scope_type is None:
        raise _not_found("scope")
    return resolve_scope(session, scope_type, scope_id)


def remove_binding(session: Session, actor: AdminUser, actor_principal, binding_id: UUID) -> list[UUID]:
    """Remove one binding, as `actor`; returns the users whose access it narrowed (the principal, or the
    group's members) for the caller's post-commit revokes. 404 unknown (or another org's), 403 when
    `may_manage` refuses it or it binds Everyone and the caller is below Org Admin, 409 when it is the
    org's last direct Org Owner (`last_owner_guard`). The caller commits."""
    from app.user_activity import record_write

    row = _one(session, RoleBinding, binding_id)
    if row is None:
        raise _not_found("binding")
    role = ROLES_BY_ID.get(row.role_id)
    scope = binding_scope(session, row)
    if role is None or not may_manage(actor_principal, scope, role.slug):
        raise HTTPException(status_code=403, detail=NOT_A_MANAGER_DETAIL)
    if row.group_id is not None:
        group = _one(session, Group, row.group_id)
        if group is not None and group.is_everyone and not actor_principal.is_org_admin:
            raise HTTPException(status_code=403, detail=EVERYONE_DETAIL)
        holder = f"group {group.name if group is not None else row.group_id}"
        affected = list(session.scalars(
            select(UserGroupMembership.user_id).where(UserGroupMembership.group_id == row.group_id).distinct()
        ).all())
    else:
        if role.slug == OWNER_ROLE:
            last_owner_guard(session, row.org_id, dropping={row.id})
        user = _one(session, AdminUser, row.user_id)
        holder = f"user {display_name(user) if user is not None else row.user_id}"
        affected = [row.user_id]
    session.delete(row)
    record_write(
        session, actor, "role_binding_remove",
        f"Revoked {role.name} on {scope.type} {scope.name} from {holder}",
        data={
            "binding_id": binding_id, "role": role.slug, "scope_type": scope.type, "scope_id": str(scope.id),
            "principal_kind": "group" if row.group_id is not None else "user",
            "principal_id": row.group_id or row.user_id,
        },
    )
    return affected


# ── Reads over bindings: who holds what at a scope, and what a user reaches ─────────────────────────


def _ancestors(session: Session, scope: Scope) -> list[Scope]:
    """`scope` and every scope it inherits from, nearest first: a project ← the org."""
    chain = [scope]
    if scope.type != SCOPE_ORG:
        org = _one(session, Org, scope.org_id)
        chain.append(Scope(SCOPE_ORG, scope.org_id, scope.org_id, org.name if org is not None else ""))
    return chain


def members(session: Session, scope: Scope, *, actor_principal=None, expand_groups: bool = True) -> list[dict]:
    """Everyone holding a role at `scope`, inheritance and groups expanded, each row labelled with its
    source: `direct` (bound here), `inherited` (bound on an enclosing scope — `via` names it) or `group`
    (a member of a group bound at or above here — `via` names the group). `editable` is `may_manage` of
    the binding the row stems from; a group member's row is never editable (edit the group's binding).
    `expand_groups=False` (a caller who may neither read the org directory nor manage the scope) serves
    each group as one row with no members and no `last_active_at` — never the org roster by the back door.
    `status` is `expired` for a lapsed binding, which confers nothing."""
    now = _now()
    ancestors = _ancestors(session, scope)
    arms = [getattr(RoleBinding, SCOPE_COLUMNS[s.type]) == s.id for s in ancestors]
    rows = session.scalars(
        select(RoleBinding).where(or_(*arms), RoleBinding.org_id == scope.org_id)
        .order_by(RoleBinding.created_at, RoleBinding.id)
    ).all()
    by_key = {(s.type, s.id): s for s in ancestors}
    user_ids = {row.user_id for row in rows if row.user_id is not None}
    group_ids = {row.group_id for row in rows if row.group_id is not None}
    group_members: dict[UUID, list[UUID]] = {}
    if group_ids:
        for group_id, user_id in session.execute(
            select(UserGroupMembership.group_id, UserGroupMembership.user_id)
            .where(UserGroupMembership.group_id.in_(sorted(group_ids, key=str))).distinct()
        ).all():
            group_members.setdefault(group_id, []).append(user_id)
            user_ids.add(user_id)
    users = {
        u.id: u for u in session.scalars(select(AdminUser).where(AdminUser.id.in_(sorted(user_ids, key=str)))).all()
    } if user_ids else {}
    groups = {
        g.id: g for g in session.scalars(select(Group).where(Group.id.in_(sorted(group_ids, key=str)))).all()
    } if group_ids else {}

    out: list[dict] = []
    for row in rows:
        role = ROLES_BY_ID.get(row.role_id)
        if role is None:
            continue
        bound_at = by_key[_scope_of(row)]
        inherited = bound_at is not scope
        source = (
            {"kind": "inherited", "via_id": str(bound_at.id), "via_name": bound_at.name}
            if inherited else {"kind": "direct", "via_id": None, "via_name": None}
        )
        role_out = {"slug": role.slug, "name": role.name}
        status = "expired" if row.expires_at is not None and row.expires_at <= now else "active"
        editable = may_manage(actor_principal, bound_at, role.slug) if actor_principal is not None else False
        if row.user_id is not None:
            user = users.get(row.user_id)
            if user is None or user.is_system:
                continue
            out.append({
                "principal": {"kind": "user", "id": user.id, "name": display_name(user), "member_count": None},
                "role": role_out, "source": source, "binding_id": row.id, "expires_at": row.expires_at,
                "status": status, "last_active_at": user.last_seen_at, "editable": editable,
            })
            continue
        group = groups.get(row.group_id)
        if group is None:
            continue
        member_ids = [uid for uid in group_members.get(group.id, []) if uid in users and not users[uid].is_system]
        seen = [users[uid].last_seen_at for uid in member_ids if users[uid].last_seen_at]
        out.append({
            "principal": {"kind": "group", "id": group.id, "name": group.name, "member_count": len(member_ids)},
            "role": role_out, "source": source, "binding_id": row.id, "expires_at": row.expires_at,
            "status": status,
            "last_active_at": max(seen) if seen and expand_groups else None, "editable": editable,
        })
        for uid in member_ids if expand_groups else ():
            user = users.get(uid)
            if user is None or user.is_system:
                continue
            out.append({
                "principal": {"kind": "user", "id": user.id, "name": display_name(user), "member_count": None},
                "role": role_out,
                "source": {"kind": "group", "via_id": str(group.id), "via_name": group.name},
                "binding_id": row.id, "expires_at": row.expires_at, "status": status,
                "last_active_at": user.last_seen_at, "editable": False,
            })
    return out


def _names(session: Session, bindings: Iterable[EffectiveBinding]) -> dict[tuple[str, object], str]:
    """Display names for every scope and group `bindings` name, one query per type."""
    bindings = tuple(bindings)
    names: dict[tuple[str, object], str] = {}
    wanted: dict[str, set] = {}
    for b in bindings:
        wanted.setdefault(b.scope_type, set()).add(b.scope_id)
        if b.via_group_id is not None:
            wanted.setdefault("group", set()).add(b.via_group_id)
    for kind, entity in (
        (SCOPE_ORG, Org), (SCOPE_PROJECT, Project),
        (SCOPE_ACCOUNT, Account), ("group", Group),
    ):
        ids = wanted.get(kind)
        if ids:
            for row_id, name in session.execute(
                select(entity.id, entity.name).where(entity.id.in_(sorted(ids, key=str)))
            ).all():
                names[(kind, int(row_id) if kind == SCOPE_PROJECT else row_id)] = name
    return names


def _binding_out(b: EffectiveBinding, names: dict, now: datetime) -> dict:
    role = ROLES_BY_SLUG.get(b.role_slug)
    return {
        "binding_id": b.binding_id,
        "role": {"slug": b.role_slug, "name": role.name if role else b.role_slug},
        "scope": {"type": b.scope_type, "id": str(b.scope_id), "name": names.get((b.scope_type, b.scope_id), "")},
        "source": (
            {"kind": "group", "via_id": str(b.via_group_id), "via_name": names.get(("group", b.via_group_id), "")}
            if b.via_group_id is not None else {"kind": "direct", "via_id": None, "via_name": None}
        ),
        "expires_at": b.expires_at,
        "status": "expired" if b.expires_at is not None and b.expires_at <= now else "active",
    }


def reaches(session: Session, user_id: UUID) -> dict:
    """What `user_id`'s live bindings reach, grouped org → project, plus accounts, each binding with its
    source (direct, or the group it comes through)."""
    now = _now()
    bindings = effective_bindings(session, user_id, now=now)
    names = _names(session, bindings)

    org: list[dict] = []
    projects: dict[int, dict] = {}
    accounts: dict[UUID, dict] = {}

    for b in bindings:
        out = _binding_out(b, names, now)
        if b.scope_type == SCOPE_ORG:
            org.append(out)
        elif b.scope_type == SCOPE_PROJECT:
            pid = int(b.scope_id)
            projects.setdefault(pid, {
                "id": str(pid), "name": names.get((SCOPE_PROJECT, pid), ""), "bindings": [],
            })["bindings"].append(out)
        else:
            accounts.setdefault(b.scope_id, {
                "id": str(b.scope_id), "name": names.get((SCOPE_ACCOUNT, b.scope_id), ""), "bindings": [],
            })["bindings"].append(out)
    return {
        "org": org,
        "projects": sorted(projects.values(), key=lambda p: p["name"]),
        "accounts": sorted(accounts.values(), key=lambda a: a["name"]),
    }


# ── The explainer: why a user may (or may not) do one thing to one resource ─────────────────────────


def _confers(expansion: Expansion, resource: Scope, key: str) -> bool:
    """Does one binding's expansion carry `key` on `resource`? The same test `Principal.has_org` /
    `has_on_scope` make, applied to a single binding."""
    if key in expansion.org_keys:
        return True
    return resource.type != SCOPE_ORG and (resource.type, resource.id, key) in expansion.scope_permissions


def _reaches_resource(expansion: Expansion, resource: Scope) -> bool:
    """Does one binding's expansion reach `resource` at all, with any key?"""
    if expansion.org_keys:
        return True
    return any(t == resource.type and i == resource.id for t, i, _k in expansion.scope_permissions)


def _missing_message(resource: Scope) -> str:
    if resource.type == SCOPE_ORG:
        return "No role on the organization."
    return f"No role on {resource.type} {resource.name} or the organization."


def explain(session: Session, user: AdminUser, resource: Scope, feature: str, action: str) -> dict:
    """May `user` do `(feature, action)` — a route class of the permission catalog — to `resource`, and
    why?

    `allowed` is the enforcement's own answer (`Principal.has_org` for an org resource, `has_on_scope`
    otherwise, on the principal `principal_for_user` resolves — the one resolution; no second checker).
    `chain` is the path that decides it: principal → [group] → binding role@scope → [inherited via] →
    resource. Expired bindings come back in `expired`, never as a path. When nothing allows it,
    `missing` names the nearest missing link: a role held here that lacks the action, else no binding on
    the resource's own scope, its project or the org. The action is served as its human label, never
    as a raw key."""
    from app.authz import catalog, principal as principal_module

    if feature not in catalog.FEATURES or action not in catalog.ACTIONS:
        raise HTTPException(status_code=422, detail="unknown action")
    key = catalog.permission_key(feature, action)
    label = catalog.permission_description(feature, action)
    now = _now()
    principal = principal_module.principal_for_user(session, user)
    user_label = display_name(user)
    user_node = {"kind": "user", "id": str(user.id), "name": user_label}
    resource_node = {"kind": "resource", **resource.wire}
    answer: dict = {"allowed": False, "action_label": label, "chain": [], "expired": [], "missing": None}

    if principal.is_superadmin:
        answer.update(allowed=True, chain=[{"kind": "operator", "id": str(user.id), "name": user_label}])
        return answer
    if user.org_id != resource.org_id:
        answer["missing"] = {
            "reason": "other_org", "scope": resource.wire,
            "message": f"{user_label} belongs to another organization.",
        }
        return answer
    if resource.personal:
        # A personal account is its owner's alone — no role reaches it, an Org Owner/Admin's included
        # (`authz.visible_account_clause`, the enforcement).
        if resource.owner_id == user.id:
            answer.update(allowed=True, chain=[user_node, {"kind": "owner", "id": str(user.id), "name": user_label}, resource_node])
        else:
            answer["missing"] = {
                "reason": "personal", "scope": resource.wire,
                "message": f"{resource.name} is a personal account: only its owner may use it.",
            }
        return answer
    answer["allowed"] = (
        principal.has_org(key) if resource.type == SCOPE_ORG
        else principal.has_on_scope(resource.type, resource.id, key)
    )

    every = effective_bindings(session, user.id, now=now, include_expired=True)
    names = _names(session, every)
    vocabulary = catalog.PERMISSION_KEYS

    def chain_for(b: EffectiveBinding) -> list[dict]:
        out = _binding_out(b, names, now)
        nodes = [user_node]
        if b.via_group_id is not None:
            nodes.append({"kind": "group", "id": out["source"]["via_id"], "name": out["source"]["via_name"]})
        nodes.append({
            "kind": "binding", "binding_id": str(b.binding_id), "role": out["role"]["name"],
            "scope": out["scope"], "expires_at": iso_or_none(b.expires_at),
            "status": out["status"],
        })
        if (b.scope_type, b.scope_id) != (resource.type, resource.id):
            nodes.append({"kind": "inherited", "via": out["scope"]})
        nodes.append(resource_node)
        return nodes

    live, expired, reaching = [], [], []
    for b in every:
        expansion = expand((b,), vocabulary=vocabulary)
        is_expired = b.expires_at is not None and b.expires_at <= now
        if _confers(expansion, resource, key):
            (expired if is_expired else live).append(b)
        elif not is_expired and _reaches_resource(expansion, resource):
            reaching.append(b)

    def rank(b: EffectiveBinding):
        """Nearest first: the resource's own scope, then its project, then the org; direct before a
        group; the strongest role first."""
        role = ROLES_BY_SLUG.get(b.role_slug)
        own = (b.scope_type, b.scope_id) == (resource.type, resource.id)
        distance = 0 if own else (2 if b.scope_type == SCOPE_ORG else 1)
        return (distance, b.via_group_id is not None, -(role.rank if role else 0), str(b.binding_id))

    if answer["allowed"] and live:
        answer["chain"] = chain_for(sorted(live, key=rank)[0])
    answer["expired"] = [chain_for(b) for b in sorted(expired, key=rank)]
    if not answer["allowed"]:
        if reaching:
            held = sorted({ROLES_BY_SLUG[b.role_slug].name for b in reaching if b.role_slug in ROLES_BY_SLUG})
            nearest = sorted(reaching, key=rank)[0]
            answer["missing"] = {
                "reason": "role",
                "scope": _binding_out(nearest, names, now)["scope"],
                "message": f"{' and '.join(held)} does not include: {label}",
            }
        else:
            answer["missing"] = {"reason": "binding", "scope": resource.wire, "message": _missing_message(resource)}
    return answer


# ── The Everyone group and email-domain auto-join (access-model C6) ─────────────────────────────────
#
# G8: every org has exactly one Everyone group (`groups.is_everyone`) and EVERY user of the org is in it,
# invited or domain-joined. Its membership is the system's: the user-create helper adds it
# (`routes/users.py::create_org_user`), the FK cascade removes it with the user, and a user never changes
# org (there is no re-home path for `admin_users`), so nothing else moves it. No route may add, remove,
# rename or delete it by hand (`EVERYONE_MANAGED_DETAIL`, `EVERYONE_FIXED_DETAIL`).
#
# D4: an org's email domains are an operator's to enter (no DNS proof); a verified Google sign-in on one
# creates the user in that org (`routes/auth.py::google_login`). The org's `default_role_id` — none by
# default — is bound to them at org scope, by the system actor.

EVERYONE_NAME = "Everyone"
EVERYONE_MANAGED_DETAIL = (
    "Everyone's members are managed automatically: every user of the organization belongs to it."
)
EVERYONE_FIXED_DETAIL = "The Everyone group cannot be renamed or deleted."
#: The roles an org may give every domain-joined user. Never an org role: a sign-in must not mint an admin.
DEFAULT_ROLE_SLUGS = ("project_viewer", "project_developer")
INVALID_DOMAIN_DETAIL = "Enter an email domain such as example.com."

_EVERYONE_UNIQUE = "uq_groups_everyone_per_org"
_GROUP_NAME_UNIQUE = "uq_groups_org_id_lower_name"
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def everyone_group(session: Session, org_id: UUID) -> Group:
    """The org's Everyone group, created when it has none (an org created before this code, until the
    mapping's re-run reaches it). Named "Everyone", else "Everyone (<org slug>)" when the org has a group
    named so already (names are unique per org), and the per-org partial unique index keeps two creators to one row. Created by the
    system actor (the column default). Flushes; the caller commits."""
    from app.entity_shape import constraint_name

    query = select(Group).where(Group.org_id == org_id, Group.is_everyone.is_(True))
    group = session.scalar(query)
    if group is not None:
        return group
    org = _one(session, Org, org_id)
    own = f"{EVERYONE_NAME} ({org.slug if org is not None else org_id})"
    # Last, a name no other org can hold, so a squatted "Everyone (<slug>)" cannot block the org.
    for name in (EVERYONE_NAME, own, f"{EVERYONE_NAME} ({org_id})"):
        candidate = Group(
            name=name, description="Every user of the organization.", org_id=org_id, is_everyone=True
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
        except IntegrityError as exc:
            if constraint_name(exc) == _EVERYONE_UNIQUE:
                # A concurrent creator won: theirs is the org's one Everyone group.
                return session.scalar(query)
            if constraint_name(exc) != _GROUP_NAME_UNIQUE:
                raise
            continue
        return candidate
    raise RuntimeError(f"no free name for org {org_id}'s Everyone group")


def join_everyone(session: Session, user: AdminUser) -> None:
    """Make `user` a member of their org's Everyone group (idempotent). Flushes; the caller commits."""
    group = everyone_group(session, user.org_id)
    member = session.scalar(
        select(UserGroupMembership.id).where(
            UserGroupMembership.user_id == user.id, UserGroupMembership.group_id == group.id
        ).limit(1)
    )
    if member is None:
        session.add(UserGroupMembership(user_id=user.id, group_id=group.id, org_id=user.org_id))
        session.flush()


def canonical_domain(raw: object) -> str:
    """`raw` in the stored spelling — trimmed, lowercase, no trailing dot — unvalidated. The one spelling
    rule, for a lookup that must match what `normalized_domain` stored."""
    return str(raw or "").strip().lower().rstrip(".")


def normalized_domain(raw: object) -> str:
    """`raw` as a stored email domain (`canonical_domain`), or a 422. At least two DNS labels, each 1-63
    letters, digits or inner hyphens, 253 characters at most, and not an IP address."""
    domain = canonical_domain(raw)
    labels = domain.split(".")
    if (
        len(domain) > 253
        or len(labels) < 2
        or not all(_DOMAIN_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise HTTPException(status_code=422, detail=INVALID_DOMAIN_DETAIL)
    return domain


def email_domain(email: str) -> str | None:
    """The domain of `email` in its stored form, or None when it has none worth resolving."""
    local, at, domain = (email or "").rpartition("@")
    if not at or not local:
        return None
    try:
        return normalized_domain(domain)
    except HTTPException:
        return None


def org_for_email_domain(session: Session, domain: str) -> UUID | None:
    """The enabled org that registered `domain`, or None. RAW SQL, deliberately: this is identity
    resolution on the unbound Google sign-in, which has no org to read `org_domains` under until this
    answers. One exact value, one column, and a global unique key (`uq_org_domains_domain`), so it can
    only ever name one org. Org zero never answers: the control plane's own org takes no sign-ups. A
    tenant may: registering a domain there admits that org's own staff, who still reach
    nothing until bound unless its default role says so.
    Registered in `test_tenancy_bypass_sites.py`'s arm C."""
    from app.tenancy.ids import ORG_ZERO_ID

    org_id = session.execute(
        text(
            "SELECT d.org_id FROM org_domains d JOIN orgs o ON o.id = d.org_id "
            "WHERE d.domain = :domain AND o.enabled"
        ),
        {"domain": domain},
    ).scalar()
    return None if org_id is None or org_id == ORG_ZERO_ID else org_id


@dataclass(frozen=True)
class _SystemActor:
    """The system actor writing inside one org — what `stamp_actor` and `record_write` read (`id`,
    `email`, `org_id`), so the audit row lands in the org the write happens in."""

    org_id: UUID
    id: UUID = SYSTEM_ACTOR_ID
    email: str = SYSTEM_ACTOR_NAME


@dataclass(frozen=True)
class _SystemPrincipal:
    """The system's standing for `add_binding`'s gate, and nothing more. PRIVATE: its callers are
    `bind_default_role` (the role is the org's own `default_role_id`, a project role), `bind_org_owner`
    (Org Owner of the user's own org) and `bind_staging_role` (Owner/Admin of a new staging org); each chooses
    the role and the scope (the user's own org) itself, so no caller-supplied input reaches it."""

    is_operator: bool = True
    is_org_admin: bool = True
    org_role: str | None = None
    org_id: UUID | None = None
    bindings: tuple = ()


def system_actor(org_id: UUID) -> _SystemActor:
    return _SystemActor(org_id=org_id)


def bind_org_owner(session: Session, user: AdminUser) -> RoleBinding:
    """Bind `user` as Org Owner of their own org, as the system — the self-service sign-up's first user
    (`app/org_create.py::bind_owner`). Must run with the user's org bound. The caller commits.

    Refuses (RuntimeError, a caller defect) unless `user` is the owner the org's `org_signups` row names:
    the system's standing must never make an arbitrary member — a domain-joined user, say — an Owner."""
    from app.models.orgs import OrgSignup

    owner_id = session.scalar(select(OrgSignup.owner_id).where(OrgSignup.org_id == user.org_id))
    if owner_id is None or owner_id != user.id:
        raise RuntimeError("bind_org_owner: the user is not the sign-up owner of their org")
    row, _created = add_binding(
        session, system_actor(user.org_id), _SystemPrincipal(),  # type: ignore[arg-type]
        principal=Holder("user", user.id, display_name(user), user.org_id),
        role_slug=OWNER_ROLE,
        scope=resolve_scope(session, SCOPE_ORG, user.org_id),
    )
    return row


def bind_staging_role(session: Session, user: AdminUser, role_slug: str) -> RoleBinding:
    """Bind `user` as Org Owner or Org Admin (`role_slug`) of their own org, as the system — a staging org's copy of
    its parent's Owners and Admins (`app/staging_org.py::create`). Must run with the user's org bound. The caller
    commits. Refuses (RuntimeError, a caller defect) any other role, or an org that is not a staging org: the
    system's standing must never make an arbitrary member an Owner."""
    from app.models.orgs import Org

    parent = session.scalar(select(Org.parent_org_id).where(Org.id == user.org_id))
    if parent is None or role_slug not in (OWNER_ROLE, "org_admin"):
        raise RuntimeError("bind_staging_role: an Owner or Admin of a staging org only")
    row, _created = add_binding(
        session, system_actor(user.org_id), _SystemPrincipal(),  # type: ignore[arg-type]
        principal=Holder("user", user.id, display_name(user), user.org_id),
        role_slug=role_slug,
        scope=resolve_scope(session, SCOPE_ORG, user.org_id),
    )
    return row


def bind_default_role(session: Session, user: AdminUser) -> RoleBinding | None:
    """Bind `user` to their org's default role at org scope, as the system; None when the org has none
    (the shipped default: they reach nothing until someone binds them). Must run with the user's org
    bound. The caller commits."""
    org = _one(session, Org, user.org_id)
    role = ROLES_BY_ID.get(org.default_role_id) if org is not None and org.default_role_id else None
    if role is None or role.slug not in DEFAULT_ROLE_SLUGS:
        return None
    row, _created = add_binding(
        session, system_actor(user.org_id), _SystemPrincipal(),  # type: ignore[arg-type]
        principal=Holder("user", user.id, display_name(user), user.org_id),
        role_slug=role.slug,
        scope=resolve_scope(session, SCOPE_ORG, user.org_id),
    )
    return row
