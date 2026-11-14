"""The persona registry — one record per persona, and the single home for what a persona is.

`PERSONA_RECORDS` is the whole vocabulary; every per-persona table is a derivation at the bottom,
exported under the name its readers use. Adding a persona is one `Persona(...)` row; a missing field
fails at the dataclass, not as a wedged task. Parity tests hold the registry against its consumers;
a registry test of its own pins its internal shape.

Not here: role prose (`app/dispatch_compose/roles.py::_ROLES`, keyed by charter key) and `INPUT_MANIFEST`
(`app/task_manifest.py`). Imports only the stdlib-only `app.task_lifecycle` and `app.task_manifest`,
so every route and compose module can read it without import cycles.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, PAUSED, SYSTEM_FAILED
from app.task_manifest import REQUIRED_ADVISORY, REQUIRED_HARD

__all__ = [
    "CREATABLE_PERSONAS",
    "CREATION_SHAPES",
    "DB_DIRECT_SETTLE_STATES",
    "DONE_CRITERION_ARTIFACT",
    "DONE_CRITERION_ARTIFACT_WITH_CODERS",
    "DONE_CRITERION_MERGED_PR",
    "DONE_CRITERION_OPERATOR",
    "DONE_CRITERION_OUTCOME",
    "DONE_CRITERIA",
    "MODES",
    "MODE_ANALYSIS",
    "MODE_BUILD",
    "DELIVERY_DB",
    "DELIVERY_OUTCOME",
    "DELIVERY_PR",
    "DELIVERIES",
    "OPERATOR_PERSONAS",
    "PERSONA_CHARTER",
    "PERSONA_CREATE_AUTONOMOUS",
    "PERSONA_DONE_CRITERION",
    "PERSONA_LABELS",
    "PERSONA_MANIFEST",
    "PERSONA_NAMES",
    "PERSONA_PICKER_HINT",
    "PERSONA_SETTLE_STATES",
    "PERSONA_TERMINALS",
    "PERSONA_RECORDS",
    "ALWAYS_INTERVIEWING_PERSONAS",
    "PIPELINE_PERSONAS",
    "PROJECT_SETTINGS_EXCLUDED_FIELDS",
    "PROJECT_SETTINGS_LICENCE",
    "PROJECT_SETTINGS_WRITABLE_FIELDS",
    "Persona",
    "REGISTRY",
    "SHAPE_FOLLOW_UP",
    "SHAPE_TASKS",
    "SHAPE_SYSTEM_TASK",
    "SHAPE_TOP_LEVEL",
    "SHAPE_CRON",
    "SHAPE_TRIGGER",
    "SHAPE_EDIT_FILED",
    "SHAPE_DELETE_FILED",
    "SCHEDULABLE_PERSONAS",
    "TRIGGER_PERSONAS",
    "SYSTEM_PERSONAS",
    "TASK_CREATION_LICENCE",
    "ProjectSettingsLicence",
    "TaskCreationLicence",
    "charter_delivery",
    "charter_mode",
    "persona",
]

# ── The knob vocabularies ─────────────────────────────────────────────────────────────────────────────
# `mode` selects the runner charter's finish flow: "build" (PR/merge/close) or "analysis" (read-only
# settle). Constants because `dispatch_compose` branches on them.
MODE_BUILD = "build"
MODE_ANALYSIS = "analysis"
MODES: tuple[str, ...] = (MODE_BUILD, MODE_ANALYSIS)

# `delivery` names the role's result channel — the delivery doctrine around the one
# `task.complete` call: `"db"` (the settled document), `"pr"` (a merged PR, recorded as evidence),
# `"outcome"` (the reported outcome, nothing to merge).
DELIVERY_DB = "db"
DELIVERY_PR = "pr"
DELIVERY_OUTCOME = "outcome"
DELIVERIES: tuple[str, ...] = (DELIVERY_DB, DELIVERY_PR, DELIVERY_OUTCOME)

# ── The done-criterion tokens ─────────────────────────────────────────────────────────────────────────
# What moves a row to `done` — the one legitimate branch on persona:
#   `artifact` — the runner's settled artifact; `artifact_with_coders` — `plan` only, so an epic never
#   completes with an append outstanding (a plan settling that there is nothing to build is a settled
#   decomposition of zero children); `merged_pr` — the runner's settle declaring its merged PR (stamps `merged_at`) or a
#   legitimate no-PR terminal; `outcome` — the runner's own settle, nothing merges; `operator` — nothing
#   the runner sends settles it, only the operator's `complete` (agent doors refuse).
DONE_CRITERION_ARTIFACT = "artifact"
DONE_CRITERION_ARTIFACT_WITH_CODERS = "artifact_with_coders"
DONE_CRITERION_MERGED_PR = "merged_pr"
DONE_CRITERION_OUTCOME = "outcome"
DONE_CRITERION_OPERATOR = "operator"
DONE_CRITERIA: tuple[str, ...] = (
    DONE_CRITERION_ARTIFACT,
    DONE_CRITERION_ARTIFACT_WITH_CODERS,
    DONE_CRITERION_MERGED_PR,
    DONE_CRITERION_OUTCOME,
    DONE_CRITERION_OPERATOR,
)

# ── Task-creation authority: one licence every persona holds, over a closed set of creation shapes ────
# Every persona's runner may use every shape (an operator decision). What a call may reach is
# bounded elsewhere: by each endpoint's path check (`append_tasks`'s `parent.persona == 'epic'`,
# `append_finish_follow_up`'s live-finish-child check, own project, own filings), by the launcher's console
# authority (`routes/agent_crons.py::_executor_authority`), and — for a door that makes work run unattended —
# by the loop guard (`routes/agent/auth.py::require_human_started`; why it keeps the work graph finite:
# `app/task_ancestry.py::machine_origin`). The shapes stay named so each door and the persona page say which.
SHAPE_TASKS = "tasks"     # append this epic's child tasks to the caller's OWN epic
SHAPE_FOLLOW_UP = "follow_up"           # file a follow-up under the caller's OWN epic
SHAPE_TOP_LEVEL = "top_level"           # file ONE standalone top-level task in the caller's own project
# `POST /api/agent/tasks/system` files a hidden non-pipeline row (`SYSTEM_PERSONAS`) and sits behind the
# same guard, so it is a named shape too.
SHAPE_SYSTEM_TASK = "system_task"
# Two lifecycle shapes that create nothing: amend (title + body) or delete a row — only a `draft` the
# caller's own task filed (`Task.created_by_task_id`).
SHAPE_EDIT_FILED = "edit_filed"         # rewrite the title and body of a draft the caller's task filed
SHAPE_DELETE_FILED = "delete_filed"     # delete a draft the caller's task filed
# Schedule a `runner` cron in the caller's own project, and edit or delete a cron the caller's task created
# (`Cron.created_by_task_id`). Every firing files one of `SCHEDULABLE_PERSONAS`, with no human watching.
SHAPE_CRON = "cron"
# Create an event-driven trigger in the caller's own project, and edit or delete a trigger the caller's task
# created (`TaskTrigger.created_by_task_id`). Every delivery files one of `TRIGGER_PERSONAS`.
SHAPE_TRIGGER = "trigger"

#: Single home for the shape vocabulary, so a test can enumerate it rather than re-spell it.
CREATION_SHAPES: tuple[str, ...] = (
    SHAPE_TASKS, SHAPE_FOLLOW_UP, SHAPE_TOP_LEVEL, SHAPE_SYSTEM_TASK, SHAPE_EDIT_FILED, SHAPE_DELETE_FILED,
    SHAPE_CRON, SHAPE_TRIGGER,
)


@dataclass(frozen=True)
class TaskCreationLicence:
    """The authority to create Tasks: which shapes, and the prose naming the bound (the persona page shows it)."""

    shapes: frozenset[str]
    constraint: str


#: The one licence, held by every persona.
TASK_CREATION_LICENCE = TaskCreationLicence(
    shapes=frozenset(CREATION_SHAPES),
    constraint=(
        "its own project, one row per dedup_key, amending or deleting only a draft it filed itself, and "
        "editing or deleting only a cron or trigger it created itself; a draft from any task, but a started "
        "task, a cron or a trigger only from a task a person started in the console"
    ),
)


# ── Project-settings authority: one licence every persona holds, over its own project's row ──────────
# Authority ("which columns may a runner write at all") is kept apart from per-field
# constraint, which stays in `routes/projects.py::has_authority_to_author_governance`, re-applied to the
# task's launcher (`app/task_ancestry.py::launcher_user_id`). No new bypass: a runner authors a
# governance column only where its launcher could.

#: The writable side — `routes/projects.py::ProjectUpdate` columns that describe the repo (name, notes,
#: branch/gate/stack commands, pool switch, port grid, tech stack). None widens the runner's own
#: reach.
PROJECT_SETTINGS_WRITABLE_FIELDS: frozenset[str] = frozenset((
    "name", "integration_branch", "branch_prefix", "gate_quick_cmd", "gate_full_cmd",
    "stack_up_cmd", "stack_down_cmd", "push_enabled",
    "default_model",
    "tech_stack", "notes",
    # The connected repositories: their branches, primary and order only — a runner adding,
    # removing or re-pointing one is refused (`routes/agent_project_settings.py::_refuse_a_repository_change`),
    # since a URL is placement, like `repo_url` below.
    "repos",
    # The answer to the 409 a primary switch off a Jaah-made repository earns (`app/jaah_repo_replace.py`): what
    # happens to that repository, never which one is connected.
    "jaah_repo",
))

#: The excluded side — each widens the runner's own reach rather than describing the repo:
#: `agent_instructions` (trusted prompt directives),
#: `repo_url` and its `forge`/`forge_base_url` (placement — which host a runner's token is sent to),
#: `egress_allow`/`egress_deny` (which names the project's MicroVMs may reach — the runner's own network) and
#: the three `microvm_*` sizes (how much of a shared node its VM may take), and `default_agent_id`/
#: `default_provider` (which engine — whose code — runs every unpicked task, `app/agent_resolution.py`), `default_persona`
#: (which persona every unnamed task runs as), and
#: `merge_lane_batch_max` (how many PRs, other tasks' included, one merge-lane run takes), `outcome_scan_interval_minutes`
#: (how often the outcome scan spends a runner), the four intelligent
#: routing fields (which engine the router may pick, and blind review of the runner's own work), and the Production QA
#: fields (the opt-in, what counts as a deploy and a regression, the revert policy).
PROJECT_SETTINGS_EXCLUDED_FIELDS: frozenset[str] = frozenset((
    "agent_instructions", "repo_url", "forge", "forge_base_url",
    # "Run jobs as" (`app/git_access.py::project_forge_person`): whose git login and coding-agent account the project's
    # background jobs spend — an Org Admin's choice (`routes/projects.py::_validated_run_jobs_as`).
    "run_jobs_as_id",
    # The platform's own mark (`models.Project.system_kind`), never edited by anyone.
    "system_kind",
    # Confirms a hosted app's delete (`app/jaah_repo_replace.py::require_choice`): a person's act, never a runner's.
    "jaah_repo_confirm",
    "egress_allow", "egress_deny", "microvm_vcpus", "microvm_ram_mib", "microvm_disk_gib",
    "default_agent_id", "default_provider",
    # Which persona — whose charter and licences — every unnamed task and plan child runs as: the operator's choice.
    "default_persona",
    # The merge lane's batch cap (`app/merge_lane.py::batch_max`): how many queued PRs, other tasks' included,
    # one CI run takes — the lane's throughput and blast radius, not a fact about the repo.
    "merge_lane_batch_max",
    # The project's own merge lane switch (`merge_lane_state.lane_states`): whether runner PRs here go through the
    # lane at all — the operator's opt-in, never a runner's.
    "merge_lane_enabled",
    # The outcome scan's cadence (`app/outcome_ledger/settings.py`): how often a runner is spent judging this
    # project's merged work — the operator's cost knob, not a fact about the repo.
    "outcome_scan_interval_minutes",
    # Which CI checks count as security scans (`app/outcome_ledger/security.py`): oversight of the runner's own
    # output — a runner widening it would turn any failing check into a security finding.
    "security_check_patterns",
    # Intelligent routing (`app/agent_router/`): which engine the router may run the work on, and whether a second
    # agent reviews it blind — oversight of the runner itself, never the runner's to loosen.
    "routing_mode", "routing_latency_cap_hours", "blind_review", "blind_review_other_vendor",
    # The share of 30-day spend exploration and benchmark runs may take (`Project.experiment_budget_share`): the
    # operator's cost knob.
    "experiment_budget_share",
    # Benchmark mode (`app/outcome_ledger/benchmark.py`): whether shadow duels run here, and how often — spend the
    # runner would be choosing for itself.
    "benchmark_opt_in", "benchmark_sample_rate",
    # Production QA (`app/production_qa/`): whether incidents file tasks and reverts here, and what counts as a deploy
    # and a regression — the operator's opt-in, never a runner's through this generic write: deploy workflows reach a
    # runner only through `project.connect_deploys` (`routes/agent_project_settings.py::connect_task_project_deploys`).
    "production_qa_enabled", "deploy_workflows", "revert_policy",
    "regression_window_min", "regression_error_ratio", "regression_min_events", "regression_p95_ratio",
    # The native probes (`app/production_qa/probes.py`): commands a node executes after each deploy — authored by a
    # `projects:edit` holder only, never by a runner.
    "health_probes", "smoke_commands",
    # Project memory On/Off (`models.Project.project_memory`): whether earlier runs' notes reach this project's
    # runners — oversight of what the runner is told, the operator's choice.
    "project_memory",
))

# The partition is fail-closed: both sides are spelled out, and a registry test
# asserts `writable | excluded == set(ProjectUpdate.model_fields)`, so a new column is agent-writable
# only once someone classifies it.


@dataclass(frozen=True)
class ProjectSettingsLicence:
    """The authority to write a task's own project settings and its project-default env-var catalogue: which
    columns, and the prose naming the bound. Per-field authority is not restated (see the section note).

    `constraint` is quoted verbatim by the 403s in `routes/agent_project_settings.py`; `fields` and
    `PROJECT_SETTINGS_WRITABLE_FIELDS` are the enforced answer."""

    #: The project-row columns a runner may write — a subset of `PROJECT_SETTINGS_WRITABLE_FIELDS`.
    fields: frozenset[str]
    #: The bound, as a noun phrase completing "licence covers …" — see the class note.
    constraint: str


#: The one licence, held by every persona. Never `branch_prefix`: it is the operator's namespace
#: for every task branch (`app/task_branch.py`) and the push guard's allowlist, so a runner's "reconciliation"
#: would move both.
PROJECT_SETTINGS_LICENCE = ProjectSettingsLicence(
    fields=PROJECT_SETTINGS_WRITABLE_FIELDS - {"branch_prefix"},
    constraint=(
        "only its own task's project row and that project's default env-var catalogue, written "
        "with the authority of the task's launcher, and never `branch_prefix`, `agent_instructions` "
        "or `repo_url`"
    ),
)


# ── The settle-state sets ─────────────────────────────────────────────────────────────────────────────
#: The states a DB-direct analysis stage may settle itself from. A stray, late or wrong-state
#: settle is a clean 409 with nothing written.
DB_DIRECT_SETTLE_STATES: tuple[str, ...] = (ACTIVE, AWAITING_RESPONSE)

#: `finish`'s own set, borrowed from the operator `complete` verb's sources: the finish phase is re-runnable
#: across exactly those states. Excludes `done` — `execute_finish_phase`'s `state != DONE` is an idempotency
#: guard, and promoting it would let a stale finish runner settle a row an operator had Reset to `draft`.
_FINISH_SETTLE_STATES: tuple[str, ...] = (ACTIVE, AWAITING_RESPONSE, PAUSED, SYSTEM_FAILED)

#: Every other settling persona: a runner that is not active has no attempt in flight to report on.
_ACTIVE_ONLY: tuple[str, ...] = (ACTIVE,)

#: The extra state a delivered terminal may settle from: `system_failed`, for `merged_pr` only — a
#: runner declared dead while still delivering can report its merged PR (`routes/tasks/runner_settle.py::end_attempt`).
#: `no_pr` is self-asserted, so excluded. Accepted cost: slotless `system_failed` rows become settleable
#: by any node credential in the org (not a runner); residual home:
#: `routes/agent/auth.py::_task_or_404_for_node`.
_DELIVERED_RECOVERY_STATES: tuple[str, ...] = (SYSTEM_FAILED,)

# ── Manifest shorthands ───────────────────────────────────────────────────────────────────────────────
# REQUIRED_HARD refuses the settle without the row; REQUIRED_ADVISORY records the absence and proceeds.
# Hard only where the deliverable is the work: the four analysis stages, `prototyper` (no PR) and
# `researcher` (the report is the product). Advisory elsewhere — a build persona's deliverable is its
# merged PR, which a missing report must never strand.
_REPORT_HARD: tuple[tuple[str, str], ...] = (("report", REQUIRED_HARD),)
_REPORT_ADVISORY: tuple[tuple[str, str], ...] = (("report", REQUIRED_ADVISORY),)


@dataclass(frozen=True)
class Persona:
    """Everything the control plane knows about one persona, in one record.

    Optional fields are real answers, not gaps:
      · `charter_key is None` — cannot reach a runner (`epic`, `janitor`, `harvest`/`triage`); `model`,
        `mode` and `delivery` are None with it.
      · `terminals == ()` — no terminal call (`epic`, `advisor`).
      · `done_criterion is None` — `epic` alone (its state is its children's rollup).
      · `option_label is None` — not offered in the Persona picker (`operator_pickable` derives from it).
    """

    #: The value stored in `tasks.persona`. Identifiers and values agree everywhere.
    name: str
    #: The board/cell label. Pipeline stages read as verbs (`business_analysis` is "Analyze"); the picker
    #: names the role, hence the separate `option_label`.
    label: str
    #: A pipeline persona (the epic container and work personas) rather than a hidden system one. Disjoint:
    #: system personas come only from the agent-token route, `hidden = true`.
    pipeline: bool
    #: The runner charter, `app/dispatch_compose/roles.py::_ROLES`' key. Not injective (`coder` runs
    #: `implementer`).
    charter_key: Optional[str] = None
    #: The stage default model — one rung of `app/model_catalog.py::MODEL_PRECEDENCE`; a Task's pin
    #: outranks it.
    model: Optional[str] = None
    #: Which flow of the universal charter governs this role's finish. One of `MODES`.
    mode: Optional[str] = None
    #: The role's RESULT channel. One of `DELIVERIES`.
    delivery: Optional[str] = None
    #: What moves the row to `done`. One of `DONE_CRITERIA`.
    done_criterion: Optional[str] = None
    #: The endings a settle call from this persona may declare — a subset of
    #: `app/task_manifest.py::TERMINAL_TYPES`. Empty means no terminal call at all (see the class note).
    terminals: tuple[str, ...] = ()
    #: What this persona must have delivered before it may settle: `(kind, level)` pairs. Empty on a
    #: persona with terminals would mean "owes nothing", so the shape test derives it from `terminals`.
    #: Never rows here: `coders`/`finish_report` (CP-computed), `merged_pr`/`no_pr` (terminal-type
    #: obligations), and free-form `file` (never owed).
    manifest: tuple[tuple[str, str], ...] = ()
    #: The states a runner may settle its own task from. The operator `complete` verb and
    #: `resolve_system_task` are wider and not gated here.
    settle_states: tuple[str, ...] = ()
    #: The Persona-picker option text, or None when not offered. Offered only if it settles at top level:
    #: `plan`/`finish` need an epic parent, and system personas are the agent route's.
    option_label: Optional[str] = None
    #: The one-line operator-facing description, or None where the picker copy covers it. Read by
    #: `GET /api/tasks/personas`.
    hint: Optional[str] = None
    #: Is a row created with this persona born `autonomous = false`? A default only, applied when the
    #: caller submitted no value — Full Auto stays editable.
    autonomous_default: Optional[bool] = None
    #: Does this persona interview the person whatever Full Auto says? Dispatch reads it before any other rule
    #: (`routes/tasks/task_wire.py::_task_effective_autonomous`), so it asks even under a Full Auto epic.
    always_interviews: bool = False

    @property
    def operator_pickable(self) -> bool:
        """Whether an operator may pick this persona on a top-level Task — `option_label is not None`.

        Derived, so "offered" and "has picker copy" cannot disagree."""
        return self.option_label is not None

    @property
    def dispatchable(self) -> bool:
        """Whether this persona can reach a runner at all — it holds a charter."""
        return self.charter_key is not None


def _system_persona(name: str, label: str, model: Optional[str] = None, mode: Optional[str] = None, *,
                    pr: bool = False) -> Persona:
    """One hidden system persona. No `model` = no charter (so no mode or delivery); `pr` = it ships a PR and
    settles on the merge, where every other one settles `outcome`."""
    chartered = model is not None
    return Persona(
        name=name, label=label, pipeline=False,
        charter_key=name if chartered else None, model=model, mode=mode,
        delivery=(DELIVERY_PR if pr else DELIVERY_OUTCOME) if chartered else None,
        done_criterion=DONE_CRITERION_MERGED_PR if pr else DONE_CRITERION_OUTCOME,
        terminals=("merged_pr", "no_pr") if pr else ("outcome",),
        manifest=_REPORT_ADVISORY, settle_states=_ACTIVE_ONLY,
    )


# ── THE REGISTRY ──────────────────────────────────────────────────────────────────────────────────────
# One declaration order, inherited by every derivation. Chosen for the one rendered derivation, the
# Persona picker (`apps/web` filters on `operator_pickable` in arrival order): `epic` first, `coder`
# (the default), the analysis roles, `advisor` last, then the hidden system rows. Every other tuple is
# membership-only (SQL `IN`, `not in`, `frozenset`). Do not make any other file's line order depend on
# this one.
PERSONA_RECORDS: tuple[Persona, ...] = (
    # ── The container ─────────────────────────────────────────────────────────────────────────────────
    Persona(
        # A non-dispatched container: its state is its children's rollup, so no charter, terminal,
        # manifest or criterion. Registered because `tasks.persona` carries it and every label and
        # validation surface must answer for it.
        name="epic", label="Epic", pipeline=True,
    ),
    # ── The pipeline work personas ────────────────────────────────────────────────────────────────────
    Persona(
        # The build persona — writes code and ships a PR, as an epic child or a top-level unit
        # (`parent_task_id` tells them apart). Charter key stays "implementer".
        name="coder", label="Coder", pipeline=True,
        charter_key="implementer", model="high", mode=MODE_BUILD, delivery=DELIVERY_PR,
        done_criterion=DONE_CRITERION_MERGED_PR,
        terminals=("merged_pr", "no_pr"), manifest=_REPORT_ADVISORY, settle_states=_ACTIVE_ONLY,
        option_label="Coder (default)",
        hint="builds the work item and ships a merged PR",
    ),
    Persona(
        name="business_analysis", label="Analyze", pipeline=True,
        charter_key="ba", model="high", mode=MODE_ANALYSIS, delivery=DELIVERY_DB,
        done_criterion=DONE_CRITERION_ARTIFACT,
        terminals=("analysis",),
        manifest=(("report", REQUIRED_HARD), ("prd", REQUIRED_HARD)),
        settle_states=DB_DIRECT_SETTLE_STATES,
        option_label="Business analyst",
        hint="settles the requirements and hands back a PRD",
    ),
    Persona(
        # The first stage of a new web app's epic (`app/project_onboarding.py`): a business-only interview of the
        # person who described the app, handed to the Architect as a PRD. Filed by the control plane alone, so
        # never in the picker. It always asks (`always_interviews`) while the epic's later stages run Full Auto.
        # Its role prose is cloud-only, like the hosting it serves (`dispatch_compose/roles_app_wizard.py`).
        name="app_wizard", label="App Wizard", pipeline=True,
        charter_key="app_wizard", model="high", mode=MODE_ANALYSIS, delivery=DELIVERY_DB,
        done_criterion=DONE_CRITERION_ARTIFACT,
        terminals=("analysis",),
        manifest=(("report", REQUIRED_HARD), ("prd", REQUIRED_HARD)),
        settle_states=DB_DIRECT_SETTLE_STATES,
        always_interviews=True,
    ),
    Persona(
        name="architecture", label="Design", pipeline=True,
        charter_key="architect", model="high", mode=MODE_ANALYSIS, delivery=DELIVERY_DB,
        done_criterion=DONE_CRITERION_ARTIFACT,
        terminals=("analysis",),
        manifest=(("report", REQUIRED_HARD), ("architecture", REQUIRED_HARD)),
        settle_states=DB_DIRECT_SETTLE_STATES,
        option_label="Architect",
        hint="settles the design and hands back an architecture document",
    ),
    Persona(
        # The researcher: a report is its product. One question taken deep with blind critical review
        # (2–5 rounds). Build mode with a hard report, so it may also ship durable guidance as a docs PR:
        # `merged_pr` then, else `no_pr` + `no_code_needed` (`app/dispatch_compose/roles.py::_ROLES["researcher"]`).
        # Opus for depth; no `autonomous_default`.
        name="researcher", label="Researcher", pipeline=True,
        charter_key="researcher", model="high", mode=MODE_BUILD, delivery=DELIVERY_PR,
        done_criterion=DONE_CRITERION_MERGED_PR,
        terminals=("merged_pr", "no_pr"), manifest=_REPORT_HARD, settle_states=_ACTIVE_ONLY,
        option_label="Researcher",
        hint=(
            "researches one question in depth, puts its findings through rounds of critical review, and "
            "hands back a report — plus a docs PR when the findings are guidance the repo should keep"
        ),
    ),
    Persona(
        # Not offered in the picker: its job is appending children to an epic (`append_tasks` refuses a
        # non-epic parent). It writes the `coder` children and the `finish` child (`SHAPE_TASKS`).
        # `coders` settles a decomposition (refused with none appended); `analysis` settles that there is
        # NOTHING to build — the plan document is the epic's deliverable (refused once children were
        # appended). The CP tells the two apart by the `no_build` stamp it writes
        # (`app/task_settle.py::NO_BUILD_OUTCOME_REASON`).
        name="plan", label="Plan", pipeline=True,
        charter_key="planner", model="high", mode=MODE_ANALYSIS, delivery=DELIVERY_DB,
        done_criterion=DONE_CRITERION_ARTIFACT_WITH_CODERS,
        terminals=("coders", "analysis"), manifest=_REPORT_HARD, settle_states=DB_DIRECT_SETTLE_STATES,
    ),
    Persona(
        # Terminal analytical phase: a dispatched read-only runner settled by the server-computed
        # `execute_finish_phase` (the runner attests nothing). Not in the picker: its report is computed
        # over its siblings.
        name="finish", label="Finish", pipeline=True,
        charter_key="finish", model="high", mode=MODE_ANALYSIS, delivery=DELIVERY_DB,
        done_criterion=DONE_CRITERION_ARTIFACT,
        terminals=("finish",), manifest=_REPORT_HARD, settle_states=_FINISH_SETTLE_STATES,
    ),
    Persona(
        # Recurring test-suite cost pass. Implementer-style,
        # delivers a merged PR, but its subject is the suite. Opus: the work is measurement judgement.
        name="test_optimizer", label="Test optimizer", pipeline=True,
        charter_key="test_optimizer", model="high", mode=MODE_BUILD, delivery=DELIVERY_PR,
        done_criterion=DONE_CRITERION_MERGED_PR,
        terminals=("merged_pr", "no_pr"), manifest=_REPORT_ADVISORY, settle_states=_ACTIVE_ONLY,
        option_label="Test optimizer",
        hint="measures the test suite's cost and ships a PR that reduces it",
    ),
    Persona(
        # The migration persona: prepares an existing repo for fleet runners, following
        # the Jaah onboarding pattern. Build mode, PR pair, advisory report; Opus.
        # Writes its own project row (`PROJECT_SETTINGS_LICENCE`; governance columns still pass
        # `routes/projects.py::has_authority_to_author_governance` for the launcher).
        name="onboard", label="Repo Wizard", pipeline=True,
        charter_key="onboard", model="high", mode=MODE_BUILD, delivery=DELIVERY_PR,
        done_criterion=DONE_CRITERION_MERGED_PR,
        terminals=("merged_pr", "no_pr"), manifest=_REPORT_ADVISORY, settle_states=_ACTIVE_ONLY,
        # One of the three wizards a new project starts with (App, Repo, Business): `label`, `option_label`
        # and the charter title say the same name.
        option_label="Repo Wizard",
        hint=(
            "prepares an existing repo to take fleet tasks — ports, gates, instrumentation and a "
            "codebase analysis, shipped as one additive PR that changes nothing for its developers"
        ),
    ),
    Persona(
        # The business project's wizard (`app/project_onboarding.py::file_business_wizard_task`): one task that
        # interviews a non-technical owner about one business activity (sales, marketing, research…), records it
        # in the repo as agent context and skills, and ships that as one PR. Filed on create for a business
        # project, new or existing; also pickable to re-run on any project. Never scheduled or filed by an agent:
        # it always interviews (`always_interviews`), so nobody would be there to answer.
        name="business_wizard", label="Business Wizard", pipeline=True,
        charter_key="business_wizard", model="high", mode=MODE_BUILD, delivery=DELIVERY_PR,
        done_criterion=DONE_CRITERION_MERGED_PR,
        terminals=("merged_pr", "no_pr"), manifest=_REPORT_ADVISORY, settle_states=_ACTIVE_ONLY,
        option_label="Business Wizard",
        hint=(
            "interviews you about a business activity — sales, marketing, research and the like — then records "
            "it in the project, with the skills its agents need, so later tasks start knowing your business"
        ),
        always_interviews=True,
    ),
    Persona(
        # The repo move (`app/jaah_repo_replace.py`): moves a Jaah-made repo's code and history into the project's
        # own repository — pushed as is to an empty one, else a branch and a PR it never merges. Filed by the
        # control plane alone, as the person who saved the switch, so never in the picker but on their board. Its
        # success is the control plane's to verify (`app/jaah_repo_retire.py`), so it settles `outcome`. Opus.
        name="repo_move", label="Move repo", pipeline=True,
        charter_key="repo_move", model="high", mode=MODE_BUILD, delivery=DELIVERY_OUTCOME,
        done_criterion=DONE_CRITERION_OUTCOME,
        terminals=("outcome",), manifest=_REPORT_ADVISORY, settle_states=_ACTIVE_ONLY,
    ),
    Persona(
        # The localization persona: translates a repo's source-language text into its target languages
        # and ships it as a PR, like `coder`. Its gate is the delta — every new or changed source string
        # translated in every target locale, every unchanged one skipped byte-identical. The procedure is
        # the `translate` skill, its reviews the `translation-reviewer`/`back-translator` subagents, all
        # shipped with the `jaah` package and named by its charter (`_ROLES["translate"]`). Opus:
        # tone, humor and context are judgement. Also filed as an epic build child by `plan`
        # (`app/routes/tasks/policy.py::EPIC_BUILD_PERSONAS`).
        name="translate", label="Translate", pipeline=True,
        charter_key="translate", model="high", mode=MODE_BUILD, delivery=DELIVERY_PR,
        done_criterion=DONE_CRITERION_MERGED_PR,
        terminals=("merged_pr", "no_pr"), manifest=_REPORT_ADVISORY, settle_states=_ACTIVE_ONLY,
        option_label="Translate",
        hint=(
            "translates new and changed text into the repo's other languages — glossary, tone and jokes "
            "kept — and ships a PR"
        ),
    ),
    Persona(
        # The marketing persona: writes a repo's marketing material in its brand voice and
        # ships it as a PR, build mode like `coder`. The procedure is the `marketing-copy` skill, its
        # review the blind `copy-reviewer` subagent, both shipped with the `jaah` package and named by
        # its charter (`_ROLES["marketer"]`). Opus: voice and judgement. Also filed as an epic build
        # child by `plan` (`app/routes/tasks/policy.py::EPIC_BUILD_PERSONAS`).
        name="marketer", label="Marketer", pipeline=True,
        charter_key="marketer", model="high", mode=MODE_BUILD, delivery=DELIVERY_PR,
        done_criterion=DONE_CRITERION_MERGED_PR,
        terminals=("merged_pr", "no_pr"), manifest=_REPORT_ADVISORY, settle_states=_ACTIVE_ONLY,
        option_label="Marketer",
        hint=(
            "writes marketing material — website pages, blog essays, lead magnets, emails, social "
            "posts — in the repo's brand voice, blind-reviewed, and ships a PR"
        ),
    ),
    Persona(
        # The speed persona, and the one shipped with no review lane (no reviewers, verifier, `/verify`
        # or spec-first gate). Product: one self-contained HTML mockup uploaded as a `report` artifact.
        #
        # Artifact only, never a PR: `outcome` (nothing to record or stamp), a hard report row, and the
        # analysis finish flow. Haiku by default for speed and cost; a stronger pin is honoured.
        name="prototyper", label="Prototyper", pipeline=True,
        charter_key="prototyper", model="low", mode=MODE_ANALYSIS, delivery=DELIVERY_DB,
        done_criterion=DONE_CRITERION_OUTCOME,
        terminals=("outcome",), manifest=_REPORT_HARD, settle_states=_ACTIVE_ONLY,
        option_label="Prototyper",
        hint="throws a mockup together and delivers it as an artifact, never a PR",
    ),
    Persona(
        # The interactive chat persona: no terminal call; only the operator's Complete ends it, hence an
        # explicit `DONE_CRITERION_OPERATOR`. Last in the picker, but offered (chat↔task conversion).
        name="advisor", label="Advisor", pipeline=True,
        charter_key="advisor", model="high", mode=MODE_BUILD, delivery=DELIVERY_PR,
        done_criterion=DONE_CRITERION_OPERATOR,
        option_label="Advisor",
        hint="talks to you in the console until you mark it complete",
        # "Run without asking" contradicts a live chat; no interview mandate is implied.
        autonomous_default=False,
    ),
    # ── The blind review children (`app/blind_review.py`) ──────────────────────────────────────────────────
    # Filed by the CP alone, `ready` under a coder task whose declared tier asks for them (A7), never in the
    # picker. Read-only analysis of the coder's PR diff, settled DB-direct with `review` and its findings, which
    # land against the coder (advisory, D2). Full Auto, since no human waits on them.
    *(
        Persona(
            name=name, label=label, pipeline=True,
            charter_key=name, model="high", mode=MODE_ANALYSIS, delivery=DELIVERY_DB,
            done_criterion=DONE_CRITERION_OUTCOME,
            terminals=("review",), manifest=_REPORT_ADVISORY, settle_states=_ACTIVE_ONLY,
            autonomous_default=True,
        )
        for name, label in (("reviewer", "Reviewer"), ("security_reviewer", "Security reviewer"),
                            ("tester", "Tester"))
    ),
    # ── The hidden system personas ────────────────────────────────────────────────────────────────────
    # Created `hidden = true`, top-level, by the agent-token route alone, via the DB-direct settle. Never
    # in the picker; disjoint from the pipeline vocabulary.
    _system_persona("maturity", "Maturity", "standard", MODE_BUILD),
    # One run per project per Portfolio Compliance scan (`app/governance/scans.py`):
    # it drives `jaah-audit` and submits through it, never editing the repo, so it reads as analysis.
    # Its verdicts land through `routes/agent_governance.py`; the settle only closes the run.
    _system_persona("compliance_auditor", "Compliance auditor", "standard", MODE_ANALYSIS),
    _system_persona("portfolio_drift", "Portfolio drift", "standard", MODE_BUILD),
    _system_persona("adhoc", "Ad-hoc", "standard", MODE_BUILD),
    # `delivery="pr"`: ships a PR remediating the deficiencies and settles on `merged_pr`, like `coder`.
    _system_persona("compliance_fix", "Compliance fix", "standard", MODE_BUILD, pr=True),
    # Settles but holds no charter: its self-heal recovery owns its settle. Registered for the settle
    # door (criterion, states) and the board label.
    _system_persona("janitor", "Janitor", pr=True),
    # No charter yet — no producer dispatches it. Registered for the same reason `janitor` is.
    _system_persona("harvest", "Harvest"),
    _system_persona("triage", "Triage"),
    # The merge lane (`app/merge_lane.py`): a Claude agent filed by the CP alone (`ensure_lane`) and the
    # only caller the `lane.*` verbs answer. It merges every queued PR and keeps develop green; its
    # Stop hook and the tick's watchdog never let it stall. Settles `outcome` like any system run.
    _system_persona("merge_lane", "Merge lane", "high", MODE_BUILD),
    # The release executor (`app/releases/`): filed hidden by the CP alone, one open per project. It runs the
    # packaged release tool, which merges each queued release's PR (through the merge lane when it is on); the
    # model judges nothing, so the cheapest tier. It pushes a release branch and merges a PR, so build.
    _system_persona("release", "Release", "low", MODE_BUILD),
    # The outcome ledger's forge reader (`app/outcome_ledger/scan.py`): filed hidden by the CP alone, one open
    # per project, when the project is due. It runs `tools/outcome_scan.py`, whose facts the CP judges; the
    # model judges nothing, so the cheapest tier. Read-only, so analysis.
    _system_persona("outcome_scan", "Outcome scan", "low", MODE_ANALYSIS),
    # Production QA's native probes (`app/production_qa/probes.py`): filed hidden by the CP alone, one per
    # successful production deploy of an opted-in project. It runs `tools/deploy_probe.py`, whose results the CP
    # records as incidents; the model judges nothing, so the cheapest tier. Edits nothing, so analysis.
    _system_persona("deploy_probe", "Deploy probes", "low", MODE_ANALYSIS),
    # Benchmark mode's blind judge (`app/outcome_ledger/benchmark.py`): filed hidden by the CP alone, one per duel
    # (a review, then close-only retries), on a provider other than both duelists when the org has one. Its
    # toolbox (`tools/duel_review.py`) diffs both sides, reports the verdict and closes the shadow PR; the model
    # judges, so the analysis default tier.
    _system_persona("duel_review", "Duel review", "high", MODE_ANALYSIS),
)

#: The registry by name — the one lookup, read-only.
REGISTRY: Mapping[str, Persona] = MappingProxyType({p.name: p for p in PERSONA_RECORDS})

#: The whole vocabulary as bare names, in registry order.
PERSONA_NAMES: tuple[str, ...] = tuple(REGISTRY)


def persona(name: Optional[str]) -> Optional[Persona]:
    """One persona's record, or None for an unknown name.

    None rather than a raise, so a row with an unrecognised persona renders and refuses cleanly.
    Callers needing the strict answer test membership in `REGISTRY`."""
    if name is None:
        return None
    return REGISTRY.get(name)


# ── The derivations ───────────────────────────────────────────────────────────────────────────────────
# Derived from the rows above, never edited by hand; exported under the names their readers use.

#: `persona -> (charter_key, default_model)` for every dispatchable persona. Absent means
#: `dispatch_compose` returns an `error` verdict, never a quiet implementer.
PERSONA_CHARTER: Mapping[str, tuple[str, str]] = MappingProxyType({
    p.name: (p.charter_key, p.model) for p in PERSONA_RECORDS if p.charter_key and p.model
})

#: `charter_key -> mode` / `charter_key -> delivery` for `dispatch_compose`. Well-defined only while
#: personas sharing a charter key agree on both knobs — `test_persona_registry.py` asserts it.
_CHARTER_MODE: Mapping[str, str] = MappingProxyType({
    p.charter_key: p.mode for p in PERSONA_RECORDS if p.charter_key and p.mode
})
_CHARTER_DELIVERY: Mapping[str, str] = MappingProxyType({
    p.charter_key: p.delivery for p in PERSONA_RECORDS if p.charter_key and p.delivery
})


def charter_mode(charter_key: str) -> Optional[str]:
    """Which charter flow a ROLE's finish runs — `MODE_BUILD` or `MODE_ANALYSIS`."""
    return _CHARTER_MODE.get(charter_key)


def charter_delivery(charter_key: str) -> Optional[str]:
    """A ROLE's result channel — one of `DELIVERIES`."""
    return _CHARTER_DELIVERY.get(charter_key)


#: Which terminals each settling persona may declare. A persona with no terminal call is absent, not
#: empty ("settles owing nothing" must never be true of `advisor`).
PERSONA_TERMINALS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    p.name: p.terminals for p in PERSONA_RECORDS if p.terminals
})

