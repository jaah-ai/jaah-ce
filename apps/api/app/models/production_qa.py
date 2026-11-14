"""Production QA: deploys, incidents, their links to tasks, and the ledger edges (revision 202611012000).

The rules over these rows are single-homed in the package docstring of `app/production_qa/`; the closed
vocabularies the CHECKs render from live in `app/production_qa/vocabulary.py`. Every table is machine-written
bookkeeping, so none carries the entity shape (`tests/test_entity_shape.py` exempt list).

Tenancy (`app/tenancy/manifest.py`): `branch_pushes`, `deployments`, `incidents`, `production_qa_edges` and
`merge_lane_stage_timings` are `Stored`; the deployment and incident children are `Derived` through their
parent (the org-inherit trigger fills `org_id` from it).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Index, Integer, REAL, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _in_check, org_unique, OrgScoped, timestamp_column
from app.production_qa.vocabulary import (
    COUNT_MODES,
    DEPLOYMENT_STATUSES,
    EDGE_KINDS,
    INCIDENT_EVENT_KINDS,
    INCIDENT_STATES,
    INCIDENT_TASK_ROLES,
    INCIDENT_TYPES,
    METRICS,
    PR_SOURCES,
    RANGE_STATUSES,
    REGRESSION_STATUSES,
    SEVERITIES,
    STAGE_OUTCOMES,
    SUSPECT_BASES,
    SUSPECT_CONFIDENCES,
    SUSPECT_STATES,
)


def _project_fk(table: str) -> ForeignKey:
    return ForeignKey("projects.id", ondelete="CASCADE", name=f"fk_{table}_project_id")


class BranchPush(OrgScoped, Base):
    """One `push` webhook event on a deploy-watched repo: the ledger a deploy's commit range is walked from.
    Two hooks on one repo collapse onto the unique key. `commit_shas` holds at most 2048 (GitHub's cap);
    `truncated` says the payload held more."""

    __tablename__ = "branch_pushes"
    __table_args__ = (
        org_unique(__tablename__),
        Index(
            "uq_branch_pushes_push", "project_id", "repo_path", "branch", "before_sha", "after_sha", unique=True,
        ),
        Index("ix_branch_pushes_after", "project_id", "repo_path", "branch", "after_sha"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    before_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    after_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    forced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    commit_shas: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # The `X-GitHub-Delivery` GUID; the only home of a forge delivery id in this package.
    gh_delivery: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pushed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    received_at: Mapped[datetime] = timestamp_column()


class Deployment(OrgScoped, Base):
    """One workflow run attempt of a project's deploy workflow (`projects.deploy_workflows`).

    `shipped`: the attempt received a successful `deployment_status` for an environment. `is_anchor`: shipped
    to the production environment from a `push` run — anchors carry the range, the `deployed` edges, the
    regression window and the probes. `reconciled_hash` is the inputs hash the last reconcile ran over."""

    __tablename__ = "deployments"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("status", DEPLOYMENT_STATUSES, "ck_deployments_status"),
        _in_check("range_status", RANGE_STATUSES, "ck_deployments_range_status"),
        _in_check("regression_status", REGRESSION_STATUSES, "ck_deployments_regression_status"),
        Index("uq_deployments_run", "project_id", "repo_path", "run_id", "run_attempt", unique=True),
        # The list route's keyset: newest first within the projects the caller may view.
        Index("ix_deployments_project_id_id", "project_id", "id"),
        Index(
            "ix_deployments_regression_due", "regression_due_at",
            postgresql_where=text("regression_status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    forge: Mapped[str] = mapped_column(Text, nullable=False, server_default="github")
    workflow_path: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    run_event: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    run_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    head_branch: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    # `unknown` when no environment is known yet.
    environment: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    is_production: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    shipped: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_anchor: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    conclusion_raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failing_job: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failing_step: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    range_from_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    range_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    range_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    finished_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    first_received_at: Mapped[datetime] = timestamp_column()
    last_received_at: Mapped[datetime] = timestamp_column()
    reconciled_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    regression_due_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    regression_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    regression_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The release this anchor made live (`app/releases/`), written by the ledger's anchoring; NULL otherwise.
    release_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("releases.id", ondelete="SET NULL", name="fk_deployments_release_id"), nullable=True
    )


def _deployment_fk(table: str) -> ForeignKey:
    return ForeignKey("deployments.id", ondelete="CASCADE", name=f"fk_{table}_deployment_id")


class DeploymentEnvironment(OrgScoped, Base):
    """One environment a run attempt reported a `deployment_status` for; `state` is the latest one sent."""

    __tablename__ = "deployment_environments"

    deployment_id: Mapped[int] = mapped_column(BigInteger, _deployment_fk(__tablename__), primary_key=True)
    environment: Mapped[str] = mapped_column(Text, primary_key=True)
    job_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = timestamp_column()


class DeploymentCommit(OrgScoped, Base):
    """One commit in an anchor's range; indexed by SHA for release containment."""

    __tablename__ = "deployment_commits"
    __table_args__ = (Index("ix_deployment_commits_sha", "sha"),)

    deployment_id: Mapped[int] = mapped_column(BigInteger, _deployment_fk(__tablename__), primary_key=True)
    sha: Mapped[str] = mapped_column(String(40), primary_key=True)


class DeploymentPr(OrgScoped, Base):
    """One PR whose merge SHA is in an anchor's range; `task_id` NULL for a PR no task delivered."""

    __tablename__ = "deployment_prs"
    __table_args__ = (_in_check("source", PR_SOURCES, "ck_deployment_prs_source"),)

    deployment_id: Mapped[int] = mapped_column(BigInteger, _deployment_fk(__tablename__), primary_key=True)
    repo_path: Mapped[str] = mapped_column(Text, primary_key=True)
    pr_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL", name="fk_deployment_prs_task_id"), nullable=True
    )
    merge_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)


