"""Forgot and reset password: the one-hour, single-use link that sets a password.

`request_reset` is the forgot door's work: a `password_resets` row and the email's values, but only for an
account `auth.password_reset_eligible` admits — never a superadmin, a disabled user, a pending or rejected
org's member, or an SSO-only org's. Every other address gets the same answer from the route and nothing is
written, so the door never says whether an account exists (R-X1). It also sets a FIRST password: an
invited or OAuth-only user has no hash yet, and this is how they get one (AC12.4). `invite_link` mints the
same link, longer-lived, for the invitation email's button (`app/user_invite.py`), so a new user creates a
password from the invitation instead of landing on a sign-in page with nothing to type.

`complete_reset` is the reset door's: it sets the hash under `auth.password_problem`, retires every session
of the person exactly as a password change on `PUT /api/auth/me` does (every membership's session retired,
trusted devices forgotten), and spends every other open link of that person. It signs nobody in (2FA still
applies).

A link is the PERSON's (`PasswordReset.identity_id`): the password is identity-level, so one
reset sets it for every membership, and the per-account limit and the spending count the person's links.
`PasswordReset.user_id` is the membership the forgot door picked; the link is checked against THAT
membership's eligibility, read under its own org (`seam.bind_from` on it: the door is public, so nothing is
bound and an unbound read of `admin_users` sees nothing).

The token is `<row id>.<secret>`; only the secret's sha256 is stored, and the row must match both.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import identities, mfa
from app.auth import SUPERADMIN_PASSWORD, hash_password, password_problem, password_refusal, password_reset_eligible
from app.config import get_settings
from app.entity_shape import optional_uuid
from app.models.identity import AdminUser, PasswordReset, set_identity_fields
from app.models.orgs import Org
from app.tenancy import seam
from app.user_activity import record_write

#: How long a link works (AC12.1: at most an hour).
TOKEN_TTL = timedelta(hours=1)
#: How long an invitation's create-password link works: an invitation is read days later, not within the hour.
INVITE_TTL = timedelta(days=7)
#: Links one account may be sent per hour, counted from `password_resets.created_at` — the fleet-wide
#: backstop behind the route's per-address limiter. Past it the door still answers the same 202, silently.
PER_ACCOUNT_PER_HOUR = 3


class ResetLinkInvalid(Exception):
    """The link is unknown, malformed, used, expired, or its account may no longer reset a password."""


class SuperadminRefused(Exception):
    """The link's account is a superadmin with no password door (PRD D7; an operator may open one,
    `auth.password_refusal`). A door an operator shut is `ResetLinkInvalid`, via `password_reset_eligible`."""


class PasswordRejected(Exception):
    """The new password breaks `auth.password_problem`; `code` is the `PASSWORD_*` reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResetMail:
    """The `password_reset` email the route sends after answering."""

    to: str
    values: dict[str, str]
    reset_url: str


@dataclass(frozen=True)
class ResetDone:
    user_id: UUID
    email: str
    retired_epoch: int


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def normalized_email(email: str) -> str:
    return email.strip().lower()


