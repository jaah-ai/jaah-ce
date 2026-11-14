"""Node hosting: allowed instance types, node hostings and their changes."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.crons import in_domain_sql
from app.models.base import (
    Base,
    EntityMixin,
    _in_check,
    org_unique,
    OrgScoped,
    StampedMixin,
    timestamp_column,
    UuidPkMixin,
)


#: The clouds a row may name (`allowed_instance_types.provider`, `org_shared_stores.provider`): the database's copy of
#: `app/clouds/base.py::ProviderKey` (models may not import `app.clouds`, which reads the plan; `tests/test_clouds.py`
#: pins the two equal).
CLOUD_PROVIDERS = ("aws", "gcp", "azure")


#: `allowed_instance_types.architecture`: what EC2 reports a type's processor as.
INSTANCE_ARCHITECTURES = ("x86_64", "arm64")


class AllowedInstanceType(EntityMixin, Base):
    """One instance type the operator allows (revision 0420): the ONE allow-list the cloud-node
    launch path, node groups and the hosting products read (`app/instance_types.py` is its owner). `name` is the
    instance type; `position` is the operator's order (the first entry is a blank instance-type knob's value).
    The spec columns are a snapshot of what the cloud described in `specs_region` at `specs_at`, NULL until looked
    up. `provider` names the cloud (`app/clouds/base.py::ProviderKey`; revision 202610211437): a type is unique per
    provider. `windows_capable`: a Windows node may launch on it (AWS rows backfilled by
    `instance_types.windows_capable`'s rule)."""

    __tablename__ = "allowed_instance_types"
    __table_args__ = (
        UniqueConstraint("provider", "instance_type", name="uq_allowed_instance_types_provider_instance_type"),
        CheckConstraint(in_domain_sql("provider", CLOUD_PROVIDERS), name="ck_allowed_instance_types_provider"),
        CheckConstraint(
            "architecture IS NULL OR architecture IN ("
            + ", ".join(f"'{a}'" for a in INSTANCE_ARCHITECTURES) + ")",
            name="ck_allowed_instance_types_architecture",
        ),
        Index("ix_allowed_instance_types_position", "position"),
    )

    provider: Mapped[str] = mapped_column(Text, nullable=False, default="aws", server_default="aws")
    instance_type: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    family: Mapped[str] = mapped_column(Text, nullable=False)
    vcpu: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_mib: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    architecture: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    network_performance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_generation: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    gpu_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # What VM isolation the type can host: KVM on a metal type, or EC2 nested virtualization.
    bare_metal: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    nested_virtualization: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    specs_region: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    specs_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    windows_capable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))


#: `node_rentals.kind`: a Dedicated host, a Shared node, or a node a plan bundles (revision 0374).
NODE_HOSTING_KINDS = ("dedicated", "shared", "bundled")


#: `node_rentals.billing`: charged a month in advance, or free.
NODE_HOSTING_BILLINGS = ("monthly", "free")


#: `node_rentals.status` (`app/billing/hostings.py`, Architecture §4.5).
NODE_HOSTING_STATUSES = (
    "pending_payment", "active", "past_due", "stopped", "cancelling", "terminated", "failed",
)


#: `node_rentals.stop_cause`: why a `stopped` hosting stopped, which decides how it resumes (G3).
NODE_HOSTING_STOP_CAUSES = ("non_payment", "cancel", "plan_end")


