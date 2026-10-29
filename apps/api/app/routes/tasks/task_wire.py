"""The Task wire shape: `TaskOut` and the shared Out models, and the readers that fill them."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, object_session

# `actors` as a module because `_task_out` takes an `actor_names` parameter that would shadow a bare import.
from app import actors, agent_providers, edition_features, org_limits, runner_terminations, slot_allocation
from app.authz.catalog import permission_key
from app.authz.object_access import can_access
from app.authz.principal import Principal
from app.authz.visibility import (
    ObjectAccess,
    authored_draft_task_clause,
    editable_task_clause,
    has_project_key,
    scoped_task_select,
    visible_task_clause,
)
from app.models.audit import ActivityEvent
from app.models.automations import TaskTrigger, TriggerDelivery
from app.models.task_extras import ATTACHMENT_KIND_FILE, TaskAttachment, TaskPr
from app.models.tasks import Task, TaskDependency, TaskQuestion
# `_CONSOLE_INTERVIEW` is private to the composer and imported anyway: one frozenset, no second copy.
from app.dispatch_compose.directives import interview_skipped
from app.dispatch_compose.role_tables import _CONSOLE_INTERVIEW
from app.persona_registry import DONE_CRITERION_OPERATOR, PERSONA_CHARTER, PERSONA_DONE_CRITERION
from app.task_artifacts import (
    current_artifact,
    uploaded_clause,
    visible_artifact_clause,
    visible_artifact_clause_joined,
)
from app.task_awaiting_response_parser import parse_response_options, question_prose
from app import playbook_engine, runner_places, task_prs
from app import fleet_verdicts, merge_lane_state, task_sections
from app.models.worktrees import SlotIntent
from app.task_sections import Section, StageCandidate, StopKind
from app.task_lifecycle import (
    AWAITING_RESPONSE,
    DONE,
    READY,
    SYSTEM_FAILED,
    transitions_from,
    _ANSWER_ACTIONABLE_STATES,
    _RUNNER_RUNNING_STATES,
)
from app.task_lookup import get_task_or_404
from app.task_manifest import INPUT_MANIFEST
from app import task_source
from app.task_run_metrics import RunCost
from app.task_tags import TagRefOut
from app.tenancy.labels import OrgRef, org_ref_of, org_refs
from app.routes.tasks.policy import INTERVIEWING_CHILD_PERSONAS, PERSONA_EPIC
from app.routes.tasks.qa_render import QAEntryOut, TaskQuestionOut


class AttachmentMetaOut(BaseModel):
    """One attachment's metadata — no bytes; those come from the agent-token content route.

    `task_id` is the OWNING task id (`_task_attachments_meta` unions a child's with its parent epic's);
    the content route 404s unless the path id matches the owner, so `materialize_attachments` uses it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    filename: str
    content_type: Optional[str]
    size_bytes: int


class PrerequisiteArtifactOut(BaseModel):
    """One PREREQUISITE stage's current artifact this persona expects as input — a staging instruction
    dispatch turns into a file on the runner's disk (handoff by materialised PATH, never inlined).

    `task_id` is the PREREQUISITE's id; `kind` labels the path and derives the deterministic basename
    (`app/dispatch_compose/input_staging.py`). NO FILENAME FIELD: the uploaded filename is runner-authored text and the
    path renders ABOVE the untrusted-content fence. `size_bytes` so a 200-byte stub input is visible in a log."""

    id: int
    task_id: int
    kind: str
    size_bytes: int


class TaskDispatchProjectOut(BaseModel):
    """The project's delivery columns a runner of ANY persona works by (the repo access levels policy): the
    same facts its prompt states. `project.get` is the onboard persona's licence, so an SDK coder reads these here."""

    integration_branch: str
    branch_prefix: str
    gate_quick_cmd: str
    gate_full_cmd: str
    push_enabled: bool


class TaskDispatchOut(BaseModel):
    """The composer's dispatch projection of one open Task (`task_dispatch_out`).

    `persona` is the sole dispatch discriminator; the rest is the DB-sourced content each prompt is
    built from. `prior_stage_output`/`prerequisite_recon_map`/`prerequisite_artifacts` are a
    PREREQUISITE sibling's; `attachments` is THIS Task's own uploads."""

    id: int
    # The project's identity: the composer reads the project card's columns by it, never by name (names are
    # unique per org, not fleet-wide).
    project_id: int
    state: str
    persona: str
    # Top-level vs epic child; the ONLY input to the composer's `epic` conditional flag
    # (`app/dispatch_compose/placeholders.py::_COND_FLAGS`). A `coder` composes byte-identically in both shapes.
    # NO DEFAULT: a constructor that forgets it must fail loudly, not compose every dispatch as top-level.
    parent_task_id: Optional[int]
    title: str
    body: str
    qa_trail: Optional[str]
    prior_stage_output: Optional[str]
    # The merge lane's verdict on this Task's last PR (`_task_merge_lane_verdict`): a reopened lane runner's brief.
    merge_lane_verdict: Optional[str] = None
    # A prior runner session ended with work still owed (`_task_prior_session_ended`). DEFAULTED FALSE:
    # the failure mode is an UNTRUE prompt, so a partial constructor falls back to the always-safe answer.
    prior_session_ended: bool = False
    autonomous: bool
    prerequisite_recon_map: Optional[list[dict[str, Any]]]
    # The `INPUT_MANIFEST` documents for this persona (`_task_prerequisite_artifacts`). NO DEFAULT: a
    # forgetful constructor must fail loudly, not dispatch an analysis stage with no inputs.
    prerequisite_artifacts: Optional[list[PrerequisiteArtifactOut]]
    attachments: Optional[list[AttachmentMetaOut]]
    # NOT-NULL ⇒ a Finish-filed follow-up (`app/dispatch_compose/task_prompt.py` renders its terms). The KEY, not a
    # boolean. NO DEFAULT: a doctrine block that silently never renders is invisible in a way a 500 is not.
    finish_follow_up_key: Optional[str]
    # Terminal-outcome evidence the runner's confirmed-idle settle reads: an `active` row already carrying
    # a terminal exits, one with neither posts `/fail`. The hook RE-POSTS the terminal from these,
    # and `task_settle.TERMINAL_REFERENCES` needs reason AND evidence for `no_pr` — the
    # evidence is the anti-spoof floor, so `outcome_no_pr_evidence` must be projected, not invented.
    outcome_summary: Optional[str] = None
    outcome_pr_url: Optional[str] = None
    outcome_no_pr_reason: Optional[str] = None
    outcome_no_pr_evidence: Optional[str] = None
    # Model pin; `app/dispatch_compose/compose.py` reads the unit through THIS projection only. NULL = pin nothing.
    model: Optional[str] = None
    # The project's delivery columns (`TaskDispatchProjectOut`). The composer reads the card by `project_id`
    # instead; this is the `task.get` answer's copy for an agent. Defaulted: only `task_dispatch_out` fills it.
    project: Optional[TaskDispatchProjectOut] = None


class StopOut(BaseModel):
    """Why a Stopped row stopped, in the words its row states (AC-6.2). PINNED: the console's row and task
    page read it. `text` is the question prose, the failure cause, or the wait's note; `by`/`at` the stop
    record's (a question or failure has no `by`; `at` is when it entered that state). For a parent placed
    by a child, `stage_*` name that child — withheld when the caller cannot read it."""

    kind: StopKind
    text: Optional[str]
    by: Optional[str]
    at: Optional[datetime]
    stage_task_id: Optional[int]
    stage_persona: Optional[str]
    stage_title: Optional[str]


class MemoryCountsOut(BaseModel):
    """How many memory units reached one task, per event kind (`project_memory.vocabulary.EVENT_KINDS`)."""

    injected: int = 0
    read: int = 0
    cited: int = 0
    followed: int = 0


def _memory_counts_out(counts: Optional[dict[int, dict[str, int]]], task_id: int) -> Optional[MemoryCountsOut]:
    """One row's `TaskOut.memory_counts` from a batched `memory_counts` map; None when no map was resolved."""
    return None if counts is None else MemoryCountsOut(**counts.get(task_id, {}))


class TaskPrOut(BaseModel):
    """One PR the task opened, in any of its project's repositories (`app/task_prs.py`); `repo_path` is `owner/name`."""

    repo_path: str
    pr_number: int
    pr_url: str
    state: Literal["open", "merged", "closed"]
    is_primary: bool


def _prs_out(pr_map: Optional[dict[int, list[TaskPr]]], task_id: int) -> list[TaskPrOut]:
    """One row's `TaskOut.prs` from a batched `task_prs.prs_by_task` map; empty when no map was resolved."""
    return [
        TaskPrOut(repo_path=r.repo_path, pr_number=r.pr_number, pr_url=r.pr_url, state=r.state,
                  is_primary=r.is_primary)
        for r in (pr_map or {}).get(task_id, [])
    ]


class RanOnOut(BaseModel):
    """A runner's node and slot, by name; `live` while the task holds that slot now."""

    node: str
    slot: int
    live: bool


class TaskMicrovmOut(BaseModel):
    """The running MicroVM a task's runner lives in — a VM task has no slot, so this is what its
    terminal opens: `POST /api/console/{project}/0/open?view=vm:<vm_id>`. `may_edit` is that console's two-way
    verdict for this caller (`authz.editable_task_vm_clause`)."""

    vm_id: UUID
    node: str
    may_edit: bool


class TaskLiveSessionOut(BaseModel):
    """The task's newest RUNNING agent session (`sessions` row, as Audit > Sessions lists it), so the task page
    names a live run even before, or without, `runner_session_id` (an `acp` run records its session there)."""

    #: `sessions.agent_session_id`, the agent CLI's own session id.
    id: str
    #: `agent_sessions.RUNTIMES`: `legacy` or `acp`.
    runtime: str
    started_at: datetime


class TemplateRefOut(BaseModel):
    """The template a task was started from: `id` while it exists (global or org), `name` kept after a delete."""

    id: Optional[UUID]
    name: str


