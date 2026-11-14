"""The task rail's data — `GET /api/tasks/tree` and `GET /api/tasks/counts` (`routes/tasks/`).

One tree for the caller: Project › Feature (live ones), every node carrying `Counts`; a project's counts are
every row of the project, its features' and the ones filed in the project itself alike. THE COUNTING UNIT is a top-level board row
(`routes/tasks/listing.py::board_row_clauses`), bucketed by `task_sections.section_sql`, so an epic counts once,
where its stage tasks place it. `questions + failed + paused == stopped`, `queued` is the slice of
`in_progress` not yet running (a `ready` row), `suggested` the slice of `draft` that is Jaah's suggestions, and every parent's
counts are the server-side sum of its children, so a closed branch never hides a blocker.

Visibility is the existing rules, read, never restated: a project is present iff the caller holds
`tasks:view` there (the key that shows its tasks), its features by `authz.visible_feature_clause`, and
every count is over `authz.scoped_task_select`. A deleted feature leaves `features`, but its rows stay in the
project's sums (deleting never hides work), and while it still holds a row on the board it is named in
`deleted_features`, so the pipeline's later stages can still group its tasks under its name.

THE PINNED SHAPE: the console's rail, section headers and feature bar bind to these names.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, and_, case, func, literal, null, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app import events, task_sections
from app.authz.catalog import permission_key
from app.authz.principal import Principal
from app.authz.visibility import (
    TASK_SCOPE_ARM_KEY,
    has_project_key,
    scoped_project_select,
    visible_feature_clause,
)
from app.entity_shape import from_row
from app.models.orgs import in_live_org
from app.models.projects import Feature, Project
from app.models.tasks import Task
from app.task_lifecycle import DONE, READY
from app.tenancy import seam
from app.tenancy.labels import OrgRef, org_ref_of, org_refs

TASKS_CREATE_KEY = permission_key("tasks", "create")
TASKS_EDIT_KEY = permission_key("tasks", "edit")
PROJECTS_EDIT_KEY = permission_key("projects", "edit")
PROJECTS_CREATE_KEY = permission_key("projects", "create")

# `count_rows`' placement for an In progress row that is still `ready`: counted in `in_progress` and `queued`.
QUEUED_SECTION = "queued"
# Its placement for a Draft row that is a Jaah suggestion (`task_source.is_suggested`): counted in `draft` and `suggested`.
SUGGESTED_SECTION = "suggested"

# `Counts`' per-kind field for each `STOP_KIND_ORDER` member. A system pause of Jaah-account work is a pause:
# the rail counts it with the others, and its row says which (`StopOut.kind`).
_KIND_FIELD: dict[str, str] = {
    "question": "questions",
    "failed": "failed",
    "paused": "paused",
    "balance": "paused",
    "jaah_unavailable": "paused",
}


class Counts(BaseModel):
    stopped: int = 0
    questions: int = 0
    failed: int = 0
    paused: int = 0
    in_progress: int = 0
    queued: int = 0
    draft: int = 0
    # The slice of `draft` that is Jaah's suggestions (`SUGGESTED_SECTION`).
    suggested: int = 0
    done: int = 0
    total: int = 0

    def add(self, other: "Counts") -> None:
        for name in type(self).model_fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def add_bucket(self, section: str, kind_rank: Optional[int], n: int) -> None:
        """Count `n` rows placed in `section` (and, when Stopped, of the `STOP_KIND_ORDER[kind_rank]` kind).
        `QUEUED_SECTION` is In progress's not-yet-running slice (`count_rows`)."""
        self.total += n
        if section == "stopped":
            self.stopped += n
            kind = task_sections.STOP_KIND_ORDER[kind_rank]  # type: ignore[index]
            field = _KIND_FIELD[kind]
            setattr(self, field, getattr(self, field) + n)
        elif section == "done":
            self.done += n
        elif section in ("draft", SUGGESTED_SECTION):
            self.draft += n
            if section == SUGGESTED_SECTION:
                self.suggested += n
        else:
            self.in_progress += n
            if section == QUEUED_SECTION:
                self.queued += n


