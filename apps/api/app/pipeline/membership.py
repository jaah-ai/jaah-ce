"""Where an item is on a project's pipeline: `locate`, the ONE place a stage is computed (architecture §2.4). Stage
counts (`view`) and stage lists (`items`) both read it, so they cannot drift apart.

**Items** are the project's top-level tasks the caller sees on the board (`items_of`), and the merged changes no such
task owns (`app/releases/waiting.py::merged_changes`: a bot's or a hand-opened PR; `c:<sha>`). A change merged by a
task's subtask belongs to the top-level task (its root).

**Place** = the last stage whose condition holds:

- a `none` stage: a pass into it exists (`pipeline_passes`, by task or by every one of the item's shas), or it is the
  first stage, or a later stage's code condition holds;
- the `work` stage: the task has started (its state is not `draft`);
- the first `branch` stage (the integration stage): a merge sha is recorded;
- a later `branch` stage: every sha of the item arrived there or later (`waiting.arrived_shas`'s two sources).

A code fact implies every earlier stage, whatever was added or skipped. An item meeting no stage's condition (a
pipeline that starts at a work or branch stage, an unstarted task) is **not started**. **Unfinished work stays before
the integration stage**: a task not `done`, or one with an open or failed merge-lane row, ignores its merge shas and
any stage from the integration stage on (a hidden task is exempt: automatic work often ends paused or failed after
its merge, and its code is where its shas say), so a running task that merged an earlier PR sits in Building (its group says
working, asking, merging…; a merging item is listed under its merge step), never in Dev. Otherwise task state decides only the in-stage `group`. Outside the stages: **archived** (`archived_at` set; restore clears it and `locate`
puts the task back where its facts say) and **done** (D14: `done` with no merge sha and no open merge-lane row).

Every fact is a set query per project (`Facts.load`), never a read per item.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.merge_lane_state import FAILED as LANE_FAILED, OPEN_STATES as LANE_OPEN
from app.models.merge_lane import MergeLaneRow
from app.models.projects import Project
from app.models.releases import PipelineApproval, PipelinePass, Release, ReleaseChange, ReleaseContained, ReleaseStage
from app.models.tasks import Task
from app.releases import lifecycle, stages as stages_mod, waiting as waiting_mod
from app.releases.waiting import Change
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, DONE, DRAFT, PAUSED, READY, SYSTEM_FAILED

NOT_STARTED, DONE_BUCKET, ARCHIVED = "not_started", "done", "archived"
BUCKETS = (NOT_STARTED, DONE_BUCKET, ARCHIVED)

#: In-stage groups: Needs your answer, Failed, Working, Merging, Waiting (a step's press or approvals), Stopped, New
#: (an idea not pressed yet), Done (merged or released). A stage lists them as ONE flat list: questions first, then
#: failures, then stops, then the rest, newest first (`_GROUP_RANK`). A failed merge is no group of its own: the lane
#: sends its runner back to fix the PR, a retry, so the task reads Working (`_group`).
ASKING, FAILED, WORKING, MERGING, WAITING, STOPPED, NEW, DONE_GROUP = (
    "asking", "failed", "working", "merging", "waiting", "stopped", "new", "done")
GROUPS = (ASKING, FAILED, WORKING, MERGING, WAITING, STOPPED, NEW, DONE_GROUP)
_GROUP_RANK = {ASKING: 0, FAILED: 1, STOPPED: 2}
_REST_RANK = 3
#: Groups a stage neither lists nor counts: a merging item is listed under its merge step's count (`?group=merging`).
ON_STEP_ONLY = frozenset({MERGING})

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def task_key(task_id: int) -> str:
    return f"t:{task_id}"


def change_key(sha: str) -> str:
    return f"c:{sha}"


@dataclass(frozen=True)
class Item:
    """One thing on the pipeline: a top-level task (`task_id`) or a taskless merged change (`change`)."""

    key: str
    task_id: Optional[int] = None
    state: Optional[str] = None
    archived: bool = False
    updated_at: Optional[datetime] = None
    title: str = ""
    feature_id: Optional[UUID] = None
    stop_reason: Optional[str] = None
    change: Optional[Change] = None
    #: `Task.hidden`: automatic work, placed by its code facts whatever its state (`locate`).
    hidden: bool = False
    #: A task off the board (hidden: automatic work) listed only because it merged a change (`board.py`).
    automatic: bool = False


@dataclass(frozen=True)
class StagePlace:
    """Where `locate` puts an item: a stage (`stage_id`, `index`) or a bucket (`bucket`), its group and failure."""

    stage_id: Optional[UUID]
    index: int
    bucket: Optional[str]
    group: str
    failure: Optional[str] = None
    lane_state: Optional[str] = None
    held: bool = False

    def sort_key(self, item: Item, asking: bool = False) -> tuple[Any, ...]:
        """Screen order: stage, attention rank (`_GROUP_RANK`; `asking`: the list marks the row "?", an epic with an
        asking child included, so it ranks with the questions), newest first, then the key (a total order for paging)."""
        stamp = item.updated_at or (item.change.merged_at if item.change is not None else None) or _EPOCH
        rank = _GROUP_RANK[ASKING] if asking else _GROUP_RANK.get(self.group, _REST_RANK)
        return (self.index, rank, -stamp.timestamp(), item.key)


@dataclass
class Facts:
    """Every fact `locate` reads for one project, each one set query."""

    changes: list[Change]
    roots: dict[int, int]
    shas_by_root: dict[int, list[str]]
    arrived: dict[UUID, set[str]]
    passes_task: dict[UUID, set[int]]
    passes_sha: dict[UUID, set[str]]
    lane: dict[int, MergeLaneRow]
    pressed: set[int]

    @classmethod
    def load(cls, session: Session, project: Project, stages: Sequence[ReleaseStage],
             changes: Optional[list[Change]] = None) -> "Facts":
        changes = waiting_mod.merged_changes(session, project) if changes is None else changes
        lane_rows = list(session.scalars(
            select(MergeLaneRow).where(
                MergeLaneRow.project_id == project.id, MergeLaneRow.state.in_((*LANE_OPEN, LANE_FAILED)))
            .order_by(MergeLaneRow.created_at)
        ))
        roots = roots_of(session, {c.task_id for c in changes if c.task_id is not None}
                         | {r.task_id for r in lane_rows})
        shas_by_root: dict[int, list[str]] = {}
        for change in changes:
            if change.task_id is not None:
                shas_by_root.setdefault(roots.get(change.task_id, change.task_id), []).append(change.merge_sha)
        lane: dict[int, MergeLaneRow] = {}
        for row in lane_rows:  # oldest first: a later row of the same task wins
            lane[roots.get(row.task_id, row.task_id)] = row
        stage_ids = [s.id for s in stages]
        arrived: dict[UUID, set[str]] = {}
        if stage_ids:
            for stage_id, sha in session.execute(
                select(Release.to_stage_id, ReleaseChange.merge_sha)
                .join(ReleaseChange, ReleaseChange.release_id == Release.id)
                .where(Release.project_id == project.id, Release.to_stage_id.in_(stage_ids),
                       Release.state.in_(tuple(lifecycle.ARRIVED)))
            ):
                arrived.setdefault(stage_id, set()).add(sha)
            for stage_id, sha in session.execute(
                select(ReleaseContained.stage_id, ReleaseContained.merge_sha)
                .where(ReleaseContained.project_id == project.id, ReleaseContained.stage_id.in_(stage_ids))
            ):
                arrived.setdefault(stage_id, set()).add(sha)
        passes_task: dict[UUID, set[int]] = {}
        passes_sha: dict[UUID, set[str]] = {}
        for stage_id, task_id, sha in session.execute(
            select(PipelinePass.stage_id, PipelinePass.task_id, PipelinePass.merge_sha)
            .where(PipelinePass.project_id == project.id)
        ):
            if task_id is not None:
                passes_task.setdefault(stage_id, set()).add(task_id)
            if sha is not None:
                passes_sha.setdefault(stage_id, set()).add(sha)
        pressed = set(session.scalars(
            select(PipelineApproval.task_id).where(
                PipelineApproval.project_id == project.id, PipelineApproval.task_id.is_not(None))
        ))
        return cls(changes=changes, roots=roots, shas_by_root=shas_by_root, arrived=arrived,
                   passes_task=passes_task, passes_sha=passes_sha, lane=lane, pressed=pressed)


def roots_of(session: Session, task_ids: Iterable[int]) -> dict[int, int]:
    """`{task id: its top-level ancestor's id}`, one read per level of nesting."""
    out: dict[int, int] = {}
    pending = {i: i for i in task_ids if i is not None}
    for _ in range(8):  # epics nest one level; the bound only stops a cycle
        if not pending:
            break
        parents = dict(session.execute(
            select(Task.id, Task.parent_task_id).where(Task.id.in_(sorted(set(pending.values()))))).all())
        nxt: dict[int, int] = {}
        for origin, current in pending.items():
            parent = parents.get(current)
            if parent is None:
                out[origin] = current
            else:
                nxt[origin] = parent
        pending = nxt
    out.update(pending)
    return out


# ── Items ─────────────────────────────────────────────────────────────────────────────────────────────

_TASK_COLUMNS = (Task.id, Task.state, Task.archived_at, Task.updated_at, Task.title, Task.feature_id,
                 Task.stop_reason, Task.hidden)


def task_items(session: Session, stmt: Any) -> list[Item]:
    """The task items a board select yields (`routes/tasks/listing.py::board_filtered_select`)."""
    return [
        Item(key=task_key(row.id), task_id=row.id, state=row.state, archived=row.archived_at is not None,
             updated_at=row.updated_at, title=row.title or "", feature_id=row.feature_id, stop_reason=row.stop_reason,
             hidden=bool(row.hidden))
        for row in session.execute(stmt.with_only_columns(*_TASK_COLUMNS))
    ]


def change_items(facts: Facts, owned: set[int]) -> list[Item]:
    """The merged changes no item task owns, one item per sha (the root of the change's task is not in `owned`)."""
    out: list[Item] = []
    for change in facts.changes:
        root = facts.roots.get(change.task_id, change.task_id) if change.task_id is not None else None
        if root is not None and root in owned:
            continue
        out.append(Item(key=change_key(change.merge_sha), title=change.title or "", change=change,
                        updated_at=change.merged_at))
    return out


# ── locate ────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Layout:
    """The stage indexes `locate` reads: the work stage, the integration stage, and each branch stage's index."""

    stages: tuple[ReleaseStage, ...]
    work: Optional[int]
    integration: Optional[int]
    branches: tuple[int, ...]

    @classmethod
    def of(cls, stages: Sequence[ReleaseStage]) -> "Layout":
        work = next((i for i, s in enumerate(stages) if s.level == stages_mod.WORK), None)
        branches = tuple(i for i, s in enumerate(stages) if s.level == stages_mod.BRANCH)
        return cls(tuple(stages), work, branches[0] if branches else None, branches)


def _sha_index(layout: Layout, facts: Facts, sha: str) -> int:
    """The furthest branch stage `sha` is on: arrived there (a later one implies the earlier), else the integration."""
    assert layout.integration is not None
    for index in reversed(layout.branches[1:]):
        if sha in facts.arrived.get(layout.stages[index].id, ()):
            return index
    return layout.integration


def _code_index(layout: Layout, facts: Facts, item: Item, shas: Sequence[str]) -> int:
    """The furthest stage a code fact puts the item on: -1 when none holds."""
    index = -1
    if item.task_id is not None and layout.work is not None and item.state not in (None, DRAFT):
        index = layout.work
    if shas and layout.integration is not None:
        # Every sha must be there: a task is on a stage once all it merged arrived.
        index = max(index, min(_sha_index(layout, facts, sha) for sha in shas))
    return index


def _passed(facts: Facts, stage_id: UUID, item: Item, shas: Sequence[str]) -> bool:
    if item.task_id is not None and item.task_id in facts.passes_task.get(stage_id, ()):
        return True
    passed = facts.passes_sha.get(stage_id, set())
    return bool(shas) and all(sha in passed for sha in shas)


def _group(item: Item, facts: Facts, shas: Sequence[str]) -> tuple[str, Optional[str], Optional[MergeLaneRow]]:
    """The item's in-stage group, its failure text, and its open (or failed) merge-lane row."""
    if item.task_id is None:
        return DONE_GROUP, None, None
    row = facts.lane.get(item.task_id)
    state = item.state
    if state == AWAITING_RESPONSE:
        return ASKING, None, row
    if state == SYSTEM_FAILED:
        return FAILED, item.stop_reason or None, row
    if row is not None and row.state == LANE_FAILED and state not in (PAUSED, DRAFT):
        return WORKING, None, row  # a retry, never a failure: the lane sends its runner back to fix the PR
    if row is not None and row.state in LANE_OPEN:
        return (WAITING if row.held else MERGING), None, row
    if state in (READY, ACTIVE):
        return WORKING, None, row
    if state == PAUSED:
        return STOPPED, None, row
    if state == DRAFT:
        return (WAITING if item.task_id in facts.pressed else NEW), None, row
    if state == DONE:
        return DONE_GROUP, None, row
    return WORKING, None, row


def locate(project: Project, stages: Sequence[ReleaseStage], items: Iterable[Item],
           facts: Facts) -> dict[str, StagePlace]:
    """`{item key: StagePlace}` for every item (module docstring). `facts` is `Facts.load` for the same project and
    stages; the project is named for the contract (the item set is the project's)."""
    del project  # every fact is already the project's
    layout = Layout.of(stages)
    out: dict[str, StagePlace] = {}
    for item in items:
        shas = (facts.shas_by_root.get(item.task_id, []) if item.task_id is not None
                else [item.change.merge_sha] if item.change is not None else [])
        group, failure, row = _group(item, facts, shas)
        lane_state = row.state if row is not None else None
        held = bool(row is not None and row.held and row.state in LANE_OPEN)
        if item.archived:
            out[item.key] = StagePlace(None, -1, ARCHIVED, group, failure, lane_state, held)
            continue
        if item.task_id is not None and item.state == DONE and not shas and row is None:
            out[item.key] = StagePlace(None, -1, DONE_BUCKET, DONE_GROUP, None, None, False)
            continue
        unfinished = item.task_id is not None and not item.hidden and (item.state != DONE or row is not None)
        index = _code_index(layout, facts, item, [] if unfinished else shas)
        last = layout.integration if unfinished and layout.integration is not None else len(layout.stages)
        for position, stage in enumerate(layout.stages[:last]):
            if stage.level != stages_mod.NONE or position <= index:
                continue
            if position == 0 or _passed(facts, stage.id, item, shas):
                index = position
        if index < 0:
            out[item.key] = StagePlace(None, -1, NOT_STARTED, group, failure, lane_state, held)
        else:
            out[item.key] = StagePlace(layout.stages[index].id, index, None, group, failure, lane_state, held)
    return out
