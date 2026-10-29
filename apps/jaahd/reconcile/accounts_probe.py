"""The account roster read and the cross-tick session-list probe cache (`AgentsProbeCache`)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, TYPE_CHECKING

import control_plane_api
import node_settings
import environment
import provision_accounts
import session_user
import shared_storage
from agents import claude as default_runtime  # the default provider's legacy-runtime facts, named in its adapter
from env_helpers import registry_norm, roster_config_dir, take_agents_dirty
from reconcile.vocab import AGENTS_PROBE_TIMEOUT, _sh

if TYPE_CHECKING:  # annotations only: these modules import this one
    from reconcile.context import ReconcileContext

log = logging.getLogger(__name__)


# In env_helpers so `fleet_launch` (which cannot import this module) shares the join key.
_registry_norm = registry_norm


# `account_roster()` is the one read behind `main._read_account_sources` and `_fleet_auth_dirs`. The TTL
# (60s, an operator decision) floors the network read only: `account_dir_usable` is re-applied every tick, and
# `invalidate_account_roster()` surfaces a new account next tick.
ACCOUNT_ROSTER_TTL_SECS = 60.0


# The last successful rows and their monotonic stamp (None = re-read). The rows outlive TTL and invalidation:
# a failed forced re-read serves them with `ok=False`, not an empty fleet.
_account_roster_rows: list[dict] = []

_account_roster_at: float | None = None


# Bumped by `invalidate_account_roster`; a fetch whose generation moved under it must not re-stamp.
_account_roster_gen: int = 0


# Sticky: has this process ever been told of an account? Never cleared by a later empty read —
# `_roster_coverage_known` uses it to tell "no accounts" from "accounts vanished under live runners" (the same
# `[]`).
_account_roster_ever_named_any: bool = False


def invalidate_account_roster() -> None:
    """Drop the roster's freshness stamp so the next reader re-reads from the CP.

    Called after an account add, re-auth or delete. Clears the stamp, never the rows. Bumps a generation so
    a fetch already in flight (up to 60s on its 401 retry) cannot re-stamp and hide a just-added account."""
    global _account_roster_at, _account_roster_gen
    _account_roster_at = None
    _account_roster_gen += 1


def account_roster() -> tuple[list[dict], bool]:
    """The raw `GET /api/agent/accounts` rows (enabled + disabled) as `(rows, ok)`, at most one live read per
    `ACCOUNT_ROSTER_TTL_SECS`.

    Raw on purpose: `main._read_account_sources` filters with `account_dir_usable`, while
    `_fleet_auth_dirs` filters nothing (`ReconcileContext.config_dirs` must carry every known dir). `ok`
    becomes `accounts_complete`, which gates an unconditional complement DELETE of `node_accounts` rows
    (`app/fleet_observe.py`): a failed refresh returns the stale rows with `ok=False`. No lock: globals are
    read into locals and replaced whole, so a race costs at most two GETs."""
    global _account_roster_rows, _account_roster_at  # noqa: PLW0603 — module cache, see docstring
    global _account_roster_ever_named_any  # noqa: PLW0603 — sticky latch, see its own comment
    stamp = _account_roster_at
    if stamp is not None and (time.monotonic() - stamp) < ACCOUNT_ROSTER_TTL_SECS:
        return list(_account_roster_rows), True
    gen = _account_roster_gen
    try:
        rows = [
            r for r in control_plane_api.agent_api_accounts(include_disabled=True) if isinstance(r, dict)
        ]
    except Exception as exc:  # noqa: BLE001 — one unavailability is never a verdict (see docstring)
        log.warning(
            "reconcile: DB account roster read failed (%s) — serving the last known %d row(s) with "
            "ok=False; a caller must NOT read this as an empty fleet",
            exc, len(_account_roster_rows),
        )
        return list(_account_roster_rows), False
    _account_roster_rows = rows
    if rows:
        _account_roster_ever_named_any = True
    # Stamp only if no invalidation landed during the fetch; the rows are still returned `ok=True`.
    if gen == _account_roster_gen:
        _account_roster_at = time.monotonic()
    return list(rows), True


def _auth_dirs_from_roster(rows: "list[dict] | tuple") -> list[str]:
    """Raw roster rows → their config dirs (`roster_config_dir`, so an API-key account's derived home too),
    `registry_norm`-normalized and deduped preserving order.

    The pure half of `_fleet_auth_dirs`, for the idle tick that reads the rows inside `asyncio.to_thread`,
    keeping the blocking accounts read (up to 60s on a 401 retry) off the event loop."""
    seen: set[str] = set()
    out: list[str] = []
    for rec in rows:
        cd = roster_config_dir(rec)
        if not cd:
            continue
        norm = _registry_norm(cd)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _fleet_auth_dirs() -> list[str]:
    """The credential-account config_dirs from the DB via `account_roster()`: whole roster, normalized,
    deduped.

    No `ok` flag: this feeds `probed_config_dirs`, whose contract is coverage, not completeness. A failed
    refresh yields the last known rows; the never-read cold start (`[]`) is caught by
    `_roster_coverage_known`."""
    rows, _ok = account_roster()
    return _auth_dirs_from_roster(rows)


def _roster_coverage_known() -> bool:
    """Does this process know which accounts exist at all? `ReconcileContext.agents()` must ask before
    `ok=True`.

    Otherwise the probe set collapses to the default CLI's own config dir, probes clean, and publishes
    `agents_ok=true` + `session_agents=false` for every slot — the pair `app/runner_liveness.py::_verdict_from_row`
    reads as DEAD, killing every task-held runner. Makes no network call (it runs on the loop; a failed refresh
    does not stamp, so falling through to `account_roster()` would re-issue a 60s blocking read every tick).
      * rows held (fresh or stale) → known;
      * no stamp → unknown (cold start);
      * stamped but empty → known only if never told of an account; "accounts existed, now `[]`" (which
        `agent_api_accounts` also returns for an empty/204 body) is unknown, so the CP holds its slots."""
    if _account_roster_rows:
        return True
    if _account_roster_at is None:
        return False
    return not _account_roster_ever_named_any


# Logged once per blind spell, not per tick; coverage can become known mid-life, so not a one-shot latch.
_agents_coverage_unknown_logged = False


def _log_agents_coverage(known: bool, dirs: "list[str]") -> None:
    global _agents_coverage_unknown_logged
    if known:
        _agents_coverage_unknown_logged = False
        return
    if _agents_coverage_unknown_logged:
        return
    _agents_coverage_unknown_logged = True
    log.warning(
        "reconcile: no account roster has ever been read by this process — reporting the agents "
        "snapshot as UNOBSERVED instead of probing %s. That probe would come back clean and empty, "
        "i.e. 'no sessions anywhere', and the CP reads agents_ok=true with no session as every runner "
        "on this node being DEAD",
        dirs,
    )


def _account_layer_providers() -> tuple[dict[str, str], bool]:
    """`(registry_norm'd config_dir -> that account's `accounts.provider`, was the roster readable)`.

    The roster is the only home of the mapping (via the shared `account_roster()` cache). `ok` is returned
    because a bare `.get(dir, "")` reads as the catalogue default, inverting the fail-closed
    `_wants_managed_account_layer`; the caller declines to act on a dir stale rows do not name. A dir
    missing from a readable roster is the default vendor's (the probe set always carries the node's own
    home)."""
    rows, ok = account_roster()
    out: dict[str, str] = {}
    for rec in rows:
        cd = roster_config_dir(rec)
        if cd:
            out[_registry_norm(cd)] = str(rec.get("provider") or "")
    return out, ok


def _wants_managed_account_layer(provider: str) -> bool:
    """May the managed (omc) account layer be planted into an account home belonging to `provider`?

    Asked of the adapter, never a vendor-name branch. Planting it in a foreign home enrols it in the
    marker-based sweeps (its config file is a `plugin_repair._CONFIG_DIR_MARKERS` entry), and a stray manifest
    can break the other CLI's MCP handshake. Same question `launch_runner` asks, one step earlier.

    Fails closed on a provider this node has no adapter for: an account home whose vendor we cannot
    identify is the last one to plant another vendor's manifests into."""
    import agent_providers  # noqa: PLC0415 — deferred: keeps this module's import surface unchanged
    import agents  # noqa: PLC0415 — deferred: the runner-adapter registry

    try:
        adapter = agents.get_adapter(str(provider or "") or agent_providers.DEFAULT_PROVIDER)
    except agents.UnknownProvider:
        return False
    return adapter.account_layer() == "omc"


