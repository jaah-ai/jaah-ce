"""`billing_subscriptions.kind`: which subscription carries the plan and which the hostings

`plan` (default) | `hosting`. A subscription with no plan item (hostings only) is backfilled `hosting`. A second
partial unique index allows one live subscription per kind; the one-live-per-org index stays until a later
revision drops it. Additive: an older release neither writes nor reads the column (the default covers its inserts).

Revision ID: 202610281200
Revises: 202610280300
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610281200"
down_revision: Union[str, None] = "202610280300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LIVE = "status NOT IN ('canceled', 'incomplete_expired')"


def upgrade() -> None:
    op.add_column("billing_subscriptions", sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'plan'")))
    op.execute("UPDATE billing_subscriptions SET kind = 'hosting' WHERE plan_item_id IS NULL")
    op.create_check_constraint("ck_billing_subscriptions_kind", "billing_subscriptions", "kind IN ('plan', 'hosting')")
    op.create_index(
        "uq_billing_subscriptions_org_kind_live", "billing_subscriptions",
        ["org_id", "stripe_account_id", "livemode", "kind"], unique=True, postgresql_where=sa.text(_LIVE),
    )


def downgrade() -> None:
    op.drop_index("uq_billing_subscriptions_org_kind_live", table_name="billing_subscriptions")
    op.drop_constraint("ck_billing_subscriptions_kind", "billing_subscriptions", type_="check")
    op.drop_column("billing_subscriptions", "kind")
