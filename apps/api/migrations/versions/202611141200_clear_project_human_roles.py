"""Retire the project "Humans" roles: clear `projects.business_analyst_id` and `projects.architect_id`

Data only. A question on a task inside an epic now goes to the task's assignee, else its creator, like any other
task's (`app/notify_targets.py::_recipient_candidates`), so nothing reads the two columns. The model no longer maps
them (`migrations/env.py::_RETIRING_COLUMNS` hides them from autogenerate) and this clears every row. The columns
stay for the outgoing release and are dropped one release later; a value it writes meanwhile is read by nothing.

The downgrade is a no-op: the cleared names are not recoverable, and an unset role is the ordinary state.

Revision ID: 202611141200
Revises: 202611141100
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202611141200"
down_revision: Union[str, Sequence[str], None] = "202611141100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE projects SET business_analyst_id = NULL, architect_id = NULL "
        "WHERE business_analyst_id IS NOT NULL OR architect_id IS NOT NULL"
    )


def downgrade() -> None:
    pass
