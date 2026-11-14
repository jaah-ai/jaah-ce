"""Stripe safety rails: `billing_stripe_writes`, `billing_reconcile_runs` + `billing_reconcile_diffs`, and `origin`
on `billing_notices` and `email_deliveries`

Additive only: three new deployment-wide tables (no tenancy column; `customer_org_id` on the log and the diffs is a
copied pointer to the customer's org, never the tenancy column) and two nullable text columns. No existing row is rewritten; a NULL `origin` reads
"raised before the column". The downgrade drops what this adds.

Revision ID: 202611130200
Revises: 202611130100
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202611130200"
down_revision: Union[str, None] = "202611130100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()"))


def _at(name: str, *, nullable: bool = False) -> sa.Column:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.add_column("billing_notices", sa.Column("origin", sa.Text(), nullable=True))
    op.add_column("email_deliveries", sa.Column("origin", sa.Text(), nullable=True))

    op.create_table(
        "billing_stripe_writes",
        _uuid_pk(),
        _at("at"),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("customer_org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("release", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("stripe_request_id", sa.Text(), nullable=True),
        sa.Column("object_id", sa.Text(), nullable=True),
        sa.Column("stripe_account_id", sa.Text(), nullable=True),
        sa.Column("livemode", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("outcome IN ('ok', 'refused', 'unavailable')", name="ck_billing_stripe_writes_outcome"),
    )
    op.create_index("ix_billing_stripe_writes_at", "billing_stripe_writes", ["at"])
    op.create_index("ix_billing_stripe_writes_origin_at", "billing_stripe_writes", ["origin", "at"])
    op.create_index(
        "ix_billing_stripe_writes_customer_org_at", "billing_stripe_writes", ["customer_org_id", "at"],
        postgresql_where=sa.text("customer_org_id IS NOT NULL"),
    )

    op.create_table(
        "billing_reconcile_runs",
        _uuid_pk(),
        _at("started_at"),
        _at("finished_at", nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("stripe_account_id", sa.Text(), nullable=True),
        sa.Column("livemode", sa.Boolean(), nullable=True),
        sa.Column("differences", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("lines", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.CheckConstraint("trigger IN ('nightly', 'manual')", name="ck_billing_reconcile_runs_trigger"),
        sa.CheckConstraint("outcome IN ('running', 'ok', 'failed')", name="ck_billing_reconcile_runs_outcome"),
    )
    op.create_index("ix_billing_reconcile_runs_started_at", "billing_reconcile_runs", ["started_at"])

    op.create_table(
        "billing_reconcile_diffs",
        _uuid_pk(),
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("billing_reconcile_runs.id", ondelete="CASCADE", name="fk_billing_reconcile_diffs_run_id"),
            nullable=False,
        ),
        sa.Column("customer_org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("lane", sa.Text(), nullable=True),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Text(), nullable=True),
        sa.Column("price_id", sa.Text(), nullable=True),
        sa.Column("what", sa.Text(), nullable=False),
        sa.Column("stripe_value", sa.Text(), nullable=True),
        sa.Column("jaah_value", sa.Text(), nullable=True),
        _at("created_at"),
    )
    op.create_index("ix_billing_reconcile_diffs_run", "billing_reconcile_diffs", ["run_id"])


def downgrade() -> None:
    op.drop_table("billing_reconcile_diffs")
    op.drop_table("billing_reconcile_runs")
    op.drop_table("billing_stripe_writes")
    op.drop_column("email_deliveries", "origin")
    op.drop_column("billing_notices", "origin")
