"""`billing_invoices.collection_method`: how Stripe collects the invoice

The operator's Resend is Stripe's own invoice email, which Stripe sends only for an invoice it collects by email
(`send_invoice`), so the desk offers it only there. NULL = not re-read since this column landed; the next event or
the nightly reconcile (which re-reads every open invoice) fills it.

EXPAND ONLY: one new nullable column the outgoing release never reads.

Revision ID: 202610261500
Revises: 202610260530
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610261500"
down_revision: Union[str, None] = "202610260530"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("billing_invoices", sa.Column("collection_method", sa.Text(), nullable=True), if_not_exists=True)


def downgrade() -> None:
    op.drop_column("billing_invoices", "collection_method")