def _agent_config_dirs(roster: "list[dict] | None" = None) -> list[str]:
    """The default CLI's own config dir (`default_runtime.CONFIG_DIR_ENV`, else its home dir) then every
    `_fleet_auth_dirs` entry, deduped preserving order.

    Deliberately unfiltered by `is_dir()`: a narrowed probe set is unobserved liveness with no flag saying so.
    `roster`, if given, must be the raw `account_roster()` rows (the filtered projection shrinks the set —
    see `ReconcileContext.config_dirs`); None reads them here.

    In sandbox posture only the host user's own dir is dropped: probing it forks the session-list probe against
    a real logged-in account the process does not own. Returning `[]` instead would fail-close
    `commands.py`'s `release`/`retire` verbs.

    The session user's own CLI home rides along: an API-key runner has no config dir, so its session lives
    there, and a probe set without it reads every such runner as never started (DEAD at launch-settle)."""
    home = os.environ.get("HOME", "~")
    dirs = [] if environment.is_sandbox() else [
        os.environ.get(default_runtime.CONFIG_DIR_ENV) or default_runtime.home_config_dir(home),
        default_runtime.session_user_config_dir()]
    for d in (_auth_dirs_from_roster(roster) if roster is not None else _fleet_auth_dirs()):
        if d:
            dirs.append(d)
    seen: set[str] = set()
    ordered: list[str] = []
    for d in dirs:
        if not d or d in seen:
            continue
        seen.add(d)
        ordered.append(d)
    return ordered