class TaskOut(BaseModel):
    id: int
    project_id: int
    # The feature this task is filed in, or null ("No feature"). Always a feature of `project_id`.
    feature_id: Optional[UUID]
    # Which tenant owns this row — the console's org indicator (`apps/web/src/ui/OrgIndicator.tsx`; shape in
    # `app/tenancy/labels.py::OrgRef`). Optional though `tasks.org_id` is NOT NULL: `null` means the id
    # resolved to no label (and covers the rolling-deploy window). Nothing may substitute the default org on
    # the serving path: a confident wrong tenant is worse than an honest blank. No default: `_task_out` is
    # the sole constructor, so a forgetful variant is a loud TypeError.
    org: Optional[OrgRef]
    parent_task_id: Optional[int]
    is_plannable: bool
    title: str
    body: str
    state: str
    # SOLE UI-filter authority: a row is filtered from the default `/tasks` UI iff hidden=true. System
    # personas are created hidden; the filter reads only this column, never `persona`. See
    # models/tasks.py:Task.hidden.
    hidden: bool
    # Still being typed: an auto-saved row whose author has not pressed Save (models/tasks.py:Task.unsaved). The
    # editor needs it to keep offering Discard. `true` is only ever seen by the author or a superadmin:
    # `authz.visible_task_clause` hides the row from everyone else.
    unsaved: bool
    # Which tenant playbook a top-level row runs (`playbook_engine.playbook_out`), and the stage a row runs
    # (`playbook_engine.stage_out`); null on both runs today's engine.
    playbook: Optional[dict[str, Any]] = None
    playbook_stage: Optional[dict[str, Any]] = None
    # The soft-archive stamp (models/tasks.py:Task.archived_at), so the filter opt-in and the finished/unfinished
    # grouping need no second request. No default: `_task_out` is the sole constructor.
    archived_at: Optional[datetime]
    github_synced_at: Optional[datetime]
    awaiting_response_body: Optional[str]
    # Unified pipeline discriminator, NOT NULL on every row. See models/tasks.py:Task.persona.
    persona: str
    # Operator dispatch-order lever: higher = sooner. Nullable; a legacy row reads as normal (0). Drives
    # the dispatch worklist ordering (`_PRIORITY_ORDER`) and is surfaced so the UI can display + set it.
    priority: Optional[int]
    # Whether this task runs (or ran) in a git worktree or an empty folder slot:
    # `slot_allocation.task_has_repo`, so a later change to the project's repositories never relabels a task already
    # leased. The one field the console reads to hide branch/PR surfaces. No default: `_task_out` decides it.
    has_repo: bool
    # Terminal evidence: the closing PR URL + the runner's summary, written by its `/settle` call.
    # A `done` row with a null `outcome_pr_url` renders "⚠ unverified".
    outcome_summary: Optional[str]
    outcome_pr_url: Optional[str]
    # Every PR the task opened, one per repository it changed: primary first, then by opening.
    # `outcome_pr_url` stays the primary's; a task settled before `task_prs` existed has none here.
    prs: list[TaskPrOut] = Field(default_factory=list)
    # The no-PR terminal: why a row closed with no PR, and the proof behind it; both on the
    # operator's read, since an evidence floor nobody can see cannot be audited.
    outcome_no_pr_reason: Optional[str] = None
    outcome_no_pr_evidence: Optional[str] = None
    # Why a `system_failed` row failed: the reason its terminal writer stored (`end_attempt`, the epic
    # child-failure rollup), read back off `awaiting_response_body`. Null in every other state, where that
    # column is a question or stale. Detail only: `TaskListOut` excludes it.
    failure_reason: Optional[str] = None
    # Number of child tasks: lets the console tell a legitimately-decomposed parent from a childless
    # false-close WITHOUT fetching the detail. List + detail routes only; null on nested-child rows.
    child_count: Optional[int] = None
    # Done-child count, populated the same way as `child_count` (exact from loaded `children` on the
    # detail route, from the caller's grouped `_child_counts` on the list), so a COLLAPSED epic row can
    # render a real done/total progress bar. Null on nested-child rows, exactly like `child_count`.
    done_child_count: Optional[int] = None
    # The subtask whose open question the operator should answer first (the oldest open question among
    # non-archived children), or null. Display-only: the epic's own `state` is its children's completion
    # rollup, never their gate (`_roll_up_epic`), so without this the row reads "Running".

    # An id, not a flag: the collapsed row has no children to scan and the next move is to open the one
    # asking. Null on a leaf, when all gated children are invisible to the caller, on nested children, and
    # when the caller is unnamed (fail-closed). Batched via `_gated_child_ids` / `_asking_task_keys`.
    gated_child_id: Optional[int] = None
    # This row itself has a question outstanding: an `open` `task_questions` round, or the legacy
    # `awaiting_response` park with no answer in flight. `state` alone cannot say (a runner may ask without
    # parking).

    # THE SAME PREDICATE `gated_child_id` APPLIES TO A CHILD, from the one home (`_asking_task_keys`), so
    # the two levels of the board cannot disagree about the row they are both describing.
    has_open_question: bool = False
    # Structured options parsed from `awaiting_response_body` at read time (never persisted):
    # the ordered `**Options:**` choices when the latest question carries them, else null.
    awaiting_response_options: Optional[list[str]]
    created_at: datetime
    updated_at: datetime
    # Resolved display names, never the raw actor uuid or a stored label: renamed operators rename here too.
    # The label derives from the name pair, else the email (`identities.person_label`).
    created_by: Optional[str] = None
    # DETAIL + MUTATION ONLY — `TaskListOut` excludes this one, because no board row renders an updater.
    # `created_by` differs: the Activity Feed draws an Author per row, so the list route batches its
    # resolution (see `list_tasks`) and serves it.
    updated_by: Optional[str] = None
    # The addressee, as id and name (unlike the provenance pair): the edit form must round-trip the id, and
    # names may collide. The id discloses nothing the assignee picker (`GET /api/projects/{id}/members`)
    # does not. The name resolves through `_actor_label`.
    assigned_to_id: Optional[UUID] = None
    assigned_to: Optional[str] = None
    # Cron provenance: the id is the live cron (NULL once deleted, FK SET NULL), the name a durable snapshot
    # taken at creation. Empty on hand-created rows. See models/tasks.py:Task.created_by_cron_id.
    created_by_cron_id: Optional[UUID] = None
    created_by_cron_name: str = ""
    # Trigger provenance, the cron pair's shape (`models.Task.created_by_trigger_id`): the live trigger, and a
    # name kept after it is deleted. `trigger_budget_exceeded_usd` is the advisory budget flag the trigger's
    # health fold stamps on this Task's delivery (never a stop, D7); null when not over, and on list reads.
    # `trigger_budget_usd` is the trigger's budget it was over, set only beside the flag.
    created_by_trigger_id: Optional[UUID] = None
    created_by_trigger_name: str = ""
    # Automation provenance, the cron pair's shape (`models.Task.created_by_automation_id`). An epic's child
    # carries none of its own, so a single-row read shows its epic's (`_automation_provenance`).
    created_by_automation_id: Optional[UUID] = None
    created_by_automation_name: str = ""
    # Template provenance (`models.Task.template_name`): "From template: <name>", a link while `id` resolves.
    template: Optional[TemplateRefOut] = None
    trigger_budget_exceeded_usd: Optional[float] = None
    trigger_budget_usd: Optional[float] = None
    # Where the row came from (`app/task_source.py`): the stored code and its readable label. Never changes.
    source: str = task_source.CONSOLE
    source_label: str = task_source.SOURCE_LABELS[task_source.CONSOLE]
    # ── Autonomous-mode fields ────────────────────────────────────────────────────────────────────────
    # Per-field rationale in models/tasks.py:Task; surfaced here so the console can display/edit them.
    autonomous: bool
    sweep_opt_out: bool
    # Project memory: the row's own On/Off (null = Default), and READ-ONLY what its last dispatch resolved —
    # the arm and where it came from (`project_memory.switch.effective_memory`). Both arm fields are null
    # until a dispatch stamps them, so a pre-memory task carries no record.
    project_memory: Optional[bool] = None
    memory_arm: Optional[bool] = None
    memory_arm_source: Optional[str] = None
    # This task's memory units by event kind (`project_memory.events.memory_counts`). Null when the payload
    # resolved none (no session and no batched map), never a fabricated zero.
    memory_counts: Optional[MemoryCountsOut] = None
    runner_session_id: Optional[str]
    # The one-shot per-subtask Run stamp, READ-ONLY, so the console can tell the two `ready` sub-states
    # apart: plain `ready` is queued behind the 60s reconcile cadence, `ready` plus this stamp is hand-Run
    # and being claimed now. Cleared on the very next dispatch claim.
    run_requested_at: Optional[datetime]
    clarity_score: Optional[float]
    spec_verdict: Optional[str]
    critic_verdict: Optional[str]
    # Epic transition-mode, surfaced so the epic edit form can display + set it. See models/tasks.py:Task.
    transition_mode: str
    # This row's own model pin, so the form opens on the stored value. NULL is inherit
    # (`app/model_catalog.py`). Readable by any reader; writing is superadmin-only (`TaskUpdate.model`).
    model: Optional[str] = None
    # The vendor this task is bound to (`models.Task.bound_provider`): the operator's provider pin, or the
    # stamp of its first dispatch. NULL = unbound ("Any"). Written through the one agent picker (`tasks:edit`).
    bound_provider: Optional[str] = None
    # The SDK agent picked for this task (`models.Task.agent_id`), the picker's other half; NULL = none.
    agent_id: Optional[UUID] = None
    # The one account this task may run on (`models.Task.bound_account_id`); NULL = any. Set by create and update.
    bound_account_id: Optional[UUID] = None
    # The one node that may place this task (`models.Task.pinned_node_id`); NULL = any. Written by operators, and by task writers while the org's node picker is on (`placement_grants.node_pin_enabled`).
    pinned_node_id: Optional[UUID] = None
    # Why this task was never interviewed despite Full Auto being cleared — one operator sentence, or NULL
    # (nearly every row). Computed by `_interview_skipped_reason`, never persisted.
    # Served as prose because the wording needs the provider's label and `can_interview`, which no screen
    # may branch on (`types.ts`). NULL never means "the interview happened": unresolved callers (nested
    # children) also serialise NULL, so only the detail page renders it.
    interview_skipped_reason: Optional[str] = None
    # Present (possibly empty) only on the detail route when a Task has children; omitted elsewhere.
    children: Optional[list["TaskOut"]] = None
    # The board section (`app/task_sections.py`, the one home of the mapping): a parent is placed by its
    # non-archived children, so an epic whose stage is asking is `stopped`. No default: `_task_out` is the
    # sole constructor.
    section: Section
    # Why the row is Stopped (null outside Stopped).
    stop: Optional[StopOut] = None
    # On a nested child only: its position in the epic's pipeline order (`task_sections.stage_ranks` —
    # dependency depth, then `phase_index`, then id). Null elsewhere.
    stage_rank: Optional[int] = None
    # For a nominally `ready` epic child still gated behind an unmet prerequisite, that prerequisite's
    # `persona` ("waiting on <stage>"); NULL otherwise. Computed, batched via `_waiting_on_personas`.
    waiting_on_persona: Optional[str] = None
    # For a `ready` (non-epic) row, why it is not dispatching — a pending org or a full Agents cap
    # (`app/org_limits.py::wait_reason`, e.g. "Waiting: organization runner limit (1/1)"), else the
    # dispatcher's own fresh last decline (`app/dispatch_wait.py`, e.g. "Waiting: no free worktree");
    # NULL otherwise. Batched per org via `org_limits.wait_reasons`.
    wait_reason: Optional[str] = None
    # On the node wait (`wait_reason` is `org_limits.AWAITING_NODE`) of a single-row read: False when Add node is the
    # remedy (`org_limits.awaits_first_node`: no node and none on its way), so every viewer gets it, not only the
    # task's own org. NULL otherwise.
    org_has_node: Optional[bool] = None
    # How many of this row's prerequisites are unmet (usually 0): a non-zero count reads "Waiting" rather
    # than Queued or Running. Same semantics as the gate itself, so badge and dispatcher agree; batched via
    # `_unmet_prerequisite_counts`.
    unmet_prerequisite_count: int = 0
    # True between an operator's answer and the node acking `deliver_answer`: the row is still
    # `awaiting_response` but nothing is left to ask, so renders read "Delivering". Batched via
    # `task_sections.answer_delivering_keys`, the single home of "in flight"; it also places the row In progress
    # unless a newer round is open.
    answer_delivering: bool = False
    # While `answer_delivering`: the node's latest reason the delivery failed (redacted, `redact_node_detail`)
    # and how many times running it failed so (the intent's `refusals`). Null/0 when no attempt failed yet.
    # Batched via `_answer_delivery_failures`.
    answer_delivery_failure: Optional[str] = None
    answer_delivery_retries: int = 0
    # The Task's Q&A resolutions, from the durable note store. DETAIL route only — null on the list
    # route and on nested children, so the list stays lightweight.
    qa_entries: Optional[list[QAEntryOut]] = None
    # Multi-round Q&A history: the ordered `task_questions` rows with each round's derived 1-based index,
    # chosen resolution and status. Detail route + Needs-attention view only, exactly like `qa_entries`.
    questions: Optional[list[TaskQuestionOut]] = None
    # The FULL body of this Task's own newest settled-output note — the complete artifact an analysis
    # child settled, not the one-line `outcome_summary` header. Read via `_latest_settled_output_note`,
    # which accepts either note kind (`_SETTLED_OUTPUT_NOTE_KINDS`). Detail-only; null when this Task
    # never settled one.
    stage_output: Optional[str] = None
    # Where this task's runner runs, or last ran (`app/runner_places.py`); null when it never launched. A list
    # read resolves only a `ready` row's held slot (a fresh lease, or one kept from an earlier attempt) and leaves the
    # rest null.
    ran_on: Optional[RanOnOut] = None
    # The running MicroVM this task's runner lives in, whose terminal the task page opens. Detail-only; null for a
    # slot-backed run, a VM not running yet, or a list read.
    microvm: Optional[TaskMicrovmOut] = None
    # The node holding this task's newest MicroVM, named exactly when "Start fresh elsewhere" would be accepted
    # (`microvm_lifecycle.fresh_start_refusal`) and the task is not running. Detail-only; null otherwise.
    vm_hold_node: Optional[str] = None
    # This task's newest running agent session (`_live_session`). Detail-only; null when none runs, or a list read.
    live_session: Optional[TaskLiveSessionOut] = None
    # When this `active` task's current run started: its newest RUNNING agent session's `started_at` (the same row
    # `live_session` names), so a board card can show the run's age. The list fills it with one batched
    # read over the page's active rows (`routes/tasks/listing.py::_run_started_ats`); the detail read takes it off
    # `live_session`. Null for any other state, or an active task with no running session yet.
    run_started_at: Optional[datetime] = None
    # Staleness marker, NOT a new lifecycle state: set on this Task's own significant edit or
    # BFS-propagated from an upstream prerequisite's edit/re-run (`_propagate_staleness`); cleared only by
    # a successful `retry` of THIS task. Both null for a never-stale row — the overwhelming default.
    stale_reason: Optional[str] = None
    stale_since: Optional[datetime] = None
    # Phase-DAG: planner-authored 1-based ordinal + semantic name assigned to a `coder` child by
    # plan's append. Both null for a non-phased/legacy child or any non-`coder` Task.
    phase_index: Optional[int] = None
    phase_name: Optional[str] = None
    # The dedup key of a Finish-filed `coder` child (models/tasks.py:Task.finish_follow_up_key), so the epic view
    # groups it under Finish. No default: a forgetful constructor would silently re-file every follow-up
    # under Build.
    finish_follow_up_key: Optional[str]
    # The external issue pair (models/tasks.py:Task.external_task_system). On the list row too: two short ids a
    # tracker chip renders. No default: `_task_out` is the sole constructor.
    external_task_system: Optional[str]
    external_task_id: Optional[str]
    # Terminal-phase substrate: the epic's own persisted aggregate — `finish_report`
    # (verify/acceptance/time/tokens/benchmarks) plus the top-level `tokens_in`/`tokens_out`/`total_seconds`
    # mirrors `execute_finish_phase` writes. Null for a non-epic Task; the console renders an empty state.
    metrics: Optional[dict[str, Any]] = None

    # Per-row transition enablement: the transition names legal from THIS row's state, from
    # `transitions_from` — genuine `sources` membership only, never `idempotent_noop` tolerance. STATE-only
    # by design; persona-specific refusals are route pre-checks. Empty for a cutover-orphaned state.
    available_transitions: list[str] = []

    # Per-row write enablement for the caller's grants: true when `authz.authored_draft` holds,
    # so a `tasks:create`-without-`tasks:edit` principal may edit, tag, attach to, re-wire and delete these
    # rows, and start none.

    # Without it the console, gated on the flat `tasks:edit` key (`hooks/useTaskAccess.ts`), would render a
    # draft read-only that the API would accept a PUT for.

    # False, never null, where it cannot be computed (nested child, unnamed caller, agent surface).
    # Fail-closed: a withheld control is one refresh away; a wrongly offered one is a 403.
    authored_draft: bool = False

    # May this caller set/clear `hidden` (`_may_set_hidden`, the rule `_apply_hidden` enforces)? The edit
    # form's Hidden checkbox renders only when true. False, never null, where no caller is named.
    may_set_hidden: bool = False

    # True when `authz.editable_task_clause(principal)` admits this row for this caller — the clause half
    # of `can_access(mode=WRITE)` that every task write door probes. Not the key half (the client checks
    # `tasks:edit` per scope) nor the author-draft fallback (`authored_draft`).

    # Reads and writes use different keys on the row's project, so a caller whose `tasks:edit` came from
    # another scope would be offered controls the guard refuses; this lets the console withhold them.

    # Three values, all read (`hooks/useTaskAccess.ts` vetoes on `=== false` alone): TRUE admits; FALSE is
    # the clause refusing, including for an unnamed caller; NONE is not computed (no session) and must not
    # read as a refusal, or every nested child would lose Edit/Start/Delete. True for every row for a
    # superadmin.
    may_edit: Optional[bool] = None

    # Active-group tags, each with its group so a collapsed row renders chips alone (`app/task_tags.py`).
    # Defaults to `[]`: precomputed by the caller, and only the list branches and the detail route pass them.
    tags: list[TagRefOut] = []

    # Five-star rating as the two numbers a collapsed row renders; the full contract is
    # `app/routes/task_ratings.py`. NULL average at zero votes, never 0.0 (unreachable, and would read as
    # unrated); `rating_count` is an int since 0 is honest. Defaulted: only the listing/detail sites compute
    # it. Not excluded on `TaskListOut` — the board is its primary surface.
    rating_average: float | None = None
    rating_count: int = 0

    # This Task's subtree run cost, from the same fold and pricing as `GET /api/tasks/{id}/run-metrics`
    # (`app/task_run_metrics.py::run_aggregates`); a decimal string, and `cost_status` is that field's
    # vocabulary (see `TaskRunMetricsOut.cost_usd`). NULL means this payload resolved no cost, so
    # `unavailable` is unreachable here. It defaults to null because the board spreads mutation responses
    # over the row (`replaceTaskEverywhere`); any other default would blank the cell. Never read null as $0.
    cost_usd: Optional[str] = None
    cost_status: Optional[Literal["measured", "partial", "unavailable"]] = None
    # True when `cost_usd` includes a cost an agent reported itself rather than one priced from tokens
    # (`RunCost.agent_reported`; Efficiency's `cost_source: agent_reported`). NULL exactly when `cost_status` is.
    cost_agent_reported: Optional[bool] = None
    # This Task's subtree token total, every class (`tokens_in + tokens_out`), from the same scope walk as
    # `cost_usd` (`app/task_run_metrics.py::run_aggregates`). Live while a session runs: the node's usage post
    # lands within a few minutes. NULL means nothing in the subtree was captured, never read as 0 (a captured
    # zero is 0). Defaults to null for `cost_usd`'s reason: the board spreads mutation responses over the row.
    tokens_total: Optional[int] = None


