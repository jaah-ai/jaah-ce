"""The observation encoder (R1): one slot's sensed record and the `POST /api/agent/fleet/observe` payload."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import shutil
import time

import cloud_modules
import control_plane_api
import host_platform
import slot_repos
from worktree_mechanics import ports as wm_ports, processes, reap
from env_helpers import now_iso
from runner_clis import sense_runner_clis
from reconcile import vocab, tick_wire, context
from reconcile.vocab import CONSOLE_PROBE_UNAVAILABLE, CONTAINER_PRESENT, CONTAINER_UNREADABLE, OBSERVATION_VERSION
from reconcile.tick_wire import RosterSlot, SLOT_FREE, SLOT_TASK, SLOT_USER
from reconcile.context import ReconcileContext

log = logging.getLogger(__name__)


# Host knobs in `$JAAH_HOME/env` are invisible to the CP container, so the raw value is forwarded (absent =
# no override).
_FORWARDED_HOST_KNOBS = ("POOL_STALE_HOURS", "POOL_LEASE_GRACE_SECS")


# ── The observation encoder (R1) ──────────────────────────────────────────────
def _n(v: str) -> str | None:
    """"" → None."""
    return None if v == "" else v


def _ok(flag: bool, value):
    """The R1 invariant: a value is emitted only when its probe flag says the probe succeeded. Every
    probe-derived field goes through this."""
    return value if flag else None


def _host_env() -> dict:
    """Host values of CP-decided thresholds, forwarded so an override survives into the CP container."""
    out: dict[str, str] = {}
    for name in _FORWARDED_HOST_KNOBS:
        raw = os.environ.get(name)
        if raw:
            out[name] = raw
    return out


async def observe_slot(
    ctx: ReconcileContext,
    slot: RosterSlot,
) -> dict | None:
    """Probe one slot and encode its observation record — sensed fields only. None when the worktree does not
    exist on disk. Decided fields belong to `fleet_decide.py`; `merged` is sensed here.

    `last_used` is not re-emitted (`POST /api/agent/slot-holder` is its sole writer); `age_last_used_secs`
    ships instead, None when absent, never `0`. `session` is a default-CLI session only; processes merely
    rooted in the tree go in `probes` and are the orphan reap's subject."""
    wt = slot.path
    if not os.path.isdir(wt):
        return None
    # `cpn_of`'s fallback, from `repo_path`'s last segment (never `wt`'s basename or the display name), matching
    # `apply_stack_identity`.
    slot_repo = (slot.repo_path or "").rstrip("/").rsplit("/", 1)[-1] or slot.project
    slot_identity = slot_repo if slot.is_main else f"{slot_repo}-{slot.number}"

    # ── git ────────────────────────────────────────────────────────────────────
    if slot.has_repo:
        branch = await context.probe_wt_branch(wt)
        dirty = await context.wt_dirty(wt)
        merged = await context.branch_merged(wt, slot.base)
        ab = await context.wt_ahead_behind(wt, slot.base)
        unpushed = await context.wt_unpushed(wt)
        head_sha, last_commit = await context.wt_head_and_last_commit(wt)
        index_mtime = await context.wt_index_mtime(wt)
    else:
        # A folder slot is never a git tree: no git runs, and every git field ships null. Null is the
        # truthful reading AND the safe one — the CP's "nothing to lose" tests require `dirty IS FALSE`, so
        # a folder's files are never read as clean (`app/slot_state.py`).
        branch = dirty = merged = ab = unpushed = head_sha = last_commit = index_mtime = None
    branch_ok = branch is not None
    dirty_ok = dirty is not None
    merged_ok = merged is not None
    ahead_behind_ok = ab is not None
    behind, ahead = ab if ab is not None else (None, None)

    # ── agents ─────────────────────────────────────────────────────────────────
    agents_ok, agents_json, agent_cwds = await ctx.agents()
    live = context.live_session_in(wt, agent_cwds) if agents_ok else False
    meta = context._session_meta_for(wt, agents_json) if (agents_ok and live) else None
    # A CLI with no session list is never named by the session-list probe: its launched pane IS the
    # sighting (`session_sweep.VendorLiveSessions` is the ledger's twin). Without it the CP reads the live
    # runner DEAD at `LAUNCH_SETTLE_SECONDS` and redispatches it every few minutes. An ACP run's pane wins even
    # over a roster entry: what the session-list probe may list there is the engine's own CLI (its ACP agent's),
    # whose pid and id are not the launch's, so the CP would rule the run DEAD and the reap below kill it
    # within a pass.
    if agents_ok and slot.state == SLOT_TASK:
        pane = context._vendor_runner_meta(wt)
        if pane is not None and (not live or pane[1]):
            live, meta = True, pane[0]

    # Any live process rooted in this tree. Does not vote on `session`; it is the reap's target set and the
    # operator's `session_cwd`/`cwd_processes`, in `probes` (an old node leaves the keys absent).

    # One /proc walk for the pool (`scan_pool_cwds`), taken before this loop: per-slot scans cost 8.8s a pass
    # on 23 slots. Occupants are reported only for a `free` slot (`== SLOT_FREE`, so `""` ships the bare
    # verdict): the list is operator-facing, asked only about a slot that will not go clean.
    collect_occupants = slot.state == SLOT_FREE
    cwd_scan = await ctx.cwd_scan(wt)
    if not collect_occupants:
        cwd_scan = processes.CwdScan(cwd_scan.status)
    cwd_ok = cwd_scan.status != processes.PROBE_FAILED
    cwd_busy = cwd_ok and bool(cwd_scan.status)

    # The orphan reap: the agents probe ran, no session claims this tree, yet processes are rooted in it.
    # Hygiene, not the restart path (`_DEATH_PREDICATE` reads the agent half), but the only thing that answers
    # debris, so a reap that cannot kill is a silent leak (`reap_orphan_processes` swallows per-pid
    # `OSError`).
    # Never on the main clone (a human's shell reads like debris) or a `user`-owned slot: a human's own
    # processes would be SIGKILLed within a pass — "while owned, nobody touches the slot".
    if agents_ok and not live and cwd_busy and not slot.is_main and slot.state != SLOT_USER:
        reaped = await asyncio.to_thread(reap.reap_orphan_processes, wt)
        if reaped:
            log.warning(
                "reconcile: reaped %d orphaned process(es) rooted in %s (%s#%s, state=%s) — no "
                "default-CLI session claimed the tree: %s",
                len(reaped), wt, slot.project, slot.number, slot.state or "unknown",
                "; ".join(f"{r['signal']} {r['pid']} {r['cmd']}" for r in reaped),
            )
            # Re-scan so this observation reports the post-reap tree; directly, not via the pre-reap memo,
            # which is replaced too.
            cwd_scan = await asyncio.to_thread(
                functools.partial(processes.scan_cwd_processes, wt, collect=collect_occupants),
            )
            ctx.note_cwd_scan(wt, cwd_scan)
            cwd_ok = cwd_scan.status != processes.PROBE_FAILED
            cwd_busy = cwd_ok and bool(cwd_scan.status)

    # `session` means a default-CLI session only — who the CP put here, not what happens to be running
    # (orphaned heredocs once held free slots `session=true` for days). The cwd scan stays for the reap and the
    # operator view.
    session_val: bool | None = live if agents_ok else None

    # ── docker / stack ─────────────────────────────────────────────────────────
    stack_present = wm_ports.has_stack(wt)
    docker_ok, docker_names = await ctx.docker()
    # `stack_up` is null for no stack and for a failed probe; `stack_present` disambiguates.
    stack_up = _ok(docker_ok and stack_present, context.stack_running(wt, docker_names, slot_identity))

    # ── tmux ───────────────────────────────────────────────────────────────────
    tmux_ok, _panes = await ctx.tmux()
    tmux_target = await context.wt_tmux_target(wt, ctx) if tmux_ok else ""
    # A subprocess, so only for a slot with a pane.
    window_id = await context.fresh_window_id_for(wt) if tmux_target else ""

    # ── console (tri-state) ────────────────────────────────────────────────────
    console = ctx.console_state(slot.project, slot.number)
    console_ok = console != CONSOLE_PROBE_UNAVAILABLE

    # The agents cache's fast lane: a resolved session is the evidence, regardless of slot state. A `free`
    # slot's lingering runner must be on the 5s floor too, or `_clear_tree`'s reap warrant would come from an
    # hour-old sighting.
    if meta and meta["account"]:
        ctx.note_leased_account(meta["account"])

    # The stall detector's transcript signal (CP `task_pipeline_cp.list_silent_active_tasks`), for a task-held slot
    # only: the session's own account when sighted, else every probed one, since a wrong session flag is the case it
    # guards.
    transcript_age = (
        await asyncio.to_thread(
            transcript_write_age_secs, wt, [meta["account"]] if meta and meta["account"] else ctx.config_dirs,
        )
        if slot.state == SLOT_TASK else None
    )

    return {
        "v": OBSERVATION_VERSION,
        "observed_at": now_iso(),
        "probes": {
            "branch_ok": branch_ok,
            "dirty_ok": dirty_ok,
            "merged_ok": merged_ok,
            "ahead_behind_ok": ahead_behind_ok,
            # In the untyped `probes` jsonb beside its ok flag: no wire field or migration, and an old node's
            # absent key reads unknown, holding the slot.
            "unpushed_ok": unpushed is not None,
            "unpushed": unpushed,
            # Two independent probes, each with its own unknown: `session_agents` decides `session`;
            # `session_cwd` is what is rooted in the tree. None = "could not judge", never "clear".
            "session_agents": live if agents_ok else None,
            "session_cwd": cwd_busy if cwd_ok else None,
            # Bounded and redacted at the sensor (`worktree_mechanics.processes._cmd_summary`): argv can carry a
            # credential. `cwd_process_count` is the true total, beyond the cap.
            "cwd_processes": [dict(p) for p in cwd_scan.processes],
            "cwd_process_count": cwd_scan.matched,
            "agents_ok": agents_ok,
            "docker_ok": docker_ok,
            "tmux_ok": tmux_ok,
            "console_ok": console_ok,
        },
        "probed_config_dirs": ctx.config_dirs,
        # Slot identity: not a probe result, but every reader resolves the tree through `path`.
        "path": wt,
        "project": os.path.basename(wt),
        "branch": _ok(branch_ok, branch),
        "dirty": _ok(dirty_ok, dirty),
        "merged": _ok(merged_ok, merged),
        "ahead": _ok(ahead_behind_ok, ahead),
        "behind": _ok(ahead_behind_ok, behind),
        "head_sha": _n(head_sha),
        "session": session_val,
        "session_id": _ok(agents_ok, _n(meta["id"]) if meta else None),
        "session_status": _ok(agents_ok, _n(meta["status"]) if meta else None),
        "session_account": _ok(agents_ok, _n(meta["account"]) if meta else None),
        "session_started_at": _ok(agents_ok, _n(meta["started"]) if meta else None),
        # Runner identity: the CP re-verifies `(pid, proc_started_at)`; a pid alone is reused.
        "session_pid": _ok(agents_ok, meta["pid"] if meta else None),
        "session_proc_started_at": _ok(agents_ok, _n(meta["proc_started"]) if meta else None),
        "console": console,
        # Ages, never stamps.
        "age_last_used_secs": vocab._age_secs(slot.last_used),
        "age_last_commit_secs": vocab._age_secs(last_commit),
        "age_index_mtime_secs": vocab._age_secs(index_mtime),
        "age_transcript_write_secs": transcript_age,
        "tmux_window_id": _ok(tmux_ok, _n(window_id)),
        "tmux_target": _ok(tmux_ok, _n(tmux_target)),
        "compose_project_name": context.cpn_of(wt, slot_identity),
        "stack_present": stack_present,
        "stack_up": stack_up,
        "ports_observed": wm_ports.wt_ports_json(wt, slot.port_vars),
        "host_env": _host_env(),
        # A multi-repo slot's manifest as its last session-start converge wrote it (the drawer's repos, kept and
        # failed rows); None for a one-repository or folder slot. Read, never converged, here (N8).
        "repos": slot_repos.observed(await asyncio.to_thread(slot_repos.read_manifest, wt)) if slot.has_repo else None,
    }


