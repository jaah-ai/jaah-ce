"""Hosting onto the product engine: the hosting catalogue (`rental_prices`) and the billed hosting rows
(`node_rentals`, monthly Shared and Dedicated) copied into `products`, `product_options`, `product_prices` and
`org_products`

Additive and SQL only: every row is an `INSERT ... ON CONFLICT (id) DO NOTHING` under a fixed or derived id, so a
re-run inserts nothing new, and no legacy row is changed. The two products' ids, the option, price and holding ids,
and every column's source are the ones `app/products/kinds_impl/hosting.py` writes on catch-up (one contract, two
writers: this frozen copy for the deploy, that one for rows written after it). A price row exists only for a
`rental_prices` row pushed to Stripe. The downgrade deletes the two products with every row under them: their
holdings (copied here, or written since by the engine), prices and options.

Revision ID: 202611140100
Revises: 202611131600
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140100"
down_revision: Union[str, None] = "202611131600"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYSTEM_ACTOR = "'01900000-0000-7000-8000-000000000001'::uuid"
_SHARED = "'01a1b5c0-0000-7000-8000-000000000001'::uuid"
_DEDICATED = "'01a1b5c0-0000-7000-8000-000000000002'::uuid"

#: The product a `rental_prices.kind` belongs to (`live_cast` is not hosting).
_PRODUCT_OF_KIND = f"CASE rp.kind WHEN 'shared' THEN {_SHARED} ELSE {_DEDICATED} END"
_HOSTING_KINDS = "('shared', 'instance', 'storage', 'windows')"

#: An option's name, from its `rental_prices` row.
_OPTION_NAME = """CASE rp.kind
    WHEN 'shared' THEN COALESCE(NULLIF(btrim(rp.name), ''), 'Shared')
    WHEN 'instance' THEN CASE
        WHEN rp.vcpu IS NOT NULL AND rp.ram_gb IS NOT NULL
        THEN rp.key || ' · ' || rp.vcpu || ' vCPU · ' || trim_scale(rp.ram_gb::numeric)::text || ' GB RAM'
        ELSE COALESCE(NULLIF(btrim(rp.name), ''), rp.key) END
    WHEN 'storage' THEN rp.key || ' GB'
    ELSE 'Windows on ' || rp.key END"""

#: An option's sort: its dimension's rank (tier and machine size, then storage, then the Windows licence — the order
#: a hosting bills its Prices in, `hostings.Choice.priced_rows`) ahead of the rental row's own order.
_OPTION_SORT = "(CASE rp.kind WHEN 'storage' THEN 1000 WHEN 'windows' THEN 2000 ELSE 0 END + rp.sort_order)"

#: A price row's id: one per `rental_prices` row and version.
_PRICE_ID = "md5('hosting-price:' || rp.id::text || ':' || rp.version::text)::uuid"


def _product(product_id: str, kind: str, name: str, slug: str, rental_kind: str) -> str:
    source = (f"(SELECT {{col}} FROM rental_prices WHERE kind = '{rental_kind}' AND stripe_product_id IS NOT NULL "
              "ORDER BY sort_order, key LIMIT 1)")
    return f"""
INSERT INTO products (id, name, description, kind, customer_org_id, self_serve, price_policy, lane, quantity_mode,
                      min_quantity, amount_mode, stripe_product_id, stripe_account_id, livemode, legacy_ref,
                      created_by_id, updated_by_id)
VALUES ({product_id}, '{name}', '', '{kind}', NULL, true, 'move_at_renewal', 'addons', 'fixed', 1, 'fixed',
        {source.format(col='stripe_product_id')}, {source.format(col='stripe_account_id')},
        {source.format(col='livemode')}, 'rental_prices:{slug}', {_SYSTEM_ACTOR}, {_SYSTEM_ACTOR})
ON CONFLICT (id) DO NOTHING"""


_OPTIONS = f"""
INSERT INTO product_options (id, name, description, product_id, dimension, dimension_label, key, required,
                             sort_order, active, attrs, created_at, updated_at, created_by_id, updated_by_id)
SELECT rp.id, {_OPTION_NAME}, '', {_PRODUCT_OF_KIND},
       CASE rp.kind WHEN 'shared' THEN 'tier' WHEN 'instance' THEN 'instance_type' ELSE rp.kind END,
       CASE rp.kind WHEN 'shared' THEN 'Tier' WHEN 'instance' THEN 'Machine size' WHEN 'storage' THEN 'Storage'
                    ELSE 'Windows licence' END,
       rp.key, rp.kind <> 'windows', {_OPTION_SORT}, rp.active,
       CASE rp.kind
           WHEN 'shared' THEN jsonb_build_object('disk_gb', rp.disk_gb)
           WHEN 'instance' THEN jsonb_build_object('vcpu', rp.vcpu, 'ram_gb', rp.ram_gb)
           WHEN 'storage' THEN jsonb_build_object('gb', CASE WHEN rp.key ~ '^[0-9]+$' THEN rp.key::int END)
           ELSE jsonb_build_object('instance_type', rp.key) END,
       rp.created_at, rp.updated_at, rp.created_by_id, rp.updated_by_id
