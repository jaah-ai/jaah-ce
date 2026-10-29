"""The `comment-image-reap` system cron and its automation parent, seeded ENABLED

Hourly, this cron (`app/cron_system_activities.py`, `app/comment_service.py::reap_unclaimed_images`) deletes each
staged comment image no post or edit claimed within a day, with its bytes. Seeded ENABLED: unarmed, every abandoned
draft's image is kept forever. Data only, no schema change. The baseline's insert shapes, so a row an operator already
created is left alone (`ON CONFLICT ... DO NOTHING`; a parent only for a row still without one).

LITERALS, not imported: a revision cannot import the ORM. `tests/test_cron_system_activities.py` holds the row
against its registration.

EXPAND ONLY: a new row the outgoing release has no registration for (an unknown activity's firing records an error
and changes nothing). The downgrade clears the parent, then deletes exactly this system row.

Revision ID: 202610282358
Revises: 202610281700
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202610282358"
down_revision: Union[str, None] = "202610281700"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAME = "Comment image reap"
_TYPE = "comment-image-reap"
_DESCRIPTION = (
    "Deletes each image staged for a comment that no post or edit claimed within a day, with its stored "
    "bytes."
)
_INTERVAL_SECONDS = 3600
_TIMEOUT_SECONDS = 300
_MIN_GAP_SECONDS = 60
# Only a pass that reaped is kept; one that found nothing is a noop.
_RETENTION_MODE = "acted"

# The baseline's parent insert (`_SYSTEM_CRON_PARENTS_SQL`), verbatim, restricted to this type.
_PARENTS_SQL = """
WITH children AS MATERIALIZED (
    SELECT c.id, c.org_id, c.name, c.description, c.created_at, c.updated_at, c.created_by_id, c.updated_by_id,
           c.activity_type, c.activity_params, uuidv7() AS parent_id
    FROM crons c
    WHERE c.is_system AND c.automation_id IS NULL AND c.activity_type = :type
),
parents AS (
    INSERT INTO automations (
        id, org_id, name, description, created_at, updated_at, created_by_id, updated_by_id, slug, when_kind,
        is_system, target_ids, action_type, action_params
    )
    SELECT parent_id, org_id, name, description, created_at, updated_at, created_by_id, updated_by_id,
           coalesce(nullif(trim(both '-' from left(trim(both '-' from
               regexp_replace(lower(name), '[^a-z0-9]+', '-', 'g')), 60)), ''), 'automation'),
           'schedule', true, '[]'::jsonb, activity_type, activity_params
    FROM children
    RETURNING id
)
UPDATE crons c SET automation_id = ch.parent_id FROM children ch WHERE c.id = ch.id
"""


def upgrade() -> None:
    # The operator plane's org, as the baseline seeds every system cron (a stdlib-only module).
    from app.tenancy.ids import ORG_ZERO_ID

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO crons (name, description, schedule_kind, cron_expression, "
            "interval_seconds, activity_type, enabled, retention_mode, min_gap_seconds, "
            "timeout_seconds, is_system, org_id) "
            "VALUES (:name, :description, 'interval', NULL, :interval_seconds, "
            ":activity_type, true, :retention_mode, :min_gap_seconds, :timeout_seconds, "
            "true, CAST(:org AS uuid)) "
            "ON CONFLICT (activity_type) WHERE is_system DO NOTHING"
        ),
        {
            "name": _NAME,
            "description": _DESCRIPTION,
            "interval_seconds": _INTERVAL_SECONDS,
            "activity_type": _TYPE,
            "retention_mode": _RETENTION_MODE,
            "min_gap_seconds": _MIN_GAP_SECONDS,
            "timeout_seconds": _TIMEOUT_SECONDS,
            "org": str(ORG_ZERO_ID),
        },
    )
    conn.execute(sa.text(_PARENTS_SQL), {"type": _TYPE})


def downgrade() -> None:
    conn = op.get_bind()
    parents = [
        row[0] for row in conn.execute(
            sa.text("SELECT automation_id FROM crons WHERE is_system AND automation_id IS NOT NULL "
                    "AND activity_type = :type"),
            {"type": _TYPE},
        )
    ]
    if parents:
        conn.execute(sa.text("UPDATE crons SET automation_id = NULL WHERE automation_id = ANY(:ids)"),
                     {"ids": parents})
        conn.execute(sa.text("DELETE FROM automations WHERE id = ANY(:ids)"), {"ids": parents})
    conn.execute(sa.text("DELETE FROM crons WHERE is_system AND activity_type = :type"), {"type": _TYPE})
