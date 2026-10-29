"""May a runner launch on this node, CPU-wise — the ONE home of that decision.

ONE RULE: a node launches no runner while its CPU is SATURATED — at least `CPU_MIN_BUCKETS` 1-minute buckets
in the last `CPU_HOLD_SECONDS`, and every one of them, read `cpu_pct` at or above `CPU_SATURATED_PCT`. A node
reporting every minute must be saturated for the whole window; a starved one that skips minutes closes sooner.

Why. On 2026-10-17 h2 ran 22 coder runners, each with its own dev stack and browser suite, at a 15-minute load
average of 328. Its daemon starved: it stopped observing its slots, its bus connection flapped, and the control
plane read 45 live runners as gone and relaunched 17 tasks into the same node every ~3 minutes, every relaunch
dying before it was sighted. A launch on a saturated node only deepens that. Memory had headroom the whole time,
so `launch_memory` never held it.

SUSTAINED, not a spike: every bucket must be saturated, so a build's burst does not close the node, and one bucket
under the line reopens it. `cpu_pct` is each bucket's MEAN, so ten saturated minutes are a node with no idle time
left, not a busy one.

Two readers, beside `launch_memory`'s pressure arm: placement leaves a saturated node out
(`routes/fleet.py::fleet_allocation`, via `ids_blocked`), and the drain declines every launch on it, a merge-lane
run included, with the reason on the task (`lease_drain._dispatch_node`, via `wait_reason`). The one exception is a
resume into the task's own held slot: it replaces the runner that slot already carried, so it adds no load.
FAILS OPEN: too few buckets in the window blocks nothing — an unreported CPU is not a busy one.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import Float, func, literal, select
from sqlalchemy.orm import Session

from app import dispatch_wait, node_disk_events
from app.models.nodes import NodeHostMetric
from app.node_identity import is_uuid_shaped, node_id_or_none

#: The bucket mean at or above which a minute counts as saturated.
CPU_SATURATED_PCT = 95.0
#: The window every bucket of which must be saturated. Ten minutes: past one gate suite's burst.
CPU_HOLD_SECONDS = 600.0
#: The fewest buckets the window must hold to judge at all. Two, not ten: a starved daemon skips minutes (h2
#: posted 19:12 and 19:17 only, both saturated), and that node is exactly the one to hold.
CPU_MIN_BUCKETS = 2


def _saturated_pcts(session: Session, node_uuids: Iterable[UUID]) -> dict[UUID, float]:
    """`{node_id: lowest cpu % in the window}` for each node saturated across the whole window. One statement,
    on the database's clock (the clock that stamped `bucket_at`). A NaN bucket is left out of the window."""
    wanted = list(node_uuids)
    if not wanted:
        return {}
    horizon = func.to_timestamp(
        func.extract("epoch", func.now()) - literal(CPU_HOLD_SECONDS, Float(precision=53))
    )
    rows = session.execute(
        select(NodeHostMetric.node_id, func.min(NodeHostMetric.cpu_pct), func.count())
        .where(
            NodeHostMetric.node_id.in_(wanted),
            NodeHostMetric.bucket_at >= horizon,
            # Bounded below `'Infinity'`: a Postgres `NaN` sorts above every number, so `min` would skip one.
            NodeHostMetric.cpu_pct < literal(math.inf, Float(precision=53)),
        )
        .group_by(NodeHostMetric.node_id)
    ).all()
    return {
        node_id: float(low) for node_id, low, buckets in rows
        if buckets >= CPU_MIN_BUCKETS and low is not None and math.isfinite(low) and low >= CPU_SATURATED_PCT
    }


def _reason(low_pct: Optional[float], *, show_figures: bool = True) -> Optional[str]:
    """The ONE sentence for one node, or None when it may launch."""
    if low_pct is None:
        return None
    return dispatch_wait.cpu_saturated(low_pct if show_figures else None, CPU_SATURATED_PCT)


def wait_reason(session: Session, node_id: str) -> Optional[str]:
    """The `dispatch_wait` sentence when this node (its uuid) may launch no runner, else None."""
    node_uuid = node_id_or_none(session, node_id)
    if node_uuid is None:
        return None
    low = _saturated_pcts(session, [node_uuid]).get(node_uuid)
    if low is not None and node_disk_events.on_foreign_host(session, node_uuid):
        # Without the figure: it is the host's, which this node's org is never told.
        return _reason(low, show_figures=False)
    return _reason(low)


def ids_blocked(session: Session, node_ids: Iterable[str]) -> frozenset[str]:
    """Which of these node uuid strings may launch no runner right now. One read for all of them. A non-uuid
    entry is never blocked (fails open, like a missing reading)."""
    ids = {node_id: UUID(node_id) for node_id in node_ids if is_uuid_shaped(node_id)}
    saturated = _saturated_pcts(session, ids.values())
    return frozenset(node_id for node_id, node_uuid in ids.items() if node_uuid in saturated)
