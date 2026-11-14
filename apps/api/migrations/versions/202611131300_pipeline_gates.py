"""Pipeline gates and deploy targets: `pipeline_gates`, `gate_runs`, `gate_results`, `stage_arrivals`, `stage_health`

Additive only. Creates the gate entity (`pipeline_gates`, `set_updated_at` trigger) and its run bookkeeping, adds each
stage's deploy target (`target_kind`, `target_config`, `target_credential`) and `gates_seeded_at`, widens
`url_source`, `releases.kind` (+ `rollback`) and `release_notices.kind` (+ `release_rolled_back`), and adds
`releases.rolls_back_id`, `reapplied_by_id` and `inputs`. Composite org keys on every stage and project reference;
the org-inherit trigger on the two derived tables.

Backfill (D14 as corrected, architecture §6), idempotent — keyed on "no gate rows yet for this stage and phase":
the integration stage's `before` list gets the command gate "Full gate" (the project's `gate_full_cmd`, required,
5400 s, one retry) when that command is not blank; every other branch stage's `before` list (a release step) gets
the CI gate "All checks on the release PR" (wait, all checks); a stage with a `deploy_environment` gets the
`forge_env` target, every other `none` with its url kept; no `after` gates. Every stage is then marked seeded. A stage
an older release writes after this ran stays unseeded, and every reader shows it the same defaults
(`app/pipeline_gates/defaults.py`), so it is never gate-less.

The downgrade drops what the upgrade added; widened checks narrow again only after deleting the rows they admit.

Revision ID: 202611131300
Revises: 202611131200
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202611131300"
down_revision: Union[str, None] = "202611131200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SYSTEM_ACTOR = sa.text("'01900000-0000-7000-8000-000000000001'::uuid")
# Frozen copies of `app/models/releases.py` and `app/releases/lifecycle.py` as of this revision: a migration never
# imports the live model.
_URL_SOURCES_OLD = ("detected", "manual", "easy_mode")
_URL_SOURCES = (*_URL_SOURCES_OLD, "argocd", "webhook", "script")
_TARGET_KINDS = ("none", "forge_env", "argocd", "webhook", "script")
_RELEASE_KINDS_OLD = ("promote", "hotfix", "backmerge", "pick")
_RELEASE_KINDS = (*_RELEASE_KINDS_OLD, "rollback")
_NOTICE_KINDS_OLD = ("release_approval", "release_live", "release_failed")
_NOTICE_KINDS = (*_NOTICE_KINDS_OLD, "release_rolled_back")
_PHASES = ("before", "after")
_GATE_KINDS = ("command", "ci", "webhook", "probe", "alerts", "ticket")
_RUN_SUBJECTS = ("lane_batch", "release", "arrival")
_RUN_STATES = ("running", "passed", "failed", "canceled")
_RESULT_STATES = ("waiting", "running", "passed", "failed", "timed_out", "off", "skipped")
_ARRIVAL_STATES = ("deploying", "arrived", "deploy_failed", "verifying", "verified", "verify_failed", "superseded",
                   "rolled_back")
_ARRIVAL_OPEN = ("deploying", "verifying")
_HEALTH_STATES = ("healthy", "down", "unchecked")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _project_column(table: str) -> sa.Column:
    return sa.Column(
        "project_id", sa.Integer(),
        sa.ForeignKey("projects.id", ondelete="CASCADE", name=f"fk_{table}_project_id"), nullable=False,
    )


def _stage_column(table: str, *, nullable: bool = False, ondelete: str = "CASCADE",
                  primary_key: bool = False) -> sa.Column:
    return sa.Column(
        "stage_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("release_stages.id", ondelete=ondelete, name=f"fk_{table}_stage_id"),
        nullable=nullable, primary_key=primary_key,
    )


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


# One integration stage per project: branch-level and storing no branch (`app/releases/stages.py::is_integration`).
_BACKFILL_FULL_GATE = sa.text(r"""
INSERT INTO pipeline_gates (org_id, project_id, stage_id, phase, position, kind, enabled, required, timeout_s,
                            retries, config, name)
SELECT s.org_id, s.project_id, s.id, 'before', 0, 'command', true, true, 5400, 1,
       jsonb_build_object('run', btrim(p.gate_full_cmd, E' \t\r\n\f\013')), 'Full gate'
FROM release_stages s JOIN projects p ON p.id = s.project_id
WHERE s.level = 'branch' AND s.branch IS NULL AND s.gates_seeded_at IS NULL
  AND btrim(coalesce(p.gate_full_cmd, ''), E' \t\r\n\f\013') <> ''
  AND NOT EXISTS (SELECT 1 FROM pipeline_gates g WHERE g.stage_id = s.id AND g.phase = 'before')
""")
# Every other branch stage is a release step's target: today's `pr checks --watch` in the release executor.
_BACKFILL_RELEASE_CHECKS = sa.text("""
INSERT INTO pipeline_gates (org_id, project_id, stage_id, phase, position, kind, enabled, required, timeout_s,
                            retries, config, name)
