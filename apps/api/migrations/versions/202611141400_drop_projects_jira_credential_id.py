"""Drop `projects.jira_credential_id`: the retired per-project Jira secret

Jira is a tracker connection; the per-project credential pointer was unmapped one release earlier (it sat in
`migrations/env.py::_RETIRING_COLUMNS`), so the outgoing release no longer reads or writes it. This contracts it:
every row is cleared first (it is expected to be NULL everywhere already), then both foreign keys over it — the
plain one to `credentials` and the composite same-org key — and the column itself go.

The downgrade restores an empty, nullable column with both keys. Lossy by design: the values were already
cleared by the upgrade.

Revision ID: 202611141400
Revises: 202611141300
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# additive-check: exempt — contraction of a column the previous release already unmapped (expand/contract step 2).

revision: str = "202611141400"
down_revision: Union[str, Sequence[str], None] = "202611141300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies (a migration never imports the live code): the names and the composite key's body.
_TABLE = "projects"
_COLUMN = "jira_credential_id"
_FK = "fk_projects_jira_credential_id"
_ORG_FK = "fk_projects_jira_credential_id_org"


def upgrade() -> None:
    op.execute(f"UPDATE {_TABLE} SET {_COLUMN} = NULL WHERE {_COLUMN} IS NOT NULL")
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_ORG_FK}")
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK}")
    op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.UUID(), nullable=True))
    op.create_foreign_key(_FK, _TABLE, "credentials", [_COLUMN], ["id"], ondelete="SET NULL")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_ORG_FK} FOREIGN KEY ({_COLUMN}, org_id) "
        f"REFERENCES credentials (id, org_id) ON DELETE SET NULL ({_COLUMN}) NOT VALID"
    )
    op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_ORG_FK}")
