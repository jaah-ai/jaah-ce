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

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import org_invitations
from app.access import (
    EVERYONE_FIXED_DETAIL,
    EVERYONE_MANAGED_DETAIL,
    ORG_OWNER,
    OWNER_SUBJECT_DETAIL,
    may_edit_user,
)
from app.clock import utcnow
from app.access_catalog import ROLES_BY_SLUG
from app.actors import actor_names, stamp_actor
from app.auth import adopt_user, require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature, org_admin
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.db_async import DbDep, on_session, release
from app.entity_shape import commit_row, flush_or_409, from_row, get_or_404, partial_of
from app.list_page import Page, PageQuery, Q_MAX, like_pattern, page_query, sql_page
from app.access_admin import (
    BYPASS_GROUP_NAME,
    is_bypass_group_name,
    refuse_foreign_org,
    revoke_direct_grants_current_epoch,
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
            or_(RoleBinding.expires_at.is_(None), RoleBinding.expires_at > utcnow()),
        ).limit(1)
    ) is not None:
        raise HTTPException(status_code=403, detail=_OWNER_SUBJECT_DETAIL)


def _reject_reserved_group(name: str | None) -> None:
    if is_bypass_group_name(name):
        raise HTTPException(status_code=400, detail=_RESERVED_RENAME_DETAIL)


# Named explicitly in the migration: the classifier keys on it. Per org, case-insensitive.
GROUP_NAME_CONSTRAINT = "uq_groups_org_id_lower_name"
GROUP_NAME_TAKEN_DETAIL = "A group with this name already exists in this organization."


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


class GroupUpdate(partial_of(GroupCreate, exclude=("org_id",))):  # type: ignore[misc,valid-type]  # generated base
    """Partial patch on a PUT via `model_fields_set`: only fields the caller sent change."""


class _GroupOut(BaseModel):
    """What a list row and a write's response say about a group alike."""

    id: UUID
    name: str
    description: str
    # The org's Everyone group (G8): every user is a member, and it cannot be renamed, deleted or edited.
    is_everyone: bool = False
    # Created by the org's identity provider over SCIM, which overwrites console edits at its next sync.
    scim_managed: bool = False
    # Resolved display names; `None` only if the actor row has vanished.
    created_by: str | None = None
    updated_by: str | None = None


class GroupSummary(_GroupOut):
    """One row of `GET /api/groups`, with its member count so the list never fans out."""

    member_count: int
    # The group's own live role bindings; zero means the group confers nothing.
    binding_count: int = 0
    # The owning org (`app/tenancy/labels.py::OrgRef`), for the operator's All-orgs list.
    org_id: UUID | None = None
    org: OrgRef | None = None


class MemberOut(BaseModel):
    id: UUID  # an `admin_users` id
    email: str
    first_name: str | None = None
    last_name: str | None = None


class GroupDetail(_GroupOut):
    """The response every group write returns, so a screen never re-fetches after a save."""

    # The org the group belongs to: an operator's editor scopes its pickers by it without walking every org.
    org_id: UUID
    # Distinct users, as `GroupSummary.member_count`; the roster itself is `GET /api/groups/{id}/members`.
    member_count: int = 0


# ── shared readers ────────────────────────────────────────────────────────────────────────────────


def _member_count(session: Session, group_id: UUID) -> int:
    # DISTINCT: legacy scoped rows can duplicate `(user_id, group_id)`.
    return session.scalar(
        select(func.count(func.distinct(UserGroupMembership.user_id))).where(UserGroupMembership.group_id == group_id)
    ) or 0


def _group_detail(session: Session, group: Group) -> GroupDetail:
    # One row, so resolving its two actor ids is one query; `list_groups` resolves a whole page instead.
    names = actor_names(session, (group.created_by_id, group.updated_by_id))
    return from_row(
        GroupDetail, group, is_everyone=bool(group.is_everyone), member_count=_member_count(session, group.id),
        created_by=names.get(group.created_by_id), updated_by=names.get(group.updated_by_id),
    )


