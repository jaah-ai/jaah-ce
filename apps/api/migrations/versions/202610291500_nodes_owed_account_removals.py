"""`nodes.owed_account_removals`: the account removals a node still owes (`app/account_removal.py`)

An account Delete that cannot reach a node (offline, or the node failed) records the removal here; the node's next
connect sends it again. Additive: a JSONB column with a `'[]'` default; an older release neither reads nor writes it.

Revision ID: 202610291500
Revises: 202610291100
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202610291500"
down_revision: Union[str, None] = "202610291100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column("owed_account_removals", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("nodes", "owed_account_removals")