def _newest_jsonl(directory: str) -> tuple[float, str] | None:
    """`(mtime, path)` of the newest `*.jsonl` directly in `directory`; None when there is none. One `scandir`, no
    sort: a reused slot's project dir holds every past session's transcript."""
    newest = None
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if newest is None or mtime > newest[0]:
                    newest = (mtime, entry.path)
    except OSError:
        return None
    return newest


def transcript_write_age_secs(wt: str, config_dirs: list[str]) -> int | None:
    """Seconds since tree `wt`'s newest session transcript under any of `config_dirs` was written — the session's
    own file or one of its subagents' (a main thread waiting on a subagent writes nothing itself) — on this node's
    clock; None when none resolves (ages only: the CP subtracts)."""
    import session_sweep  # noqa: PLC0415 — heavy; holds the path-loaded `break_cause`

    newest = None
    for config_dir in config_dirs:
        proj = session_sweep.break_cause.project_dir(cwd=wt, config_dir=config_dir)
        session_file = _newest_jsonl(proj)
        if session_file is None:
            continue
        subagents = _newest_jsonl(os.path.join(proj, os.path.basename(session_file[1])[:-len(".jsonl")], "subagents"))
        for mtime, _path in filter(None, (session_file, subagents)):
            newest = mtime if newest is None or mtime > newest else newest
    return None if newest is None else max(0, int(time.time() - newest))


