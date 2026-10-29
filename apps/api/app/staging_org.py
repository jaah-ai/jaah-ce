"""A staging org: one org linked to its parent, sharing the parent's plan and bill (D-F7). Every rule of
the link lives here; `routes/org_staging.py` is the console API.

THE LINK. `orgs.parent_org_id` names the parent; at most one live staging org per parent (`uq_orgs_parent_org_id`,
soft-deleted rows aside), and a staging org has none of its own.

THE GATE. Plan switch `staging_org_enabled`: creating one is refused 403 while it is off. Losing it later keeps the
staging org and its data, but stops its dispatch (`dispatch_hold`) — and since a staging org's terms ARE its parent's
(`plan_terms.terms_org`), its own switches go off with the parent's.

ONE BILL. A staging org has no Stripe customer (`refuse_billing`, called by `billing/customers.py::ensure_customer`),
receives no bundled products (`plan_terms.receives`), never rolls over on its own (`plan_rollover`), and its tasks hold
places in the PARENT's Agents cap: `family_ids` is the set that cap counts over and `billing_org_id` the key the
dispatch budget spends from (`app/org_limits.py`).

CREATING ONE (`create`). An Owner/Admin of the parent, the switch on: a new active org "<Parent> (staging)" on the
parent's plan, whose Owners and Admins are the parent's (direct org-scope bindings) plus the caller. The rows are
written under the new org's own scope, on a fresh session (`_in_org`).

DELETING THE PARENT (`refuse_parent_delete`). Refused 409 while its staging org is live: the staging org is deleted
first, by its own Owner or the operator.

THE ADMINS FOLLOW THE PARENT (`sync_admins`). A staging org never outlives its parent's admin decisions: after every
write that can change who is a parent Owner/Admin (a direct org-scope binding added or removed, a member disabled,
enabled or deleted — `after_parent_change`), the staging org gains the parent's new Owners/Admins and loses every COPIED
binding (one the system wrote: `created_by_id` is the system actor, the one place provenance decides access) whose holder
is no longer one, and the membership the system made for anyone no longer an enabled member of the parent is disabled.
The parent's admins may also run it by hand (`POST /api/org/staging/sync`), which settles a staging org created before
this rule or a sync that failed (logged, never failing the parent's write).

ACCEPTED LIMITS. Only DIRECT org-scope Owners/Admins are copied: one through a group is not (an access request never
grants an org role). A binding a staging admin granted by hand, user or group, is theirs and stays — so a copied Owner can
grant themselves a second, hand-made role that outlives their parent role. A staging admin who removes a parent admin's
copy sees it return at the parent's next admin change. Only the Agents cap is shared — agents are what a plan bills;
the staging org counts its own nodes, projects, users and GitHub runners against the parent's per-org values. A parent
admin the staging org's Users cap leaves no room for is skipped (logged) until the cap allows.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import access, edition_features, identities, plan_terms
from app.db import SessionLocal
from app.clock import utcnow
from app.entity_shape import SYSTEM_ACTOR_ID, constraint_name, new_uuid7
from app.models.access import Role, RoleBinding
from app.models.identity import AdminUser, UserIdentity
from app.models.orgs import ORG_ACTIVE, Org
from app.org_slug import unique_slug
from app.tenancy import seam
from app.user_activity import record_write

log = logging.getLogger(__name__)

SWITCH = "staging_org_enabled"
#: `uq_orgs_parent_org_id`: the one-per-parent rule.
ONE_PER_PARENT = "uq_orgs_parent_org_id"
#: The org roles a staging org copies from its parent, each as its own role.
_COPIED_ROLES = (access.OWNER_ROLE, "org_admin")


def billing_org_id(org: Org) -> UUID:
    """The org whose caps and bill `org` spends: its parent when it is a staging org, else itself."""
    return org.parent_org_id or org.id


def staging_of(session: Session, parent_id: UUID) -> Optional[Org]:
    """`parent_id`'s live staging org, or None."""
    return session.scalar(select(Org).where(Org.parent_org_id == parent_id, Org.deleted_at.is_(None)))


def family_ids(session: Session, org_id: UUID) -> list[UUID]:
    """The orgs whose tasks share `org_id`'s dispatch caps: its billing org and that org's live staging org."""
    org = session.get(Org, org_id)
    if org is None:
        return [org_id]
    billing = billing_org_id(org)
    child = org.id if org.parent_org_id is not None else None
    if child is None:
        found = staging_of(session, billing)
        child = found.id if found is not None else None
    return [billing] if child is None else [billing, child]


