"""Launches that failed on a node, counted per node — and the node a task stops being relaunched on.

Two causes count, both carrying the node the launch ran on (`runner_terminations.record(node=...)`):
`launch_unannounced`, a launch acked and never claiming its task, charged by the drain
(`routes/tasks/runner_settle.py::charge_unannounced_launch`); and `launch_failed`, a runner the node never sighted
running after its ack, charged by the reclaim (`runner_settle.reclaim_active_task`). On a full-disk
host three launches in a row once hung before their first transcript write, and the drain kept relaunching into the
same slot, rotating accounts that were never the problem; another time a retry went back to the node that had just
failed it, four times on one host across three accounts.

So after `PER_NODE` such launches of one task on one node, the drain stops relaunching there and releases the
slot (`lease_drain._take_for_dispatch`), and placement leaves the node out for that task
(`routes/fleet.py::_AllowedNodeNames`) while those failures are inside `WINDOW` — unless no other live node may
run the task, when the exclusion yields: a node that is the only one a task can use must not strand it
(`ExcludedNodes.launch_failed`). Each further failed launch there is charged, so the attempt budget still
ends a run the node can never start. This is the one release of a `ready` task's hold that a failed attempt
causes (an operator decision).

Each note also carries the account the launch spent. A node that failed the same task's launch under a second
account is the node's fault, not the account's, so that attempt is refunded (`cross_account_refund_due`,
bounded by `runner_settle.node_fault_refund_cap`).

A `node_fault` note (a merge-lane run saying its gate fence will not start here) counts as `PER_NODE`
on its own, since that fault is the node's, and it also leaves the node out for every `ready` merge-lane task
of the same project, so a new lane task is not filed straight back onto it (`excluded_node_ids`).

A merge-lane task is also kept off a node too small for the full gate (`LANE_MIN_CORES`, `LANE_MIN_MEM_KB`):
on a 2-vCPU, 8 GB hosted machine the gate starved the host until its NFS store and its daemon stalled. A node that
has reported no hardware is not left out on that account, nor is any node for a lane with no full gate.

Counted over `WINDOW`, and only since the task's last Reset or Start (`task:run`), so an operator's restart
forgets every node its earlier launches failed on, and every account they spent.

An operator's "Start fresh elsewhere" (`microvm_lifecycle.release_for_fresh_start`) leaves out the node of the
MicroVM it let go, for that `ready` task, until its next Start, claim or Reset (`_left_nodes`).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import String, case, cast, func, select
from sqlalchemy.orm import Session, aliased

from app import merge_lane_state, node_host_metrics, runner_terminations
from app.entity_shape import optional_uuid
from app.models.audit import ActivityEvent
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.node_identity import names_by_id
from app.task_artifacts import visible_artifact_clause_joined
from app.task_lifecycle import READY

#: Failed launches (unannounced or never sighted, any mix) of one task on one node before that node is left out
#: for it. Two, not one: a single failed launch is the price a relaunch already accepts; two is the node.
PER_NODE = 2

#: The causes that count as a failed launch on the note's node.
LAUNCH_FAILURE_CAUSES: tuple[str, ...] = (
    runner_terminations.CAUSE_LAUNCH_UNANNOUNCED, runner_terminations.CAUSE_LAUNCH_FAILED,
)

#: How far back a failure counts. Long enough to span a task's whole retry run, short enough that a node
#: whose disk was freed is offered the task again the next day.
WINDOW = timedelta(hours=24)

#: How far back a `node_fault` counts. Short: a node's stale fence clears once its server recycles, and the
#: exclusion reaches every lane task of the project, so one fault must not keep a node out for a day.
NODE_FAULT_WINDOW = timedelta(hours=2)

#: The smallest node a merge-lane run may be placed on: the full gate runs the whole stack and every suite at
#: once, so below this it starves the host rather than merely running slowly. Reported hardware, from
#: `node_host_metrics` (`/proc`), so a vnode counts its host's cores.
LANE_MIN_CORES = 4
LANE_MIN_MEM_KB = 15 * 1024 * 1024

#: The activity kind of the operator's Start (`events.emit_console_event(action="run")`).
START_KIND = "task:run"

#: The transition recording the node a task's "Start fresh elsewhere" left (`microvm_lifecycle`); placement keeps
#: the task off it (`excluded_node_ids`) until its next Start or claim.
START_ELSEWHERE_ACTION = "start_elsewhere"
#: The activity kind of "Start fresh elsewhere", whose `data.node` is the node it left.
_ELSEWHERE_KIND = f"task:{START_ELSEWHERE_ACTION}"

# The note field the node is recorded in (`runner_terminations.record(node=...)`), and the column reading it.
_NODE_FIELD = "node"
_NODE_KEY = ActivityEvent.data[_NODE_FIELD].astext
_ACCOUNT_KEY = ActivityEvent.data[runner_terminations.ACCOUNT_KEY].astext


def _restarted_since():
    """EXISTS: an operator's Start (`routes/tasks/dispatch.py::_dispatch_subtask_ready`, the only `task:run`
    writer) at or after the outer note, which forgets it as a Reset's watermark does."""
    started = aliased(ActivityEvent)
    return (
        select(started.id)
        .where(started.kind == START_KIND, started.data["id"].astext == ActivityEvent.data["id"].astext,
               started.created_at >= ActivityEvent.created_at)
        .exists()
    )


