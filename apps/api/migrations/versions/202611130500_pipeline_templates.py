"""Org pipeline templates

Additive only. Creates `pipeline_templates` (`app/models/releases.py::PipelineTemplate`; rules in
`app/releases/templates.py`): an org's template, an entity with its editable `draft` document and the version linked
projects follow (`published_version_id`, NULL until the first publish); and `pipeline_template_versions`, each
publish's document, never edited, numbered per template. Both NOT NULL `org_id` from birth; a version is held to its
template's org, and a template's published version to the template's org, by composite keys. The templates table gets
the `set_updated_at` trigger (an entity). No backfill. The downgrade drops both.

Revision ID: 202611130500
Revises: 202611130400
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202611130500"
down_revision: Union[str, None] = "202611130400"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYSTEM_ACTOR = sa.text("'01900000-0000-7000-8000-000000000001'::uuid")


def upgrade() -> None:
    from app.entity_shape import set_updated_at_trigger_sql
    from app.tenancy.keys import CompositeKey, add_sql, key_name, validate_sql

    op.create_table(
        "pipeline_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "created_by_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, server_default=_SYSTEM_ACTOR,
        ),
        sa.Column(
            "updated_by_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, server_default=_SYSTEM_ACTOR,
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("draft", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("id", "org_id", name="uq_pipeline_templates_id_org_id"),
        sa.UniqueConstraint("org_id", "slug", name="uq_pipeline_templates_org_slug"),
    )
    op.create_index("uq_pipeline_templates_org_name", "pipeline_templates", ["org_id", sa.text("lower(name)")],
                    unique=True)
    op.execute(set_updated_at_trigger_sql("pipeline_templates"))

    op.create_table(
        "pipeline_template_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "template_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_templates.id", ondelete="CASCADE",
                          name="fk_pipeline_template_versions_template_id"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_by_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_pipeline_template_versions_created_by_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("id", "org_id", name="uq_pipeline_template_versions_id_org_id"),
        sa.UniqueConstraint("template_id", "number", name="uq_pipeline_template_versions_template_number"),
    )
    op.create_foreign_key(
        "fk_pipeline_templates_published_version_id", "pipeline_templates", "pipeline_template_versions",
        ["published_version_id"], ["id"], ondelete="SET NULL",
    )
    for key in (
        CompositeKey(
            table="pipeline_template_versions", parent_table="pipeline_templates",
            child_columns=("template_id", "org_id"), parent_columns=("id", "org_id"), on_delete="CASCADE",
            name=key_name("pipeline_template_versions", "template_id"),
        ),
        CompositeKey(
            table="pipeline_templates", parent_table="pipeline_template_versions",
            child_columns=("published_version_id", "org_id"), parent_columns=("id", "org_id"), on_delete="SET NULL",
            name=key_name("pipeline_templates", "published_version_id"),
        ),
    ):
        op.execute(add_sql(key))
        op.execute(validate_sql(key))


def downgrade() -> None:
    op.drop_constraint("fk_pipeline_templates_published_version_id", "pipeline_templates", type_="foreignkey")
    op.execute("ALTER TABLE pipeline_templates DROP CONSTRAINT IF EXISTS fk_pipeline_templates_published_version_id_org")
    op.drop_table("pipeline_template_versions")
    op.drop_table("pipeline_templates")
