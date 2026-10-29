"""Intent execution (R3): the verb→executor table, slot locks, staleness guards, chains and the one ack."""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Awaitable, Callable

import control_plane_api
from reconcile import vocab, observe, enact_reset, enact_launch, enact_signal
from reconcile.vocab import (
    OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_REFUSED_PRECONDITION, OUTCOME_REFUSED_STALE, OUTCOME_UNSUPPORTED_VERB,
    VERB_DELIVER_ANSWER, VERB_DELIVER_MESSAGE, VERB_DESTROY_SLOT, VERB_ERASE_TREE, VERB_KILL_RUNNER,
    VERB_LEASE_SLOT, VERB_RELEASE_SLOT, VERB_RESET_TO_BASE, VERB_RUNTIME_CONTROL, VERB_STOP_STACK, _UNSTAMPED_VERBS,
)
from reconcile.tick_wire import RosterSlot, SlotIntent
from reconcile.context import ReconcileContext

log = logging.getLogger(__name__)


# ── Intent execution (R3) ─────────────────────────────────────────────────────
def _observation_field(obs: dict, dotted: str):
    """Read one observation field by name, `a.b` for a nested sub-record (`probes.dirty_ok`)."""
    cur: object = obs
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def preconditions_hold(obs: dict, preconditions: dict) -> tuple[bool, str]:
    """R3's second half: re-verify the CP's carried preconditions against a fresh observation.

    Fail-closed twice: an unobserved (null) field never satisfies one, and an empty set is refused rather
    than vacuously true (version equality alone is insufficient)."""
    if not preconditions:
        return False, "intent carried no preconditions (version equality alone is insufficient — R3)"
    for name, expected in preconditions.items():
        observed = _observation_field(obs, str(name))
        if observed is None:
            return False, f"{name} is unobserved"
        if observed != expected:
            return False, f"{name}={observed!r}, decided on {expected!r}"
    return True, ""


def observation_is_current(slot: RosterSlot, intent: SlotIntent) -> tuple[bool, str]:
    """R3's first half: does the intent name the observation still current for this slot? Both values are
    CP-authored.

    Fail-closed on either absence. Compared as strings — an identity test on an opaque token: any movement
    means the world was re-sensed, so no "newer than" ordering is wanted."""
    if not intent.decided_on_observed_at:
        return False, "no decided_on_observed_at — an intent with no observation stamp cannot be proven current"
    if not slot.observed_at:
        return False, (
            f"decided on observation {intent.decided_on_observed_at}, but the CP has no stored "
            f"observed_at for this slot"
        )
    if slot.observed_at != intent.decided_on_observed_at:
        return False, (
            f"decided on observation {intent.decided_on_observed_at}, slot is now at {slot.observed_at}"
        )
    return True, ""


# ── Hand-offs run off the pass ─────────────────────────────────────────────────────────────────────────
# A console hand-off's lease waits up to `HANDOFF_CLOSE_WAIT_S` for its old front to close. Inside a pass that
# wait held the pass's publish and every intent queued behind it, so `_execute_and_ack` starts it as a task
# instead and answers `OUTCOME_IN_FLIGHT` (never acked: the row stays pending, and a re-serve finds the task).
# The task acks its own outcome; a re-serve after it finished re-acks that outcome, never re-enacts it (a
# second launch would open a second front beside the first). Keyed by intent id, per loop like `_slot_locks`.
OUTCOME_IN_FLIGHT = "in_flight"

_HANDOFF_TASKS: "dict[object, asyncio.Task]" = {}

_HANDOFF_TASKS_LOOP: "asyncio.AbstractEventLoop | None" = None


# Finished hand-offs remembered for a re-serve that crossed their ack; oldest dropped past this.
_HANDOFF_TASKS_KEPT = 256