# One POST per pass with a state for every accountable slot: the only writer of every sensed
# `worktrees`/`runners` column and of `nodes.worktrees_updated_at`/`nodes.runner_clis`. Not optional:
# `pool_state` is False without a recent complete enumeration and `fleet_drain` then refuses to dispatch. The
# CP stamps its receipt time (a pass takes 10-12s).

# Keys shared by name with `ObserveSlotIn`. Explicit, because the record's `project` (a directory basename),
# `session_account` (`account` on the wire) and `v`/`observed_at` must not travel as-is.
_OBSERVE_WIRE_FIELDS = (
    "path", "branch", "dirty", "merged", "ahead", "behind", "head_sha",
    "session", "session_id", "session_status", "session_started_at",
    "session_pid", "session_proc_started_at",
    "console", "probes", "probed_config_dirs",
    "age_last_used_secs", "age_last_commit_secs", "age_index_mtime_secs", "age_transcript_write_secs",
    "tmux_window_id", "tmux_target",
    "compose_project_name", "stack_present", "stack_up", "ports_observed",
    "host_env", "repos",
)


def _wire_number(value: object) -> int | None:
    """A slot number as the wire's `int`, or None. Never coerced: `_observation_payload` drops the entry
    and loses completeness, so the CP skips the complement DELETE."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _observed_slot_entry(slot: RosterSlot, observation: dict) -> dict:
    """`present` — this pass probed the slot and carries its sensed values."""
    entry = {name: observation.get(name) for name in _OBSERVE_WIRE_FIELDS}
    entry.update({
        "project": slot.project,
        "number": _wire_number(slot.number),
        "state": control_plane_api.OBSERVE_SLOT_PRESENT,
        "account": observation.get("session_account"),
    })
    return entry


def _slot_entry(state: str, project: str, number: str, path: str, reason: str) -> dict:
    """An unprobed slot's wire entry: `state` with a required diagnostic `reason`.
      * `unobserved` — the node could not judge this slot: its row is spared, nothing written.
      * `unmaterialised` — declared by the CP, not built yet: spared like `unobserved`, a different fact. An
        absent directory either vanished or never existed; the discriminator is whether the CP ever observed
        it present. Without this state an Add would be reaped within one pass.
      * `gone` — a positive sighting of absence."""
    return {"project": project, "number": _wire_number(number), "path": path, "state": state, "reason": reason}


def _unobserved_slot_entry(project: str, number: str, path: str, reason: str) -> dict:
    """`unobserved` (`_slot_entry`)."""
    return _slot_entry(control_plane_api.OBSERVE_SLOT_UNOBSERVED, project, number, path, reason)


def _unmaterialised_slot_entry(project: str, number: str, path: str, reason: str) -> dict:
    """`unmaterialised` (`_slot_entry`)."""
    return _slot_entry(control_plane_api.OBSERVE_SLOT_UNMATERIALISED, project, number, path, reason)


def _unprobed_slot_entry(project: str, number: str, path: str, *, ever_observed: bool = False) -> dict:
    """A slot this pass did not probe, labelled from the disk test plus one CP fact. A deletion needs a
    positive observation of absence, never silence:
      * path on disk ⇒ `unobserved`;
      * absent, container positively observed (`container_state`) ⇒ `gone` if ever observed present, else
        `unmaterialised`;
      * container unreadable ⇒ `unobserved`.
    `ever_observed` defaults False: a caller that cannot say must not license a delete."""
    if os.path.isdir(path):
        return _unobserved_slot_entry(
            project, number, path,
            "this pass has no probe result for the slot and its path is on disk, so nothing about it was "
            "observed and its row must be spared",
        )
    container = vocab.slot_container(path)
    held_in = vocab.container_state(container)
    if held_in == CONTAINER_UNREADABLE:
        return _unobserved_slot_entry(
            project, number, path,
            f"path is absent and its container {container} is unreadable — the absence was never observed",
        )
    # Both remaining states are sightings of absence; the reason names which (a deleted worktree vs a lost
    # pool root).
    seen = (
        f"its container {container} is readable" if held_in == CONTAINER_PRESENT
        else f"its container {container} does not exist either"
    )
    if not ever_observed:
        return _unmaterialised_slot_entry(
            project, number, path,
            f"path is absent, {seen}, and the CP has never observed this slot present — it is declared but "
            f"not built yet, not deleted",
        )
    return _slot_entry(control_plane_api.OBSERVE_SLOT_GONE, project, number, path, f"path is absent and {seen}")


def _runner_entries(agents_json: list[dict]) -> list[dict]:
    """`ObserveRunnerIn`s from the pass's shared session-list probe (no extra fork).

    A session missing `pid`/`cwd` is skipped and `status`/`kind` coerced: one invalid element 422s the
    whole payload, worktrees included. `proc_started_at`/`account_dir` make a runner identifiable; "" never
    matches."""
    out: list[dict] = []
    for sess in agents_json:
        pid = sess.get("pid")
        cwd = sess.get("cwd")
        if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(cwd, str) or not cwd:
            continue
        kind = sess.get("kind")
        status = sess.get("status")
        out.append({
            "pid": pid,
            "cwd": cwd,
            "kind": None if kind is None else str(kind),
            "status": str(status) if status else "active",
            "proc_started_at": context.proc_started_at(pid),
            "account_dir": str(sess.get("_account") or ""),
        })
    return out


def _root_total_kb() -> int | None:
    """The root filesystem's size in kB (`/`, or `C:\\` on Windows) — the CP clears a pending root grow once
    it reaches the target. None (key omitted) on a virtual node, whose root is the host's, and when unreadable."""
    import environment  # noqa: PLC0415 — deferred, as `agent_process` below

    try:
        if environment.is_vnode():
            return None
        if host_platform.is_windows():
            return shutil.disk_usage("C:\\").total // 1024
        st = os.statvfs("/")
        return st.f_blocks * st.f_frsize // 1024
    except (OSError, RuntimeError, AttributeError):
        return None