def _failures(session: Session, task_ids: Optional[Iterable[int]] = None):
    """`(task_id, node uuid str, count, skipping)` rows for `ready` tasks' failed launches (`LAUNCH_FAILURE_CAUSES`)
    inside `WINDOW`, since the task's last Reset or Start; a `node_fault` note weighs `PER_NODE`. `skipping`: the
    node is left out for the task now — at `PER_NODE`."""
    task_key = ActivityEvent.data["id"].astext
    cause = ActivityEvent.data["cause"].astext
    count = func.sum(case((cause == runner_terminations.CAUSE_NODE_FAULT, PER_NODE), else_=1))
    stmt = (
        select(Task.id, _NODE_KEY.label("node"), count.label("n"), (count >= PER_NODE).label("skipping"))
        .select_from(ActivityEvent)
        .join(Task, task_key == cast(Task.id, String))
        .where(
            ActivityEvent.kind == f"task_note:{runner_terminations.NOTE_ACTION}",
            cause.in_(LAUNCH_FAILURE_CAUSES)
            | ((cause == runner_terminations.CAUSE_NODE_FAULT)
               & (ActivityEvent.created_at >= func.now() - NODE_FAULT_WINDOW)),
            _NODE_KEY.isnot(None),
            ActivityEvent.created_at >= func.now() - WINDOW,
            visible_artifact_clause_joined(ActivityEvent.created_at),
            ~_restarted_since(),
            Task.state == READY,
        )
        .group_by(Task.id, _NODE_KEY)
    )
    if task_ids is not None:
        stmt = stmt.where(Task.id.in_(list(task_ids)))
    return session.execute(stmt).all()


def skipping_on(session: Session, task_id: int, node_uuid: str) -> bool:
    """Whether this node is left out for `task_id` right now (`_failures`' `skipping`; False unless `ready`)."""
    return any(node == node_uuid and skipping for _, node, _count, skipping in _failures(session, [task_id]))


def _launch_failure_notes(task_id: int, node_uuid: str, *columns):
    """`select(*columns)` over `task_id`'s failed-launch notes on this node inside `WINDOW`, since its last Reset
    or Start, whatever state the task is in now."""
    return (
        select(*columns)
        .select_from(ActivityEvent)
        .join(Task, ActivityEvent.data["id"].astext == cast(Task.id, String))
        .where(
            ActivityEvent.kind == f"task_note:{runner_terminations.NOTE_ACTION}",
            ActivityEvent.data["cause"].astext.in_(LAUNCH_FAILURE_CAUSES),
            ActivityEvent.data["id"].astext == str(task_id),
            _NODE_KEY == node_uuid,
            ActivityEvent.created_at >= func.now() - WINDOW,
            visible_artifact_clause_joined(ActivityEvent.created_at),
            ~_restarted_since(),
            Task.id == task_id,
        )
    )


