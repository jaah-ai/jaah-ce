"""Org pipeline guardrails

Additive only. Creates `pipeline_guardrails` (`app/models/releases.py::PipelineGuardrail`; rules in
`app/releases/guardrails.py`): one row per org and built-in rule, with its mode and, for `production_approval`, the
approvals it asks for. An entity, so it gets the `set_updated_at` trigger. No backfill: an org's three rows are written
Off on first read. The downgrade drops the table.

Revision ID: 202611130300
Revises: 202611130200
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202611130300"
down_revision: Union[str, None] = "202611130200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYSTEM_ACTOR = sa.text("'01900000-0000-7000-8000-000000000001'::uuid")
# Frozen copies of `app/models/releases.py`'s vocabulary as of this revision: a migration never imports the live model.
_RULES = ("production_approval", "no_auto_production", "production_maintainers")
_MODES = ("off", "warn", "block")
_APPROVALS_MAX = 5


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    from app.entity_shape import set_updated_at_trigger_sql

    op.create_table(
        "pipeline_guardrails",
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
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False, server_default="off"),
        sa.Column("approvals_min", sa.Integer(), nullable=True),
        sa.UniqueConstraint("id", "org_id", name="uq_pipeline_guardrails_id_org_id"),
        sa.UniqueConstraint("org_id", "rule", name="uq_pipeline_guardrails_org_rule"),
        sa.CheckConstraint(f"rule IN {_in(_RULES)}", name="ck_pipeline_guardrails_rule"),
        sa.CheckConstraint(f"mode IN {_in(_MODES)}", name="ck_pipeline_guardrails_mode"),
        sa.CheckConstraint(
            f"approvals_min IS NULL OR approvals_min BETWEEN 1 AND {_APPROVALS_MAX}",
            name="ck_pipeline_guardrails_approvals_min",
        ),
    )
    op.execute(set_updated_at_trigger_sql("pipeline_guardrails"))


def downgrade() -> None:
    op.drop_table("pipeline_guardrails")
