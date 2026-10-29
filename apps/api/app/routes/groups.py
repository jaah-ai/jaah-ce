"""Group administration — the admin API behind the Directory's Groups tab.

Group CRUD plus per-member add/remove. Access is role bindings, written through `/api/access`; there is no
permission-key, object-access or whole-set members write (AC-5.3: no route replaces a collection).

`Tenancy.CALLER_PLANE` throughout: an operator works every org, an Org Owner/Admin their own (the seam
narrows every read, so another org's group or user id is a 404). Reads are `groups:view`, so a key holder
sees the same page read-only. Writes are `@org_admin` (G4) and no feature key reaches one: a grantable
capability conferring group administration would let its holder grant themselves the whole catalog.

Audit: `record_write` (same session, raises on failure) where the route records a value no request or
response carries; `create_group` relies on `UserActivityMiddleware` (after the response, best-effort, so
its audit row can be lost).

Never the dead `UserGroupMembership.resource_type`/`resource_id`."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import org_invitations
from app.access import (
    EVERYONE_FIXED_DETAIL,
    EVERYONE_MANAGED_DETAIL,
    ORG_OWNER,
    OWNER_SUBJECT_DETAIL,
    may_edit_user,
)
from app.access_catalog import ROLES_BY_SLUG
from app.actors import actor_names, stamp_actor
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature, org_admin
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.entity_shape import flush_or_409, get_or_404
from app.list_page import Page, PageQuery, Q_MAX, like_pattern, page_query, sql_page
from app.access_admin import (
    BYPASS_GROUP_NAME,
    is_bypass_group_name,
    refuse_foreign_org,
    revoke_direct_grants_current_epoch,
    revoke_orphaned_project_roles,
)
from app.models.access import Group, RoleBinding, UserGroupMembership
from app.models.identity import AdminUser
from app.models.orgs import OrgInvitation
from app.tenancy.create_surface import named_org
from app.tenancy.labels import OrgRef, org_refs
from app.user_activity import record_write

router = APIRouter(prefix="/api/groups", tags=["groups"])

# The seeded group whose name implies a bypass it cannot confer (only `is_superadmin` grants bypass).
# The member writes refuse it like `POST /api/users/{id}/memberships` does; real bypass is
# `PATCH /api/users/{id}`.
_BYPASS_GROUP_NAME = BYPASS_GROUP_NAME  # single home: app/access_admin.py
_BYPASS_GROUP_DETAIL = (
    "cannot manage members of the superadmin group; it confers no bypass "
    "(use PATCH /api/users/{id} to toggle the is_superadmin bypass)"
)
# The guard is keyed on the name, so the name is refused on every write verb (a rename round-trip would
# defeat it), case-insensitively (`access_admin.is_bypass_group_name`).
_RESERVED_RENAME_DETAIL = (
    f"the {_BYPASS_GROUP_NAME} group is reserved: it cannot be renamed, deleted, or created by "
    "renaming another group onto its name"
)


_OWNER_SUBJECT_DETAIL = OWNER_SUBJECT_DETAIL


def refuse_owner_subject(
    principal: Principal | None,
    session: Session,
    *,
    user_id: UUID | None = None,
    group_id: UUID | None = None,
) -> None:
    """403 when a caller below Org Owner (an Org Admin) would change an owner: the user `user_id` if they
    are an Org Owner, or the group `group_id` if it holds an org-wide `org_owner` binding. Org Owner
    outranks Org Admin (`access_catalog`), and owns who else is an owner. An operator or an Org Owner
    passes; the subject is only resolved for anyone else. The one home for this rule (also
    `routes/users.py`)."""
    if principal is not None and (principal.is_operator or principal.org_role == ORG_OWNER):
        return
    if user_id is not None and not may_edit_user(session, principal, user_id):
        raise HTTPException(status_code=403, detail=_OWNER_SUBJECT_DETAIL)
    if group_id is not None and session.scalar(
        select(RoleBinding.id).where(
            RoleBinding.group_id == group_id,
            RoleBinding.role_id == ROLES_BY_SLUG["org_owner"].id,
            RoleBinding.scope_org_id.is_not(None),
            or_(RoleBinding.expires_at.is_(None), RoleBinding.expires_at > datetime.now(timezone.utc)),
        ).limit(1)
    ) is not None:
        raise HTTPException(status_code=403, detail=_OWNER_SUBJECT_DETAIL)


def _reject_reserved_group(name: str | None) -> None:
    if is_bypass_group_name(name):
        raise HTTPException(status_code=400, detail=_RESERVED_RENAME_DETAIL)


# Named explicitly in the migration: the classifier keys on it. Per org, case-insensitive.
GROUP_NAME_CONSTRAINT = "uq_groups_org_id_lower_name"
GROUP_NAME_TAKEN_DETAIL = "A group with this name already exists in this organization."


def _flush_or_name_conflict(session: Session) -> None:
    """`uq_groups_org_id_lower_name` as a 409; anything else re-raises (`flush_or_409`). Flush-then-catch, since
    the constraint is the only race-free decider. The name is unique within the org only, so the refusal names
    the clash: it can only be the caller's own org's group."""
    flush_or_409(session, GROUP_NAME_TAKEN_DETAIL, constraints=frozenset({GROUP_NAME_CONSTRAINT}))


# ── request models ────────────────────────────────────────────────────────────────────────────────


class GroupCreate(BaseModel):
    # An edge bound; `Group.name` is unbounded `Text`.
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    # Create-only (moving org would carry permissions and memberships). An operator's create stamps
    # nothing, so it names its org explicitly; no default
    # (`app/tenancy/write_seam.py::_refuses_unstamped_create`). A tenant must name their own org
    # (`access_admin.refuse_foreign_org`). Child join rows derive org from `Group`.
    org_id: UUID = Field(description="The org this group belongs to.")


class GroupUpdate(BaseModel):
    """Partial patch on a PUT via `model_fields_set`: only fields the caller sent change."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None


