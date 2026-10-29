"""The Access API: the built-in role catalog, read-only (`GET /api/access/roles[/{role_id}]`), and the role
bindings — who holds what at a scope, per-binding add/remove, the Add dialog's picker, what a user reaches,
and Check access (the section below the catalog; the decisions are `app/access.py`'s).

Read from the `roles`/`role_permissions` rows (the catalog is data), with `valid_scopes` from
`app/access_catalog.py`. What a role grants is served as the human sentence
`app/authz/catalog.py::permission_description` renders, never as a raw `feature:action` key.

No role write route exists, and none may: tenants cannot create, edit or delete a role, so every other method
on the role paths is a 405. `@always(Tenancy.BOOTSTRAP)` plus a session: any signed-in user may read it — a fixed
platform catalog that says nothing about any user or org (the `GET /api/permissions` argument).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import access, staging_org
from app.access_catalog import ROLES_BY_SLUG, SCOPES
from app.auth import require_user
from app.authz.catalog import PERMISSION_CATALOG, Tenancy, permission_description
from app.authz.declarations import always, feature
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.access_admin import revoke_direct_grants_current_epoch, revoke_orphaned_project_roles
from app.entity_shape import StrictModel, from_row, get_or_404
from app.identities import label_expr
from app.list_page import like_pattern
from app.models.access import Group, Permission, Role, RoleBinding, RolePermission, UserGroupMembership
from app.models.identity import AdminUser

router = APIRouter(prefix="/api/access", tags=["access"])

#: How many users (and, separately, groups) the Add dialog's picker returns per query.
_PICKER_LIMIT = 50

#: Catalog (feature, action) order, so capabilities read the way the permission matrix does.
_CATALOG_ORDER = {spec.key: index for index, spec in enumerate(PERMISSION_CATALOG)}


class RoleOut(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str
    level: str
    rank: int
    valid_scopes: list[str]
    #: What the role lets its holder do, one sentence per permission, in catalog order.
    capabilities: list[str]


def _roles(session: Session, role_id: UUID | None = None) -> list[RoleOut]:
    query = select(Role).order_by(Role.rank.desc())
    if role_id is not None:
        query = query.where(Role.id == role_id)
    roles = session.scalars(query).all()
    keys: dict[UUID, list[str]] = defaultdict(list)
    for rid, key in session.execute(
        select(RolePermission.role_id, Permission.key)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(RolePermission.role_id.in_([role.id for role in roles]))
    ):
        keys[rid].append(key)
    out = []
    for role in roles:
        catalog = ROLES_BY_SLUG.get(role.slug)
        out.append(from_row(
            RoleOut, role,
            valid_scopes=list(catalog.valid_scopes) if catalog else [],
            capabilities=[
                permission_description(*key.split(":", 1))
                for key in sorted(keys[role.id], key=lambda k: _CATALOG_ORDER.get(k, len(_CATALOG_ORDER)))
            ],
        ))
    return out


@router.get("/roles", response_model=list[RoleOut])
@always(Tenancy.BOOTSTRAP)
def list_roles(
    _user: AdminUser = Depends(require_user), session: Session = Depends(get_session)
) -> list[RoleOut]:
    """Every built-in role, highest rank first."""
    return _roles(session)


@router.get("/roles/{role_id}", response_model=RoleOut)
@always(Tenancy.BOOTSTRAP)
def get_role(
    role_id: UUID, _user: AdminUser = Depends(require_user), session: Session = Depends(get_session)
) -> RoleOut:
    found = _roles(session, role_id)
    if not found:
        raise HTTPException(status_code=404, detail="role not found")
    return found[0]


# ── Role bindings: members, per-binding writes, the picker, reach and the explainer (C4) ──────────
#
# Every decision is `app/access.py`'s (`may_manage`, `add_binding`, `remove_binding`, `explain`); these
# handlers resolve the wire form and commit. DECLARED ON A FLOOR KEY, `Tenancy.CALLER_PLANE`: the guard
# resolves the principal and binds their org (an operator fleet-wide), and the handler narrows. Writes
# and the picker floor on `projects:edit` — the least key any delegated admin holds (a Project
# Maintainer's; an Org Owner/Admin holds every key). Reads floor on `projects:view`, which every role
# confers somewhere, so a caller with no role at all is refused before anything is read.


class ScopeIn(BaseModel):
    """A binding or access-request scope. Its `type` is one of `access_catalog.SCOPES`, else a 422."""

    type: str
    id: str

    @field_validator("type")
    @classmethod
    def _known_scope(cls, value: str) -> str:
        if value not in SCOPES:
            raise ValueError(f"scope type is one of {', '.join(SCOPES)}")
        return value


class PrincipalIn(BaseModel):
    kind: str
    id: UUID


class BindingsIn(StrictModel):
    principals: list[PrincipalIn] = Field(min_length=1, max_length=100)
    role: str
    scope: ScopeIn
    #: Never, by default (D2). An org-scope binding carries none.
    expires_at: datetime | None = None


class BindingOut(BaseModel):
    id: UUID
    role: str
    principal: dict
    scope: dict
    expires_at: datetime | None
    #: False when the binding already existed (the write is idempotent).
    created: bool


class MemberPrincipal(BaseModel):
    kind: str
    id: UUID
    name: str
    member_count: int | None = None


class MemberSource(BaseModel):
    kind: str
    via_id: str | None = None
    via_name: str | None = None


class MemberRole(BaseModel):
    slug: str
    name: str


class MemberOut(BaseModel):
    principal: MemberPrincipal
    role: MemberRole
    source: MemberSource
    binding_id: UUID
    expires_at: datetime | None
    status: str
    last_active_at: datetime | None
    editable: bool


class PickerEntry(BaseModel):
    kind: str
    id: UUID
    name: str
    email: str | None = None
    member_count: int | None = None


class ActionIn(BaseModel):
    """A route class of the permission catalog — a `(feature, action)` pair, never a joined key."""

    feature: str
    action: str


class CheckIn(BaseModel):
    user_id: UUID
    resource: ScopeIn
    action: ActionIn


def _scope(session: Session, raw: str) -> access.Scope:
    """The `<type>:<id>` query form, resolved (404 when it names nothing this request reads)."""
    scope_type, sep, scope_id = raw.partition(":")
    if not sep:
        raise HTTPException(status_code=422, detail="scope is <type>:<id>")
    return access.resolve_scope(session, scope_type, scope_id)


def _self_or_org_admin(principal: Principal | None, user_id: UUID) -> None:
    if principal is None or (principal.user_id != user_id and not principal.is_org_admin):
        raise HTTPException(status_code=403, detail="forbidden")


def _target_user(session: Session, user_id: UUID) -> AdminUser:
    return get_or_404(session, AdminUser, user_id, detail="unknown user", where=(AdminUser.is_system.is_(False),))


@router.get("/members", response_model=list[MemberOut])
@feature("projects", "view", tenancy=Tenancy.CALLER_PLANE)
def list_members(
    scope: str,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[dict]:
    """Who holds a role at `scope` (`<type>:<id>`), inheritance and groups expanded, each row labelled
    with its source. Readable by anyone who sees the scope, or may manage it."""
    resolved = _scope(session, scope)
    if not (access.may_read_scope(principal, resolved) or access.may_manage_any(principal, resolved)):
        raise HTTPException(status_code=403, detail="forbidden")
    expand_groups = principal.has_org("users:view") or access.may_manage_any(principal, resolved)
    return access.members(session, resolved, actor_principal=principal, expand_groups=expand_groups)


@router.post("/bindings", response_model=list[BindingOut], status_code=201)
@feature("projects", "edit", tenancy=Tenancy.CALLER_PLANE)
def add_bindings(
    body: BindingsIn,
    actor: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[BindingOut]:
    """Bind each of `principals` to `role` at `scope` — one row each, idempotent (a binding that already
    exists is returned with `created: false`). All or nothing: any refusal writes no row."""
    scope = access.resolve_scope(session, body.scope.type, body.scope.id)
    out: list[BindingOut] = []
    for entry in body.principals:
        holder = access.resolve_holder(session, entry.kind, entry.id)
        row, created = access.add_binding(
            session, actor, principal,
            principal=holder, role_slug=body.role, scope=scope, expires_at=body.expires_at,
        )
        out.append(BindingOut(
            id=row.id, role=body.role, expires_at=row.expires_at, created=created,
            principal={"kind": holder.kind, "id": str(holder.id), "name": holder.name},
            scope=scope.wire,
        ))
    session.commit()
    if scope.type == access.SCOPE_ORG:
        staging_org.after_parent_change(scope.org_id)
    return out


@router.delete("/bindings/{binding_id}", status_code=204)
@feature("projects", "edit", tenancy=Tenancy.CALLER_PLANE)
def delete_binding(
    binding_id: UUID,
    actor: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Remove one binding. 409 when it is the org's last direct Org Owner."""
    # A direct org-scope binding is what a staging org copies (`app/staging_org.py`); read before it goes.
    org_id = session.scalar(select(RoleBinding.scope_org_id).where(
        RoleBinding.id == binding_id, RoleBinding.user_id.is_not(None),
    ))
    affected = access.remove_binding(session, actor, principal, binding_id)
    session.commit()
    if org_id is not None:
        staging_org.after_parent_change(org_id)
    # A removal narrows: the same post-commit revokes a membership removal owes (`routes/users.py`).
    revoke_direct_grants_current_epoch(session, affected)
    if revoke_orphaned_project_roles(session, affected, actor_id=actor.id):
        session.commit()
    return Response(status_code=204)


