"""Releases: a project's stages and freezes, and each request to move one stage's head into the next (revision
202611102100). Every rule over these rows lives in `app/releases/`; the state vocabulary in
`app/releases/lifecycle.py`.

`release_stages` and `release_freezes` are entities a `projects:edit` holder edits (`PUT …/pipeline`, `…/release/freezes`);
the rest are machine-written bookkeeping (`tests/test_entity_shape.py` exempt list). A release holds the request's
intent and links the ledger's `deployments.id`; it never copies a deploy fact.

Tenancy (`app/tenancy/manifest.py`): stages, freezes, releases, jobs are `Stored` with their project held same-org;
approvals and changes are `Derived` through their release, contained shas through their stage.
"""
from __future__ import annotations

from datetime import datetime
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
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, OrgScoped, UuidPkMixin, _in_check, org_unique, timestamp_column
from app.releases.lifecycle import KINDS, RUNNING, STATES

#: `release_stages.url_source`: where the stage's URL came from.
URL_SOURCES = ("detected", "manual", "easy_mode", "argocd", "webhook", "script")
#: `release_stages.target_kind`: how a change reaches the stage (`app/pipeline_gates/schema.py` validates its config).
TARGET_KINDS = ("none", "forge_env", "argocd", "webhook", "script")
#: `release_jobs.mode`: the executor's work that is not a release. `connect` is retired (Connect deploys installs
#: GitHub webhooks itself, `production_qa/github_hooks.py`) and kept for the CHECK constraint and its old rows only.
JOB_MODES = ("survey", "connect")
APPROVALS_MAX = 5
#: `release_stages.level`, derived on save from the branch the owner typed (`app/releases/stages.py::level_for`):
#: empty → `none`, a pattern with `*` → `work`, a branch name → `branch`. Never shown or set as an option.
LEVELS = ("none", "work", "branch")
#: `pipeline_approvals.kind`: a press of the step's verb, or an approval.
PIPELINE_APPROVAL_KINDS = ("press", "approve")
#: `release_stages.on_timeout`: what an approval timeout does (`app/pipeline/rules.py`).
ON_TIMEOUT = ("cancel", "remind")
#: `release_stages.concurrency`: how a release step treats a second request (read only for release steps).
CONCURRENCY = ("newest", "in_order")
#: `release_stages.approval_timeout_minutes` / `soak_minutes` upper bound: 30 days.
MINUTES_MAX = 43200
#: The shortest approval timeout.
TIMEOUT_MIN = 15
INSTRUCTIONS_MAX = 1000
COMMENT_MAX = 500
#: `release_freezes.repeat` and `.scope` (`app/releases/freezes.py`).
FREEZE_REPEATS = ("never", "daily", "weekly", "monthly")
FREEZE_SCOPES = ("after_integration", "stages")
#: `pipeline_guardrails.rule`: the three built-in org rules on the step into production (`app/releases/guardrails.py`).
GUARDRAIL_RULES = ("production_approval", "no_auto_production", "production_maintainers")
#: `pipeline_guardrails.mode`: off, a warning on save, or a refused save.
GUARDRAIL_MODES = ("off", "warn", "block")


def _project_fk(table: str) -> ForeignKey:
    return ForeignKey("projects.id", ondelete="CASCADE", name=f"fk_{table}_project_id")