class GroupSummary(BaseModel):
    """One row of `GET /api/groups`, with its member count so the list never fans out."""

    id: UUID
    name: str
    description: str
    member_count: int
    # The group's own live role bindings; zero means the group confers nothing.
    binding_count: int = 0
    # The org's Everyone group (G8): every user is a member, and it cannot be renamed, deleted or edited.
    is_everyone: bool = False
    # Created by the org's identity provider over SCIM, which overwrites console edits at its next sync.
    scim_managed: bool = False
    # Resolved display names; `None` only if the actor row has vanished.
    created_by: str | None = None
    updated_by: str | None = None
    # The owning org (`app/tenancy/labels.py::OrgRef`), for the operator's All-orgs list.
    org_id: UUID | None = None
    org: OrgRef | None = None


class MemberOut(BaseModel):
    id: UUID  # an `admin_users` id
    email: str
    first_name: str | None = None
    last_name: str | None = None


class GroupDetail(BaseModel):
    """The response every group write returns, so a screen never re-fetches after a save."""

    id: UUID
    name: str
    description: str
    # The org the group belongs to: an operator's editor scopes its pickers by it without walking every org.
    org_id: UUID
    is_everyone: bool = False
    scim_managed: bool = False
    # Distinct users, as `GroupSummary.member_count`; the roster itself is `GET /api/groups/{id}/members`.
    member_count: int = 0
    created_by: str | None = None
    updated_by: str | None = None


# ── shared readers ────────────────────────────────────────────────────────────────────────────────


def _member_count(session: Session, group_id: UUID) -> int:
    # DISTINCT: legacy scoped rows can duplicate `(user_id, group_id)`.
    return session.scalar(
        select(func.count(func.distinct(UserGroupMembership.user_id))).where(UserGroupMembership.group_id == group_id)
    ) or 0


def _group_detail(session: Session, group: Group) -> GroupDetail:
    # One row, so resolving its two actor ids is one query; `list_groups` resolves a whole page instead.
    names = actor_names(session, (group.created_by_id, group.updated_by_id))
    return GroupDetail(
        id=group.id,
        name=group.name,
        description=group.description,
        org_id=group.org_id,
        is_everyone=bool(group.is_everyone),
        scim_managed=group.scim_managed,
        member_count=_member_count(session, group.id),
        created_by=names.get(group.created_by_id),
        updated_by=names.get(group.updated_by_id),
    )


def _require_group(session: Session, group_id: UUID, *, lock: bool = False) -> Group:
    """The group, or a 404.

    `lock=True` (every full-replace verb): without the row lock, two concurrent replaces under READ
    COMMITTED both re-insert overlapping sets and the second 500s on the uniqueness constraint."""
    return get_or_404(session, Group, group_id, detail="unknown group", lock=lock)


