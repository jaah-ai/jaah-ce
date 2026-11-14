"""Copy the Jaah plan catalogue and plan subscriptions into the product engine

Additive and SQL-only (architecture §4.1): no Stripe call, no notice, no legacy row changed or deleted.

* Every `billing_plans` row but `(default)` (`_default`, which no org buys) as a `products` row of kind `plan` with
  THE SAME id: `config.plan_key` its key, Self-serve its `self_serve`, retired when not `active`, price policy keep,
  its trial days and Stripe Product.
* Its monthly and yearly prices as `product_prices` rows carrying the row's own Stripe Price ids (a Free plan: a $0
  monthly row with no Price). Ids are seeded by the Stripe Price (`app/products/kinds_impl/plan.py::price_row_id`).
* Every plan subscription (`billing_subscriptions`, kind `plan`) whose plan the copy knows as an `org_products`
  holding with THE SAME id and Stripe subscription and Price; its start is the org's plan start while the org is on
  that plan.
* Each org's used trial (`trial_plan_key`/`trial_end`) as a `product_trials` row.

Idempotent: every insert is `ON CONFLICT DO NOTHING`. The downgrade removes the plan products copied from
`billing_plans` (every plan product carries that `legacy_ref`: a plan made in the engine has its `billing_plans` row
too) with their prices, holdings and trials.

Revision ID: 202611140200
Revises: 202611140100
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140200"
down_revision: Union[str, Sequence[str], None] = "202611140100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies (a migration never imports the live code): `(default)`'s key and the price row id seeds.
_DEFAULTS = "'_default'"
_PRICE_ID = "md5('jaah:plan:price:' || {price})::uuid"
_FREE_ID = "md5('jaah:plan:price:free:' || b.id::text || chr(58) || 'month')::uuid"
_PLAN_PRODUCT = "(SELECT p.id FROM products p JOIN billing_plans b ON b.id = p.id WHERE p.kind = 'plan' " \
                "AND b.key = {key})"

_UPGRADE = [
    f"""
    INSERT INTO products (id, name, description, kind, self_serve, retired_at, price_policy, lane, quantity_mode,
                          min_quantity, amount_mode, trial_days, stripe_product_id, stripe_account_id, livemode,
                          legacy_ref, config, created_at, updated_at)
    SELECT b.id, b.name, COALESCE(b.blurb, ''), 'plan', b.self_serve, CASE WHEN b.active THEN NULL ELSE now() END,
           'keep', 'plan', 'reported', 1, 'fixed', b.trial_days, b.stripe_product_id, b.stripe_account_id,
           b.livemode, 'billing_plans:' || b.id::text, jsonb_build_object('plan_key', b.key), now(), now()
    FROM billing_plans b WHERE b.key <> {_DEFAULTS}
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO product_prices (id, name, product_id, interval, currency, unit_amount, version, active,
                                stripe_price_id, stripe_account_id, livemode, legacy_ref)
    SELECT {_PRICE_ID.format(price="b.stripe_price_id")}, b.name || ' (monthly)', b.id, 'month',
           COALESCE(b.currency, 'usd'), b.price_unit_amount, 0, true, b.stripe_price_id, b.stripe_account_id,
           b.livemode, 'billing_plans:' || b.id::text
    FROM billing_plans b
    WHERE b.key <> {_DEFAULTS} AND b.stripe_price_id IS NOT NULL AND b.price_unit_amount IS NOT NULL
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO product_prices (id, name, product_id, interval, currency, unit_amount, version, active,
                                legacy_ref)
    SELECT {_FREE_ID}, b.name || ' (monthly)', b.id, 'month', 'usd', 0, 0, true, 'billing_plans:' || b.id::text
    FROM billing_plans b
    WHERE b.key <> {_DEFAULTS} AND b.stripe_price_id IS NULL AND b.price_unit_amount = 0
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO product_prices (id, name, product_id, interval, currency, unit_amount, version, active,
                                stripe_price_id, stripe_account_id, livemode, legacy_ref)
    SELECT {_PRICE_ID.format(price="b.yearly_stripe_price_id")}, b.name || ' (yearly)', b.id, 'year',
           COALESCE(b.currency, 'usd'), b.yearly_price_unit_amount, 0, true, b.yearly_stripe_price_id,
           b.stripe_account_id, b.livemode, 'billing_plans:' || b.id::text
    FROM billing_plans b
    WHERE b.key <> {_DEFAULTS} AND b.yearly_stripe_price_id IS NOT NULL AND b.yearly_price_unit_amount IS NOT NULL
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO org_products (id, name, org_id, product_id, price_id, quantity, interval, paid_mode, status,
                              status_since, cancel_at, stripe_subscription_id, stripe_price_ids, owns_subscription,
                              unit_amount, currency, legacy_ref, stripe_account_id, livemode)
    SELECT s.id, p.name, s.org_id, p.id,
           (SELECT pp.id FROM product_prices pp WHERE pp.product_id = p.id AND pp.stripe_price_id = s.stripe_price_id
            LIMIT 1),
           COALESCE(s.quantity, 1), CASE WHEN s.interval IN ('month', 'year') THEN s.interval ELSE 'month' END,
           CASE WHEN s.collection_method = 'send_invoice' THEN 'invoice' ELSE 'card' END,
           CASE
               WHEN s.status IN ('canceled', 'incomplete_expired') THEN 'ended'
               WHEN s.status IN ('past_due', 'unpaid') THEN 'past_due'
               WHEN s.status = 'incomplete' THEN 'pending_payment'
               WHEN s.status = 'paused' THEN 'stopped'
               WHEN s.cancel_at_period_end THEN 'cancelling'
               ELSE 'active' END,
           COALESCE(CASE WHEN o.plan_key = s.plan_key THEN o.plan_started_at END, s.current_period_start, now()),
           CASE WHEN s.cancel_at_period_end THEN s.current_period_end END,
           s.stripe_subscription_id,
           CASE WHEN s.stripe_price_id IS NULL THEN ARRAY[]::text[] ELSE ARRAY[s.stripe_price_id] END,
           true, s.price_unit_amount, 'usd', 'billing_subscriptions:' || s.id::text, s.stripe_account_id, s.livemode
    FROM billing_subscriptions s
    JOIN products p ON p.id = {_PLAN_PRODUCT.format(key="s.plan_key")}
    LEFT JOIN orgs o ON o.id = s.org_id
    WHERE s.kind = 'plan' AND s.plan_key IS NOT NULL
    ON CONFLICT (id) DO NOTHING
    """,
    f"""
    INSERT INTO product_trials (org_id, product_id, trial_end)
    SELECT DISTINCT ON (s.org_id, p.id) s.org_id, p.id, s.trial_end
    FROM billing_subscriptions s
    JOIN products p ON p.id = {_PLAN_PRODUCT.format(key="s.trial_plan_key")}
    WHERE s.kind = 'plan' AND s.trial_plan_key IS NOT NULL AND s.trial_end IS NOT NULL
    ORDER BY s.org_id, p.id, s.trial_end DESC
    ON CONFLICT (org_id, product_id) DO NOTHING
    """,
]


def upgrade() -> None:
    for statement in _UPGRADE:
        op.execute(sa.text(statement))


def downgrade() -> None:
    copied = "(SELECT id FROM products WHERE kind = 'plan' AND legacy_ref LIKE 'billing_plans:%')"
    op.execute(sa.text(f"DELETE FROM product_trials WHERE product_id IN {copied}"))
    op.execute(sa.text("DELETE FROM org_products WHERE legacy_ref LIKE 'billing_subscriptions:%'"))
    op.execute(sa.text(f"DELETE FROM org_products WHERE product_id IN {copied}"))
    op.execute(sa.text(f"DELETE FROM product_prices WHERE product_id IN {copied}"))
    op.execute(sa.text("DELETE FROM products WHERE kind = 'plan' AND legacy_ref LIKE 'billing_plans:%'"))
