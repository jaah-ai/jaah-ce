"""The Playbook vocabulary — every closed set a playbook is built from, with the human label for each value.

A **Playbook** is an ordered list of **Stages**; a stage is an **Expert** × a **Deliverable** × a
**Seniority**. This module is the single home of those sets and of the facts attached to each value:
a deliverable's `mode`, `ends_when` and filing set, an expert's native charter/persona/deliverable and the
document it hands off, a seniority's model tier. `app/playbook_validation.py` reads them, and
`GET /api/playbooks/vocabulary` (`app/routes/playbooks.py`) serves them, labels included, so no raw token
reaches the console.

BEHIND ONE SEAM: a playbook runs only through `app/playbook_engine.py`, and no task/dispatch/pipeline
module may import this one (`tests/test_playbook_isolation.py`). An expert's native `charter_key`/`persona`
reaches its prose and native deliverable; which persona a stage row carries is the engine's call
(`playbook_engine.persona_for`). The one direction allowed is playbook code reading
`app/persona_registry.py`, which `tests/test_playbook_vocabulary.py` does to hold the modes equal.

Stdlib-only, so anything (a migration included) can import it without pulling in the app.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

# ── Hand-off documents (D12) ─────────────────────────────────────────────────────────────────────────

HAND_OFF_PRD = "prd"
HAND_OFF_ARCHITECTURE = "architecture"
#: Canonical order: a stage's `receives` is sorted by it.
HAND_OFF_KINDS: tuple[str, ...] = (HAND_OFF_PRD, HAND_OFF_ARCHITECTURE)
HAND_OFF_LABELS: Mapping[str, str] = MappingProxyType({
    HAND_OFF_PRD: "PRD",
    HAND_OFF_ARCHITECTURE: "Architecture",
})

# ── Filing (what a stage may create) ─────────────────────────────────────────────────────────────────

FILE_PER_ITEM_STAGE = "per_item_stage"
FILE_FINISH = "finish"
FILE_FOLLOW_UPS = "follow_ups"
FILE_TOP_LEVEL_DRAFTS = "top_level_drafts"
FILING_LABELS: Mapping[str, str] = MappingProxyType({
    FILE_PER_ITEM_STAGE: "The stage that runs once for each planned item",
    FILE_FINISH: "The finish stage",
    FILE_FOLLOW_UPS: "Follow-ups into the implement stage",
    FILE_TOP_LEVEL_DRAFTS: "Top-level draft tasks",
})

# ── Deliverables (the job contract) ──────────────────────────────────────────────────────────────────

MODE_BUILD = "build"
MODE_ANALYSIS = "analysis"
MODE_LABELS: Mapping[str, str] = MappingProxyType({
    MODE_BUILD: "Can change code",
    MODE_ANALYSIS: "Read-only",
})

ENDS_WHEN_LABELS: Mapping[str, str] = MappingProxyType({
    "merged_pr": "Ends when its pull request merges",
    "artifact": "Ends when its document is uploaded",
    "outcome": "Ends when its HTML report is uploaded",
    "artifact_with_coders": "Ends when its plan is uploaded and its items are filed, or it settles that there is nothing to build",
    "operator": "Ends when the operator completes it",
})


@dataclass(frozen=True)
class Deliverable:
    key: str
    label: str
    ends_when: str
    mode: str
    #: Everything a stage with this deliverable may file; a stage's `may_file` may only narrow it.
    may_file: tuple[str, ...] = ()


PULL_REQUEST, DOCUMENT, MOCKUP, PLAN, REVIEW, CONVERSATION = (
    "pull_request", "document", "mockup", "plan", "review", "conversation",
)
DELIVERABLE_RECORDS: tuple[Deliverable, ...] = (
    Deliverable(PULL_REQUEST, "Pull request", "merged_pr", MODE_BUILD),
    Deliverable(DOCUMENT, "Document", "artifact", MODE_ANALYSIS),
    Deliverable(MOCKUP, "Mockup", "outcome", MODE_ANALYSIS),
    Deliverable(PLAN, "Plan", "artifact_with_coders", MODE_ANALYSIS, (FILE_PER_ITEM_STAGE, FILE_FINISH)),
    Deliverable(REVIEW, "Review", "artifact", MODE_ANALYSIS, (FILE_FOLLOW_UPS,)),
    Deliverable(CONVERSATION, "Conversation", "operator", MODE_BUILD, (FILE_TOP_LEVEL_DRAFTS,)),
)
DELIVERABLES: Mapping[str, Deliverable] = MappingProxyType({d.key: d for d in DELIVERABLE_RECORDS})

# ── Experts (domain expertise) ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Expert:
    key: str
    label: str
    #: The `_ROLES` charter key and persona whose prose this expert is — never a dispatch path.
    charter_key: str
    persona: str
    #: What a chosen-at-start stage delivers when this expert is picked (D3).
    native_deliverable: str
    #: The structured document this expert hands off when it delivers a Document (D12), or None.
    hands_off: Optional[str] = None


EXPERT_RECORDS: tuple[Expert, ...] = (
    Expert("engineer", "Engineer", "implementer", "coder", PULL_REQUEST),
    Expert("analyst", "Analyst", "ba", "business_analysis", DOCUMENT, HAND_OFF_PRD),
    Expert("architect", "Architect", "architect", "architecture", DOCUMENT, HAND_OFF_ARCHITECTURE),
    Expert("planner", "Planner", "planner", "plan", PLAN),
    Expert("reviewer", "Reviewer", "finish", "finish", REVIEW),
    Expert("ui_prototyper", "UI prototyper", "prototyper", "prototyper", MOCKUP),
    Expert("advisor", "Advisor", "advisor", "advisor", CONVERSATION),
    Expert("researcher", "Researcher", "researcher", "researcher", PULL_REQUEST),
    Expert("test_optimizer", "Test optimizer", "test_optimizer", "test_optimizer", PULL_REQUEST),
    Expert("onboarding", "Onboarding engineer", "onboard", "onboard", PULL_REQUEST),
    Expert("translator", "Translator", "translate", "translate", PULL_REQUEST),
    Expert("marketer", "Marketer", "marketer", "marketer", PULL_REQUEST),
)
EXPERTS: Mapping[str, Expert] = MappingProxyType({e.key: e for e in EXPERT_RECORDS})
EXPERT_ARCHITECT = "architect"


def hands_off(expert: str, deliverable: str) -> tuple[str, ...]:
    """What a stage hands the stages that depend on it: only Analyst × Document (`prd`) and
    Architect × Document (`architecture`) hand off a structured document (D12)."""
    record = EXPERTS.get(expert)
    if record is None or record.hands_off is None or deliverable != DOCUMENT:
        return ()
    return (record.hands_off,)


# ── Seniority (model tier) ───────────────────────────────────────────────────────────────────────────

INHERIT = "inherit"
JUNIOR, MID, SENIOR = "junior", "mid", "senior"
SENIORITY_TIERS: Mapping[str, str] = MappingProxyType({JUNIOR: "low", MID: "standard", SENIOR: "high"})
SENIORITY_LABELS: Mapping[str, str] = MappingProxyType({
    JUNIOR: "Junior", MID: "Mid", SENIOR: "Senior", INHERIT: "Playbook default",
})
STAGE_SENIORITIES: tuple[str, ...] = (INHERIT, *SENIORITY_TIERS)

# ── The stage's remaining forms ──────────────────────────────────────────────────────────────────────

ENTRY_NEW_TASK, ENTRY_NEW_CHAT = "new_task", "new_chat"
ENTRY_LABELS: Mapping[str, str] = MappingProxyType({
    ENTRY_NEW_TASK: "Starts from New Task",
    ENTRY_NEW_CHAT: "Starts from New Chat",
})

EXPERT_PINNED, EXPERT_CHOSEN = "pinned", "chosen_at_start"
EXPERT_KIND_LABELS: Mapping[str, str] = MappingProxyType({
    EXPERT_PINNED: "Always this expert",
    EXPERT_CHOSEN: "User selects",
})

CREATED_AT_START, CREATED_BY = "at_start", "by"
CREATED_LABELS: Mapping[str, str] = MappingProxyType({
    CREATED_AT_START: "Created when the playbook starts",
    CREATED_BY: "Created by an earlier Plan stage",
})

RUNS_ONCE, RUNS_PER_ITEM = "once", "per_planned_item"
RUNS_LABELS: Mapping[str, str] = MappingProxyType({
    RUNS_ONCE: "Runs once",
    RUNS_PER_ITEM: "Runs once for each planned item",
})

DEPENDS_NONE, DEPENDS_ALL_EARLIER, DEPENDS_STAGES = "none", "all_earlier", "stages"
DEPENDS_LABELS: Mapping[str, str] = MappingProxyType({
    DEPENDS_NONE: "Waits for nothing",
    DEPENDS_ALL_EARLIER: "Waits for every earlier stage",
    DEPENDS_STAGES: "Waits for the stages chosen",
})

INTERVIEWS_FOLLOW, INTERVIEWS_NEVER = "follows_full_auto", "never"
INTERVIEWS_LABELS: Mapping[str, str] = MappingProxyType({
    INTERVIEWS_FOLLOW: "Asks when Full Auto is off",
    INTERVIEWS_NEVER: "Never asks",
})

FULL_AUTO_LABELS: Mapping[str, str] = MappingProxyType({"on": "Full Auto on", "off": "Full Auto off"})


def labels() -> dict[str, dict[str, str]]:
    """Every label the console renders, keyed by the field it labels — the `labels` of `/vocabulary`."""
    return {
        "entry": dict(ENTRY_LABELS),
        "full_auto": dict(FULL_AUTO_LABELS),
        "expert_kind": dict(EXPERT_KIND_LABELS),
        "seniority": dict(SENIORITY_LABELS),
        "created": dict(CREATED_LABELS),
        "runs": dict(RUNS_LABELS),
        "depends_on": dict(DEPENDS_LABELS),
        "interviews": dict(INTERVIEWS_LABELS),
        "mode": dict(MODE_LABELS),
        "ends_when": dict(ENDS_WHEN_LABELS),
        "may_file": dict(FILING_LABELS),
        "hand_off": dict(HAND_OFF_LABELS),
    }
