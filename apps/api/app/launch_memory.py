"""May a runner launch on this node, memory-wise — the ONE home of that decision.

ONE RULE, TWO INPUTS: a node launches no runner while
  * MEMORY PRESSURE: any of its 1-minute buckets in the last `MEMORY_HOLD_SECONDS` read memory at or above
    `node_host_metrics.CRITICAL_PCT`, OR
  * MEMORY CEILING: its physical machine already runs `floor(mem_total_gb / runner_memory_reservation_gb)` live
    runner sessions. A plain node is its own machine. Co-located nodes (`app/vnodes.py`) each report their
    host's `mem_total_kb`, so they share ONE ceiling, counted over every node on that host; a per-node ceiling
    would let each of N co-located nodes fill the whole host.

A merge-lane run is held by the PRESSURE arm only: the runners waiting on its merge would otherwise hold the very
ceiling that blocks it — a deadlock on a full node.

Why both exist. Placement ranks on `max(cpu, mem)` busyness, a reading that lags the launches it should
throttle: h2 (64 GB) took ~22 runner sessions in 3 minutes while it read 18%, each grew to ~3 GB (Claude, LSP
servers, its own dev stack, Chromium, gate suites), and 11 minutes later it was at 93% and the OOM killer took
its CI jobs. The ceiling is the up-front bound the reading cannot give (a launch costs its memory minutes after
it is placed); the pressure arm is the backstop for what the reservation mis-sizes. Neither implies the other,
and they answer as one: `wait_reason` names ONE reason, pressure first. `mem_pct` is each bucket's MEAN over
its window, so the pressure arm trips minutes after a spike, never on it: the ceiling is what stops a burst.

The pressure arm holds by TIME, not by a second threshold (the disk gate's `WARN_PCT` hysteresis): one bucket at
the line keeps the node closed for the whole window, so a node whose usage swings across the line with each
launch and reap does not reopen on the dip. No column holds it: the buckets are the state.

The ceiling counts the machine's live `runners` rows (the node's own `claude agents` report, the console's per-node
`runners` count, `routes/nodes/reads.py::_counts_map`) PLUS its launching runners: slots leased to a task (`state =
'task'`) that is `ready` or `active` and that no `runners` row answers yet. A launch takes a minute or more to show
up as a runner (h2: 2-12 min per answer), and the h2 burst spanned ~20 drain passes, so live rows alone
let every pass launch again. `ready` is counted, not only `active`: the task stays `ready` from the lease until the
runner itself claims it (`routes/tasks/dispatch.py::claim_task_active_ready_by_id`), which is exactly the launching window
— bounded by `LAUNCH_WINDOW_SECONDS` from the slot's `dispatched_at`, so a task queued behind its own held slot is
not a phantom launch holding the ceiling full.
The drain's own lease is written before the gate is re-read for the next decision, so a burst within one pass is
counted the same way and no separate in-pass count exists. A task's own held slot is not counted against its own
resume (`task_id`): relaunching into its reservation adds no runner. A slot held by a lane run is not counted as
launching. Co-located nodes may sit in several orgs; both readers run on the
operator plane (`seam.operator_scope`), which sees all of them.

Three readers: placement leaves a blocked node out for tasks it has not placed yet
(`routes/fleet.py::fleet_allocation`, via `ids_blocked`); the drain declines every launch on it, a held task's
resume included, with the reason on the task (`lease_drain._dispatch_node`, via `wait_reason`, after the disk
gate's `launch_disk`); and a node's CI runner pool is served `paused` under pressure only (`served_gha_state`: a
CI job is not a runner session, so the ceiling does not apply to it).

Both arms FAIL OPEN: no bucket in the window, or no `mem_total_kb` on the latest reading, blocks nothing — an
unreported memory is not evidence of a full one, as with `launch_disk`'s floor.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import Float, and_, func, literal, or_, select
from sqlalchemy.orm import Session

from app import dispatch_wait, fleet_decide, node_disk_events, node_host_metrics
from app.entity_shape import count_by
from app.operator_settings import reads as settings_reads
from app.models.nodes import Node, NodeHostMetric
from app.models.projects import Runner
from app.models.tasks import Task
from app.models.worktrees import VirtualNode, Worktree
from app.slot_state import TASK
from app.task_lifecycle import ACTIVE, READY
from app.node_identity import is_uuid_shaped, node_id_or_none

# How long one critical bucket keeps a node closed. 10 minutes: past the ~3-minute launch burst and the minutes a
# launched runner takes to grow to its footprint, so a node does not reopen before its last launches weigh in.
MEMORY_HOLD_SECONDS = 600.0
# How long a `ready` task's leased slot counts as a launching runner after `dispatched_at`: the bound the fleet already
# gives an unanswered lease (h2: 2-12 min per answer).
LAUNCH_WINDOW_SECONDS = fleet_decide.UNANSWERED_LEASE_MAX_WAIT_SECONDS

# The merge lane's persona: exempt from the ceiling (module docstring).
_LANE_PERSONAS: tuple[str, ...] = ("merge_lane",)

#: Who paused a gha pool, beside `node_disk_events.PAUSED_BY_OPERATOR`/`PAUSED_BY_DISK`.
PAUSED_BY_MEMORY = "memory"


@dataclass(frozen=True)
class _Ceiling:
    """One node's runner ceiling, the live runners on its machine, and the tasks whose runner is launching there;
    absent when the ceiling is off or unknown. `shared`: the machine is a host carrying co-located nodes, so the
    counts are over all of them."""

    runners: int
    ceiling: int
    launching: frozenset[int] = frozenset()
    shared: bool = False

    def count(self, task_id: Optional[int] = None) -> int:
        """Runners on the node, live or launching, `task_id`'s own launching slot left out."""
        return self.runners + len(self.launching - {task_id})