def _handoff_tasks() -> "dict[object, asyncio.Task]":
    """The hand-off task table for the running loop, emptied if the loop changed."""
    global _HANDOFF_TASKS, _HANDOFF_TASKS_LOOP
    loop = asyncio.get_running_loop()
    if loop is not _HANDOFF_TASKS_LOOP:
        _HANDOFF_TASKS_LOOP = loop
        _HANDOFF_TASKS = {}
    return _HANDOFF_TASKS


def _is_handoff_lease(intent: SlotIntent) -> bool:
    return intent.verb == VERB_LEASE_SLOT and intent.payload.get("handoff") is not None


def _handoff_progress(ctx: "ReconcileContext", slot: RosterSlot,
                      intent: SlotIntent) -> "tuple[str, str, dict] | None":
    """The hand-off `intent`'s finished `done` outcome, or None while it runs (started here on first sight, and
    again on a re-serve after an attempt that did not end `done`)."""
    tasks = _handoff_tasks()
    task = tasks.get(intent.id)
    if task is not None and task.done() and (task.cancelled() or task.result()[0] != OUTCOME_DONE):
        # Only `done` retires the row: anything else was acked once and is re-served for a fresh attempt.
        del tasks[intent.id]
        task = None
    if task is None:
        while len(tasks) >= _HANDOFF_TASKS_KEPT:
            oldest = next((key for key, t in tasks.items() if t.done()), None)
            if oldest is None:
                break
            del tasks[oldest]
        tasks[intent.id] = asyncio.get_running_loop().create_task(_handoff_then_ack(ctx, slot, intent))
        return None
    return task.result() if task.done() else None


async def _handoff_then_ack(ctx: "ReconcileContext", slot: RosterSlot,
                            intent: SlotIntent) -> "tuple[str, str, dict]":
    """Close the old front (`_close_for_handoff`), enact the lease under the slot lock, ack. Never raises.

    Enacted on `ctx.fresh()`: the pass that started this has published and moved on, and its snapshots predate
    the old front's close, so they would show the tree as it stood before the wait."""
    try:
        handoff = intent.payload.get("handoff")
        if isinstance(handoff, dict):
            await enact_launch._close_for_handoff(slot, str(handoff.get("from_session_id") or ""), intent.payload.get("task_id"))
        result = await execute_intent(ctx.fresh(), slot, intent)
    except Exception as exc:  # noqa: BLE001 — a hand-off's failure is its own ack, never the loop's crash
        log.warning("intent %s (hand-off %s #%s) failed: %s", intent.id, intent.project, intent.number, exc)
        result = (OUTCOME_FAILED, f"the hand-off failed: {type(exc).__name__}: {exc}", {})
    await _ack_intent(intent, *result)
    return result


# The executor table: one coroutine per verb, `(ctx, slot, intent, obs) -> (outcome, reason, ack_extras)`.
# Executors never ack; `execute_intents` sends the one ack. Membership in `_INTENT_EXECUTORS` is the only
# statement of "this node can enact X". Each returns its outcome rather than raising: refusal and failure are
# different rows CP-side.
_INTENT_EXECUTORS: dict[str, Callable[..., Awaitable[tuple[str, str, dict]]]] = {
    VERB_RESET_TO_BASE: enact_reset._enact_reset_to_base,
    VERB_STOP_STACK: enact_reset._enact_stop_stack,
    VERB_LEASE_SLOT: enact_launch._enact_lease_slot,
    VERB_RELEASE_SLOT: enact_reset._enact_release_slot,
    VERB_KILL_RUNNER: enact_signal._enact_kill_runner,
    VERB_DELIVER_ANSWER: enact_signal._enact_deliver_answer,
    VERB_DELIVER_MESSAGE: enact_signal._enact_deliver_message,
    VERB_DESTROY_SLOT: enact_signal._enact_destroy_slot,
    VERB_ERASE_TREE: enact_signal._enact_erase_tree,
    VERB_RUNTIME_CONTROL: enact_signal._enact_runtime_control,
}


