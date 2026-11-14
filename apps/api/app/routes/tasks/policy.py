"""Task policy: operator-tunable caps and timeouts, the persona vocabulary and per-persona defaults."""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.operator_settings import reads as settings_reads
from app.models.projects import Project
from app.models.tasks import Task
from app.persona_registry import (
    ALWAYS_INTERVIEWING_PERSONAS,
    DB_DIRECT_SETTLE_STATES,
    DONE_CRITERION_OPERATOR,
    OPERATOR_PERSONAS,
    PERSONA_CREATE_AUTONOMOUS,
    PERSONA_DONE_CRITERION,
    PERSONA_RECORDS,
)
from app.task_lifecycle import STATES, _RUNNER_END_STATES
from app.entity_shape import or_404


def fleet_dispatch_cap(*, org: UUID) -> int:
    """The live re-dispatch ceiling for ORG (the task's own `org_id`). Read per call, never snapshotted.
    Churn backstop: this many dispatch attempts without `done` lands `system_failed`. `dispatch_attempts`
    counts attempts STARTED; the ceiling is evaluated when an attempt ENDS, not at pickup. Operator-editable;
    default and bounds in `app/operator_settings/`."""
    return settings_reads.get_int("fleet_dispatch_cap", org=org)


def epic_fanout_cap(*, org: UUID) -> int:
    """The live epic fan-out ceiling for ORG (the epic's own `org_id`). Read per call, never snapshotted.
    At this many `draft → ready` failures the PARENT EPIC (never the child) goes `system_failed`.
    Operator-editable; default and bounds in `app/operator_settings/`."""
    return settings_reads.get_int("fleet_epic_fanout_cap", org=org)


# Grace before an unanswered runner question ends the attempt. Read per pass, so lengthening it while a
# row is parked lengthens THAT row's grace.
SETTING_UNANSWERED_QUESTION_TIMEOUT_HOURS = "unanswered_question_timeout_hours"


def unanswered_question_timeout() -> timedelta:
    """The live grace an unanswered question gets before its RUNNER is stopped, as a `timedelta`."""
    return timedelta(hours=settings_reads.get_int(SETTING_UNANSWERED_QUESTION_TIMEOUT_HOURS))


# The second, longer bound: past it the task is failed and its worktree handed back, WIP and all.
SETTING_UNANSWERED_QUESTION_RELEASE_DAYS = "unanswered_question_release_days"


def unanswered_question_release_after() -> timedelta:
    """The live age at which an unanswered question FAILS its task and frees its worktree."""
    return timedelta(days=settings_reads.get_int(SETTING_UNANSWERED_QUESTION_RELEASE_DAYS))