def one_per_parent(parent: Org, existing: Org) -> str:
    return f"{parent.name} already has a staging org, {existing.name}. Each org has at most one. Switch to it from the org menu."


def not_from_staging(parent: Org) -> str:
    return f"This is a staging org. Create one from {parent.name} instead."


def billed_with(parent: Org) -> str:
    return f"This staging org is billed with {parent.name}. Manage billing there."


def refuse_parent_delete(session: Session, org: Org) -> None:
    """409 while `org` has a live staging org: deleting the parent would leave it running on a deleted org's plan.
    Both deletes call it (`routes/orgs.py::delete_org`, `delete_own_org`); the staging org is deleted first."""
    child = staging_of(session, org.id)
    if child is not None:
        raise HTTPException(status_code=409, detail=f"{org.name} has a staging org, {child.name}. Delete the staging org first.")


def dispatch_hold(session: Session, org: Org) -> Optional[str]:
    """Why a staging org's tasks do not dispatch now, or None (an ordinary org is never held here): its parent lost
    the switch, its parent's plan ended, or its parent is out of service. Its data stays either way."""
    if org.parent_org_id is None:
        return None
    parent = session.get(Org, org.parent_org_id)
    if parent is None or parent.deleted_at is not None or not parent.enabled or parent.lifecycle != ORG_ACTIVE:
        return "Waiting: the parent organization is not active."
    if not plan_terms.switch_on(session, parent.id, SWITCH):
        return f"Waiting: {plan_terms.SWITCH_BY_NAME[SWITCH].label} is not on {parent.name}'s plan. Upgrade in {parent.name}'s Billing."
    plan = plan_terms.plan_row(session, parent.plan_key)
    end = plan_terms.ends_of(parent, plan).value
    if end is not None and end <= utcnow():
        return f"Waiting: {parent.name}'s plan has ended."
    return None


def refuse_billing(session: Session, org_id: UUID) -> None:
    """409 for a staging org: it has no Stripe customer and buys nothing (one bill, the parent's)."""
    org = session.get(Org, org_id)
    if org is not None and org.parent_org_id is not None:
        parent = session.get(Org, org.parent_org_id)
        raise HTTPException(status_code=409, detail=billed_with(parent) if parent is not None else "This staging org has no billing of its own.")


def _admin_identities(session: Session, parent_id: UUID) -> dict[UUID, str]:
    """`{identity id: role slug}` for the parent's live Owners and Admins bound directly at org scope; an Owner
    stays an Owner when they are both. Read under the parent's own scope (the caller's)."""
    rows = session.execute(
        select(AdminUser.identity_id, Role.slug)
        .join(RoleBinding, RoleBinding.user_id == AdminUser.id)
        .join(Role, Role.id == RoleBinding.role_id)
        .where(
            RoleBinding.scope_org_id == parent_id, Role.slug.in_(_COPIED_ROLES),
            AdminUser.org_id == parent_id, AdminUser.is_system.is_(False), AdminUser.disabled_at.is_(None),
            AdminUser.identity_id.is_not(None),
        )
    ).all()
    out: dict[UUID, str] = {}
    for identity_id, slug in rows:
        if out.get(identity_id) != access.OWNER_ROLE:
            out[identity_id] = slug
    return out


@contextmanager
def _in_org(org_id: UUID) -> Iterator[Session]:
    """A fresh session under one org of a staging family. The caller's request is bound to the PARENT, so the staging
    org's rows are written after leaving it. ENTITLEMENT: `org_id` is the staging org `create` mints, or the parent /
    its live staging org `sync_admins` resolves from a parent id — never a request field; every caller either passed
    the parent's Owner/Admin guard or just committed a write the parent's own guards admitted."""
    with seam.detached(), seam.bound_to(org_id), SessionLocal() as session:
        yield session


@contextmanager
def fleet_session() -> Iterator[Session]:
    """A fresh fleet-plane session to count one staging family's shared caps: a session bound to one org sees only
    that org's rows. ENTITLEMENT: every caller reads only `family_ids` of an org it already holds (the caller's own,
    or the org a cap check runs for), and takes counts out, never rows."""
    with seam.detached(), seam.operator_scope(), SessionLocal() as session:
        yield session


