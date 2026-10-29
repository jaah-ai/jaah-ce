"""Worktree slots a Task holds: bind/release, runner kill intents and sending minted intents."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import agent_session_lifecycle, agent_sessions, edition_features, fleet_verdicts, slot_state
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import SlotIntent, Worktree
from app.node_identity import node_ref
from app.task_lifecycle import _NO_RUNNER_STATES

log = logging.getLogger(__name__)


# How long a freshly minted kill intent suppresses another for the same task: one row across the window
# between a kill landing and the binding clearing, while a genuine later re-settle earns its own.
_KILL_INTENT_REMINT_COOLDOWN = timedelta(minutes=10)


def kill_intents_for_settled_holds(
    session: Session, node_id: str, *, now: Optional[datetime] = None
) -> tuple[list[dict[str, Any]], list[fleet_verdicts.IssuedIntents]]:
    """Mint a kill intent for every pool slot still bound to a task that has ALREADY SETTLED.

    Invisible to other reapers: a runner can settle and not exit, and `self_heal.classify_live_stalls`
    keys on idleness. The signal is `worktrees.task_id` naming a settled task — no clock or probe needed.
    Mints `kill_runner`; convergent on the ~5s pass, ended by the sensed-session conjunct
    below. Does not commit: the caller owes the commit and the `send_minted_intents` after it.

    `paused` counts as settled: an operator Stop kills nothing itself and keeps the slot, so this pass is
    what stops the runner, and the binding stays for the resume. Freeing `done`/`system_failed` slots is
    `lease_drain`'s job."""
    # Not a subquery: the hold is on this row, so there is no nullable node column whose NULL could fail
    # open on the one reaper that catches an unkillable runner. The id keys suppression and the `SlotIntent`;
    # the joined name goes in the reason. INNER: `worktrees.project_id` is NOT NULL with an FK.
    bound = session.execute(
        select(
            Worktree.node_id, Worktree.project_id, Project.name, Worktree.number,
            Worktree.task_id, Task.state, Worktree.launch_session_id, Worktree.pid, Worktree.generation,
        )
        .join(Project, Project.id == Worktree.project_id)
        .join(Task, Task.id == Worktree.task_id)
        .where(
            Worktree.node_id == node_ref(node_id),
            Worktree.task_id.is_not(None),
            Task.state.in_(_NO_RUNNER_STATES),
            # Mint only while there is something to kill, or the sweep never ends: for a `paused` task the binding
            # stays on purpose and enacting the kill changes neither conjunct, so the cooldown only paces the loop.
            # Each enactment also SIGKILLs every process in the worktree (`jaahd/reconcile/enact_signal.py::_enact_kill_runner`).
            # `session` is the node's `session_agents` reading: only `false` (probe ran, nothing there) stops
            # minting; `true` and NULL still mint (fail-closed). This is the backstop: `stop_task` mints its own kill
            # in the Stop's transaction, so silencing here only loses a second chance.
            Worktree.session.is_not(False),
        )
    ).all()
    return _mint_kill_runner_intents(session, bound, now=now)


def _slots_bound_to_task(task_id: int, session: Session) -> list[Any]:
    """The pool slots `worktrees.task_id` says this task holds — the addressing both kill producers share.
    Selects by the binding alone (any node). Yields rows read by attribute — `node_id`, `project_id`,
    `name`, `number`, `task_id`, `launch_session_id`, `pid`, `worktree_id`, `generation` — since
    `_release_task_slots` writes off the same read and positional unpacking could swap a pid for a
    generation.

    `launch_session_id`, not `session_id`: a kill must name the runner the CP launched, not whatever the
    host sensed in the slot now. Scoped to `state != FREE`: `worktrees.task_id` is stale until the node
    declares `free`, and an unfiltered read would mint a `kill_runner` at a stranger's process."""
    return list(session.execute(
        select(
            Worktree.node_id, Worktree.project_id, Project.name, Worktree.number, Worktree.task_id,
            Worktree.launch_session_id, Worktree.pid, Worktree.id.label("worktree_id"),
            Worktree.generation,
        )
        .join(Project, Project.id == Worktree.project_id)
        .where(Worktree.task_id == task_id, Worktree.state != slot_state.FREE)
    ).all())