# ── Persona vocabulary (Task.persona, NOT NULL — every row carries exactly one) ───────────────────
# App-layer constant, no DB CHECK/enum.
PERSONA_EPIC = "epic"
PERSONA_BUSINESS_ANALYSIS = "business_analysis"
PERSONA_ARCHITECTURE = "architecture"
PERSONA_PLAN = "plan"
# A new web app's first epic stage: a business-only interview that always asks (`persona_registry.py`).
PERSONA_APP_WIZARD = "app_wizard"
# The build persona (writes code, ships a PR), top-level or epic child — `parent_task_id` alone carries
# which. Disjoint from `SYSTEM_PERSONAS`.
PERSONA_CODER = "coder"
# Terminal analytical phase: a dispatched read-only role settled by `execute_finish_phase` (server-derived
# report; the runner attests nothing).
PERSONA_FINISH = "finish"
# Recurring test-suite cost pass; operator-pickable, so a
# pipeline persona rather than a system one.
PERSONA_TEST_OPTIMIZER = "test_optimizer"
# Interactive chat: the runner parks at its prompt (`VISIBLE_AUTOCLOSE=0`) and nothing it can POST ends the
# row — only the operator's Complete does (`DONE_CRITERION_OPERATOR`).
PERSONA_ADVISOR = "advisor"
# Speed persona: no review lane of any kind; its product is one self-contained HTML mockup uploaded as a
# `report` artifact. ARTIFACT ONLY, NEVER A PR (owner's decision) — `outcome` criterion, absent from
# `MERGED_PR_CRITERION_PERSONAS`, charter defaults it to haiku.
PERSONA_PROTOTYPER = "prototyper"
# Localization persona: a build persona like `coder` (merged PR), whose work is translating new and changed
# source text. Operator-pickable, and a `plan` breakdown may file it as an epic build child.
PERSONA_TRANSLATE = "translate"
# Marketing persona: a build persona like `translate` (merged PR), whose work is marketing copy in the
# repo's brand voice. Operator-pickable, and a `plan` breakdown may file it as an epic build child.
PERSONA_MARKETER = "marketer"
# A business project's first task (`persona_registry.py`): interviews the owner, then records the business activity in
# the repo and ships a PR. Always asks, so never filed where nobody answers.
PERSONA_BUSINESS_WIZARD = "business_wizard"
# The personas a `plan` breakdown may file as an epic's BUILD children (`coder` the default). Every site
# that counts, archives, rolls up or judges an epic's build children reads this, never `PERSONA_CODER`
# alone, so a `translate` or `marketer` child is a full sibling of a `coder` one.
EPIC_BUILD_PERSONAS: tuple[str, ...] = (PERSONA_CODER, PERSONA_TRANSLATE, PERSONA_MARKETER)


def project_default_persona(stored: Optional[str], allowed: Iterable[str]) -> str:
    """The persona a create that names none files on a project whose `default_persona` is `stored`: that persona when
    this create may file it (`allowed`), else `coder` — so a project default never widens what a path may file."""
    return stored if stored is not None and stored in allowed else PERSONA_CODER

# `PERSONAS`, `OPERATOR_PERSONAS`, `CREATABLE_PERSONAS`: derived in `app/persona_registry.py`, where the
# exclusion of `plan`/`finish` from the picker is argued on the rows.

# ── System personas ──────────────────────────────────────────────────────────────────────────────────
# Non-pipeline: created `hidden = true`, top-level, reporting through the same DB-direct API.
PERSONA_JANITOR = "janitor"
PERSONA_COMPLIANCE_FIX = "compliance_fix"
PERSONA_MATURITY = "maturity"
PERSONA_COMPLIANCE_AUDITOR = "compliance_auditor"
PERSONA_PORTFOLIO_DRIFT = "portfolio_drift"
PERSONA_HARVEST = "harvest"
PERSONA_TRIAGE = "triage"
PERSONA_ADHOC = "adhoc"

# ── Task-creation authority ──────────────────────────────────────────────────────────────────────────
# Every persona holds every shape (`persona_registry.TASK_CREATION_LICENCE`); the per-endpoint path check stays here (`append_tasks`: parent is an epic;
# `append_finish_follow_up`: caller is a live `finish` child). Neither half may be dropped.

# ── SHAPE_TOP_LEVEL's Depth vocabulary — the console's own sketch · task · epic axis ─────────────────
# `sketch` files a `prototyper`, `epic` files an epic plus its analysis children, and `task` files one
# single-row persona: `coder` unless the filer names another. The nameable set is the operator's pickable
# personas minus `prototyper` (that is `sketch`), `advisor` (a chat ends only on a person's Complete, so a
# filed one would hold a slot with nobody in it) and `onboard` (rewriting the project's own settings is the
# operator's call to start, as `persona_registry.PROJECT_DEFAULT_PERSONAS` argues), and `business_wizard` (it always
# asks, and an agent's filing has nobody to answer).
TOP_LEVEL_DEPTH_SKETCH = "sketch"
TOP_LEVEL_DEPTH_TASK = "task"
TOP_LEVEL_DEPTH_EPIC = "epic"
TOP_LEVEL_DEPTHS: tuple[str, ...] = (TOP_LEVEL_DEPTH_SKETCH, TOP_LEVEL_DEPTH_TASK, TOP_LEVEL_DEPTH_EPIC)
TOP_LEVEL_TASK_PERSONAS: tuple[str, ...] = tuple(
    p for p in OPERATOR_PERSONAS if p not in (PERSONA_PROTOTYPER, PERSONA_ADVISOR, "onboard", PERSONA_BUSINESS_WIZARD)
)
# Every persona a `SHAPE_TOP_LEVEL` row itself can carry — what the amend/delete doors reach.
TOP_LEVEL_FILED_PERSONAS: frozenset[str] = frozenset(
    TOP_LEVEL_TASK_PERSONAS + (PERSONA_PROTOTYPER, PERSONA_EPIC)
)

