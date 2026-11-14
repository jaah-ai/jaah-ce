"""Close the gate runs of releases that already failed

A release that failed before its gates finished left its gate run `running` and its gates `waiting` forever;
`app/releases/runs.py::mark_failed` now cancels them as it fails. This closes the ones left from
before: each `running` run of a `failed` release becomes `canceled`, its unfinished gates `skipped`, as
`app/pipeline_gates/results.py::cancel_runs` writes them.

Data only and idempotent (a canceled run is no longer `running`). The downgrade changes nothing: which runs this
closed is not recorded, and a run reopened as `running` would be stuck again.

Revision ID: 202611140300
Revises: 202611140200
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202611140300"
down_revision: Union[str, Sequence[str], None] = "202611140200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies (a migration never imports the live code): the stuck runs, by their failed release.
_STUCK = (
    "SELECT g.id FROM gate_runs g JOIN releases r ON g.subject_id = r.id::text AND r.project_id = g.project_id "
    "WHERE g.subject_kind = 'release' AND g.state = 'running' AND r.state = 'failed'"
)


def upgrade() -> None:
    op.execute(
        f"UPDATE gate_results SET state = 'skipped', finished_at = now() WHERE run_id IN ({_STUCK}) "
        "AND state NOT IN ('passed', 'failed', 'timed_out', 'off', 'skipped')"
    )
    op.execute(f"UPDATE gate_runs SET state = 'canceled', finished_at = now() WHERE id IN ({_STUCK})")


def downgrade() -> None:
    pass