def _release_task_slots(task_id: int, session: Session, *, reason: str) -> None:
    """Hand every pool slot bound to this task back to the pool — the release half of the two task edges
    enacted here: an operator Reset and a Delete (`delete_task_core` does its own off the same read).
    Terminal release is the drain's (`lease_drain._release_finished`; see `end_attempt`), and an
    operator's per-slot console release is `routes/pool_slots.py::release_slot`. Does NOT commit.

    Separate from the kill: most attempt ends keep the worktree, so callers release only on those edges.
    `slot_state.release` is idempotent and fenced on the `generation` read here.

    The slot's last session is closed as `abandoned` BEFORE the release blanks its id (`release` never ends
    one): after a Stop then a Delete nothing re-leases the slot to supersede it, so it stayed `running` in
    Audit > Sessions for good. A Reset closed it already (`abandon_task_slots`);
    `note_end` keeps that first reason."""
    for r in _slots_bound_to_task(task_id, session):
        agent_session_lifecycle.note_end(
            session, agent_session_id=r.launch_session_id or "",
            end_reason=agent_sessions.END_REASON_ABANDONED,
        )
        slot_state.release(
            session, worktree_id=r.worktree_id, generation=r.generation,
            reason=reason, commit=False,
        )


def abandon_task_slots(
    task_id: int, session: Session, *, reason: str,
) -> tuple[list[dict[str, Any]], list[fleet_verdicts.IssuedIntents]]:
    """Tear the runner down AND wipe the tree, for the one edge that declares an attempt's work
    ABANDONED — an operator Reset. Returns `(minted records, issued batches)` like the kill producer
    beside it; does NOT commit, so the caller owes the commit and the `send_minted_intents` after it.

    A `kill_runner` minted beside the release in one commit never fires: the node's executor is fenced
    on the CP's projection (`jaahd/reconcile/enact_signal.py::_enact_kill_runner`) and finds the slot `free`. That
    fence is correct; the verb is wrong. So a Reset mints one `reset_to_base` carrying `abandon`, whose
    executor clears processes, then tmux windows, then git — one verb behind one fence, since ordering
    two intents is only as good as every hop carrying them.

    The payload names no branch; `abandon` licenses resetting a tree dirty on its own branch.
    The slot is freed in this commit and may be re-leased before the node drains, so the node re-proves
    the address in `_enact_reset_to_base`. Must be called before `_release_task_slots` (whose scope then
    finds nothing). From `done`/`system_failed` it mints nothing: the slot was already returned and
    guessing would aim a destructive order at a re-leased slot. What it mints is an order, not an outcome."""
    minted: list[dict[str, Any]] = []
    # Grouped by node: `issue_intents` addresses one node per call, and slots may sit on several.
    by_node: dict[Any, list[dict[str, Any]]] = {}
    for r in _slots_bound_to_task(task_id, session):
        if r.node_id is None:
            # An intent is addressed to a node; without one it can never be served, so this is a skip.
            log.warning(
                "abandon-slots: slot %s #%s is bound to task %s but carries no node_id — no intent minted",
                r.name, r.number, task_id,
            )
            continue
        by_node.setdefault(r.node_id, []).append(
            fleet_verdicts.build_slot_intent(
                fleet_verdicts.VERB_RESET_TO_BASE, int(r.project_id), str(r.name), int(r.number),
                reason=f"{reason} — end the runner, reset the tree and drop the abandoned branch",
                payload={"task_id": int(task_id), "abandon": True},
            )
        )
        minted.append({
            "task_id": int(task_id), "project_id": int(r.project_id), "project": str(r.name),
            "number": int(r.number),
        })
        # The session ledger closes with the runner, or Sessions keeps showing it live. Best-effort idempotent
        # (`note_end`: the first reason wins). `ABANDONED`, not `NORMAL`: this runner was force-killed because the
        # operator abandoned its work, and `normal` means ran to completion and settled (`agent_sessions.py`).
        agent_session_lifecycle.note_end(
            session, agent_session_id=r.launch_session_id or "",
            end_reason=agent_sessions.END_REASON_ABANDONED,
        )
    # A Reset abandons the work, so the task's MicroVM goes with its disk (A7), a retained one included.
    vm_issued = _destroy_task_vm(session, task_id, retain=False, reason=f"{reason} — the work is abandoned")
    return minted, [
        fleet_verdicts.issue_intents(session, node_id, intents, commit=False)
        for node_id, intents in by_node.items()
    ] + vm_issued