def request_reset(session: Session, email: str, source_ip: Optional[str]) -> Optional[ResetMail]:
    """A new link for `email`'s account, committed with its `password_reset_requested` row, and the mail to
    send — or None, writing nothing, when no eligible account has that email or it has had
    `PER_ACCOUNT_PER_HOUR` links in the last hour."""
    address = normalized_email(email)
    if not address:
        return None
    # The person, then the membership a password sign-in would open (`login_funnel.pick_membership`, under
    # its identity escape: a public door where nothing is bound). The password is the person's.
    from app import login_funnel

    person = identities.by_email(session, address)
    user = (
        login_funnel.pick_membership(session, person, "password")
        if person is not None and not identities.is_system(session, person)
        else None
    )
    if user is None:
        return None
    now = datetime.now(timezone.utc)
    with seam.bound_to(user.org_id):
        if not password_reset_eligible(session, user):
            return None
        recent = session.scalar(
            select(func.count()).select_from(PasswordReset).where(
                PasswordReset.identity_id == user.identity_id,
                PasswordReset.created_at > now - timedelta(hours=1),
                ~_INVITATION_ROW,
            )
        )
        if recent >= PER_ACCOUNT_PER_HOUR:
            return None
        secret = secrets.token_urlsafe(32)
        row = PasswordReset(
            user_id=user.id, identity_id=user.identity_id, token_hash=_secret_hash(secret),
            expires_at=now + TOKEN_TTL, requested_ip=source_ip,
        )
        session.add(row)
        session.flush()
        record_write(
            session, None, "password_reset_requested", f"Password reset requested for {user.email}",
            data={"user_id": user.id, "email": user.email, "source_ip": source_ip},
            org_id=user.org_id,
        )
        url = reset_url(f"{row.id}.{secret}")
        mail = ResetMail(
            to=user.email,
            values={
                "first_name": user.first_name or "there",
                "email": user.email,
                "reset_url": url,
                "expires_minutes": str(int(TOKEN_TTL.total_seconds() // 60)),
            },
            reset_url=url,
        )
        session.commit()
    return mail


def _is_invitation(row: PasswordReset) -> bool:
    """An invitation's link: `invite_link` stamps both ends from one clock, `INVITE_TTL` apart. (A forgot
    link's `created_at` is the transaction's, a little before its `expires_at` base, so it reads just over
    `TOKEN_TTL`: never compare against that.)"""
    return row.expires_at - row.created_at >= INVITE_TTL


#: `_is_invitation` as SQL, for the queries that must tell the two kinds apart.
_INVITATION_ROW = PasswordReset.expires_at - PasswordReset.created_at >= INVITE_TTL


def spend_open_links(session: Session, identity_id: UUID, *, invitations_only: bool = False) -> None:
    """Spend every open link of the person (or only their invitation links), uncommitted."""
    stmt = update(PasswordReset).where(PasswordReset.identity_id == identity_id, PasswordReset.used_at.is_(None))
    if invitations_only:
        stmt = stmt.where(_INVITATION_ROW)
    session.execute(stmt.values(used_at=datetime.now(timezone.utc)))


def invite_link(session: Session, user: AdminUser) -> Optional[str]:
    """A create-password link for `user`'s invitation, flushed — or None, writing nothing, when the person
    already has a password or `password_reset_eligible` refuses them (a superadmin with no password door, an
    SSO-only org): their button opens the sign-in page instead. A resend's link replaces the last one. It
    only ever sets a FIRST password (`_account`), and it does not spend the forgot door's hourly budget. Runs
    on the caller's plane; the caller commits. The create or resend request is the audit record."""
    if user.password_hash is not None or not password_reset_eligible(session, user):
        return None
    spend_open_links(session, user.identity_id, invitations_only=True)
    secret = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    row = PasswordReset(
        user_id=user.id, identity_id=user.identity_id, token_hash=_secret_hash(secret),
        created_at=now, expires_at=now + INVITE_TTL,
    )
    session.add(row)
    session.flush()
    return reset_url(f"{row.id}.{secret}")


def reset_url(token: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/reset-password?token={token}"


def _parse(token: str) -> tuple[UUID, str]:
    raw_id, dot, secret = token.strip().partition(".")
    if not dot or not secret:
        raise ResetLinkInvalid()
    row_id = optional_uuid(raw_id)
    if row_id is None:
        raise ResetLinkInvalid()
    return row_id, secret


def _live_row(session: Session, row_id: UUID, secret: str, *, lock: bool) -> PasswordReset:
    stmt = select(PasswordReset).where(
        PasswordReset.id == row_id, PasswordReset.token_hash == _secret_hash(secret)
    )
    if lock:
        # Two submits of one link serialize here; the second reads `used_at` set and is refused.
        stmt = stmt.with_for_update()
    row = session.scalar(stmt)
    if row is None or row.used_at is not None or row.expires_at <= datetime.now(timezone.utc):
        raise ResetLinkInvalid()
    return row


def _account(session: Session, row: PasswordReset) -> AdminUser:
    # A filtered select, never `session.get`: an identity-map hit would skip the org predicate.
    user = session.scalar(select(AdminUser).where(AdminUser.id == row.user_id))
    if user is None:
        raise ResetLinkInvalid()
    # A week-long invitation link creates a first password; it never replaces one set since.
    if _is_invitation(row) and user.password_hash is not None:
        raise ResetLinkInvalid()
    if password_refusal(user) == SUPERADMIN_PASSWORD:
        raise SuperadminRefused()
    if not password_reset_eligible(session, user):
        raise ResetLinkInvalid()
    return user


@dataclass(frozen=True)
class CheckedLink:
    """What the set-new-password page shows for a live link. `org_name`/`inviter_name` only for an
    invitation's link (the page then reads as the invitation: who invites, into which org); `inviter_name`
    is None when the creator is unnamed, a system actor, or outside the org (an operator)."""

    email: str
    first_password: bool
    org_name: Optional[str] = None
    inviter_name: Optional[str] = None


def check_link(session: Session, token: str) -> CheckedLink:
    """What a live link sets the password for; raises as `complete_reset` would, writing nothing."""
    row_id, secret = _parse(token)
    row = _live_row(session, row_id, secret, lock=False)
    if row.user_id is None:  # the membership it was issued through is gone
        raise ResetLinkInvalid()
    try:
        with seam.bind_from(session, AdminUser, row.user_id):
            user = _account(session, row)
            checked = CheckedLink(email=user.email, first_password=user.password_hash is None)
            if not _is_invitation(row):
                return checked
            org = session.get(Org, user.org_id) if user.org_id is not None else None
            # A filtered select, as `_account`: the inviter is read in the invitee's own org only.
            inviter = session.scalar(select(AdminUser).where(AdminUser.id == user.created_by_id))
            # The inviter's name pair only (`identities.person_label`), never their email, which the invitee
            # has no business reading; a machine inviter and an unnamed one read as nobody.
            label = (
                identities.person_label(inviter.first_name, inviter.last_name, None)
                if inviter is not None and not inviter.is_system else ""
            )
            return CheckedLink(
                email=checked.email, first_password=checked.first_password,
                org_name=org.name if org is not None else None,
                inviter_name=label or None,
            )
    except seam.TenancyRowMissing:
        raise ResetLinkInvalid() from None


def complete_reset(session: Session, token: str, password: str) -> ResetDone:
    """Set `password` through the link `token`, committed. Raises `ResetLinkInvalid`, `SuperadminRefused` or
    `PasswordRejected`, each before anything is written.

    The caller revokes the direct-console grants of `retired_epoch` after this returns, as `update_me` does."""
    row_id, secret = _parse(token)
    # Platform-plane, so the unbound read sees it; the lock serializes two submits of one link.
    row = _live_row(session, row_id, secret, lock=True)
    if row.user_id is None:  # the membership it was issued through is gone
        raise ResetLinkInvalid()
    try:
        with seam.bind_from(session, AdminUser, row.user_id):
            user = _account(session, row)
            problem = password_problem(password, user.email)
            if problem is not None:
                raise PasswordRejected(problem)
            set_identity_fields(user, password_hash=hash_password(password))
            # The person's sign-out everywhere (`identities.end_every_session`).
            retired = identities.end_every_session(session, user)
            retired_epoch = dict(retired)[user.id]
            # A new password also forgets every trusted device, as on `PUT /api/auth/me`.
            mfa.revoke_trusted_devices(session, user)
            # This link and every other open one of the person: a reset spends them all.
            spend_open_links(session, row.identity_id)
            record_write(
                session, None, "password_reset_completed", f"Password reset completed for {user.email}",
                data={"user_id": user.id, "email": user.email, "reset_id": row.id},
                org_id=user.org_id,
            )
            done = ResetDone(user_id=user.id, email=user.email, retired_epoch=retired_epoch)
            session.commit()
    except seam.TenancyRowMissing:
        raise ResetLinkInvalid() from None
    return done
