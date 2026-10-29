"""The `capacity` verdict: a node that refused a launch because it already runs the most agent sessions it may.

A community-edition node enforces its own session cap independently of this control plane
(`apps/jaahd/ce_caps.py`), and says so by opening its slot-intent ack's detail with `capacity: `. That is not a
fault and not contention for a lock: the work must wait, never fail. So a `lease_slot` refused that way is
retired, its hold released, and its task returned `active → ready` with the attempt refunded
(`task_lifecycle.NODE_AT_CAPACITY`); the scheduler dispatches it again once a session ends. Any other verb refused
that way (a worktree create past the cap) only backs off, uncharged (`routes/agent/slot_intents.py`).

Only a community-edition control plane honours the marker, and only from the node the order was for; a cloud
control plane budgets it like any other refusal.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app import actors, edition, fleet_verdicts, merge_lane_state, slot_state, task_lifecycle
from app.events import emit_console_event
from app.models.tasks import Task
from app.models.worktrees import SlotIntent
from app.task_lifecycle import READY

log = logging.getLogger(__name__)

# Anchored, like the clone-busy marker: the node's text opens with it (`ce_caps.CAPACITY_MARKER`), so a name or
# git stderr later in a fault's detail can never pass for it.
_CAPACITY_REFUSAL = re.compile(r"^capacity: ")


def is_capacity_refusal(detail: Optional[str]) -> bool:
    return bool(_CAPACITY_REFUSAL.match(detail or ""))


def is_capacity_wait(detail: Optional[str]) -> bool:
    """A capacity refusal this control plane honours: only the community edition's nodes enforce a session cap, so a
    cloud control plane reads the marker as any other refusal (budgeted, parked)."""
    return edition.IS_CE and is_capacity_refusal(detail)


def returns_to_queue(row: SlotIntent, detail: Optional[str], caller_node_id: Any) -> bool:
    """Whether this ack is a launch its OWN node refused for capacity — the one refusal that requeues its task.
    `caller_node_id` None is this control plane itself."""
    return (row.verb == fleet_verdicts.VERB_LEASE_SLOT and is_capacity_wait(detail)
            and (caller_node_id is None or str(caller_node_id) == str(row.node_id)))


def return_to_queue(session: Session, row: SlotIntent) -> Optional[int]:
    """Retire `row` (a `lease_slot` refused for capacity), release its hold and put its task back to `ready` with
    the attempt refunded. Does not commit. Returns the requeued task's id, or None when there was none to move:
    the task moves only when this order's own hold was released, so a late ack for a superseded lease never
    requeues a task another launch is running."""
    payload = row.payload if isinstance(row.payload, dict) else {}
    row.outcome = fleet_verdicts.OUTCOME_SUPERSEDED
    row.next_attempt_at = None
    worktree_id = slot_state.resolve_slot(session, node_id=row.node_id, project_id=row.project_id,
                                          number=row.number)
    generation = payload.get("generation")
    released = worktree_id is not None and generation is not None and slot_state.release(
        session, worktree_id=worktree_id, generation=int(generation),
        reason="the node is running the most agent sessions it may", commit=False)
    task_id = payload.get("task_id")
    if not released or task_id is None:
        return None
    edge = task_lifecycle.NODE_AT_CAPACITY
    moved = session.scalar(
        update(Task)
        .where(Task.id == int(task_id), Task.state.in_(edge.sources))
        .values(
            state=READY,
            # Charged at `ready → active`; nothing ran, so the next dispatch must find the count where it was.
            dispatch_attempts=func.greatest(Task.dispatch_attempts - 1, 0),
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    if moved is None:
        return None
    merge_lane_state.lane_task_left_active(session, moved)
    emit_console_event(session, entity="task", id=moved, action="ready")
    log.info("node-capacity: task %s back to ready (no attempt spent) — its node refused the launch: %s",
             moved, row.reason)
    return moved
