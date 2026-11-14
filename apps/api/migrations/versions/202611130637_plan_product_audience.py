"""Plan product audience: which of a plan's customers a node line reaches

Additive only. `billing_plan_products.audience` (NOT NULL, default `all`): a node line added to a plan with customers
may go to the orgs already on it (`existing`), to those that join it later (`new`), or both (`all`);
`audience_at` (NULL unless the audience is not `all`) is when it was added, the line between the two
(`app/plan_terms.py::ProductLine.reaches`). Every existing line is `all`. The downgrade drops both columns.

Revision ID: 202611130637
Revises: 202611130600
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611130637"
down_revision: Union[str, None] = "202611130600"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "billing_plan_products"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("audience", sa.Text(), nullable=False, server_default="all"))
    op.add_column(_TABLE, sa.Column("audience_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_check_constraint(
        f"ck_{_TABLE}_audience", _TABLE, "audience IN ('all', 'existing', 'new')",
    )
    op.create_check_constraint(
        f"ck_{_TABLE}_audience_at", _TABLE, "(audience = 'all') = (audience_at IS NULL)",
    )
    op.create_check_constraint(
        f"ck_{_TABLE}_audience_kind", _TABLE, "audience = 'all' OR kind IN ('shared_node', 'dedicated_node')",
    )


def downgrade() -> None:
    for name in ("audience_kind", "audience_at", "audience"):
        op.drop_constraint(f"ck_{_TABLE}_{name}", _TABLE, type_="check")
    op.drop_column(_TABLE, "audience_at")
    op.drop_column(_TABLE, "audience")
