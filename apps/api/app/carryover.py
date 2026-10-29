"""The live-runner carry-over across an operator's project move.

When a project moves from org A to org B, a runner already working one of its tasks keeps running on its
org-A node until it finishes. Its task already belongs to B, so without this module every call it makes
404s: `routes/agent/auth.py::_check_token` binds the NODE's org before the task is resolved. A carry-over is the
one sanctioned exception to that, written by the transfer engine (`grant`) for each such task and bounded
per (node, task, session):

  * `scope_for` — the org a node's call about one task binds instead of the node's own. Read raw, before any
    org is bound, so it neither stamps the session nor passes through the read seam.
  * `holder_node_ids` — the carry-over node joins the task's holder set (`routes/agent/auth.py::
    _task_holder_node_ids`): the slot row stays in org A and is invisible under B.
  * `close` — called from the existing end edges only (slot release, the runner session's end, the task leaving
    the live states). It stamps `ended_at` once. The deferred erase of what the runner held rides what follows
    its death: the slot's release (`slot_released`). There is no reaper.

Secrets are not this module's: `routes/agent/task_secrets.py::_require_derived_node` refuses a carried-over caller,
except its own task's git token.

Every statement here is raw `text()` and listed in the API test suite's tenancy-bypass census. Each one runs
under whatever scope its edge has: the node's org, org B, or the operator plane."""
from __future__ import annotations

import contextvars
import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import bindparam, text

from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, PAUSED

log = logging.getLogger(__name__)

#: The states a carried-over runner may still be working in (PRD R2.3). Leaving them is an end edge, and
#: `scope_for` answers nothing for a task outside them even before that edge has stamped `ended_at`.
LIVE_STATES = (ACTIVE, AWAITING_RESPONSE, PAUSED)

#: The task this REQUEST was carried over for, set by `routes/agent/auth.py::_check_token` beside its `seam.bind`
#: and None otherwise. Every task-scoped check reads it, so a carry-over cannot widen to another task.
_REQUEST_TASK: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("carryover_task", default=None)

# A carry-over is open only while what it was granted for still holds the task on that node: the slot under
# the granted session (a redispatch re-arms it with a new one, `slot_state.redispatch`).
_STILL_HELD = """
    EXISTS (
        SELECT 1 FROM worktrees w
        WHERE w.id = c.worktree_id AND w.node_id = c.node_id AND w.task_id = c.task_id
          AND w.state = 'task' AND w.launch_session_id = c.launch_session_id
    )
"""
_LIVE_PARAMS = {"live": list(LIVE_STATES)}

_CARRIED_SLOT_SQL = text("""
    SELECT task_id FROM org_transfer_carryovers WHERE worktree_id = :worktree_id ORDER BY created_at DESC LIMIT 1
""")

_SESSION_SQL = text("""
    SELECT launch_session_id FROM org_transfer_carryovers
    WHERE node_id = :node_id AND task_id = :task_id AND ended_at IS NULL
""")

_FROM_ORG_SQL = text("""
    SELECT from_org_id FROM org_transfer_carryovers
    WHERE node_id = :node_id AND task_id = :task_id AND ended_at IS NULL
""")

_GRANT_SQL = text("""
    INSERT INTO org_transfer_carryovers
        (transfer_id, task_id, node_id, worktree_id, launch_session_id, from_org_id, to_org_id)
    VALUES
        (:transfer_id, :task_id, :node_id, :worktree_id, :launch_session_id, :from_org, :to_org)
""")

#: `close`'s selectors → the column each one matches. Exactly one per call.
_SELECTORS = ("worktree_id", "task_id", "launch_session_id")


def grant(
    session: Any,
    *,
    transfer_id: UUID,
    task_id: int,
    node_id: UUID,
    worktree_id: UUID,
    launch_session_id: Optional[str],
    from_org: UUID,
    to_org: UUID,
) -> None:
    """Record one carry-over, in the transfer engine's transaction (no commit), on the slot that holds the task
    (`slot_state.live_hold_for_task`). A second open grant for the task is refused by
    `uq_org_transfer_carryovers_open_task`."""
    if not (launch_session_id or "").strip():
        # A slot grant is bound to its session; without one it could outlive any re-arm of the slot.
        raise ValueError("a slot carry-over names the launch session it was granted for")
    session.execute(_GRANT_SQL, {
        "transfer_id": transfer_id, "task_id": int(task_id), "node_id": node_id, "worktree_id": worktree_id,
        "launch_session_id": launch_session_id, "from_org": from_org, "to_org": to_org,
    })


def scope_for(conn: Any, *, node_id: UUID, task_id: int) -> Optional[UUID]:
    """The org `node_id`'s call about `task_id` binds instead of the node's own, or None (today's behaviour).

    `conn` is a `Connection` or a `Session`; the read is raw, so it stamps nothing."""
    return conn.execute(text(f"""
        SELECT c.to_org_id FROM org_transfer_carryovers c JOIN tasks t ON t.id = c.task_id
        WHERE c.node_id = :node_id AND c.task_id = :task_id AND c.ended_at IS NULL
          AND t.state IN :live AND ({_STILL_HELD})
    """).bindparams(bindparam("live", expanding=True)),
        {"node_id": node_id, "task_id": int(task_id), **_LIVE_PARAMS}).scalar()


def from_org_for(conn: Any, *, node_id: UUID, task_id: int) -> Optional[UUID]:
    """The org an open carry-over was granted FROM — where its git connection lives (D18)."""
    return conn.execute(_FROM_ORG_SQL, {"node_id": node_id, "task_id": int(task_id)}).scalar()