class TaskListOut(TaskOut):
    """The row shape `GET /api/tasks` serialises: `TaskOut` minus the fields no LIST consumer reads.

    Unconditional: the triage tool dedups via `?q=`, so nothing wants a body here. `body` and
    `awaiting_response_body` were roughly half a list response. `exclude=True` makes each absent rather
    than a misreadable null; the detail route and its nested `children` keep them all.

    The provenance pair is split: `created_by` is resolved (the Activity Feed's Author column), while
    `updated_by` is excluded because this route resolves none and its null would read as "no actor"."""

    body: str = Field(default="", exclude=True)
    # The pending question's full prose — rendered by the detail page's GateForm, never by a list row.
    # Its DERIVED `awaiting_response_options` stays: that one is a short parsed list the row badge reads.
    awaiting_response_body: Optional[str] = Field(default=None, exclude=True)
    metrics: Optional[dict[str, Any]] = Field(default=None, exclude=True)
    # Same argument, same idiom: no-PR evidence is a runner's reproduction transcript, unbounded prose that
    # no collapsed row can show. It rides the DETAIL read, where the Outcome panel renders it.
    outcome_no_pr_evidence: Optional[str] = Field(default=None, exclude=True)
    # Unbounded prose for the detail page's failure banner; no board row renders it.
    failure_reason: Optional[str] = Field(default=None, exclude=True)
    # `created_by` is deliberately NOT excluded — see the docstring's provenance-split note.
    updated_by: Optional[str] = Field(default=None, exclude=True)
    # Both assignee fields, on `updated_by`'s truth argument: no board row renders an assignee and this
    # route resolves no assignee ids, so an unexcluded pair would serialise "not resolved" as "nobody".
    assigned_to_id: Optional[UUID] = Field(default=None, exclude=True)
    assigned_to: Optional[str] = Field(default=None, exclude=True)
    # `org` is deliberately not excluded: the board is the org indicator's primary surface, and the detail
    # page must not name a tenant the board denied. Resolved in one batched query.


def _actor_label(actor_id: Optional[UUID], names: dict[UUID, str]) -> Optional[str]:
    """One provenance id → the label a console cell renders, given an already-resolved name map.

    A NULL actor reads as NULL, never "System": a null `tasks.created_by_id` means no recorded creator
    (migration 0096), as `task_ancestry.creator_user_id` and `notify_targets._task_owner` read it.
    "System" is reached by resolution only — machine writes stamp `SYSTEM_ACTOR_ID`, whose row carries
    `SYSTEM_ACTOR_NAME`. An id that does not resolve also reads as NULL."""
    if actor_id is None:
        return None
    return names.get(actor_id)


def _org_wait_reasons(session: Session, rows) -> dict[int, str]:
    """`org_limits.wait_reasons` for those `rows` that can wait on their org: `ready`, and never an epic
    (it is never dispatched). Keyed by task id."""
    return org_limits.wait_reasons(session, [t for t in rows if t.state == READY and t.persona != PERSONA_EPIC])


