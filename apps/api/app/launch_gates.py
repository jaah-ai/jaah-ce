"""May a runner launch on this node — the ONE home of the launch gates: disk, memory, pressure, tasks and CPU.

ONE SHAPE, FIVE GATES. Each gate judges a set of nodes in one read (`_HELD[gate]`: `{node uuid: figure}` for the
nodes it holds) and writes one sentence for a held node (`_SENTENCE[gate]`). The two readers chain them in
`GATES` order, disk first: placement leaves a held node out (`routes/fleet.py::fleet_allocation`, via
`ids_blocked`), and the drain declines every launch on it, a merge-lane run included, with the first gate's
reason on the task (`lease_drain._dispatch_node`, via `wait_reason`). A node's CI runner pool is served
`paused` under memory pressure (`served_gha_state`). On another org's host the figures are the host's, which
this node's org is never told (`node_disk_events.on_foreign_host`), so the sentence leaves them out.

DISK, two inputs, one answer: the node's disk gate is `paused` (`nodes.disk_gate`, decided by
`app/node_disk_events.py`: fleet hygiene past `node_host_metrics.CRITICAL_PCT`, resuming under `WARN_PCT`), OR its
fresh reading has less free disk than `runner_launch_disk_floor_gb`, the physical minimum one launch needs on ANY
disk size (seen: 0.9 GB free, a runner that never announced itself and was relaunched three times). The pause
is named first; it also stops the node's CI runners and alerts the operator (`node_disk_events`).

MEMORY: any 1-minute bucket in the last `MEMORY_HOLD_SECONDS` read memory at or above the node's line:
`node_host_metrics.CRITICAL_PCT`, or `VNODE_CRITICAL_PCT` for a virtual node housed directly on its host
(`virtual_nodes.built_housing = 'host'`). Such a node's figure is the host's, and every host-housed vnode shares
`jaahvnodes.slice`, which throttles at MemoryHigh=80% and kills at MemoryMax=85% of host RAM (page cache charged
too), so 90% is never reached: a vnode choked at 84% with launches still landing. It reads
what the machine really uses, never a per-runner estimate (a flat reservation, since removed, held a 7.6 GB host
at two runners while it read 26%). It holds by TIME, not a second threshold: one bucket at the line keeps the node
closed for the whole window, so usage swinging across the line with each launch and reap does not reopen it on
the dip. The buckets are the state. `mem_pct` is each bucket's MEAN over its window, so the gate trips minutes
after a spike, never on it; runners waiting on a merge-lane run that keep a node critical hold it there.

PRESSURE: at least `PRESSURE_MIN_BUCKETS` buckets in the last `PRESSURE_HOLD_SECONDS`, every one with
greatest(`psi_mem_pct`, `psi_io_pct`) at or above `PRESSURE_PCT` — the share of time tasks stalled on memory or
disk. Sustained, like CPU. A vnode read disk PSI ~98% and load 150 on 8 cores while memory looked survivable,
and a release launched into it at psi_io 57-75%. A bucket with neither figure is unknown: the window
then fails open.

TASKS: the tenant's own cap, `nodes.max_running_tasks` (NULL = none): held while the node's `state='task'` slots
carry that many tasks in `task_lifecycle.RUNNER_RUNNING_STATES`, or `ready` with a launch in flight
(`account_runners.launch_in_flight`: the runner has not claimed it yet). A stopped task's held slot, or a ready one
with nothing launched, runs nothing, so it does not count. The drain re-reads the gates after each launch it
issues, so one pass does not overshoot the cap, and lets a counted task relaunch into its own slot (it would
otherwise wait on itself).

CPU: SATURATED — at least `CPU_MIN_BUCKETS` buckets in the last `CPU_HOLD_SECONDS`, every one at or above
`CPU_SATURATED_PCT`. On 2026-10-17 h2 ran 22 coder runners at a 15-minute load average of 328 with memory to
spare: its daemon starved, the control plane read 45 live runners as gone and relaunched 17 tasks into the same
node every ~3 minutes, each dying before it was sighted. Sustained, not a spike: one bucket under the line reopens
it. The one exception (CPU only) is a resume into the task's own held slot, which replaces the runner that slot already
carried (the drain's, not this module's).

Every gate FAILS OPEN: a missing, stale, diskless or too-thin reading blocks nothing — an unreported resource is
not evidence of an exhausted one; blocking on it would stop EVERY runner on a node whose metrics lag.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Optional
from uuid import UUID

from sqlalchemy import Float, and_, case, column, func, literal, or_, select, table
from sqlalchemy.orm import Session

from app import account_runners, dispatch_wait, node_disk_events, node_host_metrics
from app.models.nodes import Node, NodeHostMetric
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.node_identity import node_id_or_none
from app.operator_settings import reads as settings_reads
from app.slot_state import TASK as SLOT_TASK
from app.task_lifecycle import READY, RUNNER_RUNNING_STATES

#: The bucket mean at or above which a minute counts as saturated.
CPU_SATURATED_PCT = 95.0
#: The window every bucket of which must be saturated. Ten minutes: past one gate suite's burst.
CPU_HOLD_SECONDS = 600.0
#: The fewest buckets the window must hold to judge at all. Two, not ten: a starved daemon skips minutes (h2
#: posted 19:12 and 19:17 only, both saturated), and that node is exactly the one to hold.
CPU_MIN_BUCKETS = 2
# How long one critical bucket keeps a node closed. 10 minutes: past the ~3-minute launch burst and the minutes a
# launched runner takes to grow to its footprint, so a node does not reopen before its last launches weigh in.
MEMORY_HOLD_SECONDS = 600.0
#: The memory line of a virtual node housed on its host: under its slice's MemoryHigh=80% (module docstring).
VNODE_CRITICAL_PCT = 75.0
#: The stall share (PSI some avg, memory or io) at or above which a minute counts as under pressure.
PRESSURE_PCT = 40.0
#: The window every bucket of which must be under pressure, and the fewest buckets it must hold (as CPU's).
PRESSURE_HOLD_SECONDS = 600.0
PRESSURE_MIN_BUCKETS = 2
#: Who paused a gha pool, beside `node_disk_events.PAUSED_BY_OPERATOR`/`PAUSED_BY_DISK`.
PAUSED_BY_MEMORY = "memory"

DISK, MEMORY, PRESSURE, TASKS, CPU = "disk", "memory", "pressure", "tasks", "cpu"
#: The drain's order: the first gate that holds a node names its wait.
GATES = (DISK, MEMORY, PRESSURE, TASKS, CPU)

_INF = literal(math.inf, Float(precision=53))
# The housing fact alone, unmapped: the row is the HOST's org (`VirtualNode`), a fact about the machine the
# node's own reader must still judge by, so the ORM read seam must not narrow it away (as `vnodes.hosting_facts`).
_VNODES = table("virtual_nodes", column("node_id"), column("built_housing"))


def _free_gb_below(reading: Optional[node_host_metrics.LatestReading], floor_gb: float) -> Optional[float]:
    """The reading's free GB when the floor is on, the reading fresh and below it, else None."""
    if floor_gb <= 0 or reading is None or reading.stale or reading.disk_free_kb is None or reading.disk_free_kb < 0:
        return None
    free_gb = reading.disk_free_kb / node_host_metrics.KB_PER_GB
    return free_gb if free_gb < floor_gb else None


