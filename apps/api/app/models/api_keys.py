"""Jaah API keys: a member's personal bearer credential and its secrets. `app/api_keys.py` owns every rule."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import CHAR, Boolean, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, INET
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, org_unique, OrgScoped, timestamp_column, UuidPkMixin


class ApiKey(EntityMixin, OrgScoped, Base):
    """One API key: a personal credential of its `owner`, narrowed to `permissions` (or every honoured catalog key
    while `grants_all`) and capped by the owner's own rights at request time. No slug: a key is reached from its
    owner's settings list, never by a typed handle. The secret lives in `api_key_secrets`, hashed.

    `expires_at` is immutable (NULL = never). `revoked_at` ends it for good; `revoke_reason` says why (`revoked`,
    `owner_disabled`). The `reminded_*`/`reminder_seen_at`/`expiry_recorded_at` stamps belong to the expiry sweep.

    The key's org is the one its owner made it in: their own, or for an operator the tenant org they acted as
    (`routes/api_keys.py::create_api_key`); `api_keys.owner_refusal` refuses any other (`owner_id` is in the
    unkeyed-reference register, as the actor columns are)."""

    __tablename__ = "api_keys"
    __table_args__ = (
        org_unique(__tablename__),
        Index("ix_api_keys_owner_id", "owner_id"),
    )

    owner_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_api_keys_owner_id"), nullable=False
    )
    grants_all: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    org_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    ip_allowlist: Mapped[list[str]] = mapped_column(ARRAY(CIDR), nullable=False, server_default=text("'{}'"))
    expires_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    revoked_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    revoked_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_api_keys_revoked_by_id"), nullable=True
    )
    revoke_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_used_ip: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    reminded_7d_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    reminded_1d_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    reminder_seen_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    expiry_recorded_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class ApiKeySecret(UuidPkMixin, OrgScoped, Base):
    """One secret of an API key: only its SHA-256 (`token_hash`) and last four characters are kept. The current
    secret has `valid_until` NULL; a rotation with overlap leaves the previous one valid until then. A child of its
    key, not an entity (`tests/test_entity_shape.py`)."""

    __tablename__ = "api_key_secrets"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_api_key_secrets_token_hash"),
        Index("ix_api_key_secrets_api_key_id", "api_key_id"),
    )

    api_key_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE", name="fk_api_key_secrets_api_key_id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    last4: Mapped[str] = mapped_column(CHAR(4), nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    valid_until: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
