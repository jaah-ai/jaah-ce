"""Typed-in alert addresses and the login email's proof: every rule about adding, mailing and redeeming a
`notification_emails` row (`app/models/identity.py::NotificationEmail`).

A row is the PERSON's (`identity_id`), so an address proven once serves every org they are in; which org sends it
anything is each membership's own Email channel (`notification_channels.email_id`). The account-email channel's proof
is a row too, for `(identity, login email)`: the panel shows typed addresses only through channel rows, so that row
never appears as a typed channel, and if the person also types their login address the same row serves both.

* Who: only the person themself (any of their memberships) or the operator adds, mails or verifies — never an admin,
  who could otherwise type an inbox they control into someone's alerts (`may_prove`).
* Adding mails the address one link and one 6-digit code (`app/emailed_proof.py`: digests only). The code lives
  `CODE_TTL` and allows `MAX_CODE_ATTEMPTS` tries; the link lives `LINK_TTL`. Either one proves the address and spends
  both. A resend mints a new pair; at most `SENDS_PER_HOUR` sends reach one row before an hour without a send, and
  fleet-wide budgets cap one mailbox's and one person's confirm emails (`_per_address`, `_per_person`).
* An actor (one membership) adds at most `ADDS_PER_DAY` addresses a day, counted from their own `ADDED_KIND` audit
  rows; the fleet-wide send budgets below bound what a person with several memberships can mail.
* Redeeming a row by either path also stamps the person's `identities.email_verified_at`, but only while their login
  email still IS that address (`login_oauth.stamp_identity_email_verified`'s conditional UPDATE): an address changed
  between the mail and the click proves nothing about the new one.
* The link is two steps (`routes/public.py`): a GET shows a confirm page and changes nothing, so a mail scanner's
  prefetch cannot redeem it; the page's button POSTs.

The delivery engine reads the proof (`notify_targets.email_routes`); `important` mail keeps going to the verified login
email only (`app/email_notify.py`). Nothing here logs or stores a raw code or token.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import emailed_proof, notification_channels
from app.clock import utcnow
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser, NotificationEmail
from app.models.notifications import NotificationChannel
from app.ratelimit import SharedRateLimiter
from app.tenancy.ids import is_operator_user
from app.user_activity import record_write

log = logging.getLogger("app.notification_emails")

CODE_TTL = emailed_proof.CODE_TTL
LINK_TTL = emailed_proof.LINK_TTL
MAX_CODE_ATTEMPTS = emailed_proof.MAX_CODE_ATTEMPTS
SENDS_PER_HOUR = 5
ADDS_PER_DAY = 10
#: Fleet-wide, across every person: one mailbox gets at most `SENDS_PER_HOUR` confirm emails an hour, and one person
#: sends at most `PERSON_SENDS_PER_HOUR` an hour, whatever rows they hold — so old rows resent cannot spam third parties.
PERSON_SENDS_PER_HOUR = 20
#: And one mailbox at most `SENDS_PER_DAY` a day: with `MAX_CODE_ATTEMPTS` guesses per code, this bounds online
#: guessing at one address to 50 a day, and the mails a third party can be sent.
SENDS_PER_DAY = 10
ADDED_KIND = "notification_email_added"
TEMPLATE = "alert_email_confirm"
VERIFY_PATH = "/api/public/email/verify"
_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)
_per_address = SharedRateLimiter(SENDS_PER_HOUR, _HOUR.total_seconds(), bucket="alert-email-confirm-address")
_per_person = SharedRateLimiter(PERSON_SENDS_PER_HOUR, _HOUR.total_seconds(), bucket="alert-email-confirm-person")
_per_address_day = SharedRateLimiter(SENDS_PER_DAY, _DAY.total_seconds(), bucket="alert-email-confirm-address-day")

# The plain refusals the panel shows as they are.
NOT_YOURS = "Only they can add or confirm their own email addresses."
INVALID_ADDRESS = "Enter a valid email address."
ALREADY_ADDED = "This address is already a channel."
ALREADY_VERIFIED = "This address is already confirmed."
TOO_MANY_ADDS = f"You can add {ADDS_PER_DAY} addresses a day. Try again tomorrow."
TOO_MANY_SENDS = "Too many emails to this address. Try again in an hour."
NO_CODE = "No code is waiting. Send a new one."
CODE_SPENT = "This code was already used. Send a new one."
CODE_EXPIRED = "This code has expired. Send a new one."
TOO_MANY_ATTEMPTS = "Too many wrong codes. Send a new one."
WRONG_CODE = "That code is wrong. Check the email and try again."
NOT_SENT = "The confirmation email couldn't be sent. Try Resend later."


class Refused(HTTPException):
    """A refusal the route answers as it is: `status_code` and a plain `detail` sentence."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)