# ── Per-pass probe snapshots (cached once per pass) ───────────────────────────
AgentsProbeFn = Callable[[str], Awaitable[tuple[int, str]]]


# Quote a type alias's right-hand side if it carries `|`: an alias is an assignment, not deferred by `from
# __future__ import annotations`.


# Logged once per mode change (None = never); the mode can flip when the fleet socket appears.
_agents_probe_delegated = None


def _log_agents_probe_mode(delegated: bool) -> None:
    global _agents_probe_delegated
    if _agents_probe_delegated == delegated:
        return
    _agents_probe_delegated = delegated
    if delegated:
        log.info("reconcile: session-list probes now run as %r via the fleet tmux server",
                 session_user.session_user())
    else:
        log.info("reconcile: session-list probes run in-process as this uid — no session user to "
                 "switch to (session_user=%r, fleet socket=%r)",
                 session_user.session_user(), session_user.fleet_socket())


async def _gather_agents_probes(probe: AgentsProbeFn, dirs: list[str]) -> list[tuple[int, str]]:
    """Run `probe` over `dirs` at most `node_agents_probe_max_parallel` at a time, results in input order.

    Each session-list probe is 53-72 MB RSS, so unbounded on a 903 MB node OOM-killed a login. Every dir
    is still probed (a narrowed set is unobserved liveness), just in batches. Order is load-bearing: callers
    `zip(dirs, results)`. `asyncio.gather` keeps positions; the semaphore gates entry only."""
    sem = asyncio.Semaphore(node_settings.get("node_agents_probe_max_parallel"))

    async def _one(config_dir: str) -> tuple[int, str]:
        async with sem:
            return await probe(config_dir)

    return list(await asyncio.gather(*(_one(d) for d in dirs)))


async def _delegated_agents_probe(config_dir: str) -> tuple[int, str]:
    """The session-list probe for one account, run as the session user. Same `(rc, stdout)` contract.

    To a thread (a blocking spool round trip). The env is minimal: the daemon's carries `NODE_TOKEN`. The
    `timeout(1)` wrapper is required — the command runs across the spool — and the spool wait is a few
    seconds longer so the far side's deadline fires first (rc 124, same as in-process)."""
    # `shared_storage.probe_env`: a token in the store is not under its config dir, and an unauthenticated
    # probe returns an empty list — recorded as "no live session", the answer fail-closed exists to avoid.
    # A thread, like the run below: it reads the share, and NFS whose server is gone blocks.
    argv = _agents_probe_argv()
    if not argv:
        return 127, ""  # no listing to run: unobserved, never an empty session list
    env = await asyncio.to_thread(shared_storage.probe_env, config_dir)
    try:
        proc = await asyncio.to_thread(
            session_user.run_as_session_user,
            ["timeout", str(AGENTS_PROBE_TIMEOUT), *argv],
            env=env, timeout=AGENTS_PROBE_TIMEOUT + 5)
    except subprocess.TimeoutExpired:
        # The spool stalled; same rc as the in-process timeout.
        return 124, ""
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch vanished mid-flight), OSError
        log.warning("reconcile: agents probe as %r could not run for %s (%s) — recording it as "
                    "UNOBSERVED, not as an empty session list",
                    session_user.session_user(), config_dir, type(exc).__name__)
        return 126, ""
    return proc.returncode, proc.stdout or ""


