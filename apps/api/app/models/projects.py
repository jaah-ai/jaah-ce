"""Projects: the project row, its repos, features, env vars and runners."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ColumnElement,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    REAL,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column
from app.features import (
    FEATURE_DELETED_BY_FK,
    FEATURE_DELETED_PAIR_CHECK,
    FEATURE_DELETED_PAIR_CHECK_SQL,
    FEATURE_ICON_CHECK,
    FEATURE_ID_PROJECT_UNIQUE,
    FEATURE_LEAD_FK,
    FEATURE_NAME_CHECK,
    FEATURE_NAME_CHECK_SQL,
    FEATURE_LIVE_SQL,
    FEATURE_PROJECT_LIVE_INDEX,
    FEATURES_TABLE,
)
from app.crons import in_domain_sql
from app.production_qa.vocabulary import REVERT_POLICIES, REVERT_POLICY_DEFAULT
from app.models.agent_sessions import AGENT_OR_PROVIDER_SQL
from app.models.base import Base, EntityMixin, org_unique, OrgScoped, StampedMixin, timestamp_column


# A project's intelligent routing mode (`Project.routing_mode`), lowest first: `inherit` is off
# (`app/agent_router/modes.py` ranks them).
ROUTING_MODES = ("inherit", "recommend", "auto")
#: `projects.pipeline_mode`'s domain (`app/releases/basics.py` names each).
INTEGRATION_RELEASE = "integration_release"
PIPELINE_MODES = ("none", "pr_integration", INTEGRATION_RELEASE, "custom")


class Project(OrgScoped, Base):
    """Portfolio project metadata — the single home for it (nothing syncs it from elsewhere).

    Nullable settings columns carry both a `server_default` and a Python `default=` (the ORM names every
    column in its INSERT); nullable so the Edit form can clear them.

    Seven columns are NOT NULL with no runtime fallback anywhere: `integration_branch`, `branch_prefix`,
    `gate_quick_cmd`, `gate_full_cmd`, `push_enabled`, `stack_up_cmd`, `stack_down_cmd`.
    `_validated_fields` 422s empty values, except `''` is valid for `branch_prefix` (no prefix) and for
    the gate and stack commands (none; seeded once at create — a seed, not a fallback — from the org's
    Projects > Defaults settings, `operator_settings.defaults.project_create_defaults`). Push is allowed iff
    the ref starts with `branch_prefix` and `push_enabled`; a PR iff its head does.
    """

    __tablename__ = "projects"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "name", name="uq_projects_org_name"),
        CheckConstraint(
            AGENT_OR_PROVIDER_SQL.format(provider="default_provider").replace("agent_id", "default_agent_id"),
            name="ck_projects_default_agent_or_provider",
        ),
        # Unused since the merge lane moved to an ordinary slot; revision 0409. Awaits a drop migration.
        CheckConstraint(
            "verification_slots IS NULL OR verification_slots >= 1", name="ck_projects_verification_slots"
        ),
        # A platform-owned project (revision 0416, `app/node_targets.py`): one per kind per org, never a repo.
        CheckConstraint("system_kind IN ('nodes')", name="ck_projects_system_kind"),
        CheckConstraint(
            "routing_mode IN (" + ", ".join(f"'{mode}'" for mode in ROUTING_MODES) + ")", name="ck_projects_routing_mode"
        ),
        CheckConstraint("system_kind IS NULL OR repo_url IS NULL", name="ck_projects_system_kind_no_repo"),
        CheckConstraint(
            "experiment_budget_share >= 0 AND experiment_budget_share <= 1", name="ck_projects_experiment_budget_share"
        ),
        CheckConstraint(
            "benchmark_sample_rate >= 0 AND benchmark_sample_rate <= 1", name="ck_projects_benchmark_sample_rate"
        ),
        CheckConstraint(in_domain_sql("revert_policy", REVERT_POLICIES), name="ck_projects_revert_policy"),
        CheckConstraint(in_domain_sql("pipeline_mode", PIPELINE_MODES), name="ck_projects_pipeline_mode"),
        CheckConstraint(
            "outcome_scan_interval_minutes IS NULL OR "
            "(outcome_scan_interval_minutes >= 15 AND outcome_scan_interval_minutes <= 1440)",
            name="ck_projects_outcome_scan_interval_minutes",
        ),
        Index(
            "uq_projects_org_id_system_kind", "org_id", "system_kind", unique=True,
            postgresql_where=text("system_kind IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Display only, unique within its org (`uq_projects_org_name`); the id is the identity.
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # No default: NULL here means "no repo connected yet", which is a real, distinguishable state.
    repo_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The forge `repo_url` lives on (`app/forges.py::Forge`), derived at save. NULL = no repo URL, or one
    # that parses to no supported forge (a legacy row; dispatch skips it). Declared only for a
    # self-managed GitLab, whose host names no forge.
    forge: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A self-managed GitLab's https base URL when it is installed under a sub-path; NULL otherwise.
    forge_base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # ── the seven mandatory branch/gate/stack columns (see the class docstring) ─────────────────────

    # The one branch a runner rebases onto and PRs against. Never probed from git or defaulted at a call
    # site; `develop` is only this column's default.
    integration_branch: Mapped[str] = mapped_column(
        Text, nullable=False, default="develop", server_default="develop"
    )
    # Stored with its trailing slash (`'sn/'`; `''` = no prefix) so consumers concatenate;
    # `routes/projects.py` normalizes on write. Every created/pushed branch is `{branch_prefix}{rest}`.
    branch_prefix: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # The project's own gate commands (always-on: pre-commit + Stop hook; pre-PR). Per project, never a
    # call-site `./dc check` fallback. Not validated for existence: the runner reports that at gate time.
    # `''` = this project has no gate for that tier; a new project is seeded from its org's
    # Projects > Defaults settings (`routes/projects.py::_create_project_row`).
    gate_quick_cmd: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    gate_full_cmd: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # The project's own stack up/down commands, as shell command lines (env prefixes allowed). Per
    # project, never a call-site `./dc up` fallback; not validated for existence (the node reports it).
    # `''` = no stack command (the node refuses the stack verb); seeded like the gate pair.
    stack_up_cmd: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    stack_down_cmd: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # `push_enabled = false` also strips the clone's `origin` push URL, so a change to it or to
    # `branch_prefix` re-dispatches `sync_access_guard` (`routes/projects.py`). Default true.
    push_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # With `branch_prefix`/`push_enabled`, the primary row's write-through projection (`ProjectRepo` has each
    # repository's own; `app/project_repos.py::sync_primary`). True lets the access guard pass a push, forced or
    # not, to the integration branch; the runner asks before force-pushing it. Default false.
    allow_force_push: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # A tier from `app/model_catalog.py` (NULL/empty: no project default).
    default_model: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, default="high", server_default="high"
    )
    # A project uses a repository exactly when it has one connected: `repo_url`, the primary row's
    # projection (`app/project_repos.py::sync_primary`). None connected: tasks run in an empty folder slot — no
    # clone, branch, gate, stack or ports — and the git/forge/gate/stack columns above are unused but kept.
    # Copied onto each slot at declare (`Worktree.has_repo`); readers of a held slot ask the slot.
    @hybrid_property
    def has_repo(self) -> bool:
        return self.repo_url is not None

    @has_repo.inplace.expression
    @classmethod
    def _has_repo_expression(cls) -> ColumnElement[bool]:
        return cls.repo_url.is_not(None)

    # NULL for every project a person made; `'nodes'` for the org's system "Nodes" project node-target tasks are
    # filed in (`app/node_targets.py`). A system project is shown to org admins and operators only
    # (`authz.scope_key_clause`), and its repo, slot mode and kind are not editable (`routes/projects.py`).
    system_kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Every project is pool-capable, with no per-project ceiling: it runs on its node groups, else All
    # (`app/placement_groups.py`), on dynamic worktrees made per task.
    # MicroVM (revision 202610111900): this project's VM size ceilings (NULL = no project value,
    # `app/microvm_sizing.py`), and its edits to the default egress allowlist — lower-case DNS names,
    # optionally `*.`-prefixed (`app/microvm_egress.py` holds the default, the grammar and the effective list).
    microvm_vcpus: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    microvm_ram_mib: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    microvm_disk_gib: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    egress_allow: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    egress_deny: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    # Unused since the merge lane stopped reserving slots; kept until a drop migration.
    verification_slots: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The merge lane's record of a red integration branch (`merge_lane.py::record_develop_red`): the commit, what
    # fails on it, the gate log it was read from and the fix task; NULL while it is not known red.
    merge_lane_develop_red: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # The merge lane's last sweep of armed auto-merge (`merge_lane.py::take_batch`): `{at, armed_elsewhere}` — when
    # a lane run last disarmed the runner PRs, and the PRs it found armed by someone else. NULL: a sweep is owed.
    merge_lane_sweep: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    # PRs one merge-lane batch takes at most (`merge_lane.batch_max`, bounds checked on write); NULL = the default.
    merge_lane_batch_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The per-project merge lane switch (revision 202611141000), off by default: the lane runs here only while it is on
    # and the org's plan has the lane (`app/merge_lane_state.py::lane_states`); `_by` is who turned it on.
    merge_lane_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    merge_lane_enabled_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_projects_merge_lane_enabled_by"),
        nullable=True,
    )
    # Project memory On/Off for this project's tasks (revision 202611140400); NULL = the org's default. Read only by
    # `app/project_memory/switch.py::effective_memory`, below the kill switch and the org's plan.
    project_memory: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # The outcome scan (revision 202611012400; rules: `app/outcome_ledger/scan.py`): when the last `outcome_scan`
    # task was filed, the integration-branch head and time the last accepted report covered (its cursor), and the
    # project's scan interval override in minutes (NULL = the org's, else the operator default:
    # `outcome_ledger.settings.scan_interval`).
    outcome_scan_last_filed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    outcome_scan_cursor_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome_scan_cursor_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    outcome_scan_interval_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The project's own CI check-name patterns, added to the defaults (`app/outcome_ledger/security.py`).
    security_check_patterns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    # Production QA (revision 202611012000; rules: `app/production_qa/`). The opt-in and who turned it on; the
    # environment that counts as production; the deploy workflow paths watched; the revert policy; the regression
    # window and thresholds; the native probes (`[{id, name, url, expect_status}]`) and smoke commands
    # (`[{id, name, command, timeout_s}]`). Measurement never reads the opt-in.
    production_qa_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    production_qa_enabled_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_projects_production_qa_enabled_by"),
        nullable=True,
    )
    production_environment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deploy_workflows: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    revert_policy: Mapped[str] = mapped_column(
        Text, nullable=False, default=REVERT_POLICY_DEFAULT, server_default=REVERT_POLICY_DEFAULT
    )
    regression_window_min: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    regression_error_ratio: Mapped[float] = mapped_column(REAL, nullable=False, default=1.5, server_default="1.5")
    regression_min_events: Mapped[int] = mapped_column(Integer, nullable=False, default=20, server_default="20")
    regression_p95_ratio: Mapped[float] = mapped_column(REAL, nullable=False, default=1.3, server_default="1.3")
    health_probes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    smoke_commands: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # Intelligent routing (revision 202611012100, `app/agent_router/`): the project's mode (`inherit` = off,
    # `recommend`, `auto`; capped at `orgs.routing_max_mode`), the latency cap the router honours (hours, NULL = none),
    # and blind review — a second agent reviews without the first one's reasoning, from another vendor when
    # `blind_review_other_vendor`.
    routing_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="inherit", default="inherit")
    routing_latency_cap_hours: Mapped[Optional[Decimal]] = mapped_column(Numeric, nullable=True)
    blind_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), default=False)
    blind_review_other_vendor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    # The share of the project's rolling 30-day spend (dollars) that routing exploration and the outcome ledger's
    # benchmark runs may take, 0..1 (revision 202611020100). One knob for both.
    experiment_budget_share: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, server_default=text("0.100"), default=Decimal("0.100")
    )
    # Benchmark mode (revision 202611020500; rules: `app/outcome_ledger/benchmark.py`): the project's opt-in, read only
    # while the org's `benchmark_mode` switch is on, and the share of its coder tasks sampled into shadow duels, 0..1.
    benchmark_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    benchmark_sample_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, server_default=text("0.0500"), default=Decimal("0.0500")
    )
    # Easy Mode (revision 202611032300): a sponsored app project on the org's shared node. A tenant member's console on
    # its sessions is view-only (`routes/console.py::_easy_mode_view_only`); Easy Mode's create sets it.
    easy_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # The project's default runner, one of two exclusive halves (`ck_projects_default_agent_or_provider`): an
    # SDK agent (`app/agents.py`) or a built-in provider (an `agent_providers.PROVIDER_VALUES` member, checked
    # by the writer as `tasks.bound_provider` is). RESTRICT, as `tasks.agent_id`.
    default_agent_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True
    )
    default_provider: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The persona this project's new tasks start as (`persona_registry.PROJECT_DEFAULT_PERSONAS`, checked by the
    # writer); NULL = the org's "Persona" default. Read by every create that names none (`routes/tasks/create.py
    # ::_default_persona`, agent filing, a plan's unnamed children) and the console's create prefill.
    default_persona: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # [{"layer", "technology"}]; no-duplicate is enforced app-side (`app/project_validation.py`).
    tech_stack: Mapped[Optional[list[dict[str, str]]]] = mapped_column(
        JSONB, nullable=True, default=list, server_default=text("'[]'::jsonb")
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default="", server_default="")
    # The operator's own instructions to every runner on this project, injected into each composed
    # system prompt by `app/dispatch_compose/system_prompt.py`. Not a governance column (outside
    # `routes/projects.py::_GOVERNANCE_FIELDS`; any `projects:edit` holder sets it) and never executed.
    # Nullable with `''` like `notes`: empty is a complete answer ("adds nothing").
    agent_instructions: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, default="", server_default=""
    )
    # Who created this project; the single home for what a `created_by_id` means here and on `Task`.
    # Not the object access decision: that is `RoleBinding` alone (`app/access.py`), else revoking a
    # binding would be a lie. On `Task` it is one conjunct of one key decision
    # (`authored_draft_task_clause`: top-level, unhidden `draft` rows only), and it gates the
    # author-only states. It selects whose grants confine dispatch (the launcher, walked up the task
    # ancestry), never widens them. SET NULL; nullable with no default.
    created_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    # "Run jobs as": this project's override of `orgs.run_jobs_as_id` (`git_access.project_forge_person`). NULL: the
    # org's. SET NULL.
    run_jobs_as_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_projects_run_jobs_as"),
        nullable=True,
    )
    ingested_at: Mapped[datetime] = timestamp_column()
    # When the runtime pipeline upgrade (`app/releases/stages.py::upgrade`) wrote Ideas and Building: a stage deleted
    # afterwards is never re-created. NULL = not upgraded yet (reads synthesise them).
    pipeline_upgraded_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The pipeline's kind (`app/releases/basics.py`): a basic (`pr_integration`, `integration_release`), `custom`, or
    # `none` (no repository, or Ideas only — readers derive it, whatever is stored).
    pipeline_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=INTEGRATION_RELEASE,
                                               default=INTEGRATION_RELEASE)
    # When the release migration (revision 202611140800) reset this project's custom pipeline to a basic (off plan);
    # NULL = never. The CI Pipeline section's notice.
    pipeline_reset_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Freeze now (`app/releases/service.py::freeze_on`): every stage after the integration stage is frozen from this
    # moment until a person switches it off. NULL = off.
    freeze_now_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    freeze_now_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_projects_freeze_now_by_id"),
        nullable=True,
    )
    # The org pipeline template this project follows (`app/releases/linking.py`), and the published version it is on.
    # NULL = not linked. A template cannot be deleted while a project is linked to it (RESTRICT).
    pipeline_template_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("pipeline_templates.id", ondelete="RESTRICT", name="fk_projects_pipeline_template_id"),
        nullable=True,
    )
    pipeline_template_version_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("pipeline_template_versions.id", ondelete="RESTRICT",
                   name="fk_projects_pipeline_template_version_id"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class ProjectRepo(StampedMixin, OrgScoped, Base):
    """One repository connected to a project; `app/project_repos.py` is the single writer and holds
    every rule.

    A project with any row has exactly one `is_primary` (`uq_project_repos_primary` caps it at one, the writer
    enforces at least one). The primary is today's repo: `projects.repo_url/forge/forge_base_url/
    integration_branch` are its write-through projection, so every reader of those columns keeps working. A
    project with no row has no repository and runs in folder slots.

    Not an entity (`tests/test_entity_shape.py`): its id is a `bigint` identity because other tables key on it,
    and it is reached through its project, never by a handle. `name` is the repository's `owner/name` path,
    derived at save; `description` is free text."""

    __tablename__ = "project_repos"
    __table_args__ = (
        # The same repository twice in one project (AS3); several projects may share one (N6).
        Index("uq_project_repos_project_url", "project_id", text("lower(btrim(repo_url))"), unique=True),
        Index("uq_project_repos_primary", "project_id", unique=True, postgresql_where=text("is_primary")),
        Index("ix_project_repos_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE", name="fk_project_repos_project_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Derived at save, as `Project.forge`/`forge_base_url` are (`routes/projects.py::_derived_forge`).
    forge: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    forge_base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    integration_branch: Mapped[str] = mapped_column(Text, nullable=False)
    # This repository's Branch policy (`Project.branch_prefix`/`push_enabled`/`allow_force_push` are the primary's
    # projection). A new row inherits the project's prefix and push flag, force push off.
    branch_prefix: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    push_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    allow_force_push: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Display order; the primary is not forced first.
    position: Mapped[int] = mapped_column(Integer, nullable=False)


# A feature's optional icon: `"<shape>-<colour>"`, or NULL for "No icon" — 3 shapes x 8 colours. The
# console draws it (`apps/web/src/lib/featureIcon.ts`, which carries the per-theme hex); revision 0307
# writes the same 24 tokens as literals, and `tests/test_feature_icons.py` holds the two equal.
FEATURE_ICON_SHAPES = ("dot", "square", "triangle")


FEATURE_ICON_COLOURS = ("red", "orange", "yellow", "green", "teal", "blue", "purple", "pink")


FEATURE_ICONS = frozenset(
    f"{shape}-{colour}" for shape in FEATURE_ICON_SHAPES for colour in FEATURE_ICON_COLOURS
)


FEATURE_ICON_CHECK_SQL = "icon IS NULL OR icon IN ({})".format(
    ", ".join(f"'{token}'" for token in sorted(FEATURE_ICONS))
)


class Feature(EntityMixin, OrgScoped, Base):
    """Hierarchy level 3 — a folder of work inside one project, spanning many tasks and weeks, deleted by a
    person when done (the row stays, `deleted_at`). `app/features.py` owns the layer's names and rules.

    Its project is fixed for life (no move endpoint). `UNIQUE (id, project_id)` is what `tasks`' composite
    key targets, which is how "a task in a feature is in that feature's project" holds for every writer.

    A standard entity: no slug, and `name` is not unique."""

    __tablename__ = FEATURES_TABLE
    __table_args__ = (
        # The target of `tasks`' composite key `(feature_id, project_id)` (revision 0297).
        UniqueConstraint("id", "project_id", name=FEATURE_ID_PROJECT_UNIQUE),
        Index(FEATURE_PROJECT_LIVE_INDEX, "project_id", postgresql_where=text(FEATURE_LIVE_SQL)),
        CheckConstraint(FEATURE_NAME_CHECK_SQL, name=FEATURE_NAME_CHECK),
        CheckConstraint(FEATURE_ICON_CHECK_SQL, name=FEATURE_ICON_CHECK),
        CheckConstraint(FEATURE_DELETED_PAIR_CHECK_SQL, name=FEATURE_DELETED_PAIR_CHECK),
    )

    # The org derivation path (`app/tenancy/manifest.py`).
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE", name=f"fk_{FEATURES_TABLE}_project_id"),
        nullable=False,
    )
    # The person who leads it; the UI labels it "Owner". Not `owner_id`, because here "owner" names ACCESS
    # (the Org Owner role) and this column grants none: it is caller-chosen and informational, which is
    # also why it carries a same-org key where actor columns do not.
    lead_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name=FEATURE_LEAD_FK),
        nullable=False,
        comment=(
            "The feature's lead, shown as Owner. Not owner_id: owner means access here, and "
            "this column grants none."
        ),
    )
    # Who deleted it and when; both NULL while it is live. A deleted feature is hidden from the rail, the pickers
    # and filing, and still names its tasks in the pipeline's later stages and past releases.
    deleted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    deleted_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name=FEATURE_DELETED_BY_FK),
        nullable=True,
    )
    # The operator's order within its project on the task rail (`PUT /api/features/order`), `name` the
    # tie-break. Presentation only, as `Tag.position`.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # One of `FEATURE_ICONS`, or NULL ("No icon"). Drawn before the feature's name and its tasks' titles.
    icon: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Whether the task rail draws the feature's completion ring. Off by default: most features are task
    # buckets, not deliverables with an end. Shared, unlike a user's mute (`preferences.py`).
    show_progress: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


# ── `project_env_vars` ────────────────────────────────────────────────────────────────────────────────
#
# The variable name's grammar, rendered into the DDL below: the POSIX identifier rule, max 128 chars.
# A security control (see `ProjectEnvVar`).
PROJECT_ENV_VAR_NAME_RE = "^[A-Za-z_][A-Za-z0-9_]{0,127}$"


# A non-secret literal or a named field of a stored `Credential`: a closed vocabulary, and no column
# exists for a raw secret.
PROJECT_ENV_VAR_SOURCE_LITERAL = "literal"


PROJECT_ENV_VAR_SOURCE_CREDENTIAL = "credential"


# A host port the node picks for each slot at lease (`app/project_env.py::port_specs`); the row holds no value,
# only an optional preferred port.
PROJECT_ENV_VAR_SOURCE_PORT = "port"


PROJECT_ENV_VAR_SOURCES = (
    PROJECT_ENV_VAR_SOURCE_LITERAL, PROJECT_ENV_VAR_SOURCE_CREDENTIAL, PROJECT_ENV_VAR_SOURCE_PORT,
)

#: The range a Port row's preferred port may name: no privileged port, nothing above the last one.
PORT_PREFERRED_MIN = 1024
PORT_PREFERRED_MAX = 65535


class ProjectEnvVar(EntityMixin, OrgScoped, Base):
    """One environment variable a project's stacks are configured with, at one of two tiers.

    A child of `projects` (CASCADE): a declaration that a variable exists and where
    its value comes from. One table with nullable `user_id`: NULL is the project default, non-null that
    user's override (same shape and validation); the partial unique indexes make resolution deterministic.

    The value is either a non-secret `literal` or a credential reference (`credential_id`,
    `credential_field`; fields per `app/credentials/registry.py`), discriminated by `source` — or, for
    `source='port'`, no value at all: the node picks a free host port per slot at lease, trying
    `port_preferred` first when set. A Port row is project-tier only (`ck_project_env_vars_port_tier`). The schema
    cannot tell a hostname from a token and `literal` is cleartext, so refusing secret literals is the
    route's job: `app/routes/project_env.py::_refuse_a_secret_literal`.

    The discriminator is not an XOR on purpose: `credential_id` is `ON DELETE SET NULL`, so an XOR would
    make Postgres refuse every referenced credential delete. A deleted credential leaves a tombstone
    (`source='credential'`, NULL id, field kept) that resolves as missing and warns.

    Validation is a security control in DDL: any `projects:view` holder may author an override, and a
    newline in `name` or `literal` would inject assignments into the env file. `literal` must be
    non-empty with no CR/LF. That does not stop `${OTHER_VAR}` interpolation: the renderer must
    single-quote values.

    No `slug`; `name` is the variable name, unique per tier via the partial indexes. `updated_by_id` on
    a project-level row is the identity a default credential reference is read as (still never access).
    """

    __tablename__ = "project_env_vars"
    __table_args__ = (
        # Two partial unique indexes, since NULLs are distinct in SQL: a single
        # `UNIQUE (project_id, name, user_id)` would not constrain default rows at all.
        Index("uq_project_env_vars_default", "project_id", "name", unique=True,
              postgresql_where=text("user_id IS NULL")),
        Index("uq_project_env_vars_override", "project_id", "name", "user_id", unique=True,
              postgresql_where=text("user_id IS NOT NULL")),
        # "The variables of one project", across both tiers.
        Index("ix_project_env_vars_project_id", "project_id"),
        # Reverse lookups for user/credential deletes (Postgres does not index the referencing side).
        Index("ix_project_env_vars_user_id", "user_id"),
        Index("ix_project_env_vars_credential_id", "credential_id"),
        CheckConstraint(f"name ~ '{PROJECT_ENV_VAR_NAME_RE}'", name="ck_project_env_vars_name"),
        CheckConstraint(in_domain_sql("source", PROJECT_ENV_VAR_SOURCES),
                        name="ck_project_env_vars_source"),
        # The discriminator (not an XOR; see the class docstring). NULL `credential_id` is the tombstone.
        CheckConstraint(
            "(source = 'literal' AND literal IS NOT NULL AND credential_id IS NULL "
            "AND credential_field IS NULL) OR "
            "(source = 'credential' AND literal IS NULL AND credential_field IS NOT NULL) OR "
            "(source = 'port' AND literal IS NULL AND credential_id IS NULL AND credential_field IS NULL)",
            name="ck_project_env_vars_shape",
        ),
        # The node writes a port into every slot, so a per-user override of one has nothing to override.
        CheckConstraint("source <> 'port' OR user_id IS NULL", name="ck_project_env_vars_port_tier"),
        CheckConstraint(
            f"port_preferred IS NULL OR (source = 'port' AND port_preferred BETWEEN {PORT_PREFERRED_MIN} "
            f"AND {PORT_PREFERRED_MAX})",
            name="ck_project_env_vars_port_preferred",
        ),
        # Non-empty and free of CR/LF in one regex (`$` is end-of-string in Postgres). Non-blank is not
        # required: all-space values are legitimate.
        CheckConstraint(r"literal IS NULL OR literal ~ '^[^\n\r]+$'",
                        name="ck_project_env_vars_literal"),
        # The same rule on `credential_field`, else `''` or a newline could reach the rendered tombstone.
        # No identifier grammar: keys come from each credential type's schema.
        CheckConstraint(r"credential_field IS NULL OR credential_field ~ '^[^\n\r]+$'",
                        name="ck_project_env_vars_credential_field"),
    )

    # CASCADE, as `role_bindings.project_id`.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE", name="fk_project_env_vars_project_id"),
        nullable=False,
    )
    # NULL is the project default. CASCADE: an override is meaningless without its user.
    user_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE", name="fk_project_env_vars_user_id"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    literal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # SET NULL, never CASCADE: the tombstone (see the class docstring).
    credential_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL", name="fk_project_env_vars_credential_id"),
        nullable=True,
    )
    # Survives the credential's deletion so the tombstone still names the chosen field.
    credential_field: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A Port row's preferred host port; NULL = auto-assigned per slot. The node uses it when it is free on
    # the host and no sibling slot claims it, else the next free port (`worktree_mechanics.ports`).
    port_preferred: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Runner(OrgScoped, Base):
    """Active Claude runner sessions ingested from `claude agents --json`."""

    __tablename__ = "runners"
    # The observation upsert's `ON CONFLICT` target. `pid` stays nullable because NULL-pid inserts
    # happen today; the observation payload model requires `pid`.
    __table_args__ = (UniqueConstraint("node_id", "pid", name="uq_runners_node_pid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE, gated by the slot occupancy pre-delete guard (0106). No server default (as `worktrees`).
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # No `project` column (no reader); the project derives from `cwd`/`worktree_path`.
    worktree_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen: Mapped[datetime] = timestamp_column(server_now=False)
    ingested_at: Mapped[datetime] = timestamp_column()
    # The node's own stamp for this observation (vs `ingested_at`), as `worktrees.observed_at`.
    observed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The other half of `pid`'s identity, for every session the node sees (including one whose worktree
    # row is gone). NULL = unreadable this pass; the node's `""` is normalised to NULL on ingest.
    proc_started_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Attribution, resolved at ingest from the credential-dir path, failing open to NULL (as
    # `Worktree.account_id`). SET NULL.
    account_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
