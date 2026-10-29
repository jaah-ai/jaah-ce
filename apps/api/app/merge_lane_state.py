"""What every edition reads of the merge lane: whether a project is lane-active, which tasks are lane runs, which
done tasks still wait on a lane row, and the one hook a task leaving `active` calls.

The lane's engine — its queue moves, scheduler, verbs, console and alerts — is `app/merge_lane.py`, which only an
edition with the edition map's `merge_lane` row ships; it re-exports these names. Here, in an edition without the
row, no project is lane-active (`lane_states`), no lane run is ever filed, and `merge_lane_rows` stays empty, so
every read below answers "none".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app import edition_features, plan_terms, secret_redact
from app.models.merge_lane import MergeLaneRow
from app.models.orgs import Org
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.task_lifecycle import ACTIVE

#: The persona every lane task carries (`persona_registry`'s merge-lane persona).
LANE_PERSONA = "merge_lane"
#: The bounds of a project's `merge_lane_batch_max` (`merge_lane.batch_max`).
LANE_BATCH_MAX_BOUNDS = (1, 50)
#: The forges whose `pr merge` can pin a head SHA atomically (L3; Bitbucket cannot).
LANE_FORGES = ("github", "gitlab", "azure_devops")

QUEUED, TESTING, MERGED, FAILED, SUPERSEDED, CLOSED = (
    "queued", "testing", "merged", "failed", "superseded", "closed",
)
#: The states holding the one-open-row-per-(project, PR) index (`uq_merge_lane_open`).
OPEN_STATES = (QUEUED, TESTING)

#: `merge_lane_rows.note`: the longest line kept.
_NOTE_MAX = 300


def is_lane_persona(persona_name: Optional[str]) -> bool:
    """Whether a task of this persona is a merge-lane run."""
    return persona_name == LANE_PERSONA


def _note_line(text: str) -> str:
    """Text as a `note`-sized line: redacted, whitespace collapsed, head-capped at `_NOTE_MAX` with an ellipsis
    where the cut falls."""
    line = " ".join(secret_redact.redact(str(text))[0].split())
    return line if len(line) <= _NOTE_MAX else line[:_NOTE_MAX - 1] + "…"


#: How the merge-lane toolbox opens its `task.fail` reason (`merge lane stopped: FenceError: …`): redundant on a
#: run row, as the exception's class name is to an operator.
_RUN_FAILURE_LEAD = re.compile(r"^merge lane stopped: (?:[A-Z]\w*(?:Error|Exception): )?")


def redacted_reason(reason: str) -> str:
    """A lane run's whole `task.fail` reason, secret-shaped values replaced (`app/secret_redact.py`)."""
    return secret_redact.redact(str(reason))[0]


def run_failure_line(reason: str) -> str:
    """A runner's `task.fail` reason as its termination note keeps it (and a lane run's row shows it): a
    `_note_line` without the lane's own lead (`merge lane stopped: FenceError: the gate's sandbox would not
    start: …` -> `the gate's sandbox would not start: …`); any other reason has no such lead."""
    return _RUN_FAILURE_LEAD.sub("", _note_line(reason))


# ── Eligibility (L3) ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaneState:
    """Whether a project is lane-active, and why not."""

    active: bool
    reason: Optional[str]
    offered: bool = False


def _ineligible_reason(project: Project) -> Optional[str]:
    """The first L3 condition this project fails, as one line; None when it passes them all."""
    if not project.push_enabled:
        return "Push is off for this project, so the lane cannot merge."
    if not project.has_repo or not (project.repo_url or "").strip():
        return "This project has no repository."
    if project.forge not in LANE_FORGES:
        if project.forge == "bitbucket":
            return "Bitbucket cannot merge a pinned commit, so the lane is not available."
        return "The lane needs a GitHub, GitLab or Azure DevOps repository."
    return None


def lane_states(session: Session, projects: Iterable[Project]) -> dict[int, LaneState]:
    """`lane_state` for many projects in a fixed number of queries (the project list's projection).

    The org flag is read once per org through `plan_terms.merge_lane_enabled`, never off the columns. An edition
    without the lane (it ships no sandboxed gate to run PR code under) refuses it whatever the flag says, so no
    runner settles on a queue nobody drains."""
    projects = list(projects)
    if not edition_features.enabled("merge_lane"):
        # Lazy: `ce_caps` imports `org_limits`, which reaches this module through `budgets`.
        from app.ce_caps import NOT_IN_CE

        return {project.id: LaneState(active=False, reason=NOT_IN_CE, offered=False) for project in projects}
    flags: dict[Any, bool] = {}
    out: dict[int, LaneState] = {}
    for project in projects:
        if project.org_id not in flags:
            org = session.get(Org, project.org_id) if project.org_id is not None else None
            flags[project.org_id] = bool(org is not None and plan_terms.merge_lane_enabled(session, org))
        if not flags[project.org_id]:
            out[project.id] = LaneState(active=False, reason=None, offered=False)
            continue
        reason = _ineligible_reason(project)
        out[project.id] = LaneState(active=reason is None, reason=reason, offered=True)
    return out


