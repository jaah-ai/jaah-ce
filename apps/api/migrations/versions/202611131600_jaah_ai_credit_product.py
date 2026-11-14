"""Jaah AI credit onto the product engine: its catalogue product, lines, holdings and automatic top-up settings

A SQL-only copy, additive and idempotent: every insert is `ON CONFLICT (id) DO
NOTHING` (settings: on the org and product), no legacy row is updated or deleted, and nothing here reaches Stripe.

* `products` + `product_prices`: one catalogue product, "Jaah AI credit" — one-off, the buyer chooses the amount
  ($10.00 to $10,000.00, the legacy top-up's bounds), self-serve — with its one base price (a chosen amount's line
  is priced by the buyer, so the base is $0).
* `product_lines` from the legacy credit lines, same UUIDs, `legacy_ref` set, `amount` = the line's `credit_cents`:
  each plan's (`billing_plan_products`), each org's override (`org_plan_products`) and every comp (`org_comps`, its
  `created_at` kept: a comp's monthly grant key counts months from it).
* `org_products` from every PAID top-up (`token_topups`): a card holding of its amount whose mirror row it names
  (`fulfillment_ref`), its id derived from the row's (`md5('token_topups:<id>')`), so a re-run finds it.
* `org_product_settings` from each org's stored auto-recharge choice (`org_settings` key `jaah_ai.auto_recharge`,
  micros) as the product's settings in US dollars.

The downgrade removes only what this copied (by `legacy_ref`, and the settings, whose legacy key stays the record);
the product and its price go only while nothing bought on it since still names them.

Revision ID: 202611131600
Revises: 202611131500
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202611131600"
down_revision: Union[str, Sequence[str], None] = "202611131500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: a revision never imports the live code (`app/products/kinds_impl/jaah_ai_credit.py::PRODUCT_ID`).
_PRODUCT = "01970000-0000-7000-8000-0000000000c4"
_PRICE = "01970000-0000-7000-8000-0000000001c4"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO products (id, name, description, kind, self_serve, price_policy, lane, quantity_mode,
                              min_quantity, amount_mode, min_amount, max_amount, legacy_ref)
        VALUES ('{_PRODUCT}', 'Jaah AI credit', 'Prepaid credit for Jaah AI.', 'jaah_ai_credit', true, 'keep',
                'addons', 'fixed', 1, 'chosen', 1000, 1000000, 'token_topups')
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO product_prices (id, name, product_id, interval, currency, unit_amount, version, active)
        VALUES ('{_PRICE}', 'Jaah AI credit', '{_PRODUCT}', 'one_off', 'usd', 0, 0, true)
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO product_lines (id, name, description, created_at, updated_at, created_by_id, updated_by_id, owner,
                                   plan_id, product_id, quantity, options, amount, sort_order, legacy_ref)
        SELECT l.id, l.name, l.description, l.created_at, l.updated_at, l.created_by_id, l.updated_by_id, 'plan',
               l.plan_id, '{_PRODUCT}', l.quantity, '{{}}'::jsonb, l.credit_cents, l.sort_order,
               'billing_plan_products:' || l.id
          FROM billing_plan_products l
         WHERE l.kind = 'jaah_ai_credit'
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO product_lines (id, name, description, created_at, updated_at, created_by_id, updated_by_id, owner,
                                   customer_org_id, product_id, quantity, options, amount, sort_order, legacy_ref)
        SELECT l.id, l.name, l.description, l.created_at, l.updated_at, l.created_by_id, l.updated_by_id,
               'org_override', l.org_id, '{_PRODUCT}', l.quantity, '{{}}'::jsonb, l.credit_cents, l.sort_order,
               'org_plan_products:' || l.id
          FROM org_plan_products l
         WHERE l.kind = 'jaah_ai_credit'
        ON CONFLICT (id) DO NOTHING
    """)
    # A legacy comp's end `plan` is the engine's `plan_end` (the credit kind's compat mirror maps the two).
    op.execute(f"""
        INSERT INTO product_lines (id, name, description, created_at, updated_at, created_by_id, updated_by_id, owner,
                                   customer_org_id, product_id, quantity, options, amount, sort_order, end_mode,
                                   ends_at, ended_at, ended_by_id, origin, legacy_ref)
        SELECT c.id, c.name, c.description, c.created_at, c.updated_at, c.created_by_id, c.updated_by_id, 'comp',
               c.org_id, '{_PRODUCT}', c.quantity, '{{}}'::jsonb, c.credit_cents, c.sort_order,
               CASE c.end_mode WHEN 'plan' THEN 'plan_end' ELSE c.end_mode END,
               CASE WHEN c.end_mode = 'date' THEN c.ends_at END, c.ended_at, c.ended_by_id, c.origin,
               'org_comps:' || c.id
          FROM org_comps c
         WHERE c.kind = 'jaah_ai_credit'
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO org_products (id, name, org_id, product_id, quantity, options, amount, interval, paid_mode,
                                  status, status_since, first_invoice_id, unit_amount, currency, fulfillment_ref,
                                  legacy_ref, created_at, created_by_id, updated_by_id)
        SELECT md5('token_topups:' || t.id)::uuid, 'Jaah AI credit', t.org_id, '{_PRODUCT}', 1, '{{}}'::jsonb,
               t.amount_micros / 10000, 'one_off', 'card', 'active', t.updated_at, t.stripe_invoice_id,
               t.amount_micros / 10000, 'usd', 'token_topups:' || t.id, 'token_topups:' || t.id, t.created_at,
               coalesce(t.created_by_id, '01900000-0000-7000-8000-000000000001'::uuid),
               coalesce(t.created_by_id, '01900000-0000-7000-8000-000000000001'::uuid)
          FROM token_topups t
         WHERE t.status = 'paid'
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO org_product_settings (org_id, product_id, settings)
        SELECT s.org_id, '{_PRODUCT}',
               jsonb_strip_nulls(jsonb_build_object(
                   'enabled', coalesce((s.value ->> 'enabled')::boolean, false),
                   'threshold', (s.value ->> 'threshold_micros')::numeric / 1000000,
                   'amount', (s.value ->> 'amount_micros')::numeric / 1000000))
          FROM org_settings s
         WHERE s.key = 'jaah_ai.auto_recharge' AND jsonb_typeof(s.value) = 'object'
        ON CONFLICT ON CONSTRAINT uq_org_product_settings_org_product DO NOTHING
    """)


def downgrade() -> None:
    op.execute(f"DELETE FROM org_product_settings WHERE product_id = '{_PRODUCT}'")
    op.execute(f"DELETE FROM org_products WHERE product_id = '{_PRODUCT}' AND legacy_ref LIKE 'token_topups:%'")
    op.execute(f"DELETE FROM product_lines WHERE product_id = '{_PRODUCT}' AND legacy_ref IS NOT NULL")
    unused = (f"NOT EXISTS (SELECT 1 FROM org_products WHERE product_id = '{_PRODUCT}') "
              f"AND NOT EXISTS (SELECT 1 FROM product_lines WHERE product_id = '{_PRODUCT}')")
    op.execute(f"DELETE FROM product_prices WHERE product_id = '{_PRODUCT}' AND {unused}")
    op.execute(f"DELETE FROM products WHERE id = '{_PRODUCT}' AND {unused}")