def _critical_pcts(session: Session, node_uuids: Iterable[UUID]) -> dict[UUID, float]:
    """`{node_id: highest memory %}` for each node with a bucket at or above `CRITICAL_PCT` in the hold window.

    One statement for all ids, on the database's clock (the clock that stamped `bucket_at`). Bounded below
    `'Infinity'`: a Postgres `NaN` sorts above every number, so an unbounded `>=` would read one as critical."""
    wanted = list(node_uuids)
    if not wanted:
        return {}
    horizon = func.to_timestamp(
        func.extract("epoch", func.now()) - literal(MEMORY_HOLD_SECONDS, Float(precision=53))
    )
    rows = session.execute(
        select(NodeHostMetric.node_id, func.max(NodeHostMetric.mem_pct))
        .where(
            NodeHostMetric.node_id.in_(wanted),
            NodeHostMetric.bucket_at >= horizon,
            NodeHostMetric.mem_pct >= literal(node_host_metrics.CRITICAL_PCT, Float(precision=53)),
            NodeHostMetric.mem_pct < literal(math.inf, Float(precision=53)),
        )
        .group_by(NodeHostMetric.node_id)
    ).all()
    return {node_id: float(pct) for node_id, pct in rows}


def _machines(session: Session, node_uuids: list[UUID]) -> dict[UUID, frozenset[UUID]]:
    """`{node_id: every live node on its host}` for each co-located node among `node_uuids` (itself included);
    a plain node is absent (its machine is itself). Two statements."""
    host_of = dict(session.execute(
        select(VirtualNode.node_id, VirtualNode.host_node_id).where(VirtualNode.node_id.in_(node_uuids))
    ).all())
    if not host_of:
        return {}
    on_host: dict[UUID, set[UUID]] = {}
    for host_uuid, node_uuid in session.execute(
        select(VirtualNode.host_node_id, VirtualNode.node_id).where(
            VirtualNode.host_node_id.in_(set(host_of.values())), VirtualNode.node_id.is_not(None),
        )
    ).all():
        on_host.setdefault(host_uuid, set()).add(node_uuid)
    return {node_uuid: frozenset(on_host.get(host_uuid, ()) | {node_uuid}) for node_uuid, host_uuid in host_of.items()}


def _ceilings(session: Session, node_uuids: Iterable[UUID], reservation_gb: float) -> dict[UUID, _Ceiling]:
    """`{node_id: _Ceiling}` for each node whose latest reading carries a positive `mem_total_kb`, counted over
    its machine (`_machines`): `floor(machine GB / reservation)` against every runner on it.

    The total is hardware, so a stale reading still sizes it (as `node_host_metrics.cores`). Empty with the
    reservation at 0 (the operator's off switch)."""
    wanted = list(node_uuids)
    if reservation_gb <= 0 or not wanted:
        return {}
    readings = node_host_metrics.latest_map(session, wanted)
    machines = _machines(session, wanted)
    counted = sorted(set(wanted).union(*machines.values()))
    runners = count_by(session, Runner.node_id, counted)
    launching = _launching(session, counted)
    out: dict[UUID, _Ceiling] = {}
    for node_uuid in wanted:
        reading = readings.get(node_uuid)
        if reading is None or reading.mem_total_kb is None or reading.mem_total_kb <= 0:
            continue
        total_gb = reading.mem_total_kb / node_host_metrics.KB_PER_GB
        machine = machines.get(node_uuid, frozenset({node_uuid}))
        out[node_uuid] = _Ceiling(runners=sum(int(runners.get(n, 0)) for n in machine),
                                  ceiling=math.floor(total_gb / reservation_gb),
                                  launching=frozenset(t for n in machine for t in launching.get(n, ())),
                                  shared=node_uuid in machines)
    return out