# The work graph is not bounded by who may file whom — every persona may file every shape — but by the loop
# guard on each door that makes work run unattended (`routes/agent/auth.py::require_human_started`; the
# argument: `app/task_ancestry.py::machine_origin`).

# The analysis personas an epic child can carry: done-on-artifact, unlike `coder`
# (merged PR) and `epic` (container). Differs from `INTERVIEWING_CHILD_PERSONAS` by `plan` alone and
# answers a different question; an exact-set test pins the difference.
_ANALYSIS_PERSONAS = (PERSONA_BUSINESS_ANALYSIS, PERSONA_ARCHITECTURE, PERSONA_PLAN, PERSONA_APP_WIZARD)

# The epic children that inherit the epic's Full Auto switch: `autonomous = false` means "interview me on
# requirements and design", so `plan`/`coder`/`finish` children run Full Auto unconditionally. No
# per-child toggle exists on any surface.
INTERVIEWING_CHILD_PERSONAS = frozenset((PERSONA_BUSINESS_ANALYSIS, PERSONA_ARCHITECTURE))


def epic_child_autonomous(persona: str, epic_autonomous: bool) -> bool:
    """The Full Auto an epic child dispatches with, stored on its row: an always-interviewing persona
    (`persona_registry.ALWAYS_INTERVIEWING_PERSONAS`) never, an interviewing child its epic's, any other always."""
    if persona in ALWAYS_INTERVIEWING_PERSONAS:
        return False
    return epic_autonomous if persona in INTERVIEWING_CHILD_PERSONAS else True

# Personas whose runner settles DB-direct via `/api/agent/tasks/{id}/complete`. Derived from the
# registry; `app/task_pipeline.py` derives its copy off the same records.
DB_DIRECT_STAGE_PERSONAS = frozenset(
    p.name for p in PERSONA_RECORDS if p.settle_states == DB_DIRECT_SETTLE_STATES
)

# Top-level-list orphan guard: a child-only persona with `parent_task_id IS NULL` is stray data, so
# `list_tasks`'s default top-level branch excludes it (still reachable by id or `?persona=`). `plan` is
# produced ONLY by the epic fan-out; a parentless `coder` is the ordinary top-level build unit.
_ORPHAN_CHILD_PERSONAS = (PERSONA_PLAN,)

# `PERSONA_DONE_CRITERION` is read with `.get()` at every call site so an unregistered persona refuses
# cleanly instead of 500ing — which means an ABSENT entry lands a row in NO settle branch, `active`
# forever. `epic` is the one deliberate absence (its state is its children's rollup).


def create_autonomous_for_persona(persona: Optional[str], submitted: Optional[bool]) -> bool:
    """The `autonomous` a row created (or converted) into `persona` lands with.

    A submitted value (`None` = "did not say") is returned unchanged; only the absence is decided here,
    defaulting to `True` for a persona that declares nothing."""
    if submitted is not None:
        return submitted
    return PERSONA_CREATE_AUTONOMOUS.get(persona or "", True)


# ── The per-operator and per-project cap on live chats ────────────────────────────────────────────────
# A live chat holds a pool slot nothing reclaims, so these caps carry the whole load. Both axes: N
# creators can starve one project, and one principal can wedge every project. Nobody is exempt.
# Keys mirrored as literals in `app/operator_settings/catalog.py::CATALOG` (defaults there);
# `_advisor_chat_cap_or_409` re-reads them on every create and re-start.