def _org_wait_reason_for(
    task: Task, reasons: Optional[dict[int, str]], *, gated: bool,
) -> Optional[str]:
    """This row's `TaskOut.wait_reason`. None while a prerequisite still gates it (`gated`): that is what
    it waits on first, and the badge already names it. A Run (`run_requested_at`) bypasses that gate
    (`dispatchable_tasks`), so its row is the dispatcher's and shows the dispatcher's reason — never a bare
    "Starting" while it waits."""
    if (gated and task.run_requested_at is None) or not reasons or task.state != READY:
        return None
    return reasons.get(task.id)


def _trigger_budget_exceeded(session: Optional[Session], task: Task) -> tuple[Optional[float], Optional[float]]:
    """The budget flag of a trigger-filed Task, read off its delivery row, and the trigger's budget it was over:
    `(cost, budget)`, or `(None, None)` — also when the budget has since been removed or raised past the cost.
    Single-row reads only: a many-row caller passes no session, and an
    unfiled row never queries."""
    if session is None or task.created_by_trigger_id is None:
        return None, None
    row = session.execute(
        select(TriggerDelivery.budget_exceeded_usd, TaskTrigger.budget_usd)
        .outerjoin(TaskTrigger, TaskTrigger.id == TriggerDelivery.trigger_id)
        .where(TriggerDelivery.task_id == task.id, TriggerDelivery.budget_exceeded_usd.is_not(None))
        .limit(1)
    ).first()
    if row is None:
        return None, None
    cost, budget = row
    # The flag is read against the trigger's budget NOW: one raised past the cost, or removed, no longer flags.
    if budget is None or cost <= budget:
        return None, None
    return float(cost), float(budget)


def _automation_provenance(session: Optional[Session], task: Task) -> tuple[Optional[UUID], str]:
    """The automation that filed this row, else the one that filed its parent epic: a child is filed by the
    pipeline, not the automation, and stays unstamped so the fire engine's overlap count (`_task_scope`)
    sees only the epic. Single-row reads only, like `_trigger_budget_exceeded`; a many-row caller passes no
    session and gets the row's own."""
    if task.created_by_automation_id is not None or task.created_by_automation_name:
        return task.created_by_automation_id, task.created_by_automation_name
    if session is None or not task.parent_task_id:
        return None, ""
    parent = session.execute(
        select(Task.created_by_automation_id, Task.created_by_automation_name)
        .where(Task.id == task.parent_task_id, Task.org_id == task.org_id)
    ).first()
    if parent is None:
        return None, ""
    return parent.created_by_automation_id, parent.created_by_automation_name


