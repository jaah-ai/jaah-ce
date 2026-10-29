"""`task_questions.plain`: a question round reworded in plain terms, for display only

Written after the round is recorded by `app/durable/question_plain_queue.py` (shape: `app/question_plain.py`). NULL =
not reworded (no model, a failed call, or a round older than this column; no backfill).

EXPAND ONLY: one new nullable column the outgoing release never reads.

Revision ID: 202610291600
Revises: 202610291500
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "202610291600"
down_revision: Union[str, None] = "202610291500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("task_questions", sa.Column("plain", JSONB(), nullable=True), if_not_exists=True)


def downgrade() -> None:
    op.drop_column("task_questions", "plain")