class DeploymentMetric(OrgScoped, Base):
    """One regression measurement of one metric for one deploy, breached or not; `note` says why a value is
    missing."""

    __tablename__ = "deployment_metrics"
    __table_args__ = (
        _in_check("metric", METRICS, "ck_deployment_metrics_metric"),
        Index("ix_deployment_metrics_deployment_id", "deployment_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    deployment_id: Mapped[int] = mapped_column(BigInteger, _deployment_fk(__tablename__), nullable=False)
    # The monitor trigger whose `metrics` config was read.
    trigger_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("task_triggers.id", ondelete="SET NULL", name="fk_deployment_metrics_trigger_id"),
        nullable=True,
    )
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    baseline: Mapped[Optional[float]] = mapped_column(REAL, nullable=True)
    post: Mapped[Optional[float]] = mapped_column(REAL, nullable=True)
    delta_abs: Mapped[Optional[float]] = mapped_column(REAL, nullable=True)
    delta_pct: Mapped[Optional[float]] = mapped_column(REAL, nullable=True)
    events_post: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    window_min: Mapped[int] = mapped_column(Integer, nullable=False)
    breached: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    measured_at: Mapped[datetime] = timestamp_column()


class Incident(OrgScoped, Base):
    """One production problem of one project, keyed by its fingerprint: a monitor alert, a failed deploy, a
    failed probe, a regression or a merge-lane slowdown. Occurrences fold onto the row; `last_raised_at` throttles
    the task-raising events an occurrence may send."""

    __tablename__ = "incidents"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("type", INCIDENT_TYPES, "ck_incidents_type"),
        _in_check("severity", SEVERITIES, "ck_incidents_severity"),
        _in_check("state", INCIDENT_STATES, "ck_incidents_state"),
        _in_check("count_mode", COUNT_MODES, "ck_incidents_count_mode"),
        Index("uq_incidents_fingerprint", "project_id", "fingerprint", unique=True),
        # The list route's keyset: newest first within the projects the caller may view.
        Index("ix_incidents_project_id_id", "project_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    # The trigger source kind it arrived through (`sentry`, `github`, …) or the internal origin.
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("task_triggers.id", ondelete="SET NULL", name="fk_incidents_trigger_id"),
        nullable=True,
    )
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    first_seen: Mapped[datetime] = timestamp_column(server_now=True)
    last_seen: Mapped[datetime] = timestamp_column(server_now=True)
    last_transition_at: Mapped[datetime] = timestamp_column(server_now=True)
    last_raised_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    count_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="increment")
    release: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    commit_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # At most 50 `{path, function, lineno, in_app}`.
    stack_frames: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    vendor_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vendor_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    deployment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("deployments.id", ondelete="SET NULL", name="fk_incidents_deployment_id"),
        nullable=True,
    )
    resolved_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


def _incident_fk(table: str) -> ForeignKey:
    return ForeignKey("incidents.id", ondelete="CASCADE", name=f"fk_{table}_incident_id")