#: The one ending a benchmark shadow coder claims instead of its persona's (`app/outcome_ledger/benchmark.py`): its
#: draft PR, never a merge. A task-level swap like a folder build's, read by `task_settle.terminals_of`.
SHADOW_TERMINALS: tuple[str, ...] = ("shadow_pr",)

#: What each settling persona must have delivered before it may settle.
PERSONA_MANIFEST: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType({
    p.name: p.manifest for p in PERSONA_RECORDS if p.manifest
})

#: The states each persona may settle its OWN task from — the source-state axis of the unified settle.
PERSONA_SETTLE_STATES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    p.name: p.settle_states for p in PERSONA_RECORDS if p.settle_states
})


def settle_sources(persona: str, terminal_type: str | None) -> tuple[str, ...]:
    """The states this settle may move the row from — the persona's own set, plus the delivered-terminal
    recovery arm (`_DELIVERED_RECOVERY_STATES`) when the call carries one. The single home for that sum,
    which the settle route reads three times (guard, guarded UPDATE, helper `sources`).

    Own states first, so refusal messages lead with the ordinary case. An empty own-set stays empty:
    the arm extends a settle door and never manufactures one."""
    own = PERSONA_SETTLE_STATES.get(persona, ())
    if not own or terminal_type != DONE_CRITERION_MERGED_PR:
        return own
    return own + tuple(s for s in _DELIVERED_RECOVERY_STATES if s not in own)

