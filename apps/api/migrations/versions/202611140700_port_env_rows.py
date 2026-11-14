"""Port variables become Environment rows: `project_env_vars.source = 'port'` and `port_preferred`

A project's port variables were a free-text list on `projects.port_vars`. They are now rows of the Environment
catalogue with `source = 'port'`: the row holds no value, the node picks a free host port for each slot at lease, and
`port_preferred` (NULL = auto) names a port the node tries first. A Port row is project-tier only.

Schema (additive): the `source` domain gains `port`; new nullable `port_preferred` (1024..65535, only on a Port row);
the shape CHECK gains the Port arm (no literal, no credential); a Port row has no `user_id`.

Data: one Port row (auto) per `projects.port_vars` entry that is a valid variable name, unless the project already
has a project-tier row of that name — the existing row wins and the clash is logged. `projects.port_vars` itself is
left in place for the outgoing release and dropped one release later. Idempotent: a second run finds every name taken.

While both releases serve, the outgoing one cannot serialize a Port row (its `EnvVarOut.source` admits two values),
so its Environment tab answers 500 on a project with one until the rollout finishes; slots are unaffected (its
resolver omits a row with no value), and its writes to `projects.port_vars` are not read by the new release. Accepted:
the window is one deploy long.

The downgrade writes every Port row's name back into `projects.port_vars` (union with what is there), deletes the
Port rows and restores the old constraints. Lossy by design: a Port row's preferred port, description and authorship
have no home in the old schema.

Revision ID: 202611140700
Revises: 202611140600
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202611140700"
down_revision: Union[str, Sequence[str], None] = "202611140600"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")

# Frozen copies (a migration never imports the live code): `PROJECT_ENV_VAR_NAME_RE` and the constraint bodies.
_NAME_RE = "^[A-Za-z_][A-Za-z0-9_]{0,127}$"
_TABLE = "project_env_vars"
_OLD_SOURCE = "source IN ('literal', 'credential')"
_NEW_SOURCE = "source IN ('literal', 'credential', 'port')"
_OLD_SHAPE = (
    "(source = 'literal' AND literal IS NOT NULL AND credential_id IS NULL AND credential_field IS NULL) OR "
    "(source = 'credential' AND literal IS NULL AND credential_field IS NOT NULL)"
)
_NEW_SHAPE = (
    _OLD_SHAPE
    + " OR (source = 'port' AND literal IS NULL AND credential_id IS NULL AND credential_field IS NULL)"
)
_PORT_TIER = "source <> 'port' OR user_id IS NULL"
_PORT_PREFERRED = "port_preferred IS NULL OR (source = 'port' AND port_preferred BETWEEN 1024 AND 65535)"

# Each project's declared names, trimmed, as (project, org, name) — one row per distinct valid name.
_DECLARED = f"""
    SELECT DISTINCT p.id AS project_id, p.org_id, btrim(v.name) AS name
    FROM projects p
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(p.port_vars) = 'array' THEN p.port_vars ELSE '[]'::jsonb END
    ) AS v(name)
    WHERE btrim(v.name) ~ '{_NAME_RE}'
"""


def _replace_check(name: str, body: str) -> None:
    op.drop_constraint(name, _TABLE, type_="check")
    op.create_check_constraint(name, _TABLE, body)


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("port_preferred", sa.Integer(), nullable=True))
    _replace_check("ck_project_env_vars_source", _NEW_SOURCE)
    _replace_check("ck_project_env_vars_shape", _NEW_SHAPE)
    op.create_check_constraint("ck_project_env_vars_port_tier", _TABLE, _PORT_TIER)
    op.create_check_constraint("ck_project_env_vars_port_preferred", _TABLE, _PORT_PREFERRED)

    bind = op.get_bind()
    clashes = bind.execute(sa.text(
        f"SELECT d.project_id, d.name, e.source FROM ({_DECLARED}) d "
        f"JOIN {_TABLE} e ON e.project_id = d.project_id AND e.name = d.name AND e.user_id IS NULL "
        "AND e.source <> 'port' ORDER BY d.project_id, d.name"
    )).all()
    for project_id, name, source in clashes:
        log.warning(
            "port rows: project %s already has a %s variable %s, which is kept; no Port row was added",
            project_id, source, name,
        )
    bind.execute(sa.text(
        f"INSERT INTO {_TABLE} (org_id, project_id, name, source) "
        f"SELECT d.org_id, d.project_id, d.name, 'port' FROM ({_DECLARED}) d "
        f"WHERE NOT EXISTS (SELECT 1 FROM {_TABLE} e WHERE e.project_id = d.project_id AND e.name = d.name "
        "AND e.user_id IS NULL)"
    ))


def downgrade() -> None:
    op.execute(
        f"UPDATE projects p SET port_vars = ("
        f"  SELECT COALESCE(jsonb_agg(DISTINCT n ORDER BY n), '[]'::jsonb) FROM ("
        f"    SELECT jsonb_array_elements_text("
        f"      CASE WHEN jsonb_typeof(p.port_vars) = 'array' THEN p.port_vars ELSE '[]'::jsonb END) AS n"
        f"    UNION SELECT e.name FROM {_TABLE} e WHERE e.project_id = p.id AND e.source = 'port') names) "
        f"WHERE EXISTS (SELECT 1 FROM {_TABLE} e WHERE e.project_id = p.id AND e.source = 'port')"
    )
    op.execute(f"DELETE FROM {_TABLE} WHERE source = 'port'")
    op.drop_constraint("ck_project_env_vars_port_preferred", _TABLE, type_="check")
    op.drop_constraint("ck_project_env_vars_port_tier", _TABLE, type_="check")
    _replace_check("ck_project_env_vars_shape", _OLD_SHAPE)
    _replace_check("ck_project_env_vars_source", _OLD_SOURCE)
    op.drop_column(_TABLE, "port_preferred")