class ReleaseStage(EntityMixin, OrgScoped, Base):
    """One stage of a project's pipeline, in order (Ideas → Building → Dev → Production by default). Every stage has
    the same fields; its `level` is derived from its branch, and the columns `auto_deploy`, `approvals_required` and
    `deployers` are the settings of the step INTO it. Rules: `app/releases/stages.py`."""

    __tablename__ = "release_stages"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("project_id", "position", name="uq_release_stages_project_position"),
        Index("uq_release_stages_project_name", "project_id", text("lower(name)"), unique=True),
        CheckConstraint(
            f"approvals_required BETWEEN 0 AND {APPROVALS_MAX}", name="ck_release_stages_approvals_required"
        ),
        CheckConstraint("position >= 0", name="ck_release_stages_position"),
        _in_check("level", LEVELS, "ck_release_stages_level"),
        CheckConstraint(
            f"approval_timeout_minutes IS NULL OR approval_timeout_minutes BETWEEN {TIMEOUT_MIN} AND {MINUTES_MAX}",
            name="ck_release_stages_approval_timeout",
        ),
        _in_check("on_timeout", ON_TIMEOUT, "ck_release_stages_on_timeout"),
        CheckConstraint(f"char_length(approver_instructions) <= {INSTRUCTIONS_MAX}",
                        name="ck_release_stages_approver_instructions"),
        CheckConstraint(f"soak_minutes BETWEEN 0 AND {MINUTES_MAX}", name="ck_release_stages_soak_minutes"),
        _in_check("concurrency", CONCURRENCY, "ck_release_stages_concurrency"),
        CheckConstraint(
            "url_source IS NULL OR url_source IN (" + ", ".join(f"'{v}'" for v in URL_SOURCES) + ")",
            name="ck_release_stages_url_source",
        ),
        _in_check("target_kind", TARGET_KINDS, "ck_release_stages_target_kind"),
    )

    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL on the first `branch`-level stage (its branch is `projects.integration_branch`) and on the `work` stage
    # (`projects.branch_prefix` + `*`) and on a `none` stage (no branch); every other stage stores its own.
    branch: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    level: Mapped[str] = mapped_column(Text, nullable=False, server_default="branch")
    # The forge deployment-environment name the ledger matches; NULL = learn it from the first anchored deploy.
    deploy_environment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url_source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The linked template's stage this stage is (`app/releases/linking.py`): a renamed or moved template stage keeps
    # this stage, so its tasks stay put. NULL = never written from a template.
    template_stage_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    auto_deploy: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    approvals_required: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # NULL = the step's default (`app/releases/access.py`); else `{"roles": [slug], "users": [uuid]}`.
    deployers: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # The step's rules, read only through `app/pipeline/rules.py::StepRules` (every default is today's behaviour).
    # NULL = same as who may press; else `{"roles": [slug], "users": [uuid]}`, as `deployers`.
    approvers: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    requester_cant_approve: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"),
                                                         default=True)
    approval_timeout_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    on_timeout: Mapped[str] = mapped_column(Text, nullable=False, server_default="cancel", default="cancel")
    approver_instructions: Mapped[str] = mapped_column(Text, nullable=False, server_default="", default="")
    soak_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    # NULL = any time; else `{"days": [0..6] (0 = Monday), "from": "HH:MM", "to": "HH:MM", "timezone": IANA}`.
    allowed_hours: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    concurrency: Mapped[str] = mapped_column(Text, nullable=False, server_default="newest", default="newest")
    # A person's Resume of a step paused by itself (runtime state; a save never writes it).
    resumed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The deploy target (`app/pipeline_gates/schema.py`): `forge_env` reads `deploy_environment` above, never a copy.
    target_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")
    target_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # A credential scope path (`org/<name>`, `projects/<p>/<name>`), never a value.
    target_credential: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When this stage's gate lists were first written (`app/pipeline_gates/defaults.py::ensure`). NULL: none stored
    # yet, so every reader shows the defaults (`defaults_for`) — a row an older release wrote is never gate-less.
    gates_seeded_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class ReleaseFreeze(EntityMixin, OrgScoped, Base):
    """A period no release may start in, on the stages of its `scope` (`after_integration`: every stage after the
    integration stage; `stages`: its `release_freeze_stages`). `repeat` repeats the window's wall-clock times every
    day, week or month in `timezone` (IANA); `weekly` is its old spelling, still written as `repeat = 'weekly'` for a
    previous release reading it. Rules: `app/releases/freezes.py`, judged by `app/releases/service.py::freeze_on`."""

    __tablename__ = "release_freezes"
    __table_args__ = (
        org_unique(__tablename__),
        CheckConstraint("ends_at > starts_at", name="ck_release_freezes_window"),
        Index("ix_release_freezes_project_id", "project_id"),
        _in_check("repeat", FREEZE_REPEATS, "ck_release_freezes_repeat"),
        _in_check("scope", FREEZE_SCOPES, "ck_release_freezes_scope"),
    )

    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    starts_at: Mapped[datetime] = timestamp_column(server_now=False)
    ends_at: Mapped[datetime] = timestamp_column(server_now=False)
    weekly: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    repeat: Mapped[str] = mapped_column(Text, nullable=False, server_default="never")
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="after_integration")
    # A step a person pressed runs during the freeze (auto steps and schedules still wait).
    let_pressed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # A release already queued when the occurrence began continues.
    let_queued: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class ReleaseFreezeStage(OrgScoped, Base):
    """One stage a `scope = 'stages'` freeze covers; a removed stage or freeze takes its rows with it."""

    __tablename__ = "release_freeze_stages"
    __table_args__ = (Index("ix_release_freeze_stages_stage_id", "stage_id"),)

    freeze_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("release_freezes.id", ondelete="CASCADE", name="fk_release_freeze_stages_freeze_id"),
        primary_key=True,
    )
    stage_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("release_stages.id", ondelete="CASCADE", name="fk_release_freeze_stages_stage_id"),
        primary_key=True,
    )