# The knob's floor of 1 keeps an edit from meaning "no cap".
SETTING_ADVISOR_MAX_CHATS_PER_PROJECT = "advisor_max_chats_per_project"


def max_advisor_chats_per_project(*, org: UUID) -> int:
    """The live per-project chat cap for ORG (the project's own). Read per call, never snapshotted."""
    return settings_reads.get_int(SETTING_ADVISOR_MAX_CHATS_PER_PROJECT, org=org)


def max_advisor_chats_per_creator(*, org: UUID) -> int:
    """The live per-operator chat cap for ORG, across every project. Read per call, never snapshotted.
    Its default matches `MAX_USER_HOLDS` (same resource, same argument) but it is a knob of its own: a chat
    is reserved and released through a different door, so one retuning must not silently retune the other."""
    return settings_reads.get_int("advisor_max_chats_per_creator", org=org)


# The personas the chat caps apply to; a tuple so a second console-held persona joins rather than being
# remembered into it.
SLOT_HOLDING_INTERACTIVE_PERSONAS: tuple[str, ...] = (PERSONA_ADVISOR,)

# Personas whose done-criterion is a merged PR; what `settle_coder_on_merge`/`settle_coder_no_pr` guard
# on. No parent-NOT-NULL side condition.
MERGED_PR_CRITERION_PERSONAS = tuple(
    persona for persona, criterion in PERSONA_DONE_CRITERION.items() if criterion == "merged_pr"
)

# Personas an OPERATOR ends and nobody else. `reassert_active` keys its turn-boundary park on this set
# and `reclaim_awaiting_response_task` keys the re-arm on it, so they agree by construction.
OPERATOR_CRITERION_PERSONAS = tuple(
    persona for persona, criterion in PERSONA_DONE_CRITERION.items()
    if criterion == DONE_CRITERION_OPERATOR
)

# The only reasons a `merged_pr`-criterion Task may close WITHOUT a merged PR; `settle_task`
# 422s any other. Readers ask only WHETHER `outcome_no_pr_reason` is present, never which.
# MIRRORED in `apps/jaahd/agent_vocabulary.py::_TOOL_SETTLE` (jaahd cannot import this module) and
# enforced there BEFORE the wire; parity held by a test — move both.
NO_PR_CLOSE_REASONS = ("duplicate", "already_shipped", "no_code_needed", "remediated")

# What an operator's Complete records as terminal reference on a `coder` child carrying none: a hand-close
# asserts no code was owed. The evidence says a HUMAN decided it; `apply_complete` writes it.
_OPERATOR_HAND_CLOSE_REASON = "no_code_needed"
_OPERATOR_HAND_CLOSE_EVIDENCE = (
    "Closed out by operator decision in the console — no runner settled this row, so no reproduction "
    "was run. The task was declared finished by a human, not verified by an agent."
)

# ── Uniform per-persona task-body composition (single home) ──────────────────────────────────────────
# A runner body is COMPOSED on the dispatch read from template + live input, never a stored snapshot (a
# snapshot let an epic edited after fan-out reach its BA stale). `source` = where `{work_item}` comes from:
_WORK_ITEM_PARENT_EPIC = "parent_epic"   # the LIVE parent-epic body (analysis personas)
_WORK_ITEM_OWN_BODY = "own_body"         # the task's own stored body (coder's planner spec; finish)