FROM rental_prices rp
WHERE rp.kind IN {_HOSTING_KINDS}
ON CONFLICT (id) DO NOTHING"""

_PRICES = f"""
INSERT INTO product_prices (id, name, description, product_id, option_id, interval, currency, unit_amount, version,
                            active, stripe_price_id, stripe_account_id, livemode, legacy_ref,
                            created_at, updated_at, created_by_id, updated_by_id)
SELECT {_PRICE_ID}, {_OPTION_NAME}, '', {_PRODUCT_OF_KIND}, rp.id, 'month', 'usd',
       COALESCE(rp.unit_amount, rp.monthly_cents), rp.version, rp.active, rp.stripe_price_id, rp.stripe_account_id,
       rp.livemode, 'rental_prices:' || rp.id::text,
       rp.created_at, rp.updated_at, rp.created_by_id, rp.updated_by_id
FROM rental_prices rp
WHERE rp.kind IN {_HOSTING_KINDS} AND rp.stripe_price_id IS NOT NULL
ON CONFLICT (id) DO NOTHING"""

_HOLDINGS = f"""
INSERT INTO org_products (id, name, description, org_id, product_id, price_id, quantity, options, interval,
                          paid_mode, status, status_since, stop_cause, cancel_at, stripe_subscription_id,
                          stripe_price_ids, owns_subscription, first_invoice_id, first_invoice_due_at, attempt,
                          overlap_credit_cents, unit_amount, currency, fulfillment_ref, legacy_ref,
                          created_at, updated_at, created_by_id, updated_by_id)
SELECT nr.id, nr.name, '', nr.org_id,
       CASE nr.kind WHEN 'shared' THEN {_SHARED} ELSE {_DEDICATED} END,
       CASE WHEN rp.stripe_price_id IS NOT NULL THEN {_PRICE_ID} END,
       1,
       CASE nr.kind
           WHEN 'shared' THEN jsonb_strip_nulls(jsonb_build_object('tier', nr.shared_price_id::text))
           ELSE jsonb_strip_nulls(jsonb_build_object('instance_type', nr.instance_price_id::text,
                                                     'storage', nr.storage_price_id::text,
                                                     'windows', nr.windows_price_id::text)) END,
       'month',
       CASE WHEN EXISTS (SELECT 1 FROM node_orders o
                         WHERE o.rental_id = nr.id AND o.params -> 'operator_invoice' IS NOT NULL)
            THEN 'invoice' ELSE 'card' END,
       CASE nr.status WHEN 'terminated' THEN 'ended' ELSE nr.status END,
       nr.status_since, nr.stop_cause, nr.cancel_at, nr.stripe_subscription_id,
       ARRAY(SELECT jsonb_array_elements_text(nr.stripe_price_ids)),
       nr.owns_subscription, nr.first_invoice_id, nr.first_invoice_due_at, nr.attempt, nr.overlap_credit_cents,
       nr.monthly_cents, 'usd', nr.id::text, 'node_rentals:' || nr.id::text,
       nr.created_at, nr.updated_at, nr.created_by_id, nr.updated_by_id
FROM node_rentals nr
LEFT JOIN rental_prices rp
       ON rp.id = CASE nr.kind WHEN 'shared' THEN nr.shared_price_id ELSE nr.instance_price_id END
WHERE nr.kind IN ('shared', 'dedicated') AND nr.billing = 'monthly'
ON CONFLICT (id) DO NOTHING"""


def upgrade() -> None:
    op.execute(sa.text(_product(_SHARED, "shared_node", "Shared node", "shared", "shared")))
    op.execute(sa.text(_product(_DEDICATED, "dedicated_node", "Dedicated node", "dedicated", "instance")))
    for statement in (_OPTIONS, _PRICES, _HOLDINGS):
        op.execute(sa.text(statement))


def downgrade() -> None:
    products = f"({_SHARED}, {_DEDICATED})"
    op.execute(sa.text(
        f"DELETE FROM org_products WHERE product_id IN {products}"))
    for table in ("product_prices", "product_options"):
        op.execute(sa.text(f"DELETE FROM {table} WHERE product_id IN {products}"))
    op.execute(sa.text(f"DELETE FROM products WHERE id IN {products}"))
