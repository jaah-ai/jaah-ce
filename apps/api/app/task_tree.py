"""The task rail's data — `GET /api/tasks/tree` and `GET /api/tasks/counts` (`routes/tasks/`).

One tree for the caller: Project › Feature (active and shipped), plus each project's unfiled ("No
feature") rows, every node carrying `Counts`. THE COUNTING UNIT is a top-level board row
(`routes/tasks/listing.py::board_row_clauses`), bucketed by `task_sections.section_sql`, so an epic counts once,
where its stage tasks place it. `questions + failed + paused + waiting == stopped`, `queued` is the slice of
`in_progress` not yet running (a `ready` row), and every parent's
counts are the server-side sum of its children, so a closed branch never hides a blocker.

Visibility is the existing rules, read, never restated: a project is present iff the caller holds
`tasks:view` there (the key that shows its tasks), its features by `authz.visible_feature_clause`, and
every count is over `authz.scoped_task_select`. Shipped features stay in the tree and their rows in the
sums: shipping never hides work. `unfiled` is null when the project has no unfiled rows, so the rail draws
no empty "No feature" node.

THE PINNED SHAPE: the console's rail, section headers and feature bar bind to these names.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, and_, case, func, literal, select, text
from sqlalchemy.orm import Session

from app import task_sections
from app.authz.catalog import permission_key
from app.authz.principal import Principal
from app.authz.visibility import (
    TASK_SCOPE_ARM_KEY,
    has_project_key,
    scoped_project_select,
    visible_feature_clause,
)
from app.features import FEATURE_ACTIVE
from app.models.orgs import in_live_org
from app.models.projects import Feature, Project
from app.models.tasks import Task
from app.task_lifecycle import READY
from app.tenancy.labels import OrgRef, org_ref_of, org_refs

TASKS_CREATE_KEY = permission_key("tasks", "create")
TASKS_EDIT_KEY = permission_key("tasks", "edit")
PROJECTS_EDIT_KEY = permission_key("projects", "edit")
PROJECTS_CREATE_KEY = permission_key("projects", "create")

# `count_rows`' placement for an In progress row that is still `ready`: counted in `in_progress` and `queued`.
QUEUED_SECTION = "queued"

# `Counts`' per-kind field for each `STOP_KIND_ORDER` member. A system pause of Jaah-account work is a pause:
# the rail counts it with the others, and its row says which (`StopOut.kind`).
_KIND_FIELD: dict[str, str] = {
    "question": "questions",
    "failed": "failed",
    "paused": "paused",
    "balance": "paused",
    "jaah_unavailable": "paused",
    "waiting_on_customer": "waiting",
}


class Counts(BaseModel):
    stopped: int = 0
    questions: int = 0
    failed: int = 0
    paused: int = 0
    waiting: int = 0
    in_progress: int = 0
    queued: int = 0
    draft: int = 0
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
        elif section == "draft":
            self.draft += n
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
    status: Literal["active", "shipped"]
    lead_id: UUID
    # `models.FEATURE_ICONS` or null; every row a task tree draws takes its icon from here.
    icon: Optional[str]
    # Whether the rail draws the feature's completion ring (`models.Feature.show_progress`).
    show_progress: bool
    shipped_at: Optional[datetime]
    counts: Counts


class ProjectNode(BaseModel):
    id: int
    name: str
    # `'nodes'` for the org's built-in Nodes project (`models.Project.system_kind`); the rail draws a System chip.
    system_kind: Optional[str] = None
    # The project's tenant (`tenancy/labels.py::OrgRef`); names may repeat across orgs, so the rail tells
    # two same-named projects apart by it. Null when the row is bound to no org.
    org: Optional[OrgRef] = None
    can_edit_project: bool
    can_create_tasks: bool
    can_edit_tasks: bool
    features: list[FeatureNode]
    # The project's "No feature" rows; null when there are none.
    unfiled: Optional[Counts]
    counts: Counts


class TaskTree(BaseModel):
    projects: list[ProjectNode]
    counts: Counts
    can_create_project: bool


def _placement_columns() -> tuple[Any, Any]:
    return (
        task_sections.section_sql(Task).label("section"),
        task_sections.stop_kind_rank_sql(Task).label("kind_rank"),
    )


def count_rows(session: Session, board: Select, *group_by: Any) -> list[tuple[Any, ...]]:
    """`(*group_by, section, kind_rank, n)` over `board` — a `select(Task)` already narrowed to the rows
    to count. One GROUP BY; the section and kind are computed per row in a subquery. An In progress row still
    `ready` reads `QUEUED_SECTION`, so `Counts.add_bucket` can split it out."""
    section, kind_rank = _placement_columns()
    inner = board.with_only_columns(*group_by, section, kind_rank, Task.state.label("state")).subquery()
    keys = [inner.c[col.key] for col in group_by]
    placed = case(
        (and_(inner.c.section == "in_progress", inner.c.state == READY), literal(QUEUED_SECTION)),
        else_=inner.c.section,
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


def build_tree(session: Session, principal: Principal | None, board: Select) -> TaskTree:
    """The whole rail in a handful of queries: projects, features, and one grouped count over `board` (the
    default board's top-level rows the caller may see)."""
    projects = session.scalars(
        scoped_project_select(principal, key=TASK_SCOPE_ARM_KEY)
        .where(in_live_org(Project.org_id)).order_by(Project.name, Project.id)
    ).all()
    features = session.scalars(
        select(Feature)
        .where(visible_feature_clause(principal))
        # Active features in the project's shared order (`PUT /api/features/order`); shipped ones, which never
        # reorder, by name.
        .order_by(case((Feature.status == FEATURE_ACTIVE, Feature.position), else_=0), Feature.name, Feature.id)
    ).all()

    # (project_id, feature_id) -> Counts; feature_id None is the unfiled bucket.
    cells: dict[tuple[int, Optional[UUID]], Counts] = {}
    for project_id, feature_id, section, kind_rank, n in count_rows(
        session, board, Task.project_id, Task.feature_id
    ):
        cells.setdefault((project_id, feature_id), Counts()).add_bucket(section, kind_rank, n)

    features_by_project: dict[int, list[Feature]] = {}
    for f in features:
        features_by_project.setdefault(f.project_id, []).append(f)

    orgs = org_refs(session, (project.org_id for project in projects))
    nodes: list[ProjectNode] = []
    for project in projects:
        feature_nodes = [
            FeatureNode(
                id=f.id,
                name=f.name,
                status=f.status,  # type: ignore[arg-type]
                lead_id=f.lead_id,
                icon=f.icon,
                show_progress=f.show_progress,
                shipped_at=f.shipped_at,
                counts=cells.get((project.id, f.id), Counts()),
            )
            for f in features_by_project.get(project.id, [])
        ]
        unfiled = cells.get((project.id, None))
        nodes.append(ProjectNode(
            id=project.id,
            name=project.name,
            system_kind=project.system_kind,
            org=org_ref_of(orgs, project.org_id),
            can_edit_project=has_project_key(principal, PROJECTS_EDIT_KEY, project.id),
            can_create_tasks=has_project_key(principal, TASKS_CREATE_KEY, project.id),
            can_edit_tasks=has_project_key(principal, TASKS_EDIT_KEY, project.id),
            features=feature_nodes,
            unfiled=unfiled,
            counts=summed([*([unfiled] if unfiled else []), *(fn.counts for fn in feature_nodes)]),
        ))

    return TaskTree(
        projects=nodes,
        counts=summed(p.counts for p in nodes),
        # The create route's own floor (`authz._enforce`: the key held anywhere).
        can_create_project=principal is None or principal.has(PROJECTS_CREATE_KEY),
    )