# (template, source) per dispatchable persona; `{work_item}` is the sole substitution. `epic` and the
# system personas are absent (never dispatched / body ships unchanged).
_PERSONA_TASK_TEMPLATES: dict[str, tuple[str, str]] = {
    PERSONA_BUSINESS_ANALYSIS: (
        "Business analysis for the following work item. Clarify the problem, scope, users, and "
        "acceptance criteria; surface open questions and constraints. Do NOT design or implement.\n\n"
        "--- Work item ---\n{work_item}",
        _WORK_ITEM_PARENT_EPIC,
    ),
    PERSONA_APP_WIZARD: (
        "Interview the app's owner about the web app below, in business words only, then hand the Architect "
        "a PRD of its first version. The owner's own description is below: do not ask again what it already "
        "says. Do NOT design or implement.\n\n--- Work item ---\n{work_item}",
        _WORK_ITEM_PARENT_EPIC,
    ),
    PERSONA_ARCHITECTURE: (
        "Architecture for the following work item, building on the business analysis. Propose the "
        "technical approach, key components/interfaces, and trade-offs; call out risks. Do NOT "
        "implement.\n\n--- Work item ---\n{work_item}",
        _WORK_ITEM_PARENT_EPIC,
    ),
    PERSONA_PLAN: (
        "Plan the following work item into ordered coder tasks, building on the "
        "business analysis and architecture. Produce the child task breakdown; do NOT implement.\n\n"
        "--- Work item ---\n{work_item}",
        _WORK_ITEM_PARENT_EPIC,
    ),
    PERSONA_CODER: (
        "Implement the following work item exactly as specified — honor its acceptance criteria and "
        "stay within its scope. Nothing more, nothing less.\n\n--- Work item ---\n{work_item}",
        _WORK_ITEM_OWN_BODY,
    ),
    PERSONA_TRANSLATE: (
        "Translate the following work item exactly as specified — every new or changed source string "
        "into every target language, every unchanged one left untouched. Nothing more, nothing less.\n\n"
        "--- Work item ---\n{work_item}",
        _WORK_ITEM_OWN_BODY,
    ),
    PERSONA_MARKETER: (
        "Write the marketing copy the following work item asks for, exactly as specified — in the repo's "
        "brand voice, honoring its acceptance criteria and scope. Nothing more, nothing less.\n\n"
        "--- Work item ---\n{work_item}",
        _WORK_ITEM_OWN_BODY,
    ),
    PERSONA_BUSINESS_WIZARD: (
        "Interview the owner about the business activity below, in business words only, then record it in "
        "the repo so later tasks start knowing the business, and ship that as one PR. The owner's own words are "
        "below: do not ask again what they already say.\n\n--- Work item ---\n{work_item}",
        _WORK_ITEM_OWN_BODY,
    ),
    PERSONA_FINISH: (
        "Finish phase for this epic. Verify every build subtask resolved AND the delivered result is "
        "genuinely good, then drive the server-computed settle. Do NOT implement, merge, or push.\n\n"
        "--- Work item ---\n{work_item}",
        _WORK_ITEM_OWN_BODY,
    ),
    PERSONA_PROTOTYPER: (
        "Prototype the following work item as fast as you can — something the operator can look at "
        "now, not a finished feature. Hand it back as a single self-contained HTML file uploaded as "
        "your report artifact; nothing here is committed, branched or merged. Skip every review, "
        "verification and spec stage.\n\n--- Work item ---\n{work_item}",
        _WORK_ITEM_OWN_BODY,
    ),
}

# Personas whose STORED body is a placeholder (real work item = live parent-epic body); derived so it
# cannot drift from the `parent_epic`-sourced set.
_PLACEHOLDER_BODY_PERSONAS = frozenset(
    persona for persona, (_template, source) in _PERSONA_TASK_TEMPLATES.items()
    if source == _WORK_ITEM_PARENT_EPIC
)

# Rendered on a placeholder-persona child row; NEVER runner input (`_compose_dispatch_body` composes live).
_DISPATCH_COMPOSED_BODY = "_Automatically composed from the parent epic at dispatch time._"

# ── Epic transition-mode vocabulary (app-layer constant, no DB CHECK/enum) ──────────────────────────
# How an EPIC sequences its children's dispatch once prerequisites are `done`; read only off the parent.
TRANSITION_AUTO = "auto"
TRANSITION_HOLD = "hold"
TRANSITION_MODES = (TRANSITION_AUTO, TRANSITION_HOLD)
DEFAULT_TRANSITION_MODE = TRANSITION_AUTO