def summed(parts: Iterable[Counts]) -> Counts:
    total = Counts()
    for part in parts:
        total.add(part)
    return total


class FeatureNode(BaseModel):
    id: UUID
    name: str
    lead_id: UUID
    # `models.FEATURE_ICONS` or null; every row a task tree draws takes its icon from here.
    icon: Optional[str]
    # Whether the rail draws the feature's completion ring (`models.Feature.show_progress`).
    show_progress: bool
    counts: Counts


class DeletedFeatureNode(BaseModel):
    """A deleted feature that still holds a row on the board: its name and icon for those rows' groups, never a
    rail node or a picker choice."""

    id: UUID
    name: str
    icon: Optional[str]


class ProjectNode(BaseModel):
    id: int
    name: str
    # The project's tenant (`tenancy/labels.py::OrgRef`); names may repeat across orgs, so the rail tells
    # two same-named projects apart by it. Null when the row is bound to no org.
    org: Optional[OrgRef] = None
    can_edit_project: bool
    can_create_tasks: bool
    can_edit_tasks: bool
    features: list[FeatureNode]
    deleted_features: list[DeletedFeatureNode] = []
    counts: Counts


class TaskTree(BaseModel):
    projects: list[ProjectNode]
    counts: Counts
    can_create_project: bool


def count_rows(session: Session, board: Select, *group_by: Any) -> list[tuple[Any, ...]]:
    """`(*group_by, section, kind_rank, n)` over `board` — a `select(Task)` already narrowed to the rows
    to count. One GROUP BY; each row's placed stop rank is computed once in a subquery and its section derived
    from it (`task_sections.section_of_stop_rank_sql`), and a `done` row — the board's bulk — reads no children
    at all: done wins its placement, so its rank is never computed (NULL; `Counts.add_bucket` reads a rank
    only for Stopped). An In progress row still `ready` reads `QUEUED_SECTION`, so `Counts.add_bucket` can
    split it out.

    The rows are a MATERIALIZED CTE: the section and its slices read `kind_rank` several times, and a pulled-up
    subquery would inline its correlated child subplan at every reference (up to ~4 per row)."""
    inner = board.with_only_columns(
        *group_by, Task.state.label("state"), Task.hidden.label("hidden"),
        task_sections.suggested_sql(Task).label("suggested"),
        case((Task.state == DONE, null()), else_=task_sections.task_stop_kind_rank_sql()).label("kind_rank"),
    ).cte("board_rows").prefix_with("MATERIALIZED")
    keys = [inner.c[col.key] for col in group_by]
    section = task_sections.section_of_stop_rank_sql(inner.c.state, inner.c.hidden, inner.c.kind_rank)
    placed = case(
        (and_(section == "in_progress", inner.c.state == READY), literal(QUEUED_SECTION)),
        (and_(section == "draft", inner.c.suggested), literal(SUGGESTED_SECTION)),
        else_=section,
    ).label("section")
    # The correlated child-rank subplan inflates the estimate past `jit_above_cost`, and compiling the plan
    # then costs ~8x the query itself (measured: 375 ms with JIT, 42 ms without, over 29k board rows).
    # Transaction-local, and these routes are read-only.
    session.execute(text("SET LOCAL jit = off"))
    return list(
        session.execute(
            select(*keys, placed, inner.c.kind_rank, func.count())
            .group_by(*keys, placed, inner.c.kind_rank)
        ).all()
    )


def counts_of(session: Session, board: Select) -> Counts:
    """`Counts` for exactly the rows `board` selects (`GET /api/tasks/counts`)."""
    counts = Counts()
    for section, kind_rank, n in count_rows(session, board):
        counts.add_bucket(section, kind_rank, n)
    return counts


#: How long a tree count is reused (`_tree_cells`): the bound on a change neither an event nor the
#: freshness stamp caught.
_TREE_CELLS_TTL_SECONDS = 60.0
#: Answers held at once, least recently used dropped first.
_TREE_CELLS_MAX = 512
#: The console events that can move a count: a task's state, filing or deletion; a feature or project write (a
#: feature's delete re-files its tasks by cascade, which stamps no `updated_at`).
_TREE_EVENT_ENTITIES = frozenset({"task", "feature", "project"})
_tree_cells_lock = threading.Lock()
_tree_cells_cache: OrderedDict[tuple[Any, ...], tuple[float, int, tuple[Any, ...], dict]] = OrderedDict()
#: Bumped by every relayed event that can move a count, and on every (re)connect of the LISTEN (`app/events.py`).
_tree_generation = 0

