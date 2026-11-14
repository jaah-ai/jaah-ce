"""The `stage-health` system cron: the pipeline stage health dot's probe (`app/releases/health.py`)

Seeds the cron row with its automation parent, as the baseline seeds every system cron, enabled: it is new, replaces
nothing, and a noop while no stage has an address. The downgrade removes both.

Revision ID: 202611131330
Revises: 202611131300
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611131330"
down_revision: Union[str, None] = "202611131300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CRON = {
    "name": "Stage health", "activity_type": "stage-health", "interval_seconds": 60, "timeout_seconds": 50,
    "description": "Checks the address of each pipeline stage that has one, every minute while someone has the "
                   "pipeline open and every 10 minutes otherwise, for the stage's health dot.",
}

# The baseline's `_SYSTEM_CRON_PARENTS_SQL`, for this one row: its automation parent (the cron's name, description
# and action, a slug from its name).
_PARENT_SQL = """
WITH children AS MATERIALIZED (
    SELECT c.id, c.org_id, c.name, c.description, c.created_at, c.updated_at, c.created_by_id, c.updated_by_id,
           c.activity_type, c.activity_params, uuidv7() AS parent_id
    FROM crons c
    WHERE c.is_system AND c.automation_id IS NULL AND c.activity_type = 'stage-health'
),
parents AS (
    INSERT INTO automations (
        id, org_id, name, description, created_at, updated_at, created_by_id, updated_by_id, slug, when_kind,
        is_system, target_ids, action_type, action_params
    )
    SELECT parent_id, org_id, name, description, created_at, updated_at, created_by_id, updated_by_id,
           'stage-health', 'schedule', true, '[]'::jsonb, activity_type, activity_params
    FROM children
    RETURNING id
)
UPDATE crons c SET automation_id = ch.parent_id FROM children ch WHERE c.id = ch.id
"""


def upgrade() -> None:
    from app.tenancy.ids import ORG_ZERO_ID

    op.execute(sa.text(
        "INSERT INTO crons (name, description, schedule_kind, cron_expression, interval_seconds, activity_type, "
        "enabled, retention_mode, min_gap_seconds, timeout_seconds, is_system, org_id) "
        "VALUES (:name, :description, 'interval', NULL, :interval_seconds, :activity_type, true, 'acted', 5, "
        ":timeout_seconds, true, CAST(:org AS uuid)) ON CONFLICT (activity_type) WHERE is_system DO NOTHING"
    ).bindparams(**_CRON, org=str(ORG_ZERO_ID)))
    op.execute(sa.text(_PARENT_SQL))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM automations WHERE id IN (SELECT automation_id FROM crons WHERE is_system AND "
        "activity_type = 'stage-health')"
    ))
    op.execute(sa.text("DELETE FROM crons WHERE is_system AND activity_type = 'stage-health'"))
