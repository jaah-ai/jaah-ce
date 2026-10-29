"""`org_signups.billing_saved_at`: when a sign-up's billing details were last saved

The Customers roster's Stage column (`app/signup_stage.py`) tells a paid draft on Billing details from one on Card
without calling Stripe. NULL = not saved since this column landed (no backfill: such a draft shows Billing details).

EXPAND ONLY: one new nullable column the outgoing release never reads.

Revision ID: 202610262100
Revises: 202610262000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610262100"
down_revision: Union[str, None] = "202610262000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "org_signups", sa.Column("billing_saved_at", sa.TIMESTAMP(timezone=True), nullable=True), if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("org_signups", "billing_saved_at")
