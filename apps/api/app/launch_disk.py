"""May a runner launch on this node, disk-wise — the ONE home of that decision.

ONE RULE, TWO INPUTS: a node launches no runner while
  * its disk gate is `paused` (`nodes.disk_gate`, decided by `app/node_disk_events.py`), OR
  * its fresh reading has less free disk than `runner_launch_disk_floor_gb`.

Why both exist. The percentage gate is fleet hygiene: past `node_host_metrics.CRITICAL_PCT` it pauses the node,
stops its CI runners too, alerts the operator, and resumes on its own only under `WARN_PCT` (hysteresis), so a
node hovering at the line does not flap. The GB floor is a physical minimum one launch needs on ANY disk size:
on a 2 TB disk 90% still leaves 200 GB, on a small one 89% can leave too little to write a transcript (seen:
0.9 GB free, a runner that never announced itself and was relaunched three times). Neither implies the
other, so both are kept — but they answer as one: `wait_reason` names ONE reason, the pause first.

Two readers: placement leaves a blocked node out for tasks it has not placed yet
(`routes/fleet.py::fleet_allocation`, via `ids_blocked`), and the drain declines every launch on it, a held
task's resume included, with the reason on the task (`lease_drain._dispatch_node`, via `wait_reason`). Memory
has its own gate beside this one, read by the same two readers (`app/launch_memory.py`); disk answers first.

The floor FAILS OPEN on a missing, stale or diskless reading: it would otherwise stop EVERY runner on a node
whose metrics lag, and an unreported disk is not evidence of a full one. The gate fails open the same way at its own door: an unknown
reading never pauses a node (`node_disk_events.next_gate`); it also never resumes one.
"""
from __future__ import annotations

from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app import dispatch_wait, node_disk_events, node_host_metrics
from app.operator_settings import reads as settings_reads
from app.node_identity import is_uuid_shaped, node_id_or_none


def _free_gb_below(reading: Optional[node_host_metrics.LatestReading], floor_gb: float) -> Optional[float]:
    """The reading's free GB when the floor is on, the reading fresh and below it, else None."""
    if floor_gb <= 0:
        return None
    if reading is None or reading.stale or reading.disk_free_kb is None or reading.disk_free_kb < 0:
        return None
    free_gb = reading.disk_free_kb / node_host_metrics.KB_PER_GB
    return free_gb if free_gb < floor_gb else None


def _reason(
    gate: Optional[str], reading: Optional[node_host_metrics.LatestReading], floor_gb: float,
    *, show_pct: bool = True,
) -> Optional[str]:
    """The ONE sentence for one node, the pause first, or None when it may launch. `show_pct` False leaves
    the usage and the free figure out (a node on another org's host: they are the host's,
    `node_disk_events.on_foreign_host`)."""
    if gate == node_disk_events.GATE_PAUSED:
        pct = node_disk_events.fresh_pct(reading) if show_pct else None
        return dispatch_wait.disk_paused(pct, node_host_metrics.WARN_PCT)
    free_gb = _free_gb_below(reading, floor_gb)
    if free_gb is None:
        return None
    return dispatch_wait.low_disk(free_gb if show_pct else None, floor_gb)


def wait_reason(session: Session, node_id: str) -> Optional[str]:
    """The `dispatch_wait` sentence when this node (its uuid) may launch no runner, else None."""
    node_uuid = node_id_or_none(session, node_id)
    if node_uuid is None:
        return None
    gate = node_disk_events.gates_by_id(session, [node_uuid]).get(node_uuid, (None, None))[0]
    reading = node_host_metrics.latest_map(session, [node_uuid]).get(node_uuid)
    floor_gb = settings_reads.get_float("runner_launch_disk_floor_gb")
    # Asked only when a sentence will be written: the figures are the host's on another org's host, both arms.
    show_pct = _reason(gate, reading, floor_gb) is None or not node_disk_events.on_foreign_host(session, node_uuid)
    return _reason(gate, reading, floor_gb, show_pct=show_pct)


def ids_blocked(session: Session, node_ids: Iterable[str]) -> frozenset[str]:
    """Which of these node uuid strings may launch no runner right now. One read each for all of them.
    A non-uuid entry is never blocked (fails open, like a missing reading)."""
    floor_gb = settings_reads.get_float("runner_launch_disk_floor_gb")
    ids = {node_id: UUID(node_id) for node_id in node_ids if is_uuid_shaped(node_id)}
    gates = node_disk_events.gates_by_id(session, ids.values())
    readings = node_host_metrics.latest_map(session, ids.values())
    return frozenset(
        node_id for node_id, node_uuid in ids.items()
        if _reason(gates.get(node_uuid, (None, None))[0], readings.get(node_uuid), floor_gb) is not None
    )
