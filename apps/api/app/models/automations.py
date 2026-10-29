"""Automations: automations, crons and their runs, task triggers and their deliveries."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
# Stdlib-only leaf: the trigger tables' CHECKs render from its closed sets.
from app.triggers import (
    DEFAULT_AUTO_PAUSE_FAILURES,
    DEFAULT_CONCURRENCY_CAP,
    DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    DEFAULT_QUEUE_DEPTH,
    DEFAULT_RATE_LIMIT_PER_HOUR,
    DELIVERY_STATES,
    MAX_CONCURRENCY_CAP,
    MAX_PREFLIGHT_TIMEOUT_SECONDS,
    MAX_QUEUE_DEPTH,
    MAX_RATE_LIMIT_PER_HOUR,
    MIN_PREFLIGHT_TIMEOUT_SECONDS,
    NOTICE_KINDS as TRIGGER_NOTICE_KINDS,
    PREFLIGHT_KINDS,
    PREFLIGHT_NONE,
    PREFLIGHTING,
    RECEIVED,
    SOURCE_KINDS as TRIGGER_SOURCE_KINDS,
    TASK_TRIGGERS_TABLE,
    TRIGGER_DEDUP_TABLE,
    TRIGGER_DELIVERIES_TABLE,
    TRIGGER_NOTICES_TABLE,
)
from app.automations import (
    AUTOMATION_RUN_TARGETS_TABLE,
    AUTOMATIONS_TABLE,
    RUN_TARGET_OUTCOMES as AUTOMATION_RUN_TARGET_OUTCOMES,
    TARGET_KINDS as AUTOMATION_TARGET_KINDS,
    TARGET_MODES as AUTOMATION_TARGET_MODES,
    WHEN_KINDS as AUTOMATION_WHEN_KINDS,
)
from app.crons import (
    CRON_RUNS_TABLE,
    CRONS_TABLE,
    DEFAULT_RETENTION_MODE,
    MAX_CRON_EXPRESSION_CHARS,
    RETENTION_MODES,
    RUN_OUTCOMES,
    SCHEDULE_KIND_CRON,
    SCHEDULE_KIND_INTERVAL,
    SCHEDULE_KINDS,
    in_domain_sql,
)
from app.models.base import Base, EntityMixin, _in_check, org_unique, OrgScoped, timestamp_column, UuidPkMixin


class Automation(EntityMixin, OrgScoped, Base):
    """One automation: When -> Target -> Action (vocabulary `app/automations.py`).

    The parent of exactly one When-detail child — a `crons` row (`when_kind = 'schedule'`) or a `task_triggers`
    row (`'event'`), each pointing here through its unique `automation_id`. Revision 0417's deferred constraint
    trigger checks the one-child rule at commit; deleting the child deletes a parent it leaves childless.

    `created_by_id` is the OWNER, as whom every task is filed. `slug` is the human handle, unique per org.
    `target_*` is a live RULE (D3), never an expanded id list: NULL kind iff the action takes no target, `ids`
    empty for `all`. `action_type` is an `app/automation_actions.py` registry key, `action_params` its
    validated params. `is_system` comes from the descriptor (a system activity): at most one per type.

    There is deliberately no pause column this release: the pause is the child's (`crons.enabled`,
    `task_triggers.paused_at`), which the outgoing release still reads; `automation_service` exposes one
    `paused` over both.
    """

    __tablename__ = AUTOMATIONS_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_automations_org_slug"),
        _in_check("when_kind", AUTOMATION_WHEN_KINDS, "ck_automations_when_kind"),
        _in_check("target_kind", AUTOMATION_TARGET_KINDS, "ck_automations_target_kind"),
        _in_check("target_mode", AUTOMATION_TARGET_MODES, "ck_automations_target_mode"),
        CheckConstraint("(target_kind IS NULL) = (target_mode IS NULL)", name="ck_automations_target_pair"),
        CheckConstraint("jsonb_typeof(target_ids) = 'array'", name="ck_automations_target_ids_array"),
        CheckConstraint(
            "target_mode IS DISTINCT FROM 'all' OR target_ids = '[]'::jsonb", name="ck_automations_all_has_no_ids"
        ),
        CheckConstraint("target_kind IS NOT NULL OR target_ids = '[]'::jsonb", name="ck_automations_no_target_no_ids"),
        Index("uq_automations_system_action_type", "action_type", unique=True, postgresql_where=text("is_system")),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    when_kind: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    target_kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_mode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_ids: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    action_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    # Agent provenance (AC5.5): the task whose runner created it through `schedule_create`/`trigger_create`.
    created_by_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True,
    )


class AutomationRunTarget(UuidPkMixin, OrgScoped, Base):
    """One target of one firing, and what it came to: the per-target ledger the Runs view groups by outcome.
    The firing's own record stays `cron_runs` (schedule) or `trigger_deliveries` (event) — exactly one of the two
    is named. `target_name` is the target's name snapshotted at firing. Written by the fire engine only;
    append-only bookkeeping, so no entity shape."""

    __tablename__ = AUTOMATION_RUN_TARGETS_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        CheckConstraint("num_nonnulls(cron_run_id, delivery_id) = 1", name="ck_automation_run_targets_one_run"),
        _in_check("target_kind", AUTOMATION_TARGET_KINDS, "ck_automation_run_targets_target_kind"),
        _in_check("outcome", AUTOMATION_RUN_TARGET_OUTCOMES, "ck_automation_run_targets_outcome"),
        Index("ix_automation_run_targets_cron_run", "cron_run_id", postgresql_where=text("cron_run_id IS NOT NULL")),
        Index("ix_automation_run_targets_delivery", "delivery_id", postgresql_where=text("delivery_id IS NOT NULL")),
    )

    automation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("automations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cron_run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cron_runs.id", ondelete="CASCADE"), nullable=True
    )
    delivery_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("trigger_deliveries.id", ondelete="CASCADE"), nullable=True
    )
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )


class Cron(EntityMixin, OrgScoped, Base):
    """One scheduled activity — what to run, how often, and the runtime state of its schedule.

    A standard entity (0137). No `slug`; no `timezone` (one CP-wide knob). The schedule is two
    nullable columns plus a discriminator tied by `ck_crons_exactly_one_schedule`;
    `app/cron_schedule.py` turns either into an instant.

    `last_tick_at` is the heartbeat (a tick arrived), `last_fired_at` the anchor (an activity started),
    `last_succeeded_at` the last firing that finished `acted`/`noop`; stall alarms read the heartbeat since
    `retention_mode = 'none'` empties `cron_runs`.
    `min_gap_seconds`/`timeout_seconds`: NULL means none/default; zero is refused.

    `is_system` marks machinery, not a permission (still editable/deletable);
    `uq_crons_system_activity_type` allows at most one system cron per activity. `activity_type` has no
    FK or CHECK (registry: `app/cron_activities.py`).
    """

    __tablename__ = CRONS_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        # The 0137 CHECKs, rendered from the same constants: documentation only, since alembic does not
        # compare CHECKs.
        CheckConstraint(
            in_domain_sql("schedule_kind", SCHEDULE_KINDS), name="ck_crons_schedule_kind"
        ),
        CheckConstraint(
            in_domain_sql("retention_mode", RETENTION_MODES), name="ck_crons_retention_mode"
        ),
        CheckConstraint(
            f"(schedule_kind = '{SCHEDULE_KIND_CRON}' AND cron_expression IS NOT NULL "
            f"AND interval_seconds IS NULL) OR "
            f"(schedule_kind = '{SCHEDULE_KIND_INTERVAL}' AND interval_seconds IS NOT NULL "
            f"AND cron_expression IS NULL)",
            name="ck_crons_exactly_one_schedule",
        ),
        CheckConstraint(
            f"cron_expression IS NULL OR length(cron_expression) <= {MAX_CRON_EXPRESSION_CHARS}",
            name="ck_crons_cron_expression_length",
        ),
        CheckConstraint(
            "interval_seconds IS NULL OR interval_seconds > 0",
            name="ck_crons_interval_seconds_positive",
        ),
        CheckConstraint(
            "timeout_seconds IS NULL OR timeout_seconds > 0",
            name="ck_crons_timeout_seconds_positive",
        ),
        CheckConstraint(
            "min_gap_seconds IS NULL OR min_gap_seconds > 0",
            name="ck_crons_min_gap_seconds_positive",
        ),
        CheckConstraint(
            "min_gap_seconds IS NULL OR interval_seconds IS NULL "
            "OR min_gap_seconds < interval_seconds",
            name="ck_crons_min_gap_below_interval",
        ),
        # Partial over `is_system` (see the class docstring).
        Index(
            "uq_crons_system_activity_type",
            "activity_type",
            unique=True,
            postgresql_where=text("is_system"),
        ),
    )

    schedule_kind: Mapped[str] = mapped_column(Text, nullable=False)
    cron_expression: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # INTEGER: whole seconds, firing on a fixed UTC grid (`app/cron_schedule.py`).
    interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The phase the schedule was set from, distinct from `created_at`. Not read by next-fire (interval
    # phase is not configurable); read by the re-phasing reconcile on a settings change.
    schedule_anchor_at: Mapped[datetime] = timestamp_column()

    activity_type: Mapped[str] = mapped_column(Text, nullable=False)
    activity_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    retention_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default=DEFAULT_RETENTION_MODE, server_default=DEFAULT_RETENTION_MODE
    )
    min_gap_seconds: Mapped[Optional[Decimal]] = mapped_column(Numeric, nullable=True)
    timeout_seconds: Mapped[Optional[Decimal]] = mapped_column(Numeric, nullable=True)

    last_tick_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_fired_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # When a firing last settled `acted` or `noop` (revision 0321): the cron-starvation metric's clock
    # (`app/metrics_emf.py`), since `cron_runs` keeps no `noop` row under `retention_mode = 'acted'`.
    last_succeeded_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    consecutive_skips: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Agent provenance: the task whose runner created this cron (`routes/agent_crons.py`). A column
    # because it is an authorization predicate (the creator's edit/delete reach). SET NULL: the
    # creator's removal leaves the schedule running.
    created_by_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # The parent Automation this row is the schedule detail of (revision 0417; `app/automation_service.py`).
    # NULL only for a row a pre-automations release wrote mid-deploy. CASCADE:
    # deleting the automation deletes its schedule; deleting the schedule deletes a parent left childless
    # (the revision's trigger).
    automation_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("automations.id", ondelete="CASCADE"), nullable=True, unique=True,
    )


class CronRun(UuidPkMixin, OrgScoped, Base):
    """One firing of one cron, and what it came to.

    Append-only, so no entity shape (`tests/test_entity_shape.py` exempt list). Three timestamps, since
    the gaps are the diagnosis: `fired_at` (scheduled), `started_at` (began; NULL for a hold, skip or
    reconstructed miss), `finished_at` (returned). `outcome` (`app/crons.py`): `acted`/`noop` are the
    activity's answer, `skipped`/`missed`/`error` the wrapper's; `noop` stays distinct so "does this cron
    do any work?" is answerable. `cron_id` cascades; `task_id` SET NULL keeps the record of a created
    task.
    """

    __tablename__ = CRON_RUNS_TABLE
    __table_args__ = (
        CheckConstraint(in_domain_sql("outcome", RUN_OUTCOMES), name="ck_cron_runs_outcome"),
        # The Runs tab's one read, newest first, DESC in the index (busy crons add thousands a day).
        Index("ix_cron_runs_cron_id_fired_at", "cron_id", text("fired_at DESC")),
        # When it last did work (`automation_service._last_acted_at`): one probe past any run of noops.
        Index("ix_cron_runs_cron_id_acted", "cron_id", text("fired_at DESC"), postgresql_where=text("outcome = 'acted'")),
    )

    cron_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("crons.id", ondelete="CASCADE"), nullable=False
    )
    fired_at: Mapped[datetime] = timestamp_column()
    started_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    finished_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    # Which hold, as the wrapper's marker (rendered as prose); NULL when not held.
    skip_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class TaskTrigger(EntityMixin, OrgScoped, Base):
    """One event-driven trigger: a source (webhook, GitHub, Gmail, Slack) that files one Task shape into its own
    project, beside `crons` (vocabulary `app/triggers.py`).

    `created_by_id` is the OWNER: every filing is made as them, and their `tasks:create` on the project is
    re-asked at each one (`trigger_intake`). `project_id` must be in `org_id` (`routes/triggers.py`); a filing
    never names another project. `slug` is the human handle, unique per org and never changed after create.

    The secret (HMAC key or bearer token) is `secret_sealed`, a tier-1 envelope opened only by
    `trigger_secrets.open_secret`; no read serves it. `secret_revealed_at` NULL means its one reveal is still
    owed. `ingress_token` is the opaque URL id, not a secret (useless without the signature).

    Health is derived from the counters by `trigger_health.trigger_health`, never stored. "Enabled" means
    `paused_at IS NULL AND failed_at IS NULL`: paused is a human stop, failed a system one.
    """

    __tablename__ = TASK_TRIGGERS_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_task_triggers_org_slug"),
        UniqueConstraint("ingress_token", name="uq_task_triggers_ingress_token"),
        _in_check("source_kind", TRIGGER_SOURCE_KINDS, "ck_task_triggers_source_kind"),
        _in_check("preflight_kind", PREFLIGHT_KINDS, "ck_task_triggers_preflight_kind"),
        CheckConstraint(
            f"preflight_timeout_seconds BETWEEN {MIN_PREFLIGHT_TIMEOUT_SECONDS} AND {MAX_PREFLIGHT_TIMEOUT_SECONDS}",
            name="ck_task_triggers_preflight_timeout",
        ),
        CheckConstraint(
            f"concurrency_cap BETWEEN 1 AND {MAX_CONCURRENCY_CAP}", name="ck_task_triggers_concurrency_cap"
        ),
        CheckConstraint(f"queue_depth BETWEEN 0 AND {MAX_QUEUE_DEPTH}", name="ck_task_triggers_queue_depth"),
        CheckConstraint(
            f"rate_limit_per_hour BETWEEN 1 AND {MAX_RATE_LIMIT_PER_HOUR}", name="ck_task_triggers_rate_limit"
        ),
        CheckConstraint(
            "auto_pause_failures IS NULL OR auto_pause_failures >= 1", name="ck_task_triggers_auto_pause_failures"
        ),
        CheckConstraint(
            "auto_pause_idle_days IS NULL OR auto_pause_idle_days >= 1", name="ck_task_triggers_auto_pause_idle_days"
        ),
        Index("ix_task_triggers_project_id", "project_id"),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    ingress_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    secret_sealed: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    secret_revealed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    title_template: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    full_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    preflight_kind: Mapped[str] = mapped_column(
        Text, nullable=False, default=PREFLIGHT_NONE, server_default=PREFLIGHT_NONE
    )
    preflight_script_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    preflight_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    preflight_credential_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    preflight_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
        server_default=text(str(DEFAULT_PREFLIGHT_TIMEOUT_SECONDS)),
    )
    state_document: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    concurrency_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_CONCURRENCY_CAP, server_default=text(str(DEFAULT_CONCURRENCY_CAP))
    )
    queue_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_QUEUE_DEPTH, server_default=text(str(DEFAULT_QUEUE_DEPTH))
    )
    rate_limit_per_hour: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_RATE_LIMIT_PER_HOUR,
        server_default=text(str(DEFAULT_RATE_LIMIT_PER_HOUR)),
    )
    # Advisory only (D7): nothing ever stops a run for budget.
    budget_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    # NULL = off (D9 / D6).
    auto_pause_failures: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, default=DEFAULT_AUTO_PAUSE_FAILURES,
        server_default=text(str(DEFAULT_AUTO_PAUSE_FAILURES)),
    )
    auto_pause_idle_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    paused_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    paused_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    paused_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    failed_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    # The reason of the newest counted failure, so the Degraded badge can say what went wrong without a join.
    last_failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    budget_breach_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    budget_breach_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_delivery_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Dispatched, cleanly filtered or deduped (R13).
    last_success_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # When a human last resumed it (`trigger_health.clear_stops`): the idle rule counts from here too, so a
    # resumed idle trigger is not stopped again by the next digest before it has had a chance to run.
    resumed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Agent provenance (architecture §9, the advisor verbs): the task whose runner created this trigger. An
    # authorization predicate for that runner's own-row edits, like `Cron.created_by_task_id`. SET NULL.
    created_by_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # The parent Automation this row is the event detail of — `Cron.automation_id`'s twin (revision 0417).
    automation_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("automations.id", ondelete="CASCADE"), nullable=True, unique=True,
    )


class TriggerDelivery(OrgScoped, Base):
    """One inbound delivery of a trigger, or one preflight item of one (`parent_delivery_id`) — the processor's
    work queue and the owner's deliveries log at once (architecture §3.1/§3.6).

    `state` moves `received` → (`preflighting` →) one logged outcome, or `queued` → `dispatched`/`skipped`.
    `reason` is human text, never a code. `event` is the redacted event the processor renders from (purged at
    30 days); `preview` the readable, capped copy the log shows. `claimed_until` is the processing lease: a
    preflight's result is applied only while the claim it was taken under still stands (compare-and-swap).
    `trigger_id` is NULL only for an unmatched Slack mention or operator email (org zero).

    Append-mostly bookkeeping, so no entity shape (`tests/test_entity_shape.py` exempt list).
    """

    __tablename__ = TRIGGER_DELIVERIES_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("state", DELIVERY_STATES, "ck_trigger_deliveries_state"),
        Index("ix_trigger_deliveries_trigger_received", "trigger_id", text("received_at DESC")),
        Index("ix_trigger_deliveries_trigger_state", "trigger_id", "state"),
        # A Task delete SET NULLs these; the budget flag reads by task.
        Index("ix_trigger_deliveries_task_id", "task_id", postgresql_where=text("task_id IS NOT NULL")),
        # One preflight in flight per trigger, fleet-wide: a second row of the same trigger cannot be claimed
        # into `preflighting` on any replica (§3.6).
        Index(
            "uq_trigger_deliveries_one_preflighting", "trigger_id", unique=True,
            postgresql_where=text(f"state = '{PREFLIGHTING}'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trigger_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_triggers.id", ondelete="CASCADE"), nullable=True
    )
    parent_delivery_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("trigger_deliveries.id", ondelete="CASCADE"), nullable=True, index=True
    )
    received_at: Mapped[datetime] = timestamp_column()
    processed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, default=RECEIVED, server_default=RECEIVED)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    item_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dedup_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    missing_paths: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    event: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    preview: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    claimed_until: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # When the preflight answered `run: true` for this row, so the pass does not ask it again.
    preflighted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    budget_exceeded_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    outcome_counted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    inbound_email_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("inbound_emails.id", ondelete="SET NULL"), nullable=True
    )
    slack_event_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The API key a webhook delivery was verified with (`trigger_intake._key_verdict`): its owner files it. No FK on
    # purpose: a key deleted before the filing must refuse it (`api_keys.context_of`), never fall back to the owner.
    api_key_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # The target items a queued delivery still owes (the fire engine writes it). NULL
    # when it owes nothing, and on a delivery queued before the engine (it owes its trigger's one project).
    owed_targets: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)


class TriggerDedup(OrgScoped, Base):
    """One item a trigger has already acted on, keyed by item id or the event's natural id (R9). Reserved
    inside the filing transaction (`INSERT … ON CONFLICT DO NOTHING`), so filing is exactly-once per key; a
    reservation survives only for a `dispatched` or `queued` delivery. The owner may delete a row ("clear") to
    let the item file again. Kept 90 days (A5). Bookkeeping, so no entity shape."""

    __tablename__ = TRIGGER_DEDUP_TABLE
    __table_args__ = (
        Index("ix_trigger_dedup_task_id", "task_id", postgresql_where=text("task_id IS NOT NULL")),
    )

    trigger_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_triggers.id", ondelete="CASCADE"), primary_key=True
    )
    dedup_key: Mapped[str] = mapped_column(Text, primary_key=True)
    first_seen_at: Mapped[datetime] = timestamp_column()
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    delivery_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)


class TriggerNotice(UuidPkMixin, OrgScoped, Base):
    """One thing a trigger's owner is told (architecture §3.1/§6): the moment a trigger went Failed
    (`trigger_failed`), or their daily digest (`trigger_digest`). The bell derives its two trigger kinds from
    these rows, as `billing` derives from `billing_notices` — there is no general notifications table (G6).

    `summary` is display data only (names, counts, health text, links), never a secret. A digest carries
    `summary.day` (the UTC date it covers), and `uq_trigger_notices_daily` makes it one per owner, org and day.
    `read_at` is the owner's bell read stamp. Written only by `app/trigger_notices.py`, in the trigger's org.
    Machine-written, so no entity shape."""

    __tablename__ = TRIGGER_NOTICES_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", TRIGGER_NOTICE_KINDS, "ck_trigger_notices_kind"),
        Index("ix_trigger_notices_user_created", "user_id", text("created_at DESC")),
        Index(
            "uq_trigger_notices_daily", "user_id", "org_id", "kind", text("(summary->>'day')"), unique=True
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    trigger_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_triggers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = timestamp_column()
    read_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