class Release(OrgScoped, Base):
    """One request to move `from_stage`'s head into `to_stage`, and where it is (`app/releases/lifecycle.py`)."""

    __tablename__ = "releases"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", KINDS, "ck_releases_kind"),
        _in_check("state", STATES, "ck_releases_state"),
        # Several releases may wait for one target stage, but never two merge or deploy into it at once
        # (`app/releases/service.py::_request` serialises requests per target with a row lock).
        Index(
            "uq_releases_running", "project_id", "to_stage_id", unique=True,
            postgresql_where=text("state IN (" + ", ".join(f"'{s}'" for s in RUNNING) + ")"),
        ),
        Index("ix_releases_project_id_id", "project_id", "id"),
        Index("ix_releases_task_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="promote")
    # SET NULL keeps a release's history past its stage's removal (a stage with an open release cannot be removed).
    from_stage_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("release_stages.id", ondelete="SET NULL", name="fk_releases_from_stage_id"),
        nullable=True,
    )
    to_stage_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("release_stages.id", ondelete="SET NULL", name="fk_releases_to_stage_id"),
        nullable=True,
    )
    # Set by the executor's first event (the pinned head), never by the CP.
    head_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_releases_requested_by_id"),
        nullable=True,
    )
    requested_at: Mapped[datetime] = timestamp_column()
    scheduled_for: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The hidden `release` executor task that ran it.
    task_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL", name="fk_releases_task_id"), nullable=True
    )
    pr_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    merge_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # What people call it (`runs.stamp_version`): the project's own version tag on the pinned head, else `#N`, the
    # project's Nth release into this stage. NULL until the executor pins it.
    version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The ledger anchor that made it live.
    deployment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("deployments.id", ondelete="SET NULL", name="fk_releases_deployment_id"),
        nullable=True,
    )
    # `{kind, message, action}`; `action` one of `lifecycle.FAILURE_ACTIONS`.
    failure: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # The step's settings at request time (`app/releases/snapshot.py`): `{from_branch, to_branch, deploy_environment,
    # to_is_production, approvals_required, deployers, auto}`. NULL on a release requested before the column.
    settings: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # A `rollback`'s release whose arrival it undoes; a later promote that re-applies a rolled-back change sets
    # `reapplied_by_id` on the rollback (architecture §4.4).
    rolls_back_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("releases.id", ondelete="SET NULL", name="fk_releases_rolls_back_id"), nullable=True,
    )
    reapplied_by_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("releases.id", ondelete="SET NULL", name="fk_releases_reapplied_by_id"), nullable=True,
    )
    # What a person typed when pressing the step (`{"ticket": "CHG…"}`); NULL when nothing was asked.
    inputs: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)
    # When it arrived on its target (merged → shipped, or anchored live; `service._move`). NULL on one that never
    # did, or that arrived before the column and was not backfilled.
    arrived_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # When it last entered `queued` (`service._move`, and a request filed straight into it): a freeze's
    # "let a running step finish" compares it with the occurrence's start.
    queued_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Why it was canceled: `{kind: timeout|rejected|superseded|manual, by_id?, by_release_id?, comment?}`.
    cancellation: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # `{by_id, at, reason, freeze_id, freeze_name}`: this one release passes any freeze.
    freeze_override: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class ReleaseApproval(OrgScoped, Base):
    """One person's approval of one release; the requester never holds one (`app/releases/access.py`)."""

    __tablename__ = "release_approvals"
    __table_args__ = (
        UniqueConstraint("release_id", "user_id", name="uq_release_approvals_release_user"),
        CheckConstraint(f"comment IS NULL OR char_length(comment) <= {COMMENT_MAX}",
                        name="ck_release_approvals_comment"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    release_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("releases.id", ondelete="CASCADE", name="fk_release_approvals_release_id"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="CASCADE", name="fk_release_approvals_user_id"),
        nullable=False,
    )
    approved_at: Mapped[datetime] = timestamp_column()
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ReleaseChange(OrgScoped, Base):
    """One change a release carries, by reference only: title, author and PR are read from the owner row.

    Written with exactly one owner reference. Deleting the owner nulls it (SET NULL), so the check admits at most
    one: the row keeps `merge_sha`, the release's record of what it shipped (`waiting.arrived_shas`)."""

    __tablename__ = "release_changes"
    __table_args__ = (
        CheckConstraint(
            "task_outcome_id IS NULL OR merge_lane_row_id IS NULL", name="ck_release_changes_one_ref"
        ),
        Index("ix_release_changes_release_id", "release_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    release_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("releases.id", ondelete="CASCADE", name="fk_release_changes_release_id"),
        nullable=False,
    )
    merge_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    task_outcome_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("task_outcomes.id", ondelete="SET NULL", name="fk_release_changes_task_outcome_id"),
        nullable=True,
    )
    merge_lane_row_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merge_lane_rows.id", ondelete="SET NULL", name="fk_release_changes_merge_lane_row_id"),
        nullable=True,
    )


