"""System mail: every message the control plane sends as itself, through Amazon SES.

The provider-neutral contract every system send path uses (seam S4) — the outbox (`app/durable/notify_outbox.py`)
and the operator's test send (`routes/email_templates.py`):

  * `configured()` — whether this deployment can send at all. The domain and configuration set are the S1 env
    the deployment sets (`SES_MAIL_DOMAIN`, `SES_CONFIGURATION_SET`; region
    `AWS_REGION`): unset, as on a local stack, is "not configured" — the caller's 409, never a failed send.
  * `from_address()` — `<local part>@<domain>`, the local part the operator knob `system_mail_from_local`
    (default `notifications`); the display name is `system_mail_sender_name`. Both are read per call, so a
    Settings save takes effect at once (Settings > Integrations > Email).
  * `send(msg, *, delivery_id)` — builds the MIME (`app/mail_message.py`), sends it through the configuration set
    tagged `delivery_id` and `mail_type` (seam S2, so SES events find their `email_deliveries` row), and returns
    SES's MessageId. Raises `NotConfigured`, or `SendError` whose `status` is the provider's HTTP status (None:
    unreachable) and `retryable` says whether another try may succeed.
  * `fallback_send(msg)` — the same message from the operator org's (org zero's) tenant Gmail connection
    (`app/tenant_gmail/`, Settings › Organizations › Email), the oldest one an operator (a superadmin) connected:
    what the outbox sends once SES finally refuses one. Returns `(Gmail's message id, the From address)`. Raises
    `NoFallback` when org zero has no such connection, or `SendError` (Google's status) when Gmail refuses. No plan
    switch: system mail is the deployment's.

"""
from __future__ import annotations

import contextlib
import os
import re
from email.utils import make_msgid
from typing import TYPE_CHECKING, Any, Iterator, Optional
from uuid import UUID

from app import edition_features, mail_message

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.tenant_email import GmailConnection

DOMAIN_VARIABLE = "SES_MAIL_DOMAIN"
CONFIGURATION_SET_VARIABLE = "SES_CONFIGURATION_SET"
SENDER_NAME_KNOB = "system_mail_sender_name"
FROM_LOCAL_KNOB = "system_mail_from_local"
DEFAULT_FROM_LOCAL = "notifications"

#: A From local part the operator may choose: lower-case letters, digits and `.`/`_`/`-` inside, never a `+` tag.
LOCAL_PART_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_DOMAIN_PATTERN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_CONFIGURATION_SET_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


class NotConfigured(Exception):
    """This deployment has no SES sending set up; `str(exc)` names what is missing."""


class NoFallback(Exception):
    """The operator org has no operator-connected Gmail connection to send from once SES refuses."""