def charged_since(session: Session, task_id: int, node_uuid: str, since: Optional[datetime]) -> bool:
    """Whether a failed launch of `task_id` on this node was already charged at or after `since` (the launch's
    ack): the drain then moves the task without charging that launch again. False with no `since`."""
    if since is None:
        return False
    return session.scalar(
        _launch_failure_notes(task_id, node_uuid, func.count()).where(ActivityEvent.created_at >= since)
    ) > 0


def cross_account_refund_due(session: Session, task_id: int, node_uuid: str, account: Optional[str]) -> bool:
    """Whether the failed launch about to be charged under `account` on this node earns its attempt back: this
    node already failed the task's launch under at least one OTHER account, and never under this one (inside
    `WINDOW`, since the last Reset or Start). The node failing whichever account it is given is the node's fault.
    Read BEFORE the charge's own note is written; an earlier note with no account says nothing."""
    if not account:
        return False
    earlier = set(session.scalars(
        _launch_failure_notes(task_id, node_uuid, _ACCOUNT_KEY).where(_ACCOUNT_KEY.isnot(None))
    ).all())
    return bool(earlier) and account not in earlier


def _lane_fault_notes(*columns):
    """`select(*columns)` over the `node_fault` notes a merge-lane task reported inside `NODE_FAULT_WINDOW`,
    joined to the task, whatever state that task is in now."""
    task_key = ActivityEvent.data["id"].astext
    return (
        select(*columns)
        .select_from(ActivityEvent)
        .join(Task, task_key == cast(Task.id, String))
        .where(
            ActivityEvent.kind == f"task_note:{runner_terminations.NOTE_ACTION}",
            ActivityEvent.data["cause"].astext == runner_terminations.CAUSE_NODE_FAULT,
            _NODE_KEY.isnot(None),
            ActivityEvent.created_at >= func.now() - NODE_FAULT_WINDOW,
            visible_artifact_clause_joined(ActivityEvent.created_at),
            Task.persona == merge_lane_state.LANE_PERSONA,
        )
    )


def _lane_faults(session: Session, project_id: Optional[int] = None):
    """`(project id, node uuid str, latest note time)` rows of `_lane_fault_notes`, per project and node."""
    stmt = _lane_fault_notes(Task.project_id, _NODE_KEY, func.max(ActivityEvent.created_at)).group_by(
        Task.project_id, _NODE_KEY
    )
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    return session.execute(stmt).all()


def _lane_fault_nodes(session: Session) -> dict[int, set[str]]:
    """`{project id: node uuid strs}` the project's lane is kept off (`_lane_faults`)."""
    out: dict[int, set[str]] = defaultdict(set)
    for project_id, node, _ in _lane_faults(session):
        out[int(project_id)].add(str(node))
    return out


def lane_skipped_nodes(session: Session, project_id: int) -> list[tuple[str, datetime, Optional[str]]]:
    """`[(node name, until, reason)]`, soonest first: the nodes this project's lane runs are kept off after a
    `node_fault`, when that ends — the latest fault plus `NODE_FAULT_WINDOW` (the Merge lane tab) —
    and the latest fault's own reason (`runner_terminations.DETAIL_KEY`, None when it carried none). A node
    that no longer exists is left out."""
    rows: list[tuple[UUID, datetime]] = []
    for _, node, latest in _lane_faults(session, project_id):
        node_id = optional_uuid(node)
        if node_id is not None:
            rows.append((node_id, latest))
    names = names_by_id(session, {node for node, _ in rows})
    reasons = _lane_fault_reasons(session, project_id)
    return sorted(
        ((names[node], latest + NODE_FAULT_WINDOW, reasons.get(str(node))) for node, latest in rows if node in names),
        key=lambda row: (row[1], row[0]),
    )


