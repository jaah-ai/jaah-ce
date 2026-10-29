"""Executors that return a tree to base: `reset_to_base`, `stop_stack`, `release_slot`, `_clear_tree`."""
from __future__ import annotations

import asyncio
import functools
import logging

import control_plane_api
from worktree_mechanics import core, reap, reset
from env_helpers import now_iso
from reconcile import context
from reconcile.vocab import OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_REFUSED_PRECONDITION
from reconcile.tick_wire import RosterSlot, SLOT_FREE, SLOT_TASK, SLOT_USER, SlotIntent
from reconcile.context import ReconcileContext

log = logging.getLogger(__name__)


async def _stamp_slot_last_used(ctx: ReconcileContext, slot: RosterSlot) -> None:
    """Advance this slot's `worktrees.last_used` after a teardown (`release_slot`, `kill_runner`).

    Returns nothing: the hold is not the node's to end — both verbs are minted only after the CP freed it
    (`slot_state`). No git reset and no window kill here; those happen only where a verb asks, through
    `kill_gates.guarded_window_kill`. `last_used` has no other writer, so this keeps `age_last_used_secs`
    moving."""
    try:
        response = await asyncio.to_thread(
            functools.partial(
                control_plane_api.set_slot_holder,
                ctx.node_id or "", slot.project, slot.number, last_used=now_iso(), project_id=slot.project_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 — a stamp must never decide a teardown's outcome
        log.warning(
            "reconcile: could not stamp last_used for %s/%s (%s) — the teardown itself stands and the "
            "hold is the CP's; the column keeps its previous value",
            slot.project, slot.number, exc,
        )
        return
    if isinstance(response, dict) and response.get("mutated") == 0:
        # No sensed row to stamp; existence has one author, and it is not this call.
        log.warning(
            "reconcile: last_used stamp for %s/%s found no sensed slot row — nothing to stamp",
            slot.project, slot.number,
        )


async def _reset_to_base(ctx: ReconcileContext, slot: RosterSlot, branch: str | None, *,
                          detach_base: bool = False, own_branch: str | None = None,
                          abandon: bool = False, delete_branch: bool = False) -> None:
    """Enact `reset_to_base` through `worktree_mechanics.reset.reset_to_base`, the one destructive chokepoint, off
    the event loop. Its gates (unpushed commits; a dirty tree on its own identity branch, also on the detached
    path) raise `ResetRefused`, reported `refused_precondition` by `_enact_reset_to_base`.

    `abandon` waives only the dirty-tree gate (the operator's console Reset); unpushed commits are still
    tagged first. That label is diagnosis only; what prevents wedges elsewhere is the dirty gate's branch
    discrimination — do not weaken it, and do not widen `abandon` past its one caller."""
    await asyncio.to_thread(
        reset.reset_to_base,
        slot.pool_config(),
        slot.path,
        branch,
        fresh=False,
        detach_base=detach_base,
        own_branch=own_branch,
        abandon=abandon,
        delete_branch=delete_branch,
    )


async def _abandon_address_is_still_ours(
    ctx: "ReconcileContext", intent: SlotIntent, task_id: str,
) -> tuple[str | None, str]:
    """Prove, at the point of effect, that the slot an abandonment reset names is still the one the CP decided
    about. `(None, "")` to proceed, else the `(outcome, reason)` to return.

    The intent matches by `(project, number)` and the Reset freed the slot in the same commit, so another
    task or a human may hold it by drain time; without this the wipe lands on them (`_enact_kill_runner`
    keeps the same fence). The proof is the projection (`state`, `task_id`), not disk.
    `refused_precondition` for what may clear (projection unreachable, slot unobserved); `done` for what
    never will (someone else holds it), or it would re-arm every tick. Proceeds on `free` (nobody took it
    since) or still bound to our task."""
    records = await ctx.worktree_records()
    if records is None:
        return OUTCOME_REFUSED_PRECONDITION, (
            "the pool projection is unreachable — refusing to wipe a tree whose owner cannot be proven"
        )
    matched = context._matched_record(records, intent.project, intent.number)
    if matched is None:
        return OUTCOME_REFUSED_PRECONDITION, (
            "slot absent from this pass's projection — unobserved, not unowned"
        )
    state = str(matched.get("state") or "")
    holder = str(matched.get("task_id") or "")
    if state == SLOT_USER:
        # A human's claimed tree is never an abandonment's to wipe (`slot_state.py`).
        return OUTCOME_DONE, (
            "nothing wiped: the slot is user-owned now, and a human's tree is never an abandoned task's "
            "to reset"
        )
    if state != SLOT_FREE and holder != task_id:
        log.warning(
            "abandon: %s #%s is STALE — the slot is now %s, not task %s, so the binding this reset was "
            "minted for is gone. Retiring it untouched; wiping here would destroy whatever legitimately "
            "holds the slot now.",
            intent.project, intent.number, f"task {holder}" if holder else f"state {state!r}", task_id,
        )
        return OUTCOME_DONE, "nothing wiped: the slot is no longer bound to this task"
    if state not in (SLOT_FREE, SLOT_TASK):
        # An out-of-vocabulary state proves no owner: dropped (`done`, since it will not change).
        return OUTCOME_DONE, f"nothing wiped: the slot's state is {state!r}, which proves no owner"
    return None, ""


async def _enact_reset_to_base(ctx: "ReconcileContext", slot: RosterSlot, intent: SlotIntent,
                               obs: dict | None) -> tuple[str, str, dict]:
    """Reset a slot to its base.

    `DETACHED` is the ordinary shape of a fresh tree under D10 and is accepted; an absent `branch`
    is refused (nothing to act on); a real branch gets the ordinary reset.

    The abandonment arm (`payload["abandon"]`, the operator's console Reset) returns the tree to a clean
    detached base and deletes the `fleet/task-<id>` ref, derived node-side
    (`worktree_mechanics.lifecycle.claim_mechanics`). It is the one path that ends a session and rewrites a tree, so
    it carries both behind one proof of address, in order: `_abandon_address_is_still_ours`, `_clear_tree`,
    then git (why not two intents: `routes/tasks/slots.py::abandon_task_slots`)."""
    if obs is None:
        # Unstamped verbs do not refuse an absent tree; nothing to reset is `done`, as in
        # `_enact_release_slot`.
        return OUTCOME_DONE, "worktree absent on disk — nothing to reset", {}

    abandon = bool(intent.payload.get("abandon"))
    if abandon:
        task_id = str(intent.payload.get("task_id") or "").strip()
        if not task_id:
            # `failed`: the CP sent something unusable, so there is no precondition to refuse about (neither
            # retires).
            return OUTCOME_FAILED, (
                f"payload asks to abandon but names no task_id — the branch to delete is DERIVED from it "
                f"and this node has nothing else to derive it from (got keys {sorted(intent.payload)})"
            ), {}
        blocked, why = await _abandon_address_is_still_ours(ctx, intent, task_id)
        if blocked is not None:
            return blocked, why, {}
        # Teardown first (`_clear_tree`): a `git reset --hard` under a live runner gets written straight back
        # over.
        await _clear_tree(ctx, slot.path, f"abandon {intent.project} #{intent.number} (task {task_id})")
        if not slot.has_repo:
            # A folder has no base and no branch: ending the session is the whole abandon. Never git here —
            # run from a folder, git would climb into whatever repository sits above it.
            await _stamp_slot_last_used(ctx, slot)
            return OUTCOME_DONE, "folder cleared — a folder slot has no base to reset to", {}
        # Always detached; the derived branch rides as `own_branch`, the identity the destructive gates key
        # off (as in a D10 dispatch).
        prefix = slot.pool_config().branch_prefix
        reset_branch, detach, own_branch = None, True, f"{prefix}fleet/task-{task_id}"
    else:
        if not slot.has_repo:
            return OUTCOME_DONE, "a folder slot has no base to reset to — left as it is", {}
        branch = obs.get("branch")
        if not branch:
            return OUTCOME_REFUSED_PRECONDITION, f"no branch to reset onto (observed {branch!r})", {}
        detach = branch == "DETACHED"
        reset_branch, own_branch = (None if detach else branch), None
    try:
        # `delete_branch` tracks `abandon`; separate parameters so no other caller inherits either.
        await _reset_to_base(ctx, slot, reset_branch, detach_base=detach, own_branch=own_branch,
                             abandon=abandon, delete_branch=abandon)
    except core.ResetRefused as exc:
        # Explicit, so a considered veto and a git crash do not look the same CP-side (both re-served).
        return OUTCOME_REFUSED_PRECONDITION, f"reset refused: {exc}", {}
    if abandon:
        # As `_enact_release_slot`: `last_used` has no other writer.
        await _stamp_slot_last_used(ctx, slot)
    return OUTCOME_DONE, "", {}


async def _enact_stop_stack(ctx: "ReconcileContext", slot: RosterSlot, intent: SlotIntent, obs: dict
                            ) -> tuple[str, str, dict]:
    """Enact `stop_stack` — the mechanic of the idle stop, none of its policy. Preconditions were re-verified
    upstream; the down command is `payload["stack_down_cmd"]`, applied verbatim.

    No command is `failed` (nothing to refuse a precondition about). `stop_stack` returns the resulting
    state and never raises, so a stack still up is `failed`, left pending. Imported inside the function:
    `stack_ops` → `commands` → `reconcile` is a cycle."""
    import stack_ops  # noqa: PLC0415 — deferred: see the docstring's import-cycle note

    if not slot.has_repo:
        # A folder slot owns no stack; a command run there would act on whatever the runner left in it.
        return OUTCOME_DONE, "a folder slot has no stack to stop", {}
    down_cmd = str(intent.payload.get("stack_down_cmd") or "").strip()
    if not down_cmd:
        return OUTCOME_FAILED, (
            f"payload names no stack_down_cmd — this node has no down command of its own to fall back on "
            f"(got keys {sorted(intent.payload)})"
        ), {}
    state = await asyncio.to_thread(stack_ops.stop_stack, slot.path, down_cmd)
    if state == "down":
        return OUTCOME_DONE, "", {}
    # Not "after <cmd>": every `stop_stack` refusal arm returns "up" having run nothing, and the reason is
    # only in the node log.
    return OUTCOME_FAILED, (
        f"the stack was not brought down by {down_cmd!r} — it is still {state!r} (the node's log says "
        f"whether the command ran and failed or was refused unrun)"
    ), {}


async def _clear_tree(ctx: "ReconcileContext", path: str, what: str, *,
                      warrant_runs_before: str | None = None) -> tuple[int, int]:
    """`_clear_tree_detail`, counted: `(processes signalled, windows torn down)`."""
    swept, targets = await _clear_tree_detail(ctx, path, what, warrant_runs_before=warrant_runs_before)
    return len(swept), len(targets)


async def _clear_tree_detail(ctx: "ReconcileContext", path: str, what: str, *,
                             warrant_runs_before: str | None = None) -> tuple[list, tuple]:
    """Force-kill everything in one pool tree: every process whose cwd is inside `path` (docker excepted),
    then every tmux window in it. Returns `(reap records, window targets)`.

    One enactment for three CP decisions — `destroy_slot` too, which reports what it killed —: `release_slot` (ownership ends) and `kill_runner` (the task keeps
    the slot and restarts in it). It asks nothing about what is in there — the CP decided; the node makes
    it true. The agents probe only names targets the cwd scan cannot see (a registered session with nothing
    rooted in the tree once survived its own release): every session against the path rides as
    `extra_pids`, each a `(pid, proc_started_at)` pair re-verified before signalling. An unjudged probe
    names nothing and stops nothing. Docker is exempt from the cwd-derived half
    (`worktree_mechanics.reap._is_docker_process`) so a warm compose stack survives; a warranted pid is never a
    docker daemon."""
    import kill_gates  # noqa: PLC0415 — deferred: subprocess-heavy sibling, needed only on these verbs

    # Occupants the cwd scan cannot see (from the per-tick snapshot when cached). Every registered session,
    # not the first (a resumed runner beside its predecessor is two), and only as a `(pid, proc_started_at)`
    # pair — `reap_orphan_processes` re-asserts it before signalling, so a session with no readable start is
    # not warranted.
    # `warrant_runs_before` is the reap's per-session fence: None warrants every session (`kill_runner`, an
    # ordinary release, where the CP still owns the hold). A reap's slot is `free`, so anything may be in it:
    # only runs started before the release instant are warranted, so a bystander (an operator's SSH) is not
    # killed on the survivor's ticket. `""` warrants nothing (fail-closed for an older CP).
    agents_ok, agents_json, _agent_cwds = await ctx.agents()
    metas = context._session_metas_for(path, agents_json) if agents_ok else []
    extra_pids = tuple(
        (m["pid"], m["proc_started"]) for m in metas
        if m["pid"] and m["proc_started"]
        and (warrant_runs_before is None or m["proc_started"] < warrant_runs_before)
    )
    # Every process in the tree: the sweep's wide mode, not a second sweep.
    swept = await asyncio.to_thread(
        functools.partial(reap.reap_orphan_processes, path, orphans_only=False,
                          extra_pids=extra_pids)
    )
    # Windows after processes. `kill_gates` still resolves the stable `window_id` (hitting the right window
    # under renumbering, not whether to obey); resolved off the path, so `kill_runner` need not trust the CP's
    # `tmux_target`.
    targets = await context.wt_tmux_targets(path, ctx)
    for target in targets:
        await asyncio.to_thread(
            kill_gates.guarded_window_kill, str(target), what, expect_cwd=path,
        )
    log.info(
        "%s: %s cleared — %d process(es) signalled, %d window(s) torn down",
        what, path, len(swept or []), len(targets),
    )
    return list(swept or []), tuple(targets)


async def _enact_release_slot(ctx: "ReconcileContext", slot: RosterSlot, intent: SlotIntent,
                              obs: dict | None) -> tuple[str, str, dict]:
    """Enact `release_slot` — clear the tree (`_clear_tree`), sync it back to base, and stamp the slot.

    The sync lives here because `release_slot` is minted on every hold-ending edge, making "a free slot is at
    base" true everywhere with no new verb (a node-side convergence sweep would decide on its own initiative,
    which a node never does). Order: `_clear_tree` first, or a process in the tree writes back over the reset (docker
    is exempt, so a watching dev server can still race it — accepted). The sync never raises and never
    changes the `done` outcome; what it could not do rides the ack.

    `reap: True` is a different order (from `lease_drain._reap_occupied_free_slots`, for a slot already
    `free` whose tree was never emptied): fenced by `_release_sync_address_is_still_free` before the sweep
    (it re-orders every 10 minutes, and a new runner reports a live session exactly as the survivor did),
    and never synced (an unkillable occupant would get `reset --hard` every ten minutes). Keyed on the
    explicit marker, never on a missing `task_id` (a `user` hold's teardown has none)."""
    if obs is None:
        # No tree: only the `last_used` stamp is left (the CP already freed the hold).
        await _stamp_slot_last_used(ctx, slot)
        return OUTCOME_DONE, "worktree absent on disk — nothing to tear down", {}

    # `is True` only: a destructive fork is never inferred from a missing key or a truthy value. The
    # projection read is memoised, so asking twice costs one GET.
    is_reap = intent.payload.get("reap") is True
    if is_reap:
        retaken = await _release_sync_address_is_still_free(ctx, intent)
        if retaken:
            # `done`, not refused: the reap is convergent (the CP re-decides next pass), and a refusal would
            # sit pending until parked.
            await _stamp_slot_last_used(ctx, slot)
            return OUTCOME_DONE, f"tree not cleared: {retaken.removeprefix('tree not synced: ')}", {}

    what = f"release_slot {slot.project} #{slot.number}"
    if not is_reap:
        # A settled task's release can reach the node before any `kill_runner` (lease-drain waits only on a kill
        # already minted), with its ACP runner still mid-turn: close it first, as the kill does, or the turn's
        # tokens — reported only in its result — are swept with the engine.
        from reconcile import enact_signal  # noqa: PLC0415 — deferred: enact_signal imports this module
        task_id = intent.payload.get("task_id")
        await enact_signal._close_acp_before_kill(slot.path, None if task_id is None else str(task_id), what)
    procs, windows = await _clear_tree(
        ctx, slot.path, what,
        # A reap carries its fence; `""` for a missing stamp warrants nothing, and `str()` keeps a non-string
        # from wedging a verb that must always retire.
        warrant_runs_before=str(intent.payload.get("freed_at") or "") if is_reap else None,
    )
    # The runner is gone now: send what its hooks spooled and never shipped (a no-op for an OFF runner).
    import fleet_dispatch  # noqa: PLC0415 — deferred, as in `enact_launch._launch_into_slot`
    import project_memory_bundle  # noqa: PLC0415 — deferred with it
    project_memory_bundle.ship_orphan_spool_later(slot.path, hub_root=str(fleet_dispatch.HUB_ROOT))
    if is_reap:
        # A reap sweeps and stops: the slot was synced when genuinely released.
        await _stamp_slot_last_used(ctx, slot)
        return OUTCOME_DONE, (
            f"tree cleared — {procs} process(es) signalled, {windows} window(s) torn down; not synced to "
            f"base: a reap sweeps only, and re-ordering the sync every 10 minutes while an occupant "
            f"survives would reset the tree out from under whatever is put in it"
        ), {}

    if not slot.has_repo:
        # A folder has no base to sync to: cleared is the whole release. Its files stay until the CP's
        # `destroy_slot` deletes the folder (a freed dynamic slot is destroyed).
        await _stamp_slot_last_used(ctx, slot)
        return OUTCOME_DONE, (
            f"folder cleared — {procs} process(es) signalled, {windows} window(s) torn down; a folder slot "
            f"has no base to sync to"
        ), {}

    blocked = await _release_sync_address_is_still_free(ctx, intent)
    if blocked:
        await _stamp_slot_last_used(ctx, slot)
        return OUTCOME_DONE, blocked, {}
    synced = await asyncio.to_thread(
        reset.sync_released_to_base, slot.pool_config(), slot.path,
    )
    await _stamp_slot_last_used(ctx, slot)
    return OUTCOME_DONE, synced, {}


async def _release_sync_address_is_still_free(ctx: "ReconcileContext", intent: SlotIntent) -> str:
    """Is the slot this release names still unheld? `""` to sync, else the one line saying why not.

    A pending `release_slot` matches by `(project, number)` with no generation fence, and the drain re-leases
    freed capacity in the same pass — a late sync would wipe a fresh lease's tree and prune its new branch.
    For a hold-ending release this guards the sync alone (the sweep's older exposure is a separate decision,
    `routes/pool_slots.py`); for a reap it runs before the sweep (the fork is in `_enact_release_slot`).

    `state` from the CP's projection is the whole test (as `_abandon_address_is_still_ours`), but every
    answer is a reason string: a release always retires `done`. Unreachable reads as held — an un-synced
    tree is collected by the next release; a wiped live lease is not recoverable."""
    records = await ctx.worktree_records()
    if records is None:
        return "tree not synced: the pool projection is unreachable, so this slot's owner cannot be proven"
    matched = context._matched_record(records, intent.project, intent.number)
    if matched is None:
        return "tree not synced: slot absent from this pass's projection — unobserved, not unowned"
    state = str(matched.get("state") or "")
    if state != SLOT_FREE:
        log.info(
            "release sync: %s #%s was re-taken (state %r) between the CP's release and this enactment — "
            "leaving the tree alone; syncing it now would wipe whatever holds the slot NOW.",
            intent.project, intent.number, state,
        )
        return f"tree not synced: the slot is {state!r} again — re-taken since the release was decided"
    return ""