def _launching(session: Session, node_uuids: list[UUID]) -> dict[UUID, set[int]]:
    """`{node_id: task ids}` whose runner is launching there: a slot leased to a `ready`/`active` model task that no
    `runners` row on the node answers by the slot's sensed `session_pid`. One statement.

    A `paused`/`awaiting_response` task's slot is held with its runner stopped (a live one is a `runners` row
    already), so it is not counted; nor is a script task's (it never becomes a `runners` row). The match is by
    pid alone: nodes never send `runners.worktree_path`. A slot whose `session_pid` is not sensed yet while its
    runner already reports is counted TWICE (slot and row) until the node's next slot report — an over-count
    that holds a launch back a pass, never one that lets a burst through."""
    reported = (
        select(Runner.id)
        .where(Runner.node_id == Worktree.node_id, Runner.pid == Worktree.session_pid)
        .exists()
    )
    launch_since = func.to_timestamp(
        func.extract("epoch", func.now()) - literal(LAUNCH_WINDOW_SECONDS, Float(precision=53))
    )
    rows = session.execute(
        select(Worktree.node_id, Worktree.task_id)
        .join(Task, Task.id == Worktree.task_id)
        .where(
            Worktree.node_id.in_(node_uuids),
            Worktree.state == TASK,
            # A `ready` hold is launching only inside the window after its lease or re-dispatch: past it, the
            # task is queued behind its own slot (stopped then started, requeued), not launching, and counting it
            # would hold the machine's ceiling full with no runner on it.
            or_(Task.state == ACTIVE,
                and_(Task.state == READY, Worktree.dispatched_at.is_not(None),
                     Worktree.dispatched_at >= launch_since)),
            or_(Task.persona.is_(None), Task.persona.not_in(_LANE_PERSONAS)),
            ~reported,
        )
    ).all()
    out: dict[UUID, set[int]] = {}
    for node_uuid, task_id in rows:
        out.setdefault(node_uuid, set()).add(int(task_id))
    return out


def _reason(
    pressure_pct: Optional[float], ceiling: Optional[_Ceiling], reservation_gb: float,
    task_id: Optional[int] = None, *, show_figures: bool = True,
) -> Optional[str]:
    """The ONE sentence for one node, pressure first, or None when it may launch. `task_id` is the task about to
    launch (`_Ceiling.count`). `show_figures` False leaves the host's figures out (`on_foreign_host`)."""
    if pressure_pct is not None:
        return dispatch_wait.memory_pressure(
            pressure_pct if show_figures else None, node_host_metrics.CRITICAL_PCT, MEMORY_HOLD_SECONDS / 60.0
        )
    if ceiling is not None and ceiling.count(task_id) >= ceiling.ceiling:
        if not show_figures:
            return dispatch_wait.memory_ceiling(None, None, reservation_gb, shared=ceiling.shared)
        return dispatch_wait.memory_ceiling(ceiling.count(task_id), ceiling.ceiling, reservation_gb,
                                            shared=ceiling.shared)
    return None


def wait_reason(
    session: Session, node_id: str, *, task_id: Optional[int] = None, lane: bool = False,
) -> Optional[str]:
    """The `dispatch_wait` sentence when this node (its uuid) may launch no runner, else None. `task_id`, when
    given, is the task about to launch, whose own launching slot is not counted against it. `lane`: the launch
    is a merge-lane run, held by the pressure arm only (module docstring)."""
    node_uuid = node_id_or_none(session, node_id)
    if node_uuid is None:
        return None
    reservation_gb = settings_reads.get_float("runner_memory_reservation_gb")
    pressure = _critical_pcts(session, [node_uuid]).get(node_uuid)
    ceiling = None if lane else _ceilings(session, [node_uuid], reservation_gb).get(node_uuid)
    reason = _reason(pressure, ceiling, reservation_gb, task_id)
    if reason is not None and node_disk_events.on_foreign_host(session, node_uuid):
        # Re-rendered without the figures: they are the host's, which this node's org is never told.
        return _reason(pressure, ceiling, reservation_gb, task_id, show_figures=False)
    return reason


def ids_blocked(session: Session, node_ids: Iterable[str]) -> frozenset[str]:
    """Which of these node uuid strings may launch no runner right now. One read each for all of them.
    A non-uuid entry is never blocked (fails open, like a missing reading)."""
    reservation_gb = settings_reads.get_float("runner_memory_reservation_gb")
    ids = {node_id: UUID(node_id) for node_id in node_ids if is_uuid_shaped(node_id)}
    pressure = _critical_pcts(session, ids.values())
    ceilings = _ceilings(session, ids.values(), reservation_gb)
    return frozenset(
        node_id for node_id, node_uuid in ids.items()
        if _reason(pressure.get(node_uuid), ceilings.get(node_uuid), reservation_gb) is not None
    )


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