# Verbs addressed by the projection, not the roster: they skip the batch loop's `slot is None` refusal
# (`build_pool_roster` drops a whole project on one unresolved field, which would spend their refusal budget)
# and `observe_slot` (they ignore its result). Their own fences are the whole fence.
_PROJECTION_ADDRESSED_VERBS = frozenset({
    VERB_KILL_RUNNER, VERB_DELIVER_ANSWER, VERB_DELIVER_MESSAGE, VERB_ERASE_TREE, VERB_RUNTIME_CONTROL,
})


# One enactment at a time per tree: `lease_slot` enacts in the launch lane outside the projection lock, and
# `claim_mechanics`' `reset --hard` and `observe_slot`'s git reads take the same index lock — a lost race is a
# spent dispatch or a garbled observation.
# Keyed on the path (what they share). Never cleared: bounded, and dropping a held lock hands out a fresh one.
# Dropped wholesale when the loop changes (an `asyncio.Lock` raises on a foreign loop, outside
# `execute_intent`'s handler); compared by loop object, never `id()`.
_SLOT_LOCKS: dict[str, asyncio.Lock] = {}

_SLOT_LOCKS_LOOP: "asyncio.AbstractEventLoop | None" = None


def _slot_locks() -> dict[str, asyncio.Lock]:
    """The lock table for the current loop, emptied if the loop changed. Outside any loop it is returned
    as-is."""
    global _SLOT_LOCKS, _SLOT_LOCKS_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _SLOT_LOCKS
    if loop is not _SLOT_LOCKS_LOOP:
        _SLOT_LOCKS_LOOP = loop
        _SLOT_LOCKS = {}
    return _SLOT_LOCKS


def slot_lock(path: str) -> asyncio.Lock:
    """The enactment lock for one worktree path, created on first use."""
    locks = _slot_locks()
    lock = locks.get(path)
    if lock is None:
        lock = asyncio.Lock()
        locks[path] = lock
    return lock


async def execute_intent(
    ctx: ReconcileContext,
    slot: RosterSlot | None,
    intent: SlotIntent,
) -> tuple[str, str, dict]:
    """Execute one CP intent behind R3's two-part staleness guard → `(outcome, reason, ack_extras)`.

    For observation-decided verbs both halves are required: the stamp proves the CP decided on the current
    observation, the preconditions that the world has not moved. `_UNSTAMPED_VERBS` skip the stamp guard
    and an absent tree is not itself a refusal (`lease_slot` builds one). Unsupported verbs are refused
    first and terminally. Only `done` retires an enacted intent."""
    label = f"intent {intent.id} ({intent.verb} {intent.project} #{intent.number})"
    # Serialised per tree (`slot_lock`). No roster slot means no tree to hold; the projection-addressed verbs
    # take the unheld path.
    if slot is None:
        return await _execute_intent_locked(ctx, slot, intent, label)
    async with slot_lock(slot.path):
        return await _execute_intent_locked(ctx, slot, intent, label)


