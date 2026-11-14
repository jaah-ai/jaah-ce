"""Custom domains: a claim that has not proved ownership no longer blocks another claimant

Additive only. `uq_hosted_app_domains_env_name` becomes partial: one row per `(env, name)` among rows past
`waiting_dns` (ownership proved), so several orgs may wait on the same name at once. The trigger
`trg_hosted_app_domains_proved_name` keeps refusing a new `waiting_dns` row for a name a proved row holds, raising
`unique_violation` under the index's name, so the add route's refusal reads the same. Who wins a pending name is
`app/hosted_apps/domain_lifecycle.py`'s (the first row whose TXT verifies; the others are dropped).

The downgrade restores the full unique index after dropping every pending row that shares its name with another
row (keeping the oldest), since the full index cannot hold them.

Revision ID: 202611141500
Revises: 202611141400
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611141500"
down_revision: Union[str, Sequence[str], None] = "202611141400"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uq_hosted_app_domains_env_name"
_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION hosted_app_domains_proved_name() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state = 'waiting_dns' AND EXISTS (
      SELECT 1 FROM hosted_app_domains d
      WHERE d.env = NEW.env AND d.name = NEW.name AND d.state <> 'waiting_dns') THEN
    RAISE EXCEPTION 'hostname % is held by a proved domain', NEW.name
      USING ERRCODE = 'unique_violation', CONSTRAINT = '{_INDEX}';
  END IF;
  RETURN NEW;
END
$$
"""
_TRIGGER_SQL = ("CREATE TRIGGER trg_hosted_app_domains_proved_name BEFORE INSERT ON hosted_app_domains "
                "FOR EACH ROW EXECUTE FUNCTION hosted_app_domains_proved_name()")


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="hosted_app_domains")
    op.create_index(_INDEX, "hosted_app_domains", ["env", "name"], unique=True,
                    postgresql_where=sa.text("state <> 'waiting_dns'"))
    op.execute(_FUNCTION_SQL)
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_hosted_app_domains_proved_name ON hosted_app_domains")
    op.execute("DROP FUNCTION IF EXISTS hosted_app_domains_proved_name()")
    op.execute("""
        DELETE FROM hosted_app_domains d
        WHERE d.state = 'waiting_dns' AND EXISTS (
            SELECT 1 FROM hosted_app_domains o
            WHERE o.env = d.env AND o.name = d.name AND o.id <> d.id
              AND (o.state <> 'waiting_dns' OR (o.created_at, o.id) < (d.created_at, d.id)))
    """)
    op.drop_index(_INDEX, table_name="hosted_app_domains")
    op.create_index(_INDEX, "hosted_app_domains", ["env", "name"], unique=True)