class ReleaseJob(UuidPkMixin, OrgScoped, Base):
    """The executor's work that is not a release (`survey`, `connect`); at most one open per (project, mode)."""

    __tablename__ = "release_jobs"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("mode", JOB_MODES, "ck_release_jobs_mode"),
        Index(
            "uq_release_jobs_open", "project_id", "mode", unique=True, postgresql_where=text("done_at IS NULL"),
        ),
    )

    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_release_jobs_requested_by_id"),
        nullable=True,
    )
    requested_at: Mapped[datetime] = timestamp_column()
    claimed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    done_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


class ReleaseContained(OrgScoped, Base):
    """A change a node observed on a stage's branch (the executor's `survey`): the CP's only git-derived fact."""

    __tablename__ = "release_contained"

    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), primary_key=True)
    stage_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("release_stages.id", ondelete="CASCADE", name="fk_release_contained_stage_id"),
        primary_key=True,
    )
    merge_sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    observed_at: Mapped[datetime] = timestamp_column()


#: `release_notices.kind`: the `deploys` notices of `app/releases/notify.py`.
NOTICE_KINDS = ("release_approval", "release_live", "release_failed", "release_rolled_back", "release_approval_timeout",
                "release_approval_reminder", "release_rejected")


class ReleaseNotice(UuidPkMixin, OrgScoped, Base):
    """One deploy notice in one member's bell (`routes/inbox.py`'s `release_*` kinds), written by
    `app/releases/notify.py` in the project's org beside the external channels, whatever they did.

    `summary` is display data only (`{text, href}`), never a secret. `subject_key` is the notice's dedupe text (what
    it is about and the period or request it belongs to): unique per person and kind, so a retried tick writes no
    second row. `read_at` is the bell's read stamp. Machine-written, so no entity shape."""

    __tablename__ = "release_notices"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", NOTICE_KINDS, "ck_release_notices_kind"),
        Index("ix_release_notices_user_created", "user_id", text("created_at DESC")),
        UniqueConstraint("user_id", "kind", "subject_key", name="uq_release_notices_subject"),
        Index("ix_release_notices_project_id", "project_id"),
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE", name="fk_release_notices_user_id"),
        nullable=False,
    )
    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_key: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = timestamp_column()
    read_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class PipelinePass(UuidPkMixin, OrgScoped, Base):
    """One item moved into a no-branch stage by its step: a task, or a merged change by its sha (exactly one)."""

    __tablename__ = "pipeline_passes"
    __table_args__ = (
        CheckConstraint("(task_id IS NULL) <> (merge_sha IS NULL)", name="ck_pipeline_passes_one_subject"),
        Index("ix_pipeline_passes_stage_id", "stage_id"),
        Index("ix_pipeline_passes_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    stage_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("release_stages.id", ondelete="CASCADE", name="fk_pipeline_passes_stage_id"),
        nullable=False,
    )
    task_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE", name="fk_pipeline_passes_task_id"), nullable=True,
    )
    merge_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_pipeline_passes_created_by_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = timestamp_column()