@router.get("/principals", response_model=list[PickerEntry])
@feature("projects", "edit", tenancy=Tenancy.CALLER_PLANE)
def list_principals(
    scope: str,
    q: str = "",
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[PickerEntry]:
    """The Add dialog's picker: the users and groups of the scope's org a caller who may manage it can
    bind — id and name only (+ email, member count), never the `users:view` directory. Everyone is
    offered to an Org Owner/Admin only."""
    resolved = _scope(session, scope)
    if not access.may_manage_any(principal, resolved):
        raise HTTPException(status_code=403, detail="forbidden")
    # LIKE wildcards in the search are literal characters.
    needle = like_pattern(q)
    label = label_expr()
    users = session.execute(
        select(AdminUser.id, label, AdminUser.email)
        .where(
            AdminUser.org_id == resolved.org_id, AdminUser.is_system.is_(False),
            or_(label.ilike(needle, escape="\\"), AdminUser.email.ilike(needle, escape="\\")),
        )
        .order_by(label).limit(_PICKER_LIMIT)
    ).all()
    counts = (
        select(UserGroupMembership.group_id, func.count(func.distinct(UserGroupMembership.user_id)).label("n"))
        .where(UserGroupMembership.org_id == resolved.org_id)
        .group_by(UserGroupMembership.group_id).subquery()
    )
    group_query = (
        select(Group.id, Group.name, counts.c.n)
        .outerjoin(counts, counts.c.group_id == Group.id)
        .where(Group.org_id == resolved.org_id, Group.name.ilike(needle, escape="\\"))
        .order_by(Group.name).limit(_PICKER_LIMIT)
    )
    if not principal.is_org_admin:
        group_query = group_query.where(Group.is_everyone.is_(False))
    groups = session.execute(group_query).all()
    return [
        *(PickerEntry(kind="user", id=uid, name=name, email=email) for uid, name, email in users),
        *(PickerEntry(kind="group", id=gid, name=name, member_count=n or 0) for gid, name, n in groups),
    ]


@router.get("/reaches/{user_id}")
@feature("projects", "view", tenancy=Tenancy.CALLER_PLANE)
def get_reaches(
    user_id: UUID,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> dict:
    """What `user_id` reaches, grouped org → project, plus accounts. Self, or an Org Admin."""
    _self_or_org_admin(principal, user_id)
    return access.reaches(session, _target_user(session, user_id).id)


@router.post("/check")
@feature("projects", "view", tenancy=Tenancy.CALLER_PLANE)
def check_access(
    body: CheckIn,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> dict:
    """Check access: may `user_id` do `action` to `resource` — the chain that allows it, or the nearest
    missing link. Self, or an Org Admin. `access.explain` is the one home."""
    _self_or_org_admin(principal, body.user_id)
    user = _target_user(session, body.user_id)
    resource = access.resolve_scope(session, body.resource.type, body.resource.id)
    if not principal.is_org_admin and not access.may_read_scope(principal, resource):
        # A self-check names no resource its caller cannot see: that would be a name (and existence)
        # oracle over the whole org.
        raise HTTPException(status_code=404, detail="unknown scope")
    return access.explain(session, user, resource, body.action.feature, body.action.action)