# ── group CRUD ────────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=Page[GroupSummary])
@feature("groups", "view", tenancy=Tenancy.CALLER_PLANE)
def list_groups(
    q: str | None = Query(default=None, max_length=Q_MAX),
    query: PageQuery = Depends(page_query(("name", "members", "bindings"))),
    session: Session = Depends(get_session),
) -> Page[GroupSummary]:
    """One page of groups with their member and binding counts; default order name.

    The counts are outer-joined grouped subqueries, so they sort in SQL and the page is a fixed number of
    statements. `member_count` counts distinct users, as `_member_count` does."""
    member_counts = (
        select(UserGroupMembership.group_id, func.count(func.distinct(UserGroupMembership.user_id)).label("n"))
        .group_by(UserGroupMembership.group_id).subquery()
    )
    binding_counts = (
        select(RoleBinding.group_id, func.count(RoleBinding.id).label("n"))
        .where(
            RoleBinding.group_id.is_not(None),
            or_(RoleBinding.expires_at.is_(None), RoleBinding.expires_at > datetime.now(timezone.utc)),
        )
        .group_by(RoleBinding.group_id).subquery()
    )
    members = func.coalesce(member_counts.c.n, 0)
    bindings = func.coalesce(binding_counts.c.n, 0)
    stmt = (
        select(Group, members, bindings)
        .outerjoin(member_counts, member_counts.c.group_id == Group.id)
        .outerjoin(binding_counts, binding_counts.c.group_id == Group.id)
    )
    if q and q.strip():
        needle = like_pattern(q)
        stmt = stmt.where(or_(Group.name.ilike(needle, escape="\\"), Group.description.ilike(needle, escape="\\")))
    rows, total = sql_page(
        session, stmt, query,
        sorts={"name": Group.name, "members": members, "bindings": bindings},
        default=[Group.name.asc()],
        tie=Group.id,
    )
    # All actor ids on the page resolved in one query.
    names = actor_names(
        session,
        [group.created_by_id for group, _, _ in rows] + [group.updated_by_id for group, _, _ in rows],
    )
    orgs = org_refs(session, [group.org_id for group, _, _ in rows])
    return Page(
        items=[
            GroupSummary(
                id=group.id,
                name=group.name,
                description=group.description,
                member_count=member_count,
                binding_count=binding_count,
                is_everyone=bool(group.is_everyone),
                scim_managed=group.scim_managed,
                created_by=names.get(group.created_by_id),
                updated_by=names.get(group.updated_by_id),
                org_id=group.org_id,
                org=orgs.get(group.org_id),
            )
            for group, member_count, binding_count in rows
        ],
        total=total, page=query.page, per=query.per,
    )


