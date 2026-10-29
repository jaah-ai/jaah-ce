"""The delivery log: one `email_deliveries` row per system email (`models.EmailDelivery`), from enqueue to its fate.

`create` writes the row `queued` when a send is taken (`durable/notify_outbox.py::email`, the operator's test send),
so a message that later fails is never silent; `attempted` counts each try; `record` sets the outcome. Statuses:

  * `queued` — taken, not yet handed to SES;  `sent` — SES accepted it (`provider_message_id` is its MessageId), or,
    once SES refused it for good, the operator org's Gmail did (`gmail:<id>`, `status_detail` says so);
  * `delivered`, `delayed`, `bounced`, `complained`, `suppressed` — what SES's events said afterwards;
  * `failed` — refused for good (a 4xx, a rejected or unrenderable message, every retry spent, and no Gmail fallback
    took it): `status_detail` says why;
  * `not_configured` — the deployment had no SES sending when it was tried (final).

Every row is org zero's (system mail is the deployment's own) and is written on the operator plane, whatever scope
the sender runs in. A recipient is stored as sent: the log is operator-only. Kept 90 days (`app/cron_retention.py`).

THE LATTICE (`advance`): SES's events arrive out of order and more than once (`app/ses_events.py`), so a status moves
only up `_RANK` — `delivered` never falls back to `sent` or `delayed`, a complaint outranks the delivery before it.
`suppressed` and `not_configured` rows never reached SES and no event touches them.

SUPPRESSIONS (`models.EmailSuppression`, PRD R4): a Permanent bounce or a complaint suppresses the address; the send
path's pre-check (`suppressed`) then records `suppressed` instead of calling SES. Clearing one (`clear_suppression`)
takes the address off SES's account-level list too, so an address cleared here is sendable there. Kept until cleared.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional
from uuid import UUID

from email.utils import parseaddr

from sqlalchemy import case, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.notifications import (
    EMAIL_DELIVERY_STATUSES, EMAIL_SUPPRESSION_REASONS, EmailDelivery, EmailSuppression,
)

log = logging.getLogger("app.email_deliveries")

#: How long a row is kept (AS7).
RETENTION_DAYS = 90
#: The most SES events one row keeps, newest last.
EVENTS_CAP = 20
#: `status_detail` is cut to this many characters.
DETAIL_MAX = 1000
FINAL_STATUSES = frozenset({"failed", "not_configured", "suppressed"})
#: How far along a message is; an event moves a row only to an equal or higher rank (THE LATTICE).
_RANK = {"queued": 0, "sent": 1, "delayed": 2, "delivered": 3, "bounced": 4, "failed": 4, "complained": 5}
#: What a still-`queued` row's `status_detail` starts with once the Gmail fallback claimed it (`claim_fallback`): SES's
#: reason follows, so a re-run that finds it knows a Gmail send may have happened and never sends again.
FALLBACK_PENDING = "Gmail fallback pending: "
#: What a `suppressed` row says.
SUPPRESSED_DETAIL = "Not sent: the address is on the suppression list"
#: The most rows one page of the delivery log holds.
PAGE_SIZE = 100
#: What a secret value (a sign-up code, a link carrying a token) reads as in a logged subject.
REDACTED = "••••••"


def redacted(subject: str, secrets: Iterable[str]) -> str:
    """`subject` as the log keeps it: every non-empty value in `secrets` masked, longest first, so a template whose
    subject carries `{{code}}` never leaves a working code at rest for 90 days."""
    for value in sorted({s for s in secrets if s}, key=len, reverse=True):
        subject = subject.replace(value, REDACTED)
    return subject


@contextlib.contextmanager
def _session() -> Iterator[Any]:
    """A fresh operator-plane session, out of any caller scope: the row is org zero's whoever sends."""
    from app.db import SessionLocal
    from app.tenancy import seam

    with seam.detached(), seam.operator_scope(), SessionLocal() as session:
        yield session


def delivery_org() -> UUID:
    """The org every delivery row belongs to: org zero, the deployment's own."""
    from app.tenancy.ids import ORG_ZERO_ID

    return ORG_ZERO_ID


def create(
    *, recipient: str, subject: str, mail_type: str, essential: bool, recipient_user_id: Optional[UUID] = None,
) -> UUID:
    """Write one `queued` row and return its id (the `delivery_id` SES tags the message with). Raises on a DB fault."""
    with _session() as session:
        delivery_id = session.execute(
            insert(EmailDelivery).values(
                org_id=delivery_org(), recipient=recipient, subject=subject[:DETAIL_MAX], mail_type=mail_type,
                essential=essential, recipient_user_id=recipient_user_id, status="queued",
            ).returning(EmailDelivery.id)
        ).scalar_one()
        session.commit()
    return delivery_id