async def _execute_intent_locked(
    ctx: ReconcileContext,
    slot: RosterSlot | None,
    intent: SlotIntent,
    label: str,
) -> tuple[str, str, dict]:
    """`execute_intent`'s body, with this slot's tree already held."""
    executor = _INTENT_EXECUTORS.get(intent.verb)
    if executor is None:
        why = (
            f"this node has no executor for verb {intent.verb!r} (it can enact "
            f"{sorted(_INTENT_EXECUTORS)}) — the control plane is ahead of this package"
        )
        log.warning("%s: %s — refusing terminally", label, why)
        return OUTCOME_UNSUPPORTED_VERB, why, {}

    obs: dict | None
    if intent.verb in _PROJECTION_ADDRESSED_VERBS:
        # No stamp check, no `observe_slot`, no roster — see `_PROJECTION_ADDRESSED_VERBS`.
        obs = None
    elif intent.verb in _UNSTAMPED_VERBS:
        obs = await observe.observe_slot(ctx, slot)
        # Only fields the CP carried a reading for are re-verified; None means "no reading", not "must equal
        # None".
        meaningful = {k: v for k, v in intent.preconditions.items() if v is not None}
        if meaningful:
            if obs is None:
                why = f"worktree {slot.path} is absent on disk — cannot re-verify {sorted(meaningful)}"
                log.warning("%s: %s — refusing", label, why)
                return OUTCOME_REFUSED_PRECONDITION, why, {}
            holds, why = preconditions_hold(obs, meaningful)
            if not holds:
                log.warning("%s: precondition failed — %s", label, why)
                return OUTCOME_REFUSED_PRECONDITION, why, {}
    else:
        current, why = observation_is_current(slot, intent)
        if not current:
            log.warning("%s: %s — refusing as stale", label, why)
            return OUTCOME_REFUSED_STALE, why, {}

        obs = await observe.observe_slot(ctx, slot)
        if obs is None:
            why = f"worktree {slot.path} is absent on disk"
            log.warning("%s: %s — refusing", label, why)
            return OUTCOME_REFUSED_PRECONDITION, why, {}
        holds, why = preconditions_hold(obs, intent.preconditions)
        if not holds:
            log.warning("%s: precondition failed — %s", label, why)
            return OUTCOME_REFUSED_PRECONDITION, why, {}

    try:
        outcome, why, extras = await executor(ctx, slot, intent, obs)
    except Exception as exc:  # noqa: BLE001 — a failed enactment must never wedge the pass
        why = f"{type(exc).__name__}: {exc}"
        log.warning("%s failed — %s", label, why)
        return OUTCOME_FAILED, why, {}

    if outcome != OUTCOME_DONE:
        log.warning("%s: %s — %s", label, outcome, why)
        return outcome, why, extras
    log.warning("%s enacted (%s)", label, intent.reason or "no reason given")
    return OUTCOME_DONE, why, extras


def _merge_intents(
    intents: tuple[SlotIntent, ...], pushed: tuple[SlotIntent, ...],
) -> tuple[SlotIntent, ...]:
    """The batch this pass executes: pushed intents and this tick's, deduped by id, in mint order.

    Pushed wins on the same id (the fresher copy); executing both would be a duplicate enactment (a second
    answer typed, a second kill on a re-leased slot). Then ordered by `(created_at, id)`, as the pool tick
    serves its rows, so the channel cannot reorder cause and effect (a later `lease_slot` before an older
    `release_slot` on one slot). `_slot_intent_from_entry` guarantees a sortable id."""
    merged: dict[object, SlotIntent] = {}
    for intent in (*pushed, *intents):
        # First copy wins; pushed is enumerated first.
        merged.setdefault(intent.id, intent)
    # ISO-8601 sorts chronologically; `id` breaks ties; an absent stamp (malformed frame) sorts first.
    return tuple(sorted(merged.values(), key=lambda i: (i.created_at or "", i.id)))


