"""Product engine tables: `product_kinds` (seeded), `products`, `product_options`, `product_prices`, `product_lines`,
`org_products`, `product_trials`, `org_product_settings` (the product engine)

Additive only: eight new tables and nothing else. No existing row is read or rewritten, and no kind's legacy rows
are copied here (each kind's own child does that). `product_kinds` is seeded with the registry's six kinds as frozen
literals (`app/products/kinds.py::KINDS` is the living definition; a test holds the two equal). The downgrade drops
what this adds.

Revision ID: 202611131200
Revises: 202611130730
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202611131200"
down_revision: Union[str, None] = "202611130730"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYSTEM_ACTOR = sa.text("'01900000-0000-7000-8000-000000000001'::uuid")
_KINDS = (
    ("plan", "Plan"),
    ("shared_node", "Shared node"),
    ("dedicated_node", "Dedicated node"),
    ("jaah_ai_credit", "Jaah AI credit"),
    ("live_cast", "Live Cast"),
    ("custom", "Custom"),
)
_ENTITIES = ("products", "product_options", "product_prices", "product_lines", "org_products")


def _uuid(name: str, *args: object, nullable: bool = False, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable, **kw)  # type: ignore[arg-type]


def _at(name: str, *, nullable: bool = False) -> sa.Column:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _actor(name: str) -> sa.Column:
    return _uuid(name, sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), server_default=_SYSTEM_ACTOR)


def _stamps() -> list[sa.Column]:
    return [_at("created_at"), _at("updated_at"), _actor("created_by_id"), _actor("updated_by_id")]


def _entity() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        *_stamps(),
    ]


def _jsonb(name: str, default: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text(default))


def _text(name: str, *, nullable: bool = True, default: str | None = None) -> sa.Column:
    if default is None:
        return sa.Column(name, sa.Text(), nullable=nullable)
    return sa.Column(name, sa.Text(), nullable=nullable, server_default=sa.text(default))


def _int(name: str, default: str | None = None, *, nullable: bool = False, big: bool = False) -> sa.Column:
    kind = sa.BigInteger() if big else sa.Integer()
    if default is None:
        return sa.Column(name, kind, nullable=nullable)
    return sa.Column(name, kind, nullable=nullable, server_default=sa.text(default))


def _bool(name: str, default: str | None = None) -> sa.Column:
    if default is None:
        return sa.Column(name, sa.Boolean(), nullable=True)
    return sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text(default))


def _stripe_account() -> list[sa.Column]:
    return [_text("stripe_account_id"), _bool("livemode")]


def _kind_fk(table: str) -> sa.Column:
    return sa.Column(
        "kind", sa.Text(), sa.ForeignKey("product_kinds.key", ondelete="RESTRICT", name=f"fk_{table}_kind"),
        nullable=False,
    )


def _in(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")", name=name)


def _product(table: str, ondelete: str) -> sa.Column:
    return _uuid("product_id", sa.ForeignKey("products.id", ondelete=ondelete, name=f"fk_{table}_product_id"))


def upgrade() -> None:
    from app.entity_shape import set_updated_at_trigger_sql

    op.create_table(
        "product_kinds",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
    )
    op.bulk_insert(
        sa.table("product_kinds", sa.column("key", sa.Text()), sa.column("label", sa.Text())),
        [{"key": key, "label": label} for key, label in _KINDS],
    )

    op.create_table(
        "products",
        *_entity(),
        _kind_fk("products"),
        _uuid("customer_org_id", nullable=True),
        _bool("self_serve", "false"),
        _at("retired_at", nullable=True),
        _text("price_policy", nullable=False, default="'keep'"),
        _text("lane", nullable=False, default="'addons'"),
        _text("quantity_mode", nullable=False, default="'fixed'"),
        _int("min_quantity", "1"),
        _int("max_quantity", nullable=True),
        _text("amount_mode", nullable=False, default="'fixed'"),
        _int("min_amount", nullable=True, big=True),
        _int("max_amount", nullable=True, big=True),
        _int("trial_days", nullable=True),
        _text("stripe_product_id"),
        *_stripe_account(),
        _text("legacy_ref"),
        _jsonb("config"),
        _in("lane", ("plan", "addons"), "ck_products_lane"),
        _in("quantity_mode", ("fixed", "chosen", "reported"), "ck_products_quantity_mode"),
        _in("amount_mode", ("fixed", "chosen"), "ck_products_amount_mode"),
        _in("price_policy", ("keep", "move_at_renewal"), "ck_products_price_policy"),
        sa.CheckConstraint("min_quantity >= 0 AND (max_quantity IS NULL OR max_quantity >= min_quantity)",
                           name="ck_products_quantity_range"),
    )
    op.create_index("ix_products_kind", "products", ["kind"])
    op.create_index("ix_products_customer_org_id", "products", ["customer_org_id"],
                    postgresql_where=sa.text("customer_org_id IS NOT NULL"))

    op.create_table(
        "product_options",
        *_entity(),
        _product("product_options", "CASCADE"),
        _text("dimension", nullable=False),
        _text("dimension_label", nullable=False, default="''"),
        _text("key", nullable=False),
        _bool("required", "true"),
        _int("sort_order", "0"),
        _bool("active", "true"),
        _jsonb("attrs"),
        sa.UniqueConstraint("product_id", "dimension", "key", name="uq_product_options_product_dimension_key"),
    )

    op.create_table(
        "product_prices",
        *_entity(),
        _product("product_prices", "CASCADE"),
        _uuid("option_id", sa.ForeignKey("product_options.id", ondelete="CASCADE",
                                         name="fk_product_prices_option_id"), nullable=True),
        _text("interval", nullable=False),
        _text("currency", nullable=False, default="'usd'"),
        _int("unit_amount", big=True),
        _int("version", "0"),
        _bool("active", "true"),
        _text("stripe_price_id"),
        *_stripe_account(),
        _text("legacy_ref"),
        _in("interval", ("one_off", "month", "year"), "ck_product_prices_interval"),
        sa.CheckConstraint("unit_amount >= 0", name="ck_product_prices_unit_amount"),
    )
    op.create_index("ix_product_prices_product_id", "product_prices", ["product_id"])

    op.create_table(
        "product_lines",
        *_entity(),
        _text("owner", nullable=False),
        _uuid("plan_id", nullable=True),
        _uuid("customer_org_id", nullable=True),
        _product("product_lines", "RESTRICT"),
        _int("quantity", "1"),
        _jsonb("options"),
        _int("amount", nullable=True, big=True),
        _int("sort_order", "0"),
        _text("end_mode"),
        _at("ends_at", nullable=True),
        _at("ended_at", nullable=True),
        _uuid("ended_by_id", nullable=True),
        _text("origin"),
        _text("legacy_ref"),
        _in("owner", ("plan", "org_override", "comp"), "ck_product_lines_owner"),
        sa.CheckConstraint("end_mode IS NULL OR end_mode IN ('never', 'date', 'plan_end')",
                           name="ck_product_lines_end_mode"),
        sa.CheckConstraint("(owner = 'comp') = (end_mode IS NOT NULL)", name="ck_product_lines_comp_end"),
        sa.CheckConstraint("(end_mode = 'date') = (ends_at IS NOT NULL)", name="ck_product_lines_ends_at"),
        sa.CheckConstraint("(owner = 'plan') = (plan_id IS NOT NULL AND customer_org_id IS NULL)",
                           name="ck_product_lines_holder"),
        sa.CheckConstraint("quantity >= 1", name="ck_product_lines_quantity"),
    )
    op.create_index("ix_product_lines_customer_org_id", "product_lines", ["customer_org_id"])
    op.create_index("ix_product_lines_plan_id", "product_lines", ["plan_id"])

    op.create_table(
        "org_products",
        *_entity(),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        _product("org_products", "RESTRICT"),
        _uuid("price_id", sa.ForeignKey("product_prices.id", ondelete="RESTRICT", name="fk_org_products_price_id"),
              nullable=True),
        _int("quantity", "1"),
        _jsonb("options"),
        _int("amount", nullable=True, big=True),
        _text("interval", nullable=False),
        _text("paid_mode", nullable=False),
        _uuid("line_id", sa.ForeignKey("product_lines.id", ondelete="SET NULL", name="fk_org_products_line_id"),
              nullable=True),
        _text("status", nullable=False),
        _at("status_since"),
        _text("stop_cause"),
        _at("cancel_at", nullable=True),
        _at("comp_ends_at", nullable=True),
        _text("stripe_subscription_id"),
        sa.Column("stripe_price_ids", postgresql.ARRAY(sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        _bool("owns_subscription", "false"),
        _text("first_invoice_id"),
        _at("first_invoice_due_at", nullable=True),
        _int("attempt", "0"),
        _int("overlap_credit_cents", "0"),
        _int("unit_amount", nullable=True, big=True),
        _text("currency", nullable=False, default="'usd'"),
        _text("fulfillment_ref"),
        _text("legacy_ref"),
        *_stripe_account(),
        sa.UniqueConstraint("id", "org_id", name="uq_org_products_id_org_id"),
        _in("paid_mode", ("card", "invoice", "comped", "included"), "ck_org_products_paid_mode"),
        _in("status", ("pending_payment", "active", "past_due", "cancelling", "stopped", "ended", "failed"),
            "ck_org_products_status"),
        _in("interval", ("one_off", "month", "year"), "ck_org_products_interval"),
        sa.CheckConstraint("quantity >= 0", name="ck_org_products_quantity"),
    )
    op.create_index("ix_org_products_org_id_created_at", "org_products", ["org_id", "created_at"])
    op.create_index("ix_org_products_stripe_subscription_id", "org_products", ["stripe_subscription_id"],
                    postgresql_where=sa.text("stripe_subscription_id IS NOT NULL"))
    op.create_index("ix_org_products_line_id", "org_products", ["line_id"],
                    postgresql_where=sa.text("line_id IS NOT NULL"))

    op.create_table(
        "product_trials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        *_stamps(),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        _product("product_trials", "CASCADE"),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "org_id", name="uq_product_trials_id_org_id"),
        sa.UniqueConstraint("org_id", "product_id", name="uq_product_trials_org_product"),
    )

    op.create_table(
        "org_product_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        *_stamps(),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        _product("org_product_settings", "CASCADE"),
        _jsonb("settings"),
        sa.UniqueConstraint("id", "org_id", name="uq_org_product_settings_id_org_id"),
        sa.UniqueConstraint("org_id", "product_id", name="uq_org_product_settings_org_product"),
    )

    for table in _ENTITIES:
        op.execute(set_updated_at_trigger_sql(table))


def downgrade() -> None:
    for table in ("org_product_settings", "product_trials", "org_products", "product_lines", "product_prices",
                  "product_options", "products", "product_kinds"):
        op.drop_table(table)
