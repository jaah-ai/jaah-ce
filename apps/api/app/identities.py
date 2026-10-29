"""Reading a PERSON (`UserIdentity`) and their memberships (`AdminUser` rows) — the one home for those reads.

A person signs in by an email or a provider subject; both resolve here to the identity, and the sign-in
doors then pick which membership to open (`app/login_funnel.py::pick_membership`). Every read runs under
`seam.identity_scope()`: it resolves who is asking, before any org is bound, and `admin_users` is
tenant-owned (`identities` is not, so it needs no escape of its own — the escape is for the rows).

`create_org_user`-style refusals ("this email already has an account") ask `has_membership`, never the
email column of `admin_users`, which is no longer unique (`tests/test_identity_email_census.py`).
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.identity import (
    IDENTITY_FIELDS,
    AdminUser,
    UserIdentity,
    assert_member_row_not_superadmin,
    set_identity_fields,
)
from app.tenancy import seam


class EmailTaken(IntegrityError):
    """An account already holds this email (`has_membership`). An `IntegrityError`, so every create path
    that answered the retired `admin_users.email` unique answers this one the same way."""

    def __init__(self) -> None:
        super().__init__("an account already holds this email", None, Exception("email_taken"))


def person_label(first_name: Optional[str], last_name: Optional[str], email: Optional[str]) -> str:
    """How a PERSON is named everywhere a human reads one: "First Last" from the structured
    pair, else the email. THE ONE derivation — `admin_users` and `identities` carry no `name` column
    (`app/models/identity.py::AdminUser`), so no surface can hold a label that drifts from the names the
    person typed (one row read "Jaah Team" on Created by and "Ada Lovelace" on Answered by). Mirrors
    `apps/web/src/lib/memberLabel.ts::memberName`. A surface the email may not reach substitutes its own
    fallback (an invitee reads the org's name, `org_invitations.pending_for`)."""
    full = " ".join(part for part in (first_name, last_name) if part).strip()
    return full or (email or "")


def display_name(person) -> str:
    """`person_label` off one `AdminUser` or `UserIdentity` row."""
    return person_label(person.first_name, person.last_name, person.email)


def operator_label(user: Optional[AdminUser]) -> Optional[str]:
    """How the human who resolved a question is named — the console byline and the Slack card alike: `display_name`
    ("First Last", else the email), so both share one spelling. The audit trail (`activity_events.actor_label`) stays
    the email; this is text for a human, so a name is preferred and an address beats a placeholder.

    Reads ORM attributes: call it in the request's session, before the commit — a caller that opens its own session
    (`slack_answer_echo.echo_resolution`) would lazy-refresh an expired attribute on another connection."""
    if user is None:
        return None
    return display_name(user) or None


#: The whitespace `person_label`'s `.strip()` removes, for SQL `BTRIM` (which strips spaces alone);
#: `person_label_sql` spells the same set as an `E''` literal.
_SQL_WHITESPACE = " \t\r\n"


def label_expr():
    """`person_label` as a SQL expression over `AdminUser` — an ORDER BY, an ILIKE filter, a GROUP BY key.
    The identity hybrids compile to correlated subqueries into `identities`, so a statement needs no join
    to use it, and a rename made through another membership of the same person shows here at once."""
    full = func.nullif(
        func.btrim(func.concat_ws(" ", AdminUser.first_name, AdminUser.last_name), _SQL_WHITESPACE), ""
    )
    return func.coalesce(full, AdminUser.email)


def person_label_sql(alias: str, *, fallback: str) -> str:
    """`person_label` for a RAW-SQL statement, over the `admin_users` row aliased `alias`: the name pair off
    the person's IDENTITY (never the membership's own copy, which a rename through another membership leaves
    stale), else `fallback` — a SQL expression the caller spells for its surface (`{alias}.email` where the
    address may be shown; an org's name where it may not, `org_invitations.pending_for`). The ONE raw-SQL
    spelling; the operator's entitlements-export tool carries a copy it cannot import, pointed back here."""
    pair = (
        "(SELECT NULLIF(BTRIM(CONCAT_WS(' ', i.first_name, i.last_name), E' \\t\\r\\n'), '') "
        f"FROM identities i WHERE i.id = {alias}.identity_id)"
    )
    return f"COALESCE({pair}, {fallback})"


def by_email(session: Session, email: str) -> Optional[UserIdentity]:
    """The identity of `email`, case-insensitive, or None. The oldest wins should two differ only by case
    (the column is unique as typed, as `admin_users.email` was)."""
    address = (email or "").strip().lower()
    if not address:
        return None
    return session.scalar(
        select(UserIdentity)
        .where(func.lower(UserIdentity.email) == address)
        .order_by(UserIdentity.created_at, UserIdentity.id)
        .limit(1)
    )


def by_google_sub(session: Session, sub: str) -> Optional[UserIdentity]:
    """The identity linked to this Google `sub`, or None. A falsy `sub` matches nobody (never `IS NULL`)."""
    if not sub:
        return None
    return session.scalar(select(UserIdentity).where(UserIdentity.google_sub == sub))


def by_github_user_id(session: Session, github_user_id: int) -> Optional[UserIdentity]:
    """The identity linked to this GitHub numeric user id, or None."""
    return session.scalar(select(UserIdentity).where(UserIdentity.github_user_id == github_user_id))


def by_microsoft(session: Session, tenant_id: UUID, object_id: UUID) -> Optional[UserIdentity]:
    """The identity linked to this Entra (`tid`, `oid`) pair, or None."""
    return session.scalar(
        select(UserIdentity).where(
            UserIdentity.microsoft_tenant_id == tenant_id, UserIdentity.microsoft_object_id == object_id
        )
    )


def memberships(session: Session, identity_id: UUID) -> list[AdminUser]:
    """Every membership row of this person (the system actor's included — callers refuse it), read under
    the identity escape: these rows are in several orgs, and none is bound yet."""
    with seam.identity_scope():
        return list(
            session.scalars(
                select(AdminUser).where(AdminUser.identity_id == identity_id).order_by(AdminUser.created_at)
            )
        )


def membership_in(session: Session, identity_id: Optional[UUID], org_id: UUID) -> Optional[AdminUser]:
    """This person's membership row in `org_id` (disabled included — callers judge it), or None. The org
    switch's lookup (`login_funnel.switch_membership`), under the identity escape as `memberships` is."""
    if identity_id is None:
        return None
    with seam.identity_scope():
        return session.scalar(
            select(AdminUser).where(AdminUser.identity_id == identity_id, AdminUser.org_id == org_id).limit(1)
        )


def is_system(session: Session, identity: UserIdentity) -> bool:
    """Whether this identity is the system actor's (`AdminUser.is_system`): never a sign-in identity."""
    return any(row.is_system for row in memberships(session, identity.id))


def has_membership(session: Session, email: str) -> bool:
    """Whether the person with this email holds any membership at all (disabled ones included) — the
    "this email already has an account" refusal every create path gives, as the old global unique on
    `admin_users.email` did."""
    identity = by_email(session, email)
    if identity is None:
        return False
    with seam.identity_scope():
        return session.scalar(
            select(AdminUser.id).where(AdminUser.identity_id == identity.id).limit(1)
        ) is not None


def set_fields(session: Session, identity: UserIdentity, **fields) -> None:
    """Write person-level fields when the caller holds the identity rather than one membership (a stale
    provider link released at sign-in): the identity, and the legacy copy on each of its memberships
    (`models.set_identity_fields`'s dual write, until revision 2)."""
    rows = memberships(session, identity.id)
    if not rows:
        for field, value in fields.items():
            setattr(identity, field, value)
        return
    for row in rows:
        set_identity_fields(row, **fields)


def first_membership(session: Session, email: str) -> Optional[AdminUser]:
    """The OLDEST membership of the person with this email, or None. For machine callers where an address
    names one membership by construction — a seed's or a fixture's own login, the operator naming
    themselves on a command — never for a sign-in, which opens `login_funnel.pick_membership`'s choice."""
    person = by_email(session, email)
    if person is None:
        return None
    rows = memberships(session, person.id)
    return rows[0] if rows else None


def add_membership(session: Session, identity: UserIdentity, *, org_id: UUID, actor) -> AdminUser:
    """A new membership of an EXISTING person in `org_id`, attributed to `actor` — the way a second row is
    made (a self-created org's owner, `app/signup.py::create_signup`). It NAMES the identity, so the 0360
    insert trigger attaches it rather than refusing the person's email; the legacy columns take the
    person's current values (revision-1 dual write). Never `is_superadmin`. Refused 409 past the org's Users
    cap, like every create. Joins the org's Everyone group; flushes, the caller commits."""
    from app import access
    from app.actors import stamp_actor
    from app.org_limits import require_headroom

    require_headroom(session, org_id, "users")
    user = AdminUser(
        identity=identity, org_id=org_id,
        **{field: getattr(identity, field) for field in IDENTITY_FIELDS},
    )
    assert_member_row_not_superadmin(user)
    stamp_actor(user, actor, created=True)
    session.add(user)
    session.flush()
    access.join_everyone(session, user)
    return user


def end_every_session(session: Session, user: AdminUser) -> list[tuple[UUID, int]]:
    """Sign-out and a password change: end this person's session in EVERY membership, on every device.

    Bumps the identity epoch (`auth.session_row_is_live`) and — while revision 1 overlaps the outgoing
    release, which checks only the row epoch, and so that a downgrade cannot revive a retired cookie — every
    membership's row epoch too, in one statement keyed by the person (their rows may be in other orgs than
    the one this request is bound to). Returns each membership's `(id, retired row epoch)` for the
    direct-console revoke the caller sends after its commit. Staged, not committed."""
    from sqlalchemy import update
    from sqlalchemy.orm.attributes import set_committed_value

    identity = user.identity
    identity.session_epoch = (identity.session_epoch or 0) + 1
    retired = [
        (row.id, row.session_epoch - 1)
        for row in session.execute(
            update(AdminUser.__table__)
            .where(AdminUser.__table__.c.identity_id == identity.id)
            .values(session_epoch=AdminUser.__table__.c.session_epoch + 1)
            .returning(AdminUser.__table__.c.id, AdminUser.__table__.c.session_epoch)
        )
    ]
    # The acting row's in-memory epoch, so a re-mint in this request carries the new one.
    for row_id, epoch in retired:
        if row_id == user.id:
            set_committed_value(user, "session_epoch", epoch + 1)
    return retired


def identity_provider(identity: UserIdentity) -> str:
    """How a person signs in: the provider they are linked to (Google, then Microsoft, then GitHub), else `email` —
    what a self-create's `org_signups.provider` and a joined member's registration event record."""
    if identity.google_sub:
        return "google"
    if identity.microsoft_object_id:
        return "microsoft"
    if identity.github_user_id is not None:
        return "github"
    return "email"