def create(session: Session, parent: Org, caller: AdminUser) -> UUID:
    """Create `parent`'s staging org and return its id; commits it on its own session. `session` is the caller's,
    bound to the parent. 403 off the plan, 409 from a staging org, for a parent not active, or when the parent
    already has one. A staging org is a second org: an edition without multi-org tenancy has none (404)."""
    if not edition_features.enabled("multi_org_tenancy"):
        from app.ce_caps import NOT_IN_CE  # inline: `ce_caps` imports `org_limits`, which imports this module

        raise HTTPException(status_code=404, detail=NOT_IN_CE)
    plan_terms.require_switch(session, parent.id, SWITCH)
    if parent.parent_org_id is not None:
        grand = session.get(Org, parent.parent_org_id)
        raise HTTPException(status_code=409, detail=not_from_staging(grand or parent))
    if parent.lifecycle != ORG_ACTIVE or not parent.enabled:
        raise HTTPException(status_code=409, detail="Your organization must be active to create a staging org.")
    existing = staging_of(session, parent.id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=one_per_parent(parent, existing))
    admins = _admin_identities(session, parent.id)
    if caller.identity_id is not None:
        admins.setdefault(caller.identity_id, "org_admin")
    parent_id, parent_name, plan_key, account_type = parent.id, parent.name, parent.plan_key, parent.account_type
    slug_base = parent.slug[:40] + "-staging"
    org_id = new_uuid7()
    from app.org_create import create_org_row

    with _in_org(org_id) as fresh:
        try:
            org = create_org_row(
                fresh, org_id=org_id, slug=unique_slug(fresh, slug_base), name=f"{parent_name} (staging)",
                self_service=True, parent_org_id=parent_id, plan_key=plan_key,
            )
        except IntegrityError as exc:
            if constraint_name(exc) != ONE_PER_PARENT:
                raise
            raise HTTPException(status_code=409, detail=f"{parent_name} already has a staging org. Each org has at most one.") from None
        org.account_type = account_type
        system = access.system_actor(org_id)
        for identity_id, slug in admins.items():
            identity = fresh.scalar(select(UserIdentity).where(UserIdentity.id == identity_id))
            if identity is None:
                continue
            user = identities.add_membership(fresh, identity, org_id=org_id, actor=system)
            access.bind_staging_role(fresh, user, slug)
        record_write(
            fresh, system,  # type: ignore[arg-type]  # the system actor carries the id/email/org_id it reads
            "org_created", f"{caller.email} created the staging organization {org.name}",
            data={"org_id": org_id, "slug": org.slug, "parent_org_id": parent_id, "by": caller.id},
        )
        fresh.commit()
    return org_id


@dataclass(frozen=True)
class SyncResult:
    staging_id: UUID
    added: int
    removed: int


def _live_identities(session: Session, parent_id: UUID) -> set[UUID]:
    """The identities with an enabled membership in the parent (read under the parent's scope)."""
    return set(session.scalars(
        select(AdminUser.identity_id).where(
            AdminUser.org_id == parent_id, AdminUser.disabled_at.is_(None), AdminUser.identity_id.is_not(None),
        )
    ).all())


