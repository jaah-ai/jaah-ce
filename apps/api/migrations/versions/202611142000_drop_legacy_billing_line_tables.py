"""Drop the legacy billing line and hosting price tables; `org_bundled_products.kind` references `product_kinds`

Contraction step of the product engine. The release before this one reads none of these tables; every plan line, override line and comp is a `product_lines` row with the same id, and the hosting catalogue
is the two hosting products' options and prices (revision 202611141800, same ids):

- `billing_plan_products` (a plan's bundled product lines), `org_plan_products` (an org's override lines),
  `org_comps` (comps), each with its `set_updated_at` trigger and its CHECKs spelling the product kinds;
- `rental_prices` (the legacy hosting price catalogue).

`org_bundled_products` stays (hosting's fulfillment row): its CHECK spelling the node kinds becomes a foreign key to
the `product_kinds` lookup, as every engine table's `kind` is (AC-16). Added NOT VALID, then validated: every row is
a node kind, which the lookup holds (both steps share the revision's transaction).

During the deploy overlap the outgoing release still copies each line write into the three line tables (it never
reads them): such a write fails whole and rolls back (no Stripe call happens in it) and succeeds once retried.

The downgrade restores each dropped table as an empty shell with its constraints and the CHECK on
`org_bundled_products`. Lossy by design: the rows are gone (their engine copies stay).

Revision ID: 202611142000
Revises: 202611141900
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# additive-check: exempt — contraction of tables the previous release never reads; it still copies line writes into the three line tables, so during the deploy overlap such a write fails whole (its transaction rolls back, no Stripe call) and is retried; plus a CHECK replaced by a foreign key to the kinds lookup.

revision: str = "202611142000"
down_revision: Union[str, Sequence[str], None] = "202611141900"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies (a migration never imports the live code).
_BUNDLED = "org_bundled_products"
_BUNDLED_CHECK = "ck_org_bundled_products_kind"
_BUNDLED_FK = "fk_org_bundled_products_kind"
_SYSTEM_ACTOR = "'01900000-0000-7000-8000-000000000001'::uuid"


def upgrade() -> None:
    op.drop_table("billing_plan_products")
    op.drop_table("org_plan_products")
    op.drop_table("org_comps")
    op.drop_table("rental_prices")
    op.execute(f"ALTER TABLE {_BUNDLED} DROP CONSTRAINT IF EXISTS {_BUNDLED_CHECK}")
    op.execute(
        f"ALTER TABLE {_BUNDLED} ADD CONSTRAINT {_BUNDLED_FK} FOREIGN KEY (kind) "
        "REFERENCES product_kinds (key) ON DELETE RESTRICT NOT VALID"
    )
    op.execute(f"ALTER TABLE {_BUNDLED} VALIDATE CONSTRAINT {_BUNDLED_FK}")


# ── Downgrade: empty shells, as the tables stood at revision 202611141900 ────────────────────────────────

_ENTITY = f"""
    id uuid NOT NULL DEFAULT uuidv7(),
    name text NOT NULL,
    description text NOT NULL DEFAULT ''::text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by_id uuid NOT NULL DEFAULT {_SYSTEM_ACTOR},
    updated_by_id uuid NOT NULL DEFAULT {_SYSTEM_ACTOR}"""

_LINE = """
    kind text NOT NULL,
    quantity integer NOT NULL DEFAULT 1,
    tier_key text,
    instance_type text,
    storage_gb integer,
    credit_cents bigint,
    sort_order integer NOT NULL DEFAULT 0,
    backfill_tasks integer"""

_LINE_KINDS = "('shared_node', 'dedicated_node', 'jaah_ai_credit', 'live_cast')"


def _line_checks(table: str) -> str:
    return f""",
    CONSTRAINT ck_{table}_kind CHECK (kind IN {_LINE_KINDS}),
    CONSTRAINT ck_{table}_quantity CHECK (quantity >= 1),
    CONSTRAINT ck_{table}_credit_cents CHECK (credit_cents IS NULL OR credit_cents >= 0),
    CONSTRAINT ck_{table}_shared_node CHECK (kind <> 'shared_node' OR tier_key IS NOT NULL),
    CONSTRAINT ck_{table}_dedicated_node
        CHECK (kind <> 'dedicated_node' OR (instance_type IS NOT NULL AND storage_gb IS NOT NULL)),
    CONSTRAINT ck_{table}_jaah_ai_credit CHECK (kind <> 'jaah_ai_credit' OR credit_cents IS NOT NULL),
    CONSTRAINT ck_{table}_live_cast CHECK (kind <> 'live_cast' OR coalesce(backfill_tasks, 0) >= 1),
    CONSTRAINT ck_{table}_backfill_tasks CHECK (backfill_tasks IS NULL OR kind = 'live_cast')"""


def _actors(created: str, updated: str) -> str:
    return f""",
    CONSTRAINT {created} FOREIGN KEY (created_by_id) REFERENCES admin_users (id) ON DELETE RESTRICT,
    CONSTRAINT {updated} FOREIGN KEY (updated_by_id) REFERENCES admin_users (id) ON DELETE RESTRICT"""


def _trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_set_updated_at "
        f"BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_BUNDLED} DROP CONSTRAINT IF EXISTS {_BUNDLED_FK}")
    op.execute(
        f"ALTER TABLE {_BUNDLED} ADD CONSTRAINT {_BUNDLED_CHECK} CHECK (kind IN ('shared_node', 'dedicated_node'))"
    )

    op.execute(f"""CREATE TABLE rental_prices ({_ENTITY},
    kind text NOT NULL,
    key text NOT NULL,
    monthly_cents bigint NOT NULL,
    active boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 0,
    vcpu integer,
    ram_gb double precision,
    disk_gb integer,
    stripe_product_id text,
    stripe_price_id text,
    unit_amount bigint,
    version integer NOT NULL DEFAULT 0,
    stripe_account_id text,
    livemode boolean,
    CONSTRAINT pk_rental_prices PRIMARY KEY (id),
    CONSTRAINT uq_rental_prices_kind_key UNIQUE (kind, key),
    CONSTRAINT ck_rental_prices_kind CHECK (kind IN ('instance', 'storage', 'shared', 'live_cast', 'windows')),
    CONSTRAINT ck_rental_prices_monthly_cents CHECK (monthly_cents >= 0){
        _actors("fk_rental_prices_created_by_id", "fk_rental_prices_updated_by_id")})""")
    _trigger("rental_prices")

    op.execute(f"""CREATE TABLE billing_plan_products ({_ENTITY},
    plan_id uuid NOT NULL,{_LINE},
    audience text NOT NULL DEFAULT 'all'::text,
    audience_at timestamptz,
    CONSTRAINT pk_billing_plan_products PRIMARY KEY (id),
    CONSTRAINT fk_billing_plan_products_plan_id FOREIGN KEY (plan_id) REFERENCES billing_plans (id) ON DELETE CASCADE,
    CONSTRAINT ck_billing_plan_products_audience CHECK (audience IN ('all', 'existing', 'new')),
    CONSTRAINT ck_billing_plan_products_audience_at CHECK ((audience = 'all') = (audience_at IS NULL)),
    CONSTRAINT ck_billing_plan_products_audience_kind
        CHECK (audience = 'all' OR kind IN ('shared_node', 'dedicated_node')){_line_checks("billing_plan_products")}{
        _actors("fk_billing_plan_products_created_by_id",
                "fk_billing_plan_products_updated_by_id")})""")
    op.execute("CREATE INDEX ix_billing_plan_products_plan_id ON billing_plan_products (plan_id)")
    _trigger("billing_plan_products")

    op.execute(f"""CREATE TABLE org_plan_products ({_ENTITY},
    org_id uuid NOT NULL,{_LINE},
    CONSTRAINT pk_org_plan_products PRIMARY KEY (id),
    CONSTRAINT uq_org_plan_products_id_org_id UNIQUE (id, org_id){_line_checks("org_plan_products")}{
        _actors("fk_org_plan_products_created_by_id", "fk_org_plan_products_updated_by_id")})""")
    op.execute("CREATE INDEX ix_org_plan_products_org_id ON org_plan_products (org_id)")
    _trigger("org_plan_products")

    op.execute(f"""CREATE TABLE org_comps ({_ENTITY},
    org_id uuid NOT NULL,{_LINE},
    end_mode text NOT NULL,
    ends_at timestamptz,
    ended_at timestamptz,
    ended_by_id uuid,
    origin text,
    CONSTRAINT org_comps_pkey PRIMARY KEY (id),
    CONSTRAINT uq_org_comps_id_org_id UNIQUE (id, org_id),
    CONSTRAINT ck_org_comps_end_mode CHECK (end_mode IN ('never', 'date', 'plan')),
    CONSTRAINT ck_org_comps_ends_at CHECK ((end_mode = 'date') = (ends_at IS NOT NULL)),
    CONSTRAINT ck_org_comps_origin CHECK (origin IS NULL OR origin IN ('easy_mode')){_line_checks("org_comps")}{
        _actors("org_comps_created_by_id_fkey", "org_comps_updated_by_id_fkey")})""")
    op.execute("CREATE INDEX ix_org_comps_org_id ON org_comps (org_id)")
    _trigger("org_comps")
