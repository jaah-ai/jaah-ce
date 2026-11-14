"""The product engine's tables (written by `app/products/`).

A catalogue (`products` → `product_options` → `product_prices`), what each org holds (`org_products`), plan
inclusion and comps in one table (`product_lines`), once-per-org trials (`product_trials`) and per-org product
settings (`org_product_settings`). Every table naming a product kind references the `product_kinds` lookup by
foreign key; its rows equal `app/products/kinds.py::KINDS` (a test holds them equal), so the kind list has one home.

Legacy kinds move in one at a time (each its own child task) by a SQL copy that keeps the legacy row's UUID and
Stripe ids; until then their rows live in the legacy tables. `customer_org_id` on `products` and `product_lines`
is a copied pointer to an org (a typed product's buyer, an override or comp line's org), never the tenancy column:
both tables also hold rows that belong to no org (the catalogue, a plan's lines).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    EntityMixin,
    OrgScoped,
    StampedMixin,
    UuidPkMixin,
    _in_check,  # pyright: ignore[reportPrivateUsage]
    org_unique,
    timestamp_column,
)

#: THE product kind keys, each spelled here once. `app/products/kinds.py::KINDS` gives each its behaviour and the
#: `product_kinds` rows equal them (`tests/products/test_kinds.py`). They live here rather than in `app/products/`,
#: which only the billing edition ships, so modules every edition ships (`app/plan_terms.py`, the billing models) name
#: a kind without importing the engine.
KIND_PLAN = "plan"
KIND_SHARED_NODE = "shared_node"
KIND_DEDICATED_NODE = "dedicated_node"
KIND_JAAH_AI_CREDIT = "jaah_ai_credit"
KIND_LIVE_CAST = "live_cast"
KIND_CUSTOM = "custom"
#: The kinds that make a node (hosting's fulfillment: `org_bundled_products.kind`, a node line's fingerprint).
NODE_KINDS = (KIND_SHARED_NODE, KIND_DEDICATED_NODE)
#: The kinds a plan's term lines name (`app/plan_terms.py::ProductLine`): every kind but the plan's own and custom's,
#: whose lines are the engine's alone (`app/products/inclusion.py`).
TERM_LINE_KINDS = (*NODE_KINDS, KIND_JAAH_AI_CREDIT, KIND_LIVE_CAST)

#: Which subscription a recurring price rides: the org's plan subscription, or its add-ons one
#: (`billing_subscriptions.kind = 'hosting'`). A one-off price rides neither (`LANE_ONE_OFF`, never stored).
LANE_PLAN = "plan"
LANE_ADDONS = "addons"
LANE_ONE_OFF = "one_off"
PRODUCT_LANES = (LANE_PLAN, LANE_ADDONS)
#: `quantity_mode`: one unit, a buyer-chosen count, or a count the kind reports (seats, projects).
QUANTITY_MODES = ("fixed", "chosen", "reported")
#: `amount_mode`: the price's own amount, or one the buyer chooses between `min_amount` and `max_amount`.
AMOUNT_MODES = ("fixed", "chosen")
#: `price_policy`: holders keep the price they bought, or move to the current one at their next renewal.
PRICE_POLICIES = ("keep", "move_at_renewal")
#: `product_prices.interval`.
INTERVAL_ONE_OFF = "one_off"
PRICE_INTERVALS = (INTERVAL_ONE_OFF, "month", "year")
#: `org_products.paid_mode`: paid by card, invoiced by email, given free (a comp), or included in the plan.
PAID_MODES = ("card", "invoice", "comped", "included")
FREE_MODES = ("comped", "included")
#: `org_products.status` — `node_rentals`' vocabulary, `terminated` named `ended`.
HOLDING_STATUSES = ("pending_payment", "active", "past_due", "cancelling", "stopped", "ended", "failed")
#: A holding's item still rides its subscription: billed now, or still owed.
BILLED_STATUSES = ("active", "past_due")
#: `product_lines.owner`.
LINE_OWNERS = ("plan", "org_override", "comp")
COMP_END_MODES = ("never", "date", "plan_end")
#: `product_lines.audience`: which of the plan's customers a plan line reaches — every org on the plan, those already
#: on it when the line was added (`existing`), or those that join after (`new`); `audience_at` is that moment.
LINE_AUDIENCES = ("all", "existing", "new")


class ProductKind(Base):
    """One product kind (`app/products/kinds.py::KINDS` is the definition; this lookup row is what foreign keys
    point at). Seeded by the revision that made it; a new kind adds its row in its own revision."""

    __tablename__ = "product_kinds"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)


def kind_fk(table: str) -> Any:
    """A `kind` column's reference to the `product_kinds` lookup (never a CHECK spelling the kinds)."""
    return ForeignKey("product_kinds.key", ondelete="RESTRICT", name=f"fk_{table}_kind")


