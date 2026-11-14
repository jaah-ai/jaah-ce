"""The reconcile passes: fetch → execute → probe → publish, the launch lane, and the legacy-layout migration."""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Awaitable, Callable, Sequence

import cloud_modules
import git_mirror
import host_platform
import legacy_layout
from worktree_mechanics import processes
from env_helpers import jaah_dir, now_iso
from jaah_env import env_number
from reconcile import vocab, tick_wire, observe, execute
from reconcile.vocab import (
    CONTAINER_ABSENT, CONTAINER_UNREADABLE, VERB_DELIVER_ANSWER, VERB_DELIVER_MESSAGE, VERB_LEASE_SLOT,
)
from reconcile.tick_wire import RosterSlot, SlotIntent
from reconcile.context import ReconcileContext

# The MicroVM marker (D0); None on a community-edition node, which is never a MicroVM node.
microvm_mark = cloud_modules.load("microvm_mark")
microvm_bridge = cloud_modules.load("microvm_bridge")

log = logging.getLogger(__name__)


async def migrate_legacy_layout(tick: tick_wire.PoolTick, *,
                                fresh_context: "Callable[[], ReconcileContext]" = ReconcileContext) -> list[str]:
    """`legacy_layout`'s three steps (its module docstring), with step 1 under the enactment lock of each free slot it would erase (so a
    lease cannot clone into a tree while it goes; a slot whose lock is held — an enactment in flight — is skipped
    this pass) → the paths erased. Off the loop, on ONE fresh agents probe (`fresh_context()`, no cross-tick cache)
    taken only when there is a candidate; steps 2 and 3 touch nothing a lease reads, so they run unlocked."""
    repos_root = str(jaah_dir("repos"))
    free = [legacy_layout.Slot(path=slot.path, free=True)
            for slot in tick.slots if slot.has_repo and slot.state == tick_wire.SLOT_FREE]
    legacy = [slot for slot in free if await asyncio.to_thread(legacy_layout.slot_trees, slot.path)]
    later = await asyncio.to_thread(legacy_layout.unlocked_candidates, repos_root,
                                    roster_complete=tick.roster_complete)
    if not legacy and not later:
        return []
    agents_ok, _agents_json, cwds = await fresh_context().agents()
    locks = {slot.path: execute.slot_lock(slot.path) for slot in legacy}
    held = [slot for slot in legacy if not locks[slot.path].locked()]
    for slot in held:
        await locks[slot.path].acquire()
    try:
        trees = await asyncio.to_thread(legacy_layout.free_legacy_trees, held)
        erased = await asyncio.to_thread(legacy_layout.erase_idle, trees, agents_ok, cwds)
    finally:
        for slot in held:
            locks[slot.path].release()
    # Re-read: a main step 1 just freed is erasable on this pass.
    later = await asyncio.to_thread(legacy_layout.unlocked_candidates, repos_root,
                                    roster_complete=tick.roster_complete)
    return erased + await asyncio.to_thread(legacy_layout.erase_idle, later, agents_ok, cwds)


# ── Slot observation (D4) ─────────────────────────────────────────────────────
async def scan_pool_cwds(ctx: ReconcileContext, paths: "Sequence[str]") -> None:
    """Take this pass's gate-0 reading for every slot on one /proc walk and bind it to `ctx`.

    Called once before the probe loop (per-slot scans: 8.8s vs 0.19s pooled on 23 slots). A raising walk
    leaves no pre-scan and every slot scans its own; `scan_cwd_processes_multi` already answers
    PROBE_FAILED for a broken /proc."""
    wanted = list(dict.fromkeys(paths))
    if not wanted:
        return
    try:
        scans = await asyncio.to_thread(
            functools.partial(processes.scan_cwd_processes_multi, wanted, collect=True),
        )
    except Exception as exc:  # noqa: BLE001 — a failed pre-scan costs speed, never correctness
        log.warning("reconcile: the pooled cwd walk failed (%s) — every slot falls back to its own", exc)
        return
    ctx.adopt_cwd_scans(dict(zip(wanted, scans, strict=False)))


