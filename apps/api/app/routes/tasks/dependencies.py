"""Task dependencies: the edge routes, cycle and deadlock checks, and staleness propagation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import actors, merge_lane_state
from app.auth import require_user
from app.authz.catalog import AUTHOR_DRAFT_KEY, permission_key
from app.authz.declarations import feature
from app.authz.object_access import author_draft_admits, authored_draft, can_access, key_scope_satisfied
from app.authz.principal import Principal, current_principal
from app.authz.visibility import TASK_SPEC, ObjectAccess
from app.db import get_session
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.tasks import Task, TaskDependency
from app.task_lifecycle import DONE
from app.task_lookup import get_task_or_404
from app.routes.tasks.task_wire import _available_transitions_for, _prerequisite_ids, _visible_task_ids

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _reaches(start_id: int, target_id: int, session: Session) -> bool:
    """True if `target_id` is reachable from `start_id` by following prerequisite edges. Iterative DFS over
    `task_dependencies`, bounded by a `seen` set so a pre-existing cycle can never loop forever."""
    stack = [start_id]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current == target_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(_prerequisite_ids(current, session))
    return False


def _gate_would_deadlock(task_id: int, depends_on_task_id: int, session: Session) -> bool:
    """True if the edge `task_id → depends_on_task_id` could never be satisfied, because the prerequisite
    sits UNDER the gate the edge itself would create.

    `_would_create_cycle` walks `task_dependencies` alone; this runs through `parent_task_id`:
    `_parent_epic_has_unmet_prerequisite` holds every child of a gated epic, so a prerequisite that is (or
    waits on) a child of `task_id` never reaches `done`. The traversal follows effective prerequisites
    (own edges plus the parent epic's), bounded by a `seen` set."""
    stack = [depends_on_task_id]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        row = session.get(Task, current)
        if row is None:
            continue
        if current == task_id or row.parent_task_id == task_id:
            return True
        stack.extend(_prerequisite_ids(current, session))
        if row.parent_task_id is not None:
            stack.extend(_prerequisite_ids(row.parent_task_id, session))
    return False


def _would_create_cycle(task_id: int, depends_on_task_id: int, session: Session) -> bool:
    """True if adding edge `task_id → depends_on_task_id` would close a dependency cycle — i.e. `task_id`
    is already a transitive prerequisite of `depends_on_task_id`, so the new edge loops back. A self-edge
    is caught separately, with a clearer message."""
    return _reaches(depends_on_task_id, task_id, session)


# ── Phase-DAG staleness propagation ═════════════════════════════════════════════════════════════════
# `task_dependencies` has no DB cycle prevention, so both traversals are bounded by a visited set. A
# significant edit marks the task and every transitive dependent stale; `retry --cascade` marks
# dependents too, but re-running them stays a human decision.

# `project_id` is significant: a recon map names paths in a specific repo, and a move changes the prompt's
# scope and audience. The set is intersected with submitted `fields`, so every entry
# must be a field `TaskUpdate` accepts, or it silently matches nothing. `external_task_system`/
# `external_task_id` must stay absent: no prompt text comes from them, so relinking must not stale a subtree.
_SIGNIFICANT_EDIT_FIELDS = frozenset(
    {"title", "body", "autonomous", "project_id"}
)


def _dependent_ids_bfs(root_id: int, session: Session) -> list[int]:
    """Every task transitively blocked-by `root_id`, EXCLUDING `root_id` itself even if a cycle loops back
    to it: `visited` seeds with `root_id`, so a cyclic graph can never revisit it or loop forever."""
    visited: set[int] = {root_id}
    queue = list(
        session.scalars(
            select(TaskDependency.task_id).where(TaskDependency.depends_on_task_id == root_id)
        ).all()
    )
    order: list[int] = []
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        order.append(current)
        queue.extend(
            session.scalars(
                select(TaskDependency.task_id).where(TaskDependency.depends_on_task_id == current)
            ).all()
        )
    return order


def _mark_stale(
    task_ids: list[int], reason: str, session: Session, *, notify_ids: Optional[list[int]] = None
) -> None:
    """Set `stale_reason`/`stale_since` on every id in `task_ids` (a no-op on an empty list), emitting a live
    `stale` event per id so dependents' views update. `record_transition=False`: the state does not
    change, and a same-state `task:` row would inflate completion counts. The columns are the record.

    `notify_ids` narrows who is told, never who is marked; None notifies the whole set. See
    `_propagate_staleness`'s `notify_root`."""
    if not task_ids:
        return
    session.execute(
        update(Task)
        .where(Task.id.in_(task_ids))
        .values(
            stale_reason=reason, stale_since=datetime.now(timezone.utc),
            **actors.task_actor_values(session),
        )
    )
    for tid in (task_ids if notify_ids is None else notify_ids):
        emit_console_event(session, entity="task", id=tid, action="stale", record_transition=False)


def _propagate_staleness(
    root_id: int, reason: str, session: Session, *, notify_root: bool = True
) -> list[int]:
    """Mark `root_id` itself AND every transitive `task_dependencies` dependent stale, bounded on a cyclic
    graph by `_dependent_ids_bfs`'s visited set. Returns the full list of ids marked, root first.

    `notify_root=False` marks the root without an event, for auto-save only: the console remounts its
    edit form on any event for the row, which would destroy the editor mid-typing. Dependents are still
    told."""
    marked = [root_id] + _dependent_ids_bfs(root_id, session)
    _mark_stale(marked, reason, session, notify_ids=None if notify_root else marked[1:])
    return marked


class TaskDepAddIn(BaseModel):
    """Declare a prerequisite: `task_id` (path) becomes blocked-by `depends_on_task_id` (spec R1)."""

    depends_on_task_id: int


class TaskDepRef(BaseModel):
    """A task referenced from the dependency view — enough for the UI to render + link it, plus the
    derived `done` flag the readiness gate turns on (`state == 'done'`)."""

    id: int
    title: str
    state: str
    done: bool
    # `done` but its PR still waits in the merge lane (`merge_lane.awaiting_lane_merge`): the dispatch gate still
    # counts it unmet, so the panel must not mark it satisfied.
    awaiting_merge: bool = False
    # Staleness marker, surfaced so the dependency panel can render a dirty badge without a second fetch.
    stale_reason: Optional[str] = None
    stale_since: Optional[datetime] = None
    # Same per-row transition enablement `TaskOut` carries, so the panel's row controls never re-derive it.
    available_transitions: list[str] = []


class TaskDependenciesOut(BaseModel):
    """A Task's full dependency picture: its `prerequisites` (what it is blocked-by), its `dependents` (what
    is blocked-by it), the `unmet_prerequisite_ids` that gate its dispatch, and the derived `ready` flag.

    `ready` and the unmet count cover the full prerequisite set (dispatch facts about the caller's own
    task; filtering them would report `ready: true` for a task that will not run). The lists carry only
    visible rows; the remainder becomes the two `hidden_*` counts."""

    task_id: int
    prerequisites: list[TaskDepRef]
    dependents: list[TaskDepRef]
    unmet_prerequisite_ids: list[int]
    ready: bool
    hidden_unmet_prerequisite_count: int = 0
    hidden_dependent_count: int = 0


def _dep_ref(task: Task, awaiting: frozenset[int] | set[int] = frozenset()) -> TaskDepRef:
    return TaskDepRef(
        id=task.id,
        title=task.title,
        state=task.state,
        done=task.state == DONE,
        awaiting_merge=task.id in awaiting,
        stale_reason=task.stale_reason,
        stale_since=task.stale_since,
        available_transitions=_available_transitions_for(task),
    )


def _dependencies_out(
    task_id: int, session: Session, principal: Principal | None
) -> TaskDependenciesOut:
    """Assemble the dependency view for `task_id` (assumes the Task exists — the routes 404 first). Gating
    facts over the full set, details over the visible one (see `TaskDependenciesOut`). One scoped id
    query per side, no N+1."""
    prereq_ids = _prerequisite_ids(task_id, session)
    prereqs = (
        sorted(session.scalars(select(Task).where(Task.id.in_(prereq_ids))).all(), key=lambda t: t.id)
        if prereq_ids
        else []
    )
    dependent_ids = list(
        session.scalars(
            select(TaskDependency.task_id)
            .where(TaskDependency.depends_on_task_id == task_id)
            .order_by(TaskDependency.task_id)
        ).all()
    )
    dependents = (
        sorted(session.scalars(select(Task).where(Task.id.in_(dependent_ids))).all(), key=lambda t: t.id)
        if dependent_ids
        else []
    )
    # Over the full prerequisite list (dispatch facts). Unmet follows the dispatch gate, archived rule
    # included (`_has_unmet_prerequisite`); an archived prerequisite still appears under `prerequisites`.
    awaiting = merge_lane_state.awaiting_lane_merge(session, [t.id for t in prereqs if t.state == DONE])
    unmet = [t.id for t in prereqs if (t.state != DONE or t.id in awaiting) and t.archived_at is None]
    ready = not unmet
    visible_ids = _visible_task_ids(session, principal, prereq_ids + dependent_ids)
    visible_prereqs = [t for t in prereqs if t.id in visible_ids]
    visible_dependents = [t for t in dependents if t.id in visible_ids]
    return TaskDependenciesOut(
        task_id=task_id,
        prerequisites=[_dep_ref(t, awaiting) for t in visible_prereqs],
        dependents=[_dep_ref(t) for t in visible_dependents],
        unmet_prerequisite_ids=[tid for tid in unmet if tid in visible_ids],
        ready=ready,
        # Both invisible and unmet, explaining an otherwise unaccountable `ready: false`; excluded as in `unmet`.
        hidden_unmet_prerequisite_count=sum(
            1 for t in prereqs
            if t.id not in visible_ids and t.id in unmet
        ),
        hidden_dependent_count=sum(1 for t in dependents if t.id not in visible_ids),
    )


@router.get("/{task_id}/dependencies", response_model=TaskDependenciesOut)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def get_task_dependencies(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskDependenciesOut:
    """A Task's prerequisites + dependents + readiness. Read-only; 404 on unknown id. The RESPONSE embeds
    other tasks, which the route-level guard cannot scope (it reads path params, not response bodies), so
    the split lives in `_dependencies_out` — see `TaskDependenciesOut` for which halves are filtered."""
    get_task_or_404(task_id, session)
    return _dependencies_out(task_id, session, principal)


def _may_attach_prerequisite(
    session: Session, principal: Principal | None, task_id: int, dep_id: int
) -> bool:
    """May `principal` name `dep_id` as a prerequisite of `task_id`? The guard's path-param decision
    (`authz._enforce_object_params`) restated for a body-borne id, so this admits exactly what the
    `DELETE` twin admits.

    WRITE mode on the prerequisite. In the guard's order: the caller's own still-draft row
    (`author_draft_admits`); else the write clause admits it and the key is held on its
    scope by the granting subject: `tasks:edit`, or `tasks:create` when the route's row
    is the caller's draft. Never `tasks:create` alone."""
    if author_draft_admits(session, principal, TASK_SPEC, dep_id, mode=ObjectAccess.WRITE):
        return True
    if not can_access(session, principal, "task", dep_id, mode=ObjectAccess.WRITE):
        return False
    if key_scope_satisfied(
        session, principal, TASK_SPEC, dep_id, key=permission_key("tasks", "edit"),
        mode=ObjectAccess.WRITE,
    ):
        return True
    return key_scope_satisfied(
        session, principal, TASK_SPEC, dep_id, key=AUTHOR_DRAFT_KEY, mode=ObjectAccess.WRITE
    ) and authored_draft(session, principal, task_id)


@router.post("/{task_id}/dependencies", response_model=TaskDependenciesOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",), author_draft=True)
def add_task_dependency(
    task_id: int,
    body: TaskDepAddIn,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskDependenciesOut:
    """Declare `task_id` blocked-by `depends_on_task_id`. Rejects a self-edge (422), a cycle (422), and an
    edge whose prerequisite it would hold back (422, `_gate_would_deadlock`); idempotent otherwise. The
    gate applies at dispatch time, so a new unmet prerequisite only gates the next dispatch. Editable in
    any state."""
    get_task_or_404(task_id, session)
    dep_id = body.depends_on_task_id
    if dep_id == task_id:
        raise HTTPException(status_code=422, detail=f"task {task_id} cannot depend on itself")
    # The referenced task is access-checked, and inaccessible is indistinguishable from nonexistent (same
    # 422): a body-borne id the route guard cannot see, or the route becomes a task-enumeration oracle. See
    # `_may_attach_prerequisite`.
    if session.get(Task, dep_id) is None or not _may_attach_prerequisite(
        session, principal, task_id, dep_id
    ):
        raise HTTPException(status_code=422, detail=f"prerequisite task {dep_id} not found")
    # Idempotent: an existing edge returns the current view, no duplicate insert, no event.
    if session.get(TaskDependency, {"task_id": task_id, "depends_on_task_id": dep_id}) is not None:
        return _dependencies_out(task_id, session, principal)
    if _would_create_cycle(task_id, dep_id, session):
        raise HTTPException(
            status_code=422,
            detail=(
                f"adding prerequisite {dep_id} to task {task_id} would create a dependency cycle "
                f"(task {dep_id} already depends on task {task_id}, directly or transitively)"
            ),
        )
    if _gate_would_deadlock(task_id, dep_id, session):
        raise HTTPException(
            status_code=422,
            detail=(
                f"adding prerequisite {dep_id} to task {task_id} would deadlock: {dep_id} is, or waits "
                f"on, a subtask of {task_id}, which this edge would itself hold back — so the "
                f"prerequisite could never be met"
            ),
        )
    session.add(TaskDependency(task_id=task_id, depends_on_task_id=dep_id))
    # Emit on the DEPENDENT task so its console detail (which shows the dependency view) refetches live.
    emit_console_event(session, entity="task", id=task_id, action="dependency_added")
    session.commit()
    return _dependencies_out(task_id, session, principal)


@router.delete("/{task_id}/dependencies/{depends_on_task_id}", status_code=204)
@feature(
    "tasks", "edit", object_type="task",
    object_params=("task_id", "depends_on_task_id"), author_draft=True,
)
def remove_task_dependency(
    task_id: int,
    depends_on_task_id: int,
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """Remove the `task_id → depends_on_task_id` prerequisite edge. Idempotent — a 204 whether or not the
    edge existed, since removing an absent prerequisite is not an error. A removed prerequisite may unblock
    the Task; its `ready` recomputes on the next read."""
    get_task_or_404(task_id, session)
    edge = session.get(TaskDependency, {"task_id": task_id, "depends_on_task_id": depends_on_task_id})
    if edge is not None:
        session.delete(edge)
        emit_console_event(session, entity="task", id=task_id, action="dependency_removed")
        session.commit()
    return Response(status_code=204)
