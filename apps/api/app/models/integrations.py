"""The integration catalog's own tables: the connectors an org defines for itself, and people's votes for a vendor."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import _in_check, Base, EntityMixin, org_unique, OrgScoped, timestamp_column

#: What a custom connector speaks: a REST API, or an MCP server.
CUSTOM_CONNECTOR_KINDS = ("rest", "mcp")
#: How a request carries the connection's secret.
CUSTOM_CONNECTOR_AUTH_SCHEMES = ("bearer", "header_key", "basic", "oauth2_client", "none")
#: An MCP connector's wire transport.
CUSTOM_CONNECTOR_MCP_TRANSPORTS = ("streamable_http", "sse")


class CustomConnector(EntityMixin, OrgScoped, Base):
    """A connector an org defines for itself: a REST API or an MCP server outside the built-in catalog, which its
    connections (credential rows) then name. `slug` is the handle a connection's type refers to it by, so it is
    unique per org. `header_name` / `value_prefix` shape a `header_key` or `bearer` request; the `oauth_*` columns
    are an `oauth2_client` connector's endpoints; `test_path` is what a connection test calls; `mcp_transport` is
    an MCP connector's alone. This revision lands the table only; its routes are a later child (C5)."""

    __tablename__ = "custom_connectors"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_custom_connectors_org_slug"),
        _in_check("kind", CUSTOM_CONNECTOR_KINDS, "ck_custom_connectors_kind"),
        _in_check("auth_scheme", CUSTOM_CONNECTOR_AUTH_SCHEMES, "ck_custom_connectors_auth_scheme"),
        CheckConstraint(
            "mcp_transport IS NULL OR mcp_transport IN ("
            + ", ".join(f"'{v}'" for v in CUSTOM_CONNECTOR_MCP_TRANSPORTS) + ")",
            name="ck_custom_connectors_mcp_transport",
        ),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_scheme: Mapped[str] = mapped_column(Text, nullable=False)
    header_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    value_prefix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    oauth_authorize_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    oauth_token_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    oauth_scope: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    test_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mcp_transport: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class IntegrationVote(Base):
    """One PERSON's (`identity_id`) vote for a catalog vendor (`slug`) still in Beta: the composite primary key is
    "one vote per person", so a second press in any org they act in is the same vote. Withdrawing deletes the row.
    The slug is catalog data (`app/integrations/catalog.py`), not a row, so no foreign key; a vendor leaving the
    catalog leaves its votes unread. Machine-written, so no entity shape."""

    __tablename__ = "integration_votes"

    identity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), primary_key=True
    )
    # Indexed for the per-vendor count (the operator's Settings list).
    slug: Mapped[str] = mapped_column(Text, primary_key=True, index=True)
    created_at: Mapped[datetime] = timestamp_column()