def lane_state(session: Session, project: Project) -> LaneState:
    """Is `project` lane-active (L3)? Every reader — runner verb, console, lease filter, CI — asks here."""
    return lane_states(session, [project])[project.id]


def gating_node_ids(session: Session) -> set[str]:
    """Node uuid strings where an `active` lane run with a `testing` batch and a full gate holds a slot;
    placement pauses new runner dispatch there (`routes/fleet.py::fleet_allocation`)."""
    gating = select(MergeLaneRow.id).where(
        MergeLaneRow.lane_task_id == Task.id, MergeLaneRow.state == TESTING).exists()
    return {str(node) for node in session.scalars(
        select(Worktree.node_id).distinct()
        .join(Task, Task.id == Worktree.task_id)
        .join(Project, Project.id == Task.project_id)
        .where(
            # `slot_state.TASK` spelled out: keeps `slot_state`'s import graph out.
            Worktree.state == "task",
            Task.persona == LANE_PERSONA,
            Task.state == ACTIVE,
            func.length(func.btrim(Project.gate_full_cmd, ' \t\r\n')) > 0,
            gating,
        )
    )}


def lane_task_left_active(session: Session, task_id: int) -> None:
    """The L10 hook, called by every writer that moves a task out of `active` (`merge_lane.lane_task_left_active`):
    nothing to do in an edition without the lane, which never files a lane run."""
    if not edition_features.enabled("merge_lane"):
        return
    from app import merge_lane

    merge_lane.lane_task_left_active(session, task_id)


# ── Done but not delivered ────────────────────────────────────────────────────────────────────────────


def awaiting_lane_merge_clause(task_id_column: Any) -> Any:
    """SQL predicate: the task's latest lane row is still open (`queued`/`testing`/`failed`). A runner settles
    `done` once its PR is queued, so such a task is done-but-not-delivered: no dependency gate or epic rollup may
    treat it as `done` yet. `merged`, `closed` and `superseded` rows, and a task with no row, do not hold it."""
    # "An open row with no newer row", not `id == (latest-row subquery)`: a scalar subquery two levels down does not
    # correlate to the outer task, and read the newest row of ANY task.
    held, newer = aliased(MergeLaneRow), aliased(MergeLaneRow)
    superseded_by = (select(newer.id).where(
        newer.task_id == held.task_id,
        (newer.created_at > held.created_at) | ((newer.created_at == held.created_at) & (newer.id > held.id)),
    ).exists())
    return (select(held.id)
            .where(held.task_id == task_id_column, held.state.in_((QUEUED, TESTING, FAILED)), ~superseded_by)
            .exists())


def awaiting_lane_merge(session: Session, task_ids: Iterable[int]) -> set[int]:
    """The subset of `task_ids` that is done-but-awaiting-the-lane (`awaiting_lane_merge_clause`), in one query.
    Every reader that asks whether a `done` task is delivered goes through this or the clause."""
    ids = list(task_ids)
    if not ids:
        return set()
    return set(session.scalars(select(Task.id).where(Task.id.in_(ids), awaiting_lane_merge_clause(Task.id))).all())


#: The `task_note` saying why the lane sent a runner back to its PR, above the reopen's `run` row (or alone, for a
#: live runner it only messaged), or that it resolved the PR's additive conflict itself and sent nobody back
#: (`REDISPATCH_RESOLVED`); `data.cause` is a `REDISPATCH_LABELS` key, `data.pr` the PR number.
REDISPATCH_NOTE = "lane_redispatch"
REDISPATCH_CONFLICT, REDISPATCH_FAILED, REDISPATCH_RESOLVED = "conflict", "failed", "resolved"
REDISPATCH_LABELS = {
    REDISPATCH_CONFLICT: "PR #{pr} conflicts — rebasing",
    REDISPATCH_FAILED: "PR #{pr} failed the lane gate — fixing",
    REDISPATCH_RESOLVED: "PR #{pr} conflicts — lane resolved",
}


def redispatch_label(data: Mapping[str, Any]) -> str:
    """The timeline label of a `REDISPATCH_NOTE` row. An unknown cause reads as the failure: never a raise on an
    append-only row."""
    label = REDISPATCH_LABELS.get(str(data.get("cause")), REDISPATCH_LABELS[REDISPATCH_FAILED])
    return label.format(pr=data.get("pr"))