class IncidentEvent(OrgScoped, Base):
    """One transition of an incident, in order — its History tab."""

    __tablename__ = "incident_events"
    __table_args__ = (
        _in_check("kind", INCIDENT_EVENT_KINDS, "ck_incident_events_kind"),
        Index("ix_incident_events_incident_id", "incident_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, _incident_fk(__tablename__), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = timestamp_column()
    delivery_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("trigger_deliveries.id", ondelete="SET NULL", name="fk_incident_events_delivery_id"),
        nullable=True,
    )
    actor_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_incident_events_actor_id"),
        nullable=True,
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class IncidentSuspect(OrgScoped, Base):
    """One PR suspected of causing an incident, with its basis and confidence; an operator's decision
    (`state`, `decided_by`) is never overwritten by a recompute."""

    __tablename__ = "incident_suspects"
    __table_args__ = (
        _in_check("basis", SUSPECT_BASES, "ck_incident_suspects_basis"),
        _in_check("confidence", SUSPECT_CONFIDENCES, "ck_incident_suspects_confidence"),
        _in_check("state", SUSPECT_STATES, "ck_incident_suspects_state"),
        Index("uq_incident_suspects_pr", "incident_id", "repo_path", "pr_number", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, _incident_fk(__tablename__), nullable=False)
    deployment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("deployments.id", ondelete="SET NULL", name="fk_incident_suspects_deployment_id"),
        nullable=True,
    )
    task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL", name="fk_incident_suspects_task_id"), nullable=True
    )
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    pr_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(REAL, nullable=False, server_default="0")
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="suggested")
    decided_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_incident_suspects_decided_by"),
        nullable=True,
    )
    decided_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class IncidentTask(OrgScoped, Base):
    """A task filed for an incident, and the dedup claim behind it: at most one unreleased non-revert claim per
    incident (`uq_incident_tasks_open_claim`). `claimed_at` is part of the key, so a released claim stays as
    history beside the next one."""

    __tablename__ = "incident_tasks"
    __table_args__ = (
        _in_check("role", INCIDENT_TASK_ROLES, "ck_incident_tasks_role"),
        Index(
            "uq_incident_tasks_open_claim", "incident_id", unique=True,
            postgresql_where=text("role <> 'revert' AND released_at IS NULL"),
        ),
        Index("ix_incident_tasks_task_id", "task_id", postgresql_where=text("task_id IS NOT NULL")),
    )

    incident_id: Mapped[int] = mapped_column(BigInteger, _incident_fk(__tablename__), primary_key=True)
    role: Mapped[str] = mapped_column(Text, primary_key=True)
    # Not `timestamp_column` (it takes no `primary_key`). `clock_timestamp()`, not `now()`: a claim released and
    # retaken inside one transaction must not repeat the key.
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=text("clock_timestamp()")
    )
    task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL", name="fk_incident_tasks_task_id"), nullable=True
    )
    delivery_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("trigger_deliveries.id", ondelete="SET NULL", name="fk_incident_tasks_delivery_id"),
        nullable=True,
    )
    released_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class ProductionQaEdge(OrgScoped, Base):
    """One fact the outcome ledger reads about a task: it was deployed, linked to an incident, or resolved one.
    Append-only; a dismissal sets `revoked_at`; the ledger applier stamps `applied_at`."""

    __tablename__ = "production_qa_edges"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", EDGE_KINDS, "ck_production_qa_edges_kind"),
        Index(
            "uq_production_qa_edges_fact", "task_id", "kind",
            text("coalesce(deployment_id, 0)"), text("coalesce(incident_id, 0)"), unique=True,
        ),
        Index("ix_production_qa_edges_deployment_id", "deployment_id", postgresql_where=text("deployment_id IS NOT NULL")),
        Index("ix_production_qa_edges_incident_id", "incident_id", postgresql_where=text("incident_id IS NOT NULL")),
        Index("ix_production_qa_edges_unapplied", "id", postgresql_where=text("applied_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE", name="fk_production_qa_edges_task_id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("deployments.id", ondelete="CASCADE", name="fk_production_qa_edges_deployment_id"),
        nullable=True,
    )
    incident_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE", name="fk_production_qa_edges_incident_id"),
        nullable=True,
    )
    occurred_at: Mapped[datetime] = timestamp_column()
    revoked_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    applied_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class MergeLaneStageTiming(OrgScoped, Base):
    """How long one gate stage of one merge-lane run took, and how it ended."""

    __tablename__ = "merge_lane_stage_timings"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("outcome", STAGE_OUTCOMES, "ck_merge_lane_stage_timings_outcome"),
        Index("ix_merge_lane_stage_timings_stage", "project_id", "stage", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    lane_task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL", name="fk_merge_lane_stage_timings_lane_task_id"),
        nullable=True,
    )
    tree_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pr_numbers: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{}'::integer[]")
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    seconds: Mapped[float] = mapped_column(REAL, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = timestamp_column()
