"""Drop the retired project port and "Humans" columns and the port grid table

Contraction step. The release before this one stopped mapping all of them (they sat in
`migrations/env.py::_RETIRING_COLUMNS` / `_RETIRING_TABLES`), so the outgoing release no longer reads them:

- `projects.port_block` and its per-org unique `uq_projects_org_id_port_block` (slot ports are node-assigned);
- `projects.port_vars` (a project's port vars are Port rows of `project_env_vars`, revision 202611140700);
- table `port_grid_vars` (the retired port grid's range starts);
- `projects.business_analyst_id` and `projects.architect_id` with their SET NULL foreign keys (revision
  202611141200 cleared every row).

The downgrade restores each as an empty shell with its constraints. Lossy by design: the values are gone.

Revision ID: 202611141600
Revises: 202611141500
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# additive-check: exempt — contraction of columns and a table the previous release already unmapped (expand/contract step 2).

revision: str = "202611141600"
down_revision: Union[str, Sequence[str], None] = "202611141500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies (a migration never imports the live code).
_PROJECTS = "projects"
_PORT_BLOCK_UNIQUE = "uq_projects_org_id_port_block"
_HUMAN_ROLE_FKS = (("business_analyst_id", "fk_projects_business_analyst"), ("architect_id", "fk_projects_architect"))
_GRID = "port_grid_vars"
_SYSTEM_ACTOR = "'01900000-0000-7000-8000-000000000001'::uuid"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_PROJECTS} DROP CONSTRAINT IF EXISTS {_PORT_BLOCK_UNIQUE}")
    for column, fk in _HUMAN_ROLE_FKS:
        op.execute(f"ALTER TABLE {_PROJECTS} DROP CONSTRAINT IF EXISTS {fk}")
        op.drop_column(_PROJECTS, column)
    op.drop_column(_PROJECTS, "port_block")
    op.drop_column(_PROJECTS, "port_vars")
    op.drop_table("port_grid_vars")


def downgrade() -> None:
    op.create_table(
        _GRID,
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("name", sa.TEXT(), nullable=False),
        sa.Column("description", sa.TEXT(), server_default=sa.text("''::text"), nullable=False),
        sa.Column("range_start", sa.INTEGER(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", sa.UUID(), server_default=sa.text(_SYSTEM_ACTOR), nullable=False),
        sa.Column("updated_by_id", sa.UUID(), server_default=sa.text(_SYSTEM_ACTOR), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["admin_users.id"], name=f"{_GRID}_created_by_id_fkey", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["admin_users.id"], name=f"{_GRID}_updated_by_id_fkey", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{_GRID}_pkey"),
        sa.UniqueConstraint("id", "org_id", name=f"uq_{_GRID}_id_org_id"),
        sa.UniqueConstraint("name", name=f"uq_{_GRID}_name"),
        sa.UniqueConstraint("org_id", "name", name=f"uq_{_GRID}_org_id_name"),
    )
    op.execute(
        f"CREATE TRIGGER trg_{_GRID}_set_updated_at "
        f"BEFORE UPDATE ON {_GRID} FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()"
    )
    op.add_column(_PROJECTS, sa.Column("port_vars", postgresql.JSONB(astext_type=sa.Text()),
                                       server_default=sa.text("'[]'::jsonb"), nullable=True))
    op.add_column(_PROJECTS, sa.Column("port_block", sa.INTEGER(), nullable=True))
    op.create_unique_constraint(_PORT_BLOCK_UNIQUE, _PROJECTS, ["org_id", "port_block"])
    for column, fk in _HUMAN_ROLE_FKS:
        op.add_column(_PROJECTS, sa.Column(column, sa.UUID(), nullable=True))
        op.create_foreign_key(fk, _PROJECTS, "admin_users", [column], ["id"], ondelete="SET NULL")
