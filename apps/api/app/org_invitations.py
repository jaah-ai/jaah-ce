"""Joining another org by invitation (multi-org membership R5): the one home for `org_invitations`.

An org admin "creating" a user whose email already has an account elsewhere (`routes/users.py::create_user`)
makes a PENDING invitation here instead of a second account: nothing is a member until the invitee accepts.
The accept makes their membership in the inviting org (`identities.add_membership`: never `is_superadmin`,
joined to Everyone), applies the invitation's `grant` verbatim, and marks it `accepted`; a decline marks it
`declined` and makes nothing. Both answer 404 for an invitation that is not the caller's own, or no longer
pending.

THE MEMBERSHIP IS MADE AT INVITE TIME (`invited_member`): an admin's invite makes the person's row
in the inviting org at once, DISABLED and pointing at the invitation (`AdminUser.invitation_id`), so People
lists it like any invited user and admins grant it access before the answer. Disabled is what keeps it inert
— every sign-in door, the org switch and the request principal already refuse a disabled row — and the
invitation link is what tells it apart from an admin's disable. The accept enables that row; a decline
leaves it, waiting on the declined invitation, until an admin invites again or deletes it (which withdraws
a still-pending invitation, `routes/users.py::delete_user`).

THE INVITEE READS ACROSS ORGS, BY PERSON. Their invitations live in OTHER orgs than the one their session is
bound to, so the two reads that find them (`pending_for`, `_claimed_org`) are raw SQL keyed by `identity_id`
— never by org — registered in `test_tenancy_bypass_sites.py`'s arm C (`invitee-own`). Each returns values,
never rows. Every write then runs under `seam.bound_to` the invitation's own org, where the seam re-reads and
locks the row it is about to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import access, identities
from app.identities import person_label_sql
from app.access_admin import is_bypass_group_name
from app.actors import stamp_actor
from app.entity_shape import SYSTEM_ACTOR_ID, conflict_as_409, constraint_name, get_or_404
from app.models.access import Group, UserGroupMembership
from app.models.identity import AdminUser, UserIdentity
from app.models.orgs import OrgInvitation
from app.tenancy import seam

NOT_FOUND = "unknown invitation"
_ONE_MEMBERSHIP_PER_ORG = "uq_admin_users_identity_id_org_id"
ALREADY_MEMBER = "This person is already a member of this organization."


@dataclass(frozen=True)
class Pending:
    """One invitation awaiting the invitee's answer, as `/api/auth/me` lists it."""

    id: UUID
    org_name: str
    invited_by_name: str
    created_at: datetime
    #: The inviting org (`routes/users.py::_shared_person`); never sent to the invitee.
    org_id: Optional[UUID] = None


def invite(session: Session, actor, *, org_id: UUID, identity: UserIdentity, grant: Optional[dict] = None
           ) -> tuple[OrgInvitation, bool]:
    """The pending invitation of `identity` into `org_id` — made now, attributed to `actor`, or the one
    already pending (re-inviting is idempotent: one pending invitation per person per org). Returns it and
    whether it was made by this call. Runs under the caller's own scope (the inviting org's, or the
    operator's); flushes, the caller commits."""
    existing = session.scalar(
        select(OrgInvitation).where(
            OrgInvitation.org_id == org_id, OrgInvitation.identity_id == identity.id,
            OrgInvitation.state == "pending",
        )
    )
    if existing is not None:
        return existing, False
    invitation = OrgInvitation(
        org_id=org_id, identity_id=identity.id, name=identity.email, grant=dict(grant or {}), state="pending",
    )
    stamp_actor(invitation, actor, created=True)
    try:
        # Added INSIDE the savepoint, so a lost race rolls back to it and the session stays usable.
        with session.begin_nested():
            session.add(invitation)
            session.flush()
    except IntegrityError:
        # A concurrent invite won `uq_org_invitations_pending`: theirs is the one pending.
        existing = session.scalar(
            select(OrgInvitation).where(
                OrgInvitation.org_id == org_id, OrgInvitation.identity_id == identity.id,
                OrgInvitation.state == "pending",
            )
        )
        if existing is None:
            raise
        return existing, False
    return invitation, True


def typed_names(invitation: Optional[OrgInvitation]) -> tuple[Optional[str], Optional[str]]:
    """The first and last name the inviter typed (`invited_member`), shown on the row that waits on
    `invitation` in place of the person's own pair."""
    typed = (invitation.grant or {}).get("typed_names") if invitation is not None else None
    typed = typed if isinstance(typed, dict) else {}
    return typed.get("first_name") or None, typed.get("last_name") or None


def invitation_states(session: Session, users: list[AdminUser]) -> dict[UUID, OrgInvitation]:
    """The org invitation each of these rows waits on (`AdminUser.invitation_id`), in one read."""
    ids = {user.invitation_id for user in users if user.invitation_id is not None}
    if not ids:
        return {}
    return {row.id: row for row in session.scalars(select(OrgInvitation).where(OrgInvitation.id.in_(ids)))}