async def reconcile_slot(slot: RosterSlot, ctx: ReconcileContext) -> dict | None:
    """Observe one slot; None when absent on disk. The pass sends all observations in one POST. Ahead/behind
    reads the slot clone's own `origin/<base>`, refreshed at each lease (`slot_clone.refresh`)."""
    return await observe.observe_slot(ctx, slot)


# Verbs the launch lane may enact outside the projection lock — exactly one, and the narrowness is the safety
# argument: `lease_slot` writes none of the lock's projection keys; it is unstamped with no precondition
# reading, so a concurrent pass cannot stale it; and the CP's `launch-claim` single-flights it. Stamped and
# projection-deciding verbs stay on the pass.
LAUNCH_LANE_VERBS = frozenset({VERB_LEASE_SLOT})

# The pane-write lane's verbs, enacted outside the projection lock AND the launch lane's lock: a human is waiting
# on each, and both locks can be held for minutes by a launch that hangs (measured: an answer acked five
# minutes late, the moment another slot's `lease_slot` ended). Same argument as the launch lane — they write no
# projection key, are unstamped and projection-addressed, and their own session fence is the whole fence
# (`enact_signal._fenced_pane`); `execute._enact_once` keeps a polled twin from typing the text twice.
SIGNAL_LANE_VERBS = frozenset({VERB_DELIVER_ANSWER, VERB_DELIVER_MESSAGE})


def launch_lane_intents(
    entries: "Sequence[dict]", verbs: frozenset[str] = LAUNCH_LANE_VERBS,
) -> tuple[tuple[tuple[SlotIntent, dict], ...], tuple[dict, ...]]:
    """Split raw pushed intent entries into `(the lane's — those of VERBS —, everybody else's)`.

    The lane's half keeps each raw entry: a declined intent must go back to the pass in the shape it
    parses."""
    lane: list[tuple[SlotIntent, dict]] = []
    rest: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        intent = tick_wire._slot_intent_from_entry(entry)
        if intent is None:
            # Refused by the parser; re-queueing would only refuse it again.
            continue
        if intent.verb in verbs:
            lane.append((intent, entry))
        else:
            rest.append(entry)
    return tuple(lane), tuple(rest)


async def launch_pass(
    ctx: ReconcileContext,
    tick: tick_wire.PoolTick,
    intents: "Sequence[tuple[SlotIntent, dict]]",
    running: "asyncio.Semaphore | None" = None,
) -> tuple[dict, ...]:
    """Enact one fast lane's pushed intents (`LAUNCH_LANE_VERBS` or `SIGNAL_LANE_VERBS`) and nothing else — no
    probes, no POST, no projection write. The lane a human pressing Run, or answering a question, waits on (it
    otherwise waited out a whole in-flight pass holding the lock).

    The caller owns how lane passes share the node (`main.NodeStatePublisher`: the launch lane's passes overlap
    under one shared `running` bound, the pane-write lane's take turns); per-tree exclusion against any other
    pass is `execute_intent`'s `slot_lock`, and a re-served intent is enacted once (`execute._enact_once`).

    Returns the raw entries it declined, for the projection pass: on a degraded tick (no roster), and when
    an older intent for the same slot is still pending — this lane cannot order across channels, and a
    lease must not jump an older release on its slot (`_merge_intents`)."""
    if not intents:
        return ()
    if not tick.ok:
        # No roster: left pending for the next tick, as `reconcile_pass` does.
        log.warning("fast lane (%s): no roster this pass — %d intent(s) left pending for the next tick",
                    ", ".join(sorted({i.verb for i, _e in intents})), len(intents))
        return tuple(entry for _intent, entry in intents)
    oldest_pending: dict[tuple[str, str], object] = {}
    for pending in tick.intents:
        current = oldest_pending.get(pending.ref)
        if current is None or pending.id < current:
            oldest_pending[pending.ref] = pending.id
    runnable: list[SlotIntent] = []
    declined: list[dict] = []
    for intent, entry in intents:
        older = oldest_pending.get(intent.ref)
        if older is not None and older < intent.id:
            log.info(
                "fast lane: intent %s (%s %s #%s) is behind still-pending intent %s for the same slot — "
                "leaving it to the projection pass, which orders both channels by mint order",
                intent.id, intent.verb, intent.project, intent.number, older,
            )
            declined.append(entry)
        else:
            runnable.append(intent)
    if runnable:
        ctx.adopt_auto_ports(tick.auto_ports)
        await execute.execute_intents(ctx, {s.ref: s for s in tick.slots}, (), pushed=tuple(runnable),
                                      running=running)
    return tuple(declined)


