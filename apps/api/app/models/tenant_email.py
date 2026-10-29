"""Tenant email: an org's Gmail connections and the audit row of every message sent through one.
`app/tenant_gmail/` owns every rule."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, org_unique, OrgScoped, timestamp_column, UuidPkMixin


class GmailConnection(EntityMixin, OrgScoped, Base):
    """One Gmail mailbox an org connected: by a member's Google OAuth consent (`kind = 'oauth'`, the
    `gmail.send` scope alone, so it sends and never reads) or by a Workspace service account with domain-wide delegation
    naming the mailbox (`kind = 'service_account'`, send and read). `name` is the mailbox address as connected. No slug:
    a connection is reached from Settings › Organizations › Email, never by a typed handle.

    `sealed_secret` is the OAuth refresh token or the service-account JSON key, a tier-1 envelope bound to the row id
    (`tenant_gmail.seal_secret`). A COLUMN, never a credential row: credential rows are dispatched to runners, and no
    dispatch path reads this table (`tests/test_tenant_gmail.py` holds that), so the secret never reaches a node.
    Disconnecting deletes the row and its secret; its sends stay (`tenant_email_sends.connection_id` keeps the id).

    `history_id` (the Gmail history cursor), `last_sync_at` and `sync_error` belong to receiving
    (`app/tenant_gmail/receive.py`); `last_error` names the latest send failure; `can_receive` is true for a
    service account only."""

    __tablename__ = "gmail_connections"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "address", name="uq_gmail_connections_org_id_address"),
        CheckConstraint("kind IN ('oauth', 'service_account')", name="ck_gmail_connections_kind"),
        CheckConstraint("NOT can_receive OR kind = 'service_account'", name="ck_gmail_connections_receive_kind"),
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The mailbox, a bare lowercased address: what mail is sent From.
    address: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_secret: Mapped[dict] = mapped_column(JSONB, nullable=False)
    can_receive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    history_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    last_sync_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: Why the latest use of the connection failed (a send Google refused, a revoked grant); NULL after one worked.
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Why the latest receive sync failed or what it skipped (an expired cursor); NULL after one worked.
    sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class TenantEmailSend(UuidPkMixin, OrgScoped, Base):
    """One message an org sent through a Gmail connection, or tried to (`status`): written by `tenant_gmail.send`
    whatever the outcome, and kept 365 days (`app/cron_retention.py`). `connection_id` and `from_address` are copied,
    not a foreign key: the record outlives a disconnect. `actor_id` is the person the send is attributed to (an agent's
    task's creator, an automation's owner); `task_id` / `automation_run_id` / `trigger_delivery_id` say what sent it. A history record, not an
    entity (`tests/test_entity_shape.py`)."""

    __tablename__ = "tenant_email_sends"
    __table_args__ = (
        org_unique(__tablename__),
        CheckConstraint("status IN ('sent', 'failed')", name="ck_tenant_email_sends_status"),
        Index("ix_tenant_email_sends_org_id_created_at", "org_id", "created_at"),
        Index("ix_tenant_email_sends_connection_id_created_at", "connection_id", "created_at"),
    )

    created_at: Mapped[datetime] = timestamp_column()
    connection_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    from_address: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    actor_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    #: The sending task's id. Column `sent_by_task_id`: the first `task_id` column was a uuid, which no task id is
    #: (revision 202610240300); it stays unmapped until a later release drops it.
    task_id: Mapped[Optional[int]] = mapped_column("sent_by_task_id", Integer, nullable=True)
    #: A schedule run's `cron_runs` id; an event run's is `trigger_delivery_id` (revision 202610240800).
    automation_run_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    trigger_delivery_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    to_addresses: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    cc_addresses: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    subject: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Why it was not sent; NULL when it was.
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gmail_message_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TenantMailAddress(EntityMixin, OrgScoped, Base):
    """One Jaah-hosted address an org receives mail at: `<org-slug>-<10 base32>@<mail domain>`,
    the random part making it unguessable (AS2). `name` is the label an admin gave it (the address when none).
    `trigger_id` is the email trigger it feeds (seam S8's `address_id`); NULL until one names it, when its mail is
    listed in the org's Unmatched emails. Deleted with its trigger or on revoke; its stored mail keeps the id in
    `inbound_emails.tenant_address_id` until then (SET NULL). Every rule is `app/tenant_gmail/addresses.py`'s."""

    __tablename__ = "tenant_mail_addresses"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("address", name="uq_tenant_mail_addresses_address"),
        Index("ix_tenant_mail_addresses_trigger_id", "trigger_id"),
    )

    #: The full address, lowercased: what the resolver matches an envelope recipient against.
    address: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_triggers.id", ondelete="CASCADE"), nullable=True
    )
