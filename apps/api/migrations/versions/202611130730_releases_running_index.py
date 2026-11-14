"""Releases: one RUNNING release per target stage, not one open one

Swaps the partial unique index `uq_releases_open` (`state` in the non-terminal states) for `uq_releases_running`
(`state IN ('merging', 'deploying')`) on `releases (project_id, to_stage_id)`: several releases into one stage may now
wait at once ("In order" keeps every request), but never two merge or deploy at once. Requests into one stage are
serialised by a row lock on the target `release_stages` row instead (`app/releases/service.py::_request`), shipped in
the same release as this swap (architecture risk R-1).

The downgrade cancels every waiting release but the newest per target (the old index holds one), then restores
`uq_releases_open`.

Revision ID: 202611130730
Revises: 202611130700
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611130730"
down_revision: Union[str, None] = "202611130700"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies of `app/releases/lifecycle.py`'s vocabularies, as of this revision.
_OPEN = ("awaiting_approval", "scheduled", "queued", "merging", "deploying")
_RUNNING = ("merging", "deploying")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_index("uq_releases_running", "releases", ["project_id", "to_stage_id"], unique=True,
                    postgresql_where=sa.text(f"state IN {_in(_RUNNING)}"))
    op.drop_index("uq_releases_open", table_name="releases")


def downgrade() -> None:
    # The old index holds one open release per target: keep the running one, else the newest.
    op.execute(sa.text(f"""
        UPDATE releases SET state = 'canceled'
        WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY project_id, to_stage_id
                    ORDER BY (state IN {_in(_RUNNING)}) DESC, id DESC) AS n
                FROM releases WHERE state IN {_in(_OPEN)} AND to_stage_id IS NOT NULL
            ) ranked WHERE n > 1
        )
    """))
    op.create_index("uq_releases_open", "releases", ["project_id", "to_stage_id"], unique=True,
                    postgresql_where=sa.text(f"state IN {_in(_OPEN)}"))
    op.drop_index("uq_releases_running", table_name="releases")