def load(delivery_id: UUID) -> Optional[EmailDelivery]:
    """The row, detached from its session, or None when it is gone (swept)."""
    with _session() as session:
        row = session.get(EmailDelivery, delivery_id)
        if row is not None:
            session.expunge(row)
        return row


def attempted(delivery_id: UUID) -> None:
    """Count one try at the send."""
    with _session() as session:
        session.execute(
            update(EmailDelivery).where(EmailDelivery.id == delivery_id).values(attempts=EmailDelivery.attempts + 1)
        )
        session.commit()


def record(
    delivery_id: UUID, status: str, *, detail: Optional[str] = None, provider_message_id: Optional[str] = None,
) -> None:
    """Set the row's outcome. A DB fault is logged, never raised: the send already happened (or did not) whatever the
    log says. An unknown `status` is a programming error and raises `ValueError`."""
    if status not in EMAIL_DELIVERY_STATUSES:
        raise ValueError(f"unknown delivery status {status!r}")
    values: dict[str, Any] = {"status": status, "status_detail": detail[:DETAIL_MAX] if detail else None}
    if status == "sent":
        values["sent_at"] = datetime.now(timezone.utc)
        values["provider_message_id"] = provider_message_id
        # An SES event (`advance`) may already have moved the row past `sent`: never back.
        values["status"] = case((EmailDelivery.status == "queued", "sent"), else_=EmailDelivery.status)
        # `detail` (a fallback's reason, `durable/notify_outbox.py`) is kept on that same move, else cleared.
        values["status_detail"] = case(
            (EmailDelivery.status == "queued", detail[:DETAIL_MAX] if detail else None), else_=EmailDelivery.status_detail
        )
    try:
        with _session() as session:
            session.execute(update(EmailDelivery).where(EmailDelivery.id == delivery_id).values(**values))
            session.commit()
    except Exception:
        log.exception("email deliveries: delivery %s could not be marked %s", delivery_id, status)


def claim_fallback(delivery_id: UUID, expected_detail: Optional[str], detail: str) -> bool:
    """Claim the still-`queued`, unsent row for its one Gmail fallback send: `status_detail` becomes `FALLBACK_PENDING`
    + `detail` (SES's reason) only while it still reads `expected_detail`. True when this call made the claim, so at
    most one caller ever sends. Unlike `record`, a DB fault RAISES: no claim, no send."""
    with _session() as session:
        claimed = session.execute(
            update(EmailDelivery)
            .where(
                EmailDelivery.id == delivery_id, EmailDelivery.status == "queued",
                EmailDelivery.provider_message_id.is_(None),
                EmailDelivery.status_detail.is_not_distinct_from(expected_detail),
            )
            .values(status_detail=f"{FALLBACK_PENDING}{detail}"[:DETAIL_MAX])
        ).rowcount
        session.commit()
    return claimed == 1


def advance(
    session: Session, *, delivery_id: Optional[UUID], message_id: str, status: Optional[str],
    detail: Optional[str], event: dict[str, Any], at: datetime,
) -> Optional[EmailDelivery]:
    """Apply one SES event to its row in the caller's transaction, found by the `delivery_id` tag (seam S2), else by
    SES's MessageId; the row, or None when no row is this message's (swept, or not system mail). `status` None (an
    event that names no status) only records the event. The event is appended once (a redelivery is a no-op), the
    list capped at `EVENTS_CAP`; the status moves only up THE LATTICE."""
    row = None
    if delivery_id is not None:
        row = session.execute(
            select(EmailDelivery).where(EmailDelivery.id == delivery_id).with_for_update()
        ).scalar_one_or_none()
    if row is None and message_id:
        row = session.execute(
            select(EmailDelivery).where(EmailDelivery.provider_message_id == message_id).with_for_update()
        ).scalar_one_or_none()
    if row is None:
        return None
    # A send-path `failed` with no MessageId (every try timed out after SES took it) is no verdict of SES's: its
    # own events outrank it.
    current = 0 if row.status == "failed" and row.provider_message_id is None else _RANK.get(row.status)
    if row.provider_message_id is None and message_id:
        row.provider_message_id = message_id
    events = list(row.events or [])
    if event not in events:
        row.events = (events + [event])[-EVENTS_CAP:]
    if row.last_event_at is None or at > row.last_event_at:
        row.last_event_at = at
    if status is not None and current is not None and _RANK[status] >= current:
        row.status = status
        row.status_detail = detail[:DETAIL_MAX] if detail else None
    return row


