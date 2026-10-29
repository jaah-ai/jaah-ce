"""DBOS rows move to the durable engine's new application name, `jaah` (`app/durable/runtime.py::APP_NAME`)

DBOS stamps every workflow, queue, schedule, version and step row with the application name it ran under, and an
executor recovers only rows carrying its own. The name changed, so the rows written under the retired one move with
it: in-flight workflows keep recovering, and retention (`app/cron_retention.py`) still finds them.

Data only, no DDL. DBOS creates `durable_dbos_sys` and its columns lazily, so every table is guarded: a database
whose engine never launched, or launched on a DBOS without the column, is skipped. A version row whose new name is
already claimed stays as it was rather than violating `uq_application_versions_owner_version`.

Revision ID: 202610291800
Revises: 202610291600
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202610291800"
down_revision: Union[str, None] = "202610291600"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCHEMA = "durable_dbos_sys"
_TABLES = ("workflow_status", "queues", "workflow_schedules", "application_versions", "operation_outputs")
_OLD = "cto-cockpit-durable"
_NEW = "jaah"


def _rename(old: str, new: str) -> None:
    for table in _TABLES:
        skip_claimed = (
            f' AND NOT EXISTS (SELECT 1 FROM {_SCHEMA}.application_versions c '
            f"WHERE c.application_name = ''{new}'' AND c.version_name = t.version_name)"
            if table == "application_versions" else ""
        )
        op.execute(f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = '{_SCHEMA}' AND table_name = '{table}' AND column_name = 'application_name') THEN
        EXECUTE 'UPDATE {_SCHEMA}.{table} t SET application_name = ''{new}'' '
                'WHERE t.application_name = ''{old}''{skip_claimed}';
    END IF;
END $$;
""")


def upgrade() -> None:
    _rename(_OLD, _NEW)


def downgrade() -> None:
    _rename(_NEW, _OLD)
