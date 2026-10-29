"""Outbound and inbound notifications: alerts, Slack threads and events, email templates and mailboxes."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
# Stdlib-only leaves: the ticket and email-template CHECKs render from their vocabularies.
from app import email_templates
from app.models.base import (
    Base, EntityMixin, _in_check, org_unique, OrgScoped, StampedMixin, timestamp_column, UuidPkMixin,
)


class Alert(OrgScoped, Base):
    """One alert accepted for a dedup window.

    The unique key is the dedup: a second alert with the same identity in the same 300s bucket
    conflicts, and the conflict suppresses it. A row is window ownership plus audit, not a delivery
    record: `create_alert` withdraws it when the relay failed (so a retry is not suppressed) or the alert
    was `test`-flagged, and keeps it undelivered when the relay is off or unconfigured.

    Omittable members are NOT NULL with sentinels (`repo`/`issue` `''`, `task_id`/`question_id` `0`),
    since NULLs never conflict in a unique index. `issue` carries the per-slot reap identity;
    `task_id`/`question_id` the per-Task and per-round one (each open round is its own Slack card).
    `window_start` is a tumbling bucket (a unique key cannot slide): alerts 290s apart in one bucket
    collapse, a disclosed loss direction."""

    __tablename__ = "alerts"
    __table_args__ = (
        org_unique(__tablename__),
        # The name must never change: `alert_record.record_alert` uses `ON CONFLICT ON CONSTRAINT` by
        # name (migration 0119 widened the tuple under it).
        UniqueConstraint(
            "repo", "issue", "kind", "window_start", "task_id", "question_id",
            name="uq_alerts_dedup",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # SET NULL (0106). Not part of `uq_alerts_dedup`.
    node_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    repo: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    issue: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # `0` = not scoped to a Task/round (sentinel, as `''` above). Not an FK: the audit row outlives the
    # Task, and no FK can point at a sentinel.
    task_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    question_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    window_start: Mapped[datetime] = timestamp_column(server_now=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = timestamp_column()


class SlackThread(OrgScoped, Base):
    """The thread ↔ Task correlation record: a row means "a reply in this Slack thread answers this
    Task", and it is the only place that fact lives.

    Not `alerts.payload`: alerts are withdrawn on relay failure and window-scoped, while a correlation
    must outlive both. `channel_id`/`thread_ts` come from the `chat.postMessage` response and the row is
    written only after Slack accepts the post (the relay is DB-free), so failed posts leave no orphan; the
    pair is the row's identity. `task_id` cascades; `admin_user_id` SET NULL makes the thread unanswerable
    rather than misattributed.

    `question_id` names the round the thread was posted for, so several live threads per Task are
    unambiguous. NULL for the roundless park (`task_pipeline_cp.block_task`) and pre-0119 rows, which keep
    the older rule: only the newest row per Task is answerable (`slack_inbound._is_stale_thread`)."""

    __tablename__ = "slack_threads"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("channel_id", "thread_ts", name="uq_slack_threads_channel_thread"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_ts: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The round this thread was posted for; NULL = names none. Not an FK: the thread must stay readable
    # as audit after the round cascades away.
    question_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # A brief recipient's one line for a question batch (`alert_relay.brief_skips`): it speaks for every round
    # of the batch from `question_id` on, so it settles when the batch does (`slack_answer_echo._batch_lines`).
    brief: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    admin_user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = timestamp_column()


class SlackEvent(Base):
    """The durable at-most-once record for an inbound Slack Socket Mode envelope.

    The unique key on `event_id` is the dedup (as `Alert`): a duplicate delivery is a uniqueness
    violation. An in-process set fails across replicas and restarts. Retention is short (Slack retries
    within minutes); the indexed `created_at` backs the prune."""

    __tablename__ = "slack_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_slack_events_event_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = timestamp_column(index=True)


class EmailTemplate(EntityMixin, Base):
    """One operator-written content set for one email template TYPE (Settings > Integrations > Email
    templates, `app/routes/email_templates.py`). The type's layout and field set are code
    (`app/email_templates.py`); `fields` holds only the text of those fields, validated on every write and
    re-validated on every read (`app/email_template_store.py::active_fields`).

    Deployment-wide, no `org_id`: the mailbox that sends is the deployment's own (`Shared` in
    `app/tenancy/manifest.py`). At most one row per type is active (`uq_email_templates_active_type`); none
    active means the type's built-in defaults. `name` is unique per type. No `slug`: reached by id, and it
    materializes nowhere outside Postgres. Revision 0332 spells the `type` CHECK as a frozen literal.
    """

    __tablename__ = "email_templates"
    __table_args__ = (
        UniqueConstraint("type", "name", name="uq_email_templates_type_name"),
        _in_check("type", email_templates.TYPE_KEYS, "ck_email_templates_type"),
        CheckConstraint("jsonb_typeof(fields) = 'object'", name="ck_email_templates_fields_object"),
        Index("uq_email_templates_active_type", "type", unique=True, postgresql_where=text("is_active")),
    )

    type: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class EmailMailbox(UuidPkMixin, OrgScoped, Base):
    """RETIRED: the Gmail push cursor of the deployment mailbox system mail was once received into. Nothing reads or
    writes it since inbound moved to SES (`app/ses_inbound.py`); the table stays while the release before that cutover
    can still run, and is dropped with `inbound_emails.gmail_message_id` (F4). Machine state, not a user entity
    (`tests/test_entity_shape.py`)."""

    __tablename__ = "email_mailboxes"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("address", name="uq_email_mailboxes_address"),
    )

    address: Mapped[str] = mapped_column(Text, nullable=False)
    history_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    watch_expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    updated_at: Mapped[datetime] = timestamp_column()


#: Where an `inbound_emails` row came from (`provider`): the retired deployment Gmail mailbox (history only), SES
#: system inbound (`app/ses_inbound.py`), or a tenant's Gmail connection.
INBOUND_EMAIL_PROVIDERS: tuple[str, ...] = ("gmail", "ses", "tenant_gmail")
#: Why a stored message was never routed (`ses_inbound.py` owns each value's meaning); NULL = routed as usual.
INBOUND_REJECTED_REASONS: tuple[str, ...] = ("spam", "virus", "dmarc", "rate_limited", "plan_off")


class InboundEmail(UuidPkMixin, OrgScoped, Base):
    """One message received, stored as it arrived, one row per target it resolved to (`app/ses_inbound.py`).

    `org_id` is the target's org: org zero for system mail, a tenant's for its hosted address (`tenant_address_id`) or
    its service-account Gmail (`provider = 'tenant_gmail'`, `tenant_connection_id`). Routed once by `trigger_intake.route_emails`
    (`trigger_routed_at`), unless `rejected_reason` says it must not be (listed to the operator as Rejected).
    Append-only, so no entity shape. Unique on (`provider`, `source_ref`, `provider_message_id`, `recipient`), so a
    queue message handed out twice stores each copy once. Gmail-era rows (`provider = 'gmail'`) keep
    `gmail_message_id`, which new rows leave NULL."""

    __tablename__ = "inbound_emails"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("gmail_message_id", name="uq_inbound_emails_gmail_message_id"),
        UniqueConstraint(
            "provider", "source_ref", "provider_message_id", "recipient", name="uq_inbound_emails_provider_message"
        ),
        _in_check("provider", INBOUND_EMAIL_PROVIDERS, "ck_inbound_emails_provider"),
        _in_check("rejected_reason", INBOUND_REJECTED_REASONS, "ck_inbound_emails_rejected_reason"),
        Index("ix_inbound_emails_received_at", text("received_at DESC")),
        # The rate limit's count (`ses_inbound.over_rate_limit`), over SES and tenant Gmail rows alike.
        Index(
            "ix_inbound_emails_rate", "org_id", text("(verdicts ->> 'sender_domain')"), "stored_at",
            postgresql_where=text("rejected_reason IS NULL"),
        ),
        # What a connection's disconnect or an address's revoke sets NULL (revision 202610241100).
        Index("ix_inbound_emails_tenant_connection_id", "tenant_connection_id",
              postgresql_where=text("tenant_connection_id IS NOT NULL")),
        Index("ix_inbound_emails_tenant_address_id", "tenant_address_id",
              postgresql_where=text("tenant_address_id IS NOT NULL")),
    )

    mailbox: Mapped[str] = mapped_column(Text, nullable=False)
    gmail_message_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The `'gmail'` default and a nullable `provider_message_id` only let the release before the SES cutover keep
    # inserting while both run; every row since names both (dropped with `gmail_message_id`, F4).
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'gmail'"))
    #: The provider's id of the message: SES's `mail.messageId`, or Gmail's message id for a Gmail-era row.
    provider_message_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: What received it: `system` (the deployment's mail domain), a connection id, or a hosted-address id.
    source_ref: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'system'"))
    #: The envelope recipient this row was stored for; `''` on a Gmail-era row.
    recipient: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: SES's receipt verdicts (`spam`, `virus`, `spf`, `dkim`, `dmarc`, `dmarc_policy`) and the authenticated
    #: `sender_domain` the rate limit counts by; `{}` on a Gmail-era row.
    verdicts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    rejected_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The tenant Gmail connection that received it (`app/tenant_gmail/receive.py`); NULL once disconnected.
    tenant_connection_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gmail_connections.id", ondelete="SET NULL"), nullable=True
    )
    #: The hosted address that received it (`app/tenant_gmail/addresses.py`); NULL once revoked.
    tenant_address_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant_mail_addresses.id", ondelete="SET NULL"), nullable=True
    )
    thread_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    from_address: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    to_address: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    subject: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # When the provider accepted the message (SES's `mail.timestamp`), not the sender's `Date` header.
    received_at: Mapped[datetime] = timestamp_column(server_now=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # The first text part, capped (`ses_inbound.BODY_MAX`); `body_truncated` says it was cut.
    body_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    body_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    stored_at: Mapped[datetime] = timestamp_column()
    # When the trigger router evaluated this message; NULL = not yet. Stamped once, which is what makes
    # "processed at most once" checkable.
    trigger_routed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


#: An `email_deliveries` row's lifecycle (`app/email_deliveries.py` owns each value's meaning).
EMAIL_DELIVERY_STATUSES: tuple[str, ...] = (
    "queued", "sent", "delivered", "delayed", "bounced", "complained", "suppressed", "failed", "not_configured",
)


class EmailDelivery(UuidPkMixin, OrgScoped, Base):
    """One system email (`app/system_mail.py`), from the moment it is enqueued: the operator's delivery log.

    Always org zero: system mail is the deployment's own, whoever receives it, and no route serves the table to a
    tenant. Written `queued` at enqueue (so a failure is never silent), then by the outbox and by SES's events, found
    by `provider_message_id` (SES's MessageId) or the `delivery_id` tag. Kept 90 days (`app/cron_retention.py`).
    `recipient_user_id` is copied, not a foreign key: the record outlives the person. A history record, not an entity
    (`tests/test_entity_shape.py`)."""

    __tablename__ = "email_deliveries"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("status", EMAIL_DELIVERY_STATUSES, "ck_email_deliveries_status"),
        UniqueConstraint("provider_message_id", name="uq_email_deliveries_provider_message_id"),
        Index("ix_email_deliveries_created_at", "created_at"),
    )

    created_at: Mapped[datetime] = timestamp_column()
    #: The `app/email_templates.py` type key it was rendered from (seam S2's `mail_type` tag).
    mail_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: PRD R6: mail a person cannot unsubscribe from (invitations, sign-up, password reset, billing...).
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_user_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    #: Why it is not (yet) delivered, in words: the provider's error, a bounce's diagnostic. NULL when nothing to say.
    status_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sent_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_event_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: SES's events for this message, newest last and capped (`email_deliveries.EVENTS_CAP`).
    events: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))


#: Why an address is on the system-mail suppression list (`app/email_deliveries.py` owns each value's meaning).
EMAIL_SUPPRESSION_REASONS: tuple[str, ...] = ("bounce", "complaint", "manual")


class EmailSuppression(UuidPkMixin, OrgScoped, Base):
    """One address system mail is not sent to (PRD R4): a hard bounce or a complaint put it here, and it stays until
    the operator clears it (`cleared_at`). At most one ACTIVE row per address, stored lower-case; a cleared row is
    kept as the history of that address. Always org zero, like the delivery log it guards, and served to no tenant.
    `source_delivery_id` is copied, not a foreign key: the delivery row is swept after 90 days, the suppression is
    not. A history record, not an entity (`tests/test_entity_shape.py`)."""

    __tablename__ = "email_suppressions"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("reason", EMAIL_SUPPRESSION_REASONS, "ck_email_suppressions_reason"),
        Index("uq_email_suppressions_active_address", "address", unique=True,
              postgresql_where=text("cleared_at IS NULL")),
    )

    created_at: Mapped[datetime] = timestamp_column()
    address: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    #: What SES said (a bounce's diagnostic, a complaint's feedback type). NULL when nothing to say.
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_delivery_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    cleared_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    cleared_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


class DmarcReport(UuidPkMixin, OrgScoped, Base):
    """One DMARC aggregate (`rua`) report about the mail domain, as a receiver (Google, Microsoft, ...) sent it to
    `dmarc-reports@<domain>` (PRD R8). Unique per (`reporter_domain`, `report_id`), so a report delivered twice is
    kept once; `reporter_domain` is the domain SES authenticated the message as, `reporter_org` the XML's own name. Always org zero, like the delivery log, and served to no tenant. `inbound_email_id` is copied, not a foreign
    key: the carrying message's row has its own lifetime. Every rule: `app/dmarc.py`. A history record, not an entity
    (`tests/test_entity_shape.py`)."""

    __tablename__ = "dmarc_reports"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("reporter_domain", "report_id", name="uq_dmarc_reports_reporter_domain_report"),
        Index("ix_dmarc_reports_date_end", text("date_end DESC")),
    )

    created_at: Mapped[datetime] = timestamp_column()
    reporter_domain: Mapped[str] = mapped_column(Text, nullable=False)
    reporter_org: Mapped[str] = mapped_column(Text, nullable=False)
    reporter_email: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    report_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: The report's `date_range`: the window of mail it covers.
    date_begin: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    date_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The published policy the receiver applied: its domain and `p=`.
    policy_domain: Mapped[str] = mapped_column(Text, nullable=False)
    policy: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    inbound_email_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


class DmarcReportRow(UuidPkMixin, OrgScoped, Base):
    """One `<record>` of a DMARC report: `count` messages from `source_ip` with one outcome. `dkim` and `spf` are
    the receiver's ALIGNED results (`policy_evaluated`), `dmarc` passes when either does. Goes with its report."""

    __tablename__ = "dmarc_report_rows"
    __table_args__ = (Index("ix_dmarc_report_rows_dmarc_report_id", "dmarc_report_id"),)

    dmarc_report_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dmarc_reports.id", ondelete="CASCADE", name="fk_dmarc_report_rows_dmarc_report_id"),
        nullable=False,
    )
    source_ip: Mapped[str] = mapped_column(Text, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    disposition: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    dkim: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    spf: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    dmarc: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    header_from: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class SlackInstallation(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """An org's Slack workspace, installed through the console's Add to Slack button.

    One row per org (v1: one workspace each). `team_id` routes every inbound Socket Mode envelope to its org
    and is unique among connected rows, so a workspace can never answer for two orgs. `token_sealed` is the
    installation's bot token, a tier-1 envelope bound to the row id (`app/slack_workspaces.py`, which owns every
    rule); cleared on disconnect, so a disconnected row holds no secret. `bot_user_id` is that workspace's
    self-echo filter. An org's setting, not an entity (`tests/test_entity_shape.py`)."""

    __tablename__ = "slack_installations"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", name="uq_slack_installations_org_id"),
        Index(
            "uq_slack_installations_connected_team_id", "team_id", unique=True,
            postgresql_where=text("disconnected_at IS NULL"),
        ),
        CheckConstraint(
            "(disconnected_at IS NULL) = (token_sealed IS NOT NULL)", name="ck_slack_installations_token_iff_connected"
        ),
    )

    team_id: Mapped[str] = mapped_column(String(32), nullable=False)
    team_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    app_id: Mapped[str] = mapped_column(String(32), nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    token_sealed: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    disconnected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why it is disconnected, a short sentence for the console (`slack_workspaces.DISCONNECT_REASONS`).
    disconnect_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