def _accountable_refs(tick: tick_wire.PoolTick) -> set[tuple[str, str]]:
    """Every slot this tick makes the node accountable for: roster ∪ `known_slots` — a roster slot with no
    row needs observing, and a filtered known slot is what the complement DELETE would widen onto."""
    return {s.ref for s in tick.slots} | {k.ref for k in tick.known_slots}


def _observation_payload(
    node_id: str,
    tick: tick_wire.PoolTick,
    entries: dict[tuple[str, str], dict],
    *,
    observed_at: str,
    fetched: set[str],
    runners: list[dict],
    runners_ok: bool,
    accounts: list[dict],
    accounts_ok: bool,
    host_metrics: dict | None = None,
    runner_clis: list[str] | None = None,
    platform: dict | None = None,
) -> dict:
    """One pass's `ObservationIn`.

    The three completeness flags are computed, never asserted — each gates a complement DELETE:
    `worktrees_complete` iff every accountable slot came back nameable; `runners_complete` is the caller's
    narrowed `agents_ok`; `accounts_complete` its `roster_ok`. This is the account family's only writer.

    `host_metrics` and `runner_clis` are the keys whose absence asserts nothing: None omits the key (a null
    would need a CP meaning — wipe or ignore — and both are wrong). For `runner_clis`, see its module.
    `platform` is `host_platform.platform_info()`, asked here when the caller did not (off the loop) already."""
    # Name → `projects.id` off this tick's roster: a slot entry carries its project's id beside the name
    # (C6). A project the roster does not name (a known slot only) goes by name alone; the CP falls back.
    ids = {name: proj.project_id for name, proj in tick.projects.items() if proj.project_id is not None}
    payload = {
        "node_id": node_id,
        "observed_at": observed_at,
        "worktrees_complete": _accountable_refs(tick) <= {
            ref for ref, entry in entries.items() if entry.get("number") is not None
        },
        "runners_complete": runners_ok,
        "accounts_complete": accounts_ok,
        "worktrees": [
            {**entry, "project_id": ids[entry["project"]]} if entry.get("project") in ids else entry
            for _ref, entry in sorted(entries.items()) if entry.get("number") is not None
        ],
        "runners": runners,
        "accounts": accounts,
        # The write half of the fetch stamp (`RosterProject.fetch_stamp_age_secs`); without it gate 2 is
        # inert.
        "fetched_projects": sorted(fetched),
        # The same projects by id (C6): the CP prefers these; the names stay for an older CP.
        "fetched_project_ids": sorted({ids[name] for name in fetched if name in ids}),
    }
    if host_metrics is not None:
        payload["host_metrics"] = host_metrics
    root_total_kb = _root_total_kb()
    if root_total_kb is not None:
        payload["root_total_kb"] = root_total_kb
    # `{os, arch, os_version}`, arch normalised (`host_platform/_types.py::normalise_arch`); a CP that predates it
    # ignores the key.
    # `launch_kinds`: which `launch.kind`s this node enacts (`agent_process.LAUNCH_KINDS`); the CP places an
    # SDK agent run only where "agent" is listed, and a CP that predates it ignores the key.
    import agent_process  # noqa: PLC0415 — deferred: a leaf, read only here and on `lease_slot`

    payload["platform"] = {**(platform if platform is not None else host_platform.platform_info()),
                           "launch_kinds": list(agent_process.launch_kinds())}
    if runner_clis is not None:
        payload["runner_clis"] = runner_clis
        # This list senses each provider's ACP adapter (`runner_clis.RUNNER_CLI_BINARIES`), so its lacking one
        # means not installed; the CP gates ACP dispatch only on a node that says so.
        payload["senses_acp_adapters"] = True
    return payload