class Product(EntityMixin, Base):
    """A catalogue product (`customer_org_id` NULL) or a typed one made for one org (never self-serve). Retiring
    sets `retired_at` and deactivates its Stripe Prices; holders keep theirs. `legacy_ref` names the legacy row a
    moved product was copied from (`billing_plans:<id>`); `config` is the kind's own (a plan's `plan_key`)."""

    __tablename__ = "products"
    __table_args__ = (
        _in_check("lane", PRODUCT_LANES, "ck_products_lane"),
        _in_check("quantity_mode", QUANTITY_MODES, "ck_products_quantity_mode"),
        _in_check("amount_mode", AMOUNT_MODES, "ck_products_amount_mode"),
        _in_check("price_policy", PRICE_POLICIES, "ck_products_price_policy"),
        CheckConstraint("min_quantity >= 0 AND (max_quantity IS NULL OR max_quantity >= min_quantity)",
                        name="ck_products_quantity_range"),
        Index("ix_products_kind", "kind"),
        Index("ix_products_customer_org_id", "customer_org_id", postgresql_where=text("customer_org_id IS NOT NULL")),
    )

    kind: Mapped[str] = mapped_column(Text, kind_fk("products"), nullable=False)
    customer_org_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    self_serve: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    retired_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    price_policy: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'keep'"))
    lane: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'addons'"))
    quantity_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'fixed'"))
    min_quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    max_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    amount_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'fixed'"))
    min_amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    max_amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    trial_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stripe_product_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_account_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    livemode: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    legacy_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class ProductOption(EntityMixin, Base):
    """One priced choice of a product along a dimension (`instance_type`, `storage`): `key` is stable, `name` the
    label shown. `attrs` holds the kind's facts its fulfillment validates against (`windows_capable`)."""

    __tablename__ = "product_options"
    __table_args__ = (
        UniqueConstraint("product_id", "dimension", "key", name="uq_product_options_product_dimension_key"),
    )

    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE", name="fk_product_options_product_id"), nullable=False,
    )
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    dimension_label: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    key: Mapped[str] = mapped_column(Text, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    attrs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class ProductPrice(EntityMixin, Base):
    """One price of a product (or of one of its options). An edit is a new `version` row; Stripe Prices are
    immutable, so a holder keeps the row it bought unless the product's `price_policy` moves it at renewal."""

    __tablename__ = "product_prices"
    __table_args__ = (
        _in_check("interval", PRICE_INTERVALS, "ck_product_prices_interval"),
        CheckConstraint("unit_amount >= 0", name="ck_product_prices_unit_amount"),
        Index("ix_product_prices_product_id", "product_id"),
    )

    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE", name="fk_product_prices_product_id"), nullable=False,
    )
    option_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("product_options.id", ondelete="CASCADE", name="fk_product_prices_option_id"), nullable=True,
    )
    interval: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'usd'"))
    unit_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    stripe_price_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_account_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    livemode: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    legacy_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ProductLine(EntityMixin, Base):
    """What a plan includes, what one org's override lists, or a comp: `quantity` of a product (with its
    `options` and chosen `amount`). A comp ends by `end_mode` (`ends_at` for a date); `ended_at`/`ended_by_id`
    record an operator's removal. Plan lines name `plan_id` (`billing_plans.id`, a pointer kept until the plan
    kind moves); override and comp lines name `customer_org_id`."""

    __tablename__ = "product_lines"
    __table_args__ = (
        _in_check("owner", LINE_OWNERS, "ck_product_lines_owner"),
        CheckConstraint("end_mode IS NULL OR end_mode IN (" + ", ".join(f"'{m}'" for m in COMP_END_MODES) + ")",
                        name="ck_product_lines_end_mode"),
        CheckConstraint("(owner = 'comp') = (end_mode IS NOT NULL)", name="ck_product_lines_comp_end"),
        CheckConstraint("(end_mode = 'date') = (ends_at IS NOT NULL)", name="ck_product_lines_ends_at"),
        CheckConstraint("(owner = 'plan') = (plan_id IS NOT NULL AND customer_org_id IS NULL)",
                        name="ck_product_lines_holder"),
        CheckConstraint("quantity >= 1", name="ck_product_lines_quantity"),
        _in_check("audience", LINE_AUDIENCES, "ck_product_lines_audience"),
        CheckConstraint("(audience = 'all') = (audience_at IS NULL)", name="ck_product_lines_audience_at"),
        CheckConstraint("audience = 'all' OR owner = 'plan'", name="ck_product_lines_audience_owner"),
        Index("ix_product_lines_customer_org_id", "customer_org_id"),
        Index("ix_product_lines_plan_id", "plan_id"),
    )

    owner: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    customer_org_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT", name="fk_product_lines_product_id"), nullable=False,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    end_mode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ends_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    ended_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: Provenance only, no foreign key (a user may be purged; the line's record stays).
    ended_by_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    origin: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    legacy_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The kind's unpriced extras for what the line gives, shaped by its `config_schema` (Live Cast: backfill N).
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    #: `LINE_AUDIENCES` (revision 202611140900): a plan line only; `audience_at` set unless `all`.
    audience: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'all'"))
    audience_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class OrgProduct(EntityMixin, OrgScoped, Base):
    """A holding: one org's `quantity` of a product, how it is paid (`paid_mode`), and where its money stands.
    The Stripe columns carry `node_rentals`' meaning (`app/models/hosting.py::NodeHosting`), so the add-ons lane
    (`app/products/lanes/addons.py`) bills a holding exactly as it bills a hosting. `line_id` is the plan or comp
    line a free holding comes from; `fulfillment_ref` the kind's own row; `legacy_ref` the legacy row a moved
    holding was copied from (a moved holding keeps that row's id)."""

    __tablename__ = "org_products"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("paid_mode", PAID_MODES, "ck_org_products_paid_mode"),
        _in_check("status", HOLDING_STATUSES, "ck_org_products_status"),
        _in_check("interval", PRICE_INTERVALS, "ck_org_products_interval"),
        CheckConstraint("quantity >= 0", name="ck_org_products_quantity"),
        Index("ix_org_products_org_id_created_at", "org_id", "created_at"),
        Index("ix_org_products_stripe_subscription_id", "stripe_subscription_id",
              postgresql_where=text("stripe_subscription_id IS NOT NULL")),
        Index("ix_org_products_line_id", "line_id", postgresql_where=text("line_id IS NOT NULL")),
    )

    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT", name="fk_org_products_product_id"), nullable=False,
    )
    price_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("product_prices.id", ondelete="RESTRICT", name="fk_org_products_price_id"), nullable=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    interval: Mapped[str] = mapped_column(Text, nullable=False)
    paid_mode: Mapped[str] = mapped_column(Text, nullable=False)
    line_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("product_lines.id", ondelete="SET NULL", name="fk_org_products_line_id"), nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    status_since: Mapped[datetime] = timestamp_column()
    stop_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancel_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: A comp's end (`end_mode = date`), read by the inclusion pass.
    comp_ends_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_price_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"),
    )
    owns_subscription: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    first_invoice_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_invoice_due_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    overlap_credit_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: The month (or the one-off) as charged, in the price's minor unit.
    unit_amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'usd'"))
    fulfillment_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    legacy_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_account_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    livemode: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)


class ProductTrial(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """An org used its one trial of a product (until `trial_end`): never offered again."""

    __tablename__ = "product_trials"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "product_id", name="uq_product_trials_org_product"),
    )

    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE", name="fk_product_trials_product_id"), nullable=False,
    )
    trial_end: Mapped[datetime] = timestamp_column(server_now=False)


class OrgProductSettings(UuidPkMixin, StampedMixin, OrgScoped, Base):
    """One org's settings of one product, shaped by its kind's `settings_schema` (Jaah AI credit: automatic
    top-up). Needs no holding, so a one-off product can carry them."""

    __tablename__ = "org_product_settings"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "product_id", name="uq_org_product_settings_org_product"),
    )

    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE", name="fk_org_product_settings_product_id"), nullable=False,
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