def _disk_held(session: Session, node_uuids: Iterable[UUID]) -> dict[UUID, tuple]:
    """`{node_id: (sentence, figure, line)}` for each node disk-paused (first) or below the floor."""
    wanted = list(node_uuids)
    floor_gb = settings_reads.get_float("runner_launch_disk_floor_gb")
    gates = node_disk_events.gates_by_id(session, wanted)
    readings = node_host_metrics.latest_map(session, wanted)
    held: dict[UUID, tuple] = {}
    for node_uuid in wanted:
        reading = readings.get(node_uuid)
        if gates.get(node_uuid, (None, None))[0] == node_disk_events.GATE_PAUSED:
            held[node_uuid] = (dispatch_wait.disk_paused, node_disk_events.fresh_pct(reading),
                               node_host_metrics.WARN_PCT)
        elif (free_gb := _free_gb_below(reading, floor_gb)) is not None:
            held[node_uuid] = (dispatch_wait.low_disk, free_gb, floor_gb)
    return held


def _critical_pcts(session: Session, node_uuids: Iterable[UUID]) -> dict[UUID, tuple[float, float]]:
    """`{node_id: (highest memory %, the line it met)}` for each node with a bucket at or above its line in the
    hold window: `VNODE_CRITICAL_PCT` for a host-housed vnode, else `CRITICAL_PCT`.

    One statement for all ids, on the database's clock (the clock that stamped `bucket_at`). Bounded below
    `'Infinity'`: a Postgres `NaN` sorts above every number, so an unbounded `>=` would read one as critical."""
    wanted = list(node_uuids)
    if not wanted:
        return {}
    line = case(
        (_VNODES.c.node_id.is_not(None), literal(VNODE_CRITICAL_PCT, Float(precision=53))),
        else_=literal(node_host_metrics.CRITICAL_PCT, Float(precision=53)),
    )
    rows = session.execute(
        select(NodeHostMetric.node_id, func.max(NodeHostMetric.mem_pct), line)
        .select_from(NodeHostMetric)
        .outerjoin(_VNODES, and_(_VNODES.c.node_id == NodeHostMetric.node_id, _VNODES.c.built_housing == "host"))
        .where(
            NodeHostMetric.node_id.in_(wanted),
            NodeHostMetric.bucket_at >= node_host_metrics.db_now_minus(MEMORY_HOLD_SECONDS),
            NodeHostMetric.mem_pct >= line,
            NodeHostMetric.mem_pct < _INF,
        )
        .group_by(NodeHostMetric.node_id, line)
    ).all()
    return {node_id: (float(pct), float(at)) for node_id, pct, at in rows}