# `Task.priority`, higher = sooner: the DISPATCH worklist's order key, not the console list's. Null = 0.
_PRIORITY_ORDER = func.coalesce(Task.priority, 0).desc()

# ── Pydantic models ───────────────────────────────────────────────────────────────────────────────
# Priority bound well inside int4, so out-of-range is a 422 not a NumericValueOutOfRange 500.
_PRIORITY_ABS_MAX = 1_000_000

# Answer/comment text bound: both PERSIST into `activity_events` JSONB. Oversized and empty are 422.
MAX_TASK_TEXT_LEN = 100_000

# Agent-supplied title INTAKE bound (the stored value is `short_title`'s); not a second clipper.
MAX_TASK_TITLE_LEN = 1_000

# The chat states the cap counts: holding a slot or queued to. Derived from `_RUNNER_END_STATES`, so new
# states count by default. `paused` is excluded: the backstop reclaimer
# (`lease_drain._pause_unattended_advisors`) pauses chats to free slots, so counting it would tighten the
# cap every time. `draft` counts, or N drafts would be no cap.
_ADVISOR_CAP_COUNTED_STATES: frozenset[str] = STATES - set(_RUNNER_END_STATES)


def _advisor_chat_cap_or_409(
    session: Session, *, project_id: int, creator_id: Optional[UUID], persona: Optional[str],
    exclude_task_id: Optional[int] = None,
) -> None:
    """Refuse a chat that would push either cap over (H-1), naming the cap and the remedy. No-op for every
    persona outside `SLOT_HOLDING_INTERACTIVE_PERSONAS`.

    Read-then-create like `MAX_USER_HOLDS`, so the refusal can name numbers; concurrent creates may
    overshoot by the concurrency. 409: completing a chat makes it succeed. `creator_id` is `None` for the
    agent principal (no allowance), but the per-project arm still applies. `exclude_task_id` drops one
    existing row from both counts, so a re-start does not count itself."""
    if persona not in SLOT_HOLDING_INTERACTIVE_PERSONAS:
        return
    live_chats = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.persona == persona,
            Task.state.in_(_ADVISOR_CAP_COUNTED_STATES),
            # An archived chat holds nothing and cannot be freed, so it must not spend an allowance.
            Task.archived_at.is_(None),
        )
    )
    if exclude_task_id is not None:
        live_chats = live_chats.where(Task.id != exclude_task_id)
    in_project = session.scalar(live_chats.where(Task.project_id == project_id)) or 0
    # Both caps are read for the org of the project the chat lands in: the per-creator count spans the
    # caller's org (the seam scopes it), and the destination project is the work this chat is for.
    org = or_404(
        session.scalar(select(Project.org_id).where(Project.id == project_id)),
        detail=f"project {project_id} not found",
    )
    # Resolved once so the 409 names the number actually compared.
    project_cap = max_advisor_chats_per_project(org=org)
    if in_project >= project_cap:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this project already has {in_project} live chat(s), which is the per-project cap of "
                f"{project_cap} — every chat holds a worktree slot that its project's "
                "builds cannot then use, so Complete a chat that is finished (or Stop one to hand its "
                "slot back) before opening another here"
            ),
        )
    if creator_id is None:
        return
    # Across every project, as `MAX_USER_HOLDS` argues; the seam scopes it to the caller's org.
    mine = session.scalar(live_chats.where(Task.created_by_id == creator_id)) or 0
    creator_cap = max_advisor_chats_per_creator(org=org)  # once, into a local — see the project arm above
    if mine >= creator_cap:
        # Worded like the knob (`advisor_max_chats_per_creator`, "Advisor chats per person … across every
        # project"), the same in both consoles; no deployment-wide vocabulary (`app/tenancy/copy.py`).
        raise HTTPException(
            status_code=409,
            detail=(
                f"you already have {mine} live chat(s) across every project, which is the per-person cap "
                f"of {creator_cap} — Complete one of yours (or Stop it to hand its "
                "worktree slot back) before opening another"
            ),
        )