def may_prove(user: AdminUser, actor: AdminUser) -> bool:
    """Whether `actor` may add, mail or verify an address for `user`: the same person, or the operator."""
    same_person = user.identity_id is not None and actor.identity_id == user.identity_id
    return same_person or is_operator_user(actor)


def _require(user: AdminUser, actor: AdminUser) -> None:
    if not may_prove(user, actor):
        raise Refused(403, NOT_YOURS)


def _normalized(raw: str) -> str:
    try:
        return emailed_proof.normalized_email(raw)
    except HTTPException:
        raise Refused(422, INVALID_ADDRESS) from None


def _login(user: AdminUser) -> str:
    return (user.email or "").strip().lower()


# ── The rows ──────────────────────────────────────────────────────────────────────────────────────────────────


def _row_for(session: Session, user: AdminUser, address: str, *, create: bool) -> Optional[NotificationEmail]:
    """The person's row for `address`, locked; created (unverified) when asked and missing."""
    if create:
        session.execute(
            pg_insert(NotificationEmail)
            .values(identity_id=user.identity_id, address=address)
            .on_conflict_do_nothing(constraint="uq_notification_emails_identity_address")
        )
    return session.scalars(
        select(NotificationEmail)
        .where(NotificationEmail.identity_id == user.identity_id, NotificationEmail.address == address)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _proof_row(session: Session, user: AdminUser, channel: NotificationChannel, *, create: bool) -> Optional[NotificationEmail]:
    """The row that proves `channel`: the login email's for the account-email channel, else the typed one."""
    if channel.account_email:
        return _row_for(session, user, _login(user), create=create) if _login(user) else None
    if channel.email_id is None:
        return None
    return session.scalars(
        select(NotificationEmail).where(
            NotificationEmail.id == channel.email_id, NotificationEmail.identity_id == user.identity_id,
        ).with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _already_proven(user: AdminUser, channel: NotificationChannel, row: Optional[NotificationEmail]) -> bool:
    if channel.account_email:
        return user.email_verified_at is not None
    return row is not None and row.verified_at is not None


def _code_purpose(row: NotificationEmail) -> str:
    # Bound to the row, so one address's digest says nothing about another's.
    return f"alert-email-code:{row.id}"


def _link_digest(token: str) -> str:
    # Not bound to a row: the link arrives with nothing else, so its digest is the lookup key.
    return emailed_proof.digest("alert-email-link", token)


# ── Sending ───────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Mail:
    """What to mail once the caller committed: raw only in memory."""

    address: str
    code: str
    token: str


def _rotate(row: NotificationEmail, now: datetime) -> Mail:
    """A fresh code and link for `row`, under its send budget (`TOO_MANY_SENDS` over it). Staged, not committed."""
    if row.last_sent_at is None or now - row.last_sent_at >= _HOUR:
        row.sends_in_window = 0
    if row.sends_in_window >= SENDS_PER_HOUR:
        raise Refused(429, TOO_MANY_SENDS)
    if not (_per_address.allow(row.address) and _per_address_day.allow(row.address)
            and _per_person.allow(str(row.identity_id))):
        raise Refused(429, TOO_MANY_SENDS)
    code, token = emailed_proof.new_code(), emailed_proof.new_token()
    row.code_hash = emailed_proof.digest(_code_purpose(row), code)
    row.code_expires_at = now + CODE_TTL
    row.link_hash = _link_digest(token)
    row.link_expires_at = now + LINK_TTL
    row.attempts = 0
    row.last_sent_at = now
    row.sends_in_window += 1
    return Mail(address=row.address, code=code, token=token)


def link_url(token: str) -> str:
    """The emailed link, on the console's origin: it serves `/api/public/*`, while the hooks edge forwards only its
    own few paths (`app/ingress_surface.py::HOOKS_ALLOWED_PATHS`)."""
    from app.config import console_url

    return console_url(f"{VERIFY_PATH}/{token}")


def send(mail: Mail) -> None:
    """Mail the confirm-address email (`email_templates.ALERT_EMAIL_CONFIRM`), after the caller committed. A send that
    could not be handed off answers 503 `NOT_SENT` (the row stays; Resend tries again)."""
    from app import onboarding_mail

    url = link_url(mail.token)
    values = {
        "email": mail.address, "code": mail.code, "verify_url": url,
        "expires_minutes": str(int(CODE_TTL.total_seconds() // 60)),
    }
    outcome = onboarding_mail.send(TEMPLATE, mail.address, values, button_url=url)
    if outcome != "sent":
        log.warning("notification email: the confirm email was not sent (%s)", outcome)
        raise Refused(503, NOT_SENT)


# ── The panel's writes ────────────────────────────────────────────────────────────────────────────────────────


def _adds_today(session: Session, actor: AdminUser, now: datetime) -> int:
    return session.scalar(
        select(func.count()).select_from(ActivityEvent).where(
            ActivityEvent.user_id == actor.id, ActivityEvent.kind == ADDED_KIND, ActivityEvent.created_at >= now - _DAY,
        )
    ) or 0


def add(session: Session, user: AdminUser, actor: AdminUser, raw_address: str) -> tuple[NotificationChannel, Optional[Mail]]:
    """Add `raw_address` as one of `user`'s Email channels, unverified, committed; returns it and the mail to send.

    Typing the login address while the account-email channel is gone brings that channel back instead. An address the
    person already proved (or their proven login email) is added verified, with no mail. Refusals (`Refused`):
    403 not the person or the operator, 422 not an address, 409 already a channel, 429 over a budget."""
    _require(user, actor)
    address = _normalized(raw_address)
    if address == _login(user) and not any(c.account_email for c in notification_channels.channels_of(session, user, "email")):
        notification_channels.ensure_account_email(session, user)
        session.commit()
        return next(c for c in notification_channels.channels_of(session, user, "email") if c.account_email), None
    now = utcnow()
    if _adds_today(session, actor, now) >= ADDS_PER_DAY:
        raise Refused(429, TOO_MANY_ADDS)
    row = _row_for(session, user, address, create=True)
    assert row is not None
    if any(c.email_id == row.id for c in notification_channels.channels_of(session, user, "email")):
        session.rollback()
        raise Refused(409, ALREADY_ADDED)
    mail: Optional[Mail] = None
    if row.verified_at is None and address == _login(user) and user.email_verified_at is not None:
        row.verified_at = now
    elif row.verified_at is None:
        try:
            mail = _rotate(row, now)
        except Refused:
            session.rollback()
            raise
    channel = NotificationChannel(
        org_id=user.org_id, admin_user_id=user.id, type="email", account_email=False, email_id=row.id,
        name=address, settings={},
    )
    session.add(channel)
    session.flush()
    record_write(
        session, actor, ADDED_KIND, f"{actor.email} added {address} as an alert email for {user.email}",
        data={"user_id": user.id, "channel_id": channel.id, "notification_email_id": row.id, "address": address},
    )
    session.commit()
    return channel, mail


def resend(session: Session, user: AdminUser, actor: AdminUser, channel: NotificationChannel) -> Mail:
    """A fresh link and code for `channel`'s address (the account-email channel's Verify), committed; the caller
    mails it. 422 when it is already proven, 429 over the row's send budget."""
    _require(user, actor)
    if channel.type != "email":
        raise Refused(422, "Only an email channel is confirmed by email.")
    row = _proof_row(session, user, channel, create=True)
    if row is None:
        raise Refused(422, INVALID_ADDRESS)
    if _already_proven(user, channel, row):
        session.rollback()
        raise Refused(422, ALREADY_VERIFIED)
    try:
        mail = _rotate(row, utcnow())
    except Refused:
        session.rollback()
        raise
    session.commit()
    return mail


def verify_code(session: Session, user: AdminUser, actor: AdminUser, channel: NotificationChannel, code: str) -> None:
    """Prove `channel`'s address with the emailed `code`, committed. Refusals are 422 with a plain sentence: nothing
    sent yet or already used, expired, too many attempts (`MAX_CODE_ATTEMPTS` per code), wrong (spends one)."""
    _require(user, actor)
    if channel.type != "email":
        raise Refused(422, "Only an email channel is confirmed by email.")
    row = _proof_row(session, user, channel, create=False)
    try:
        if _already_proven(user, channel, row):
            raise Refused(422, ALREADY_VERIFIED)
        if row is None:
            raise Refused(422, NO_CODE)
        if row.code_hash is None:
            raise Refused(422, CODE_SPENT if row.verified_at is not None else NO_CODE)
        now = utcnow()
        if row.attempts >= MAX_CODE_ATTEMPTS:
            raise Refused(422, TOO_MANY_ATTEMPTS)
        if row.code_expires_at is None or row.code_expires_at <= now:
            raise Refused(422, CODE_EXPIRED)
        row.attempts += 1
        if not emailed_proof.matches(row.code_hash, _code_purpose(row), code):
            session.commit()  # the spent attempt
            raise Refused(422, WRONG_CODE)
    except Refused:
        session.rollback()  # releases the row lock; a spent attempt was committed before the raise
        raise
    _redeem(session, row, now)
    session.commit()


def _redeem(session: Session, row: NotificationEmail, now: datetime) -> None:
    """Mark `row` proven and spend both secrets; stamp the person's login email too while it still is this address.
    Staged, not committed."""
    from app.login_oauth import stamp_identity_email_verified

    row.verified_at = row.verified_at or now
    row.code_hash = row.code_expires_at = row.link_hash = row.link_expires_at = None
    row.attempts = 0
    stamp_identity_email_verified(session, row.identity_id, row.address)
    log.info("notification email: identity %s confirmed an alert address", row.identity_id)


# ── The emailed link (`routes/public.py`) ─────────────────────────────────────────────────────────────────────

LINK_CONFIRM, LINK_DONE, LINK_INVALID, LINK_EXPIRED = "confirm", "done", "invalid", "expired"


def use_link(token: str, *, redeem: bool) -> str:
    """What the link `token` does: `confirm` (a live link, GET: nothing written), `done` (redeemed, POST), `invalid`
    (unknown or already used) or `expired`. On a session of its own under `seam.identity_scope()`: the public route
    binds no org."""
    from app.db import BridgedSession
    from app.tenancy import seam

    with seam.identity_scope(), BridgedSession() as session:
        query = select(NotificationEmail).where(NotificationEmail.link_hash == _link_digest(token or ""))
        row = session.scalars(
            (query.with_for_update() if redeem else query).execution_options(populate_existing=True)
        ).first()
        now = utcnow()
        if row is None:
            return LINK_INVALID
        if row.link_expires_at is None or row.link_expires_at <= now:
            return LINK_EXPIRED
        if not redeem:
            return LINK_CONFIRM
        _redeem(session, row, now)
        session.commit()
        return LINK_DONE


def page(kind: str, nonce: str) -> str:
    """The link's page for `use_link`'s answer, as a whole HTML document (`email_unsubscribe.page_document`)."""
    from app import email_unsubscribe

    alerts = f'<a href="{html.escape(email_unsubscribe.profile_alerts_url(), quote=True)}">Profile &gt; Alerts</a>'
    if kind == LINK_CONFIRM:
        title, body = "Confirm address", (
            "<h1>Confirm this address for Jaah alerts?</h1>"
            "<p>Jaah will send the alerts you chose to this address.</p>"
            '<form method="post"><button type="submit">Confirm address</button></form>'
            '<p class="muted">Didn\'t ask for this? Close this page — nothing is sent here.</p>'
        )
    elif kind == LINK_DONE:
        title, body = "Address confirmed", (
            "<h1>Address confirmed</h1>"
            "<p>Jaah alerts can now go to this address.</p>"
            f'<p class="muted">Choose which alerts in {alerts}.</p>'
        )
    elif kind == LINK_EXPIRED:
        title, body = "Link expired", (
            "<h1>This link has expired</h1>"
            f"<p>Send a new one from {alerts}: open the address and choose Resend.</p>"
        )
    else:
        title, body = "Link not valid", (
            "<h1>This link doesn't work</h1>"
            f"<p>It was already used, or it is incomplete. Check the address in {alerts}.</p>"
        )
    return email_unsubscribe.page_document(title, body, nonce)
