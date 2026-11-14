"""A node's cap on tasks running at once: `nodes.max_running_tasks`

Additive only. A nullable integer, NULL = no limit (every existing node), held to >= 1 by
`ck_nodes_max_running_tasks`; read by the launch gate `app/launch_gates.py` (TASKS). The downgrade drops both.

Revision ID: 202611142200
Revises: 202611142100
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611142200"
down_revision: Union[str, Sequence[str], None] = "202611142100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("max_running_tasks", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_nodes_max_running_tasks", "nodes", "max_running_tasks >= 1")


def downgrade() -> None:
    op.drop_constraint("ck_nodes_max_running_tasks", "nodes", type_="check")
    op.drop_column("nodes", "max_running_tasks")