def kill_runner_keeping_hold(
    task_id: int, session: Session, *, reason: str, now: Optional[datetime] = None,
    fresh_decision: bool = False,
) -> tuple[list[dict[str, Any]], list[fleet_verdicts.IssuedIntents]]:
    """Stop the runner in the slot(s) bound to this task WITHOUT giving the slot back — the one kill
    producer for a task's own bound slot.

    A kill is not a release: the node's enactment touches no occupancy column (only the CP writes occupancy). Most
    callers (a retryable `end_attempt`, a break, a Stop's settled-hold reap) keep the tree for the next dispatch;
    `slot_state.release` would hand it to a stranger. Does NOT commit — the caller owes the commit and the
    `send_minted_intents` that follows it.

    A task bound to a MicroVM has no slot; its kill is that VM's `destroy` (D20), keeping the disk exactly when
    the task now reads `system_failed` (A7). The node never sees `kill_runner` for a VM."""
    minted, issued = _mint_kill_runner_intents(
        session,
        [(r.node_id, r.project_id, r.name, r.number, r.task_id, "", r.launch_session_id, r.pid, r.generation)
         for r in _slots_bound_to_task(task_id, session)],
        now=now,
        reason=reason,
        fresh_decision=fresh_decision,
    )
    return minted, issued + _destroy_task_vm(session, task_id, reason=reason)


def _destroy_task_vm(session: Session, task_id: int, *, reason: str, retain: Optional[bool] = None,
                     ) -> list[fleet_verdicts.IssuedIntents]:
    """`microvm_lifecycle.destroy_for_task`; nothing where MicroVM is not shipped (the community edition)."""
    if not edition_features.enabled("microvm"):
        return []
    from app import microvm_lifecycle

    return microvm_lifecycle.destroy_for_task(session, task_id, reason=reason, retain=retain)


