"""The four built-in playbooks — Task, Sketch, Chat, Epic — the single home of the seed.

Each one describes what the console does TODAY (`app/routes/tasks/policy.py`: `TOP_LEVEL_DEPTHS`,
`TOP_LEVEL_TASK_PERSONAS`, the epic's analysis children and `INTERVIEWING_CHILD_PERSONAS`), written in the
`PlaybookIn` wire shape `app/routes/playbooks.py` accepts. Revision 0310 inserts exactly these rows into
`built_in_playbooks` — one global copy each (D1), read-only to every org, duplicable into a tenant copy.

EDITING THIS MODULE IS A SCHEMA CHANGE. The applied revision already seeded the old values, so a change
here must ship a revision that re-seeds; `tests/test_playbook_seed.py` compares the migrated rows with
this module and fails until one does. That revision must UPSERT by id: a fresh database runs 0310 against
the edited module and already holds the new values. Renaming or deleting this module breaks 0310 on a
fresh database — freeze its values into the revision first.

STDLIB-ONLY AND LITERAL: a migration imports it (`tests/test_frozen_migration_imports.py` polices what a
revision may import), so it names no app module — not even `app/playbook_vocabulary.py`, whose values it
spells as strings. `tests/test_playbook_seed.py` validates every built-in against that vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class BuiltIn:
    #: Fixed UUIDv7 literal, so a migration and a test can name the row.
    id: str
    name: str
    description: str
    entry: str
    full_auto_default: bool
    default_seniority: str
    stages: tuple[dict[str, Any], ...]


def _stage(
    key: str,
    label: str,
    expert: dict[str, Any],
    deliverable: Optional[str],
    *,
    seniority: str = "inherit",
    created: Optional[dict[str, Any]] = None,
    runs: str = "once",
    depends_on: Optional[dict[str, Any]] = None,
    interviews: str = "never",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "expert": expert,
        "deliverable": deliverable,
        "seniority": seniority,
        "created": created or {"kind": "at_start"},
        "runs": runs,
        "depends_on": depends_on or {"kind": "none"},
        "interviews": interviews,
        "extra_instructions": "",
        "may_file": None,
    }


def _pinned(expert: str) -> dict[str, Any]:
    return {"kind": "pinned", "expert": expert}


TASK = BuiltIn(
    id="019a0000-0000-7000-8000-00000000b001",
    name="Task",
    description="One expert does one piece of work. Engineer by default; pick another when you start it.",
    entry="new_task",
    full_auto_default=True,
    default_seniority="senior",
    stages=(
        _stage(
            "work", "Work",
            {
                "kind": "chosen_at_start",
                "default": "engineer",
                "allowed": ["engineer", "analyst", "architect", "researcher", "test_optimizer", "translator",
                            "marketer"],
            },
            None,
            interviews="follows_full_auto",
        ),
    ),
)

SKETCH = BuiltIn(
    id="019a0000-0000-7000-8000-00000000b002",
    name="Sketch",
    description="A UI prototyper draws a quick mockup.",
    entry="new_task",
    full_auto_default=True,
    default_seniority="junior",
    stages=(_stage("sketch", "Sketch", _pinned("ui_prototyper"), "mockup"),),
)

CHAT = BuiltIn(
    id="019a0000-0000-7000-8000-00000000b003",
    name="Chat",
    description="A conversation with the Advisor, who can file draft tasks for you.",
    entry="new_chat",
    full_auto_default=False,
    default_seniority="senior",
    stages=(
        _stage("chat", "Chat", _pinned("advisor"), "conversation", interviews="follows_full_auto"),
    ),
)

EPIC = BuiltIn(
    id="019a0000-0000-7000-8000-00000000b004",
    name="Epic",
    description=(
        "Analyze, design and plan, then build each planned item as its own pull request and review the "
        "result."
    ),
    entry="new_task",
    full_auto_default=True,
    default_seniority="senior",
    stages=(
        _stage("analyze", "Analyze", _pinned("analyst"), "document", interviews="follows_full_auto"),
        _stage(
            "design", "Design", _pinned("architect"), "document",
            depends_on={"kind": "stages", "keys": ["analyze"]}, interviews="follows_full_auto",
        ),
        _stage("plan", "Plan", _pinned("planner"), "plan", depends_on={"kind": "all_earlier"}),
        _stage(
            "implement", "Implement", _pinned("engineer"), "pull_request",
            created={"kind": "by", "stage": "plan"}, runs="per_planned_item",
        ),
        _stage(
            "finish", "Finish", _pinned("reviewer"), "review",
            created={"kind": "by", "stage": "plan"}, depends_on={"kind": "stages", "keys": ["implement"]},
        ),
    ),
)

#: Seed order — the order `GET /api/playbooks` lists them in.
BUILT_INS: tuple[BuiltIn, ...] = (TASK, SKETCH, CHAT, EPIC)