#: What "done" means, per persona. `epic` is absent — its state is its children's rollup.
PERSONA_DONE_CRITERION: Mapping[str, str] = MappingProxyType({
    p.name: p.done_criterion for p in PERSONA_RECORDS if p.done_criterion
})

#: The pipeline vocabulary (epic container plus work personas) and the hidden system one. Disjoint:
#: `pipeline` is one boolean.
PIPELINE_PERSONAS: tuple[str, ...] = tuple(p.name for p in PERSONA_RECORDS if p.pipeline)
SYSTEM_PERSONAS: tuple[str, ...] = tuple(p.name for p in PERSONA_RECORDS if not p.pipeline)

#: The non-epic personas an operator may pick as a top-level Task's Persona, in picker (declaration)
#: order. The create gate, the conversion gate and the picker all read this.
#: Paired by position with `task_default_persona`'s `choice_help` (`app/operator_settings/catalog_tasks.py`):
#: adding or reordering a pickable persona needs its line there (an always-interviewing one is no default and has
#: none), or the catalogue refuses to import.
OPERATOR_PERSONAS: tuple[str, ...] = tuple(p.name for p in PERSONA_RECORDS if p.operator_pickable)

#: The personas a Runner cron may fire: every pickable persona but `advisor`, which ends only on an
#: operator's Complete and is never idle-reaped, so a scheduled one would hold its worktree slot forever, and
#: `business_wizard`, which always asks and would wait on nobody.
SCHEDULABLE_PERSONAS: tuple[str, ...] = tuple(
    persona for persona in OPERATOR_PERSONAS if persona not in ("advisor", "business_wizard")
)