def _mint_kill_runner_intents(
    session: Session,
    bound: Sequence[tuple[Any, Any, Any, Any, Any, str, Any, Any, Any]],
    *,
    now: Optional[datetime] = None,
    reason: Optional[str] = None,
    fresh_decision: bool = False,
) -> tuple[list[dict[str, Any]], list[fleet_verdicts.IssuedIntents]]:
    """Mint one `kill_runner` slot intent per bound-slot row that has not already earned one — the write half
    of the slot release, shared by its producers so their suppression rules cannot drift apart.

    `reason` overrides the default for the producer whose kill is not a release. `session_id`/`pid`/`generation`
    come off the hold row, so the node kills the launch the CP meant and retires the order stale once a
    successor holds the slot (`fleet_verdicts.build_kill_intent`). `commit=False` because a kill must land
    in the caller's commit: the returned `IssuedIntents` go to `send_minted_intents` after it. Returns
    `(minted records, issued batches)`, one batch per node."""
    if not bound:
        return [], []
    # Suppressed by a pending intent or a recently minted one: the binding does not clear the instant a kill
    # lands, so a ~5s pass would otherwise mint every five seconds. Keyed on (task_id, project_id, slot).
    # A fresh decision (an operator Stop) skips only the recency arm: the cooldown defends against a loop,
    # and swallowing a Stop left the runner working for minutes. A pending kill still suppresses (no double
    # order).
    recent_cutoff = (
        None if fresh_decision
        else (now or datetime.now(timezone.utc)) - _KILL_INTENT_REMINT_COOLDOWN
    )
    # Keyed per launch too: a pending kill minted for a predecessor generation is retired stale at the node
    # (`_enact_kill_runner`), so it must not swallow the kill this launch earns. `None` (a legacy row, or a hold
    # with no generation) suppresses across generations, as before the fence.
    suppressed: dict[tuple[int, int, str], set[Optional[str]]] = {}
    for project_id, number, task_key, generation_key in session.execute(
        select(
            SlotIntent.project_id, SlotIntent.number, SlotIntent.payload["task_id"].astext,
            SlotIntent.payload["generation"].astext,
        ).where(
            SlotIntent.verb == fleet_verdicts.VERB_KILL_RUNNER,
            or_(
                # Still pending. `outcome IS NULL` must be explicit: `NOT IN (...)` over a NULL is not true.
                SlotIntent.outcome.is_(None),
                SlotIntent.outcome.notin_(fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES),
                # Dropped entirely for a fresh decision rather than given a NULL cutoff, which only works by accident of
                # three-valued logic.
                *([] if recent_cutoff is None else [SlotIntent.created_at >= recent_cutoff]),
            ),
        )
    ).all():
        if task_key is not None:
            suppressed.setdefault((int(project_id), int(number), str(task_key)), set()).add(generation_key)

    def _is_suppressed(key: tuple[int, int, str], generation: Any) -> bool:
        generations = suppressed.get(key)
        if generations is None:
            return False
        return generation is None or None in generations or str(int(generation)) in generations

    minted: list[dict[str, Any]] = []
    # Grouped by node: `issue_intents` addresses one node per call, and `_slots_bound_to_task` selects by
    # binding alone.
    by_node: dict[Any, list[dict[str, Any]]] = {}
    for node_id, project_id, project, number, task_id, state, launch_session_id, pid, generation in bound:
        if node_id is None:
            # An intent is addressed to a node; without one it can never be served, so this is a skip.
            log.warning(
                "kill-intent: slot %s #%s is bound to task %s but carries no node_id — no intent minted",
                project, number, task_id,
            )
            continue
        key = (int(project_id), int(number), str(int(task_id)))
        if _is_suppressed(key, generation):
            continue
        # Not `str(node_id)`: both columns are `nodes.id` uuids.
        by_node.setdefault(node_id, []).append(
            fleet_verdicts.build_kill_intent(
                int(project_id), str(project), int(number),
                reason=reason or (
                    f"task {task_id} is '{state}' but pool slot {project} #{number} is still bound to it "
                    "— kill the runner and free the slot"
                ),
                task_id=int(task_id),
                session_id=launch_session_id,
                pid=pid,
                generation=int(generation) if generation is not None else None,
            )
        )
        # Both roles on the record: callers log `project`, key on `project_id`.
        minted.append({
            "task_id": int(task_id), "project_id": int(project_id), "project": str(project),
            "number": int(number),
        })
        suppressed.setdefault(key, set()).add(None if generation is None else str(int(generation)))
    return minted, [
        fleet_verdicts.issue_intents(session, node_id, intents, commit=False)
        for node_id, intents in by_node.items()
    ]


def send_minted_intents(issued: Iterable[fleet_verdicts.IssuedIntents]) -> None:
    """Push the intents a `commit=False` producer handed back (`_mint_kill_runner_intents`,
    `abandon_task_slots`) — AFTER YOUR COMMIT, NEVER BEFORE.

    These producers write in the caller's transaction, so the caller owns the push; a frame overtaking
    the commit has the node acking a row that does not exist yet. Best-effort: `.send()` never raises and
    a dropped frame costs only latency (the node's pool tick serves it)."""
    for batch in issued:
        batch.send()
