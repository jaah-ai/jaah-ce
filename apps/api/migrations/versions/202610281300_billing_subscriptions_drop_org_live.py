"""`billing_subscriptions`: a live plan and a live hosting subscription per org, replacing one live per org

Drops `uq_billing_subscriptions_org_live` (one non-terminal subscription per org) and revision 202610281200's per-kind
index, and bounds the table by two partial indexes instead: one live `plan` subscription WITH a plan item, and one live
`hosting` subscription, per (org, account, mode). A `plan` row whose plan item is gone (a cancelled plan's phase ran
while hostings still ride it: a legacy combined subscription) is in neither: it is no plan, so a plan bought next is
its own subscription beside it, and it never blocks the org's `hosting` subscription. Downgrade restores both old
indexes, which fails while an org holds two live subscriptions (end one first).

Revision ID: 202610281300
Revises: 202610281200
"""
from __future__ import annotations

# additive-check: exempt — drops UNIQUE indexes only (each replaced by a narrower one): a constraint loosens, no column
# or table the previous release reads or writes goes away, and that release never inserts a second live subscription
# for an org.

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610281300"
down_revision: Union[str, None] = "202610281200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LIVE = "status NOT IN ('canceled', 'incomplete_expired')"
_COLUMNS = ["org_id", "stripe_account_id", "livemode"]


def upgrade() -> None:
    op.create_index(
        "uq_billing_subscriptions_org_plan_live", "billing_subscriptions", _COLUMNS, unique=True,
        postgresql_where=sa.text(f"kind = 'plan' AND plan_item_id IS NOT NULL AND {_LIVE}"),
    )
    op.create_index(
        "uq_billing_subscriptions_org_hosting_live", "billing_subscriptions", _COLUMNS, unique=True,
        postgresql_where=sa.text(f"kind = 'hosting' AND {_LIVE}"),
    )
    op.drop_index("uq_billing_subscriptions_org_kind_live", table_name="billing_subscriptions")
    op.drop_index("uq_billing_subscriptions_org_live", table_name="billing_subscriptions")


def downgrade() -> None:
    op.create_index(
        "uq_billing_subscriptions_org_live", "billing_subscriptions", _COLUMNS, unique=True,
        postgresql_where=sa.text(_LIVE),
    )
    op.create_index(
        "uq_billing_subscriptions_org_kind_live", "billing_subscriptions", [*_COLUMNS, "kind"], unique=True,
        postgresql_where=sa.text(_LIVE),
    )
    op.drop_index("uq_billing_subscriptions_org_hosting_live", table_name="billing_subscriptions")
    op.drop_index("uq_billing_subscriptions_org_plan_live", table_name="billing_subscriptions")