SELECT s.org_id, s.project_id, s.id, 'before', 0, 'ci', true, true, 7200, 0,
       '{"mode": "wait", "workflow": null, "all_checks": true}'::jsonb, 'All checks on the release PR'
FROM release_stages s
WHERE s.level = 'branch' AND s.branch IS NOT NULL AND s.gates_seeded_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM pipeline_gates g WHERE g.stage_id = s.id AND g.phase = 'before')
""")
_BACKFILL_TARGETS = sa.text("""
UPDATE release_stages SET target_kind = 'forge_env'
WHERE deploy_environment IS NOT NULL AND btrim(deploy_environment) <> '' AND gates_seeded_at IS NULL
""")
_MARK_SEEDED = sa.text("UPDATE release_stages SET gates_seeded_at = now() WHERE gates_seeded_at IS NULL")


def upgrade() -> None:
    from app.entity_shape import set_updated_at_trigger_sql
    from app.tenancy.inherit_trigger import org_inherit_trigger_sql
    from app.tenancy.keys import CompositeKey, add_sql, key_name, validate_sql

    # ── Stage targets ────────────────────────────────────────────────────────────────────────────────
    op.add_column("release_stages", sa.Column("target_kind", sa.Text(), nullable=False, server_default="none"))
    op.add_column("release_stages", sa.Column("target_config", postgresql.JSONB(astext_type=sa.Text()),
                                              nullable=True))
    op.add_column("release_stages", sa.Column("target_credential", sa.Text(), nullable=True))
    op.add_column("release_stages", _ts("gates_seeded_at", nullable=True))
    op.create_check_constraint("ck_release_stages_target_kind", "release_stages", f"target_kind IN {_in(_TARGET_KINDS)}")
    _replace_check("release_stages", "ck_release_stages_url_source",
                   f"url_source IS NULL OR url_source IN {_in(_URL_SOURCES)}")

    # ── Releases and notices ─────────────────────────────────────────────────────────────────────────
    op.add_column("releases", sa.Column(
        "rolls_back_id", sa.BigInteger(),
        sa.ForeignKey("releases.id", ondelete="SET NULL", name="fk_releases_rolls_back_id"), nullable=True))
    op.add_column("releases", sa.Column(
        "reapplied_by_id", sa.BigInteger(),
        sa.ForeignKey("releases.id", ondelete="SET NULL", name="fk_releases_reapplied_by_id"), nullable=True))
    op.add_column("releases", sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    _replace_check("releases", "ck_releases_kind", f"kind IN {_in(_RELEASE_KINDS)}")
    _replace_check("release_notices", "ck_release_notices_kind", f"kind IN {_in(_NOTICE_KINDS)}")

    # ── Gates ────────────────────────────────────────────────────────────────────────────────────────
    op.create_table(
        "pipeline_gates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        _ts("created_at"), _ts("updated_at"),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, server_default=_SYSTEM_ACTOR),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, server_default=_SYSTEM_ACTOR),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        _project_column("pipeline_gates"),
        _stage_column("pipeline_gates"),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("timeout_s", sa.Integer(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("credential", sa.Text(), nullable=True),
        sa.UniqueConstraint("id", "org_id", name="uq_pipeline_gates_id_org_id"),
        sa.UniqueConstraint("stage_id", "phase", "position", name="uq_pipeline_gates_stage_phase_position",
                            deferrable=True, initially="DEFERRED"),
        sa.CheckConstraint(f"phase IN {_in(_PHASES)}", name="ck_pipeline_gates_phase"),
        sa.CheckConstraint(f"kind IN {_in(_GATE_KINDS)}", name="ck_pipeline_gates_kind"),
        sa.CheckConstraint("position >= 0", name="ck_pipeline_gates_position"),
        sa.CheckConstraint("timeout_s BETWEEN 60 AND 21600", name="ck_pipeline_gates_timeout_s"),
        sa.CheckConstraint("retries BETWEEN 0 AND 3", name="ck_pipeline_gates_retries"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="ck_pipeline_gates_name"),
    )
    op.create_index("ix_pipeline_gates_project_id", "pipeline_gates", ["project_id"])

    op.create_table(
        "gate_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        _project_column("gate_runs"),
        _stage_column("gate_runs", nullable=True, ondelete="SET NULL"),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("sha", sa.String(64), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="running"),
        sa.Column("gates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _ts("created_at"), _ts("finished_at", nullable=True),
        sa.UniqueConstraint("id", "org_id", name="uq_gate_runs_id_org_id"),
        sa.CheckConstraint(f"phase IN {_in(_PHASES)}", name="ck_gate_runs_phase"),
        sa.CheckConstraint(f"subject_kind IN {_in(_RUN_SUBJECTS)}", name="ck_gate_runs_subject_kind"),
        sa.CheckConstraint(f"state IN {_in(_RUN_STATES)}", name="ck_gate_runs_state"),
    )
    op.create_index("ix_gate_runs_project_subject", "gate_runs", ["project_id", "subject_kind", "subject_id"])

    op.create_table(
        "gate_results",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("gate_runs.id", ondelete="CASCADE", name="fk_gate_results_run_id"), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("gate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="waiting"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _ts("started_at", nullable=True), _ts("finished_at", nullable=True),
        sa.Column("report", sa.Text(), nullable=True),
        sa.Column("link", sa.Text(), nullable=True),
        sa.CheckConstraint(f"state IN {_in(_RESULT_STATES)}", name="ck_gate_results_state"),
        sa.CheckConstraint("report IS NULL OR char_length(report) <= 1000", name="ck_gate_results_report"),
        sa.CheckConstraint("link IS NULL OR link LIKE 'https://%'", name="ck_gate_results_link"),
    )

    op.create_table(
        "stage_arrivals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        _project_column("stage_arrivals"),
        _stage_column("stage_arrivals"),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("release_id", sa.BigInteger(),
                  sa.ForeignKey("releases.id", ondelete="SET NULL", name="fk_stage_arrivals_release_id"),
                  nullable=True),
        sa.Column("merge_lane_row_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("target_kind", sa.Text(), nullable=False, server_default="none"),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("report", sa.Text(), nullable=True),
        sa.Column("gate_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("after_gates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _ts("created_at"), _ts("updated_at"),
        sa.UniqueConstraint("id", "org_id", name="uq_stage_arrivals_id_org_id"),
        sa.CheckConstraint(f"state IN {_in(_ARRIVAL_STATES)}", name="ck_stage_arrivals_state"),
        sa.CheckConstraint(f"target_kind IN {_in(_TARGET_KINDS)}", name="ck_stage_arrivals_target_kind"),
    )
    op.create_index("uq_stage_arrivals_open", "stage_arrivals", ["stage_id"], unique=True,
                    postgresql_where=sa.text(f"state IN {_in(_ARRIVAL_OPEN)}"))
    op.create_index("ix_stage_arrivals_project_id", "stage_arrivals", ["project_id"])

    op.create_table(
        "stage_health",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        _stage_column("stage_health", primary_key=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="unchecked"),
        _ts("checked_at", nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        _ts("wanted_until", nullable=True),
        sa.CheckConstraint(f"state IN {_in(_HEALTH_STATES)}", name="ck_stage_health_state"),
        sa.CheckConstraint("detail IS NULL OR char_length(detail) <= 200", name="ck_stage_health_detail"),
    )

    op.execute(set_updated_at_trigger_sql("pipeline_gates"))

    def key(table: str, column: str, parent: str, on_delete: str) -> CompositeKey:
        return CompositeKey(table=table, parent_table=parent, child_columns=(column, "org_id"),
                            parent_columns=("id", "org_id"), on_delete=on_delete, name=key_name(table, column))

    keys = [
        *(key(table, "project_id", "projects", "CASCADE")
          for table in ("pipeline_gates", "gate_runs", "stage_arrivals")),
        key("pipeline_gates", "stage_id", "release_stages", "CASCADE"),
        key("gate_runs", "stage_id", "release_stages", "SET NULL"),
        key("stage_arrivals", "stage_id", "release_stages", "CASCADE"),
        key("gate_results", "run_id", "gate_runs", "CASCADE"),
        key("stage_health", "stage_id", "release_stages", "CASCADE"),
    ]
    for one in keys:
        op.execute(add_sql(one))
        op.execute(validate_sql(one))
    op.execute(org_inherit_trigger_sql("gate_results", (("gate_runs", "run_id"),)))
    op.execute(org_inherit_trigger_sql("stage_health", (("release_stages", "stage_id"),)))

    op.execute(_BACKFILL_FULL_GATE)
    op.execute(_BACKFILL_RELEASE_CHECKS)
    op.execute(_BACKFILL_TARGETS)
    op.execute(_MARK_SEEDED)


def downgrade() -> None:
    for table in ("stage_health", "stage_arrivals", "gate_results", "gate_runs", "pipeline_gates"):
        op.drop_table(table)
    op.execute(sa.text("DELETE FROM release_notices WHERE kind = 'release_rolled_back'"))
    _replace_check("release_notices", "ck_release_notices_kind", f"kind IN {_in(_NOTICE_KINDS_OLD)}")
    op.execute(sa.text("DELETE FROM releases WHERE kind = 'rollback'"))
    _replace_check("releases", "ck_releases_kind", f"kind IN {_in(_RELEASE_KINDS_OLD)}")
    for column in ("inputs", "reapplied_by_id", "rolls_back_id"):
        op.drop_column("releases", column)
    op.execute(sa.text("UPDATE release_stages SET url_source = 'manual' "
                       f"WHERE url_source IS NOT NULL AND url_source NOT IN {_in(_URL_SOURCES_OLD)}"))
    _replace_check("release_stages", "ck_release_stages_url_source",
                   f"url_source IS NULL OR url_source IN {_in(_URL_SOURCES_OLD)}")
    op.drop_constraint("ck_release_stages_target_kind", "release_stages", type_="check")
    for column in ("gates_seeded_at", "target_credential", "target_config", "target_kind"):
        op.drop_column("release_stages", column)
