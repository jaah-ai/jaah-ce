"""Tasks: the task row, its questions, dependencies, ratings and body versions, and the playbooks it runs."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.features import TASK_FEATURE_PROJECT_FK, TASK_FEATURE_INDEX
from app.task_sections import (
    TASK_STOP_NOTE_CHECK,
    TASK_STOP_NOTE_CHECK_SQL,
    TASK_STOP_REASON_CHECK,
    TASK_STOP_REASON_CHECK_SQL,
    TASK_STOP_RECORD_CHECK,
    TASK_STOP_RECORD_CHECK_SQL,
    TASK_STOPPED_BY_FK,
)
from app.task_source import TASK_SOURCE_CHECK, TASK_SOURCE_CHECK_SQL, TASK_SOURCE_INDEX
# Leaf import (stdlib-only).
from app.task_memory_arm import ARM_SOURCES as MEMORY_ARM_SOURCES
# Leaf import (stdlib-only): the playbook tables' CHECKs render from the closed sets.
from app.playbook_vocabulary import ENTRY_LABELS, SENIORITY_TIERS
from app.models.agent_sessions import AGENT_OR_PROVIDER_SQL
from app.models.base import Base, EntityMixin, _in_check, org_unique, OrgScoped, timestamp_column


# The compliance fix-job state lives on the hidden `compliance_fix` group Task (`Task.compliance_group_id`,
# slot lock `uq_tasks_compliance_slot`, partial on `persona = 'compliance_fix'`), not a table. See
# `routes/agent/task_terminal.py::_settle_compliance_group`.


class Task(OrgScoped, Base):
    """Cockpit-authored unit of work — the row that tracks one piece of fleet work end to end.

    A human drafts it and marks it for development; the row then tracks the dispatch through the state
    lifecycle (`app/task_lifecycle.py`, app-layer, no CHECK). Analysis is dependency-ordered child Tasks
    of an epic, by `persona`. `parent_task_id` is a CASCADE self-FK.

    `github_synced_at` is the state-transition watermark (historical name): every
    lifecycle writer stamps it on the edge it lands, so "moved since T" compares it (the drain's
    unannounced-launch charge reads it against `dispatched_at`), unlike `updated_at`. A Task's dispatch
    identity is its `id`.

    `stale_reason`/`stale_since` mark a Task dirty without a new state, BFS-propagated to dependents and
    cleared only by a successful re-run of the marked Task.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        org_unique(__tablename__),
        # A task in a feature is in that feature's project (`app/features.py`, revision 0297). Declared
        # WITHOUT its delete action: the revision renders `ON DELETE SET NULL (feature_id)`, which
        # `ondelete=` cannot spell (a bare SET NULL would null `project_id` too). Readers rely on the
        # database, never on the ORM cascading — nothing here has a relationship to delete through.
        ForeignKeyConstraint(
            ["feature_id", "project_id"],
            ["features.id", "features.project_id"],
            name=TASK_FEATURE_PROJECT_FK,
        ),
        Index(TASK_FEATURE_INDEX, "feature_id", postgresql_where=text("feature_id IS NOT NULL")),
        # The org template a task was started from is its own org's (revision 202610281500). Declared WITHOUT its
        # delete action, as the feature key above: the revision renders `ON DELETE SET NULL (org_template_id)`, so a
        # template delete never nulls `org_id`.
        ForeignKeyConstraint(
            ["org_template_id", "org_id"],
            ["org_task_templates.id", "org_task_templates.org_id"],
            name="fk_tasks_org_template_id",
        ),
        Index("ix_tasks_template_id", "template_id", postgresql_where=text("template_id IS NOT NULL")),
        Index("ix_tasks_org_template_id", "org_template_id", postgresql_where=text("org_template_id IS NOT NULL")),
        # The stop record is whole or absent, and a note rides a wait only (`app/task_sections.py`).
        CheckConstraint(TASK_STOP_REASON_CHECK_SQL, name=TASK_STOP_REASON_CHECK),
        CheckConstraint(TASK_STOP_RECORD_CHECK_SQL, name=TASK_STOP_RECORD_CHECK),
        CheckConstraint(TASK_STOP_NOTE_CHECK_SQL, name=TASK_STOP_NOTE_CHECK),
        # Revision 0383; the value list is `app/task_source.py`'s.
        CheckConstraint(TASK_SOURCE_CHECK_SQL, name=TASK_SOURCE_CHECK),
        Index(TASK_SOURCE_INDEX, "project_id", "source"),
        CheckConstraint(
            "memory_arm_source IN (" + ", ".join(f"'{s}'" for s in MEMORY_ARM_SOURCES) + ")",
            name="ck_tasks_memory_arm_source",
        ),
        Index("ix_tasks_pinned_node_id", "pinned_node_id", postgresql_where=text("pinned_node_id IS NOT NULL")),
        CheckConstraint(AGENT_OR_PROVIDER_SQL.format(provider="bound_provider"), name="ck_tasks_agent_or_provider"),
        Index("ix_tasks_agent_id", "agent_id", postgresql_where=text("agent_id IS NOT NULL")),
        Index("ix_tasks_bound_account_id", "bound_account_id", postgresql_where=text("bound_account_id IS NOT NULL")),
        # The fire engine's overlap read: an automation's newest task per project / per pinned node.
        Index(
            "ix_tasks_created_by_automation_project", "created_by_automation_id", "project_id", text("id DESC"),
            postgresql_where=text("created_by_automation_id IS NOT NULL"),
        ),
        Index(
            "ix_tasks_created_by_automation_node", "created_by_automation_id", "pinned_node_id", text("id DESC"),
            postgresql_where=text("created_by_automation_id IS NOT NULL"),
        ),
        Index("ix_tasks_external_task_id", "external_task_id", postgresql_where=text("external_task_id IS NOT NULL")),
        Index("ix_tasks_project_state_dispatchable", "project_id", "state", postgresql_where=text("state = 'ready'")),
        Index(
            "ix_tasks_done_workflow_pending", "done_workflow_attempts", "id",
            postgresql_where=text("state = 'done' AND done_workflow_at IS NULL"),
        ),
        Index("ix_tasks_persona_dedup_key", "persona", "dedup_key", postgresql_ops={"dedup_key": "text_pattern_ops"}),
        # The keyed half of the system-task dedup guard (`routes/tasks/agent_filing.py::_UNIQUE_DEDUP_INDEX`).
        Index(
            "uq_tasks_open_dedup_key", "project_id", "persona", "dedup_key", unique=True,
            postgresql_where=text(
                "dedup_key IS NOT NULL AND state NOT IN ('done', 'system_failed', 'paused', 'awaiting_response')"
            ),
        ),
        # One live compliance fix per (project, path) — the slot lock this class's preamble names.
        Index(
            "uq_tasks_compliance_slot", "project_id", "compliance_path", unique=True,
            postgresql_where=text(
                "persona = 'compliance_fix' AND compliance_path IS NOT NULL"
                " AND (state IN ('ready', 'active') OR (state = 'awaiting_response' AND outcome_pr_url IS NOT NULL))"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), nullable=False, index=True
    )
    # The feature this task is filed in; NULL is "No feature". Always a feature of `project_id` (the
    # composite keys above). A stage task takes its epic's by the resolve trigger on insert, and a filing or
    # move rewrites the whole subtree (`routes/tasks/update.py::_apply_feature_filing`).
    feature_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    parent_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # The one node that may place this task — a node-target automation's task (`app/node_targets.py`). NULL for
    # every other task, whose placement it never touches (`app/placement_grants.py`). NO foreign key: deleting
    # the node must never silently free the pin; a pin to a gone node is a visible refusal (revision 0416).
    pinned_node_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    is_plannable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="draft", server_default="draft"
    )
    # The sole UI-filter authority: hidden rows are excluded from the default `/tasks` list, and
    # `persona` never implies hidden. Set at create; an editor may set or clear it on a top-level row
    # afterwards, applied to the whole subtree (`routes/tasks/update.py::_apply_hidden`). Readers take the live value.
    hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Created by the task editor's auto-save and never explicitly saved. Drives the "New task" title,
    # withheld LLM title refinement and deferred epic children. Unlike `hidden` (off every list), it
    # narrows the audience to the author. Cleared once by explicit Save/Go (`TaskUpdate.autosave=False`)
    # and never set again, so `false` is permanent. Also an authorization input:
    # `authz.visible_task_clause` and `authz.editable_task_clause` admit only the author and superadmins.
    unsaved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # A per-cell `compliance_fix` mirror's group Task id (the settle cascade finds mirrors by it). NULL
    # on the group row and non-compliance rows.
    compliance_group_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Fix-path discriminator on a group `compliance_fix` Task; NULL on mirrors (NOT NULL ⟺ group). With
    # `project_id` it keys `uq_tasks_compliance_slot` (one live fix per project). Rows carry `"runner"`;
    # legacy rows may carry `"mechanical"`.
    compliance_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Rule coordinates stamped on each per-cell `compliance_fix` mirror. NULL for other personas.
    rule_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    asset_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The settle instant, stamped by every `settle_coder_on_merge`. A `compliance_fix` mirror
    # is re-stamped to its group's value, so a group and its mirrors carry one stable stamp.
    merged_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The state-transition watermark (see the class docstring), no default. Distinct from `updated_at`,
    # which bumps on any ORM write including a human's console edit.
    github_synced_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Raw-text cache of the gate question a Task is waiting on a human to answer; parsed on read.
    awaiting_response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Set when the CP stopped the runner of an unanswered question after the operator's timeout; the row
    # stays `awaiting_response` and answerable. Cleared on the next `ready -> active` claim.
    question_runner_stopped_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Soft-delete watermark for a superseded attempt's artifacts: Reset stamps `now()`, and older rows
    # are hidden. NULL hides nothing. Obeying tables and the predicate: `app/task_artifacts.py`.
    artifacts_hidden_before: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The dispatch discriminator. App-layer vocabulary single-homed in
    # `app/persona_registry.py` (`PIPELINE_PERSONAS` + `SYSTEM_PERSONAS`). NOT NULL. `epic` is a
    # non-dispatched value, so a persona does not imply dispatch.
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    # Per-source dedup key for a hidden system Task: the match for `create_system_task` dedup and the
    # `app/lease_drain.py` alarm channels, as an index probe instead of a `body LIKE` scan. Not yet
    # universal: `app/runner_breaks.py` still matches on the body marker. The `<!-- sysdedup:KEY -->`
    # marker is still stamped into `body` for older workers during rollout (additive-migrations rule).
    dedup_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Operator dispatch-order lever: higher = sooner, with 0 meaning normal. The order-by vocabulary
    # (`priority DESC, id`) is app-layer, not a DB constraint.
    priority: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, default=0, server_default="0"
    )
    # Done-outcome evidence, derived by the jaahd backstop, never by a transition. `outcome_pr_url` is
    # write-once; `outcome_summary` is not (a later write may add the PR url). NULL renders as the
    # console's "unverified" state. `Text`, not `String(n)`: the target length
    # (`task_manifest.OUTCOME_SUMMARY_TARGET_CHARS`) is only asked for, since a DataError inside the
    # settle transaction would lose the terminal.
    outcome_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome_pr_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Legitimate-no-PR terminal evidence (dedup / already-shipped / no-code-needed) — unlocks the epic
    # rollup in lieu of a merged PR. Set only from a deterministic marker, never prose-parsed, and only
    # when its evidence is non-empty: the anti-spoof rule, so a bare false-close never rolls up.
    outcome_no_pr_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The BEHAVIORAL-proof half of a legitimate no-PR terminal — the exact reproduction the implementer
    # ran and what it observed. A no-PR terminal is honored only when the reason is recognized AND this
    # is non-empty: the anti-spoof floor.
    outcome_no_pr_evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A path or URL sufficient to build a clickable link to the worktree an in-progress task runs in.
    # Set while a task is being worked, NULL otherwise.
    worktree_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Which vendor's CLI ran this task, stamped once from the claimed account's `accounts.provider` by
    # `routes/tasks/dispatch.py::_bound_provider_stamp` (coalesced, so re-dispatch keeps it). NULL = never
    # dispatched, hence no default. A binding: `routes/fleet.py::_provider_narrowed` offers only this
    # vendor's accounts. A superadmin may also set or clear it from the task form (`TaskUpdate.bound_provider`).
    bound_provider: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The SDK agent picked for this task (`app/agents.py`), the other half of the one picker beside
    # `bound_provider`: never both (`ck_tasks_agent_or_provider`). RESTRICT, so a picked agent is disabled,
    # never deleted, while a task names it.
    agent_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True
    )
    # The one account this task may run on, picked on the New-task form; NULL = any. Set with
    # `bound_provider` stamped from the account's own provider (`routes/tasks/create.py::_account_pick_or_422`);
    # `routes/fleet.py::_account_pin_narrowed` offers only this account, so an unavailable one is a wait. SET
    # NULL: a deleted account leaves the task on its vendor.
    bound_account_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    # The claim stamps of an SDK agent run, as `bound_provider` is a built-in's: which agent and which commit
    # the last dispatch ran. No FK: a stamp is history, and the bundle route keys on the pair alone.
    run_agent_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    run_agent_commit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The kind of slot the task was last leased into (`Worktree.has_repo`), written by the lease
    # (`app/slot_state.py`). NULL = never dispatched. Read only through `slot_allocation.task_has_repo`, so a
    # finished task keeps its kind after its project's repository flips.
    has_repo: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # What the LAST dispatch actually launched, stamped by `app/lease_drain.py` from the compose verdict:
    # the effective tier (inherited or pinned; NULL for an exact override) and the concrete `--model` the
    # provider's CLI was given (NULL = none: the CLI's own default). Historical fact, never a setting — the
    # console renders "High → gpt-5.6-sol" from them. NULL on both = never dispatched since 0274.
    dispatched_tier: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dispatched_model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Autonomous-mode master switch. NOT NULL, default true.
    autonomous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Project memory (revision 0391; `app/project_memory/__init__.py`). `project_memory` is the task's own
    # On/Off, NULL = inherit (epic, then the System Settings default). `memory_arm` is what the last dispatch
    # attempt resolved and `memory_arm_source` where it came from (`switch.effective_memory`): the A/B arm,
    # stamped by the compose callers, never a setting.
    project_memory: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    memory_arm: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    memory_arm_source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Opts this task out of the periodic agentic-maturity/compliance sweep.
    sweep_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The `claude --resume` session id for live steer/resume. Set only while a runner session is
    # live or resumable.
    runner_session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Clarity-gate score; NULL = not yet scored.
    clarity_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Spec-review gate verdict. Vocabulary is app-layer, matching `state`/`persona`.
    spec_verdict: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Critic-review gate verdict — same rationale as `spec_verdict` above.
    critic_verdict: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Per-child epic-fanout retry counter (`task_pipeline_cp.fanout_attempt_failed`); at
    # `epic_fanout_cap()` the parent epic escalates, never this child.
    fanout_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Staleness marker, not a lifecycle state: set by the task's own edit or an upstream edit/re-run,
    # cleared together only by a successful re-run of this task.
    stale_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stale_since: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Structured recon artifact captured once per analysis stage, generated by the runner's file-footprint tool
    # (see the `recon_map` settle argument in `apps/jaahd/agent_vocabulary.py`). Threaded verbatim via
    # `task_dependencies` and rendered inside the untrusted fence by `dispatch_compose.recon_map._render_recon_map`.
    # Free-shape JSONB; NULL threads nothing.
    recon_map: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # The playbook engine (`app/playbook_engine.py`, the single home of both shapes). `playbook`: on a
    # top-level row, the snapshot of the tenant playbook it was created from, taken when it is set, so a later
    # edit of the playbook never changes it. `playbook_stage`: the resolved stage a row runs (expert ×
    # deliverable, tier, filing) — on every child of a playbook container, and on a one-stage row itself.
    # NULL on both runs today's persona-keyed engine.
    playbook: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    playbook_stage: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # Phase-DAG: the plan stage puts each coder in one phase (1-based ordinal + label). No `phases`
    # table: `append_tasks` wires phase k+1 to depend on phase k, and `dispatchable_tasks` gates on that.
    phase_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    phase_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Provenance: non-NULL means the Finish phase filed this `coder` child (the value is that filing's
    # dedup key); the epic view groups it under Finish. In a column, not the body, so the predicate cannot
    # be spoofed.
    finish_follow_up_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Who created this task (see `Project.created_by_id`). A runner-filed row (coder/finish/follow-up
    # children, an agent filing and its analysis children, a system task) takes the filer's original
    # executor (`task_ancestry.executor_user_id`: starter, else creator); an epic's fan-out children take the
    # epic's own creator, never the principal whose request fanned it out (`routes/tasks/create.py::
    # _create_epic_children`). Indexed (plain) for the board's creator filter.
    created_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    # Who this task's notifications go to: the outcome's owner, distinct from `created_by_id` (whose
    # grants confine) and `started_by_id` (whose git). Defaults to the creator, inherited by fan-out
    # children from the epic, editable in any state. Read by `task_ancestry.assignee_user_id`; NULL falls
    # back to the creator chain (0261 backfilled it). Addressing only, never access. SET NULL, indexed.
    assigned_to_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    # Who started this task: whose git access a runner borrows (`app/git_access.py`), distinct
    # from `created_by_id`. Which edges stamp it, including a runner's top-level filing: `routes/tasks/` above
    # `_start`. No read-time fallback to the creator: no starter means no git credential and a refused dispatch.
    # SET NULL (a dangling starter fails closed); indexed for `git_access.starter_user_id`'s ancestor walk.
    started_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    # The stop record: why this row stopped, who stopped it and when (`app/task_sections.py`). Written by
    # `routes/tasks/transitions.py::_stop_task_core` and the record-only wait-on-customer path; cleared by the
    # `trg_tasks_clear_stop_record` trigger when the row leaves for ready/active/done/draft. NULL on nearly
    # every row. Not a state: `Task.state` alone still drives the lifecycle.
    stop_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stop_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # An actor stamp, RESTRICT like `features.shipped_by_id`: SET NULL would leave a half-record the
    # record check refuses.
    stopped_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="RESTRICT", name=TASK_STOPPED_BY_FK),
        nullable=True,
    )
    stopped_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Who wrote this row last; provenance only. Two writers: `actors.stamp_dirty_task_actors` for ORM
    # flushes and `actors.task_actor_values` for Core `update(Task)`. Machine writes stamp the system
    # actor. Nullable + SET NULL (see 0128's header).
    updated_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    # Metrics for the terminal `finish` phase, free-shape like `recon_map`: each child its capture, the
    # epic the aggregate.
    metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # `ready → active` edges since the last forward progress (`done` or a human clear); at
    # `fleet_dispatch_cap()` the edge parks the row instead.
    dispatch_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Settle calls refused for an incomplete manifest (`app/persona_registry.py::PERSONA_MANIFEST`): a
    # nudge counted toward a later waiver. Cleared by Reset (`_RESET_TO_DRAFT_COLUMNS`), not by
    # re-dispatch.
    settle_refusals: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # This Task's model pin; NULL defers to the persona stage default and the project card. Values and
    # precedence: `app/model_catalog.py`. No DB CHECK.
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Cron provenance: a SET NULL FK plus a durable name snapshot, so a task still names a deleted cron.
    # NULL/`''` when no cron created it.
    created_by_cron_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("crons.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    created_by_cron_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Trigger provenance, the cron pair's shape: a SET NULL FK plus a durable name snapshot
    # (`trigger_intake` stamps both when a trigger files this row).
    created_by_trigger_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_triggers.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    created_by_trigger_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # The API key a webhook delivery filed this row with, the same pair's shape: its owner is `created_by_id`, and
    # the Activity feed names the row "<owner> via <key name>" (`routes/tasks/activity.py`).
    created_by_api_key_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    created_by_api_key_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Automation provenance (revision 0417), the cron pair's shape, stamped beside whichever of the two above
    # the filer writes: the fire engine's per-target overlap check reads it (indexes in `__table_args__`).
    created_by_automation_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("automations.id", ondelete="SET NULL"), nullable=True,
    )
    created_by_automation_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Template provenance (revision 202610281500): the global or org template a person started this row from (at
    # most one; the live link nulls on the template's delete) and its name, kept after the delete (PRD D15).
    template_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_templates.id", ondelete="SET NULL"), nullable=True,
    )
    org_template_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    template_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Agent provenance: the task whose runner filed this row (`routes/tasks/agent_filing.py::file_agent_top_level_task`).
    # A column because it is an authorization predicate (the filer's `edit_filed`/`delete_filed` reach).
    # SET NULL: the filer's removal neither deletes nor orphans the work.
    created_by_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # Where the row came from, set at filing and never changed; values, labels and the epic-inheritance
    # rule live in `app/task_source.py`. The default covers the system filers that name none.
    source: Mapped[str] = mapped_column(
        Text, nullable=False, default="console", server_default="console"
    )
    # How an epic sequences dep-satisfied children, read only off the parent: `auto` dispatches, `hold`
    # waits for a per-subtask Run. App-layer vocabulary.
    transition_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="auto", server_default="auto"
    )
    # One-shot manual Run: stamped by Run, cleared on the next `ready → active` claim
    # (`_active_dispatch_values`). Bypasses `hold` and unmet prerequisites.
    run_requested_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Why the lease-drain last declined this `ready` task (no account, no worktree) and when;
    # machinery, written without touching `updated_at`, read only while fresh (`app/dispatch_wait.py`).
    dispatch_wait_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dispatch_wait_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Archive stamp for a replaced epic coder/finish child (`append_tasks`); `state` is kept as evidence.
    # Every epic-children reader excludes archived rows at the query.
    archived_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Auto-re-tag-on-done markers, written only by `app/task_retag.py::retag_done_task`, making the step
    # idempotent. `auto_retagged_at` stamps success; NULL = not yet classified (a verdict always names a
    # tag).
    auto_retagged_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Per-task failures only (billable ~20s calls); a missing key or empty catalogue ends the pass
    # without spending attempts.
    auto_retag_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    # ── the external issue this task was created from ──────────────────────────────────────────────
    # Tracker and its issue id (`'jira'` + `'OAM-123'`), written by the "From Jira" lookup; storage only,
    # null together.
    external_task_system: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    external_task_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Done-comment bookkeeping, shaped like the auto-retag pair (`app/task_external_comment.py` is the
    # only writer): success stamp plus per-task failure counter.
    external_done_comment_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    external_done_comment_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    # The done workflow's convergence marker (migration 0147), written only by
    # `app/durable/task_done.py::mark_converged`: every owed action has settled (succeeded, skipped or
    # given up), not necessarily succeeded. NULL is the admission predicate (`_admission`), so a failed step
    # withholds or clears it and re-emitted `done` events on later edits cost nothing. A knob-disabled
    # action does not withhold it (the marker is per row, knobs per action); nothing replays a converged
    # row when a knob comes back on.
    done_workflow_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Give-up counter, incremented by `mark_converged` whenever a run ends with work still owed: the
    # exit from `_admission`, since the epic fan-in and `task_retag`'s deployment-wide refusals spend no
    # per-action attempts (else every edit would rerun a billable workflow forever).
    done_workflow_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    # When the task reached `done` (revision 202610120500), stamped by the compliance seal's writer (C4). NOT
    # backfilled: NULL on a done task means it finished before the column existed.
    done_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The counter a compliance seal takes its manifest `version` from (`compliance_manifests`); 0 = never sealed.
    compliance_seal_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


# ── DB-only pipeline status vocabularies ──────────────────────────────────────────────────────────
# App-layer constants, not DB CHECKs or enums, like `Task.state`, so a new value needs no migration.
QUESTION_STATUSES: tuple[str, ...] = ("open", "answered", "dismissed")


class TaskQuestion(OrgScoped, Base):
    """A blocking question, its offered options, and its chosen resolution — first-class queryable rows.

    `task_id` cascades; `status` follows `QUESTION_STATUSES` (app-layer). `delivered_at` is when the
    resolution was handed to a runner, not when a human decided: a Task can carry several open rounds
    and the gate clears only when none is outstanding, so the batch is delivered once.
    `_settle_gate_if_nothing_outstanding` is the single writer."""

    __tablename__ = "task_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # Ordered offered choices; NULL for a free-form question.
    options: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    chosen_resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The round's `AskUserQuestion` `multiSelect` flag (`routes/agent/task_terminal.py::create_task_question`); a
    # column because it is a fact about the round, not an option. False means single-choice.
    multi_select: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The runner's short per-question `header` ("Missing input"), telling rounds of a batch apart. A
    # column for `multi_select`'s reason. `Text`: the length bound lives at the trust boundary
    # (`routes/agent/task_terminal.py::TaskQuestionIn.header`, a readable 422). Stored verbatim. NULL means no header,
    # whether pre-0145 or unstated (the same absence).
    header: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The Architectural Guard rule a "Guard" stuck-limit round is about (`app/governance/guard_escalation.py`):
    # `{pack_id, rule_key, rule_version, mode, project_id, path, line, excerpt}`; on a round the control plane raised
    # itself, `{cp_gate, project_id}` (`app/routes/tasks/checkout_gate.py`). NULL on every other round.
    guard_context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # The operator's free-text note beside the choice; separate from `chosen_resolution` (see
    # `app/routes/tasks/answers.py::_resolution_for_delivery`). NULL = none typed.
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The round reworded in plain terms (`app/question_plain.py::QuestionPlain`), written after the round is
    # recorded. DISPLAY ONLY: an answer is matched against `options`, never this. NULL = not reworded.
    plain: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="open", server_default="open"
    )
    # Who resolved this round (answer or dismissal; `status` says which), hence `resolved_*`. The label
    # is joined at read time (`app/actors.py::actor_names`). SET NULL, as `Task.created_by_id`.
    resolved_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    # When it was resolved; not `updated_at`, which `delivered_at`'s later write drags forward. NULL
    # while `open`.
    resolved_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # When the resolution was handed to a runner (see the class docstring). NULL until the gate clears,
    # which makes the batch hand-off exactly-once.
    delivered_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class TaskDependency(OrgScoped, Base):
    """Cross-task prerequisite edge: `task_id` is blocked-by `depends_on_task_id`.

    A row (A, B) means Task A cannot dispatch until Task B reaches `done`. The pair is the composite
    primary key, and both FKs cascade, so deleting either endpoint removes its edges. Cycle and
    self-edge prevention is app-layer, not a DB constraint, matching how `Task.state` keeps its
    vocabulary out of the schema."""

    __tablename__ = "task_dependencies"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    # Indexed for the reverse lookup (dependents of a completed prerequisite, and cascade deletes).
    depends_on_task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class TaskRating(OrgScoped, Base):
    """One user's five-star judgement of one Task — the whole rating feature's storage.

    The composite PK (task_id, user_id) is the only enforcement of one vote per user: writes are an
    UPSERT on it (`app/routes/task_ratings.py`), so concurrent votes replace rather than race. Votes are
    replaced, never appended, so `updated_at` is when the voter last changed their mind.

    Both FKs cascade: `user_id` is half the PK, and a rating is a named judgement, so a deleted user's
    votes go. The 1..5 bound is a DB CHECK (a fixed range, not a vocabulary; see migration 0154).
    """

    __tablename__ = "task_ratings"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    # Indexed for the reverse lookup (a voter's own vote; cascade from `admin_users`).
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    # SmallInteger for the 1..5 domain; the CHECK bounds it.
    stars: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    # `onupdate` is fine here: not an entity table (`_EXEMPT_TABLES`), so no `set_updated_at` trigger.
    # The UPSERT sets it explicitly (`onupdate` does not reach raw SQL).
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class TaskBodyVersion(OrgScoped, Base):
    """Snapshot-on-write version history for a Task's `body`.

    Each row is the body about to be replaced, captured just before an edit (a prior description, not
    the new value). `source`: `manual` | `agent_filed_edit` | `runner_break_refresh` |
    `system_dedup_refresh`. `restored_from_id` is historical only (no live writer sets it); `SET NULL`
    keeps a snapshot valid if the version it names is removed."""

    __tablename__ = "task_body_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    restored_from_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_body_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = timestamp_column()


