"""What an access change owes, and the tenant create-surface org guard: the direct-console revokes, the reserved-group name check, and `refuse_foreign_org`.

One home so the user, group and access routes cannot drift apart. A module rather than a route helper
because `routes/` importing `routes/` becomes a cycle.

Nothing here authorizes: `app/access.py` (role bindings) and `app/authz/` own decisions."""
from __future__ import annotations

from typing import Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.identity import AdminUser
from app.tenancy.seam import current_scope

# The seeded group whose name is reserved. It confers no bypass (only `admin_users.is_superadmin`
# does), so every write verb that could reach it refuses it by name, in `routes/groups.py` and
# `routes/users.py`; one literal so those doors cannot drift.
BYPASS_GROUP_NAME = "superadmin"

# The two other role-named groups the baseline migration seeds in the operator org ("developer",
# "product_manager"), by the ids it pins: legacy, with no members and no bindings. Next to a Role field
# they read as roles, so the access picker (`routes/access.py::list_principals`) hides them while they
# stay empty. Ids, not names: a tenant may name its own group "developer".
LEGACY_ROLE_GROUP_IDS = (
    UUID("01900000-0002-7000-8000-000000000002"),
    UUID("01900000-0002-7000-8000-000000000003"),
)

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
