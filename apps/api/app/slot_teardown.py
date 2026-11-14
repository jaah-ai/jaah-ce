"""Ending what is inside a pool slot, for the paths about to destroy the slot row itself: slot Delete
(`routes/pool_slots.py`), node deregistration (`routes/nodes/delete.py`), scale-in (`node_group_reconcile.py`).

Deleting the row stops neither the remote runner nor its session-ledger entry. `end_holder` closes the
session as `abandoned` (never `normal`) and mints a `release_slot` intent; the kill rides
`erase_tree_on_node`. The `tasks` row is never touched (the task is re-dispatched). The node side is
best effort and never raises, so a dead node cannot hold an operator's Delete hostage.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from app import agent_session_lifecycle, agent_sessions, fleet_verdicts, slot_state
from app.models.worktrees import Worktree

log = logging.getLogger("cockpit-slot-teardown")

# How long a best-effort node erase may hold an operator's Delete: well below `COMMAND_TIMEOUT` (180s)
# because the answer is advisory (the row goes either way). 45s covers `stack_down_cmd` plus `rm -rf`
# on a healthy node with margin.
NODE_ERASE_TIMEOUT = 45.0


@dataclass(frozen=True)
class HolderSnapshot:
    """Everything the teardown needs about a hold, read off the locked row before it is changed.

    A snapshot because both callers mutate first (delete, or `slot_state.release` blanking the runner
    columns), after which an ORM read is stale or empty. These values are the teardown's only address."""

    state: str
    node_id: Optional[UUID]
    project_id: Optional[int]
    number: int
    task_id: Optional[int]
    session_id: Optional[str]
    pid: Optional[int]


def snapshot(row: Worktree) -> HolderSnapshot:
    """Take the pre-mutation snapshot off a locked `worktrees` row."""
    return HolderSnapshot(
        state=row.state,
        node_id=row.node_id,
        project_id=row.project_id,
        number=row.number,
        task_id=row.task_id,
        session_id=row.launch_session_id,
        pid=row.pid,
    )


def describe_holder(snap: HolderSnapshot) -> Optional[str]:
    """What is holding this slot, in one phrase for a report — or None when nothing is.

    So a Delete that ended a live runner does not read like one that removed an empty directory."""
    if snap.state == slot_state.FREE:
        return None
    if snap.state == slot_state.TASK:
        return f"task {snap.task_id}" if snap.task_id is not None else "a fleet task"
    # Any other state (a legacy row) is reported verbatim rather than as free.
    return f"state {snap.state!r}"


def end_holder(
    session,
    snap: HolderSnapshot,
    *,
    project: str,
    reason: str,
) -> list[fleet_verdicts.IssuedIntents]:
    """End the runner (or session) inside one slot: the session ledger's close plus one durable
    `release_slot` intent. Returns the issued batches, which the caller pushes after its own commit.

    `commit=False` throughout so a rolled-back delete leaves no durable sweep order behind.

    Neither `kill_runner` nor `release_slot` can be enacted on a delete: the node refuses verbs whose
    slot is missing from its roster (built from `worktrees` rows), and the tree is already gone. So the
    kill rides the erase: `worktree_erase` kills the tree's processes and tmux windows first
    (`jaahd/worktree_ops.py::_clear_the_tree_before_it_goes`). The intent is still minted as the
    durable record, and it is the enacting path on release, where the row survives.

    A nodeless slot mints nothing (logged). Residual: a pending `release_slot` matches by `(project,
    number)` alone, so a re-declared slot could be swept; `VERB_RELEASE_SLOT` is superseding.
    """
    if snap.state == slot_state.FREE:
        return []
    # The ledger first and unconditionally: it depends on no node. `note_end` is savepoint-wrapped and
    # swallows its own failures.
    agent_session_lifecycle.note_end(
        session,
        agent_session_id=snap.session_id or "",
        end_reason=agent_sessions.END_REASON_ABANDONED,
    )
    if snap.node_id is None or snap.project_id is None:
        log.warning(
            "slot teardown %s #%s: the hold names %s but the slot carries no node — no teardown intent "
            "minted, so its runner (session %s, pid %s) is not addressable",
            project, snap.number, describe_holder(snap), snap.session_id or "?",
            snap.pid if snap.pid is not None else "?",
        )
        return []
    issued = fleet_verdicts.issue_intents(
        session,
        str(snap.node_id),
        [
            fleet_verdicts.build_slot_intent(
                fleet_verdicts.VERB_RELEASE_SLOT,
                int(snap.project_id),
                project,
                snap.number,
                reason=reason,
                # `lease_drain._release`'s payload: `session_id`/`pid` name the process the CP meant.
                payload={
                    "task_id": snap.task_id,
                    "session_id": snap.session_id,
                    "pid": snap.pid,
                },
            )
        ],
        commit=False,
    )
    return [issued]


def push_intents(issued: list[fleet_verdicts.IssuedIntents]) -> None:
    """Send minted intent frames — after the caller's commit, never before (else the node acks a row
    that does not exist yet). Deferred import (`routes/tasks/` cycle); `.send()` never raises."""
    if not issued:
        return
    from app.routes.tasks.slots import send_minted_intents  # noqa: PLC0415

    send_minted_intents(issued)


@dataclass(frozen=True)
class TreeErase:
    """What one best-effort node erase came to.

    `confirmed`: the directory is gone. `reached`: the node answered at all — a multi-slot caller stops
    after the first unreached tree rather than waiting out every timeout, but keeps going after a
    refusal, which is about that one tree."""

    confirmed: bool
    reached: bool
    # The one line an operator reads, in the node's own words. None iff `confirmed`.
    reason: Optional[str] = None


