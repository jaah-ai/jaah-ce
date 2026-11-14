"""Epic throttle and the project's memory switch: `tasks.max_running`, `projects.project_memory`

Additive only. `tasks.max_running` (nullable int, NULL = no limit) caps how many of an epic's subtasks run at once;
`projects.project_memory` (nullable bool, NULL = the org's default) replaces the per-task override as the layer
`app/project_memory/switch.py` reads. `ck_tasks_memory_arm_source` widens to admit `project`. The per-task
`tasks.project_memory` column stays (two releases overlap) and is no longer read.

The downgrade drops both columns; the widened check narrows again after rewriting `project` arms to `default`.

Revision ID: 202611140400
Revises: 202611140300
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140400"
down_revision: Union[str, None] = "202611140300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy of `app/models/tasks.py::MEMORY_ARM_SOURCES` as of this revision: a migration never imports the model.
_SOURCES_OLD = ("task", "epic", "default", "holdout", "killed", "plan")
_SOURCES = (*_SOURCES_OLD, "project")
_CHECK = "ck_tasks_memory_arm_source"


def _in(values: tuple[str, ...]) -> str:
    return "memory_arm_source IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column("tasks", sa.Column("max_running", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_tasks_max_running", "tasks", "max_running IS NULL OR max_running >= 1")
    op.add_column("projects", sa.Column("project_memory", sa.Boolean(), nullable=True))
    op.drop_constraint(_CHECK, "tasks", type_="check")
    op.create_check_constraint(_CHECK, "tasks", _in(_SOURCES))


def downgrade() -> None:
    op.execute("UPDATE tasks SET memory_arm_source = 'default' WHERE memory_arm_source = 'project'")
    op.drop_constraint(_CHECK, "tasks", type_="check")
    op.create_check_constraint(_CHECK, "tasks", _in(_SOURCES_OLD))
    op.drop_column("projects", "project_memory")
    op.drop_constraint("ck_tasks_max_running", "tasks", type_="check")
    op.drop_column("tasks", "max_running")
