"""Link projects to org pipeline templates

Additive only. Adds `projects.pipeline_template_id` (the template the project follows, RESTRICT: a template is not
deleted while linked) and `projects.pipeline_template_version_id` (the published version it is on), both NULL = not
linked, each held to the project's org by a composite key; and `release_stages.template_stage_key` (the template stage
a stage is, so a renamed or moved template stage keeps the project's stage). Rules: `app/releases/linking.py`. No
backfill. The downgrade drops the three columns.

Revision ID: 202611130700
Revises: 202611130637
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202611130700"
down_revision: Union[str, None] = "202611130637"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.tenancy.keys import CompositeKey, add_sql, key_name, validate_sql

    op.add_column("projects", sa.Column(
        "pipeline_template_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("pipeline_templates.id", ondelete="RESTRICT", name="fk_projects_pipeline_template_id"),
        nullable=True,
    ))
    op.add_column("projects", sa.Column(
        "pipeline_template_version_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("pipeline_template_versions.id", ondelete="RESTRICT",
                      name="fk_projects_pipeline_template_version_id"),
        nullable=True,
    ))
    op.add_column("release_stages", sa.Column("template_stage_key", sa.Text(), nullable=True))
    for column, parent in (("pipeline_template_id", "pipeline_templates"),
                           ("pipeline_template_version_id", "pipeline_template_versions")):
        key = CompositeKey(
            table="projects", parent_table=parent, child_columns=(column, "org_id"), parent_columns=("id", "org_id"),
            on_delete="RESTRICT", name=key_name("projects", column),
        )
        op.execute(add_sql(key))
        op.execute(validate_sql(key))


def downgrade() -> None:
    op.drop_column("release_stages", "template_stage_key")
    op.drop_column("projects", "pipeline_template_version_id")
    op.drop_column("projects", "pipeline_template_id")