def _pressure_pcts(session: Session, node_uuids: Iterable[UUID]) -> dict[UUID, float]:
    """`{node_id: lowest stall % in the window}` for each node under pressure across the whole window. One
    statement, on the database's clock. A bucket whose stall is NULL (neither figure reported) or NaN is unknown
    and opens the window: it is counted in the buckets but not among the known ones."""
    wanted = list(node_uuids)
    if not wanted:
        return {}
    stall = func.greatest(NodeHostMetric.psi_mem_pct, NodeHostMetric.psi_io_pct)
    known = case((stall < _INF, stall), else_=None)
    rows = session.execute(
        select(NodeHostMetric.node_id, func.min(known), func.count(known), func.count())
        .where(
            NodeHostMetric.node_id.in_(wanted),
            NodeHostMetric.bucket_at >= node_host_metrics.db_now_minus(PRESSURE_HOLD_SECONDS),
        )
        .group_by(NodeHostMetric.node_id)
    ).all()
    return {
        node_id: float(low) for node_id, low, known_buckets, buckets in rows
        if buckets >= PRESSURE_MIN_BUCKETS and known_buckets == buckets and low is not None and low >= PRESSURE_PCT
    }


def _tasks_at_cap(session: Session, node_uuids: Iterable[UUID]) -> dict[UUID, tuple[str, int]]:
    """`{node_id: (node name, its cap)}` for each node whose running tasks (module docstring) reached
    `nodes.max_running_tasks`. One statement; a node with no cap is never read as held."""
    wanted = list(node_uuids)
    if not wanted:
        return {}
    running = func.count(Worktree.id)
    rows = session.execute(
        select(Node.id, Node.name, Node.max_running_tasks)
        .join(Worktree, and_(Worktree.node_id == Node.id, Worktree.state == SLOT_TASK))
        .join(Task, and_(Task.id == Worktree.task_id, or_(
            Task.state.in_(RUNNER_RUNNING_STATES),
            and_(Task.state == READY, account_runners.launch_in_flight()),
        )))
        .where(Node.id.in_(wanted), Node.max_running_tasks.is_not(None))
        .group_by(Node.id, Node.name, Node.max_running_tasks)
        .having(running >= Node.max_running_tasks)
    ).all()
    return {node_id: (name, int(cap)) for node_id, name, cap in rows}


def _saturated_pcts(session: Session, node_uuids: Iterable[UUID]) -> dict[UUID, float]:
    """`{node_id: lowest cpu % in the window}` for each node saturated across the whole window. One statement,
    on the database's clock. A NaN bucket is left out of the window (bounded below `'Infinity'`, as above)."""
    wanted = list(node_uuids)
    if not wanted:
        return {}
    rows = session.execute(
        select(NodeHostMetric.node_id, func.min(NodeHostMetric.cpu_pct), func.count())
        .where(
            NodeHostMetric.node_id.in_(wanted),
            NodeHostMetric.bucket_at >= node_host_metrics.db_now_minus(CPU_HOLD_SECONDS),
            NodeHostMetric.cpu_pct < _INF,
        )
        .group_by(NodeHostMetric.node_id)
    ).all()
    return {
        node_id: float(low) for node_id, low, buckets in rows
        if buckets >= CPU_MIN_BUCKETS and low is not None and math.isfinite(low) and low >= CPU_SATURATED_PCT
    }


