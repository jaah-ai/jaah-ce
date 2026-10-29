"""The forecast a task gets before it is started.

One read, no writes. `GET /api/tasks/{id}/dispatch-forecast` answers "will this run now, or wait — and
behind what?". Its `queue_position` counts dispatchable, non-hidden `ready` rows fleet-wide in dispatch
order (`priority DESC, id`, as `routes/tasks/dispatch.py::dispatchable_tasks`).

The forecast is apparent capacity, an estimate: the real decision is
`routes/fleet.py::build_dispatch_plan`, whose reads (`_room_by_node`, `_pool_read`, `_best_account`) are
imported rather than re-derived. The scarce resources are the worktrees and the accounts: `headroom` is
the slots this project could still take per live node (free statics plus `slot_allocation.dynamic_room`),
and a node with no free worktree contributes 0 unless the task's own held slot is there.

`preemptable` is false when a Stop cannot help: the target is held by its own gates, no live node has a
free worktree or the task's slot (Stop keeps the victim's tree). Candidates are only
runners on this project's live nodes (a slot freed elsewhere is no use here).

An unreadable pool reports `headroom 0` with "Pool state unavailable" rather than 500ing. Rows are
scoped by `scoped_task_select(principal)`; the counts (`ahead`, `queue_position`) deliberately include
org rows the caller cannot list, since the dispatcher ignores grants — count only, no identities.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app import pool_state, slot_state
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import scoped_task_select
from app.db import get_session
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.routes import fleet as fleet_routes
# The dispatcher's own read surface, gate predicates and order key — imported, never restated.
from app.routes.tasks.policy import PERSONA_EPIC, TRANSITION_HOLD
from app.routes.tasks.dispatch import (
    _has_unmet_prerequisite,
    _parent_epic_has_unmet_prerequisite,
    _parent_epic_paused,
    _vanish_backoff_holds,
    dispatchable_tasks,
)
from app.task_lifecycle import _RUNNER_RUNNING_STATES
from app.task_lookup import get_task_or_404
from app.task_priority import priority_label, sorts_ahead

# No `prefix=`: the route spells its own path.
router = APIRouter(tags=["task-queue"])

#: The ways a `ready` row can be held off the dispatcher, from the predicates `dispatchable_tasks`
#: excludes on. A paused epic beats even a manual Run; the others sit inside the Run bypass.
HOLD_EPIC_PAUSED = "epic_paused"
HOLD_PREREQUISITES = "prerequisites"
HOLD_EPIC_HOLD = "epic_hold"
#: Its runner was lost recently, so its re-dispatch backs off (`routes/tasks/dispatch.py::vanish_backoff_secs`).
HOLD_RUNNER_LOST = "runner_lost"
#: Its own worktree is on a node that is not live (`pool_state.held_on_dead_nodes`). `dispatchable_tasks`
#: passes it (the SQL gates know nothing of nodes), but the drain offers a held task only to its hold node and
#: plans only live ones, so the forecast must test it.
HOLD_NODE_OFFLINE = "node_offline"

#: The forecast's sentence for each hold name.
_HOLD_SENTENCES = {
    HOLD_PREREQUISITES: "Waiting on prerequisites",
    HOLD_EPIC_HOLD: "Epic is on hold",
    HOLD_RUNNER_LOST: "Runner lost; retrying after a backoff",
    HOLD_EPIC_PAUSED: "Epic is stopped",
    HOLD_NODE_OFFLINE: "Its worktree's node is offline",
}


# ── The wire ──────────────────────────────────────────────────────────────────────────────────────────


class PreemptCandidateOut(BaseModel):
    """A running task the operator could Stop to make room — sorted least-costly-to-stop first."""

    id: int
    title: str
    project_name: str
    priority: int
    priority_label: str
    state: str
    #: When its slot was last dispatched into (`Worktree.dispatched_at`), None for a hold with no stamp.
    dispatched_at: Optional[datetime] = None


class DispatchForecastOut(BaseModel):
    can_dispatch_now: bool
    #: Short human sentences, one per thing that BLOCKS; empty when nothing does.
    reasons: list[str]
    #: `ahead + 1`: where this task would stand if marked ready now. None for a row that cannot queue.
    queue_position: Optional[int] = None
    #: Free runner places across the project's live nodes (0 for a node with no free worktree, unless
    #: the task's own held slot sits there).
    headroom: int
    #: True iff Stopping one of `running` could plausibly let this task dispatch (not self-held,
    #: dispatch on, a leasable live node, and a runner there to stop).
    preemptable: bool
    #: Running tasks holding a slot on one of this task's project's live nodes, least-costly-to-stop
    #: first. Empty when preemption cannot help (a held target, no live node).
    running: list[PreemptCandidateOut]


# ── The holds ─────────────────────────────────────────────────────────────────────────────────────────


def _hold_reasons(session: Session, task_ids: list[int]) -> dict[int, Optional[str]]:
    """Why each of `task_ids` is held, from the same predicates `dispatchable_tasks` excludes on, in one
    statement.

    Arm order is gate order: a paused epic first (outside the Run bypass), then a Run request clears
    the rest, then the two dependency gates (one label, one remedy), then the epic's `hold` mode, then the
    lost-runner backoff."""
    if not task_ids:
        return {}
    parent = aliased(Task)
    parent_epic_holds = (
        select(parent.id)
        .where(parent.id == Task.parent_task_id, parent.transition_mode == TRANSITION_HOLD)
        .exists()
    )
    rows = session.execute(
        select(
            Task.id,
            Task.run_requested_at.is_not(None).label("run_requested"),
            _parent_epic_paused().label("epic_paused"),
            _has_unmet_prerequisite().label("own_prerequisite"),
            _parent_epic_has_unmet_prerequisite().label("epic_prerequisite"),
            parent_epic_holds.label("epic_hold"),
            _vanish_backoff_holds().label("runner_lost"),
        ).where(Task.id.in_(task_ids))
    ).all()
    out: dict[int, Optional[str]] = {}
    for row in rows:
        if row.epic_paused:
            out[row.id] = HOLD_EPIC_PAUSED
        elif row.run_requested:
            out[row.id] = None
        elif row.own_prerequisite or row.epic_prerequisite:
            out[row.id] = HOLD_PREREQUISITES
        elif row.epic_hold:
            out[row.id] = HOLD_EPIC_HOLD
        elif row.runner_lost:
            out[row.id] = HOLD_RUNNER_LOST
        else:
            out[row.id] = None
    return out


# ── The forecast ──────────────────────────────────────────────────────────────────────────────────────


def _preempt_candidates(
    session: Session, principal: Principal | None, live_nodes: list[str]
) -> list[PreemptCandidateOut]:
    """Every running non-epic task the caller may see with a held slot on one of `live_nodes` (node uuid
    strings, `pool_state.live_nodes_for_project`), least sunk
    work first (lowest priority, then highest id). Not filtered on `hidden`: hidden tasks hold runners
    too.

    Scoped to the target's nodes (a slot freed elsewhere is no use); any project's task there qualifies. The slot
    rows are read first so the task read stays a plain `scoped_task_select`."""
    if not live_nodes:
        return []
    # The held slot per running task on these nodes, with its dispatch stamp.
    dispatched_at = dict(
        session.execute(
            select(Worktree.task_id, Worktree.dispatched_at)
            .where(
                Worktree.state == slot_state.TASK,
                Worktree.task_id.is_not(None),
                Worktree.node_id.in_([UUID(node_id) for node_id in live_nodes]),
            )
        ).all()
    )
    if not dispatched_at:
        return []
    rows = session.scalars(
        scoped_task_select(principal)
        .where(
            Task.id.in_(list(dispatched_at)),
            Task.state.in_(_RUNNER_RUNNING_STATES),
            Task.persona != PERSONA_EPIC,
            Task.archived_at.is_(None),
        )
        .order_by(func.coalesce(Task.priority, 0).asc(), Task.id.desc())
    ).all()
    if not rows:
        return []
    project_names = dict(
        session.execute(
            select(Project.id, Project.name).where(Project.id.in_({t.project_id for t in rows}))
        ).all()
    )
    return [
        PreemptCandidateOut(
            id=t.id,
            title=t.title,
            project_name=project_names.get(t.project_id, ""),
            priority=t.priority if t.priority is not None else 0,
            priority_label=priority_label(t.priority),
            state=t.state,
            dispatched_at=dispatched_at.get(t.id),
        )
        for t in rows
    ]


def _queued_ahead(session: Session, task: Task) -> int:
    """How many dispatchable `ready` tasks across all projects the dispatcher would take before
    `task` if it were ready now. `sorts_ahead` is the order key; the task itself never counts."""
    others = [
        other
        for project_id in session.scalars(select(Project.id)).all()
        for other in dispatchable_tasks(project_id, session)
        # Hidden rows are excluded: they are not part of the queue an operator counts.
        if not other.hidden and other.id != task.id
        and sorts_ahead(other.priority, other.id, task.priority, task.id)
    ]
    # A row stranded on an offline node is held (`HOLD_NODE_OFFLINE`), so it is not ahead.
    stranded = pool_state.held_on_dead_nodes(session, [other.id for other in others])
    return sum(1 for other in others if other.id not in stranded)


@router.get("/api/tasks/{task_id}/dispatch-forecast", response_model=DispatchForecastOut)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def dispatch_forecast(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> DispatchForecastOut:
    """Would this task run now if it were marked ready — and if not, what stands in the way.

    `can_dispatch_now = headroom > ahead and account_ok`. `queue_position` is
    `ahead + 1`. An epic or already-running task gets one reason instead of a position.

    The target's own hold (from `_hold_reasons`) is answered first: no headroom or Stop moves it, so it
    gets one reason naming the gate, no position, no candidates."""
    task = get_task_or_404(task_id, session)
    if task.persona == PERSONA_EPIC or task.state in _RUNNER_RUNNING_STATES:
        return DispatchForecastOut(
            can_dispatch_now=False, reasons=["not a dispatchable task"],
            queue_position=None, headroom=0, preemptable=False, running=[],
        )
    # As `force_start_task` guards it: a 422 naming the dangling project id.
    project = session.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=422, detail=f"project {task.project_id} not found")
    hold = _hold_reasons(session, [task.id]).get(task.id)
    if hold is None and task.id in pool_state.held_on_dead_nodes(session, [task.id]):
        hold = HOLD_NODE_OFFLINE
    if hold is not None:
        return DispatchForecastOut(
            can_dispatch_now=False, reasons=[_HOLD_SENTENCES[hold]],
            queue_position=None, headroom=0, preemptable=False, running=[],
        )
    ahead = _queued_ahead(session, task)

    live_nodes = pool_state.live_nodes_for_project(session, project.id)
    running = _preempt_candidates(session, principal, live_nodes)
    # Free static slots plus the dynamic worktrees each node could still make, as the allocator reads them.
    free_by_node = fleet_routes._room_by_node(session, project.id, live_nodes)
    # A task already holding a slot (Stopped mid-run) resumes into it, so that node counts as leasable.
    own_hold = slot_state.task_hold(session, task.id)
    own_hold_node = own_hold["node_id"] if own_hold is not None else None
    org_by_node: dict[str, Optional[UUID]] = {}
    if live_nodes:
        org_by_node = dict(
            (str(node_id), org_id)
            for node_id, org_id in session.execute(
                select(Node.id, Node.org_id).where(Node.id.in_([UUID(node_id) for node_id in live_nodes]))
            ).all()
        )
    headroom = 0
    pool_unreadable = False
    account_ok = False
    # Decided before the pool read: an unreadable pool hides occupancy, not slots.
    has_leasable_node = False
    # Pinned as `build_dispatch_plan` pins, collapsing `_best_account`'s per-call reads; disarmed in
    # `finally` because the session outlives this block.
    fleet_routes._pin_account_candidates(session)
    try:
        # Node uuid strings throughout: `live_nodes`, `free_by_node`, `own_hold_node` and `org_by_node` share them.
        for node_id in live_nodes:
            # The slots this project could still take here: free statics plus the dynamic worktrees the node
            # could still make, and this task's own slot when it holds one (a resume takes no new place).
            room = free_by_node.get(node_id, 0) + (1 if node_id == own_hold_node else 0)
            has_leasable_node = has_leasable_node or room > 0
            _records, pool_ok, _detail = fleet_routes._pool_read(session, node_id)
            if not pool_ok:
                # The plan's fail-closed verdict, reported rather than raised.
                pool_unreadable = True
                continue
            node_org_id = org_by_node.get(node_id)
            headroom += room
            if not account_ok:
                account_ok = (
                    fleet_routes._best_account(session, node_id, node_org_id=node_org_id) is not None
                )
    finally:
        fleet_routes._unpin_account_candidates(session)

    reasons: list[str] = []
    if not live_nodes:
        reasons.append("Project has no live node")
    elif not has_leasable_node:
        # Distinct remedy: a Stop keeps the victim's tree, so the operator needs a slot freed or enrolled.
        reasons.append("No free worktree")
    elif headroom == 0 and pool_unreadable:
        reasons.append("Pool state unavailable")
    if ahead > 0 and ahead >= headroom:
        reasons.append(f"{ahead} higher-priority task(s) queued ahead")
    if not account_ok:
        reasons.append("No account with quota available")
    return DispatchForecastOut(
        can_dispatch_now=headroom > ahead and account_ok,
        reasons=reasons,
        queue_position=ahead + 1,
        headroom=headroom,
        # Not held (returned above); needs a leasable node and someone to stop.
        preemptable=has_leasable_node and bool(running),
        running=running,
    )
