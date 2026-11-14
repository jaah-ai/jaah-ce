"""`product_lines.audience` / `audience_at`: a plan line limited to the plan's existing or new customers.

The plan and override line lists move onto `product_lines` (`app/plan_terms.py::plan_lines` reads them), so the one
fact only `billing_plan_products` carried — a node line's audience — moves with them. Additive: `audience` defaults
to `all`; each engine plan line copied from a legacy row (same id) takes that row's audience. No legacy row is
written; the compat mirror (`app/products/kinds_impl/legacy_lines.py`) keeps the two equal from here on. Every hosting
plan and override line not yet in the engine is copied in (same id, as the mirror's catch-up does), since
`plan_terms` now reads them only there.

Revision ID: 202611140900
Revises: 202611140800
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140900"
down_revision: Union[str, Sequence[str], None] = "202611140800"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The two hosting products (`app/products/kinds_impl/hosting.py`, revision 202611140100).
_SHARED, _DEDICATED = "01a1b5c0-0000-7000-8000-000000000001", "01a1b5c0-0000-7000-8000-000000000002"


def _option(kind: str, key: str) -> str:
    """The option id (= its catalogue row's id) a legacy column names: one for sale first, as the catch-up picks it."""
    return (f"(SELECT r.id::text FROM rental_prices r WHERE r.kind = '{kind}' AND r.key = {key} "
            "ORDER BY r.active DESC, r.updated_at DESC LIMIT 1)")


def _copy_hosting_lines(table: str, owner: str, holder: str) -> str:
    """Every hosting line of a legacy line table not yet in the engine, copied as the line mirror's catch-up copies
    it (`legacy_lines.LineMirror.from_legacy`): same id, its options, its audience (a plan's)."""
    audience = "t.audience, t.audience_at" if table == "billing_plan_products" else "'all', NULL"
    return f"""
        INSERT INTO product_lines (id, name, owner, plan_id, customer_org_id, product_id, quantity, options,
                                   sort_order, created_by_id, updated_by_id, legacy_ref, audience, audience_at)
        SELECT t.id, t.name, '{owner}', {holder},
               CASE t.kind WHEN 'shared_node' THEN '{_SHARED}'::uuid ELSE '{_DEDICATED}'::uuid END,
               t.quantity,
               CASE t.kind WHEN 'shared_node' THEN jsonb_strip_nulls(jsonb_build_object('tier', {_option('shared', 't.tier_key')}))
                    ELSE jsonb_strip_nulls(jsonb_build_object(
                        'instance_type', {_option('instance', 't.instance_type')},
                        'storage', {_option('storage', 't.storage_gb::text')})) END,
               t.sort_order, t.created_by_id, t.updated_by_id, '{table}:' || t.id, {audience}
        FROM {table} t
        WHERE t.kind IN ('shared_node', 'dedicated_node')
          AND EXISTS (SELECT 1 FROM products p WHERE p.id = CASE t.kind WHEN 'shared_node' THEN '{_SHARED}'::uuid
                                                                 ELSE '{_DEDICATED}'::uuid END)
        ON CONFLICT (id) DO NOTHING
    """


def upgrade() -> None:
    op.add_column("product_lines", sa.Column("audience", sa.Text(), nullable=False, server_default=sa.text("'all'")))
    op.add_column("product_lines", sa.Column("audience_at", sa.DateTime(timezone=True), nullable=True))
    # `plan_terms` reads plan and override lines from the engine from this release on: the hosting ones the release
    # before only caught up on a pass are there from the start (the other kinds' revisions copied theirs).
    op.execute(_copy_hosting_lines("billing_plan_products", "plan", "t.plan_id, NULL"))
    op.execute(_copy_hosting_lines("org_plan_products", "org_override", "NULL, t.org_id"))
    op.execute(
        "UPDATE product_lines AS pl SET audience = b.audience, audience_at = b.audience_at "
        "FROM billing_plan_products AS b WHERE b.id = pl.id AND pl.owner = 'plan' AND b.audience <> 'all' "
        "AND b.audience_at IS NOT NULL"
    )
    op.create_check_constraint(
        "ck_product_lines_audience", "product_lines", "audience IN ('all', 'existing', 'new')")
    op.create_check_constraint(
        "ck_product_lines_audience_at", "product_lines", "(audience = 'all') = (audience_at IS NULL)")
    op.create_check_constraint("ck_product_lines_audience_owner", "product_lines", "audience = 'all' OR owner = 'plan'")


def downgrade() -> None:
    for name in ("ck_product_lines_audience_owner", "ck_product_lines_audience_at", "ck_product_lines_audience"):
        op.drop_constraint(name, "product_lines", type_="check")
    op.drop_column("product_lines", "audience_at")
    op.drop_column("product_lines", "audience")
