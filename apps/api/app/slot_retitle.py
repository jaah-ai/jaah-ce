"""After a task's title changes: re-stamp its slot's stored label and relabel its live tmux window.

The window name and `worktrees.name` are composed once, at dispatch/lease, and nothing re-reads them;
the async LLM title refinement makes a later title change the common case. This closes that race after
the fact rather than preventing it.

No slot row is the ordinary case (next dispatch composes from the current row). No live window is
answered by the node: `rename_tmux` exits 0 with "nothing to rename". So nothing is probed.

Best effort and never raises: both callers (the DBOS refinement step, the operator's committed edit)
must not fail over a cosmetic label. Failures are logged.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app import slot_mutate, slot_title
from app.entity_shape import SYSTEM_ACTOR_ID
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import Worktree
from app.routes.commands import dispatch_and_audit_sync

log = logging.getLogger("cockpit-slot-retitle")

# The `command_audit_log.actor`. No operator is behind this call (one caller has no principal, the other
# only triggers it), so a `system:<what>` actor rather than a human's email.
_ACTOR = "system:title"


def retitle_task_slot(session, task_id: int) -> None:
    """Re-stamp every slot bound to `task_id` and relabel its live window. Best effort; never raises.

    Two passes: the `name` re-stamp is a correctness write committed first, releasing row locks before
    pass 2 holds a bus round trip per slot (which would stall the node's observation upsert).

    Per node, from each slot's own row — never `resolve_node_id()`."""
    try:
        _retitle_task_slot(session, task_id)
    except Exception:  # noqa: BLE001 — the whole point: see the module docstring's contract paragraph.
        # Rolled back, not merely logged: both callers keep using this session.
        session.rollback()
        log.exception("slot retitle: task %s — the slot label and its live window were left as they were",
                      task_id)


def _retitle_task_slot(session, task_id: int) -> None:
    """The body, free to raise — `retitle_task_slot` is the seam that makes the contract true."""
    slots = session.execute(
        select(
            # The node uuid addresses the bus and `apply_mutation`; names are for logs and the node's
            # window composition only (names repeat across orgs).
            Worktree.node_id,
            Project.name.label("project_name"),
            Worktree.project_id,
            # `default_title` lets `slot_title.operator_named` tell a derived label from the operator's.
            Worktree.name.label("slot_name"), Worktree.default_title,
            Worktree.number, Worktree.path,
            Task.state.label("task_state"), Task.title.label("task_title"),
        )
        .select_from(Worktree)
        .join(Node, Node.id == Worktree.node_id)
        .join(Project, Project.id == Worktree.project_id)
        # LEFT: `worktrees.task_id` has no FK, so a deleted task yields NULL (handled by the state guard).
        .outerjoin(Task, Task.id == Worktree.task_id)
        .where(Worktree.task_id == task_id)
        .order_by(Node.name, Worktree.number)
    ).all()
    if not slots:
        # The ordinary case (never dispatched); not logged.
        return

    # ── PASS 1: the stored label follows the title, durably ──
    # Collects the composed Title for pass 2 so the node receives exactly what landed on the row.
    pending: list[tuple[str, dict[str, object]]] = []
    restamped = False
    for slot in slots:
        if not slot_title.task_active_from_state(slot.task_state):
            # `resolved_title` answers `free` for an inactive task; only release may write `free`.
            log.info("slot retitle: task %s slot %s/%s skipped — task state %r is not active",
                     task_id, slot.project_name, slot.number, slot.task_state)
            continue
        if slot_title.operator_named(
            slot.slot_name,
            default_title=slot.default_title,
            default_name=slot_title.default_name(slot.project_name, slot.number),
        ):
            # Operator-named slots outrank a title change (same predicate as rule 3). Our own `{id}|…`
            # composite is not operator-named, so it is re-stamped.
            log.info("slot retitle: task %s slot %s/%s skipped — the operator named it",
                     task_id, slot.project_name, slot.number)
            continue
        if not (slot.task_title or "").strip():
            # No title: `resolved_title` would fall through to `free`, which must never land on an
            # active slot. Rule 3 leaves the label alone here too.
            log.info("slot retitle: task %s slot %s/%s skipped — the task has no title to compose with",
                     task_id, slot.project_name, slot.number)
            continue
        # Composed in its one home (`stored=None` → the composite branch), never spelled out here.
        title = slot_title.resolved_title(
            None,
            task_active=True,
            task_id=task_id,
            task_title=slot.task_title,
        )
        reason = slot_title.invalid_title_reason(title)
        if reason is not None:
            # Same validation as the other producers; skipped, since there is no operator to 422 and the
            # node would refuse it anyway.
            log.warning("slot retitle: task %s slot %s/%s skipped — %s",
                        task_id, slot.project_name, slot.number, reason)
            continue
        touched = slot_mutate.apply_mutation(
            session, str(slot.node_id), slot.project_id, slot.number,
            # The system actor: nobody deliberately wrote this label.
            actor_id=SYSTEM_ACTOR_ID, name=title,
        )
        if not touched:
            # Row vanished between SELECT and UPDATE; no window to relabel either.
            log.info("slot retitle: task %s slot %s/%s vanished before the re-stamp",
                     task_id, slot.project_name, slot.number)
            continue
        restamped = True
        if not slot.path:
            # No path means no window (and `rename_tmux` would refuse); pass 1 already fixed the label.
            continue
        pending.append((
            str(slot.node_id),
            # `rename_tmux`'s pinned contract `{project, number, path, title}`; the node composes the
            # window name from the resolved title.
            {"project": slot.project_name, "number": slot.number, "path": slot.path, "title": title},
        ))
    # Guarded so a no-op call does not commit the caller's in-flight work.
    if restamped:
        session.commit()

    # ── PASS 2: relabel the live window, best effort, per the slot's own node ──
    # `dispatch_and_audit_sync` swallows and audits bus refusals.
    for node_id, args in pending:
        dispatch_and_audit_sync(session, node_id, "rename_tmux", args, _ACTOR)