class NodeHosting(EntityMixin, OrgScoped, Base):
    """One hosted machine or node and where its billing stands (revision 0354). Written by
    `app/billing/hostings.py`; a hosting is the billable unit — the host for Dedicated (`host_node_id`), the node
    for Shared and bundled (`node_id`). Co-located nodes on a hosted host have no row of their own.

    A monthly hosting's money rides the org's ONE subscription as licensed items (`stripe_price_ids`, one Stripe
    Price per unit it bills: instance + storage, or the Shared tier); `stripe_subscription_id` is that
    subscription, `first_invoice_id` the invoice its first month was paid on, and `overlap_credit_cents` the
    credit for the days that month overlaps the next combined bill (Stripe's own figure). `monthly_cents` is the
    month's price as charged. `attempt` advances on each declined first payment, so a new card is a new Stripe
    request. Both node references are `SET NULL`: a terminated hosting outlives its machine."""

    __tablename__ = "node_rentals"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", NODE_HOSTING_KINDS, "ck_node_rentals_kind"),
        _in_check("billing", NODE_HOSTING_BILLINGS, "ck_node_rentals_billing"),
        _in_check("status", NODE_HOSTING_STATUSES, "ck_node_rentals_status"),
        CheckConstraint(
            "stop_cause IS NULL OR stop_cause IN (" + ", ".join(f"'{c}'" for c in NODE_HOSTING_STOP_CAUSES) + ")",
            name="ck_node_rentals_stop_cause",
        ),
        Index("ix_node_rentals_org_id_created_at", "org_id", "created_at"),
        Index("ix_node_rentals_host_node_id", "host_node_id"),
        Index("ix_node_rentals_node_id", "node_id"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    billing: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    status_since: Mapped[datetime] = timestamp_column()
    # A cancelled hosting runs to here (the end of its paid month), then stops.
    cancel_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    stopped_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    stop_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    host_node_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL", name="fk_node_rentals_host_node_id"), nullable=True
    )
    node_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL", name="fk_node_rentals_node_id"), nullable=True
    )
    instance_price_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("product_options.id", ondelete="RESTRICT", name="fk_node_rentals_instance_price_id"), nullable=True
    )
    storage_price_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("product_options.id", ondelete="RESTRICT", name="fk_node_rentals_storage_price_id"), nullable=True
    )
    shared_price_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("product_options.id", ondelete="RESTRICT", name="fk_node_rentals_shared_price_id"), nullable=True
    )
    # The four product columns name the hosting products' options (`product_options`, revision 202611141800; option
    # ids are the legacy catalogue row ids). A Windows Dedicated host's licence option (key = its type); NULL = Ubuntu.
    windows_price_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("product_options.id", ondelete="RESTRICT", name="fk_node_rentals_windows_price_id"), nullable=True
    )
    instance_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    storage_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    monthly_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stripe_price_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # True when the first month MADE `stripe_subscription_id` (a hostings-only subscription, R-1), so its first
    # invoice is that subscription's; False when it rode a live one on a one-off invoice.
    owns_subscription: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    first_invoice_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The operator-raised first invoice's due date (Stripe's `due_date`); unused until the billed operator add.
    first_invoice_due_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    overlap_credit_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


#: `hosting_changes.mode`: the type changes on the running machine, or on a new one.
HOSTING_CHANGE_MODES = ("in_place", "reprovision")


#: `hosting_changes.state` (`app/billing/hostings.py`): quoted → paying → resizing_storage | changing_type |
#: reprovisioning → done, or reverting → reverted, or stuck; a quote that never paid ends declined or expired.
HOSTING_CHANGE_STATES = (
    "quoted", "paying", "resizing_storage", "changing_type", "reprovisioning", "reverting",
    "done", "reverted", "stuck", "declined", "expired",
)


#: A change in one of these holds its hosting: one at a time (`uq_hosting_changes_in_flight`, DEC-11).
HOSTING_CHANGE_IN_FLIGHT = ("paying", "resizing_storage", "changing_type", "reprovisioning", "reverting", "stuck")


class HostingChange(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """One product change of a Dedicated hosting (revision 202610111300): its quote, its payment and its machine
    progress, from `from_*` to `to_*`. Written by `app/billing/hostings.py` (quote, payment, reversal) and the
    machine workflow (`steps`, `step`, `state` after payment); `node_rentals` keeps the billed product, written only
    by `_price`.

    The entity shape without `name`, `description` or `slug`: a change is a child record of its hosting, named by
    it, and nobody CRUDs one (`tests/test_entity_shape.py` lists it exempt). `updated_at` is set by its writers.
    `amount_due_cents` is Stripe's own preview total (negative = credited), never computed here (NC-9)."""

    __tablename__ = "hosting_changes"
    __table_args__ = (
        _in_check("mode", HOSTING_CHANGE_MODES, "ck_hosting_changes_mode"),
        _in_check("state", HOSTING_CHANGE_STATES, "ck_hosting_changes_state"),
        CheckConstraint("to_storage_gb >= from_storage_gb", name="ck_hosting_changes_no_shrink"),
        Index(
            "uq_hosting_changes_in_flight", "rental_id", unique=True,
            postgresql_where=text("state IN (" + ", ".join(f"'{s}'" for s in HOSTING_CHANGE_IN_FLIGHT) + ")"),
        ),
        Index("ix_hosting_changes_rental_id_created_at", "rental_id", "created_at"),
    )

    rental_id: Mapped[UUID] = mapped_column(
        ForeignKey("node_rentals.id", ondelete="CASCADE", name="fk_hosting_changes_rental_id"), nullable=False
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    from_instance_type: Mapped[str] = mapped_column(Text, nullable=False)
    to_instance_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_storage_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    to_storage_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    # The hosting catalogue's option ids (for `_price` and the reversal) and the Stripe Prices the swap moves.
    from_price_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    to_price_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    from_stripe_price_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    to_stripe_price_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Stripe's `proration_date` (unix seconds): the quote's, reused by the apply and the reversal.
    proration_date: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_due_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    quote_expires_at: Mapped[datetime] = timestamp_column(server_now=False)
    invoice_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    revert_invoice_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The last machine step reached (`stopped`, `modified`, `volume_modified`, `new_host_launched`, ...).
    step: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_host_node_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL", name="fk_hosting_changes_new_host_node_id"), nullable=True
    )
    # The user-facing sentence of a reverted or stuck change.
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The timeline: `{step, at, detail}` per step; `detail` carries instance ids, never a secret.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    sessions_at_quote: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
