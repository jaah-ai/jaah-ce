"""A webhook delivery's API key, and the key a task was filed with

`trigger_deliveries.api_key_id` names the Jaah API key a webhook delivery was verified with (its owner files it);
`tasks.created_by_api_key_id` / `created_by_api_key_name` record it on the filed task, the trigger pair's shape.

Safe while two releases overlap: additive nullable / defaulted columns the outgoing release never reads. While both
run, an outgoing replica's intake pass may file a keyed delivery as its trigger's owner (it reads no `api_key_id`), and
its editor refuses `auth: api_key` (422) — both end with the outgoing release.

Revision ID: 202610272100
Revises: 202610271800
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "202610272100"
down_revision: Union[str, None] = "202610271800"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trigger_deliveries", sa.Column("api_key_id", UUID(as_uuid=True), nullable=True))
    op.add_column("tasks", sa.Column(
        "created_by_api_key_id", UUID(as_uuid=True),
        sa.ForeignKey("api_keys.id", ondelete="SET NULL", name="fk_tasks_created_by_api_key_id"),
        nullable=True,
    ))
    op.add_column("tasks", sa.Column("created_by_api_key_name", sa.Text(), nullable=False, server_default=""))
    # A key's delete sets its tasks' column NULL: indexed, as `created_by_trigger_id` is.
    op.create_index("ix_tasks_created_by_api_key_id", "tasks", ["created_by_api_key_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_created_by_api_key_id", table_name="tasks")
    op.drop_column("tasks", "created_by_api_key_name")
    op.drop_column("tasks", "created_by_api_key_id")
    op.drop_column("trigger_deliveries", "api_key_id")
