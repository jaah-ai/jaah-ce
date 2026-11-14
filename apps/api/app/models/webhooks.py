"""Outgoing webhooks (`app/webhooks/`): an org's endpoints and each event delivered to one. Revision 202611110110
creates both."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, OrgScoped, UuidPkMixin, _in_check, org_unique, timestamp_column

#: A delivery's states (`app/webhooks/engine.py`): waiting to be sent or retried, accepted (2xx), given up.
DELIVERY_STATES = ("pending", "succeeded", "failed")


class OrgWebhookEndpoint(EntityMixin, OrgScoped, Base):
    """One URL an org admin points at chosen event types (`event_types`, from the catalog's `[webhooks]` table),
    optionally narrowed to some projects (`project_ids`, NULL = every project). `slug` is unique per org.

    The signing secret is a row envelope (`secret_sealed`), shown once at create and at each rotation; during a
    rotation's overlap (`previous_secret_until`) the old one (`previous_secret_sealed`) signs too. `cursor_event_id`
    is the last `activity_events.id` the delivery pass has turned into this endpoint's deliveries."""

    __tablename__ = "org_webhook_endpoints"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_org_webhook_endpoints_org_slug"),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    event_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    project_ids: Mapped[Optional[list[int]]] = mapped_column(ARRAY(Integer), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    secret_sealed: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    secret_rotated_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    previous_secret_sealed: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    previous_secret_until: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    cursor_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))


class WebhookDelivery(UuidPkMixin, OrgScoped, Base):
    """One event owed to one endpoint, and how sending it went. `(endpoint_id, event_id)` is unique, so a pass that
    sees an event twice inserts it once; a test ping has no `event_id`. `payload` is the body's object, frozen when
    the row is made: a retry or a redelivery sends the same body. Swept after `engine.RETENTION_DAYS`."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        _in_check("state", DELIVERY_STATES, "ck_webhook_deliveries_state"),
        UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_deliveries_endpoint_event"),
        Index("ix_webhook_deliveries_due", "next_attempt_at", postgresql_where=text("state = 'pending'")),
        Index("ix_webhook_deliveries_endpoint_newest", "endpoint_id", "id"),
        Index("ix_webhook_deliveries_created_at", "created_at"),
    )

    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("org_webhook_endpoints.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    next_attempt_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_attempt_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