def _task_out(
    task: Task,
    children: Optional[list[Task]] = None,
    child_count: Optional[int] = None,
    done_child_count: Optional[int] = None,
    qa_entries: Optional[list[QAEntryOut]] = None,
    session: Optional[Session] = None,
    waiting_on_persona: Optional[str] = None,
    unmet_prerequisite_count: Optional[int] = None,
    stage_output: Optional[str] = None,
    ran_on: Optional[RanOnOut] = None,
    microvm: Optional[TaskMicrovmOut] = None,
    vm_hold_node: Optional[str] = None,
    live_session: Optional[TaskLiveSessionOut] = None,
    # The caller's batched run start for an `active` row (`TaskOut.run_started_at`); `_task_out` never queries
    # for it. None = not resolved, or no running session.
    run_started_at: Optional[datetime] = None,
    questions: Optional[list[TaskQuestionOut]] = None,
    tags: Optional[list[TagRefOut]] = None,
    actor_names: Optional[dict[UUID, str]] = None,
    answer_delivering: Optional[bool] = None,
    # The caller's batched `_answer_delivery_failures` entry for this row; None derives it here when a session
    # is passed and the row is delivering.
    answer_delivery_failure: Optional[tuple[str, int]] = None,
    gated_child_id: Optional[int] = None,
    has_open_question: Optional[bool] = None,
    authored_draft: Optional[bool] = None,
    may_edit: Optional[bool] = None,
    # The caller's batched rating for this row, as one pair (so no count without an average); `_task_out`
    # never queries for it. None = not resolved, serialising as unrated.
    rating: Optional[tuple[float, int]] = None,
    # The caller's batched subtree cost for this row, one pair so a status never comes without a figure;
    # `_task_out` never queries for it (N+1). None = not resolved, and also what a resolving caller passes
    # for an id `run_aggregates` left out of its cost map.
    cost: Optional[RunCost] = None,
    # The nested children's costs, keyed by child id — unlike `tags` and `rating`: expanding an epic renders
    # these children, and a `coder` child has a real cost. Resolved in the same `run_aggregates` call as
    # the root.
    child_costs: Optional[dict[int, RunCost]] = None,
    # The subtree token total and the children's, from that same `run_aggregates` call; `cost`'s and
    # `child_costs`' terms (None = not resolved, or nothing captured).
    tokens: Optional[int] = None,
    child_tokens: Optional[dict[int, int]] = None,
    # Org labels for this row and its children, precomputed like `actor_names`. A caller with a session and
    # no map gets them resolved here (one small `IN (…)`): required, since the board spreads mutation
    # responses over the row and `org=null` would blank the indicator.
    org_labels: Optional[dict[UUID, OrgRef]] = None,
    principal: Principal | None = None,
    # The caller's batched placement (`_list_placements`); None derives it here (`_placement`).
    placement: Optional[tuple[Section, Optional[StopOut]]] = None,
    # This row's position among its epic's stages, passed by the parent for a nested child.
    stage_rank: Optional[int] = None,
    # The caller's batched `org_limits.wait_reasons` map (task id -> reason); None derives it here when a
    # session is passed, for this row and its children.
    org_wait_reasons: Optional[dict[int, str]] = None,
    # The caller's batched `memory_events.memory_counts` map; None derives it here when a session is passed,
    # for this row and its children in one grouped query.
    memory_count_map: Optional[dict[int, dict[str, int]]] = None,
    # The caller's batched `task_prs.prs_by_task` map; None derives it here when a session is passed, for this
    # row and its children in one select.
    pr_map: Optional[dict[int, list[TaskPr]]] = None,
    # The model to build: the list routes pass `TaskListOut`, built here directly rather than dumped and
    # re-validated per row.
    out_model: type[TaskOut] = TaskOut,
) -> TaskOut:
    if pr_map is None and session is not None:
        pr_map = task_prs.prs_by_task(session, [task.id, *(c.id for c in children or [])])
    if org_wait_reasons is None and session is not None:
        org_wait_reasons = _org_wait_reasons(session, [task, *(children or [])])
    if memory_count_map is None and session is not None:
        # The community edition has no project memory: no events.
        memory_count_map = {}
        if edition_features.enabled("project_memory"):
            from app.project_memory import events as memory_events

            memory_count_map = memory_events.memory_counts(session, [task.id, *(c.id for c in children or [])])
    # `waiting_on_persona` is only computed when the caller passes both `children` and `session`. ONE batched
    # query for the whole child list (`_waiting_on_personas`), surfaced only on a child that is itself
    # `ready`.
    waiting_map: dict[int, str] = (
        _waiting_on_personas([c.id for c in children], session) if children and session is not None else {}
    )
    # `unmet_prerequisite_count` covers this row as well as its children. One grouped query, only when a
    # session is passed; the list routes batch their own page and pass each value in.

    # Optional because 0 is a real answer. A caller passing neither gets 0 — so every response carrying a
    # live row passes one, or a fabricated 0 would clear the badge.
    gate_map: dict[int, int] = (
        _unmet_prerequisite_counts([task.id, *(c.id for c in children or [])], session)
        if session is not None
        else {}
    )
    # Narrowed to `awaiting_response` rows, and skipped entirely when there are none (nearly always). The list
    # routes pass the answer in, as for `gate_map`.
    delivering_candidates = [
        t.id for t in (task, *(children or [])) if t.state == AWAITING_RESPONSE
    ]
    delivering_ids: set[int] = (
        _answer_delivering_ids(delivering_candidates, session)
        if session is not None and delivering_candidates
        else set()
    )
    # The caller's answer for this row wins; else the batched read. False when neither.
    own_delivering = answer_delivering if answer_delivering is not None else task.id in delivering_ids
    if answer_delivery_failure is None and own_delivering and session is not None:
        answer_delivery_failure = _answer_delivery_failures([task.id], session).get(task.id)
    # The epic's "a subtask is asking" rollup, this row only (nested children get no session, hence null).

    # Fail-closed: computed only for a resolved principal and scoped to what it may read
    # (`visible_task_clause(None)` is `true()`); an unnamed caller gets `{}`.
    gated_map: dict[int, int] = (
        _gated_child_ids(session, [task.id], principal)
        if session is not None and principal is not None
        else {}
    )
    # This row only, on the same fail-closed condition: the field decides whether a write control is
    # offered. The list routes pass it in.
    authored_draft_ids: set[int] = (
        _authored_draft_ids([task.id], session, principal)
        if authored_draft is None and session is not None and principal is not None
        else set()
    )
    # This row and its children in one probe: nested `_task_out` gets no session, and an unasked child must
    # serialise `None`, never `False`. The row is skipped when `may_edit` was passed.
    may_edit_ids: set[int] = (
        _may_edit_ids(
            [*([task.id] if may_edit is None else []), *(c.id for c in children or [])],
            session, principal,
        )
        if session is not None and principal is not None
        else set()
    )
    # This row's own "I am asking", plus its children's, from one batched probe on the rollup's fail-closed
    # rule. The list route passes the answer in.
    asking_keys: dict[int, datetime] = (
        _asking_task_keys(
            session, [(t.id, t.state, t.updated_at) for t in (task, *(children or []))]
        )
        if session is not None and principal is not None
        else {}
    )
    # Same two-source rule as `own_delivering`.
    own_asking = has_open_question if has_open_question is not None else task.id in asking_keys
    # Rows whose delivering answer ends their question: a newer open round still asks (`stop_kind_of`).
    answered_ids = {i for i in delivering_ids if i not in asking_keys and i != task.id} | (
        {task.id} if own_delivering and not own_asking else set()
    )
    # Provenance labels: `actor_names` is the batched path, but unlike `tags` a caller with a session and no
    # map gets the two ids resolved here, so a cell never flips to an em-dash on a mutation response.

    # An empty map is the honest no-session answer: `_actor_label` renders unresolved and null alike, so
    # every call site hands in a session or a map (`TaskListOut` serves `created_by`).

    # The children's ids ride this same query, both creator and updater (deduped; a detail read), so child
    # rows carry labels without a lookup each.
    actor_map: dict[UUID, str] = (
        actor_names
        if actor_names is not None
        else (
            actors.actor_names(
                session,
                (
                    task.created_by_id,
                    task.updated_by_id,
                    # The addressee rides the same deduped read — usually the creator, so usually
                    # free — and the children's too, so an expanded epic's child rows carry theirs.
                    task.assigned_to_id,
                    # The stop record's actor, the row's and its children's (`StopOut.by`).
                    task.stopped_by_id,
                    *(c.created_by_id for c in children or []),
                    *(c.updated_by_id for c in children or []),
                    *(c.assigned_to_id for c in children or []),
                    *(c.stopped_by_id for c in children or []),
                ),
                # A response for this reader: an acting operator reads their colleagues by name (`actors.actor_names`).
                name_acting=True,
            )
            if session is not None
            else {}
        )
    )
    # This row's tenant and its children's, on `actor_map`'s two-source rule, in one query. Each child
    # carries its own org: inheriting the parent's label could show a confident wrong tenant.
    org_map: dict[UUID, OrgRef] = (
        org_labels
        if org_labels is not None
        else (
            org_refs(session, [t.org_id for t in (task, *(children or []))])
            if session is not None
            else {}
        )
    )
    # The children's pipeline positions, from the edges when a session can read them.
    child_ranks: dict[int, int] = (
        (
            task_sections.stage_ranks_for(session, [task.id])
            if session is not None
            else task_sections.stage_ranks([(c.id, c.phase_index) for c in children], ())
        )
        if children
        else {}
    )
    section, stop = (
        placement if placement is not None
        else _placement(
            task, children, child_ranks, actor_map, session, principal, answered_ids,
        )
    )
    budget_exceeded, trigger_budget = _trigger_budget_exceeded(session, task)
    automation_id, automation_name = _automation_provenance(session, task)
    wait_reason = _org_wait_reason_for(
        task, org_wait_reasons,
        gated=bool(waiting_on_persona) or bool(
            unmet_prerequisite_count if unmet_prerequisite_count is not None else gate_map.get(task.id, 0)
        ),
    )
    return out_model(
        id=task.id,
        project_id=task.project_id,
        feature_id=task.feature_id,
        org=org_ref_of(org_map, task.org_id),
        parent_task_id=task.parent_task_id,
        is_plannable=task.is_plannable,
        title=task.title,
        body=task.body,
        state=task.state,
        hidden=task.hidden,
        unsaved=task.unsaved,
        playbook=playbook_engine.playbook_out(task.playbook),
        playbook_stage=playbook_engine.stage_out(task.playbook_stage),
        archived_at=task.archived_at,
        github_synced_at=task.github_synced_at,
        awaiting_response_body=task.awaiting_response_body,
        persona=task.persona,
        priority=task.priority,
        # The task's own snapshot needs no read; NULL (never dispatched) reads its project, an identity-map hit
        # after the first row of a page.
        has_repo=slot_allocation.task_has_repo(session or object_session(task), task),
        outcome_summary=task.outcome_summary,
        outcome_pr_url=task.outcome_pr_url,
        outcome_no_pr_reason=task.outcome_no_pr_reason,
        outcome_no_pr_evidence=task.outcome_no_pr_evidence,
        failure_reason=task.awaiting_response_body if task.state == SYSTEM_FAILED else None,
        awaiting_response_options=parse_response_options(task.awaiting_response_body),
        created_at=task.created_at,
        updated_at=task.updated_at,
        created_by=_actor_label(task.created_by_id, actor_map),
        updated_by=_actor_label(task.updated_by_id, actor_map),
        assigned_to_id=task.assigned_to_id,
        assigned_to=_actor_label(task.assigned_to_id, actor_map),
        created_by_cron_id=task.created_by_cron_id,
        created_by_cron_name=task.created_by_cron_name,
        created_by_trigger_id=task.created_by_trigger_id,
        created_by_trigger_name=task.created_by_trigger_name,
        created_by_automation_id=automation_id,
        created_by_automation_name=automation_name,
        template=(
            None if task.template_name is None
            else TemplateRefOut(id=task.template_id or task.org_template_id, name=task.template_name)
        ),
        trigger_budget_exceeded_usd=budget_exceeded,
        trigger_budget_usd=trigger_budget,
        source=task.source,
        source_label=task_source.source_label(task.source),
        autonomous=task.autonomous,
        # Derived from (`autonomous`, `bound_provider`) only. The one field here that can query: one
        # `session.get` for an interviewing epic child on a single-row route; many-row callers pass no session.
        interview_skipped_reason=_interview_skipped_reason(session, task),
        sweep_opt_out=task.sweep_opt_out,
        project_memory=task.project_memory,
        memory_arm=task.memory_arm,
        memory_arm_source=task.memory_arm_source,
        memory_counts=_memory_counts_out(memory_count_map, task.id),
        prs=_prs_out(pr_map, task.id),
        runner_session_id=task.runner_session_id,
        run_requested_at=task.run_requested_at,
        clarity_score=task.clarity_score,
        spec_verdict=task.spec_verdict,
        critic_verdict=task.critic_verdict,
        transition_mode=task.transition_mode,
        model=task.model,
        bound_provider=task.bound_provider,
        agent_id=task.agent_id,
        bound_account_id=task.bound_account_id,
        pinned_node_id=task.pinned_node_id,
        children=[
            # Still no session (no lookup per child); it gets the map, which skips the child's own fallback. Child
            # rows need labels for the Activity Feed's expanded epic.
            _task_out(
                c,
                waiting_on_persona=waiting_map.get(c.id) if c.state == READY else None,
                unmet_prerequisite_count=gate_map.get(c.id, 0),
                actor_names=actor_map,
                org_wait_reasons=org_wait_reasons or {},
                # From this call's one batched read, since the child has no session to derive it.
                answer_delivering=c.id in delivering_ids,
                # Same batched read, same reason: the child row an epic's header points at must show its
                # own `?`, and with no session it could not answer for itself.
                has_open_question=c.id in asking_keys,
                # From the parent's one `_may_edit_ids` probe, passed explicitly: falling through would answer False, a
                # refusal the clause never gave. With no session up here there was no probe, so `None`.
                may_edit=(c.id in may_edit_ids) if session is not None else None,
                # From the caller's one `run_aggregates` call; absent from the map serialises the null pair.
                cost=(child_costs or {}).get(c.id),
                tokens=(child_tokens or {}).get(c.id),
                # The map, not the child's own label, so the child's fallback is skipped (resolved in the parent's read).
                org_labels=org_map,
                stage_rank=child_ranks.get(c.id),
                # From this call's one grouped read (the child has no session); null when the parent resolved none.
                memory_count_map=memory_count_map,
                pr_map=pr_map,
            )
            for c in children
        ] if children is not None else None,
        section=section,
        stop=stop,
        stage_rank=stage_rank,
        # Exact when children are loaded (detail route), else the caller's grouped count. Null on a child.
        child_count=len(children) if children is not None else child_count,
        done_child_count=(
            sum(1 for c in children if c.state == DONE) if children is not None else done_child_count
        ),
        # Same two-source rule as `unmet_prerequisite_count`; null when neither. Not re-derived from loaded
        # `children`, which lack their open rounds.
        gated_child_id=(
            gated_child_id if gated_child_id is not None else gated_map.get(task.id)
        ),
        # Same two-source rule as `gated_child_id` above, and DELIBERATELY the same batched read, so a
        # response can never carry an epic pointing at a child whose own row denies it is asking.
        has_open_question=own_asking,
        # Detail-only: the caller passes these on the detail route, never on the list or nested children.
        qa_entries=qa_entries,
        questions=questions,
        waiting_on_persona=waiting_on_persona,
        wait_reason=wait_reason,
        org_has_node=(
            not org_limits.awaits_first_node(session, task.org_id)
            if session is not None and task.org_id is not None and wait_reason == org_limits.AWAITING_NODE
            else None
        ),
        unmet_prerequisite_count=(
            unmet_prerequisite_count
            if unmet_prerequisite_count is not None
            else gate_map.get(task.id, 0)
        ),
        answer_delivering=own_delivering,
        answer_delivery_failure=(answer_delivery_failure[0] or None) if own_delivering and answer_delivery_failure else None,
        answer_delivery_retries=answer_delivery_failure[1] if own_delivering and answer_delivery_failure else 0,
        # Precomputed by the caller (`tag_refs_for` is a batch loader; per row would be N+1). `None` and `[]`
        # both serialise `[]`.
        tags=tags or [],
        stage_output=stage_output,
        ran_on=ran_on,
        microvm=microvm,
        vm_hold_node=vm_hold_node,
        live_session=live_session,
        run_started_at=run_started_at,
        stale_reason=task.stale_reason,
        stale_since=task.stale_since,
        phase_index=task.phase_index,
        phase_name=task.phase_name,
        finish_follow_up_key=task.finish_follow_up_key,
        external_task_system=task.external_task_system,
        external_task_id=task.external_task_id,
        metrics=task.metrics,
        available_transitions=_available_transitions_for(task),
        authored_draft=(
            authored_draft if authored_draft is not None else task.id in authored_draft_ids
        ),
        may_set_hidden=principal is not None and _may_set_hidden(task, principal),
        # The caller's batched answer, else this call's probe, else `None` (not computed). Only a probe that ran
        # may say False.
        may_edit=(
            may_edit if may_edit is not None
            else (task.id in may_edit_ids) if session is not None
            else None
        ),
        # Precomputed by the caller (`rating_aggregates` is a batch loader). Unresolved leaves the unrated
        # defaults; unpacked from one pair so no count comes without an average.
        rating_average=rating[0] if rating is not None else None,
        rating_count=rating[1] if rating is not None else 0,
        # Precomputed like `rating`; one pair, so an unresolved payload leaves both null.
        cost_usd=cost.usd if cost is not None else None,
        cost_status=cost.status if cost is not None else None,
        cost_agent_reported=cost.agent_reported if cost is not None else None,
        tokens_total=tokens,
    )


def _stop_out(
    kind: StopKind, source: Task, actor_map: Mapping[UUID, str], *, stage: bool, visible: bool = True,
) -> StopOut:
    """`StopOut` for a row placed in Stopped by `source` — itself, or (`stage=True`) the child that stopped
    it. A child the caller cannot read names nothing: only the kind survives."""
    if not visible:
        return StopOut(kind=kind, text=None, by=None, at=None,
                       stage_task_id=None, stage_persona=None, stage_title=None)
    if kind == "question":
        text, by, at = question_prose(source.awaiting_response_body) or None, None, source.github_synced_at
    elif kind == "failed":
        # The same column `TaskOut.failure_reason` reads for a `system_failed` row.
        text, by, at = source.awaiting_response_body or None, None, source.github_synced_at
    else:
        # A stop with no record (an idle kill, an epic cascade) names no one; its time is the edge's.
        text = source.stop_note if kind == "waiting_on_customer" else None
        by = _actor_label(source.stopped_by_id, actor_map)
        at = source.stopped_at or source.github_synced_at
    return StopOut(
        kind=kind, text=text, by=by, at=at,
        stage_task_id=source.id if stage else None,
        stage_persona=source.persona if stage else None,
        stage_title=source.title if stage else None,
    )


