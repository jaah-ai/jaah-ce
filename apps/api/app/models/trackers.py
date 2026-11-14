"""Issue-tracker intake (`app/trackers/`): an org's connection to Linear, Jira or Backlog, the Jaah projects each of
its tracker containers is linked to, the issue each tracker Task was started from, and the write-backs posted to that
issue. Revision 202611102000 creates three; 202611122000 adds the links (`tracker_mappings`)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Identity, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    EntityMixin,
    OrgScoped,
    StampedMixin,
    UuidPkMixin,
    _in_check,
    org_unique,
    timestamp_column,
)
from app.trackers.vocabulary import (
    AUTH_KINDS,
    LINK_ORIGINS,
    ORIGIN_INTAKE,
    SCOPE_FULL,
    TRACKER_CONNECTIONS_TABLE,
    TRACKER_ISSUE_LINKS_TABLE,
    TRACKER_MAPPINGS_TABLE,
    TRACKER_WRITEBACKS_TABLE,
    TRACKERS,
    WRITEBACK_KINDS,
    WRITEBACK_SCOPES,
)


class TrackerConnection(EntityMixin, OrgScoped, Base):
    """One org's connection to one tracker workspace, site or space (`tracker`), made once by an org admin
    (`trackers.connections.connect`). `name` is the server-written label. No slug: reached from the org's
    integrations, never by a typed handle.

    The tracker's own token lives in the linked credential (`credential_id`, `auth_kind` `oauth`/`basic`) or, for a
    Jira Forge app, sealed here (`forge_token_sealed`, a row envelope). What the tracker presents to Jaah is never
    stored in the clear: the per-connection URL token (`connection_token_hash`) and the Forge claim code
    (`claim_code_hash`) as SHA-256, the webhook signing secret sealed (`webhook_secret_sealed`). Each is answered once,
    at connect. `trigger_id` is the connection's own trigger: every event becomes one of its deliveries, so tracker
    starts show in Automations. `settings` holds the start label, done state and the unmapped-user switch
    (`trackers.vocabulary.DEFAULT_SETTINGS`); which Jaah project each tracker container feeds is `TrackerMapping`.
    `last_error` is the latest outbound failure's sentence (a `TrackerError` message, never a token), cleared by the
    next successful call (`trackers.project_links.record_outbound`)."""

    __tablename__ = TRACKER_CONNECTIONS_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("tracker", TRACKERS, "ck_tracker_connections_tracker"),
        _in_check("auth_kind", AUTH_KINDS, "ck_tracker_connections_auth_kind"),
        UniqueConstraint("connection_token_hash", name="uq_tracker_connections_connection_token_hash"),
        # One connection per tracker workspace, fleet-wide: the shared ingress finds a connection by it, so two orgs
        # holding one workspace would send its events to whichever connected first.
        Index("uq_tracker_connections_external_org", "tracker", "external_org_id", unique=True,
              postgresql_where=text("external_org_id IS NOT NULL")),
    )

    tracker: Mapped[str] = mapped_column(Text, nullable=False)
    auth_kind: Mapped[str] = mapped_column(Text, nullable=False)
    site: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    external_org_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    credential_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    forge_token_sealed: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    forge_token_expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    claim_code_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claim_expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    connection_token_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    webhook_secret_sealed: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    trigger_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_triggers.id", ondelete="SET NULL"), nullable=True
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class TrackerMapping(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """One link of a connection's tracker container (team, project, space) to a Jaah project — the one link model:
    the project page and the connection's Mapping tab edit these rows (`trackers.project_links`). `container_id` NULL
    is a project linked to the connection with no container picked yet. A container may be linked to several
    projects; exactly one link per container is `is_primary` and receives tracker-started tasks (the service keeps
    the rule under a lock on the connection row). An association row: no name, slug or description."""

    __tablename__ = TRACKER_MAPPINGS_TABLE
    __table_args__ = (
        UniqueConstraint("connection_id", "project_id", "container_id", name="uq_tracker_mappings_link",
                         postgresql_nulls_not_distinct=True),
        Index("uq_tracker_mappings_primary", "connection_id", "container_id", unique=True,
              postgresql_where=text("is_primary")),
        CheckConstraint("NOT is_primary OR container_id IS NOT NULL",
                        name="ck_tracker_mappings_primary_needs_container"),
        Index("ix_tracker_mappings_project_id", "project_id"),
    )

    connection_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tracker_connections.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    container_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    container_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))


class TrackerIssueLink(OrgScoped, Base):
    """The tracker issue one Task was started from (`task_id` unique: one issue per task). One live task per issue is
    the intake's rule (`trackers.intake.live_link`), not a constraint: a finished task's link stays as history.
    `started_by_external` is who started it in the tracker (`{tracker, user_id, name, member_id}`). `origin`
    (`LINK_ORIGINS`) and `writeback_scope` (`WRITEBACK_SCOPES`) say how the link came to be and what is posted back."""

    __tablename__ = TRACKER_ISSUE_LINKS_TABLE
    __table_args__ = (
        _in_check("origin", LINK_ORIGINS, "ck_tracker_issue_links_origin"),
        _in_check("writeback_scope", WRITEBACK_SCOPES, "ck_tracker_issue_links_writeback_scope"),
        UniqueConstraint("task_id", name="uq_tracker_issue_links_task_id"),
        Index("ix_tracker_issue_links_issue", "connection_id", "issue_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    connection_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tracker_connections.id", ondelete="CASCADE"), nullable=False
    )
    issue_key: Mapped[str] = mapped_column(Text, nullable=False)
    issue_url: Mapped[str] = mapped_column(Text, nullable=False)
    container_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_by_external: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default=ORIGIN_INTAKE)
    writeback_scope: Mapped[str] = mapped_column(Text, nullable=False, server_default=SCOPE_FULL)
    created_at: Mapped[datetime] = timestamp_column()


class TrackerWriteback(OrgScoped, Base):
    """One thing Jaah posted back to a task's issue (`kind`), once: the unique key is the write-back's idempotency.
    `ref` names the instance where a kind repeats (a question round, a PR), `''` where it does not."""

    __tablename__ = TRACKER_WRITEBACKS_TABLE
    __table_args__ = (
        _in_check("kind", WRITEBACK_KINDS, "ck_tracker_writebacks_kind"),
        UniqueConstraint("task_id", "kind", "ref", name="uq_tracker_writebacks_task_kind_ref"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = timestamp_column()
