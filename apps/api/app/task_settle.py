"""The manifest predicate: what a persona still owes at the moment it asks to settle.

What each persona must deliver lives in `app/persona_registry.py` (pure data, imported by
`app/models/`, so it cannot hold ORM code); this module checks whether it did.

The bar is shape only: a document kind needs a visible row, non-empty, with its kind's content type —
no judgement of content. Always read through `task_artifacts.current_artifact` (the watermark fails
open). Reference kinds are the call's own parameters, checked present only; `merged_pr` is
self-attested. No manifest row names a reference kind today, but the predicate answers for
them so a future row cannot read as silently satisfied.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.orm import Session

from app import playbook_engine, slot_allocation
from app.models.tasks import Task
from app.persona_registry import PERSONA_MANIFEST, PERSONA_TERMINALS
from app.task_artifacts import current_artifact
from app.task_manifest import (
    DOCUMENT_KINDS,
    KIND_MEDIA,
    REQUIRED_ADVISORY,
    REQUIRED_HARD,
)

#: The reference parameters each terminal type carries — required together, forbidden on every other
#: terminal type. CP half of the settle tool's `oneOf`
#: (`apps/jaahd/agent_vocabulary.py::_TOOL_SETTLE.one_of`); the two cannot share a literal, so each
#: side is pinned against `TERMINAL_TYPES` and drift can only make one door stricter. `outcome` carries
#: nothing: outcome personas never declare `merged_pr`.
TERMINAL_REFERENCES: dict[str, tuple[str, ...]] = {
    "analysis": (),
    "coders": (),
    "merged_pr": ("pr_url",),
    "no_pr": ("no_pr_reason", "no_pr_evidence"),
    "finish": (),
    "outcome": (),
}

#: Every reference parameter any terminal type carries — the set a branch that does not name one FORBIDS.
#: Derived, so adding a reference to a terminal type above enrolls it in the forbid half automatically.
REFERENCE_PARAMS: tuple[str, ...] = tuple(
    dict.fromkeys(name for names in TERMINAL_REFERENCES.values() for name in names)
)


def _document_satisfied(session: Session, task: Task, kind: str) -> bool:
    """Shape-only verification of one DOCUMENT kind: a visible row of that kind, non-empty, stored under
    the content type its kind is served as (checked, since bytes are served into a sandboxed iframe).

    Format-pinned kinds only: a free-form kind would `KeyError` on `KIND_MEDIA`, and
    a manifest-shape test asserts no manifest row names one."""
    row = current_artifact(session, task, kind)
    return (
        row is not None
        and (row.size_bytes or 0) > 0
        and row.content_type == KIND_MEDIA[kind][1]
    )


def _kind_satisfied(
    session: Session, task: Task, kind: str, payload: Mapping[str, Any]
) -> bool:
    """Whether ONE manifest row is satisfied. Refuses an unknown kind LOUDLY rather than reading it as
    unsatisfied, which would refuse every settle of that persona forever."""
    if kind in DOCUMENT_KINDS:
        return _document_satisfied(session, task, kind)
    if kind in TERMINAL_REFERENCES:
        names = TERMINAL_REFERENCES[kind]
        return bool(names) and all(str(payload.get(name) or "").strip() for name in names)
    raise ValueError(
        f"task_settle: {kind!r} is neither a document artifact nor a reference terminal — a derived kind "
        f"(`app/task_manifest.py::DERIVED_KINDS`) is CP-computed and is never a manifest row"
    )


#: What a folder-run build task's `outcome` settle stamps on `tasks.outcome_no_pr_reason`, so the rollup floor
#: (`routes/tasks/epics.py::_child_rollup_ready`) and Finish read it as delivered with no edit. CP-written only: never
#: in the runner-claimable `NO_PR_CLOSE_REASONS`.
NO_REPO_OUTCOME_REASON = "no_repo"

#: The one ending a folder-run build task may declare: its report is the deliverable.
FOLDER_BUILD_TERMINALS: tuple[str, ...] = ("outcome",)

#: What a plan's `analysis` settle — nothing to build, the plan document is the deliverable — stamps on
#: `tasks.outcome_no_pr_reason`, so the epic rollup (`routes/tasks/epics.py::_epic_append_pending`) and the
#: final-report publisher read it as a settled decomposition of zero children, never an append still
#: outstanding. CP-written only, like `NO_REPO_OUTCOME_REASON`: never in `NO_PR_CLOSE_REASONS`.
NO_BUILD_OUTCOME_REASON = "no_build"


def _declared_terminals(task: Task) -> tuple[str, ...]:
    """The endings `task` declares as its persona, or — a playbook stage row — its deliverable's for its expert
    (`playbook_engine.settle_terms`), which the stage's prompt was told."""
    if task.playbook_stage:
        return tuple(playbook_engine.settle_terms(task.playbook_stage).terminals)
    return tuple(PERSONA_TERMINALS.get(task.persona, ()))


def is_folder_build(session: Session, task: Task) -> bool:
    """Whether `task` is a build task (its declared endings include `merged_pr`) running in a folder slot, which
    has no repository to merge into — so it settles on `outcome` with its report HARD. Analysis, plan,
    finish and outcome personas declare no `merged_pr` and keep their terms."""
    return "merged_pr" in _declared_terminals(task) and not slot_allocation.task_has_repo(session, task)


def terminals_of(session: Session, task: Task) -> tuple[str, ...]:
    """The endings `task` may declare at settle: its declared ones, or `FOLDER_BUILD_TERMINALS` for a folder
    build task."""
    return FOLDER_BUILD_TERMINALS if is_folder_build(session, task) else _declared_terminals(task)


def manifest_of(session: Session, task: Task) -> tuple[tuple[str, str], ...]:
    """What `task` owes at settle: its persona's manifest, or — a playbook stage row — its deliverable's for
    its expert (`playbook_engine.settle_terms`), which the stage's prompt was told. A folder build task owes its
    `report` HARD: with no PR it is the whole deliverable."""
    declared = (
        tuple(playbook_engine.settle_terms(task.playbook_stage).manifest)
        if task.playbook_stage else tuple(PERSONA_MANIFEST.get(task.persona, ()))
    )
    if not is_folder_build(session, task):
        return declared
    others = tuple(row for row in declared if row[0] != "report")
    return (("report", REQUIRED_HARD),) + others


def _missing(
    session: Session, task: Task, payload: Mapping[str, Any], level: str
) -> list[str]:
    """The kinds of `level` this task still owes, IN MANIFEST DECLARATION ORDER — the order the refusal
    text renders them in."""
    return [
        kind
        for kind, required in manifest_of(session, task)
        if required == level and not _kind_satisfied(session, task, kind, payload)
    ]


def missing_hard_kinds(
    task: Task, payload: Mapping[str, Any], session: Session
) -> list[str]:
    """The HARD rows this task has not delivered — the settle's refusal set. EVERY missing kind, never
    just the first, so the runner fixes all gaps in one round."""
    return _missing(session, task, payload, REQUIRED_HARD)


def missing_advisory_kinds(
    task: Task, payload: Mapping[str, Any], session: Session
) -> list[str]:
    """The ADVISORY rows this task has not delivered. RECORDED, never refused: a build persona's
    deliverable is its merged PR, and a missing report never strands it."""
    return _missing(session, task, payload, REQUIRED_ADVISORY)
