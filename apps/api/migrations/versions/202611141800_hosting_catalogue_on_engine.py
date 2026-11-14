"""The hosting catalogue's source moves to the product engine: every hosting option carries its catalogue facts, and
the four `node_rentals` product columns point at `product_options`

From this release the two hosting products' `product_options` (their `attrs["row"]`) and `product_prices` are the
hosting catalogue; `rental_prices` is no longer read or written (dropped by a later release). Three steps, all
additive for the release before this one, which still writes `rental_prices` and projects every write onto the options
in the same flush:

1. Any `rental_prices` hosting row with no option yet, and any pushed version with no price row, is inserted (the
   same statements as 202611140100, frozen here), so every row a rental can name has an option of the same id.
2. Every option with a `rental_prices` row gets that row's facts in `attrs["row"]` (the shape
   `app/products/kinds_impl/hosting_rows.py::snapshot` writes, its key included), whether or not the projection
   stamped it: the row is the truth at this moment, and the previous release projects the same values.
3. `node_rentals.instance_price_id` / `storage_price_id` / `shared_price_id` / `windows_price_id` reference
   `product_options(id)` instead of `rental_prices(id)` (same ids, same names, still `ON DELETE RESTRICT`). The
   previous release reads nothing from the keys themselves.

The downgrade points the four keys back at `rental_prices`; the stamped facts stay (the previous release rewrites
them on its next projection). It holds only until a hosting option this release made (which has no `rental_prices`
row) is named by a rental: the key back to `rental_prices` then fails to validate and the downgrade stops, unchanged.

Revision ID: 202611141800
Revises: 202611141700
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611141800"
down_revision: Union[str, Sequence[str], None] = "202611141700"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SHARED = "'01a1b5c0-0000-7000-8000-000000000001'::uuid"
_DEDICATED = "'01a1b5c0-0000-7000-8000-000000000002'::uuid"
_PRODUCT_OF_KIND = f"CASE rp.kind WHEN 'shared' THEN {_SHARED} ELSE {_DEDICATED} END"
_HOSTING_KINDS = "('shared', 'instance', 'storage', 'windows')"

_OPTION_NAME = """CASE rp.kind
    WHEN 'shared' THEN COALESCE(NULLIF(btrim(rp.name), ''), 'Shared')
    WHEN 'instance' THEN CASE
        WHEN rp.vcpu IS NOT NULL AND rp.ram_gb IS NOT NULL
        THEN rp.key || ' · ' || rp.vcpu || ' vCPU · ' || trim_scale(rp.ram_gb::numeric)::text || ' GB RAM'
        ELSE COALESCE(NULLIF(btrim(rp.name), ''), rp.key) END
    WHEN 'storage' THEN rp.key || ' GB'
    ELSE 'Windows on ' || rp.key END"""
_OPTION_SORT = "(CASE rp.kind WHEN 'storage' THEN 1000 WHEN 'windows' THEN 2000 ELSE 0 END + rp.sort_order)"
_PRICE_ID = "md5('hosting-price:' || rp.id::text || ':' || rp.version::text)::uuid"

#: The facts an option's `attrs["row"]` carries (`hosting_rows.snapshot`).
_SNAPSHOT = """jsonb_build_object(
    'kind', rp.kind, 'key', rp.key, 'name', rp.name, 'monthly_cents', rp.monthly_cents, 'sort_order', rp.sort_order,
    'version', rp.version, 'vcpu', rp.vcpu, 'ram_gb', rp.ram_gb, 'disk_gb', rp.disk_gb,
    'stripe_product_id', rp.stripe_product_id, 'unit_amount', rp.unit_amount, 'active', rp.active)"""

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
  AND NOT EXISTS (SELECT 1 FROM product_options o
                  WHERE o.product_id = {_PRODUCT_OF_KIND} AND o.key = rp.key
                    AND o.dimension = CASE rp.kind WHEN 'shared' THEN 'tier' WHEN 'instance' THEN 'instance_type'
                                                   ELSE rp.kind END
                    AND o.id <> rp.id)
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
  AND EXISTS (SELECT 1 FROM product_options o WHERE o.id = rp.id)
ON CONFLICT (id) DO NOTHING"""

_STAMP = f"""
UPDATE product_options o
SET attrs = COALESCE(o.attrs, '{{}}'::jsonb) || jsonb_build_object('row', {_SNAPSHOT})
FROM rental_prices rp
WHERE rp.id = o.id AND rp.kind IN {_HOSTING_KINDS} AND o.product_id IN ({_SHARED}, {_DEDICATED})"""

_TABLE = "node_rentals"
_COLUMNS = ("instance_price_id", "storage_price_id", "shared_price_id", "windows_price_id")


def _repoint(target: str) -> None:
    for column in _COLUMNS:
        name = f"fk_{_TABLE}_{column}"
        op.drop_constraint(name, _TABLE, type_="foreignkey")
        op.create_foreign_key(name, _TABLE, target, [column], ["id"], ondelete="RESTRICT")


def upgrade() -> None:
    for statement in (_OPTIONS, _PRICES, _STAMP):
        op.execute(sa.text(statement))
    _repoint("product_options")


def downgrade() -> None:
    _repoint("rental_prices")