class PipelineApproval(UuidPkMixin, OrgScoped, Base):
    """One person's press of a step's verb, or approval of it, for one item (a task or a merged change)."""

    __tablename__ = "pipeline_approvals"
    __table_args__ = (
        _in_check("kind", PIPELINE_APPROVAL_KINDS, "ck_pipeline_approvals_kind"),
        UniqueConstraint("stage_id", "task_id", "merge_sha", "user_id", "kind", name="uq_pipeline_approvals_subject"),
        Index("ix_pipeline_approvals_project_id", "project_id"),
        Index("ix_pipeline_approvals_project_kind", "project_id", "kind"),
        CheckConstraint(f"comment IS NULL OR char_length(comment) <= {COMMENT_MAX}",
                        name="ck_pipeline_approvals_comment"),
    )

    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    stage_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("release_stages.id", ondelete="CASCADE", name="fk_pipeline_approvals_stage_id"),
        nullable=False,
    )
    task_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE", name="fk_pipeline_approvals_task_id"), nullable=True,
    )
    merge_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="CASCADE", name="fk_pipeline_approvals_user_id"),
        nullable=False,
    )
    created_at: Mapped[datetime] = timestamp_column()
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # On a press: `{by_id, at, reason, freeze_id, freeze_name}`, that one request's freeze override.
    freeze_override: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # On the first press: when the approvers were last reminded ("keep waiting and remind").
    reminded_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


#: `pipeline_revisions.kind`: a save of Project settings › Pipeline (stages, steps or freezes), or a Restore.
REVISION_KINDS = ("save", "restore")