def _placement(
    task: Task,
    children: Optional[list[Task]],
    child_ranks: Mapping[int, int],
    actor_map: Mapping[UUID, str],
    session: Optional[Session],
    principal: Principal | None,
    delivering: set[int],
) -> tuple[Section, Optional[StopOut]]:
    """One row's section and stop (`task_sections.place`). Loaded `children` are read directly; with none
    loaded, a session reads the stopping child itself (`stopping_children`), and without either the row is
    placed on its own — right for a leaf, and all a sessionless nested child can say. `delivering`: the ids
    among this row and its loaded children whose answer is in flight with no newer round open."""
    if children is None and session is not None:
        return _list_placements(session, principal, [task], actor_map, delivering)[task.id]
    live = [c for c in children or [] if c.archived_at is None]
    by_id = {c.id: c for c in live}
    candidates = [
        StageCandidate(c.id, c.state, c.stop_reason, child_ranks.get(c.id, 0), c.id in delivering) for c in live
    ]
    section, kind, chosen = task_sections.place(
        task.state, task.stop_reason, candidates, task.id in delivering, hidden=task.hidden,
    )
    if kind is None:
        return section, None
    if chosen is None:
        return section, _stop_out(kind, task, actor_map, stage=False)
    # Loaded children are already on this response, so naming one discloses nothing new.
    return section, _stop_out(kind, by_id[chosen.id], actor_map, stage=True)


def _list_placements(
    session: Session, principal: Principal | None, rows: Sequence[Task], actor_map: Mapping[UUID, str],
    delivering: set[int],
) -> dict[int, tuple[Section, Optional[StopOut]]]:
    """`_placement` for a whole page in a fixed number of queries: the stopping child of every row not
    already placed by itself, the caller's visibility of those children, and their stoppers' names.
    `delivering`: the page's ids whose answer is in flight with no newer round open; the stopping child is
    chosen in SQL, which already reads it."""
    unplaced = [
        t.id for t in rows
        if t.state != DONE and task_sections.stop_kind_of(t.state, t.stop_reason, t.id in delivering) is None
    ]
    stage_of = task_sections.stopping_children(session, unplaced)
    visible = _visible_task_ids(session, principal, [c.id for c in stage_of.values()])
    names = {**actor_map, **actors.actor_names(
        session, [c.stopped_by_id for c in stage_of.values() if c.stopped_by_id is not None], name_acting=True
    )} if stage_of else actor_map
    out: dict[int, tuple[Section, Optional[StopOut]]] = {}
    for t in rows:
        child = stage_of.get(t.id)
        candidates = [StageCandidate(child.id, child.state, child.stop_reason, 0)] if child is not None else []
        section, kind, chosen = task_sections.place(
            t.state, t.stop_reason, candidates, t.id in delivering, hidden=t.hidden,
        )
        if kind is None:
            out[t.id] = (section, None)
        elif chosen is None:
            out[t.id] = (section, _stop_out(kind, t, names, stage=False))
        else:
            out[t.id] = (section, _stop_out(kind, child, names, stage=True, visible=child.id in visible))
    return out


def _child_counts(
    session: Session, parent_ids: list[int], principal: Principal | None
) -> dict[int, tuple[int, int]]:
    """`{parent_id: (child_count, done_child_count)}` for the given parent ids in ONE grouped query, so the
    top-level list carries both counts without an N+1. Childless parents are absent; archived children
    are excluded, matching the detail route.

    Object-scoped: a count not matching the list it summarizes is a leak. `.select_from(Task)` is
    spelled out because `visible_task_clause` correlates to the enclosing `Task` FROM; without it the
    clause compiles to a cross join allowing everything."""
    if not parent_ids:
        return {}
    rows = session.execute(
        select(
            Task.parent_task_id,
            func.count(),
            func.sum(case((Task.state == DONE, 1), else_=0)),
        )
        .select_from(Task)
        .where(Task.parent_task_id.in_(parent_ids), Task.archived_at.is_(None))
        .where(visible_task_clause(principal))
        .group_by(Task.parent_task_id)
    ).all()
    return {parent_id: (count, int(done_count)) for parent_id, count, done_count in rows}


def _authored_draft_ids(
    task_ids: list[int], session: Session, principal: Principal | None
) -> set[int]:
    """Which of `task_ids` the CALLER authored and are still `draft` — the rows `authz.authored_draft`
    would admit under `AUTHOR_DRAFT_KEY`, answered for a whole page in ONE query.

    The predicate is `authz.authored_draft_task_clause`, never restated, so the offered control and
    the guard cannot drift. A superadmin gets the empty set: the clause is `true()` for them, and the
    flag would falsely claim authorship (they never need the fallback; `hooks/useTaskAccess.ts`). An
    unnamed (agent) caller also gets the empty set, fail-closed."""
    if not task_ids or principal is None or principal.is_superadmin:
        return set()
    return set(
        session.scalars(
            select(Task.id)
            .where(Task.id.in_(task_ids))
            .where(authored_draft_task_clause(principal))
        ).all()
    )


def _may_edit_ids(
    task_ids: list[int], session: Session, principal: Principal | None
) -> set[int]:
    """Which of `task_ids` the CALLER may WRITE — the rows `authz.editable_task_clause` admits (the clause
    half of `can_access(mode=WRITE)`), answered for a whole page in ONE query.

    The predicate is read from `authz`, never restated, so a Save button never fronts a 403. It says
    nothing about the key half; the client checks that. A superadmin gets every row (short-circuited,
    since the clause is `true()` and the claim is true). An unnamed (agent) caller gets the empty set."""
    if not task_ids or principal is None:
        return set()
    if principal.is_superadmin:
        return set(task_ids)
    return set(
        session.scalars(
            select(Task.id)
            .where(Task.id.in_(task_ids))
            .where(editable_task_clause(principal))
        ).all()
    )


def _asking_task_keys(
    session: Session, rows: Sequence[tuple[int, str, datetime]]
) -> dict[int, datetime]:
    """The single home of "is this row asking?", keyed by when it started asking, over `(task_id, state,
    updated_at)` rows. Non-asking rows are absent; the map serves `TaskOut.has_open_question` and
    `_gated_child_ids`, so a leaf and its epic cannot disagree.

    Two gate mechanisms, both live:
      (a) multi-round — `task_questions` rows still `open`, keyed by `MIN(created_at)`.
      (b) legacy single-shot — `state == awaiting_response`, keyed by `updated_at`.
    A row can be both; (a) wins, since a new round is asking even while an older answer delivers.

    Not asking: any state outside `_ANSWER_ACTIONABLE_STATES` (a paused child cannot act on an answer),
    and for (b) a row whose answer is already in flight (`_answer_delivering_ids`; reads "Delivering")."""
    candidates = [row for row in rows if row[1] in _ANSWER_ACTIONABLE_STATES]
    if not candidates:
        return {}
    # (a) Keyed by each task's oldest open round. Reset-hidden rounds are excluded (`app/task_artifacts.py`):
    # a Reset writes no per-row status, so a hidden round stays `open` for ever and would otherwise invite an
    # answer the resolve routes 409.
    open_round_rows = session.execute(
        select(TaskQuestion.task_id, func.min(TaskQuestion.created_at))
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(
            TaskQuestion.task_id.in_([task_id for task_id, _s, _u in candidates]),
            TaskQuestion.status == "open",
            visible_artifact_clause_joined(TaskQuestion.created_at),
        )
        .group_by(TaskQuestion.task_id)
    ).all()
    # (b) No query at all: the row IS the gate and its own `updated_at` is the key.
    parked = [
        (task_id, updated_at)
        for task_id, state, updated_at in candidates
        if state == AWAITING_RESPONSE
    ]
    # Narrowed to rows that can be delivering, so a batch with nothing parked pays no query.
    delivering_ids = (
        _answer_delivering_ids([task_id for task_id, _u in parked], session) if parked else set()
    )
    keys: dict[int, datetime] = {}
    for task_id, parked_at in parked:
        if task_id in delivering_ids:
            continue
        keys[task_id] = parked_at
    for task_id, first_open_at in open_round_rows:
        keys[task_id] = first_open_at
    return keys


def _gated_child_ids(
    session: Session, parent_ids: list[int], principal: Principal | None
) -> dict[int, int]:
    """`{parent_id: child_id}` — for each given parent, which child the operator should answer first, in
    one scoped query plus the shared probe. Parents with no asking child are absent; archived children
    are excluded. An id, not a flag: it spares the expand-and-hunt (the epic stays `active`).

    Which children count as asking is `_asking_task_keys`' rule. Object-scoped: the id is a navigation
    target, so an unreadable one is a leak. `.select_from(Task)` is defensive; `visible_task_clause`
    correlates to that FROM and would otherwise compile to a cross join allowing everything."""
    if not parent_ids:
        return {}
    child_rows = session.execute(
        select(Task.parent_task_id, Task.id, Task.state, Task.updated_at)
        .select_from(Task)
        .where(Task.parent_task_id.in_(parent_ids), Task.archived_at.is_(None))
        .where(visible_task_clause(principal))
    ).all()
    child_keys = _asking_task_keys(
        session, [(child_id, state, updated_at) for _p, child_id, state, updated_at in child_rows]
    )
    parent_of = {child_id: parent_id for parent_id, child_id, _s, _u in child_rows}
    # Winner per parent: oldest key, ties broken by the lower child id so the link is stable across
    # refreshes (rounds from one commit share a timestamp).
    winners: dict[int, tuple[datetime, int]] = {}
    for child_id, sort_key in child_keys.items():
        parent_id = parent_of[child_id]
        candidate = (sort_key, child_id)
        if parent_id not in winners or candidate < winners[parent_id]:
            winners[parent_id] = candidate
    return {parent_id: child_id for parent_id, (_key, child_id) in winners.items()}


def _task_prior_session_ended(session: Session, task: Task) -> bool:
    """Has a runner session on THIS task already ended with the work still owed — i.e. is the dispatch
    about to be composed a RESTART rather than a first start?

    The slot cannot answer it: resume terms were gated on relaunching onto the same worktree, but a
    relaunch onto a fresh slot is equally a restart. `dispatch_attempts` cannot either: it is zeroed on
    `start` and `answer` (`app/task_lifecycle.py`). `app/runner_terminations.py` writes exactly one row
    per ended session, never removed; which causes count is its `RESTART_HINT_CAUSES`.

    Reset-aware via the `visible_artifact_clause` watermark: after a Reset the next dispatch is a real
    first start (matching `operator_reset`'s exclusion from `RESTART_HINT_CAUSES`)."""
    return session.scalar(
        select(ActivityEvent.id)
        .where(
            ActivityEvent.kind == f"task_note:{runner_terminations.NOTE_ACTION}",
            ActivityEvent.data["id"].astext == str(task.id),
            ActivityEvent.data["cause"].astext.in_(
                sorted(runner_terminations.RESTART_HINT_CAUSES)
            ),
            visible_artifact_clause(ActivityEvent.created_at, task.artifacts_hidden_before),
        )
        .limit(1)
    ) is not None


# Anti-runaway per-prerequisite cap for threaded stage output: 64 KB never touches a genuine settled
# artifact, only a runaway.
_PREREQ_OUTPUT_MAX_CHARS = 65536


# The dual-read: older stage outputs are `task_note:stage_output` rows with no backfill, the unified
# settle writes `task_note:outcome`, so both are read, newest wins. Needed while two releases overlap.
_SETTLED_OUTPUT_NOTE_KINDS: tuple[str, ...] = ("task_note:outcome", "task_note:stage_output")

