"""Node groups (code name `placement_groups`): the three tables, the plan switch, and static slots demoted

Additive only. Creates `placement_groups` (entity shape plus `slug`, `set_updated_at` trigger, unique per org by
slug and by lower(name), no row named "All"), `placement_group_nodes` (group ↔ node) and `project_placement_groups`
(project ↔ group), every `org_id` NOT NULL from birth and each reference held to its row's org by a composite key.
"All" — a project with no `project_placement_groups` row — is computed, so nothing is written for it. Adds the plan
switch `billing_plans.placement_groups_allowed` (default false; true on `business`, which Business Trial and
Enterprise include) with `orgs.placement_groups_allowed_override`; the operator's own org (org zero) has no plan, so
its override is set true. Every `static` worktree becomes `dynamic` and `worktrees.kind` defaults to `dynamic`: free
ones are retired by the next sweep, held ones once freed. `node_projects` and `node_group_projects` stay, unread.

The downgrade drops what the upgrade added and restores the `static` default; demoted rows stay `dynamic`.

Revision ID: 202611141900
Revises: 202611141800
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202611141900"
down_revision: Union[str, Sequence[str], None] = "202611141800"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYSTEM_ACTOR = sa.text("'01900000-0000-7000-8000-000000000001'::uuid")
# Frozen copy (a migration never imports the live code): org zero.
_ORG_ZERO = "01900000-0000-7000-8000-000000000010"


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()"))


def _group_id() -> sa.Column:
    return sa.Column("group_id", postgresql.UUID(as_uuid=True),
                     sa.ForeignKey("placement_groups.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    from app.entity_shape import set_updated_at_trigger_sql
    from app.tenancy.keys import CompositeKey, add_sql, key_name, validate_sql

    op.create_table(
        "placement_groups",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        _ts("created_at"), _ts("updated_at"),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, server_default=_SYSTEM_ACTOR),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, server_default=_SYSTEM_ACTOR),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.UniqueConstraint("id", "org_id", name="uq_placement_groups_id_org_id"),
        sa.UniqueConstraint("org_id", "slug", name="uq_placement_groups_org_slug"),
        sa.CheckConstraint("lower(name) <> 'all'", name="ck_placement_groups_name_not_all"),
    )
    op.create_index("uq_placement_groups_org_lower_name", "placement_groups", ["org_id", sa.text("lower(name)")],
                    unique=True)
    op.execute(set_updated_at_trigger_sql("placement_groups"))

    op.create_table(
        "placement_group_nodes",
        _uuid_pk(), sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        _group_id(),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("nodes.id", ondelete="CASCADE"),
                  nullable=False),
        sa.UniqueConstraint("group_id", "node_id", name="uq_placement_group_nodes_group_node"),
    )
    op.create_index("ix_placement_group_nodes_node_id", "placement_group_nodes", ["node_id"])

    op.create_table(
        "project_placement_groups",
        _uuid_pk(), sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        _group_id(),
        sa.UniqueConstraint("project_id", "group_id", name="uq_project_placement_groups_project_group"),
    )
    op.create_index("ix_project_placement_groups_project_id", "project_placement_groups", ["project_id"])
    op.create_index("ix_project_placement_groups_group_id", "project_placement_groups", ["group_id"])

    for table, column, parent in (
        ("placement_group_nodes", "group_id", "placement_groups"),
        ("placement_group_nodes", "node_id", "nodes"),
        ("project_placement_groups", "group_id", "placement_groups"),
        ("project_placement_groups", "project_id", "projects"),
    ):
        key = CompositeKey(table=table, parent_table=parent, child_columns=(column, "org_id"),
                           parent_columns=("id", "org_id"), on_delete="CASCADE", name=key_name(table, column))
        op.execute(add_sql(key))
        op.execute(validate_sql(key))

    op.add_column("billing_plans", sa.Column("placement_groups_allowed", sa.Boolean(), nullable=True,
                                             server_default=sa.text("false")))
    op.add_column("orgs", sa.Column("placement_groups_allowed_override", sa.Boolean(), nullable=True))
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE billing_plans SET placement_groups_allowed = true WHERE key = 'business'"))
    bind.execute(sa.text("UPDATE orgs SET placement_groups_allowed_override = true WHERE id = CAST(:z AS uuid) "
                         "AND placement_groups_allowed_override IS NULL"), {"z": _ORG_ZERO})

    bind.execute(sa.text("UPDATE worktrees SET kind = 'dynamic' WHERE kind = 'static'"))
    op.alter_column("worktrees", "kind", server_default="dynamic")


def downgrade() -> None:
    op.alter_column("worktrees", "kind", server_default="static")
    op.drop_column("orgs", "placement_groups_allowed_override")
    op.drop_column("billing_plans", "placement_groups_allowed")
    op.drop_table("project_placement_groups")
    op.drop_table("placement_group_nodes")
    op.drop_table("placement_groups")