def _agents_probe_argv() -> tuple[str, ...]:
    """The session-list argv: the probe set's homes are the default provider's (`_agent_config_dirs`), so its
    session listing is the probe (`agent_providers.session_list_argv`, the catalogue's `sessions.list_command`)."""
    import agent_providers  # noqa: PLC0415 — deferred, as the module's other provider reads

    return agent_providers.session_list_argv(agent_providers.DEFAULT_PROVIDER)


async def _default_agents_probe(config_dir: str) -> tuple[int, str]:
    """The session-list probe (`_agents_probe_argv`) for one config dir → (rc, stdout).

    As the session user when there is one: the CLI rewrites `.credentials.json` 0600 owned by whoever
    forked it, so a daemon-forked probe would take the account from its sessions. Fail-closed on both
    paths: a probe that could not run is non-zero with empty stdout, recorded `ok=False` — never "no live
    sessions"."""
    if session_user.can_run_as_session_user():
        _log_agents_probe_mode(True)
        return await _delegated_agents_probe(config_dir)
    _log_agents_probe_mode(False)
    # Same pin as above. `probe_env` omits the store var when the token is in the config dir, so the caller's
    # own store var rides in through this spread (as `accounts.py` runs the CLI).
    env = dict(os.environ, **await asyncio.to_thread(shared_storage.probe_env, config_dir))
    try:
        argv = _agents_probe_argv()
    except OSError:
        return 127, ""  # as the delegated path: unobserved
    if not argv:
        return 127, ""
    rc, out, _err = await _sh(list(argv), AGENTS_PROBE_TIMEOUT, env=env)
    return rc, out


# The session-list probe is a Node cold start (~0.3-0.7s CPU per fork), so the snapshot is cached across
# ticks (`AgentsProbeCache`) at tiered rates, read per call (Settings > Infrastructure > Daemon, via `node_settings`).

def _agents_leased_probe_interval() -> int:
    """Freshness floor for an account working a leased slot — the fast lane the CP's reclamation reads.

    5s, and this is the floor on runner recovery: the CP cannot reclaim until this node reports the session
    gone. Aligned with the ~5s observation pass (smaller buys nothing). Cost: one fork per busy slot per
    5s, bounded by the pool; every other account stays on `_agents_full_scan_interval`."""
    return node_settings.get("node_agents_leased_probe_secs")


# The script's own bound, from its single home.
_CONVERGE_COOLDOWN_S = provision_accounts.CONVERGE_COOLDOWN_SECONDS


def _agents_full_scan_interval() -> int:
    """Freshness floor for every other account: finds orphaned or manually started sessions no lease names."""
    return node_settings.get("node_agents_full_scan_secs")


def _agents_failed_retry_interval() -> int:
    """Freshness floor for a dir whose last probe failed — the recovery lane.

    Neither lane above can serve it: `note_leased` needs a resolved session, and one failed dir makes the
    all-or-nothing snapshot unusable, so nothing is noted leased and the node would wait out the full-scan
    floor with `session_id=None` everywhere (every `deliver_answer` refused and parked). Same default as the
    leased floor; bounded by the failure clearing."""
    return node_settings.get("node_agents_failed_retry_secs")


def _agents_dirty_watch_interval() -> int:
    """How long a dirty mark keeps its account on the leased floor — the launch lane.

    One probe per mark is not enough: `fleet_dispatch` marks right after spawning the pane, and a CLI
    that has not yet written its session file makes that probe come back empty. Nothing then notes the
    account leased (no session resolved), so a node with no other session on that account serves the empty
    list until the full scan, and the CP reads the live runner DEAD at `LAUNCH_SETTLE_SECONDS` (90s,
    `apps/api/app/runner_liveness.py`). Must outlast that settle; bounded, so a mark costs at
    most this long on the leased floor."""
    return node_settings.get("node_agents_dirty_watch_secs")


@dataclass
class AgentsCacheEntry:
    """One account's last session-list probe result. `ok` is R1's probe flag: no entry and a failed probe are
    both "not observed", never an empty-but-successful list."""

    last_probed_at: float
    ok: bool
    sessions: list[dict]


