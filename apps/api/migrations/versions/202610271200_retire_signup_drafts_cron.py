"""Retire the `signup-drafts` system cron

Sign-up drafts are no longer purged: one stays until an operator deletes it from Customers. The seeded system row
(`202610251700_baseline.py`) goes; its runs cascade and its parent automation drops with its last child
(`automation_drop_childless`).

Safe while two releases overlap: the outgoing release still registers the cadence, but the scheduler fires rows only,
so with the row gone nothing runs it. Downgrade re-seeds nothing: the incoming release no longer registers it.

Revision ID: 202610271200
Revises: 202610270300
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202610271200"
down_revision: Union[str, None] = "202610270300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM crons WHERE is_system AND activity_type = 'signup-drafts'")


def downgrade() -> None:
    pass