# The kind alone is not enough: `task_note:outcome` also has an older audit-note writer whose `data`
# has no `summary` key (`record_task_note`, `app/user_activity.py`). Without requiring the body,
# latest-wins could select that audit row and render nothing. The unified settle writes its artifact
# under exactly this key.
def _has_settled_output_body() -> Any:
    """The `data ? 'summary'` half of a settled-output read, spelled once. A key test, not
    `data->>'summary' IS NOT NULL`: with `none_as_null=True`, a JSON `null` and an absent key differ,
    and only the absent key is the bodiless audit note excluded here."""
    return ActivityEvent.data.has_key("summary")  # noqa: W601 — JSONB `?`, not dict.has_key


def _latest_settled_output_note(session: Session, task: Task) -> Optional[str]:
    """The newest settled-output note's body for `task` — the complete artifact an analysis child posted
    when it reached `done`. Same durable store and `data->>'id'` idiom as `_task_qa_entries`. None when
    the Task has settled no output yet.

    Either kind qualifies, only with a `data["summary"]` body; newest across both kinds wins. The Reset
    watermark is inside the ordering, or a soft-deleted newest note would be returned rather than
    skipped. `_task_prerequisite_outputs` applies each prerequisite's own watermark."""
    row = session.scalars(
        select(ActivityEvent)
        .where(ActivityEvent.kind.in_(_SETTLED_OUTPUT_NOTE_KINDS))
        .where(_has_settled_output_body())
        .where(ActivityEvent.data["id"].astext == str(task.id))
        .where(visible_artifact_clause(
            ActivityEvent.created_at, task.artifacts_hidden_before))
        .order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
    ).first()
    if row is None:
        return None
    return (row.data or {}).get("summary") or None


def _visible_prerequisites(session: Session, task: Task, principal: Principal | None) -> list[Task]:
    """`task`'s prerequisites (`task_dependencies`) the caller may see (`_visible_task_ids`), oldest-first
    by id; empty when it has none. `principal` has no default: `None` is the unfiltered agent principal."""
    prereq_ids = session.scalars(
        select(TaskDependency.depends_on_task_id).where(TaskDependency.task_id == task.id)
    ).all()
    if not prereq_ids:
        return []
    visible_ids = _visible_task_ids(session, principal, list(prereq_ids))
    if not visible_ids:
        return []
    return list(session.scalars(select(Task).where(Task.id.in_(visible_ids)).order_by(Task.id)).all())


def _task_prerequisite_outputs(
    session: Session, task: Task, principal: Principal | None
) -> Optional[str]:
    """The settled outputs of `task`'s prerequisite analysis stages, threaded so an analysis child builds ON
    the prior phase's output rather than re-deriving it. Walks `task_dependencies`, reads each
    prerequisite's latest settled-output note, and concatenates oldest-first by id, each clamped.
    Object-scoped; `principal` has no default because `None` is the unfiltered agent principal."""
    prereqs = _visible_prerequisites(session, task, principal)
    if not prereqs:
        return None
    blocks: list[str] = []
    for prereq in prereqs:
        output = _latest_settled_output_note(session, prereq)
        if not output:
            continue
        if len(output) > _PREREQ_OUTPUT_MAX_CHARS:
            output = output[:_PREREQ_OUTPUT_MAX_CHARS].rstrip() + "\n…[truncated]"
        label = prereq.persona.replace("_", " ").title()
        blocks.append(f"=== {label} ===\n{output}")
    combined = "\n\n".join(blocks).strip()
    return combined or None


def _task_prerequisite_recon_map(
    session: Session, task: Task, principal: Principal | None
) -> Optional[list[dict[str, Any]]]:
    """The structured recon artifact(s) of `task`'s prerequisite stage(s), threaded so a dependent receives
    them VERBATIM instead of re-exploring the same files. Walks the SAME `task_dependencies` edges as
    `_task_prerequisite_outputs`, oldest-first.

    Returns `[{"persona": <label>, "recon_map": <dict>}, ...]`: a list, since two prerequisites can
    share a persona. Object-scoped with no default, as its sibling."""
    prereqs = _visible_prerequisites(session, task, principal)
    if not prereqs:
        return None
    combined: list[dict[str, Any]] = []
    for prereq in prereqs:
        if not prereq.recon_map:
            continue
        label = prereq.persona.replace("_", " ").title()
        combined.append({"persona": label, "recon_map": prereq.recon_map})
    return combined or None


def _task_prerequisite_artifacts(
    session: Session, task: Task, principal: Principal | None
) -> Optional[list[PrerequisiteArtifactOut]]:
    """The prerequisite-stage DOCUMENTS `task`'s persona expects as inputs, as a staging instruction
    dispatch materialises into files on the runner's disk. Same edges and access gating as its two
    siblings, oldest-first.

    Kinds come from `task_manifest.INPUT_MANIFEST[task.persona]`; a persona absent there returns `None`
    without a query — a `coder` child gets no artifact path (its body carries the planner's spec).

    Read through `task_artifacts.current_artifact`, never a local query: it applies the prerequisite's
    own Reset watermark; a hand-rolled query would fail open. One artifact per kind, first prerequisite
    wins; a kind nobody has is absent and dispatch says so (`app/dispatch_compose._start_here_block`).
    `principal` has no default; `None` is the unfiltered agent principal."""
    expected = INPUT_MANIFEST.get(task.persona) or ()
    if not expected:
        return None
    prereqs = _visible_prerequisites(session, task, principal)
    if not prereqs:
        return None
    out: list[PrerequisiteArtifactOut] = []
    for kind in expected:
        for prereq in prereqs:
            row = current_artifact(session, prereq, kind)
            if row is None:
                continue
            out.append(
                PrerequisiteArtifactOut(
                    id=row.id, task_id=row.task_id, kind=kind, size_bytes=row.size_bytes
                )
            )
            break
    return out or None


def _task_attachments_meta(
    session: Session, task: Task, principal: Principal | None
) -> Optional[list[AttachmentMetaOut]]:
    """This Task's own uploaded attachments UNIONed with its parent epic's, metadata only, ordered
    owner-then-id. Uploads land on the epic, so a child's body may reference rows it does not own; one
    parent hop suffices since every dispatched child parents directly to its epic.

    Each row's `task_id` is its owner, so bytes are fetched from the right task. The parent hop is
    access-gated (a READ probe); `None` (agent) takes it unconditionally.

    Runner-produced rows are excluded by a positive filter — this docstring is the single home of that
    rule (`app/routes/attachments.py` points here). This projection is what
    `apps/jaahd/fleet_dispatch.py::materialize_attachments` lays into a workspace, and only
    `kind == ATTACHMENT_KIND_FILE` is the human's input (`app/task_manifest.py::DOCUMENT_KINDS`). A
    prerequisite's documents travel via `_task_prerequisite_artifacts` instead."""
    task_ids = [task.id]
    if task.parent_task_id is not None and can_access(
        session, principal, "task", task.parent_task_id, mode=ObjectAccess.READ
    ):
        task_ids.append(task.parent_task_id)
    rows = session.scalars(
        select(TaskAttachment)
        .where(
            TaskAttachment.task_id.in_(task_ids),
            TaskAttachment.kind == ATTACHMENT_KIND_FILE,
            # Pending rows are not projected: a row whose presigned PUT never landed would send the daemon fetching
            # bytes that do not exist. See `app/task_artifacts.py::uploaded_clause`.
            uploaded_clause(),
        )
        .order_by(TaskAttachment.task_id, TaskAttachment.id)
    ).all()
    if not rows:
        return None
    return [AttachmentMetaOut.model_validate(row) for row in rows]


def _task_effective_autonomous(session: Session, task: Task) -> bool:
    """The autonomous-mode switch actually binding on `task` at dispatch: an epic child in
    `INTERVIEWING_CHILD_PERSONAS` takes its parent epic's `autonomous`, every other child runs Full
    Auto, and a parentless row takes its own.

    The persona condition keeps the console honest: a `coder` child showing "Full Auto: ON" must not
    dispatch with the epic's OFF. Membership is tested negatively, so a new persona is Full Auto by
    default — fail-open, caught by the exact-set test in `tests/test_task_model.py`.

    Ungated, unlike the fence hop: this is one bit of the epic's switch and must be the true dispatch
    value even when the principal cannot fetch the epic. A pure read."""
    # A playbook stage row: "Never asks" runs Full Auto; "Asks when Full Auto is off" takes the switch LIVE
    # (`playbook_engine.follows_full_auto`) — a child its container's, as an interviewing epic child does,
    # a one-stage row its own.
    if task.playbook_stage:
        if not playbook_engine.follows_full_auto(task.playbook_stage):
            return True
        if task.parent_task_id is not None:
            container = session.get(Task, task.parent_task_id)
            if container is not None:
                return container.autonomous
        return task.autonomous
    if task.parent_task_id is not None:
        # `plan`/`coder`/`finish` always run Full Auto; returned without loading the epic so nothing starts
        # reading its value again.
        if task.persona not in INTERVIEWING_CHILD_PERSONAS:
            return True
        epic = session.get(Task, task.parent_task_id)
        if epic is not None:
            return epic.autonomous
    return task.autonomous


def _interview_skipped_reason(session: Optional[Session], task: Task) -> Optional[str]:
    """The operator-facing statement that this task had Full Auto cleared but ran on a CLI with no question
    channel, so its interview never happened, and why. NULL for every other row.

    The prompt half is `dispatch_compose.directives._interview_unavailable_directive`; this half needs no promise
    from the runner, being derived from row columns, and delegates to the composer's own
    `interview_skipped` predicate.

    Inputs: `bound_provider` (the CLI that ran it, stamped at claim by `_bound_provider_stamp`; NULL means
    never dispatched), the effective switch (`_task_effective_autonomous`, never the raw column), and
    the console exemption (an `advisor` converses in its terminal). `autonomous` stays editable after the
    run, so this is a derived reading of the row as it is now; a stamped run record is a deferred
    follow-up. No session ⇒ no claim: every many-row caller passes none, so list and detail may differ.

    Cheap: NULL or interviewing providers return before any query. Accepted disclosure: for an
    interviewing child this is a one-bit oracle on the parent epic's `autonomous`, matching
    `_task_effective_autonomous`'s ungated contract (`task_dispatch_out.autonomous` already serves it)."""
    provider = agent_providers.by_value(task.bound_provider or "")
    # Not `provider.can_interview` as a shortcut: the answer comes from the shared predicate, so it cannot
    # drift from the prompt the runner got. `provider is None` means never dispatched: nothing skipped yet.
    if provider is None:
        return None
    charter_key = PERSONA_CHARTER.get(task.persona or "", (None, None))[0]
    if charter_key is None or session is None:
        return None
    if not interview_skipped(
        autonomous=_task_effective_autonomous(session, task),
        console_present=charter_key in _CONSOLE_INTERVIEW,
        provider=provider,
    ):
        return None
    # Server-side prose naming what was asked, what happened, why (the vendor), and that nothing is waiting on
    # the operator.
    return (
        "The interview was skipped. Full Auto is cleared on this task, so a runner would normally stop "
        f"and ask you — but it ran on {provider.label}, which exposes no structured question channel "
        "that reaches the operator. Nothing was asked and nothing is waiting on you: the runner "
        "resolved every ambiguity itself on the most defensible reading and recorded the assumptions "
        "in its own summary and report."
    )