def _lane_fault_reasons(session: Session, project_id: int) -> dict[str, Optional[str]]:
    """`{node uuid str: reason}` — the latest `node_fault`'s reason per node (None when it carried none), under
    `_lane_faults`' own filter."""
    detail = ActivityEvent.data[runner_terminations.DETAIL_KEY].astext
    out: dict[str, Optional[str]] = {}
    for node, reason in session.execute(
        _lane_fault_notes(_NODE_KEY, detail)
        .where(Task.project_id == project_id)
        .order_by(ActivityEvent.created_at, ActivityEvent.id)
    ).all():
        out[str(node)] = reason
    return out


def node_fault_on(session: Session, task_id: int, node_uuid: str) -> bool:
    """Whether `task_id` reported a `node_fault` on this node inside `NODE_FAULT_WINDOW` (False unless `ready`).
    The one failure the drain moves a resuming task off its node for whatever its last launch did: the node
    cannot run it at all, so nothing is lost by leaving."""
    task_key = ActivityEvent.data["id"].astext
    return session.scalar(
        select(func.count())
        .select_from(ActivityEvent)
        .join(Task, task_key == cast(Task.id, String))
        .where(
            ActivityEvent.kind == f"task_note:{runner_terminations.NOTE_ACTION}",
            ActivityEvent.data["cause"].astext == runner_terminations.CAUSE_NODE_FAULT,
            ActivityEvent.data[_NODE_FIELD].astext == node_uuid,
            ActivityEvent.created_at >= func.now() - NODE_FAULT_WINDOW,
            visible_artifact_clause_joined(ActivityEvent.created_at),
            Task.id == task_id,
            Task.state == READY,
        )
    ) > 0


def _left_nodes(session: Session, task_ids: Optional[Iterable[int]] = None):
    """`(task_id, node uuid str)` for each `ready` task whose newest "Start fresh elsewhere" inside `WINDOW` still
    stands (bounded so the per-pass read never scans the whole history). It is
    written in the same transaction as that Start's own `task:run`, so both carry the same `now()`: only a
    STRICTLY later `task:run`, `task:active` (the claim) or newer such row cancels it, never its own Start. A
    Reset hides it behind the task's watermark (`visible_artifact_clause_joined`)."""
    task_key = ActivityEvent.data["id"].astext
    later = aliased(ActivityEvent)
    superseded = (
        select(later.id)
        .where(later.kind.in_((START_KIND, "task:active", _ELSEWHERE_KIND)), later.data["id"].astext == task_key,
               later.created_at > ActivityEvent.created_at)
        .exists()
    )
    stmt = (
        select(Task.id, _NODE_KEY)
        .select_from(ActivityEvent)
        .join(Task, task_key == cast(Task.id, String))
        .where(
            ActivityEvent.kind == _ELSEWHERE_KIND,
            _NODE_KEY.isnot(None),
            visible_artifact_clause_joined(ActivityEvent.created_at),
            ActivityEvent.created_at >= func.now() - WINDOW,
            ~superseded,
            Task.state == READY,
        )
    )
    if task_ids is not None:
        stmt = stmt.where(Task.id.in_(list(task_ids)))
    return session.execute(stmt).all()


def left_node_ids(session: Session, task_ids: Iterable[int]) -> dict[int, frozenset[str]]:
    """`{task id: node uuid strs}` its standing "Start fresh elsewhere" left (`_left_nodes`): re-read by admission
    under the task's lock, so a pass allocated before the release cannot land the task back there."""
    out: dict[int, set[str]] = defaultdict(set)
    for task_id, node in _left_nodes(session, task_ids):
        out[int(task_id)].add(str(node))
    return {task_id: frozenset(nodes) for task_id, nodes in out.items()}


def _too_small_for_lane(session: Session) -> set[UUID]:
    """Nodes whose reported hardware is below the lane's floor (`LANE_MIN_CORES`, `LANE_MIN_MEM_KB`). A count
    the node never reported says nothing, so it excludes nothing."""
    latest = node_host_metrics.latest_map(session, session.scalars(select(Node.id)).all())
    return {
        node for node, reading in latest.items()
        if (reading.cpu_count is not None and reading.cpu_count < LANE_MIN_CORES)
        or (reading.mem_total_kb is not None and reading.mem_total_kb < LANE_MIN_MEM_KB)
    }