Cells = dict[tuple[int, Optional[UUID]], Counts]


def _note_console_event(event: Optional[dict[str, Any]]) -> None:
    """`events.add_event_hook`: a relayed event that can move a count retires every cached answer in this process,
    as does a (re)connect, after which events may have been missed. Org-blind — a task event names no org."""
    global _tree_generation
    if event is None or event.get("entity") in _TREE_EVENT_ENTITIES:
        with _tree_cells_lock:
            _tree_generation += 1


events.add_event_hook(_note_console_event)


def ordered_features() -> Select:
    """`select(Feature)` in the rail's order: live ones as shared (`PUT /api/features/order`), deleted ones by name."""
    order = case((Feature.deleted_at.is_(None), Feature.position), else_=0)
    return select(Feature).order_by(order, Feature.name, Feature.id)


def project_cells(session: Session, board: Select) -> Cells:
    """`_tree_cells` with no principal: the uncached count for a board no caller scope bounds (`routes/live_public.py`)."""
    return _tree_cells(session, None, board, None, "")


def _frozen(value: Any) -> Any:
    """A bound parameter as a hashable value: an expanding `IN` list becomes a tuple, a JSON dict sorted items."""
    if isinstance(value, dict):
        return tuple(sorted((k, _frozen(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_frozen(v) for v in value)
    return value


def _statement_key(stmt: Select) -> tuple[str, tuple[tuple[str, Any], ...]]:
    """`stmt`'s SQL and bound parameters: two statements with equal keys select the same rows under one seam."""
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), tuple(sorted((name, _frozen(value)) for name, value in compiled.params.items()))


def _tree_cells(
    session: Session, principal: Principal | None, board: Select, scope: Select | None, board_key: str
) -> Cells:
    """`(project_id, feature_id) -> Counts` over `board`; feature_id None is the rows filed in the project itself.

    The grouped count is the tree's one slow query (a correlated child-rank subplan per open row, `count_rows`;
    seconds on a big board), and every open console reads the tree on a 30s poll as well as on events. With `scope` (every task the
    caller may see, children included: a child's state moves its parent's placement) the answer is reused only
    while all three hold:
    * no console event that can move a count reached this process since (`_note_console_event`) — every write that
      changes a count emits one on commit, including a question's delivery, which writes no task row;
    * `scope`'s row count and newest `updated_at` stand — a write that emitted nothing (`tasks` has no
      `set_updated_at` trigger; the ORM's `onupdate` stamps it);
    * it is younger than `_TREE_CELLS_TTL_SECONDS`.
    Keyed on `board_key` (the caller names its board), the compiled `board` and `scope` statements with their
    parameters (`_statement_key`), and the request's bound scope and operator org filter (`tenancy/seam.py`) —
    everything the count reads — so two reads share an answer exactly when they would count the same rows: a
    caller's visibility (its user id, reach, system projects) is spelled into those statements, never restated
    here, and a principal's other fields (its API key, binding provenance, expiry) split nothing. Never across
    orgs: the bound scope is in the key. Never for no principal. A hit hands out copies: the cached `Counts` are
    never a response's own objects."""
    stamp: tuple[Any, ...] | None = None
    key: tuple[Any, ...] | None = None
    generation = _tree_generation
    if scope is not None and principal is not None:
        key = (
            board_key, _statement_key(board), _statement_key(scope), seam.current_scope(), seam.request_org_filter()
        )
        stamp = tuple(session.execute(
            scope.with_only_columns(func.count(), func.max(Task.updated_at)).order_by(None)
        ).one())
        now = time.monotonic()
        with _tree_cells_lock:
            held = _tree_cells_cache.get(key)
            if held is not None and held[1:3] == (generation, stamp) and now - held[0] < _TREE_CELLS_TTL_SECONDS:
                _tree_cells_cache.move_to_end(key)
                return {cell: counts.model_copy() for cell, counts in held[3].items()}
    cells: Cells = {}
    for project_id, feature_id, section, kind_rank, n in count_rows(
        session, board, Task.project_id, Task.feature_id
    ):
        cells.setdefault((project_id, feature_id), Counts()).add_bucket(section, kind_rank, n)
    if stamp is not None and key is not None:
        with _tree_cells_lock:
            # The generation read BEFORE the count: an event during it leaves this entry already retired.
            _tree_cells_cache[key] = (time.monotonic(), generation, stamp, {c: n.model_copy() for c, n in cells.items()})
            _tree_cells_cache.move_to_end(key)
            while len(_tree_cells_cache) > _TREE_CELLS_MAX:
                _tree_cells_cache.popitem(last=False)
    return cells


def build_tree(
    session: Session, principal: Principal | None, board: Select, *, scope: Select | None = None,
    board_key: str = "",
) -> TaskTree:
    """The whole rail in a handful of queries: projects, features, and one grouped count over `board` (the
    default board's top-level rows the caller may see), reused while `scope` stands (`_tree_cells`); `board_key`
    names `board` for that reuse and is required with `scope`. A system project (`models.Project.system_kind`, the
    org's Nodes project) is never on the rail: its tasks are reached from the node's Automations tab."""
    if scope is not None and not board_key:
        raise ValueError("build_tree: a cached tree names its board (board_key)")
    projects = session.scalars(
        scoped_project_select(principal, key=TASK_SCOPE_ARM_KEY)
        .where(in_live_org(Project.org_id), Project.system_kind.is_(None)).order_by(Project.name, Project.id)
    ).all()
    features = session.scalars(ordered_features().where(visible_feature_clause(principal))).all()
    cells = _tree_cells(session, principal, board, scope, board_key)
    nodes = project_nodes(session, projects, features, cells, lambda key, pid: has_project_key(principal, key, pid))
    floor = principal is None or principal.has(PROJECTS_CREATE_KEY)  # the create route's own (`authz._enforce`)
    # The root counts every cell, a hidden system project's included: the All-projects board still lists its tasks.
    return TaskTree(projects=nodes, counts=summed(cells.values()), can_create_project=floor)


def project_nodes(session: Session, projects: Sequence[Project], features: Sequence[Feature], cells: Cells,
                  can: Callable[[str, int], bool]) -> list[ProjectNode]:
    """One `ProjectNode` per project, its `features` (order kept) and `cells`' counts summed up; `can(key,
    project_id)` answers the `can_*` flags. Shared by `build_tree` and the
    public tree (`routes/live_public.py`)."""
    features_by_project: dict[int, list[Feature]] = {}
    for f in features:
        features_by_project.setdefault(f.project_id, []).append(f)

    cells_by_project: dict[int, list[Counts]] = {}
    for (project_id, _feature_id), counts in cells.items():
        cells_by_project.setdefault(project_id, []).append(counts)
    orgs = org_refs(session, (project.org_id for project in projects))
    nodes: list[ProjectNode] = []
    for project in projects:
        own = features_by_project.get(project.id, [])
        feature_nodes = [
            from_row(FeatureNode, f, counts=cells.get((project.id, f.id), Counts())) for f in own if f.deleted_at is None
        ]
        deleted_nodes = [
            DeletedFeatureNode(id=f.id, name=f.name, icon=f.icon)
            for f in own if f.deleted_at is not None and (project.id, f.id) in cells
        ]
        nodes.append(ProjectNode(
            id=project.id,
            name=project.name,
            org=org_ref_of(orgs, project.org_id),
            can_edit_project=can(PROJECTS_EDIT_KEY, project.id),
            can_create_tasks=can(TASKS_CREATE_KEY, project.id),
            can_edit_tasks=can(TASKS_EDIT_KEY, project.id),
            features=feature_nodes,
            deleted_features=deleted_nodes,
            counts=summed(cells_by_project.get(project.id, [])),
        ))
    return nodes
