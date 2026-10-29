"""What an access change owes, and the tenant create-surface org guard: the orphaned "Humans" role revoke,
the direct-console revokes, the reserved-group name check, and `refuse_foreign_org`.

One home so the user, group and access routes cannot drift apart. A module rather than a route helper
because `routes/` importing `routes/` becomes a cycle.

Nothing here authorizes: `app/access.py` (role bindings) and `app/authz/` own decisions."""
from __future__ import annotations

from typing import Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.identity import AdminUser
from app.models.projects import Project
from app.tenancy.seam import current_scope
from app.user_activity import record_write

# The seeded group whose name is reserved. It confers no bypass (only `admin_users.is_superadmin`
# does), so every write verb that could reach it refuses it by name, in `routes/groups.py` and
# `routes/users.py`; one literal so those doors cannot drift.
BYPASS_GROUP_NAME = "superadmin"

# The two "Humans" columns on `projects`, validated against project access on write and cleared with
# it on revoke; one list so both paths cover the same pair (`routes/projects.py` imports it).
PROJECT_ROLE_FIELDS = ("business_analyst_id", "architect_id")


def is_bypass_group_name(name: str | None) -> bool:
    """Does `name` name the reserved group? Case-insensitively: that is the guard, not a nicety.

    Letting `Superadmin` past seats a visually identical row in every group picker. One comparison so
    the refusal sites cannot disagree about case."""
    return name is not None and name.casefold() == BYPASS_GROUP_NAME.casefold()


def refuse_foreign_org(org_id: UUID) -> None:
    """422 when a tenant caller names an org other than their own on a create (users, groups, nodes, node
    groups, crons).

    A tenant superadmin's request is bound to their org (`Tenancy.CALLER_PLANE`); an operator's is
    `seam.OPERATOR` and may name any org. Runs before `create_surface.named_org`, whose 409 reads as a
    session conflict rather than bad input."""
    scope = current_scope()
    if isinstance(scope, UUID) and org_id != scope:
        raise HTTPException(status_code=422, detail="org_id must be your own organization")


def revoke_orphaned_project_roles(
    session: Session, user_ids: Sequence[UUID], *, actor_id: UUID | None = None
) -> list[tuple[int, str]]:
    """Clear `projects.business_analyst_id` / `architect_id` wherever the user can no longer read the
    project; returns the `(project_id, field)` pairs cleared. Audited, on the caller's session.

    Owed by every event that narrows project access: the role is who `notify_targets._referred_role` DMs
    and who the console shows as the project's human. Called by the binding writes (`routes/access.py`), the
    membership writes and group delete (`routes/groups.py`, `routes/users.py`) and the superadmin demote.

    The predicate is `authz.can_access(..., READ)`, what `routes/projects.py::_validated_project_role`
    admits on, so write and revoke agree on the operator plane. For a tenant actor that route drops the
    target's `is_superadmin` first, so they diverge there; no tenant can drive this (callers are
    `@superadmin_only(Tenancy.OPERATOR)`). A superadmin keeps every role, and a role justified by a group
    binding survives a direct-binding revoke. Nothing is committed or flushed here."""
    from app.authz.object_access import can_access
    from app.authz.principal import principal_for_user
    from app.authz.visibility import ObjectAccess

    unique = sorted(set(user_ids))
    if not unique:
        return []
    projects = session.scalars(
        select(Project)
        .where(
            or_(Project.business_analyst_id.in_(unique), Project.architect_id.in_(unique))
        )
        .order_by(Project.id)
    ).all()
    if not projects:
        return []

    # One `Principal` per user, not per (user, project): it costs three queries, none project-dependent.
    holders = {user.id: user for user in session.scalars(
        select(AdminUser).where(AdminUser.id.in_(unique))
    ).all()}
    principals = {
        uid: principal_for_user(session, user) for uid, user in holders.items()
    }

    actor = session.get(AdminUser, actor_id) if actor_id is not None else None
    cleared: list[tuple[int, str]] = []
    for project in projects:
        for field in PROJECT_ROLE_FIELDS:
            holder_id = getattr(project, field)
            if holder_id not in principals:
                continue
            if can_access(
                session, principals[holder_id], "project", project.id, mode=ObjectAccess.READ
            ):
                continue
            setattr(project, field, None)
            cleared.append((project.id, field))
            # Audited as a removal, one row per role, keyed on `project_id`. The email is the point of the row.
            record_write(
                session, actor, "project_role_revoked",
                f"Cleared {field} on project {project.name}: "
                f"{holders[holder_id].email} no longer has access to it",
                data={
                    "project_id": project.id, "field": field,
                    "user_id": holder_id, "email": holders[holder_id].email,
                },
            )
    return cleared


def revoke_direct_grants_current_epoch(session: Session, user_ids: Sequence[UUID]) -> None:
    """Drop the outstanding direct-console grants of every user whose effective permissions just changed.

    Owed by every route that narrows an effective set (role bindings, membership, group delete). Drive
    rights are derived (`authz.worktree_editable_clause`), so dropping a binding demotes the tier too; the tier rides a node-side record living 24h, so unpushed it stays writable.

    Current epoch, not a retired one: these routes do not bump `session_epoch`, so this reads the epoch
    off the row (a retired-epoch revoke would match nothing and report success).

    Call after the commit (a node acting on a rolled-back edit signs out a live console), but read the
    affected ids before any statement that destroys them. Empty `user_ids` is a no-op."""
    # Deferred: this module is imported by route modules and should not pull in the node-bus graph.
    from app import console_direct, node_bus

    unique = sorted(set(user_ids))
    if not unique:
        return
    sessions = session.execute(
        select(AdminUser.id, AdminUser.session_epoch).where(AdminUser.id.in_(unique))
    ).all()
    console_direct.revoke_for_permission_change_sync(node_bus.registry, sessions)


def revoke_direct_grants_retired_epoch(user_id: UUID, retired_epoch: int) -> None:
    """Push a direct-console revoke for a session that was just retired (the epoch-bumping verbs).

    The retired epoch, never the incremented one: `session_id` is `<admin_id>.<epoch>`, and the new
    number names a session that does not exist yet. Contract and verb list:
    `app/console_direct.py::revoke_for_admin_session`.

    The superadmin-driven verbs (password reset, demote, delete) are incident response; a node cannot see
    `session_epoch`, so without this push the direct console keeps running. Sync, so `hash_password`'s
    bcrypt stays off the event loop."""
    from app import console_direct, node_bus  # deferred, as above

    console_direct.revoke_for_admin_session_sync(node_bus.registry, user_id, retired_epoch)