class ExcludedNodes(dict):
    """`{task id: node uuid strings}` each `ready` task may not be placed on, in placement's spelling (C10: node
    names are not fleet-unique). `launch_failed` names the part of each set that is there ONLY for the task's own
    failed launches (`PER_NODE`): that part yields when no other live node may run the task
    (`routes/fleet.py::_AllowedNodeNames`), so the task is never stranded. Everything else — a lane's node fault,
    a node too small for the lane's gate, an operator's "Start fresh elsewhere" — never yields. A plain dict in
    its place is all hard."""

    def __init__(self, nodes: dict[int, frozenset[str]], launch_failed: dict[int, frozenset[str]]) -> None:
        super().__init__(nodes)
        self.launch_failed = launch_failed


def excluded_node_ids(session: Session) -> ExcludedNodes:
    """The nodes each `ready` task may no longer be placed on (`ExcludedNodes`, its yielding part named). Read once
    per drain pass and shared (`routes/fleet.py::DispatchPassFacts`). A note whose node is not a uuid, or names no node any more, is
    skipped: one bad row must not cost the pass. A `node_fault` reaching `PER_NODE` on its own is hard."""
    hard: dict[int, set[UUID]] = defaultdict(set)
    soft: dict[int, set[UUID]] = defaultdict(set)
    faulted = {
        (int(task_id), str(node)) for task_id, node in session.execute(
            _lane_fault_notes(Task.id, _NODE_KEY).where(Task.state == READY)).all()
    }
    for task_id, node, _count, skipping in _failures(session):
        node_id = optional_uuid(node) if skipping else None
        if node_id is None:
            continue
        (hard if (int(task_id), str(node)) in faulted else soft)[int(task_id)].add(node_id)
    for task_id, node in _left_nodes(session):
        node_id = optional_uuid(node)
        if node_id is not None:
            hard[int(task_id)].add(node_id)
    # A faulty node is the node's, not one task's: every `ready` lane task of a project whose lane faulted
    # there is kept off it too.
    lane_nodes = _lane_fault_nodes(session)
    if lane_nodes:
        for task_id, project_id in session.execute(
                select(Task.id, Task.project_id).where(
                    Task.state == READY, Task.persona == merge_lane_state.LANE_PERSONA,
                    Task.project_id.in_(list(lane_nodes)))):
            for node in lane_nodes[int(project_id)]:
                node_id = optional_uuid(node)
                if node_id is not None:
                    hard[int(task_id)].add(node_id)
    # Only a lane that runs a full gate needs a big node; one that merges on the PR's own checks does not.
    gated_lanes = session.scalars(
        select(Task.id).join(Project, Project.id == Task.project_id).where(
            Task.state == READY, Task.persona == merge_lane_state.LANE_PERSONA,
            func.length(func.btrim(Project.gate_full_cmd, ' \t\r\n')) > 0)).all()
    if gated_lanes:
        small = _too_small_for_lane(session)
        for task_id in gated_lanes:
            hard[int(task_id)].update(small)
    # Only nodes that still exist: a deleted node is no exclusion.
    named = {node for by_task in (hard, soft) for nodes in by_task.values() for node in nodes}
    present = {str(node) for node in session.scalars(select(Node.id).where(Node.id.in_(named)))} if named else set()

    def _present(by_task: dict[int, set[UUID]]) -> dict[int, frozenset[str]]:
        out = {task_id: frozenset(str(n) for n in nodes if str(n) in present) for task_id, nodes in by_task.items()}
        return {task_id: nodes for task_id, nodes in out.items() if nodes}

    hard_nodes, soft_nodes = _present(hard), _present(soft)
    merged = {task_id: hard_nodes.get(task_id, frozenset()) | soft_nodes.get(task_id, frozenset())
              for task_id in hard_nodes.keys() | soft_nodes.keys()}
    only_failed = {task_id: nodes - hard_nodes.get(task_id, frozenset()) for task_id, nodes in soft_nodes.items()}
    return ExcludedNodes(merged, {task_id: nodes for task_id, nodes in only_failed.items() if nodes})