def granted_session(conn: Any, *, node_id: UUID, task_id: int) -> Optional[str]:
    """The runner session an open carry-over was granted for: the only session whose own records (its
    transcript) a carried-over call may reach (`routes/agent/auth.py::_carries`)."""
    return conn.execute(_SESSION_SQL, {"node_id": node_id, "task_id": int(task_id)}).scalar()


def holder_node_ids(conn: Any, task_id: int) -> set[UUID]:
    """The nodes holding `task_id` through an open carry-over."""
    return set(conn.execute(text(f"""
        SELECT c.node_id FROM org_transfer_carryovers c JOIN tasks t ON t.id = c.task_id
        WHERE c.task_id = :task_id AND c.ended_at IS NULL AND t.state IN :live AND ({_STILL_HELD})
    """).bindparams(bindparam("live", expanding=True)),
        {"task_id": int(task_id), **_LIVE_PARAMS}).scalars().all())


def bind_request(task_id: Optional[int]) -> None:
    """Record which task this request was carried over for (None: it was not). `_check_token` only."""
    _REQUEST_TASK.set(task_id)


def request_task() -> Optional[int]:
    """The task this request was carried over for, or None."""
    return _REQUEST_TASK.get()


def close(
    session: Any,
    *,
    worktree_id: Optional[UUID] = None,
    task_id: Optional[int] = None,
    launch_session_id: Optional[str] = None,
) -> bool:
    """End the open carry-over an end edge names, in the edge's transaction (no commit): stamp `ended_at` once.
    Exactly one selector: `slot_state.release` (`worktree_id`), the runner session's end (`launch_session_id`), the task leaving the live states (`task_id`). A no-op when
    nothing open matches, so the second edge to fire stamps nothing. True when one closed.

    The deferred erase is not written here, because the first edge to fire can precede the runner's death (a
    settle, a session end): it rides what always follows a runner's death. A slot's tree is ordered erased by
    its release (`slot_released`), which comes after the kill."""
    given = {name: value for name, value in (
        ("worktree_id", worktree_id), ("task_id", task_id), ("launch_session_id", launch_session_id),
    ) if value is not None}
    if len(given) != 1:
        raise ValueError(f"close() takes exactly one of {_SELECTORS}, got {sorted(given)}")
    (column, value), = given.items()
    if column == "launch_session_id" and not str(value).strip():
        return False
    # `column` is one of the three literal names above, never a caller's string.
    closed = session.execute(text(
        f"UPDATE org_transfer_carryovers SET ended_at = now() "
        f"WHERE {column} = :value AND ended_at IS NULL "
        f"RETURNING id, task_id, node_id"
    ), {"value": value}).all()
    for row in closed:
        log.info("carry-over %s closed (task %s, node %s) on its %s end edge", row.id, row.task_id, row.node_id,
                 column)
    return bool(closed)


def slot_released(session: Any, worktree_id: UUID) -> None:
    """`slot_state.release` freed `worktree_id` (no commit): close any open carry-over on it, and when a
    carry-over names it, order its tree erased — exactly once, since a slot whose project left its node's org is
    never leased again (the node's roster no longer names the project)."""
    close(session, worktree_id=worktree_id)
    task_id = session.execute(_CARRIED_SLOT_SQL, {"worktree_id": worktree_id}).scalar()
    if task_id is not None:
        _erase_slot(session, worktree_id, task_id)


def carried_project_ids(conn: Any, node: str) -> dict[int, str]:
    """`{project id: name}` for the projects node `node` (its uuid) runs a carried-over slot of. A moved project
    is not in the node's org, so without this `fleet_observe.resolve_project_ids` would skip the carried slot
    and its runner would read as lost. Keyed by id: names are unique per org only, so two carried projects can
    share one; the name is for an older daemon that reports a slot by name."""
    rows = conn.execute(text("""
        SELECT p.id, p.name FROM org_transfer_carryovers c
        JOIN worktrees w ON w.id = c.worktree_id JOIN projects p ON p.id = w.project_id
        WHERE c.ended_at IS NULL AND c.node_id::text = :node
    """), {"node": node}).all()
    return {row.id: row.name for row in rows}


def _erase_slot(session: Any, worktree_id: UUID, task_id: int) -> None:
    """Order the carried slot's tree erased on its node. A slot already gone (deleted by an operator, or its
    node deregistered) has nothing left to erase."""
    # Deferred: `slot_destroy` imports `slot_state`, whose `release` calls `close`.
    from app import slot_destroy  # noqa: PLC0415
    from app.node_clone import clone_for_slot_path, resolve_clone  # noqa: PLC0415

    # The slot, its project and its node in one statement: the project is in the new org and the slot and node
    # in the old one, so no scope sees all three.
    slot = session.execute(text("""
        SELECT w.node_id, w.project_id, w.number, w.path, w.has_repo, p.name AS project, n.jaah_home
        FROM worktrees w JOIN projects p ON p.id = w.project_id JOIN nodes n ON n.id = w.node_id
        WHERE w.id = :worktree_id
    """), {"worktree_id": worktree_id}).first()
    if slot is None:
        log.info("carry-over of task %s: slot %s is already gone — nothing to erase", task_id, worktree_id)
        return
    clone = clone_for_slot_path(slot.jaah_home, slot.path, slot.number) or resolve_clone(
        slot.jaah_home, slot.project_id
    )
    if not clone:
        # Re-served forever otherwise: the node refuses an erase it cannot place under its own home.
        log.warning("carry-over of task %s: where slot %s lives on its node is not known — tree left in place",
                    task_id, worktree_id)
        return
    slot_destroy.enqueue_erase(
        session,
        node_id=slot.node_id,
        project_id=slot.project_id,
        project=slot.project,
        number=slot.number,
        clone_path=clone,
        has_repo=slot.has_repo,
        worktree_id=worktree_id,
        label=f"task {task_id}'s carried-over slot {slot.project} #{slot.number}",
    )