async def execute_intents(
    ctx: ReconcileContext,
    slots: dict[tuple[str, str], RosterSlot],
    intents: tuple[SlotIntent, ...],
    *,
    pushed: tuple[SlotIntent, ...] = (),
) -> dict[object, str]:
    """Execute every pending intent and ack each outcome. Runs before this pass publishes.

    Ordering is load-bearing: publishing first advances `observed_at` and every intent reads stale. A
    roster miss is a re-decidable refusal, checked after the unsupported-verb one (else a temporary refusal
    hides a permanent one). Every intent is acked, refusals included. `pushed` are bus-pushed intents,
    parsed by the same parser; a dropped push costs latency only, since the row stays pending.

    Independent chains run at once (`_intent_chains`, at most `_intent_concurrency()`), so a pass that
    leases N slots launches N runners together instead of one claim's git work after another; inside a chain
    mint order holds. The network git ops stay serialized daemon-wide (`clone_identity.cloning`), and so do
    tier-2 port picks (`worktree_mechanics.ports.apply_auto_ports`)."""
    outcomes: dict[object, str] = {}
    running = asyncio.Semaphore(_intent_concurrency())

    async def run_chain(chain: list[SlotIntent]) -> None:
        async with running:
            for intent in chain:
                outcomes[intent.id] = await _execute_and_ack(ctx, slots, intent)
                if outcomes[intent.id] == OUTCOME_IN_FLIGHT:
                    # Mint order holds across the wait: the rest of the chain waits for a later pass.
                    break

    # Every chain finishes before a raise surfaces: the pass must not probe a tree another chain still builds.
    results = await asyncio.gather(
        *(run_chain(chain) for chain in _intent_chains(_merge_intents(intents, pushed))), return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return outcomes


# Lease chains in flight at once. 4: a claim is mostly a `git worktree add` checkout plus a launch, and more
# at once only queues them on disk and on `clone_identity`'s one network-git lock.
_INTENT_CONCURRENCY_DEFAULT = 4


def _intent_concurrency() -> int:
    """Intent chains executed at once, read per pass, floored at 1 (a bad value degrades to sequential)."""
    return max(1, vocab._env_int("JAAHD_INTENT_CONCURRENCY", _INTENT_CONCURRENCY_DEFAULT))


def _intent_chains(ordered: tuple[SlotIntent, ...]) -> list[list[SlotIntent]]:
    """`ordered` split into chains that may run concurrently, each in mint order, chains ordered by first intent.

    Two intents share a chain when they name the same slot or the same task, transitively: one slot's
    release-then-lease, or a task's kill on its old slot before its launch on a new one, must keep their
    order; anything else touches nothing the other does."""
    parent: dict[object, object] = {}

    def find(key: object) -> object:
        while parent.setdefault(key, key) != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for intent in ordered:
        task_id = str(intent.payload.get("task_id") or "").strip()
        if task_id:
            parent[find(("slot", intent.ref))] = find(("task", task_id))
    chains: dict[object, list[SlotIntent]] = {}
    for intent in ordered:
        chains.setdefault(find(("slot", intent.ref)), []).append(intent)
    return list(chains.values())


async def _execute_and_ack(
    ctx: ReconcileContext, slots: dict[tuple[str, str], RosterSlot], intent: SlotIntent,
) -> str:
    """One intent of `execute_intents`: refuse or execute it, ack the outcome, return the outcome."""
    slot = slots.get(intent.ref)
    extras: dict = {}
    if intent.verb not in _INTENT_EXECUTORS:
        why = (
            f"this node has no executor for verb {intent.verb!r} (it can enact "
            f"{sorted(_INTENT_EXECUTORS)}) — the control plane is ahead of this package"
        )
        log.warning("intent %s (%s): %s — refusing terminally", intent.id, intent.verb, why)
        outcome = OUTCOME_UNSUPPORTED_VERB
    elif slot is None and intent.verb not in _PROJECTION_ADDRESSED_VERBS:
        # A roster miss refuses only verbs that need the roster (`_PROJECTION_ADDRESSED_VERBS`).
        why = f"{intent.project} #{intent.number} is not in this pass's roster"
        log.warning("intent %s (%s): %s — refusing", intent.id, intent.verb, why)
        outcome = OUTCOME_REFUSED_PRECONDITION
    elif _is_handoff_lease(intent):
        # Off the pass (`_handoff_progress`): its own task acks it; a finished one is re-acked below.
        finished = _handoff_progress(ctx, slot, intent)
        if finished is None:
            return OUTCOME_IN_FLIGHT
        outcome, why, extras = finished
    elif intent.verb in _ENACT_ONCE_VERBS:
        enacted = await _enact_once(ctx, slot, intent)
        if enacted is None:
            # A twin's refusal or failure: that enactment acked it, and a second ack would count it twice.
            return OUTCOME_IN_FLIGHT
        outcome, why, extras = enacted
    else:
        outcome, why, extras = await execute_intent(ctx, slot, intent)
    await _ack_intent(intent, outcome, why, extras)
    return outcome


# Verbs enacted at most once per intent id on this node (`_enact_once`): each can be served by two lanes at
# once — a pushed copy in its fast lane (`orchestrate.launch_lane_intents`) and the projection pass's polled
# twin. A second `lease_slot` kills the fresh runner; a second `deliver_answer`/`deliver_message` types the
# same text into the pane twice.
_ENACT_ONCE_VERBS = frozenset({VERB_LEASE_SLOT, VERB_DELIVER_ANSWER, VERB_DELIVER_MESSAGE})

# Each `_ENACT_ONCE_VERBS` intent this node enacted, by intent id, so a re-serve joins or re-acks it instead of
# enacting it again. The row stays pending until the ack lands, so the projection pass (or a joined pool tick)
# serves the copy a lane is enacting; a second enactment of a resume killed that fresh runner before its
# launch-claim was refused, and every relaunch of the task died unsighted. Per loop and bounded, like
# `_HANDOFF_TASKS`; lost on restart, where the CP's one-per-generation launch-claim is the backstop.
_ONCE_TASKS: "dict[object, asyncio.Task]" = {}

_ONCE_TASKS_LOOP: "asyncio.AbstractEventLoop | None" = None

_ONCE_TASKS_KEPT = 256


def _once_tasks() -> "dict[object, asyncio.Task]":
    """The once-enactment table for the running loop, emptied if the loop changed."""
    global _ONCE_TASKS, _ONCE_TASKS_LOOP
    loop = asyncio.get_running_loop()
    if loop is not _ONCE_TASKS_LOOP:
        _ONCE_TASKS_LOOP = loop
        _ONCE_TASKS = {}
    return _ONCE_TASKS


def _retired(task: asyncio.Task) -> bool:
    """A finished enactment that a re-serve must not repeat: one that ended `done`."""
    return task.done() and not task.cancelled() and task.exception() is None and task.result()[0] == OUTCOME_DONE


async def _enact_once(ctx: ReconcileContext, slot: RosterSlot | None,
                      intent: SlotIntent) -> "tuple[str, str, dict] | None":
    """`execute_intent` for an `_ENACT_ONCE_VERBS` intent, at most once per intent id while it runs or once it
    ended `done`: a twin served meanwhile awaits the same enactment, and a re-serve after `done` gets its outcome
    to re-ack. None tells the twin there is nothing to ack (the enactment it joined did not end `done`, and acked
    that itself). Any other finished outcome was acked once and is tried afresh, as before."""
    tasks = _once_tasks()
    prior = tasks.get(intent.id)
    if prior is not None and (not prior.done() or _retired(prior)):
        log.info("intent %s (%s %s #%s): already enacted on this node — re-acking its outcome, not "
                 "enacting again", intent.id, intent.verb, intent.project, intent.number)
        try:
            await asyncio.shield(prior)
        except Exception:  # noqa: BLE001 — the enactment's own caller handles its failure
            return None
        return prior.result() if _retired(prior) else None
    while len(tasks) >= _ONCE_TASKS_KEPT:
        oldest = next((key for key, t in tasks.items() if t.done()), None)
        if oldest is None:
            break
        del tasks[oldest]
    task = asyncio.get_running_loop().create_task(execute_intent(ctx, slot, intent))
    tasks[intent.id] = task
    # Shielded: a cancelled pass must not cancel an enactment half done, which its twin may be awaiting.
    return await asyncio.shield(task)


async def _ack_intent(intent: SlotIntent, outcome: str, why: str, extras: dict) -> None:
    """Ack one intent's outcome to the CP. Never raises: a lost ack just re-delivers the intent next tick."""
    try:
        # The launch half of the ack; without it `record_launch` never stamps the hold.
        await asyncio.to_thread(
            functools.partial(control_plane_api.ack_slot_intent, intent.id, outcome, why or None, **extras)
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("intent %s: ack (%s) failed: %s", intent.id, outcome, exc)