def apply_event(
    *, delivery_id: Optional[UUID], message_id: str, status: Optional[str], detail: Optional[str],
    event: dict[str, Any], at: datetime, hits: Iterable[tuple[str, str, Optional[str]]],
) -> tuple[bool, int]:
    """One SES event (`app/ses_events.py`) in one committed transaction: `advance` its row, then `suppress` each
    `(address, reason, detail)` in `hits`. `(a row was found, addresses newly suppressed)`. Raises on a DB fault."""
    with _session() as session:
        row = advance(
            session, delivery_id=delivery_id, message_id=message_id, status=status, detail=detail, event=event, at=at,
        )
        source = row.id if row is not None else None
        added = sum(
            suppress(session, address, reason, detail=why, source_delivery_id=source)
            for address, reason, why in hits
        )
        session.commit()
    return row is not None, added


def normalised(address: str) -> str:
    """An address as the suppression list keys it: the bare address of `Name <a@b>`, lower-case."""
    return parseaddr(address or "")[1].strip().lower()


def suppress(
    session: Session, address: str, reason: str, *, detail: Optional[str] = None,
    source_delivery_id: Optional[UUID] = None,
) -> bool:
    """Suppress `address` in the caller's transaction; True when it was not already (an active row wins)."""
    if reason not in EMAIL_SUPPRESSION_REASONS:
        raise ValueError(f"unknown suppression reason {reason!r}")
    key = normalised(address)
    if "@" not in key:
        return False
    inserted = session.execute(
        pg_insert(EmailSuppression).values(
            org_id=delivery_org(), address=key, reason=reason, detail=detail[:DETAIL_MAX] if detail else None,
            source_delivery_id=source_delivery_id,
        ).on_conflict_do_nothing(
            index_elements=[EmailSuppression.address], index_where=EmailSuppression.cleared_at.is_(None),
        ).returning(EmailSuppression.id)
    ).scalar_one_or_none()
    return inserted is not None


def suppressed(address: str) -> bool:
    """Whether system mail to `address` is suppressed: the send path's pre-check."""
    key = normalised(address)
    with _session() as session:
        return session.execute(
            select(EmailSuppression.id).where(EmailSuppression.address == key, EmailSuppression.cleared_at.is_(None))
        ).first() is not None


def suppressions() -> list[EmailSuppression]:
    """Every active suppression, newest first, detached."""
    with _session() as session:
        rows = list(session.execute(
            select(EmailSuppression).where(EmailSuppression.cleared_at.is_(None))
            .order_by(EmailSuppression.created_at.desc(), EmailSuppression.address)
        ).scalars())
        session.expunge_all()
        return rows


def clear_suppression(address: str, *, by: UUID) -> bool:
    """Make `address` sendable again: off SES's account-level list first (where system mail is set up here; SES's 404
    counts as done), then the active row stamped cleared. False when no active row holds it. Raises `ses.SesError`
    when SES refused, leaving the row active so the operator can retry."""
    from app import edition_features, system_mail

    key = normalised(address)
    with _session() as session:
        active = session.execute(
            select(EmailSuppression.id).where(EmailSuppression.address == key, EmailSuppression.cleared_at.is_(None))
        ).scalar_one_or_none()
    if active is None:
        return False
    # Outside any transaction: SES's answer can take seconds, and no row lock or connection waits on it.
    if edition_features.enabled("system_mail"):
        from app.aws import ses

        if system_mail.configured():
            ses.delete_suppressed(key)
    with _session() as session:
        cleared = session.execute(
            update(EmailSuppression)
            .where(EmailSuppression.id == active, EmailSuppression.cleared_at.is_(None))
            .values(cleared_at=datetime.now(timezone.utc), cleared_by=by)
        ).rowcount
        session.commit()
    return bool(cleared)


def deliveries(
    *, recipient: Optional[str] = None, status: Optional[str] = None, before: Optional[datetime] = None,
    limit: int = PAGE_SIZE,
) -> list[EmailDelivery]:
    """One page of the log, newest first, detached: `recipient` matches any part of the address (any case),
    `status` one status, `before` pages on from the last row's `created_at`."""
    query = select(EmailDelivery)
    if recipient:
        query = query.where(EmailDelivery.recipient.icontains(recipient, autoescape=True))
    if status:
        query = query.where(EmailDelivery.status == status)
    if before is not None:
        query = query.where(EmailDelivery.created_at < before)
    with _session() as session:
        rows = list(session.execute(
            query.order_by(EmailDelivery.created_at.desc(), EmailDelivery.id.desc()).limit(limit)
        ).scalars())
        session.expunge_all()
        return rows