# ── Playbooks (run only through `app/playbook_engine.py`, the one bridge — `tests/test_playbook_isolation.py`)


def _playbook_checks(table: str) -> tuple[CheckConstraint, ...]:
    """The CHECKs both playbook tables carry, rendered from `app/playbook_vocabulary.py` (revision 0310
    spells the same bodies as frozen literals)."""
    return (
        _in_check("entry", tuple(ENTRY_LABELS), f"ck_{table}_entry"),
        _in_check("default_seniority", tuple(SENIORITY_TIERS), f"ck_{table}_default_seniority"),
        CheckConstraint("jsonb_typeof(stages) = 'array'", name=f"ck_{table}_stages_array"),
    )


class _PlaybookColumns:
    """What a playbook is, whichever table holds it: an ordered list of stages (array order IS stage
    order), each in the `StageIn` shape `app/routes/playbooks.py` accepts and `app/playbook_validation.py`
    checks. No `slug` (D10) and no `origin` column — origin is which table the row lives in."""

    entry: Mapped[str] = mapped_column(Text, nullable=False)
    full_auto_default: Mapped[bool] = mapped_column(Boolean, nullable=False)
    default_seniority: Mapped[str] = mapped_column(Text, nullable=False)
    stages: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)


class Playbook(EntityMixin, OrgScoped, _PlaybookColumns, Base):
    """A tenant's own playbook: a duplicated built-in or one written from scratch. `name` is unique per
    org, so the list and the duplicate's "<name> copy" suffix have one handle each."""

    __tablename__ = "playbooks"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "name", name="uq_playbooks_org_name"),
        *_playbook_checks(__tablename__),
    )


class BuiltInPlaybook(EntityMixin, _PlaybookColumns, Base):
    """One of the four built-in playbooks, one global copy each (D1), seeded by revision 0310 from
    `app/playbook_built_ins.py` with fixed ids. Read-only through the API; duplicate one to edit a copy.
    A separate table because `playbooks.org_id` is NOT NULL and the read seam fails closed on it."""

    __tablename__ = "built_in_playbooks"
    __table_args__ = (
        UniqueConstraint("name", name="uq_built_in_playbooks_name"),
        *_playbook_checks(__tablename__),
    )
