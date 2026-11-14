"""`projects.merge_lane_enabled` / `merge_lane_enabled_by`: the per-project merge lane switch.

Additive: every project, lane-active today or not, starts OFF and must opt in (`app/merge_lane_state.py::lane_states`
reads it after the org's plan switch). `merge_lane_enabled_by` is who turned it on (SET NULL on the person's delete).
The downgrade drops both columns.

Revision ID: 202611141000
Revises: 202611140900
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "202611141000"
down_revision: Union[str, Sequence[str], None] = "202611140900"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column(
        "merge_lane_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("projects", sa.Column("merge_lane_enabled_by", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_projects_merge_lane_enabled_by", "projects", "admin_users",
        ["merge_lane_enabled_by"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_projects_merge_lane_enabled_by", "projects", type_="foreignkey")
    op.drop_column("projects", "merge_lane_enabled_by")
    op.drop_column("projects", "merge_lane_enabled")
