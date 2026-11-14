"""Copy Live Cast into the product engine

Additive and SQL-only (architecture §4.1): no Stripe call, no notice, no legacy row changed or deleted.

* `product_lines.config` — a line's unpriced extras (Live Cast: its backfill N, which the legacy line rows carry as
  `backfill_tasks`).
* The Live Cast catalogue product (one fixed id) and its monthly price, copied from the `rental_prices`
  `live_cast:project` row: the price row keeps that row's id and its Stripe Product and Price ids.
* Every Live Cast line — a plan's (`billing_plan_products`), an org's override (`org_plan_products`) and a comp
  (`org_comps`) — as a `product_lines` row with the same id.
* For every org with its own Live Cast override lines, the one holding that bills the projects beyond its plan's on
  its plan subscription (`app/products/kinds_impl/live_cast.py::billed_id`, the same id). Its quantity is read live
  (the kind's reported quantity), so the copy carries 0.

Free holdings of the copied lines are made by the inclusion pass (`app/products/inclusion.py`), which calls no
Stripe. Idempotent: every insert is `ON CONFLICT (id) DO NOTHING`. The downgrade removes exactly what this added.

Revision ID: 202611131500
Revises: 202611131400
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611131500"
down_revision: Union[str, Sequence[str], None] = "202611131400"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies (a migration never imports the live code): the product's fixed id seed and the billed holding's.
_PRODUCT_ID = "md5('jaah:product:live_cast')::uuid"
_BILLED_ID = "md5('jaah:live_cast:billed:' || {org}::text)::uuid"
_RENTAL = "(SELECT r.* FROM rental_prices r WHERE r.kind = 'live_cast' AND r.key = 'project')"
_DESCRIPTION = "Publish chosen projects' tasks on a public page."

_UPGRADE = [
    "ALTER TABLE product_lines ADD COLUMN IF NOT EXISTS config jsonb NOT NULL DEFAULT '{}'::jsonb",
    # The catalogue product: from the price row when there is one (its Stripe Product), else bare.
    f"""
    INSERT INTO products (id, name, description, kind, self_serve, retired_at, price_policy, lane, quantity_mode,
                          min_quantity, amount_mode, stripe_product_id, stripe_account_id, livemode, legacy_ref)
    SELECT {_PRODUCT_ID}, 'Live Cast', '{_DESCRIPTION.replace("'", "''")}', 'live_cast', false,
           CASE WHEN r.id IS NULL OR r.active THEN NULL ELSE now() END, 'move_at_renewal', 'plan', 'reported', 1,
           'fixed', r.stripe_product_id, r.stripe_account_id, r.livemode,
           CASE WHEN r.id IS NULL THEN NULL ELSE 'rental_prices:' || r.id END
    FROM (SELECT 1) one LEFT JOIN {_RENTAL} r ON true
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO product_prices (id, name, product_id, interval, currency, unit_amount, version, active,
                                stripe_price_id, stripe_account_id, livemode, legacy_ref, created_at, updated_at)
    SELECT r.id, 'Live Cast', {_PRODUCT_ID}, 'month', 'usd', COALESCE(r.unit_amount, r.monthly_cents), r.version,
           true, r.stripe_price_id, r.stripe_account_id, r.livemode, 'rental_prices:' || r.id, r.created_at,
           r.updated_at
    FROM {_RENTAL} r
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO product_lines (id, name, description, created_at, updated_at, created_by_id, updated_by_id, owner,
                               plan_id, customer_org_id, product_id, quantity, sort_order, config, legacy_ref)
    SELECT b.id, b.name, b.description, b.created_at, b.updated_at, b.created_by_id, b.updated_by_id, 'plan',
           b.plan_id, NULL, {_PRODUCT_ID}, b.quantity, b.sort_order,
           jsonb_build_object('backfill_tasks', b.backfill_tasks), 'billing_plan_products:' || b.id
    FROM billing_plan_products b WHERE b.kind = 'live_cast'
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO product_lines (id, name, description, created_at, updated_at, created_by_id, updated_by_id, owner,
                               plan_id, customer_org_id, product_id, quantity, sort_order, config, legacy_ref)
    SELECT o.id, o.name, o.description, o.created_at, o.updated_at, o.created_by_id, o.updated_by_id,
           'org_override', NULL, o.org_id, {_PRODUCT_ID}, o.quantity, o.sort_order,
           jsonb_build_object('backfill_tasks', o.backfill_tasks), 'org_plan_products:' || o.id
    FROM org_plan_products o WHERE o.kind = 'live_cast'
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO product_lines (id, name, description, created_at, updated_at, created_by_id, updated_by_id, owner,
                               plan_id, customer_org_id, product_id, quantity, sort_order, end_mode, ends_at,
                               ended_at, ended_by_id, origin, config, legacy_ref)
    SELECT c.id, c.name, c.description, c.created_at, c.updated_at, c.created_by_id, c.updated_by_id, 'comp', NULL,
           c.org_id, {_PRODUCT_ID}, c.quantity, c.sort_order,
           CASE WHEN c.end_mode = 'plan' THEN 'plan_end' ELSE c.end_mode END,
           CASE WHEN c.end_mode = 'date' THEN c.ends_at END, c.ended_at, c.ended_by_id, c.origin,
           jsonb_build_object('backfill_tasks', c.backfill_tasks), 'org_comps:' || c.id
    FROM org_comps c WHERE c.kind = 'live_cast'
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO org_products (id, name, org_id, product_id, price_id, quantity, interval, paid_mode, status,
                              stripe_price_ids, unit_amount, currency, legacy_ref, stripe_account_id, livemode)
    SELECT {_BILLED_ID.format(org="o.org_id")}, 'Live Cast', o.org_id, {_PRODUCT_ID}, r.id, 0, 'month', 'invoice',
           'active', ARRAY[r.stripe_price_id], COALESCE(r.unit_amount, r.monthly_cents), 'usd', 'live_cast:override',
           r.stripe_account_id, r.livemode
    FROM (SELECT DISTINCT org_id FROM org_plan_products WHERE kind = 'live_cast') o
    JOIN {_RENTAL} r ON r.stripe_price_id IS NOT NULL
    ON CONFLICT (id) DO NOTHING
    """,
]


def upgrade() -> None:
    for statement in _UPGRADE:
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(sa.text(f"DELETE FROM org_products WHERE product_id = {_PRODUCT_ID}"))
    op.execute(sa.text(f"DELETE FROM product_lines WHERE product_id = {_PRODUCT_ID}"))
    op.execute(sa.text(f"DELETE FROM product_prices WHERE product_id = {_PRODUCT_ID}"))
    op.execute(sa.text(f"DELETE FROM products WHERE id = {_PRODUCT_ID}"))
    op.execute(sa.text("ALTER TABLE product_lines DROP COLUMN IF EXISTS config"))
