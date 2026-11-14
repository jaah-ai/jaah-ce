"""Pipeline step rules: approvers, timeouts, soak, allowed hours, concurrency, scoped and repeating freezes, Freeze now

Schema only, additive (every default reads as today's behaviour, so the previous release keeps working against it):

- `release_stages` (the settings of the step INTO the stage): `approvers` (NULL = same as who may press),
  `requester_cant_approve` (true), `approval_timeout_minutes` (NULL = none, else 15..43200), `on_timeout`
  (`cancel`), `approver_instructions` (''), `soak_minutes` (0), `allowed_hours` (NULL = any time), `concurrency`
  (`newest`), `resumed_at` (NULL), each range a CHECK;
- `release_freezes`: `repeat` (backfilled from `weekly`, then default `never`), `scope` (`after_integration`),
  `let_pressed` (false), `let_queued` (true); `weekly` stays and is still written, for the previous release;
- table `release_freeze_stages` (a freeze's stages when `scope = 'stages'`), org-keyed through its freeze;
- `projects.freeze_now_at` / `freeze_now_by_id` (NULL = off);
- `releases.arrived_at` (backfilled from `updated_at` for live/shipped releases), `queued_at`, `cancellation`,
  `freeze_override`;
- `release_approvals.comment`, `pipeline_approvals.comment` / `freeze_override` / `reminded_at`, and index
  `ix_pipeline_approvals_project_kind`.

The downgrade drops all of it.

Revision ID: 202611130400
Revises: 202611130300
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202611130400"
down_revision: Union[str, None] = "202611130300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies of `app/models/releases.py`'s vocabularies, as of this revision.
_ON_TIMEOUT = ("cancel", "remind")
_CONCURRENCY = ("newest", "in_order")
_REPEATS = ("never", "daily", "weekly", "monthly")
_SCOPES = ("after_integration", "stages")
_MINUTES_MAX = 43200
_INSTRUCTIONS_MAX = 1000
_COMMENT_MAX = 500


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    from app.tenancy.inherit_trigger import org_inherit_trigger_sql
    from app.tenancy.keys import CompositeKey, add_sql, key_name, validate_sql

    stages = "release_stages"
    op.add_column(stages, sa.Column("approvers", _jsonb(), nullable=True))
    op.add_column(stages, sa.Column("requester_cant_approve", sa.Boolean(), nullable=False,
                                    server_default=sa.text("true")))
    op.add_column(stages, sa.Column("approval_timeout_minutes", sa.Integer(), nullable=True))
    op.add_column(stages, sa.Column("on_timeout", sa.Text(), nullable=False, server_default="cancel"))
    op.add_column(stages, sa.Column("approver_instructions", sa.Text(), nullable=False, server_default=""))
    op.add_column(stages, sa.Column("soak_minutes", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column(stages, sa.Column("allowed_hours", _jsonb(), nullable=True))
    op.add_column(stages, sa.Column("concurrency", sa.Text(), nullable=False, server_default="newest"))
    op.add_column(stages, _ts("resumed_at"))
    op.create_check_constraint(
        "ck_release_stages_approval_timeout", stages,
        f"approval_timeout_minutes IS NULL OR approval_timeout_minutes BETWEEN 15 AND {_MINUTES_MAX}")
    op.create_check_constraint("ck_release_stages_on_timeout", stages, f"on_timeout IN {_in(_ON_TIMEOUT)}")
    op.create_check_constraint("ck_release_stages_approver_instructions", stages,
                               f"char_length(approver_instructions) <= {_INSTRUCTIONS_MAX}")
    op.create_check_constraint("ck_release_stages_soak_minutes", stages, f"soak_minutes BETWEEN 0 AND {_MINUTES_MAX}")
    op.create_check_constraint("ck_release_stages_concurrency", stages, f"concurrency IN {_in(_CONCURRENCY)}")

    freezes = "release_freezes"
    op.add_column(freezes, sa.Column("repeat", sa.Text(), nullable=False, server_default="never"))
    op.execute(sa.text("UPDATE release_freezes SET repeat = 'weekly' WHERE weekly"))
    op.add_column(freezes, sa.Column("scope", sa.Text(), nullable=False, server_default="after_integration"))
    op.add_column(freezes, sa.Column("let_pressed", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column(freezes, sa.Column("let_queued", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.create_check_constraint("ck_release_freezes_repeat", freezes, f"repeat IN {_in(_REPEATS)}")
    op.create_check_constraint("ck_release_freezes_scope", freezes, f"scope IN {_in(_SCOPES)}")

    op.create_table(
        "release_freeze_stages",
        sa.Column("freeze_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("release_freezes.id", ondelete="CASCADE", name="fk_release_freeze_stages_freeze_id"),
                  primary_key=True),
        sa.Column("stage_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("release_stages.id", ondelete="CASCADE", name="fk_release_freeze_stages_stage_id"),
                  primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.create_index("ix_release_freeze_stages_stage_id", "release_freeze_stages", ["stage_id"])
    # Derived through its freeze (`app/tenancy/manifest.py`), as `pipeline_approvals` is through its stage.
    key = CompositeKey(
        table="release_freeze_stages", parent_table="release_freezes", child_columns=("freeze_id", "org_id"),
        parent_columns=("id", "org_id"), on_delete="CASCADE", name=key_name("release_freeze_stages", "freeze_id"),
    )
    op.execute(add_sql(key))
    op.execute(validate_sql(key))
    op.execute(org_inherit_trigger_sql("release_freeze_stages", (("release_freezes", "freeze_id"),)))

    op.add_column("projects", _ts("freeze_now_at"))
    op.add_column("projects", sa.Column(
        "freeze_now_by_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_projects_freeze_now_by_id"), nullable=True))

    op.add_column("releases", _ts("arrived_at"))
    op.add_column("releases", _ts("queued_at"))
    op.add_column("releases", sa.Column("cancellation", _jsonb(), nullable=True))
    op.add_column("releases", sa.Column("freeze_override", _jsonb(), nullable=True))
    # A live or shipped release's last move was its arrival.
    op.execute(sa.text("UPDATE releases SET arrived_at = updated_at WHERE state IN ('live', 'shipped')"))

    op.add_column("release_approvals", sa.Column("comment", sa.Text(), nullable=True))
    op.create_check_constraint("ck_release_approvals_comment", "release_approvals",
                               f"comment IS NULL OR char_length(comment) <= {_COMMENT_MAX}")
    op.add_column("pipeline_approvals", sa.Column("comment", sa.Text(), nullable=True))
    op.add_column("pipeline_approvals", sa.Column("freeze_override", _jsonb(), nullable=True))
    op.add_column("pipeline_approvals", _ts("reminded_at"))
    op.create_check_constraint("ck_pipeline_approvals_comment", "pipeline_approvals",
                               f"comment IS NULL OR char_length(comment) <= {_COMMENT_MAX}")
    op.create_index("ix_pipeline_approvals_project_kind", "pipeline_approvals", ["project_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_approvals_project_kind", table_name="pipeline_approvals")
    op.drop_constraint("ck_pipeline_approvals_comment", "pipeline_approvals", type_="check")
    for column in ("reminded_at", "freeze_override", "comment"):
        op.drop_column("pipeline_approvals", column)
    op.drop_constraint("ck_release_approvals_comment", "release_approvals", type_="check")
    op.drop_column("release_approvals", "comment")
    for column in ("freeze_override", "cancellation", "queued_at", "arrived_at"):
        op.drop_column("releases", column)
    op.drop_column("projects", "freeze_now_by_id")
    op.drop_column("projects", "freeze_now_at")
    op.drop_table("release_freeze_stages")
    for name in ("ck_release_freezes_scope", "ck_release_freezes_repeat"):
        op.drop_constraint(name, "release_freezes", type_="check")
    for column in ("let_queued", "let_pressed", "scope", "repeat"):
        op.drop_column("release_freezes", column)
    for name in ("ck_release_stages_concurrency", "ck_release_stages_soak_minutes",
                 "ck_release_stages_approver_instructions", "ck_release_stages_on_timeout",
                 "ck_release_stages_approval_timeout"):
        op.drop_constraint(name, "release_stages", type_="check")
    for column in ("resumed_at", "concurrency", "allowed_hours", "soak_minutes", "approver_instructions",
                   "on_timeout", "approval_timeout_minutes", "requester_cant_approve", "approvers"):
        op.drop_column("release_stages", column)
