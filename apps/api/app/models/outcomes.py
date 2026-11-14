"""The outcome ledger: one `task_outcomes` row per pipeline dispatch, its per-push heads and its defect links.

`app/outcome_ledger/` is the single home for every rule over these tables: the state machine
(`outcome_ledger/states.py`), the only writer (`outcome_ledger/writer.py`) and the derived dimensions
(`outcome_ledger/dimensions.py`). CP-written system ledgers, so no entity shape; `outcome_links` alone carries the
actor columns, because an operator confirms or dismisses a link.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, Numeric, String, Text, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _in_check, org_unique, OrgScoped, timestamp_column, UuidPkMixin
from app.models.tasks import REVIEW_TIERS as TASK_REVIEW_TIERS

#: `task_outcomes.outcome_state` — the closed vocabulary; the legal moves are `outcome_ledger.states.TRANSITIONS`.
OUTCOME_STATES = (
    "pending", "merged", "deployed", "survived", "reverted", "fixed_forward", "incident_linked", "failed",
    "abandoned", "settled",
)
#: The normalised `review_tier` a settle may declare (`outcome_ledger.dimensions.normalise_review_tier`).
REVIEW_TIERS = (*TASK_REVIEW_TIERS, "unknown")

#: `task_outcome_heads` vocabularies.
#: `lane_verdict`: a head known only from a gate's verdict (the daemon never reported its push).
HEAD_SOURCES = ("runner_push", "lane_resolve", "lane_verdict")
HEAD_VERDICTS = ("pass", "fail", "pending", "unknown")

#: `outcome_links` vocabularies. `incident` is Production QA's (`writer.record_incident_link`).
LINK_KINDS = ("revert", "fixes_ref", "same_files", "caused_by", "incident")
LINK_EVIDENCE_KINDS = ("commit", "pr", "task")
LINK_DEFECT_STATES = ("reverted", "fixed_forward", "incident_linked")
LINK_STATES = ("proposed", "confirmed", "dismissed")


class TaskOutcome(UuidPkMixin, OrgScoped, Base):
    """One dispatch of a pipeline persona: attribution (snapshot at dispatch), timeline, gate facts, cost and the
    CP-only outcome state. NULL in a timeline or fact column means "did not happen / not known", never zero.

    `task_id` is SET NULL: a deleted task's rows are kept, `excluded`, with `task_title` as their label."""

    __tablename__ = "task_outcomes"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("task_id", "dispatch_seq", name="uq_task_outcomes_task_seq"),
        _in_check("outcome_state", OUTCOME_STATES, "ck_task_outcomes_outcome_state"),
        _in_check("review_tier", REVIEW_TIERS, "ck_task_outcomes_review_tier"),
        Index("ix_task_outcomes_org_persona_state", "org_id", "persona", "outcome_state"),
        Index("ix_task_outcomes_project_merged", "project_id", "merged_at"),
        Index("ix_task_outcomes_state_survival", "outcome_state", "survival_ends_at"),
        Index("ix_task_outcomes_task_id", "task_id"),
        Index("ix_task_outcomes_routing_decision_id", "routing_decision_id"),
    )

    # ── Identity ──
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", name="fk_task_outcomes_task_id"), nullable=True
    )
    task_title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # 1-based, monotonic per task, never reset (`tasks.dispatch_attempts` resets on start).
    dispatch_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_session_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL", name="fk_task_outcomes_agent_session_id"),
        nullable=True,
    )
    project_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL", name="fk_task_outcomes_project_id"), nullable=True
    )

    # ── Attribution: snapshot at dispatch, never rewritten (`review_tier` alone is written at settle) ──
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    agent_kind: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    agent_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    agent_commit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_family: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    auth_kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    account_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    review_tier: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    # The `routing_decisions` row the claim finalized for this dispatch (`writer.link_routing_decision`); NULL for a
    # dispatch the router never recorded.
    routing_decision_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    node_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    node_hosting: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    node_isolation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Timeline ──
    task_ready_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    dispatched_at: Mapped[datetime] = timestamp_column()
    first_active_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    pr_opened_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    enqueued_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    merged_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    merge_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pr_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The first production deploy: Production QA's `deployed` fact (`production_qa.ledger` → `writer.record_deployed`).
    deployed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # `merged_at + outcome_survival_days(org)`, fixed at merge.
    survival_ends_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    ended_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)

    # ── Outcome ──
    outcome_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    state_changed_at: Mapped[datetime] = timestamp_column()
    end_kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    end_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    excluded_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: True on the rows of tasks settled before the ledger shipped, written once by a job that has run; no code
    #: writes it now. Readers still treat such a row's null facts as unknown (`outcome_ledger/metrics.py`).
    backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    shadow: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # ── Gate facts ──
    lane_failures: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pushes_after_enqueue: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    nonrunner_commits: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    security_findings: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # `{detecting lane: count}` of the security findings (`review_findings`) against this dispatch; `security_findings`
    # is its sum. One writer: `outcome_ledger.writer.add_findings`. How many settle `findings` entries were malformed.
    security_by_detector: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    findings_malformed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ── Cost (this dispatch only) ──
    tokens_in: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    tokens_out: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    cost_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    node_minutes: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    lane_minutes: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    # ── Complexity ──
    additions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deletions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    files_changed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    language: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    clarity_score: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    complexity_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class TaskOutcomeHead(UuidPkMixin, OrgScoped, Base):
    """One head SHA a runner pushed (or the lane resolved) for a dispatch, with its gate verdict: the single home of
    "lane verdict per push". `pushed_shas` is every commit that push sent (≤ 250) — the runner-commit set."""

    __tablename__ = "task_outcome_heads"
    __table_args__ = (
        UniqueConstraint("task_id", "head_sha", name="uq_task_outcome_heads_task_head"),
        _in_check("source", HEAD_SOURCES, "ck_task_outcome_heads_source"),
        _in_check("verdict", HEAD_VERDICTS, "ck_task_outcome_heads_verdict"),
        Index("ix_task_outcome_heads_outcome_id", "outcome_id"),
    )

    outcome_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_outcomes.id", ondelete="CASCADE", name="fk_task_outcome_heads_outcome_id"),
        nullable=False,
    )
    # The dispatch's task, kept beside `outcome_id` for the `(task_id, head_sha)` key; SET NULL like the outcome's.
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", name="fk_task_outcome_heads_task_id"), nullable=True
    )
    pr_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pushed_shas: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    pushed_at: Mapped[datetime] = timestamp_column()
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="runner_push")
    after_enqueue: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    verdict: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    verdict_source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verdict_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class OutcomeLink(UuidPkMixin, OrgScoped, Base):
    """One piece of defect evidence against a merged dispatch. Only a LIVE link (`confirmed`, or `proposed` with
    `confidence >= 0.5`) moves or blocks the origin's state (`outcome_ledger.writer.apply_link`). The unique key makes
    a detector re-run idempotent and never re-proposes a dismissed link. NULL `created_by_id` = a detector."""

    __tablename__ = "outcome_links"
    __table_args__ = (
        UniqueConstraint("origin_outcome_id", "kind", "evidence_ref", name="uq_outcome_links_origin_kind_ref"),
        _in_check("kind", LINK_KINDS, "ck_outcome_links_kind"),
        _in_check("evidence_kind", LINK_EVIDENCE_KINDS, "ck_outcome_links_evidence_kind"),
        _in_check("defect_state", LINK_DEFECT_STATES, "ck_outcome_links_defect_state"),
        _in_check("state", LINK_STATES, "ck_outcome_links_state"),
    )

    origin_outcome_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_outcomes.id", ondelete="CASCADE", name="fk_outcome_links_origin_id"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    defect_state: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    decided_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_outcome_links_decided_by_id"), nullable=True
    )
    decided_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_outcome_links_created_by_id"), nullable=True
    )
    updated_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_outcome_links_updated_by_id"), nullable=True
    )
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