def shown_names(user: AdminUser, states: dict[UUID, OrgInvitation]) -> tuple[Optional[str], Optional[str]]:
    """The name pair a roster shows for `user` (`states` from `invitation_states`): a row waiting on an
    invitation shows what the inviter typed, never the person's own pair — it is not this org's to read
    before they join, and it would tell an admin the person has an account elsewhere."""
    if user.invitation_id is not None:
        return typed_names(states.get(user.invitation_id))
    return user.first_name, user.last_name


def shown_label():
    """`shown_names` as one SQL label — what a roster's `?q=` matches and `?sort=name` orders by, so a search
    never answers what the row hides. Needs `OrgInvitation` outer-joined on `AdminUser.invitation_id`."""
    typed = OrgInvitation.grant["typed_names"]
    typed_pair = func.nullif(
        func.btrim(
            func.concat_ws(" ", typed["first_name"].astext, typed["last_name"].astext), identities._SQL_WHITESPACE
        ),
        "",
    )
    return case(
        (AdminUser.invitation_id.is_(None), identities.label_expr()),
        else_=func.coalesce(typed_pair, AdminUser.email),
    )


def held_by_invitation(member: AdminUser) -> bool:
    """Is this row disabled ONLY because it waits on its invitation — the system's disable, not an admin's?
    An admin may disable (and re-enable) a waiting row like any other; the accept then keeps their disable."""
    return member.invitation_id is not None and member.disabled_by_id == SYSTEM_ACTOR_ID


def invited_member(
    session: Session, actor, *, org_id: UUID, identity: UserIdentity,
    first_name: Optional[str] = None, last_name: Optional[str] = None, renew: bool = False,
) -> tuple[AdminUser, OrgInvitation, bool]:
    """Invite `identity` into `org_id` AND make the membership that waits on it: their row in
    the org, DISABLED and pointing at the invitation (`AdminUser.invitation_id`), so an admin lists it and
    grants it access before the person answers; the accept enables it. Returns the row, the pending
    invitation and whether this call made the invitation.

    The person's row already here: the 409, as a new user's address is when created twice — unless `renew`
    (a Resend on that row), which renews a declined invitation (a fresh pending one, the row re-pointed at
    it) and returns a pending one as is. A row waiting on nothing is a member already: always the 409. The
    row carries no label of its own: the person's derives from their name pair (`identities.person_label`),
    and what the inviter typed rides the invitation. Under the caller's scope; flushes, the caller commits."""
    member = session.scalar(
        select(AdminUser).where(AdminUser.identity_id == identity.id, AdminUser.org_id == org_id)
    )
    if member is not None and (member.invitation_id is None or not renew):
        raise HTTPException(status_code=409, detail=ALREADY_MEMBER)
    # What the inviter typed rides the invitation (`typed_names`) — a renewal carries the previous one's.
    typed = {"first_name": first_name, "last_name": last_name}
    if member is not None and not (first_name or last_name):
        previous = session.scalar(select(OrgInvitation).where(OrgInvitation.id == member.invitation_id))
        typed = dict(zip(("first_name", "last_name"), typed_names(previous)))
    invitation, made = invite(
        session, actor, org_id=org_id, identity=identity, grant={"typed_names": typed},
    )
    if member is None:
        try:
            with session.begin_nested():
                member = identities.add_membership(session, identity, org_id=org_id, actor=actor)
        except IntegrityError as exc:
            # A concurrent invite made the row first: theirs is the one waiting.
            if constraint_name(exc) != _ONE_MEMBERSHIP_PER_ORG:
                raise
            raise HTTPException(status_code=409, detail=ALREADY_MEMBER)
        member.disabled_at = datetime.now(timezone.utc)
        # The system's disable, not an admin's (`held_by_invitation`): `enable_user` refuses a tenant admin
        # one it did not make, so the row stays inert even should its invitation link ever be lost.
        member.disabled_by_id = SYSTEM_ACTOR_ID
    member.invitation_id = invitation.id
    stamp_actor(member, actor)
    session.flush()
    return member, invitation, made


def pending_for(session: Session, identity_id: Optional[UUID]) -> list[Pending]:
    """This person's pending invitations, oldest first, with the inviting org's name and the inviter's.

    RAW SQL keyed by the person (module docstring): the invitations are in other orgs than the caller's
    session, and only values leave. An org that is disabled no longer invites."""
    if identity_id is None:
        return []
    rows = session.execute(
        text(
            # The inviter's name pair (`identities.person_label_sql`), never their email (it is not the
            # invitee's to see — so the org's name is the fallback here, not the address); unnamed, the org's.
            f"SELECT i.id, o.name, {person_label_sql('u', fallback='o.name')}, i.created_at, i.org_id "
            "FROM org_invitations i JOIN orgs o ON o.id = i.org_id "
            "LEFT JOIN admin_users u ON u.id = i.created_by_id "
            "WHERE i.identity_id = :identity AND i.state = 'pending' AND o.enabled "
            "ORDER BY i.created_at, i.id"
        ),
        {"identity": identity_id},
    ).all()
    return [Pending(id=r[0], org_name=r[1], invited_by_name=r[2], created_at=r[3], org_id=r[4]) for r in rows]


