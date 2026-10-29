"""`slack_threads.brief`: a brief recipient's one line for a question batch

A brief recipient is told once per batch of questions (`alert_relay.brief_skips`), so that line speaks for every
round of the batch and settles when the whole batch does (`slack_answer_echo._batch_lines`). Additive: an older
release neither writes nor reads the column (the default covers its inserts).

Revision ID: 202610281400
Revises: 202610281300
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610281400"
down_revision: Union[str, None] = "202610281300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "slack_threads", sa.Column("brief", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )


def downgrade() -> None:
    op.drop_column("slack_threads", "brief")
