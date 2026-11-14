"""`trigger_notices.kind` gains `system_alert`: a node failure or AI account login alert on the bell

Additive only. Widens `ck_trigger_notices_kind` so the Owners and Admins told of `node-offline`, `runner-crashing`,
`login-expiring` and `login-expired` (`alert_record._tell_org_admins`) also get a bell row.

The downgrade deletes the `system_alert` rows and restores the old list.

Revision ID: 202611142100
Revises: 202611142000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202611142100"
down_revision: Union[str, Sequence[str], None] = "202611142000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies (a migration never imports the live code) of `app.triggers.NOTICE_KINDS`, before and after.
_OLD = ("trigger_failed", "trigger_digest", "automation_alert")
_NEW = (*_OLD, "system_alert")


def _check(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ", ".join(f"'{k}'" for k in kinds) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_trigger_notices_kind", "trigger_notices", type_="check")
    op.create_check_constraint("ck_trigger_notices_kind", "trigger_notices", _check(_NEW))


def downgrade() -> None:
    op.execute("DELETE FROM trigger_notices WHERE kind = 'system_alert'")
    op.drop_constraint("ck_trigger_notices_kind", "trigger_notices", type_="check")
    op.create_check_constraint("ck_trigger_notices_kind", "trigger_notices", _check(_OLD))