class AgentsProbeCache:
    """Daemon-lifetime cache of per-config_dir session-list probe results, shared by every tick.

    Not owned by the per-pass `ReconcileContext`: results outlive the pass. Due when: no entry (new account
    visible in one tick); `env_helpers.mark_agents_dirty` (process-global, the mutation sites cannot reach
    this instance), then on the leased floor for `_agents_dirty_watch_interval`; `note_leased` (the LAST pass saw a live session on a slot this account holds); or a failed
    last probe, which needs its own input because it cannot produce the third (`_agents_failed_retry_interval`). Coverage never
    shrinks: `ReconcileContext.config_dirs` lists every known dir, served however stale."""

    def __init__(self, *, agents_probe: AgentsProbeFn | None = None) -> None:
        # Injected here too, so a background full scan cannot reach the real CLI while a pass drives a
        # test stub.
        self._agents_probe = agents_probe or _default_agents_probe
        self._entries: dict[str, AgentsCacheEntry] = {}
        self._leased: set[str] = set()
        # config_dir → monotonic deadline: a dirty-marked account stays on the leased floor until then
        # (`_agents_dirty_watch_interval`).
        self._watched: dict[str, float] = {}
        # Monotonic stamp of the last full scan's firing (not completion) + its task. None ⇒ never.
        self._full_scan_at: float | None = None
        self._full_scan_task: asyncio.Task | None = None
        # First-sight provisioning (`converge_unprovisioned`): per-account cooldowns, one task node-wide so a
        # burst of accounts cannot fan out CLI installs.
        self._converge_at: dict[str, float] = {}
        self._converge_task: asyncio.Task | None = None

    def provisioned_since(self, seconds: float) -> bool:
        """Did this node start provisioning an account in the last `seconds`? The converge is detached, so
        its end is never seen; a new node's accounts become usable one by one in this window."""
        now = time.monotonic()
        return any(now - started < seconds for started in self._converge_at.values())

    # ── invalidation inputs ───────────────────────────────────────────────────
    def note_leased(self, config_dir: str) -> None:
        """Record that this account is working a leased slot (called by `observe_slot`).

        Stored verbatim: it arrives as a session's `_account` tag, set from this cache's own key;
        normalizing could move it off that key and silently retire the fast lane."""
        if config_dir:
            self._leased.add(config_dir)

    # ── reads ─────────────────────────────────────────────────────────────────
    def entry(self, config_dir: str) -> AgentsCacheEntry | None:
        return self._entries.get(config_dir)

    def forget_unknown(self, dirs: list[str]) -> None:
        """Drop entries for accounts the roster no longer lists (bounded growth; full scans can complete)."""
        for config_dir in set(self._entries) - set(dirs):
            del self._entries[config_dir]

    def snapshot(self, dirs: list[str]) -> tuple[bool, list[dict], list[str]]:
        """(ok, merged sessions, cwds) over `dirs`, from cache alone — no probe.

        All-or-nothing `ok`: a partial union would look like "that account has no sessions". Deduped by
        `sessionId`, first wins, each tagged with its account."""
        merged: list[dict] = []
        seen_sids: set = set()
        for config_dir in dirs:
            cached = self._entries.get(config_dir)
            if cached is None or not cached.ok:
                return False, [], []
            for sess in cached.sessions:
                tagged = {**sess, "_account": config_dir}
                sid = tagged.get("sessionId")
                if sid in seen_sids:
                    continue
                seen_sids.add(sid)
                merged.append(tagged)
        return True, merged, [s.get("cwd") for s in merged if s.get("cwd")]

    # ── refresh ───────────────────────────────────────────────────────────────
    def due_dirs(self, dirs: list[str]) -> list[str]:
        """Which of `dirs` this tick must probe, draining the dirty and leased sets as it goes.

        Draining is the contract: the caller probes what it is handed, and the leased set is rebuilt from
        what each pass observed. A failed entry is due on its own floor, since the leased set is downstream
        of the failure (`_agents_failed_retry_interval`); a marked one stays on the leased floor for
        `_agents_dirty_watch_interval`, since a launch's mark can precede its session."""
        dirty = take_agents_dirty()
        leased = self._leased
        self._leased = set()
        floor = _agents_leased_probe_interval()
        failed_floor = _agents_failed_retry_interval()
        now = time.monotonic()
        watch_until = now + _agents_dirty_watch_interval()
        for config_dir in dirty:
            self._watched[config_dir] = watch_until
        self._watched = {d: until for d, until in self._watched.items() if until > now}
        due: list[str] = []
        for config_dir in dirs:
            cached = self._entries.get(config_dir)
            if cached is None or config_dir in dirty:
                due.append(config_dir)
            elif not cached.ok and (now - cached.last_probed_at) >= failed_floor:
                due.append(config_dir)
            elif (config_dir in leased or config_dir in self._watched) and (
                    now - cached.last_probed_at) >= floor:
                due.append(config_dir)
        return due

    async def converge_unprovisioned(self, dirs: list[str]) -> None:
        """Start a converge for any account in `dirs` this node has not provisioned yet. Never awaited.

        First sight is the probe: `refresh` forks the session-list probe against every roster account, and that fork
        creates the config dir with only a bare `settings.json` — from which instant `account_dir_usable` says
        yes and a runner could be dispatched into a dir with no hooks, plugins or notification policy. The
        daemon is the one process that can provision it (it alone reads `/etc/jaah/node.env`), and doing it
        here precedes telling the CP, leaving `fleet_dispatch.converge_account_layer` as the backstop.

        Anti-churn, all load-bearing: `absent_only` (never undo a live session's rewrite, e.g. `/model`); a
        per-account cooldown; one converge in flight node-wide. Fire-and-forget; every failure swallowed."""
        if self._converge_task is not None and not self._converge_task.done():
            return
        # Read once per pass, not per account.
        providers, roster_ok = _account_layer_providers()
        now = time.monotonic()
        # The daemon's own home config dir is probed (a liveness floor) but is no runner's account dir once
        # the daemon and the session user differ, and the session user cannot create it in the daemon's
        # home: converging it failed every cooldown and cost the pass its one converge (a real Windows node).
        daemon_dir = (None if os.environ.get(default_runtime.CONFIG_DIR_ENV) or not session_user.switch_needed()
                      else default_runtime.home_config_dir(os.environ.get('HOME', '~')))
        for config_dir in dirs:
            if config_dir == daemon_dir:
                continue
            if now - self._converge_at.get(config_dir, -_CONVERGE_COOLDOWN_S) < _CONVERGE_COOLDOWN_S:
                continue
            # Before the cooldown stamp: a foreign vendor's home is a "never", not a "not yet".
            # `provision_accounts` cannot tell whose home it is and would plant the default vendor's layer
            # every cooldown, forever. Asked of the adapter, as `fleet_dispatch.launch_runner` does.
            if not roster_ok and config_dir not in providers:
                # An unreadable roster is not "no provider" (which defaults to the catalogue's vendor);
                # skipping costs one cooldown.
                continue
            if not _wants_managed_account_layer(providers.get(config_dir, "")):
                continue
            try:
                reason = provision_accounts.needs_converge(config_dir, absent_only=True)
            except Exception:  # noqa: BLE001 — a predicate that raises must not break the tick
                continue
            if not reason:
                continue
            self._converge_at[config_dir] = now
            log.warning(
                "reconcile: %s is not provisioned on this node (%s) — starting a converge before the "
                "control plane is told this node has it. A runner launched into it before that lands "
                "carries no hooks (apps/jaahd/provision_accounts.py)", config_dir, reason)
            self._converge_task = asyncio.create_task(self._run_converge(config_dir))
            return

    async def _run_converge(self, config_dir: str) -> None:
        """One detached converge, started as the session user, on a thread; waits only for the hand-off.

        The directory is created first: `provision_accounts.resolve_config_dirs` skips an explicit
        `--config-dir` that does not exist (right for a human's typo, wrong for a CP-published roster row).
        Created as the session user at `accounts.ensure_account_dir`'s mode, since `<jaah_home>/auth` is
        shared between the two uids and a dir owned by the wrong one is untraversable to the other."""
        try:
            if not Path(config_dir).is_dir():
                # Lazy: `accounts` → `commands` → this module is circular at module scope.
                import accounts
                err = await asyncio.to_thread(
                    session_user.ensure_dir_as_session_user, config_dir,
                    mode=accounts.ACCOUNT_DIR_MODE)
                if err:
                    log.warning("reconcile: could not create the config dir for %s (%s) — the converge "
                                "would find nothing to do, so it is not started", config_dir, err)
                    return
                log.info("reconcile: created this node's config dir for %s", config_dir)
        except Exception as exc:  # noqa: BLE001 — best-effort by contract, see the docstring
            log.warning("reconcile: could not create the config dir for %s (%s: %s)",
                        config_dir, type(exc).__name__, exc)
            return
        argv = provision_accounts.detached_command(config_dir)
        try:
            if session_user.switch_needed():
                await asyncio.to_thread(session_user.run_as_session_user, argv, timeout=60)
            else:
                await asyncio.to_thread(
                    subprocess.run, argv, capture_output=True, text=True, timeout=60)
        except Exception as exc:  # noqa: BLE001 — best-effort by contract, see the docstring
            # The stamp stays: a broken switch retries per cooldown, not per tick.
            log.warning("reconcile: could not start a converge for %s (%s: %s)",
                        config_dir, type(exc).__name__, exc)

    async def refresh(self, dirs: list[str]) -> None:
        """Probe `dirs` in parallel and record each result. Non-JSON-array stdout is recorded `ok=False`,
        so a failure keeps reading as unobserved instead of ageing into the last good list."""
        if not dirs:
            return
        # Before the probes, which materialize an unprovisioned account's dir; starts at most one detached
        # converge.
        await self.converge_unprovisioned(dirs)
        results = await _gather_agents_probes(self._agents_probe, dirs)
        now = time.monotonic()
        for config_dir, (_rc, out) in zip(dirs, results):
            try:
                arr = json.loads(out)
            except ValueError:
                arr = None
            ok = isinstance(arr, list)
            self._entries[config_dir] = AgentsCacheEntry(
                last_probed_at=now, ok=ok, sessions=arr if ok else [],
            )
        # A refresh covering every known dir is a full scan; stamping it stops a cold daemon's first tick
        # spawning a redundant one.
        if self._entries and not set(self._entries) - set(dirs):
            self._full_scan_at = now

    def is_full_scan_due(self) -> bool:
        return (
            self._full_scan_at is None
            or (time.monotonic() - self._full_scan_at) >= _agents_full_scan_interval()
        )

    def start_full_scan(self, dirs: list[str]) -> asyncio.Task | None:
        """Fire the full scan in the background; None when one is already in flight. Returns the task.

        Not awaited by the tick (one Node fork per account). The in-flight guard and the firing-time stamp
        stop a slow scan re-fanning-out on every 5s check."""
        if self._full_scan_task is not None and not self._full_scan_task.done():
            return None
        if not dirs:
            return None
        self._full_scan_at = time.monotonic()
        self._full_scan_task = asyncio.create_task(self._guarded_refresh(list(dirs)))
        return self._full_scan_task

    async def _guarded_refresh(self, dirs: list[str]) -> None:
        """`refresh`, fully guarded (a bare `create_task` would only warn at GC). A failed scan keeps prior
        entries."""
        try:
            await self.refresh(dirs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed scan must never take the daemon down
            log.warning("reconcile: the agents full scan failed, keeping the prior cache: %s", exc)

    @property
    def full_scan_task(self) -> asyncio.Task | None:
        return self._full_scan_task


async def refresh_agents_cache(ctx: "ReconcileContext") -> None:
    """Bring this tick's agents cache up to its freshness floors, then let the pass read it.

    Takes no roster: the leased set comes from the previous pass (`note_leased`), keeping this step
    probe-free. A context with no cache (one-shot `commands.py`, tests) probes every dir once."""
    cache = ctx.agents_cache
    if cache is None:
        await ctx.agents()
        return
    dirs = ctx.config_dirs
    if not dirs:
        return
    cache.forget_unknown(dirs)
    await cache.refresh(cache.due_dirs(dirs))
    if cache.is_full_scan_due():
        cache.start_full_scan(dirs)


def start_agents_refresh(ctx: "ReconcileContext") -> "asyncio.Task":
    """`refresh_agents_cache` as a background task — `reconcile_pass`'s `agents_refresh` form, overlapping the
    pass's fetch and execute arms.

    Guarded, so joining never raises and abandoning never warns at shutdown. A failed refresh is not read
    as "no live sessions": unrefreshed dirs serve `ok=False` and the pass preserves the prior `runner.*`
    keys."""
    async def _guarded() -> None:
        try:
            await refresh_agents_cache(ctx)
        except Exception as exc:  # noqa: BLE001 — a failed refresh costs freshness, never the pass
            log.warning("reconcile: the agents-cache refresh failed (%s) — this pass reads whatever "
                        "the cache already held", exc)

    return asyncio.ensure_future(_guarded())
