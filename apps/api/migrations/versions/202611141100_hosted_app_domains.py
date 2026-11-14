"""Custom domains for Easy Mode apps: `hosted_app_domains`, `hosted_app_domain_months` and their plan terms

Additive only. Creates `hosted_app_domains` (entity shape, `set_updated_at` trigger, composite org key onto
`hosted_apps`, unique `(env, name)` across every org, at most one main domain per app) and
`hosted_app_domain_months` (the hostnames that served in a metered month); adds the plan switch
`billing_plans.custom_domains_allowed` (default false, so OFF on every plan and `(default)`) with
`orgs.custom_domains_allowed_override`, and the per-app cap `billing_plans.easy_mode_domains_per_app` (NULL =
`(default)`'s, else 2) with `orgs.easy_mode_domains_per_app_override`. Rules: `app/hosted_apps/domains.py`.

The downgrade drops what the upgrade added.

Revision ID: 202611141100
Revises: 202611141000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202611141100"
down_revision: Union[str, Sequence[str], None] = "202611141000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYSTEM_ACTOR = sa.text("'01900000-0000-7000-8000-000000000001'::uuid")
# Frozen copies of `app/models/hosted_apps.py` as of this revision: a migration never imports the live model.
_ENVS = ("dev", "main")
_STATES = ("waiting_dns", "issuing", "live", "attention", "removing")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def upgrade() -> None:
    from app.entity_shape import set_updated_at_trigger_sql
    from app.tenancy.keys import CompositeKey, add_sql, key_name, validate_sql

    op.add_column("billing_plans", sa.Column("custom_domains_allowed", sa.Boolean(), nullable=True,
                                             server_default=sa.text("false")))
    op.add_column("orgs", sa.Column("custom_domains_allowed_override", sa.Boolean(), nullable=True))
    op.add_column("billing_plans", sa.Column("easy_mode_domains_per_app", sa.Integer(), nullable=True))
    op.add_column("orgs", sa.Column("easy_mode_domains_per_app_override", sa.Integer(), nullable=True))

    op.create_table(
        "hosted_app_domains",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        _ts("created_at"), _ts("updated_at"),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, server_default=_SYSTEM_ACTOR),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, server_default=_SYSTEM_ACTOR),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hosted_app_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("hosted_apps.id", ondelete="CASCADE", name="fk_hosted_app_domains_hosted_app_id"),
                  nullable=False),
        sa.Column("env", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="waiting_dns"),
        sa.Column("is_main", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("verify_token", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=True),
        sa.Column("attention_reason", sa.Text(), nullable=True),
        _ts("attention_since", nullable=True), _ts("last_checked_at", nullable=True),
        _ts("next_check_at", nullable=True), _ts("live_at", nullable=True), _ts("notified_at", nullable=True),
        sa.Column("main_pick", sa.Text(), nullable=True),
        sa.CheckConstraint(f"env IN {_in(_ENVS)}", name="ck_hosted_app_domains_env"),
        sa.CheckConstraint(f"state IN {_in(_STATES)}", name="ck_hosted_app_domains_state"),
    )
    op.create_index("uq_hosted_app_domains_env_name", "hosted_app_domains", ["env", "name"], unique=True)
    op.create_index("uq_hosted_app_domains_main", "hosted_app_domains", ["hosted_app_id"], unique=True,
                    postgresql_where=sa.text("is_main"))
    op.create_index("ix_hosted_app_domains_next_check", "hosted_app_domains", ["next_check_at"])
    op.execute(set_updated_at_trigger_sql("hosted_app_domains"))
    key = CompositeKey(table="hosted_app_domains", parent_table="hosted_apps",
                       child_columns=("hosted_app_id", "org_id"), parent_columns=("id", "org_id"),
                       on_delete="CASCADE", name=key_name("hosted_app_domains", "hosted_app_id"))
    op.execute(add_sql(key))
    op.execute(validate_sql(key))

    op.create_table(
        "hosted_app_domain_months",
        sa.Column("env", sa.Text(), primary_key=True),
        sa.Column("month", sa.Date(), primary_key=True),
        sa.Column("hostname", sa.Text(), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.CheckConstraint(f"env IN {_in(_ENVS)}", name="ck_hosted_app_domain_months_env"),
    )


def downgrade() -> None:
    op.drop_table("hosted_app_domain_months")
    op.drop_table("hosted_app_domains")
    op.drop_column("orgs", "easy_mode_domains_per_app_override")
    op.drop_column("billing_plans", "easy_mode_domains_per_app")
    op.drop_column("orgs", "custom_domains_allowed_override")
    op.drop_column("billing_plans", "custom_domains_allowed")