#: `benchmark_duels` vocabularies (`app/outcome_ledger/benchmark.py` is their one home of rules).
DUEL_STATES = ("running", "reviewing", "decided", "void")
DUEL_GATES = ("pass", "fail", "unknown")
DUEL_SIDES = ("origin", "shadow", "tie")
#: Which side the blind review saw as `A`: the order is drawn at random once and stored, so a retry shows the same.
DUEL_ORDERS = ("origin_first", "shadow_first")


class BenchmarkDuel(UuidPkMixin, OrgScoped, Base):
    """One benchmark duel: a sampled coder task (`origin_task_id`) re-run as a hidden shadow on another allowed
    configuration, judged pairwise from gate pass, a blind review and diff distance (`result`). `config_*` carry
    `{agent_key, tier, key}` — `key` is the ledger configuration key (`outcome_ledger.metrics.config_key`), stamped
    at decision, that the Bradley-Terry ratings are keyed by. `cost_usd` (the shadow's and review's runs) counts
    toward the project's benchmark budget only, never toward the observational metrics."""

    __tablename__ = "benchmark_duels"
    __table_args__ = (
        UniqueConstraint("origin_task_id", name="uq_benchmark_duels_origin_task_id"),
        _in_check("state", DUEL_STATES, "ck_benchmark_duels_state"),
        _in_check("gate_origin", DUEL_GATES, "ck_benchmark_duels_gate_origin"),
        _in_check("gate_shadow", DUEL_GATES, "ck_benchmark_duels_gate_shadow"),
        _in_check("review_verdict", DUEL_SIDES, "ck_benchmark_duels_review_verdict"),
        _in_check("result", DUEL_SIDES, "ck_benchmark_duels_result"),
        _in_check("review_order", DUEL_ORDERS, "ck_benchmark_duels_review_order"),
        Index("ix_benchmark_duels_org_state", "org_id", "state"),
        Index("ix_benchmark_duels_project_created", "project_id", "created_at"),
        Index("ix_benchmark_duels_shadow_task_id", "shadow_task_id"),
        Index("ix_benchmark_duels_review_task_id", "review_task_id"),
    )

    # The sampled task: a duel is about it, so it goes with it (CASCADE).
    origin_task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_benchmark_duels_origin_task_id"), nullable=False
    )
    project_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL", name="fk_benchmark_duels_project_id"), nullable=True
    )
    shadow_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", name="fk_benchmark_duels_shadow_task_id"), nullable=True
    )
    # The newest `duel_review` task (a review, or a close retry).
    review_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", name="fk_benchmark_duels_review_task_id"), nullable=True
    )
    config_origin: Mapped[dict] = mapped_column(JSONB, nullable=False)
    config_shadow: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    void_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gate_origin: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gate_shadow: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    review_order: Mapped[str] = mapped_column(Text, nullable=False)
    review_verdict: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    review_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    diff_distance: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4), nullable=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The origin's merge commit: both sides are diffed against its first parent, which the shadow branches from.
    origin_merge_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    shadow_pr_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shadow_pr_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # How many `duel_review` tasks this duel has filed (a review, then close retries), capped by the engine.
    review_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, server_default=text("0"))
    decided_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)