def _claimed_org(session: Session, invitation_id: UUID, identity_id: Optional[UUID]) -> UUID:
    """The org of the caller's OWN pending invitation `invitation_id`, else the 404 — one value, keyed by the
    person (module docstring), so another person's invitation, a decided one and a disabled org's read as
    nonexistent."""
    org_id = None
    if identity_id is not None:
        org_id = session.execute(
            text(
                "SELECT i.org_id FROM org_invitations i JOIN orgs o ON o.id = i.org_id "
                "WHERE i.id = :id AND i.identity_id = :identity AND i.state = 'pending' AND o.enabled"
            ),
            {"id": invitation_id, "identity": identity_id},
        ).scalar()
    if org_id is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return org_id


def _locked(session: Session, invitation_id: UUID, identity_id: UUID) -> OrgInvitation:
    """The invitation, re-read under its own org and locked, still pending and still the caller's."""
    return get_or_404(
        session, OrgInvitation, invitation_id, detail=NOT_FOUND,
        where=(OrgInvitation.identity_id == identity_id, OrgInvitation.state == "pending"), lock=True,
    )


def _apply_grant(session: Session, member: AdminUser, grant: dict) -> None:
    """The access the inviter chose, verbatim: `{"groups": [<group name>, ...]}` in the inviting org. The
    Everyone group is the accept's own (`add_membership`), and the bypass group confers nothing — both are
    skipped, as is a group renamed or deleted since the invite."""
    for name in dict.fromkeys(grant.get("groups") or []):
        if not isinstance(name, str) or is_bypass_group_name(name):
            continue
        group = session.scalar(select(Group).where(Group.name == name, Group.org_id == member.org_id))
        if group is None or group.is_everyone:
            continue
        # An admin may already have added the invited row to it.
        if session.scalar(select(UserGroupMembership.id).where(
            UserGroupMembership.user_id == member.id, UserGroupMembership.group_id == group.id,
        ).limit(1)) is not None:
            continue
        session.add(UserGroupMembership(user_id=member.id, group_id=group.id))
    session.flush()


def _decide(invitation: OrgInvitation, state: str, org_id: UUID) -> None:
    """Record the invitee's answer on the locked invitation, attributed to the inviting org's system actor (the
    invitee's own membership is in another org, and a provenance stamp would pin it against deletion)."""
    invitation.state = state
    invitation.decided_at = datetime.now(timezone.utc)
    stamp_actor(invitation, access.system_actor(org_id))


def accept(session: Session, admin: AdminUser, invitation_id: UUID) -> UUID:
    """The caller's person joins the inviting org: their new membership, the grant, the invitation marked
    `accepted` — one transaction, committed. Returns the org. Already a member there (joined another way since):
    the invitation is marked accepted and nothing is made. 404 unless the invitation is the caller's own and
    pending; 409 when a concurrent accept won, or the org has no seat left."""
    identity = admin.identity
    org_id = _claimed_org(session, invitation_id, admin.identity_id)
    with seam.bound_to(org_id):
        invitation = _locked(session, invitation_id, identity.id)
        member = session.scalar(select(AdminUser).where(
            AdminUser.identity_id == identity.id, AdminUser.org_id == org_id,
        ).with_for_update())
        # The membership made at invite time (`invited_member`): enabled now, with the access the admins
        # granted it meanwhile already in place.
        if member is not None and member.invitation_id == invitation.id:
            if held_by_invitation(member):
                member.disabled_at = None
                member.disabled_by_id = None
            member.invitation_id = None
            member.activated_at = datetime.now(timezone.utc)
            identities.assert_member_row_not_superadmin(member)
            stamp_actor(member, access.system_actor(org_id))
            _apply_grant(session, member, invitation.grant or {})
            _decide(invitation, "accepted", org_id)
            session.commit()
            _joined(member.id, org_id)
            return org_id
        # A member here already (joined another way since the invite): the invitation is answered, nothing made.
        if member is not None:
            _decide(invitation, "accepted", org_id)
            session.commit()
            return org_id
        # Only the one-membership-per-person-per-org key is a concurrent accept; anything else is a fault.
        with conflict_as_409(session, ALREADY_MEMBER, constraints=(_ONE_MEMBERSHIP_PER_ORG,)):
            member = identities.add_membership(session, identity, org_id=org_id, actor=access.system_actor(org_id))
        _apply_grant(session, member, invitation.grant or {})
        _decide(invitation, "accepted", org_id)
        session.commit()
        _joined(member.id, org_id)
    return org_id


def _joined(member_id: UUID, org_id: UUID) -> None:
    """After the accept committed: the inviting org's `user_registered` triggers."""
    from app import trigger_user_registered

    trigger_user_registered.on_member_joined(member_id, org_id, via=trigger_user_registered.VIA_INVITATION)


def decline(session: Session, admin: AdminUser, invitation_id: UUID) -> None:
    """The caller turns the invitation down: marked `declined`, nothing made. Committed. 404 as `accept`."""
    org_id = _claimed_org(session, invitation_id, admin.identity_id)
    with seam.bound_to(org_id):
        _decide(_locked(session, invitation_id, admin.identity_id), "declined", org_id)
        session.commit()