class PipelineRevision(OrgScoped, Base):
    """One save of a project's pipeline and freezes (`app/releases/history.py`): who, when, and the whole setup
    before and after (`{stages, steps, freezes}`), so a diff reads the pair and Restore writes `after` back.
    Machine-written by the save itself (append-only), so no entity shape."""

    __tablename__ = "pipeline_revisions"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", REVISION_KINDS, "ck_pipeline_revisions_kind"),
        Index("ix_pipeline_revisions_project_id_id", "project_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="save")
    # A restore's source revision, by id: the same project's row (rows go only with their project).
    restored_from_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_pipeline_revisions_created_by_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = timestamp_column()
    before: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class PipelineGuardrail(EntityMixin, OrgScoped, Base):
    """One org's setting for one built-in pipeline rule (revision 202611130300): its mode and, for
    `production_approval`, the approvals it asks for. Every org holds all three rows, written Off on first read
    (`app/releases/guardrails.py::for_org`), so a mode change's who and when is the row's stamp. `name` is the rule's
    title as written then; the rule's meaning is `rule`'s."""

    __tablename__ = "pipeline_guardrails"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "rule", name="uq_pipeline_guardrails_org_rule"),
        _in_check("rule", GUARDRAIL_RULES, "ck_pipeline_guardrails_rule"),
        _in_check("mode", GUARDRAIL_MODES, "ck_pipeline_guardrails_mode"),
        CheckConstraint(
            f"approvals_min IS NULL OR approvals_min BETWEEN 1 AND {APPROVALS_MAX}",
            name="ck_pipeline_guardrails_approvals_min",
        ),
    )

    rule: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="off")
    approvals_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class PipelineTemplate(EntityMixin, OrgScoped, Base):
    """One org pipeline template (revision 202611130500; rules in `app/releases/templates.py`): an editable `draft`
    (a `app/releases/pipeline_doc.py` document, with lock marks) and, once published, the version linked projects
    follow (`published_version_id`). Never published: NULL. Saving the draft never touches a project; only a
    publish makes a new version."""

    __tablename__ = "pipeline_templates"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_pipeline_templates_org_slug"),
        Index("uq_pipeline_templates_org_name", "org_id", text("lower(name)"), unique=True),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    draft: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_version_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("pipeline_template_versions.id", ondelete="SET NULL", use_alter=True,
                   name="fk_pipeline_templates_published_version_id"),
        nullable=True,
    )


class PipelineTemplateVersion(UuidPkMixin, OrgScoped, Base):
    """One published version of a template: its document as published, never edited (a publish adds the next
    `number`). Written by the publish alone (append-only), so no entity shape: who and when are its own columns."""

    __tablename__ = "pipeline_template_versions"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("template_id", "number", name="uq_pipeline_template_versions_template_number"),
    )

    template_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("pipeline_templates.id", ondelete="CASCADE", name="fk_pipeline_template_versions_template_id"),
        nullable=False,
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_pipeline_template_versions_created_by_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = timestamp_column()


# ── Pipeline gates, run bookkeeping, arrivals, health (revision 202611131300; rules: `app/pipeline_gates/`) ────

#: `pipeline_gates.phase`: `before` = the step INTO the stage; `after` = verify once a change arrives on it.
GATE_PHASES = ("before", "after")
#: `pipeline_gates.kind`; a ticket's system (Jira or ServiceNow) is `config.system`.
GATE_KINDS = ("command", "ci", "webhook", "probe", "alerts", "ticket")
GATE_TIMEOUT_MIN, GATE_TIMEOUT_MAX = 60, 21600
GATE_RETRIES_MAX = 3
#: `gate_runs.subject_kind`: what a run gated.
GATE_RUN_SUBJECTS = ("lane_batch", "release", "arrival")
GATE_RUN_STATES = ("running", "passed", "failed", "canceled")
GATE_RESULT_STATES = ("waiting", "running", "passed", "failed", "timed_out", "off", "skipped")
#: The longest report a gate result keeps (plain text).
GATE_REPORT_MAX = 1000
ARRIVAL_STATES = ("deploying", "arrived", "deploy_failed", "verifying", "verified", "verify_failed", "superseded",
                  "rolled_back")
#: The arrival states still moving: at most one per stage.
ARRIVAL_OPEN = ("deploying", "verifying")
HEALTH_STATES = ("healthy", "down", "unchecked")


def _stage_fk(table: str, ondelete: str = "CASCADE") -> ForeignKey:
    return ForeignKey("release_stages.id", ondelete=ondelete, name=f"fk_{table}_stage_id")