#: The personas a trigger may file: any a cron may schedule, plus `epic` (never `advisor`, R2).
TRIGGER_PERSONAS: tuple[str, ...] = (*SCHEDULABLE_PERSONAS, "epic")

#: The personas an operator may create or convert a top-level Task to: the epic plus the pickable
#: personas. Anything else is a 422.
CREATABLE_PERSONAS: tuple[str, ...] = ("epic",) + OPERATOR_PERSONAS

#: The personas only the control plane files, through the console's own create and Start as the person whose action
#: asked for it (`routes/tasks/create.py::ServerTaskCreate`, which no request body can be): never picked, never
#: converted to.
SERVER_FILED_PERSONAS: tuple[str, ...] = ("repo_move",)

# The picker's option list is `OPERATOR_PERSONAS` crossed with each row's `option_label`, served by
# `GET /api/tasks/personas` — not a separate constant here.

#: The picker field's only operator-facing copy, so it names every group offered; per-persona `hint`s
#: say the same one row at a time.
PERSONA_PICKER_HINT = (
    "Which runner persona picks this task up. The default coder builds and ships a PR; the prototyper "
    "throws one together and delivers it as an artifact, never a PR; the analysis roles produce a "
    "settled artifact instead; the researcher answers one question in depth as a reviewed "
    "report, adding a docs PR only when the findings are guidance; the Repo Wizard prepares a "
    "repo new to Jaah to take tasks at all; the Business Wizard interviews you about a business activity "
    "and records it for later tasks; translate localizes new and changed text and ships a PR; "
    "the marketer writes website, blog, lead-magnet, email and social copy in the repo's voice and "
    "ships a PR; "
    "the advisor just talks to you in the "
    "console until you mark it complete."
)

#: `persona -> board label`, system personas included (the Needs-attention view shows hidden rows).
PERSONA_LABELS: Mapping[str, str] = MappingProxyType({p.name: p.label for p in PERSONA_RECORDS})

#: What a project's `default_persona` may name: every schedulable persona (no `advisor`: a create that named no persona
#: never silently opens a chat holding a slot; no `business_wizard`, which always asks) but `onboard`, which an implicit default would start on every unnamed
#: create — rewriting the project's own settings (`PROJECT_SETTINGS_LICENCE`) is the operator's call to start.
PROJECT_DEFAULT_PERSONAS: tuple[str, ...] = tuple(
    persona for persona in SCHEDULABLE_PERSONAS if persona != "onboard"
)

#: The personas that interview whatever Full Auto says (`Persona.always_interviews`).
ALWAYS_INTERVIEWING_PERSONAS: frozenset[str] = frozenset(p.name for p in PERSONA_RECORDS if p.always_interviews)

#: The `autonomous` value a row is BORN with, where the persona decides one at all.
PERSONA_CREATE_AUTONOMOUS: Mapping[str, bool] = MappingProxyType({
    p.name: p.autonomous_default for p in PERSONA_RECORDS if p.autonomous_default is not None
})
