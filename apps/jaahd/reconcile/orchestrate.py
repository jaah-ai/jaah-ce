"""The reconcile passes: fetch → execute → probe → publish, the launch lane, materialisation, mirror sweep."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
from typing import Awaitable, Callable, Sequence

import cloud_modules
import host_platform
import git_mirror
import pool_origins
from worktree_mechanics import core, processes, lifecycle
from env_helpers import now_iso
from reconcile import vocab, tick_wire, context, observe, execute
from reconcile.vocab import (
    CONTAINER_ABSENT, CONTAINER_UNREADABLE, VERB_DELIVER_ANSWER, VERB_DELIVER_MESSAGE, VERB_LEASE_SLOT,
)
from reconcile.tick_wire import RosterSlot, SLOT_FREE, SLOT_TASK, SlotIntent
from reconcile.context import ReconcileContext

# The MicroVM marker (D0); None on a community-edition node, which is never a MicroVM node.
microvm_mark = cloud_modules.load("microvm_mark")
microvm_bridge = cloud_modules.load("microvm_bridge")

log = logging.getLogger(__name__)


def sweep_pool_mirrors(tick: tick_wire.PoolTick) -> list[str]:
    """Remove the static pool's mirrors (`git_mirror.sweep_shared`) of projects and repositories this tick's
    roster no longer names → the names removed. A removed project's mirror holds its whole history,
    session-group readable, and a second copy of its clone on disk.

    Fail-closed: nothing goes unless the tick is `ok`, its roster parsed whole (`roster_complete`), and every
    known slot's project is on it — a project still holding a slot the roster filtered out is one whose id
    this node cannot read, so it may yet be the owner of any mirror."""
    if not tick.ok or not tick.roster_complete:
        return []
    if any(known.project not in tick.projects for known in tick.known_slots):
        return []
    keep = {
        proj.project_id: (frozenset(spec.id for spec in proj.repos) if proj.repos else None)
        for proj in tick.projects.values() if proj.project_id is not None
    }
    ids_complete = all(proj.project_id is not None for proj in tick.projects.values())
    removed = git_mirror.sweep_shared(keep, ids_complete=ids_complete)
    if removed:
        log.info("reconcile: removed pool mirrors the roster no longer names: %s", ", ".join(removed))
    return removed


# ── Convergence: declared → materialised ──────────────────────────────────────
def _materialisation_budget() -> int:
    """How many declared-but-unbuilt slots one pass may build, read at call time. `0` disables convergence
    (the kill switch for a misbehaving remote)."""
    return vocab._env_int("POOL_MATERIALISE_PER_PASS", 1)


def _unmaterialised_candidates(pool_tick: tick_wire.PoolTick) -> list[RosterSlot]:
    """This pass's declared-but-unbuilt slots, in roster order.

    Excluded: `is_main` (the main clone materialises as the first slot's clone rung, `ensure_clone`); an
    existing directory; a state other than `free`/`task` (`user` is a human's tree; `""` fails closed).
    Included regardless of `observed_at`: a row is a human's capacity declaration, so a vanished
    tree is rebuilt, empty (the CP alerts: `app/fleet_observe.py::vanished_hold_statement`). `task` is
    included because only that task's own edge frees the slot and the resume leg never builds a tree.
    No race with dispatch: `execute_intents` completes before this runs.

    Excluded too: a slot this daemon just erased. `pool_slots.py::delete_one_slot` deletes the row first
    and the tree second, so a roster fetched before that commit would rebuild a tree for a row already
    gone. `worktree_mechanics.core.was_just_erased` is a short-lived in-process note, not a lock."""
    return [
        slot for slot in pool_tick.slots
        if not slot.is_main
        and slot.state in (SLOT_FREE, SLOT_TASK)
        and not os.path.isdir(slot.path)
        and not core.was_just_erased(slot.path)
        # Ordered destroyed (`destroy_slot`): rebuilding it would hand a retry an empty tree to report on.
        and not slot.retiring
    ]


def _tick_roster(pool_tick: tick_wire.PoolTick, project: str) -> dict[int, dict]:
    """One project's declared slots as `{number: record}` for the mechanics, from this pass's tick (not a
    second `roster_from_db` read that could also be unavailable)."""
    return {
        int(slot.number): {"number": int(slot.number), "path": slot.path}
        for slot in pool_tick.slots
        if slot.project == project and str(slot.number).isdigit()
    }


async def materialise_declared_slots(
    ctx: ReconcileContext, pool_tick: tick_wire.PoolTick, *, node_id: str | None = None,
) -> list[str]:
    """Make the host match the CP for at most `_materialisation_budget()` declared slots. Returns the paths
    built; never raises into the pass.

    The CP dictates which slots exist, the node builds them (the `create` verb's op, not a second
    implementation). Bounded: a first clone is minutes of network. Failures are logged per slot; unbuilt
    rows are reported `unmaterialised` and spared."""
    budget = _materialisation_budget()
    if budget <= 0:
        return []
    built: list[str] = []
    for slot in _unmaterialised_candidates(pool_tick):
        if len(built) >= budget:
            log.info(
                "reconcile: materialisation budget (%d) spent — %s #%s and any further declared slots stay "
                "unmaterialised this pass and are retried next pass",
                budget, slot.project, slot.number,
            )
            break
        project = pool_tick.projects.get(slot.project)
        config = project.pool_config() if project is not None else slot.pool_config()
        try:
            record = await asyncio.to_thread(
                lifecycle.create_mechanics,
                config, number=slot.number, node_id=node_id,
                roster=_tick_roster(pool_tick, slot.project),
                band=dict(slot.ports),
                # From this tick, like the band, so one `.env` write is sourced from one answer.
                auto_ports=pool_tick.auto_ports,
            )
        except Exception as exc:  # noqa: BLE001 — a build failure must never wedge the probe/publish halves
            log.warning(
                "reconcile: could not materialise declared slot %s #%s at %s: %s — it stays unmaterialised "
                "(row spared) and is retried next pass",
                slot.project, slot.number, slot.path, exc,
            )
            continue
        path = str((record or {}).get("path") or slot.path)
        log.info("reconcile: materialised declared slot %s #%s at %s", slot.project, slot.number, path)
        built.append(path)
    return built


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
    ctx.adopt_cwd_scans(dict(zip(wanted, scans)))


async def reconcile_slot(
    slot: RosterSlot,
    ctx: ReconcileContext,
    *,
    fetch_stamp_age_secs: int | None = None,
) -> dict | None:
    """Observe one slot; None when absent on disk. The pass sends all observations in one POST.

    `fetch_base_if_stale` first, so ahead/behind reads a refreshed `origin/<base>`; `fetch_stamp_age_secs`
    None means "never fetched" and fetches."""
    await context.fetch_base_if_stale(slot, age_secs=fetch_stamp_age_secs, fetched=ctx.fetched)
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
) -> tuple[dict, ...]:
    """Enact one fast lane's pushed intents (`LAUNCH_LANE_VERBS` or `SIGNAL_LANE_VERBS`) and nothing else — no
    probes, no POST, no projection write. The lane a human pressing Run, or answering a question, waits on (it
    otherwise waited out a whole in-flight pass holding the lock).

    The caller owns exclusion between launch passes (`main.NodeStatePublisher`); per-tree exclusion against a
    probing pass is `execute_intent`'s `slot_lock`.

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
        await execute.execute_intents(ctx, {s.ref: s for s in tick.slots}, (), pushed=tuple(runnable))
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
    return max(1, vocab._env_int("JAAHD_PROBE_CONCURRENCY", _PROBE_CONCURRENCY_DEFAULT))


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
        project = tick.projects.get(slot.project)
        observation = await reconcile_slot(
            slot, ctx,
            fetch_stamp_age_secs=project.fetch_stamp_age_secs if project else None,
        )
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
    # Before the execute and converge arms, which allocate tier-2 ports: how an operator's grid edit reaches
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
        # (1.5) Each clone's `origin` follows a changed Repo URL (a repo rename), before anything fetches.
        await pool_origins.repoint_origins(pool_tick)
        slots_by_ref = {s.ref: s for s in pool_tick.slots}
        # (2) EXECUTE — before any publish, against the revisions the CP decided on.
        if pool_tick.intents or pushed:
            await execute.execute_intents(pass_ctx, slots_by_ref, pool_tick.intents, pushed=pushed)
        # (2.5) CONVERGE — build at most one slot the CP declared and this host has not materialised.
        await materialise_declared_slots(pass_ctx, pool_tick, node_id=node_id or pass_ctx.node_id)
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
        # (7) The roster-driven cleanup of what a removed project left beyond its slots: its git mirrors.
        try:
            await asyncio.to_thread(sweep_pool_mirrors, pool_tick)
        except Exception as exc:  # noqa: BLE001 — a failed sweep retries next pass; it must never fail this one
            log.warning("reconcile: pool-mirror sweep failed: %s", exc)

    # Node-level stamps are written by the POST's own node-row write
    # (`app/fleet_observe.py::node_stamp_statement`) from the CP's receipt time.
    return observed