def _disk_sentence(session: Session, node_uuid: UUID, held: tuple, show: bool) -> str:
    sentence, figure, line = held
    return sentence(figure if show else None, line)


def _memory_sentence(session: Session, node_uuid: UUID, held: tuple[float, float], show: bool) -> str:
    """The window's peak against the line that applied, and the machine's free memory now from the node's latest
    reading (a co-located node reports its host's) when it carries a total."""
    pressure, line = held
    hold_minutes = MEMORY_HOLD_SECONDS / 60.0
    if not show:
        return dispatch_wait.memory_pressure(None, line, hold_minutes)
    free_gb = total_gb = None
    reading = node_host_metrics.latest_map(session, [node_uuid]).get(node_uuid)
    if reading is not None and reading.mem_total_kb and reading.mem_total_kb > 0 and math.isfinite(reading.mem_pct):
        total_gb = reading.mem_total_kb / node_host_metrics.KB_PER_GB
        free_gb = total_gb * (100.0 - reading.mem_pct) / 100.0
    return dispatch_wait.memory_pressure(pressure, line, hold_minutes, free_gb=free_gb, total_gb=total_gb)


def _pressure_sentence(session: Session, node_uuid: UUID, low_pct: float, show: bool) -> str:
    return dispatch_wait.node_stalled(low_pct if show else None)


def _tasks_sentence(session: Session, node_uuid: UUID, held: tuple[str, int], show: bool) -> str:
    # The cap is the node's own org's setting, so it is shown on any host.
    name, cap = held
    return dispatch_wait.node_task_limit(name, cap)


def _cpu_sentence(session: Session, node_uuid: UUID, low_pct: float, show: bool) -> str:
    return dispatch_wait.cpu_saturated(low_pct if show else None, CPU_SATURATED_PCT)


#: One read judging many nodes → `{uuid: figure}`; the figure's shape is the gate's own (`_SENTENCE` reads it).
_HELD: dict[str, Callable[[Session, Iterable[UUID]], dict[UUID, Any]]] = {
    DISK: _disk_held, MEMORY: _critical_pcts, PRESSURE: _pressure_pcts, TASKS: _tasks_at_cap, CPU: _saturated_pcts,
}
_SENTENCE: dict[str, Callable[[Session, UUID, Any, bool], str]] = {
    DISK: _disk_sentence, MEMORY: _memory_sentence, PRESSURE: _pressure_sentence, TASKS: _tasks_sentence,
    CPU: _cpu_sentence,
}


def wait_reason(session: Session, node_id: str, gates: Iterable[str] = GATES) -> Optional[str]:
    """The `dispatch_wait` sentence of the first of `gates` holding this node (its uuid), else None."""
    node_uuid = node_id_or_none(session, node_id)
    if node_uuid is None:
        return None
    for gate in gates:
        held = _HELD[gate](session, [node_uuid]).get(node_uuid)
        if held is not None:
            # Asked only when a sentence will be written: the figures are the host's on another org's host.
            return _SENTENCE[gate](session, node_uuid, held, not node_disk_events.on_foreign_host(session, node_uuid))
    return None


def ids_blocked(session: Session, node_ids: Iterable[str], gates: Iterable[str] = GATES) -> frozenset[str]:
    """Which of these node uuid strings any of `gates` holds right now (`node_host_metrics.blocked_node_ids`:
    one read per gate for all of them; a non-uuid entry is never blocked)."""
    ids = list(node_ids)
    return frozenset().union(*(node_host_metrics.blocked_node_ids(session, ids, _HELD[gate]) for gate in gates))


def served_gha_state(
    session: Session, node: Optional[Node], desired_state: str, disk_gate: Optional[str],
) -> tuple[str, Optional[str]]:
    """`(state the node is served, paused_by)` for a gha pool: `node_disk_events.served_gha_state`, and a pool it
    leaves `running` is served `paused` by memory while the pressure arm holds. Never written to
    `desired_state`, so it lifts on its own when the window clears; the operator's and the disk's pause win."""
    state, paused_by = node_disk_events.served_gha_state(desired_state, disk_gate)
    if state == "running" and node is not None and node.id in _critical_pcts(session, [node.id]):
        return "paused", PAUSED_BY_MEMORY
    return state, paused_by