async def erase_tree_on_node(
    session,
    *,
    node_id: str,
    project: str,
    number: int,
    clone_path: str,
    stack_down_cmd: Optional[str],
    actor_email: str,
    node_label: Optional[str] = None,
    timeout: float = NODE_ERASE_TIMEOUT,
    folder_project_id: Optional[int] = None,
) -> TreeErase:
    """Ask one node to stop a slot's stack and delete its worktree directory. Never raises.

    Answers a `TreeErase`: confirmed, or a one-line reason (not connected, timed out, refused, failed),
    reported beside the row deletion rather than refusing it. The row goes first and unconditionally.

    `node_id` is the dispatch address and `node_label` the word. After the `nodes` row is deleted, pass
    the uuid: `node_identity.bus_key` falls back to the raw value, which is the socket's registry key
    only if it is the uuid. `clone_path` and `stack_down_cmd` are resolved by the caller (before the
    cascade, since the path comes from `nodes.jaah_home`); `stack_down_cmd` is never taken from a
    request, as it is executed on a node.

    The node's erase never refuses for what the tree holds (`force=True` is still sent for daemons that
    predate that); only a failure arrives as `TreeErase(confirmed=False)` with the node's sentence.

    `folder_project_id`: set (to the project's id) for a FOLDER slot (`Worktree.has_repo` false),
    which the node erases on its folder path — no git, no stack command; read off the row before it went.
    None for a git worktree, whose payload is byte for byte what it was."""
    # Deferred to avoid a service→routes import at module scope; `dispatch_and_audit` is the single home
    # of dispatch plus its audit row.
    from app.routes.commands import dispatch_and_audit  # noqa: PLC0415

    label = node_label or node_id
    try:
        reply, _audit_id = await dispatch_and_audit(
            session,
            node_id,
            "worktree_erase",
            {
                "project": project,
                "number": number,
                "clone_path": clone_path,
                "force": True,
                # A folder runs no stack, so the project's command is never run inside one.
                "stack_down_cmd": stack_down_cmd if folder_project_id is None else None,
                **({"has_repo": False, "project_id": folder_project_id} if folder_project_id is not None else {}),
            },
            actor_email,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — nothing on this hop may fail the caller's delete
        # Wide on purpose: `dispatch_and_audit` reaches a pool, a registry and a JSON decode too, and a
        # narrow catch turned a completed deletion into a 500. Bus refusals log one line; anything else
        # keeps its traceback.
        is_bus_refusal = _is_http_exception(exc)
        detail = _http_detail(exc) if is_bus_refusal else f"{type(exc).__name__}: {exc}"
        log.warning(
            "delete %s #%s on %s: row removed, node erase not delivered: %s",
            project, number, label, detail, exc_info=not is_bus_refusal,
        )
        # Not reached: the failure is about the hop, so more slots on this node would learn nothing new.
        return TreeErase(
            confirmed=False, reached=False,
            reason=f"the node was not reached: {_in_the_operators_words(detail, node_id, label)}",
        )
    if str(reply.get("status") or "failed") == "done":
        return TreeErase(confirmed=True, reached=True)
    result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    said = str(result.get("stderr") or result.get("reason") or "").strip()
    log.warning(
        "delete %s #%s on %s: row removed, node did not erase the tree: %s",
        project, number, label, said or "no reason given",
    )
    # Reached: the node answered about this tree, so a sibling slot may still succeed.
    return TreeErase(
        confirmed=False, reached=True,
        reason=f"the node did not erase the directory{f': {said}' if said else ''}",
    )


def _in_the_operators_words(detail: str, node_id: str, label: str) -> str:
    """The bus's own refusal, with the node's UUID swapped for the name an operator knows it by.

    The dispatch address must be the uuid on the node-delete path, but that row is already deleted, so
    the uuid names nothing; only the identifier is translated. A no-op when the two are the same (the
    slot-delete path dispatches by name)."""
    if not label or label == node_id or node_id not in detail:
        return detail
    return detail.replace(node_id, label)


def _is_http_exception(exc: BaseException) -> bool:
    """Is this one of the bus's own refusals (503 not connected / 504 silent / 502 malformed)?

    Imported lazily: `fastapi` belongs to the route layer, this module is service-tier."""
    from fastapi import HTTPException  # noqa: PLC0415

    return isinstance(exc, HTTPException)


def _http_detail(exc: BaseException) -> str:
    """An `HTTPException`'s detail as one line — the node's own words where it has them."""
    detail: Any = getattr(exc, "detail", None)
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, dict):
        for key in ("message", "reason", "error"):
            value = detail.get(key)
            if isinstance(value, str) and value:
                return value
    return str(exc)


def end_holders_for_node(
    session, slots: list[tuple[HolderSnapshot, str]], *, reason: str,
) -> tuple[list[fleet_verdicts.IssuedIntents], list[str]]:
    """`end_holder` over every slot on a node that is being deregistered or terminated.

    Returns intent batches to push after commit and one report line per slot that held something.
    `slots` pairs each snapshot with its project name."""
    issued: list[fleet_verdicts.IssuedIntents] = []
    ended: list[str] = []
    for snap, project in slots:
        holder = describe_holder(snap)
        if holder is None:
            continue
        ended.append(f"{project} #{snap.number} (was {holder})")
        issued.extend(
            end_holder(session, snap, project=project, reason=reason)
        )
    return issued, ended