def _sense_runner_clis() -> list[str] | None:
    """The runner CLIs this node attests: a Linux node's own or, once marked MicroVM, its guest's
    (`microvm_images.guest_clis`; a macOS guest's set is what its base installed) — on Windows as the WSL2 backend
    reports it (`microvm_bridge`)."""
    if host_platform.is_windows():
        bridge = cloud_modules.load("microvm_bridge")
        client = bridge.windows_client() if bridge is not None else None
        if client is not None and bridge.windows_marked():
            return client.runner_clis()
        return sense_runner_clis()
    mark = cloud_modules.load("microvm_mark")
    microvm_images = cloud_modules.load("microvm_images") if mark is not None and mark.host_supported() else None
    if microvm_images is None:  # a community-edition node: no MicroVM image stack
        return sense_runner_clis()

    return microvm_images.guest_clis(host_clis=sense_runner_clis)


async def post_observation(
    node_id: str,
    tick: tick_wire.PoolTick,
    entries: dict[tuple[str, str], dict],
    *,
    observed_at: str,
    ctx: ReconcileContext,
    accounts: list[dict] | None = None,
    accounts_ok: bool = False,
    host_metrics: dict | None = None,
) -> dict | None:
    """Compose and POST this pass's observation. Returns the CP's counts, or None.

    The `pool_tick.ok` gate is the caller's and structural (called only from `reconcile_pass`'s `ok`
    branch): the CP answers an unresolvable node id with an empty roster, so a POST after a bad tick would
    let the complement DELETE take every slot. An empty `node_id` skips for the same reason. None ("not
    known to have landed") is not retried; the next pass supersedes it."""
    if not node_id:
        log.warning(
            "reconcile: no node_id this pass — skipping the observation POST. Without it the CP cannot "
            "scope the write or the complement DELETE, so there is no safe payload to send."
        )
        return None
    agents_ok, agents_json, _cwds = await ctx.agents()
    runner_entries = _runner_entries(agents_json) if agents_ok else []
    # `runners_complete` = `agents_ok` and every deduped session made it onto the wire (`_runner_entries`
    # drops the unnameable; the raw arrays would double-count a session seen from two accounts).
    payload = _observation_payload(
        node_id, tick, entries,
        observed_at=observed_at,
        fetched=ctx.fetched,
        runners=runner_entries,
        runners_ok=agents_ok and len(runner_entries) == len(agents_json),
        accounts=accounts or [],
        # False by default: no family posted means the complement DELETE stays gated shut.
        accounts_ok=accounts_ok,
        # From `main.NodeStatePublisher`'s sampler; None for the `commands` `reconcile` verb, which has no
        # baseline.
        host_metrics=host_metrics,
        # Sensed here (no sampler needed; a threaded value could be forgotten, and a node that stops attesting
        # CLIs looks unable to run any). Off the loop: the stats walk a runner-controlled dir and could hit a
        # hung mount.
        runner_clis=await asyncio.to_thread(_sense_runner_clis),
        # Off the loop: a Windows node's KVM probe runs wsl.exe (`host_platform.windows.KVM_PROBE_TIMEOUT_S`).
        platform=await asyncio.to_thread(host_platform.platform_info),
    )
    result = await asyncio.to_thread(control_plane_api.agent_api_fleet_observe, payload)
    if result is None:
        log.warning(
            "reconcile: the observation POST for node %s did not land (%d slots, complete=%s) — "
            "`nodes.worktrees_updated_at` did not advance, and the node's occupancy stops being trusted once it "
            "ages past `FLEET_MAX_OBSERVATION_AGE_SECS`",
            node_id, len(payload["worktrees"]), payload["worktrees_complete"],
        )
    return result