def sync_admins(parent_id: UUID) -> Optional[SyncResult]:
    """Make `parent_id`'s live staging org's Owners/Admins its parent's again; None when it has none. Commits on its
    own sessions, so call it after the parent's write committed.

    Under a per-staging-org lock (two syncs never interleave a stale read with a newer write), the parent is read and
    the staging org: gains each parent Owner/Admin it lacks (a membership first, when they have none; one the system
    disabled is enabled again), each in its own savepoint so one refused add never blocks the rest; loses every copied
    binding (written by the system) whose holder is no longer that role on the parent, except the last Owner binding
    (the staging org is never left ownerless; logged); and disables every membership the system made for a person
    with no enabled membership in the parent left."""
    with _in_org(parent_id) as read:
        staging = staging_of(read, parent_id)
        if staging is None:
            return None
        staging_id = staging.id
    added: list[str] = []
    removed: list[tuple[str, str]] = []
    narrowed: list[UUID] = []
    retired: list[tuple[UUID, int]] = []
    with _in_org(staging_id) as fresh:
        fresh.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"staging-admins:{staging_id}"))))
        with _in_org(parent_id) as read:
            wanted = _admin_identities(read, parent_id)
            live = _live_identities(read, parent_id)
        system = access.system_actor(staging_id)
        members = {
            user.identity_id: user for user in fresh.scalars(
                select(AdminUser).where(AdminUser.org_id == staging_id, AdminUser.identity_id.is_not(None))
            ).all()
        }
        bindings = fresh.execute(
            select(RoleBinding, Role.slug, AdminUser.identity_id)
            .join(Role, Role.id == RoleBinding.role_id)
            .join(AdminUser, AdminUser.id == RoleBinding.user_id)
            .where(RoleBinding.scope_org_id == staging_id, Role.slug.in_(_COPIED_ROLES))
        ).all()
        held = {(identity_id, slug) for _row, slug, identity_id in bindings}
        for identity_id, slug in wanted.items():
            user = members.get(identity_id)
            if user is not None and user.disabled_at is not None and user.disabled_by_id == SYSTEM_ACTOR_ID:
                user.disabled_at, user.disabled_by_id = None, None
                added.append(f"{user.email} enabled")
            if (identity_id, slug) in held:
                continue
            try:
                with fresh.begin_nested():
                    if user is None:
                        identity = fresh.scalar(select(UserIdentity).where(UserIdentity.id == identity_id))
                        if identity is None:
                            continue
                        user = identities.add_membership(fresh, identity, org_id=staging_id, actor=system)
                        members[identity_id] = user
                    access.bind_staging_role(fresh, user, slug)
            except Exception as exc:  # noqa: BLE001 — one refused add (the Users cap, a race) never blocks the rest.
                log.warning("staging admin sync: %s not added to %s: %s", identity_id, staging_id, exc)
                continue
            added.append(f"{user.email} as {slug}")
            held.add((identity_id, slug))
        dropping = [
            (row, slug) for row, slug, identity_id in bindings
            if row.created_by_id == SYSTEM_ACTOR_ID and wanted.get(identity_id) != slug
        ]
        # Never ownerless: when every Owner binding left would go, the first copied one stays.
        owners_after = sum(1 for _identity, slug in held if slug == access.OWNER_ROLE) - sum(
            1 for _row, slug in dropping if slug == access.OWNER_ROLE
        )
        kept: set[UUID] = set()
        for row, slug in dropping:
            if slug == access.OWNER_ROLE and owners_after < 1:
                owners_after += 1
                kept.add(row.user_id)
                log.warning("staging admin sync: kept %s's Owner binding, the last one of %s", row.user_id, staging_id)
                continue
            removed.append((str(row.user_id), slug))
            narrowed.append(row.user_id)
            fresh.delete(row)
        now = utcnow()
        for identity_id, user in members.items():
            if (
                identity_id in live or user.created_by_id != SYSTEM_ACTOR_ID or user.disabled_at is not None
                or user.id in kept
            ):
                continue
            retired.append((user.id, user.session_epoch or 0))
            user.disabled_at, user.disabled_by_id = now, SYSTEM_ACTOR_ID
            user.session_epoch = (user.session_epoch or 0) + 1
            removed.append((str(user.id), "membership"))
        if added or removed:
            record_write(
                fresh, system, "staging_admins_synced",  # type: ignore[arg-type]  # the system actor carries the id/email/org_id it reads
                f"Synced the staging organization's admins with its parent: {len(added)} added, {len(removed)} removed",
                data={"org_id": staging_id, "parent_org_id": parent_id, "added": added, "removed": removed},
            )
        fresh.commit()
    from app.access_admin import revoke_direct_grants_current_epoch, revoke_direct_grants_retired_epoch

    if narrowed:
        with _in_org(staging_id) as after:
            revoke_direct_grants_current_epoch(after, narrowed)
    for user_id, epoch in retired:
        revoke_direct_grants_retired_epoch(user_id, epoch)
    return SyncResult(staging_id=staging_id, added=len(added), removed=len(removed))


def after_parent_change(org_id: UUID) -> None:
    """`sync_admins` after a committed write in `org_id` that may change its Owners/Admins: a no-op unless `org_id`
    has a live staging org. Never raises: the parent's write is already committed, and a failed sync is logged and
    settled by the next write or by Sync admins."""
    try:
        sync_admins(org_id)
    except Exception:  # noqa: BLE001 — see the docstring: the caller's write already landed.
        log.exception("staging admin sync after a change in org %s failed", org_id)