@router.post("", response_model=GroupDetail, status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
def create_group(
    body: GroupCreate,
    actor: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> GroupDetail:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    # Before the uniqueness check, so the reserved name answers 400, not 409.
    _reject_reserved_group(name)
    refuse_foreign_org(body.org_id)
    # `''`, never `None`: `description` is NOT NULL.
    group = Group(
        name=name,
        description=(body.description or "").strip(),
        # Set explicitly: an operator's request binds no org for the write seam.
        org_id=named_org(session, body.org_id),
    )
    stamp_actor(group, actor, created=True)
    session.add(group)
    _flush_or_name_conflict(session)
    session.commit()
    session.refresh(group)
    return _group_detail(session, group)


@router.get("/{group_id}", response_model=GroupDetail)
@feature("groups", "view", tenancy=Tenancy.CALLER_PLANE)
def get_group(
    group_id: UUID,
    session: Session = Depends(get_session),
) -> GroupDetail:
    return _group_detail(session, _require_group(session, group_id))


@router.get("/{group_id}/members", response_model=Page[MemberOut])
@feature("groups", "view", tenancy=Tenancy.CALLER_PLANE)
def list_group_members(
    group_id: UUID,
    q: str | None = Query(default=None, max_length=Q_MAX),
    query: PageQuery = Depends(page_query(("email", "name"))),
    session: Session = Depends(get_session),
) -> Page[MemberOut]:
    """One page of a group's members, read as `get_group` is; default order the name label."""
    group = _require_group(session, group_id)
    # The People rule: a row waiting on an invitation is found, ordered and named by what the inviter typed.
    label = org_invitations.shown_label()
    # IN, not a join: legacy scoped rows can duplicate `(user_id, group_id)`, and each member is one row. Bound to
    # the group's org, as every member is: the email search then reads one membership per person, never an
    # arbitrary one (`tests/test_identity_email_census.py`).
    stmt = select(AdminUser).outerjoin(OrgInvitation, OrgInvitation.id == AdminUser.invitation_id).where(
        AdminUser.org_id == group.org_id,
        AdminUser.id.in_(select(UserGroupMembership.user_id).where(UserGroupMembership.group_id == group_id)),
    )
    if q and q.strip():
        needle = like_pattern(q)
        stmt = stmt.where(or_(label.ilike(needle, escape="\\"), AdminUser.email.ilike(needle, escape="\\")))
    users, total = sql_page(
        session, stmt, query,
        sorts={"email": AdminUser.email, "name": label},
        default=[label.asc()],
        tie=AdminUser.id,
    )
    states = org_invitations.invitation_states(session, list(users))
    return Page(
        items=[
            MemberOut(id=user.id, email=user.email, first_name=first, last_name=last)
            for user in users
            for first, last in [org_invitations.shown_names(user, states)]
        ],
        total=total, page=query.page, per=query.per,
    )


@router.put("/{group_id}", response_model=GroupDetail)
@org_admin(Tenancy.CALLER_PLANE)
def update_group(
    group_id: UUID,
    body: GroupUpdate,
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> GroupDetail:
    group = _require_group(session, group_id)
    refuse_owner_subject(principal, session, group_id=group_id)
    provided = body.model_fields_set
    changed: list[str] = []
    name_from_to: tuple[str, str] | None = None
    if "name" in provided and body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        if name != group.name:
            if group.is_everyone:
                raise HTTPException(status_code=409, detail=EVERYONE_FIXED_DETAIL)
            # Neither direction: renaming the reserved group away, nor renaming onto its name.
            _reject_reserved_group(group.name)
            _reject_reserved_group(name)
            name_from_to = (group.name, name)
            group.name = name
            changed.append("name")
    if "description" in provided:
        # `''` for a cleared field (NOT NULL).
        description = (body.description or "").strip()
        if description != group.description:
            group.description = description
            changed.append("description")
    if changed:
        # A no-op PUT must not move `updated_by_id`/`updated_at`.
        stamp_actor(group, actor)
    _flush_or_name_conflict(session)
    # No audit row for a no-op PUT.
    if changed:
        # The old→new pair: memberships resolve groups by name, so the trail needs both.
        data: dict = {"group_id": group_id, "name": group.name, "fields": changed}
        if name_from_to is not None:
            data["name_from"], data["name_to"] = name_from_to
        record_write(
            session, actor, "group_update",
            f"Updated group {group.name} ({', '.join(changed)})",
            data=data,
        )
    session.commit()
    session.refresh(group)
    return _group_detail(session, group)


def _member_ids(session: Session, group_id: UUID) -> list[UUID]:
    """The user ids currently joined to this group (distinct: legacy scoped rows duplicate)."""
    return list(session.scalars(
        select(UserGroupMembership.user_id)
        .where(UserGroupMembership.group_id == group_id)
        .distinct()
    ).all())


@router.delete("/{group_id}", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
def delete_group(
    group_id: UUID,
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Delete a group — 204, no body; its `user_group_memberships` and `role_bindings` rows go with it.
    400 on the reserved group, 404 if it does not exist.

    The cascade is the database's (`ON DELETE CASCADE` on both FKs), not this handler's."""
    # `lock=True` closes a security window: a concurrent membership insert (FK insert takes FOR KEY
    # SHARE on the group) would otherwise be cascaded away without reaching `demoted`, leaving that
    # member's writable direct console alive for its TTL.
    group = _require_group(session, group_id, lock=True)
    delete_group_row(session, actor, principal, group)
    # Not `-> None`: FastAPI would derive a response model and assert on a bodiless 204.
    return Response(status_code=204)


def delete_group_row(session: Session, actor: AdminUser, principal: Principal | None, group: Group) -> None:
    """`DELETE /api/groups/{id}` past its lookup — the route and a SCIM delete (`app/scim.py`): 400 the reserved
    group, 409 Everyone, 403 an owner group for a non-owner; committed, then the owed revokes. `group` is read
    `FOR UPDATE` by the caller."""
    group_id = group.id
    _reject_reserved_group(group.name)
    if group.is_everyone:
        raise HTTPException(status_code=409, detail=EVERYONE_FIXED_DETAIL)
    # The cascade drops the group's `org_owner` binding too, so this is an owner change.
    refuse_owner_subject(principal, session, group_id=group_id)
    name = group.name
    # Snapshot before the delete (the cascade takes the membership rows); revoke after the commit
    # (`access_admin.revoke_direct_grants_current_epoch`).
    demoted = _member_ids(session, group_id)
    session.delete(group)
    record_write(
        session, actor, "group_delete", f"Deleted group {name}",
        data={"group_id": group_id, "name": name},
    )
    session.commit()
    revoke_direct_grants_current_epoch(session, demoted)
    # The cascade bypasses the binding writes, so this route owes the orphaned-role revoke itself, after
    # the commit.
    if revoke_orphaned_project_roles(session, demoted, actor_id=actor.id):
        session.commit()


def remove_member(
    session: Session, actor: AdminUser, principal: Principal | None, group: Group, user: AdminUser
) -> bool:
    """Stage removing every row joining `user` to `group` (legacy scoped ones included) and its audit row; False
    (nothing staged) when they are not a member. 403 as `add_member`. The caller commits, then owes
    `revoke_direct_grants_current_epoch` and `revoke_orphaned_project_roles` for the user."""
    rows = session.scalars(
        select(UserGroupMembership).where(
            UserGroupMembership.group_id == group.id, UserGroupMembership.user_id == user.id
        )
    ).all()
    if not rows:
        return False
    refuse_owner_subject(principal, session, group_id=group.id)
    for row in rows:
        session.delete(row)
    record_write(
        session, actor, "group_member_remove",
        f"Removed {user.email} from group {group.name}",
        data={"group_id": group.id, "name": group.name, "user_id": user.id, "email": user.email},
    )
    return True


# ── group membership, per member ──────────────────────────────────────────────────────────────────

# The member writes do not stamp the group row, deliberately: they write join rows with no actor columns
# (exempt from the entity actor columns). Who changed what is the audit trail's job.


def _member_subject(session: Session, group_id: UUID, user_id: UUID) -> tuple[Group, AdminUser]:
    """The group and user a per-member write names: 404 unknown, 400 the reserved bypass group, 409 the
    Everyone group (its membership is the system's), 422 a user of another org (the operator's seam
    reaches every org; a membership never crosses one)."""
    group = _require_group(session, group_id, lock=True)
    if is_bypass_group_name(group.name):
        raise HTTPException(status_code=400, detail=_BYPASS_GROUP_DETAIL)
    if group.is_everyone:
        raise HTTPException(status_code=409, detail=EVERYONE_MANAGED_DETAIL)
    user = get_or_404(session, AdminUser, user_id, detail="unknown user", where=(AdminUser.is_system.is_(False),))
    if user.org_id != group.org_id:
        raise HTTPException(status_code=422, detail="the user must belong to the group's organization")
    return group, user


@router.post("/{group_id}/members/{user_id}", response_model=GroupDetail, status_code=201)
@org_admin(Tenancy.CALLER_PLANE)
def add_group_member(
    group_id: UUID,
    user_id: UUID,
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> GroupDetail:
    """Add one member; idempotent (an existing member is left as is). Returns the full `GroupDetail`.
    Adding a member to a group holding `org_owner` makes an owner, so only an owner may."""
    group, user = _member_subject(session, group_id, user_id)
    if add_member(session, actor, principal, group, user):
        session.commit()
    return _group_detail(session, group)


def add_member(session: Session, actor: AdminUser, principal: Principal | None, group: Group, user: AdminUser) -> bool:
    """Stage `user` joining `group` and its audit row; False (nothing staged) when they are a member already. 403
    when the group holds `org_owner` and the caller is not an owner. The caller has checked both
    (`_member_subject`) and commits. The route and a SCIM write (`app/scim.py`)."""
    if user.id in set(_member_ids(session, group.id)):
        return False
    refuse_owner_subject(principal, session, group_id=group.id)
    session.add(UserGroupMembership(user_id=user.id, group_id=group.id))
    record_write(
        session, actor, "group_member_add",
        f"Added {user.email} to group {group.name}",
        data={"group_id": group.id, "name": group.name, "user_id": user.id, "email": user.email},
    )
    return True


@router.delete("/{group_id}/members/{user_id}", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
def remove_group_member(
    group_id: UUID,
    user_id: UUID,
    actor: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Remove one member — every row joining them to the group, legacy scoped ones included. 404 when
    they are not a member."""
    group, user = _member_subject(session, group_id, user_id)
    if not remove_member(session, actor, principal, group, user):
        raise HTTPException(status_code=404, detail="not a member of this group")
    session.commit()
    # A removal narrows: the owed post-commit revokes, as `delete_group`.
    revoke_direct_grants_current_epoch(session, [user_id])
    if revoke_orphaned_project_roles(session, [user_id], actor_id=actor.id):
        session.commit()
    return Response(status_code=204)
