"""Where a task's runner runs, or last ran: its node and slot, by name.

The slot a task holds now answers while it holds one (`worktrees`, `state = 'task'`). A hold is released
when the run ends, so the place is also stamped on the `task_note:runner_session` row its launch writes
(`routes/agent/task_terminal.py::set_task_runner_session`, `stamp`): the latest such note answers once the slot is gone.
The node is stored as its uuid and named at read time, so a rename is a rename everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.entity_shape import optional_uuid
from app.models.audit import ActivityEvent
from app.models.nodes import Node
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.node_identity import names_by_id
from app.slot_state import TASK
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, READY

#: The `task_note:runner_session` keys the place rides in.
NODE_KEY = "node"
SLOT_KEY = "slot"
_NOTE_KIND = "task_note:runner_session"
#: The states whose held slot is where the task runs NOW (a `ready` hold is a launch on its way).
RUNNING = frozenset({READY, ACTIVE, AWAITING_RESPONSE})


@dataclass(frozen=True)
class Place:
    node: str
    slot: int
    #: True while the task runs in that slot now (`RUNNING`); False for where it last ran — a slot a stopped or
    #: failed task still holds included.
    live: bool


def stamp(session: Session, task_id: int) -> dict[str, Any]:
    """The `runner_session` note's place keys for `task_id`'s held slot, or `{}` when it holds none."""
    row = session.execute(
        select(Worktree.node_id, Worktree.number).where(Worktree.task_id == task_id, Worktree.state == TASK)
    ).first()
    return {} if row is None else {NODE_KEY: str(row.node_id), SLOT_KEY: int(row.number)}


def held(session: Session, task_ids: Iterable[int]) -> dict[int, Place]:
    """`{task id: Place}` for the ids given that hold a slot now. One query; the board's read for a `ready`
    row (a fresh lease or a slot kept from an earlier attempt), where `places`' launch-note fallback would be noise."""
    ids = sorted({int(i) for i in task_ids})
    if not ids:
        return {}
    return {
        int(task_id): Place(node=str(node), slot=int(number), live=state in RUNNING)
        for task_id, node, number, state in session.execute(
            select(Worktree.task_id, Node.name, Worktree.number, Task.state)
            .join(Node, Node.id == Worktree.node_id)
            .join(Task, Task.id == Worktree.task_id)
            .where(Worktree.task_id.in_(ids), Worktree.state == TASK)
        ).all()
    }


def places(session: Session, task_ids: Iterable[int]) -> dict[int, Place]:
    """`{task id: Place}` for the ids given: the held slot, else the latest stamped launch. Two queries; an id
    with neither, or whose node no longer exists, is absent."""
    ids = sorted({int(i) for i in task_ids})
    if not ids:
        return {}
    out = held(session, ids)
    rest = [i for i in ids if i not in out]
    if not rest:
        return out
    task_key = ActivityEvent.data["id"].astext
    # Newest first, so the first note seen per task is its latest launch.
    notes = session.execute(
        select(task_key, ActivityEvent.data[NODE_KEY].astext, ActivityEvent.data[SLOT_KEY].astext)
        .where(
            ActivityEvent.kind == _NOTE_KIND,
            task_key.in_([str(i) for i in rest]),
            ActivityEvent.data[NODE_KEY].astext.isnot(None),
        )
        .order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
    ).all()
    latest: dict[int, tuple[str, str]] = {}
    for task_id, node, slot in notes:
        latest.setdefault(int(task_id), (node, slot))
    uuids = {task_id: parsed for task_id, (node, _) in latest.items() if (parsed := optional_uuid(node)) is not None}
    names = names_by_id(session, set(uuids.values()))
    for task_id, node_uuid in uuids.items():
        slot = latest[task_id][1]
        if node_uuid in names and slot is not None and slot.isdigit():
            out[task_id] = Place(node=names[node_uuid], slot=int(slot), live=False)
    return out