async def drain_pushed_intents(
    ctx: ReconcileContext,
    slots: dict[tuple[str, str], RosterSlot],
    drain: "Callable[[], list[dict]] | None",
) -> None:
    """Enact any intent that landed on the push queue since this pass began — the backstop lane, for
    intents that arrived while the launch lane was busy or that it does not carry. Never raises into the
    probe loop."""
    if drain is None:
        return
    entries = drain()
    if not entries:
        return
    pushed = tick_wire._slot_intents_from_entries(entries)
    if not pushed:
        return
    log.info("reconcile: %d intent(s) landed mid-pass — enacting before continuing the probe loop",
             len(pushed))
    try:
        await execute.execute_intents(ctx, slots, (), pushed=pushed)
    except Exception as exc:  # noqa: BLE001 — the pass must finish and publish whatever happens here
        log.warning("reconcile: the mid-pass intent drain failed: %s", exc)


# Concurrent slot probes (`JAAHD_PROBE_CONCURRENCY`): subprocess-bound, so they overlap; bounded to spare
# the runners' host. 6: measured on a 23-slot node, ~7s serial → ~1.5s with at most six git processes in flight.
# Safe only because the three shared snapshots (`agents`/`docker`/`tmux`) are primed before the loop: each
# memo stamps `_done` before its first await, so a concurrent first caller gets the un-computed default. Check
# that property before parallelising anything else onto a cold context.
_PROBE_CONCURRENCY_DEFAULT = 6


def _probe_concurrency() -> int:
    """Slot probes in flight at once, read per pass, floored at 1 (a bad value degrades to sequential)."""
    return max(1, env_number("JAAHD_PROBE_CONCURRENCY", _PROBE_CONCURRENCY_DEFAULT, parse=int))


async def _probe_slot_entry(slot: RosterSlot, ctx: ReconcileContext, tick: tick_wire.PoolTick) -> dict:
    """One slot's wire entry for this pass. Every path returns an entry: silence reads as "gone" to the
    CP's complement DELETE."""
    # An enactment holds this tree: report `unobserved` (spares the row) rather than read a half-built tree or
    # wait. Check and acquire are one step (`acquire()` on an unheld lock does not yield), and holding it
    # makes a later enactment wait out this probe.
    lock = execute.slot_lock(slot.path)
    if lock.locked():
        return observe._unobserved_slot_entry(
            slot.project, slot.number, slot.path, "an enactment is in flight in this tree",
        )
    async with lock:
        return await _probe_slot_entry_locked(slot, ctx, tick)


async def _probe_slot_entry_locked(slot: RosterSlot, ctx: ReconcileContext, tick: tick_wire.PoolTick) -> dict:
    """`_probe_slot_entry`'s body, with this slot's tree already held."""
    try:
        observation = await reconcile_slot(slot, ctx)
        if observation is not None:
            return observe._observed_slot_entry(slot, observation)
        # Absent dir: the verdict is `_unprobed_slot_entry`'s (shared with the `known_slots` loop); the log
        # reads it off the entry rather than re-deriving it.
        container = vocab.slot_container(slot.path)
        held_in = vocab.container_state(container)
        entry = observe._unprobed_slot_entry(
            slot.project, slot.number, slot.path, ever_observed=bool(slot.observed_at),
        )
        if held_in == CONTAINER_UNREADABLE:
            log.warning(
                "reconcile: slot %s/%s absent AND its container %s is unreadable — treating as "
                "unobserved (not a deletion), so its row is spared",
                slot.project, slot.number, container,
            )
        elif held_in == CONTAINER_ABSENT:
            log.warning(
                "reconcile: slot %s/%s absent AND its container %s does not exist either — that "
                "is a positive observation of absence (not a failure to look), so the row is "
                "reported %s",
                slot.project, slot.number, container, entry["state"],
            )
        return entry
    except Exception as exc:  # noqa: BLE001 — one slot must never wedge the whole pass
        log.warning("reconcile: slot %s/%s failed: %s", slot.project, slot.number, exc)
        # A raised probe is unknown, not gone: spared, without re-running the disk test (nothing was
        # observed), and still emitted.
        return observe._unobserved_slot_entry(
            slot.project, slot.number, slot.path, f"probe raised: {type(exc).__name__}: {exc}",
        )