class PipelineGate(EntityMixin, OrgScoped, Base):
    """One gate in a stage's list: `before` (the step into the stage) or `after` (verify on arrival), in `position`
    order. `config` is per `kind` (`app/pipeline_gates/schema.py`); `credential` a scope path, never a value."""

    __tablename__ = "pipeline_gates"
    __table_args__ = (
        org_unique(__tablename__),
        # Deferred: a reorder rewrites every position in one save.
        UniqueConstraint("stage_id", "phase", "position", name="uq_pipeline_gates_stage_phase_position",
                         deferrable=True, initially="DEFERRED"),
        _in_check("phase", GATE_PHASES, "ck_pipeline_gates_phase"),
        _in_check("kind", GATE_KINDS, "ck_pipeline_gates_kind"),
        CheckConstraint("position >= 0", name="ck_pipeline_gates_position"),
        CheckConstraint(f"timeout_s BETWEEN {GATE_TIMEOUT_MIN} AND {GATE_TIMEOUT_MAX}",
                        name="ck_pipeline_gates_timeout_s"),
        CheckConstraint(f"retries BETWEEN 0 AND {GATE_RETRIES_MAX}", name="ck_pipeline_gates_retries"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="ck_pipeline_gates_name"),
        Index("ix_pipeline_gates_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    stage_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), _stage_fk(__tablename__), nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    credential: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class GateRun(UuidPkMixin, OrgScoped, Base):
    """One run of one gate list on one subject (a lane batch, a release, an arrival), with the list it ran frozen in
    `gates`. Machine-written by the node driver's events (`app/pipeline_gates/results.py`)."""

    __tablename__ = "gate_runs"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("phase", GATE_PHASES, "ck_gate_runs_phase"),
        _in_check("subject_kind", GATE_RUN_SUBJECTS, "ck_gate_runs_subject_kind"),
        _in_check("state", GATE_RUN_STATES, "ck_gate_runs_state"),
        Index("ix_gate_runs_project_subject", "project_id", "subject_kind", "subject_id"),
    )

    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    stage_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), _stage_fk(__tablename__, "SET NULL"), nullable=True)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    subject_kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    gates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    finished_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class GateResult(OrgScoped, Base):
    """One gate's result in one run, by its position in the frozen list. `gate_id` has no key: the gate may be
    deleted since. `report` is plain text; `link` https only."""

    __tablename__ = "gate_results"
    __table_args__ = (
        _in_check("state", GATE_RESULT_STATES, "ck_gate_results_state"),
        CheckConstraint(f"report IS NULL OR char_length(report) <= {GATE_REPORT_MAX}", name="ck_gate_results_report"),
        CheckConstraint("link IS NULL OR link LIKE 'https://%'", name="ck_gate_results_link"),
    )

    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("gate_runs.id", ondelete="CASCADE", name="fk_gate_results_run_id"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    gate_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="waiting")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    started_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    finished_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    report: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class StageArrival(UuidPkMixin, OrgScoped, Base):
    """A change reaching a stage's branch, its deploy and its verify (architecture §4.3; lifecycle: child E)."""

    __tablename__ = "stage_arrivals"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("state", ARRIVAL_STATES, "ck_stage_arrivals_state"),
        _in_check("target_kind", TARGET_KINDS, "ck_stage_arrivals_target_kind"),
        Index("uq_stage_arrivals_open", "stage_id", unique=True,
              postgresql_where=text("state IN (" + ", ".join(f"'{s}'" for s in ARRIVAL_OPEN) + ")")),
        Index("ix_stage_arrivals_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(Integer, _project_fk(__tablename__), nullable=False)
    stage_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), _stage_fk(__tablename__), nullable=False)
    sha: Mapped[str] = mapped_column(String(64), nullable=False)
    release_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("releases.id", ondelete="SET NULL", name="fk_stage_arrivals_release_id"),
        nullable=True,
    )
    merge_lane_row_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")
    state: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    report: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gate_run_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    after_gates: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class StageHealth(OrgScoped, Base):
    """The stage address's last probe (the health dot; cron: child F). One row per stage, written lazily."""

    __tablename__ = "stage_health"
    __table_args__ = (
        _in_check("state", HEALTH_STATES, "ck_stage_health_state"),
        CheckConstraint("detail IS NULL OR char_length(detail) <= 200", name="ck_stage_health_detail"),
    )

    stage_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), _stage_fk(__tablename__), primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="unchecked")
    checked_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    wanted_until: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