def _require_group(session: Session, group_id: UUID, *, lock: bool = False) -> Group:
    """The group, or a 404.

    `lock=True` (every full-replace verb): without the row lock, two concurrent replaces under READ
    COMMITTED both re-insert overlapping sets and the second 500s on the uniqueness constraint."""
    return get_or_404(session, Group, group_id, detail="unknown group", lock=lock)


# ── group CRUD ────────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=Page[GroupSummary])
@feature("groups", "view", tenancy=Tenancy.CALLER_PLANE)
@on_session
def list_groups(
    session: Session,
    q: str | None = Query(default=None, max_length=Q_MAX),
    query: PageQuery = Depends(page_query(("name", "members", "bindings"))),
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
            or_(RoleBinding.expires_at.is_(None), RoleBinding.expires_at > utcnow()),
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
@on_session
def create_group(session: Session, body: GroupCreate, actor: AdminUser = Depends(require_user)) -> GroupDetail:
    actor = adopt_user(session, actor)
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
    # Flush-then-catch: the constraint is the only race-free decider, and a clash can only be the caller's own org's.
    commit_row(session, group, taken=GROUP_NAME_TAKEN_DETAIL, constraints=frozenset({GROUP_NAME_CONSTRAINT}))
    return _group_detail(session, group)


@router.get("/{group_id}", response_model=GroupDetail)
@feature("groups", "view", tenancy=Tenancy.CALLER_PLANE)
@on_session
def get_group(session: Session, group_id: UUID) -> GroupDetail:
    return _group_detail(session, _require_group(session, group_id))


@router.get("/{group_id}/members", response_model=Page[MemberOut])
@feature("groups", "view", tenancy=Tenancy.CALLER_PLANE)
@on_session
def list_group_members(
    session: Session,
    group_id: UUID,
    q: str | None = Query(default=None, max_length=Q_MAX),
    query: PageQuery = Depends(page_query(("email", "name"))),
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
@on_session
def update_group(
    session: Session,
    group_id: UUID,
    body: GroupUpdate,
    caller: AsyncCaller = Depends(current_caller),
) -> GroupDetail:
    actor = adopt_user(session, caller.actor)
    group = _require_group(session, group_id)
    refuse_owner_subject(caller.principal, session, group_id=group_id)
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
    flush_or_409(session, GROUP_NAME_TAKEN_DETAIL, constraints=frozenset({GROUP_NAME_CONSTRAINT}))
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
    commit_row(session, group)
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
async def delete_group(
    db: DbDep,
    group_id: UUID,
    caller: AsyncCaller = Depends(current_caller),
) -> Response:
    """Delete a group — 204, no body; its `user_group_memberships` and `role_bindings` rows go with it.
    400 on the reserved group, 404 if it does not exist.

    The cascade is the database's (`ON DELETE CASCADE` on both FKs), not this handler's."""
    demoted, actor_id = await db.run_sync(_delete_group, group_id, caller.actor, caller.principal)
    await after_narrowing(db, demoted, actor_id)
    # Not `-> None`: FastAPI would derive a response model and assert on a bodiless 204.
    return Response(status_code=204)


def _delete_group(
    session: Session,
    group_id: UUID,
    actor: AdminUser,
    principal: Principal | None,
) -> tuple[list[UUID], UUID]:
    """`delete_group`'s body up to its commit, over the route's async session (`run_sync`): the members it
    demoted and the actor's id, for the owed revokes (`after_narrowing`)."""
    actor = adopt_user(session, actor)
    # `lock=True` closes a security window: a concurrent membership insert (FK insert takes FOR KEY
    # SHARE on the group) would otherwise be cascaded away without reaching `demoted`, leaving that
    # member's writable direct console alive for its TTL.
    group = _require_group(session, group_id, lock=True)
    demoted = delete_group_row(session, actor, principal, group, defer=True)
    return demoted or [], actor.id


def delete_group_row(
    session: Session, actor: AdminUser, principal: Principal | None, group: Group, *, defer: bool = False
) -> list[UUID] | None:
    """`DELETE /api/groups/{id}` past its lookup — the route and a SCIM delete (`app/scim.py`): 400 the reserved
    group, 409 Everyone, 403 an owner group for a non-owner; committed, then the owed revokes. `group` is read
    `FOR UPDATE` by the caller. `defer`: skip the revokes and return the demoted ids instead — an async route
    owes them itself, off its `run_sync` (`after_narrowing`)."""
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
    if defer:
        return demoted
    revoke_direct_grants_current_epoch(session, demoted)
    return None


def remove_member(
    session: Session, actor: AdminUser, principal: Principal | None, group: Group, user: AdminUser
) -> bool:
    """Stage removing every row joining `user` to `group` (legacy scoped ones included) and its audit row; False
    (nothing staged) when they are not a member. 403 as `add_member`. The caller commits, then owes
    `revoke_direct_grants_current_epoch` for the user."""
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
@on_session
def add_group_member(
    session: Session,
    group_id: UUID,
    user_id: UUID,
    caller: AsyncCaller = Depends(current_caller),
) -> GroupDetail:
    """Add one member; idempotent (an existing member is left as is). Returns the full `GroupDetail`.
    Adding a member to a group holding `org_owner` makes an owner, so only an owner may."""
    actor = adopt_user(session, caller.actor)
    group, user = _member_subject(session, group_id, user_id)
    if add_member(session, actor, caller.principal, group, user):
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
async def remove_group_member(
    db: DbDep,
    group_id: UUID,
    user_id: UUID,
    caller: AsyncCaller = Depends(current_caller),
) -> Response:
    """Remove one member — every row joining them to the group, legacy scoped ones included. 404 when
    they are not a member."""
    actor_id = await db.run_sync(_remove_group_member, group_id, user_id, caller.actor, caller.principal)
    # A removal narrows: the owed post-commit revokes, as `delete_group`.
    await after_narrowing(db, [user_id], actor_id)
    return Response(status_code=204)


def _remove_group_member(
    session: Session,
    group_id: UUID,
    user_id: UUID,
    actor: AdminUser,
    principal: Principal | None,
) -> UUID:
    """`remove_group_member`'s body up to its commit, over the route's async session (`run_sync`). Returns the
    actor's id, for the owed revokes."""
    actor = adopt_user(session, actor)
    group, user = _member_subject(session, group_id, user_id)
    if not remove_member(session, actor, principal, group, user):
        raise HTTPException(status_code=404, detail="not a member of this group")
    session.commit()
    return actor.id


async def after_narrowing(db: AsyncSession, user_ids: list[UUID], actor_id: UUID) -> None:
    """What a committed narrowing owes, for an async route: `revoke_direct_grants_current_epoch` (the epochs read
    on the route's session, the bus push on a worker thread — its sync bridge needs one, and no connection is held
    across it). The same as the sync callers (`delete_group_row`)."""
    pairs = await db.run_sync(_current_epochs, user_ids)
    await release(db)
    if pairs:
        # Deferred, as `app/access_admin.py` does: the node-bus graph stays out of route-module imports.
        from app import console_direct, node_bus

        # Shielded: a cancelled request must not drop a revoke its committed narrowing owes.
        await asyncio.shield(run_in_threadpool(console_direct.revoke_for_permission_change_sync, node_bus.registry, pairs))


def _current_epochs(session: Session, user_ids: list[UUID]) -> list[tuple[UUID, int]]:
    """`(id, session_epoch)` for each user — the pairs `revoke_direct_grants_current_epoch` reads and pushes."""
    unique = sorted(set(user_ids))
    if not unique:
        return []
    return [
        (row_id, epoch)
        for row_id, epoch in session.execute(
            select(AdminUser.id, AdminUser.session_epoch).where(AdminUser.id.in_(unique))
        ).all()
    ]