# ── Plan-as-task → coders append ───────────────────────────────────────────────────
# An epic's `plan` child produces a breakdown that becomes coder Tasks of the epic,
# dependency-linked, then readied by the fan-out and dispatched in dependency order. apps/api stays
# gh-free: the plan RUNNER POSTs the breakdown, and this core only writes rows and edges.


def _epic_children(parent_id: int, session: Session) -> list[Task]:
    """Every LIVE (non-archived) child of `parent_id`, ascending by id — the ONE query every route loading a
    Task's children shares, so `append_tasks`'s archive+replace cannot be forgotten by a new caller.
    An archived child is a replaced decomposition's kept evidence, excluded HERE rather than by each caller."""
    return list(
        session.scalars(
            select(Task)
            .where(Task.parent_task_id == parent_id, Task.archived_at.is_(None))
            .order_by(Task.id)
        ).all()
    )


def _task_out_with_children(task_id: int, session: Session, principal: Principal | None) -> TaskOut:
    """Re-read `task_id` and render it with its child rows — the tail every mutating verb ends on, usually
    after its own `expire_all`. `session.get` serves an already-loaded row from the identity map, so a caller
    holding the row pays no second SELECT."""
    task = get_task_or_404(task_id, session)
    children = _epic_children(task_id, session)
    return _task_out(task, children=list(children) if children else None, session=session, principal=principal)


def _subtree_task_ids(root_id: int, session: Session) -> list[int]:
    """Every task id in `root_id`'s subtree, the root included, at any depth — a recursive CTE over
    `parent_task_id`; nothing caps the depth. Archived rows are included: they still carry
    `project_id` and would come back to the wrong audience when un-archived."""
    tree = select(Task.id).where(Task.id == root_id).cte("subtree", recursive=True)
    # `union`, not `union_all`: it is the termination guarantee. `UNION ALL` never dedupes and Postgres has
    # no recursion limit, so a `parent_task_id` cycle would exhaust memory on the single worker.

    # No API path can create a cycle today; hardening against migrations, manual UPDATEs or future
    # re-parenting.
    tree = tree.union(select(Task.id).where(Task.parent_task_id == tree.c.id))
    return list(session.scalars(select(tree.c.id)))


def _may_set_hidden(task: Task, principal: Principal) -> bool:
    """The `hidden` rule, one home for `_apply_hidden` and `TaskOut.may_set_hidden`: a top-level row's
    creator holding `tasks:edit` on its project, or a superadmin. Key-only, no query."""
    if task.parent_task_id is not None:
        return False
    return principal.is_superadmin or (
        principal.user_id == task.created_by_id
        and has_project_key(principal, permission_key("tasks", "edit"), task.project_id)
    )


def _ran_on(session: Session, task_id: int) -> Optional[RanOnOut]:
    return _ran_on_of(runner_places.places(session, [task_id]).get(task_id))


def _ran_on_of(place: Optional[runner_places.Place]) -> Optional[RanOnOut]:
    return None if place is None else RanOnOut(node=place.node, slot=place.slot, live=place.live)


# ── What a chat may actually be asked to do, so the projection cannot advertise a refusal ──────────
def _available_transitions_for(task: Task) -> list[str]:
    """`transitions_from(task.state)`, MINUS the verbs this file refuses for an interactive chat.

    The table is keyed on state alone, rightly. For `awaiting_response` it yields `answer`, which a chat
    refuses (`_refuse_chat_answer_door_or_409`) because landing `ready` would re-dispatch a second runner
    into the live conversation's worktree. (`start` is no longer offered from that state for anyone.)
    Advertised verbs are a contract: `apps/web` renders controls from this list. Keyed on the
    done-criterion and scoped to `_RUNNER_RUNNING_STATES`, as the refusal is."""
    allowed = list(transitions_from(task.state))
    if (
        PERSONA_DONE_CRITERION.get(task.persona) != DONE_CRITERION_OPERATOR
        or task.state not in _RUNNER_RUNNING_STATES
    ):
        return allowed
    return [verb for verb in allowed if verb not in _CHAT_REFUSED_TRANSITIONS]


# Named once, so this set and the 409 guard stay one idea. Not `start` (the table no longer offers it
# from `awaiting_response`), and not `complete`/`reset`/`stop`, the operator's real exits (Complete is the
# only edge to `done`). Asserted against the table in `test_tasks_routes_transitions.py`.
_CHAT_REFUSED_TRANSITIONS: frozenset[str] = frozenset({"answer"})


# ══ Cross-task dependencies + readiness gate ════════════════════════════════════════════════════════
# A Task may declare prerequisites: `task_id → depends_on_task_id` in `task_dependencies`, "task_id is
# blocked-by depends_on_task_id". Any unmet prerequisite excludes it from `dispatchable_tasks`. Cycles and
# self-edges are rejected at add time (422), not by a DB constraint; readiness is never persisted.


def _prerequisite_ids(task_id: int, session: Session) -> list[int]:
    """The ids `task_id` directly depends on, ascending. Empty for a Task with no declared prerequisites."""
    return list(
        session.scalars(
            select(TaskDependency.depends_on_task_id)
            .where(TaskDependency.task_id == task_id)
            .order_by(TaskDependency.depends_on_task_id)
        ).all()
    )


def _waiting_on_personas(child_ids: list[int], session: Session) -> dict[int, str]:
    """For each id in `child_ids`, the `persona` of its earliest unmet prerequisite, feeding
    `TaskOut.waiting_on_persona`. One query across all of an epic's children; ids with nothing unmet
    are absent.

    Unmet means what the dispatch gate means (`_has_unmet_prerequisite`): not archived, and not `done` or done with
    its PR still in the merge lane — or an archived prerequisite would leave a dispatchable row "Waiting" for ever,
    and a merging one would leave a gated row reading Queued."""
    if not child_ids:
        return {}
    rows = session.execute(
        select(TaskDependency.task_id, TaskDependency.depends_on_task_id, Task.persona)
        .join(Task, Task.id == TaskDependency.depends_on_task_id)
        .where(
            TaskDependency.task_id.in_(child_ids),
            or_(Task.state != DONE, merge_lane_state.awaiting_lane_merge_clause(Task.id)),
            Task.archived_at.is_(None),
        )
        .order_by(TaskDependency.task_id, TaskDependency.depends_on_task_id)
    ).all()
    result: dict[int, str] = {}
    for task_id, _dep_id, persona in rows:
        result.setdefault(task_id, persona)
    return result


def _unmet_prerequisite_counts(task_ids: list[int], session: Session) -> dict[int, int]:
    """For each id in `task_ids`, how many of its prerequisites are still unmet — the batched SQL mirror of
    `_has_unmet_prerequisite`, feeding `TaskOut.unmet_prerequisite_count`. One grouped query; absent
    ids read as 0.

    A count, not the prerequisites: how many things gate the caller's own row is a dispatch fact, while
    which rows those are stays scoped to what they may see (see `TaskDependenciesOut`)."""
    if not task_ids:
        return {}
    rows = session.execute(
        select(TaskDependency.task_id, func.count())
        .join(Task, Task.id == TaskDependency.depends_on_task_id)
        .where(
            TaskDependency.task_id.in_(task_ids),
            or_(Task.state != DONE, merge_lane_state.awaiting_lane_merge_clause(Task.id)),
            Task.archived_at.is_(None),
        )
        .group_by(TaskDependency.task_id)
    ).all()
    return {task_id: count for task_id, count in rows}


def _answer_delivering_ids(task_ids: list[int], session: Session) -> set[int]:
    """Which of `task_ids` have a gate answer STILL IN FLIGHT to a live runner — the set behind
    `TaskOut.answer_delivering`. ONE query for the whole row set; an id with nothing in flight is absent.

    A warm delivery leaves the Task `awaiting_response` until the node acks
    (`_deliver_answer_to_live_runner`), so without this the row would read "Asking" with nothing left to
    ask. "In flight" is `task_sections.answer_delivering_keys`, which the board's sections read too."""
    if not task_ids:
        return set()
    rows = session.execute(task_sections.answer_delivering_keys(task_ids)).all()
    out: set[int] = set()
    for (task_key,) in rows:
        try:
            out.add(int(task_key))
        except (TypeError, ValueError):
            # A payload not written by `build_deliver_answer_intent`: skip it rather than fail the page.
            continue
    return out


# The ack outcomes that mean "this delivery attempt did not land" — the non-retiring, node-authored ones.
_DELIVERY_FAILURE_OUTCOMES = (
    fleet_verdicts.OUTCOME_FAILED, fleet_verdicts.OUTCOME_REFUSED_STALE, fleet_verdicts.OUTCOME_REFUSED_PRECONDITION,
)


# A batched caller's "looked, none failed": keeps `_task_out` from querying again for that row.
NO_DELIVERY_FAILURE: tuple[str, int] = ("", 0)


def _answer_delivery_failures(task_ids: list[int], session: Session) -> dict[int, tuple[str, int]]:
    """Per task, the newest pending `deliver_answer` intent the node acked as not landed: (its redacted
    reason, its `refusals`). ONE query; a task with no failed attempt is absent.

    The count is the ack path's own retry budget (`routes/agent/slot_intents.py::_account_for_refusal`): failures
    in a row with the same reason; a changed reason restarts it at 1. No column of its own."""
    if not task_ids:
        return {}
    # The key read under the verb test, as `task_sections.answer_delivering_keys` does.
    is_deliver = SlotIntent.verb == fleet_verdicts.VERB_DELIVER_ANSWER
    key = case((is_deliver, SlotIntent.payload["task_id"].astext))
    rows = session.execute(
        select(key, SlotIntent.reason, SlotIntent.refusals)
        .where(is_deliver, key.in_([str(i) for i in task_ids]),
               SlotIntent.outcome.in_(_DELIVERY_FAILURE_OUTCOMES))
        .order_by(SlotIntent.created_at.desc(), SlotIntent.id.desc())
    ).all()
    out: dict[int, tuple[str, int]] = {}
    for task_key, reason, refusals in rows:
        try:
            task_id = int(task_key)
        except (TypeError, ValueError):
            continue
        if task_id not in out:
            out[task_id] = (fleet_verdicts.redact_node_detail(reason) or "no reason reported", max(refusals or 0, 1))
    return out


def _visible_task_ids(
    session: Session, principal: Principal | None, task_ids: Sequence[int]
) -> set[int]:
    """Which of `task_ids` this principal may see, in ONE scoped query.

    Via `scoped_task_select`, the sanctioned entry point (a bare `visible_task_clause` select with no
    `Task` FROM compiles to an allow-all cross join). `with_only_columns(Task.id)` is safe: the re-derived
    FROM is still `tasks`. Empty input, the agent principal and a superadmin short-circuit."""
    if not task_ids:
        return set()
    if principal is None or principal.is_superadmin:
        return set(task_ids)
    return set(
        session.scalars(
            scoped_task_select(principal)
            .with_only_columns(Task.id)
            .where(Task.id.in_(set(task_ids)))
        ).all()
    )