async def reconcile_pass(
    *,
    node_id: str | None = None,
    ctx: ReconcileContext | None = None,
    tick: tick_wire.PoolTick | None = None,
    accounts: list[dict] | None = None,
    accounts_ok: bool = False,
    host_metrics: dict | None = None,
    extra_intents: "list[dict] | tuple[dict, ...] | None" = None,
    agents_refresh: "Awaitable[None] | None" = None,
    drain_pushed: "Callable[[], list[dict]] | None" = None,
) -> dict | None:
    """One pass, in order: fetch → execute → probe → publish, then POST. Returns the CP's observe response
    (None when no POST was made or it did not land). Publishing before executing would
    invalidate every intent in the batch.

    `agents_refresh`: an already-started `refresh_agents_cache` task, joined between execute and probe so its
    fan-out does not delay `lease_slot`. The execute arm does read the snapshot (`observe_slot` →
    `ctx.agents()`), harmlessly (a lease carries no precondition reading); `invalidate_agents` then drops
    that memo so the probes and POST read the refreshed cache.
    `drain_pushed`: the push-queue drain, called between slot probes (the backstop lane).
    `extra_intents`: raw bus-pushed intents, parsed by the one parser and merged (`_merge_intents`); a
    dropped frame costs latency only.

    The POST sits inside the `pool_tick.ok` branch: a degraded tick's empty roster would hand the CP a
    complement DELETE over every slot. A slot whose probe raised still gets an entry (silence reads
    "gone")."""
    pass_ctx = ctx or ReconcileContext()
    # Parsed first, so an unusable pushed entry is logged whatever the tick is.
    pushed = tick_wire._slot_intents_from_entries(extra_intents)

    # (1) FETCH.
    pool_tick = tick if tick is not None else await tick_wire.fetch_pool_tick(node_id or pass_ctx.node_id)
    observed: dict | None = None
    # Before the execute and converge arms, which allocate node-assigned ports: how an operator's grid edit reaches
    # the allocator.
    pass_ctx.adopt_auto_ports(pool_tick.auto_ports)
    # The MicroVM marker (D0): jaahd's whole part in switching a node's mode. Linux and macOS.
    if pool_tick.microvm is not None and microvm_mark is not None and microvm_mark.host_supported():
        microvm_mark.sync(pool_tick.microvm)
    # A marked node builds its base image once (D5) and enacts the `microvm` rail; neither ever awaits a VM.
    if microvm_mark is not None and microvm_mark.host_supported() and microvm_mark.is_marked():
        rail = tick_wire._microvm_rail()
        try:
            rail.microvm_images.ensure_started()
        except Exception as exc:  # noqa: BLE001 — a broken pins file is the base fact's to report, not this pass's
            log.warning("reconcile: the MicroVM base image could not be started: %s", exc)
        rail.enact(pool_tick.microvm_intents)
    # Windows: the same mark and orders, to the Linux runtime in the WSL2 backend (`microvm_bridge`).
    windows_microvm = microvm_bridge.windows_client() if microvm_bridge is not None else None
    if windows_microvm is not None and host_platform.is_windows():
        await asyncio.to_thread(windows_microvm.tick, pool_tick.microvm, list(pool_tick.microvm_intents))
    if not pool_tick.ok:
        # A pushed intent waits with the degraded tick: no roster to execute against; its row stays pending.
        log.warning("reconcile: no roster this pass — publishing nothing and preserving existing keys")
        # Joined even here, or the next pass would start a second refresh; the memo is dropped so both joins
        # stay identical.
        if agents_refresh is not None:
            await agents_refresh
            pass_ctx.invalidate_agents()
    else:
        # One node-clock stamp for the pass, taken before the probes (reads older, the conservative
        # direction). Occupancy trust's freshness uses the CP's receipt time instead.
        observed_at = now_iso()
        # Wire state per slot: an entry spares a row; silence deletes it.
        entries: dict[tuple[str, str], dict] = {}
        slots_by_ref = {s.ref: s for s in pool_tick.slots}
        # (2) EXECUTE — before any publish, against the revisions the CP decided on.
        if pool_tick.intents or pushed:
            await execute.execute_intents(pass_ctx, slots_by_ref, pool_tick.intents, pushed=pushed)
        # No pre-build: a repository slot's tree is cloned at its lease (`lifecycle.claim_mechanics`), never
        # from the pass.
        # (2.9) Join the in-flight agents refresh, then drop the memo the execute arm latched, so the probes
        # and POST see the refreshed snapshot. Never raises.
        if agents_refresh is not None:
            await agents_refresh
            pass_ctx.invalidate_agents()
        # Prime the three shared snapshots first (see `_PROBE_CONCURRENCY_DEFAULT`), after the refresh join.
        await asyncio.gather(pass_ctx.agents(), pass_ctx.docker(), pass_ctx.tmux())
        # ONE /proc WALK for every slot in the roster, instead of one per slot inside `observe_slot`.
        await scan_pool_cwds(pass_ctx, [slot.path for slot in pool_tick.slots])
        probing = asyncio.Semaphore(_probe_concurrency())

        async def probe(slot: RosterSlot) -> tuple[tuple[str, str], dict]:
            async with probing:
                # The backstop drain; normally an empty queue.
                await drain_pushed_intents(pass_ctx, slots_by_ref, drain_pushed)
                return slot.ref, await _probe_slot_entry(slot, pass_ctx, pool_tick)

        for ref, entry in await asyncio.gather(*(probe(slot) for slot in pool_tick.slots)):
            entries[ref] = entry
        # (5) Account for every `known_slots` slot the filtered roster omitted — exactly what the complement
        # DELETE would otherwise widen onto.
        for known in pool_tick.known_slots:
            if known.ref in entries:
                continue
            entries[known.ref] = observe._unprobed_slot_entry(
                known.project, known.number, known.path, ever_observed=bool(known.observed_at),
            )

        # (6) POST, inside the `ok` branch by construction.
        observed = await observe.post_observation(
            node_id or pass_ctx.node_id or "", pool_tick, entries,
            observed_at=observed_at, ctx=pass_ctx,
            accounts=accounts, accounts_ok=accounts_ok,
            # Rides the same ~5s POST rather than a second node→CP path.
            host_metrics=host_metrics,
        )
        # (7) Erase what the retired pool layout left (`legacy_layout`), each slot tree under its enactment lock,
        # then the slot mirrors unused past their grace and an older daemon's per-project ones (`git_mirror.sweep`).
        try:
            await migrate_legacy_layout(pool_tick)
        except Exception as exc:  # noqa: BLE001 — a failed migration retries next pass; it must never fail this one
            log.warning("reconcile: legacy-layout migration failed: %s", exc)
        try:
            removed = await asyncio.to_thread(git_mirror.sweep)
            if removed:
                log.info("reconcile: removed unused git mirrors: %s", ", ".join(removed))
        except Exception as exc:  # noqa: BLE001 — a failed sweep retries next pass; it must never fail this one
            log.warning("reconcile: git-mirror sweep failed: %s", exc)

    # Node-level stamps are written by the POST's own node-row write
    # (`app/fleet_observe.py::node_stamp_statement`) from the CP's receipt time.
    return observed