class SendError(Exception):
    """The provider refused the message or could not be reached. `status` is its HTTP status (None: unreachable or
    never sent); `str(exc)` names the provider's error and never the recipient."""

    def __init__(self, message: str, *, status: Optional[int], code: Optional[str] = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


def _domain() -> str:
    value = os.environ.get(DOMAIN_VARIABLE, "").strip().lower()
    return value if _DOMAIN_PATTERN.fullmatch(value) else ""


def on_mail_domain(address: str) -> bool:
    """Whether `address` is on this deployment's mail domain: sendable in the sandbox already, and never an SES
    address identity (one would override the domain identity's settings for that address)."""
    domain = _domain()
    return bool(domain) and address.rsplit("@", 1)[-1].strip().lower().rstrip(".") == domain


def _configuration_set() -> str:
    value = os.environ.get(CONFIGURATION_SET_VARIABLE, "").strip()
    return value if _CONFIGURATION_SET_PATTERN.fullmatch(value) else ""


def _region_set() -> bool:
    if not edition_features.enabled("system_mail"):
        # No SES in this edition, so system mail is never configured.
        return False
    from app.aws import ses

    return bool(ses.common.REGION_PATTERN.fullmatch(os.environ.get(ses.REGION_VARIABLE, "").strip()))


def configured() -> bool:
    """Whether system mail can be sent at all (the SES domain, configuration set and AWS region are set)."""
    return bool(_domain() and _configuration_set() and _region_set())


def from_local() -> str:
    """The From local part the operator chose, else `notifications` (also for a value the pattern refuses)."""
    from app.operator_settings import reads as settings_reads

    value = settings_reads.get_text(FROM_LOCAL_KNOB).strip().lower()
    return value if LOCAL_PART_PATTERN.fullmatch(value) else DEFAULT_FROM_LOCAL


def sender_name() -> str:
    """The From display name the operator chose; blank sends from the bare address."""
    from app.operator_settings import reads as settings_reads

    return settings_reads.get_text(SENDER_NAME_KNOB).strip()


def from_address() -> str:
    """The address system mail is sent from, e.g. `notifications@mail.example.com`. Raises `NotConfigured`."""
    domain = _domain()
    if not domain or not _configuration_set() or not _region_set():
        raise NotConfigured(
            f"system mail is not set up ({DOMAIN_VARIABLE}, {CONFIGURATION_SET_VARIABLE} and AWS_REGION)"
        )
    return f"{from_local()}@{domain}"


def send(msg: mail_message.OutboundEmail, *, delivery_id: UUID) -> str:
    """Send `msg` through SES; its MessageId. Raises `NotConfigured` or `SendError` (see the module docstring)."""
    sender = from_address()
    try:
        message = mail_message.build(
            sender, [msg.to], [], subject=msg.subject, text=msg.text, html=msg.html, sender_name=sender_name(),
            reply_to=msg.reply_to, extra_headers=msg.headers,
        )
    except mail_message.InvalidMessage as exc:
        # Our own refusal before any send: final, as SES's 400 would be.
        raise SendError(str(exc), status=400, code="InvalidMessage") from None
    if not edition_features.enabled("system_mail"):
        raise NotConfigured("system mail is not in this edition")
    from app.aws import ses

    try:
        return ses.send_raw(
            # The whole From line, display name included: SES stamps `FromEmailAddress` over the raw From header.
            mail_message.wire_bytes(message), configuration_set=_configuration_set(), from_address=str(message["From"]),
            tags={"delivery_id": delivery_id.hex, "mail_type": msg.mail_type},
        )
    except ses.SesError as exc:
        raise SendError(str(exc), status=exc.status_code, code=exc.code, retryable=exc.retryable) from None
    except ValueError as exc:
        # `send_raw`'s own refusal (a message over SES's size, a tag outside its alphabet): final, never retried.
        raise SendError(str(exc), status=400, code="InvalidMessage") from None


@contextlib.contextmanager
def _operator_session() -> Iterator[Any]:
    """A fresh operator-plane session, out of any caller scope: the fallback mailbox is org zero's whoever sends."""
    from app.db import SessionLocal
    from app.tenancy import seam

    with seam.detached(), seam.operator_scope(), SessionLocal() as session:
        yield session


def fallback_connection(session: Session) -> Optional[GmailConnection]:
    """The Gmail connection `fallback_send` sends from: org zero's oldest whose creator is a superadmin — an org-zero
    member who is not an operator never chooses the mailbox system mail goes out from. None when there is none."""
    from sqlalchemy import select

    from app.models.identity import AdminUser
    from app.models.tenant_email import GmailConnection
    from app.tenancy.ids import ORG_ZERO_ID

    return session.scalars(
        select(GmailConnection)
        .join(AdminUser, AdminUser.id == GmailConnection.created_by_id)
        .where(GmailConnection.org_id == ORG_ZERO_ID, AdminUser.is_superadmin.is_(True))
        .order_by(GmailConnection.created_at, GmailConnection.id).limit(1)
    ).first()


def fallback_send(msg: mail_message.OutboundEmail) -> tuple[str, str]:
    """Send `msg` from `fallback_connection`; `(Gmail's message id, the From address)`. Raises `NoFallback` or
    `SendError` (see the module docstring)."""
    if not edition_features.enabled("tenant_mail"):
        raise NoFallback("this edition has no Gmail connections")
    from app import tenant_gmail
    from app.tenant_gmail import client

    with _operator_session() as session:
        connection = fallback_connection(session)
        if connection is None:
            raise NoFallback("the operator org has no Gmail connection an operator made")
        session.expunge(connection)
    try:
        message = mail_message.build(
            connection.address, [msg.to], [], subject=msg.subject, text=msg.text, html=msg.html,
            sender_name=sender_name(), reply_to=msg.reply_to, extra_headers=msg.headers,
            message_id=make_msgid(domain=connection.address.rsplit("@", 1)[-1]),
        )
    except mail_message.InvalidMessage as exc:
        raise SendError(str(exc), status=400, code="InvalidMessage") from None
    try:
        gmail_id = client.send_message(tenant_gmail._access_token(connection), message)
    except tenant_gmail.GmailRefused as exc:
        raise SendError(str(exc), status=exc.status) from None
    except client.GmailError as exc:
        # Google's own words can echo the message; the sentence names the status alone.
        raise SendError(tenant_gmail._google_sentence(exc), status=exc.status) from None
    return gmail_id, connection.address
