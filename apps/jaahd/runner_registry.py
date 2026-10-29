"""Which console Task does a local process belong to? — answered so the asker cannot forge it.

The daemon records the tmux pane pid it observed at dispatch; a caller is identified by walking UP its
/proc parent chain (world-readable) until it meets a recorded pane — ancestry is the one thing a process
cannot choose. A match needs pid AND the pane's start time (raw field-22 ticks), so a recycled pid misses.

Windows: tmux reports `#{pane_pid}` as an MSYS pid, and the Windows process behind it CHANGES when the
pane's bash `exec`s the runner, so no Windows pid is recorded at all. A Windows entry is keyed by the tmux
pid and carries its MSYS start time (`msys_start`) — the pid survives the exec, the start time only moves
forward with it (`host_platform.windows.pane_os_pid`); every lookup re-maps each entry
to its CURRENT Windows pid (`host_platform.pane_os_pid(tmux_pane_pid, msys_start=...)`) — the only pid the
kernel-sourced walk can meet — once per call (`_windows_live`). An entry that does not re-map is not alive,
but is pruned only once its pane has DEFINITELY ended (`windows.pane_gone`): a re-map can fail transiently
(a `cat.exe` timeout), and pruning on that would strand a live runner. A reused MSYS pid re-maps only if
the runtime's pid counter wrapped between the pane's end and the next prune. The walk proves less there than on Linux: every runner runs as the one
`jaah` account, a process creator may name any process it can open as the new child's parent
(`PROC_THREAD_ATTRIBUTE_PARENT_PROCESS`), and the MSYS process table is the runtime's own data, so one
runner can forge another's ancestry (Linux's Yama `ptrace_scope=1` closes the comparable route there). It
separates runners from processes that are no runner at all, not runners of one account from each other.

Fail closed: every unreadable or unmatched case returns None, which `agent_vocabulary` turns into a
refusal. Stdlib only (`cred_broker` imports this on the request path); no import-time filesystem work
or environment reads — the location is resolved per call (`_registry_dir`)."""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import env_helpers  # the atomic file write
# The daemon's POSTURE; a stdlib-only leaf with no import-time work.
import environment
from jaah_env import env_get
import host_platform

log = logging.getLogger("jaahd.runner_registry")

# 0750 jaahd:jaahd — group `jaahd`, NOT `jaah`: a runner that reads siblings' task ids holds the
# input to impersonating one. `jaahd.service`'s ExecStartPre owns the mode. This is the regular node's
# spelling; `_registry_dir` resolves per call so a virtual node's registry is its own.
_RUN_PARTS = ("runners",)
_PROVISIONED_DIR = Path(environment.RUN_ROOT_DEFAULT, *_RUN_PARTS)

# Explicit registry directory override, honoured in any posture; `_write`'s refusal names it.
_REGISTRY_DIR_ENV = "JAAH_RUNNER_REGISTRY_DIR"

# Kept in step with `jaahd.service`'s ExecStartPre, so `_write` never inherits a umask.
_REGISTRY_DIR_MODE = 0o750
_REGISTRY_FILENAME = "registry.json"
_PROVISIONED_PATH = _PROVISIONED_DIR / _REGISTRY_FILENAME

# The defaults, rebindable by the suite — NOT an environment read at import: an override read only at
# import is silently ignored when set later, and `_write` would then REPLACE the live daemon's registry,
# stripping every runner's identity. Resolution is per call in `_registry_dir`/`_registry_path`.
REGISTRY_DIR = _PROVISIONED_DIR
REGISTRY_PATH = _PROVISIONED_PATH

# A sandbox's own, under its pool root's `run/` (`env_helpers._LAYOUT`). Spelled here, not via
# `env_helpers.jaah_dir`, which RAISES without a pool root — the request and dispatch paths must not.
_SANDBOX_PARTS = ("run", "runners")

# Depth cap on the parent walk. Real chains are depth 1-4; 64 only stops a malformed /proc or a cycle.
_MAX_WALK_DEPTH = 64

# Guards the read-modify-write (`launch_runner` may run off the event loop); readers need no lock since
# the file is replaced atomically.
_LOCK = threading.Lock()


@dataclass(frozen=True)
class RunnerIdentity:
    """One live runner, as the daemon observed it at launch. `task_id` is validated as digits."""

    task_id: str
    pane_pid: int
    pane_start_ticks: int
    worktree: str
    session_id: str
    account_dir: str = ""
    # The API-key account slug this runner was leased on (`""` for OAuth). `cred_broker._resolve` refuses
    # any other asked-for slug, else a runner could walk the roster collecting every key.
    api_key_account: str = ""
    # `JAAH_REPORT_DIR`: the confinement root for `cred_broker._open_confined_upload`. Recorded, never
    # re-derived; every launch records one, so `""` is only an entry recorded before that — uploads refused.
    report_dir: str = ""
    # Which vendor's CLI the pane runs (`agent_providers` value). `""` = recorded before this field existed.
    provider: str = ""
    # The Jaah session (`bedrock_launch`) this runner runs on, `""` for every other account. The ONLY
    # `bedrock_session:<id>` name `cred_broker._resolve` serves it, and what `bedrock_close` kills by.
    bedrock_session: str = ""
    # The pane as TMUX names it (`#{pane_pid}`), for tmux-side lookups (`answer_delivery.pane_text_for_pid`).
    # Equal to `pane_pid` on posix; the MSYS pid on Windows, where `pane_pid` is the CURRENT Windows pid,
    # re-mapped at lookup, and `pane_start_ticks` the MSYS start time. Left 0 by
    # a constructor that does not know it, it defaults to `pane_pid` (`__post_init__`).
    tmux_pane_pid: int = 0
    # `acp` when the pane is `jaah-acp-host` (`acp_runtime.RUNTIME_ACP`), `""` for a CLI launch (and for an
    # entry recorded before this field existed). An ACP run is sensed by its pane whatever its provider.
    runtime: str = ""
    # The CP dispatch that launched this pane (`lease_slot`'s `dispatch_id` and `generation`), recorded with the pane
    # (`record`); `""`/None when no dispatch fenced it. Lets a re-served lease recognise its own runner.
    dispatch_id: str = ""
    generation: int | None = None

    def __post_init__(self) -> None:
        if not self.tmux_pane_pid:
            object.__setattr__(self, "tmux_pane_pid", self.pane_pid)


def _registry_dir() -> Path | None:
    """The registry directory, resolved at call time, or None when this process has none of its own.

    Order is load-bearing: (1) `$JAAH_RUNNER_REGISTRY_DIR`, any posture; (2) a rebound `REGISTRY_DIR`
    (the suite's seam, tested by identity); (3) sandbox posture: under its pool root, NEVER
    `/run/jaah/runners`, which `_write` would replace, clobbering the packaged daemon's registry;
    (4) the provisioned default.

    A sandbox is redirected, not disabled (unlike `upgrade_request`'s markers): this process is the
    file's only reader, so a redirected registry is self-consistent, while a disabled one would report
    `not_a_runner` for a runner it just recorded. None only for sandbox posture with no pool root."""
    explicit = env_get(_REGISTRY_DIR_ENV)
    if explicit:
        return Path(explicit)
    if REGISTRY_DIR is not _PROVISIONED_DIR:
        return REGISTRY_DIR
    if environment.is_sandbox():
        root = environment.pool_root_or_empty().rstrip("/")
        return Path(root).joinpath(*_SANDBOX_PARTS) if root else None
    if host_platform.is_windows():
        # NOT under `run\`: the runner account holds Modify on that whole tree (the installer's access
        # list), and a registry a runner can rewrite names any task, account and upload root it likes.
        # `state\` is SYSTEM, Administrators and the daemon only.
        return Path(host_platform.paths().state_dir, *_RUN_PARTS)
    return Path(environment.run_path(*_RUN_PARTS))


def _registry_path() -> Path | None:
    """The registry file, or None — `_registry_dir` plus the filename, with a rebound `REGISTRY_PATH` arm."""
    if REGISTRY_PATH is not _PROVISIONED_PATH:
        return REGISTRY_PATH
    directory = _registry_dir()
    return None if directory is None else directory / _REGISTRY_FILENAME


def _start_ticks(pid: int) -> int | None:
    """The process's start time (raw field-22 ticks on Linux), or None when unreadable — delegated to
    `host_platform.start_time`, looked up per call so a test fakes either name."""
    return host_platform.start_time(pid)


def _parent_of(pid: int) -> int | None:
    """The parent pid, or None when unreadable — delegated to `host_platform.parent_of`.

    A targeted read, not a process-table scan: this is on the broker's request path."""
    return host_platform.parent_of(pid)


def _load() -> dict[str, dict]:
    """The registry as `{pane_pid_str: entry}`, or `{}` when there is nothing readable there.

    A corrupt file or no registry directory is `{}` and logged: fails closed (every task-scoped verb
    refuses) rather than raising into the broker's handler."""
    path = _registry_path()
    if path is None:
        log.debug("runner-registry: this process has no registry of its own (%s posture with no pool "
                  "root) — no runner is identifiable", environment.posture())
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("runner-registry: cannot read %s (%s) — no runner will be identifiable", path, exc)
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        log.warning("runner-registry: %s is not valid JSON (%s) — treating it as empty", path, exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _alive(pane_pid: int, entry: dict) -> bool:
    """Is this pid still the recorded process? The start time must match, so a recycled pid misses."""
    ticks = _start_ticks(pane_pid)
    return ticks is not None and ticks == entry.get("pane_start_ticks")


def _pane_fields(tmux_pid: int, what: str) -> dict | None:
    """The identity fields of a new entry for tmux pane `tmux_pid`, or None (logged) when it cannot be
    recorded. Posix: `pane_start_ticks` of that pid (byte-identical to before Windows). Windows: the tmux
    pid and its MSYS start time (`windows.pane_identity`), never a Windows pid — that one changes on exec."""
    if not host_platform.is_windows():
        ticks = _start_ticks(tmux_pid)
        if ticks is None:
            log.warning("runner-registry: %s pane %s is already gone (no /proc entry) — not recorded",
                        what, tmux_pid)
            return None
        return {"pane_start_ticks": ticks}
    from host_platform import windows  # the MSYS reads exist only there
    ident = windows.pane_identity(tmux_pid)
    if ident is None:
        log.warning("runner-registry: %s pane %s has no MSYS identity this node can vouch for (exited, a "
                    "zombie, or unreadable) — not recorded; its task-scoped calls will be refused",
                    what, tmux_pid)
        return None
    return {"msys_start": ident[1], "tmux_pane_pid": tmux_pid}


def _windows_pid(entry: dict) -> int | None:
    """A Windows entry's CURRENT Windows pid, or None when its pane no longer re-maps (exited, a zombie, a
    reused MSYS pid, an unreadable /proc). A missing or non-integer field is None: never an unchecked map."""
    tmux_pid, start = entry.get("tmux_pane_pid"), entry.get("msys_start")
    if type(tmux_pid) is not int or type(start) is not int:
        return None
    return host_platform.pane_os_pid(tmux_pid, msys_start=start)


def _windows_live(entries: dict[str, dict]) -> dict[int, tuple[str, dict]] | None:
    """Windows: current Windows pid -> (key, entry) for every entry that re-maps, each re-mapped ONCE per
    call (each re-map is two `cat.exe` spawns, and a walk meets up to 64 pids). None off Windows, where a
    key is already a kernel pid and `_alive` is checked lazily, as before."""
    if not host_platform.is_windows():
        return None
    live: dict[int, tuple[str, dict]] = {}
    for key, entry in entries.items():
        if key.isdigit() and isinstance(entry, dict):
            pid = _windows_pid(entry)
            if pid is not None:
                live[pid] = (key, entry)
    return live


def _match(pid: int, entries: dict[str, dict], live: dict[int, tuple[str, dict]] | None) -> dict | None:
    """The live entry whose pane IS kernel pid `pid`, or None."""
    if live is not None:
        hit = live.get(pid)
        return hit[1] if hit else None
    entry = entries.get(str(pid))
    return entry if isinstance(entry, dict) and _alive(pid, entry) else None


def _live_entries(entries: dict[str, dict]) -> dict[str, dict]:
    """`entries` less every dead one (record's prune). `k.isdigit()` FIRST: `int(k)` on a non-digit key
    raises `ValueError`, which the callers' `except OSError` would not catch, taking the dispatch down."""
    live = _windows_live(entries)
    if live is not None:
        # Kept unless DEFINITELY gone (`windows.pane_gone`): a re-map that merely failed this once (a
        # `cat.exe` timeout under load) must not delete a live runner's identity for good.
        from host_platform import windows  # the MSYS reads exist only there
        keep = {key for key, _entry in live.values()}
        return {k: v for k, v in entries.items()
                if k in keep or (isinstance(v, dict) and not _definitely_gone(windows, v))}
    return {k: v for k, v in entries.items() if k.isdigit() and _alive(int(k), v)}


def _definitely_gone(windows, entry: dict) -> bool:
    """A Windows entry whose pane has definitely ended, or whose identity fields are unusable."""
    tmux_pid, start = entry.get("tmux_pane_pid"), entry.get("msys_start")
    if type(tmux_pid) is not int or type(start) is not int:
        return True
    return windows.pane_gone(tmux_pid, start)


def _write(entries: dict[str, dict]) -> None:
    """Replace the registry atomically (0640 in the 0750 directory that is the real ACL), so a reader
    never sees a prefix.

    RAISES a plain `OSError` when this process has no registry directory, never falling back to the
    node's own; callers already catch `OSError` as best-effort."""
    directory = _registry_dir()
    path = _registry_path()
    if directory is None or path is None:
        raise OSError(
            f"this process has no runner registry: {environment.posture()} posture with no pool root, "
            f"and {_PROVISIONED_DIR} belongs to this machine's packaged daemon rather than to us. Set "
            f"{_REGISTRY_DIR_ENV} to name a directory of your own.")
    # Mode stated explicitly: a umask-default 0755 would expose every runner's task id. `mkdir`'s mode is
    # umask-masked, so the chmod is what pins it.
    directory.mkdir(parents=True, exist_ok=True, mode=_REGISTRY_DIR_MODE)
    try:
        os.chmod(directory, _REGISTRY_DIR_MODE)
    except OSError as exc:
        # Not fatal: a registry we cannot re-chmod is still writable.
        log.debug("runner-registry: could not re-assert %s's mode (%s)", directory, exc)
    env_helpers.atomic_write_json(path, entries, mode=0o640)


#: The `provider` of an SDK agent's pane (`fleet_dispatch.launch_agent`): its `jaah-acp-host` runs the
#: tenant's code, which no agent adapter describes. Every reader that dispatches on an agent provider skips it;
#: the observation senses it by its pane alone (`reconcile.context._vendor_runner_meta`), so it is alive exactly while
#: its process is.
SDK_PROVIDER = "sdk"
#: The providers whose pane runs no agent CLI: every reader that asks an adapter about a runner skips them.
NO_MODEL_PROVIDERS = frozenset({SDK_PROVIDER})


def record(*, task_id: object, pane_pid: object, worktree: str = "", session_id: str = "",
           account_dir: str = "", api_key_account: str = "", report_dir: str = "",
           provider: str = "", bedrock_session: str = "", runtime: str = "", dispatch_id: str = "",
           generation: "int | None" = None) -> None:
    """Remember that `pane_pid` is the tmux pane of a runner working `task_id`; called by
    `fleet_dispatch.launch_runner` right after the pane pid is known.

    `dispatch_id`/`generation`: the CP dispatch that launched the pane (the `lease_slot` fence), written in this same
    entry so no launched runner is ever on record without it — a re-served lease recognises its own runner
    (`reconcile.enact_launch._launched_by`) even when the daemon died the instant after the spawn.

    BEST EFFORT: on the dispatch path, after the CP committed a slot, so faults are logged and swallowed
    (the runner's task-scoped calls then fail loudly). Prunes dead entries on the way through."""
    try:
        pid = int(str(pane_pid).strip())
    except (TypeError, ValueError):
        log.warning("runner-registry: refusing to record task %s — pane pid %r is not a number "
                    "(no runner identity; its task-scoped calls will be refused)", task_id, pane_pid)
        return
    task = str(task_id).strip()
    if not task.isdigit():
        log.warning("runner-registry: refusing to record pane %s — task id %r is not digits", pid, task_id)
        return
    fields = _pane_fields(pid, f"task {task}'s")
    if fields is None:
        return
    try:
        with _LOCK:
            entries = _live_entries(_load())
            entries[str(pid)] = {
                "task_id": task,
                **fields,
                "worktree": worktree,
                "session_id": session_id,
                # Stamped onto runner-break reports by the daemon: a caller-supplied account would let any
                # runner mark any account usage-limited.
                "account_dir": account_dir,
                # The API-key account this runner may ask the broker for, and the ONLY one it may
                # (see `RunnerIdentity.api_key_account`).
                "api_key_account": api_key_account,
                # Where this runner renders its report (see `RunnerIdentity`).
                "report_dir": report_dir,
                # Read by `live_in_worktree`'s caller: a non-Claude runner is sensed by this pane alone.
                "provider": provider,
                # The Jaah session this runner may ask Bedrock credentials for, and the ONLY one.
                "bedrock_session": bedrock_session,
                # Read by `reconcile.context._vendor_runner_meta`: an ACP run is sensed by this pane alone.
                "runtime": runtime,
                # The dispatch fence (see above); `""`/None for a launch no dispatch fences.
                "dispatch_id": dispatch_id,
                "generation": int(generation) if generation is not None else None,
            }
            _write(entries)
    except OSError as exc:
        log.warning("runner-registry: could not record pane %s for task %s (%s) — that runner's "
                    "task-scoped calls will be refused", pid, task, exc)
        return
    log.info("runner-registry: pane %s -> task %s (worktree=%s)", pid, task, worktree or "-")


# An entry of the OTHER kind: a console tab on an API-key account or a Jaah session, which has no task. Keyed
# like a runner's (pane pid + start ticks) but carrying no `task_id`, so `resolve_peer`, `live_in_worktree`
# and `live_runners` — which all require a digit task id — never see it as a runner. Its readers are
# `console_key_account` and `console_bedrock_session`, consumed by `cred_broker._resolve`'s two account arms.
_CONSOLE_KIND = "console_key"


def record_console_key(*, pane_pid: object, api_key_account: str, worktree: str = "") -> bool:
    """Remember that `pane_pid` is a console tab the CP opened on API-key account `api_key_account`, so its
    `apiKeyHelper` may read THAT account's key and nothing else. Called by `ctl.py tmux-window` the moment
    the pane pid is known. Returns whether it was recorded; faults are logged, never raised."""
    slug = str(api_key_account or "").strip()
    if not slug:
        return False
    return _record_console(pane_pid, worktree, f"api-key account {slug}", api_key_account=slug)


def record_console_bedrock(*, pane_pid: object, bedrock_session: str, worktree: str = "") -> bool:
    """`record_console_key`'s Jaah-account twin: `pane_pid` is a console tab the CP opened on Jaah session
    `bedrock_session`, so its `awsCredentialExport` may read THAT session's credentials and nothing else."""
    sid = str(bedrock_session or "").strip()
    if not sid:
        return False
    return _record_console(pane_pid, worktree, f"Jaah session {sid}", api_key_account="", bedrock_session=sid)


def _record_console(pane_pid: object, worktree: str, what: str, **grant: str) -> bool:
    try:
        pid = int(str(pane_pid).strip())
    except (TypeError, ValueError):
        log.warning("runner-registry: refusing a console grant — pane pid %r is not a number", pane_pid)
        return False
    fields = _pane_fields(pid, "console")
    if fields is None:
        return False
    try:
        with _LOCK:
            entries = _live_entries(_load())
            entries[str(pid)] = {
                "kind": _CONSOLE_KIND,
                "task_id": "",
                **fields,
                "worktree": worktree,
                **grant,
            }
            _write(entries)
    except OSError as exc:
        log.warning("runner-registry: could not record console pane %s's grant (%s) — that tab's "
                    "credential will be refused", pid, exc)
        return False
    log.info("runner-registry: console pane %s -> %s (worktree=%s)", pid, what, worktree or "-")
    return True


def console_key_account(peer_pid: object) -> str:
    """The API-key account slug of the console tab owning `peer_pid`, or `""`.

    The same kernel-sourced ancestry walk as `resolve_peer`, matching ONLY console entries: a runner's pane
    never answers here, and a console entry never answers `resolve_peer`."""
    return _console_grant(peer_pid, "api_key_account")


def console_bedrock_session(peer_pid: object) -> str:
    """The Jaah session of the console tab owning `peer_pid`, or `""` — `console_key_account`'s twin."""
    return _console_grant(peer_pid, "bedrock_session")


def _console_grant(peer_pid: object, field: str) -> str:
    try:
        pid = int(str(peer_pid).strip())
    except (TypeError, ValueError):
        return ""
    entries = _load()
    live = _windows_live(entries)
    seen: set[int] = set()
    depth = 0
    while pid > 1 and depth < _MAX_WALK_DEPTH and pid not in seen:
        seen.add(pid)
        entry = _match(pid, entries, live)
        if entry is not None and entry.get("kind") == _CONSOLE_KIND:
            return str(entry.get(field) or "")
        parent = _parent_of(pid)
        if parent is None:
            return ""
        pid = parent
        depth += 1
    return ""


def bedrock_panes(bedrock_session: str) -> list[int]:
    """Every live pane — runner or console tab — this node launched on Jaah session `bedrock_session`
    (`bedrock_close`, K10). Same liveness test as everywhere here, so a recycled pid is never named. The
    pids are TMUX's (`bedrock_close` matches `#{pane_pid}`): the key on every OS; on Windows a pane is live
    when it still re-maps (`_windows_pid`)."""
    sid = str(bedrock_session or "").strip()
    if not sid:
        return []
    windows = host_platform.is_windows()
    return sorted(
        int(key) for key, entry in _load().items()
        if key.isdigit() and isinstance(entry, dict) and str(entry.get("bedrock_session") or "") == sid
        and ((_windows_pid(entry) is not None) if windows else _alive(int(key), entry))
    )


def forget(pane_pid: object) -> None:
    """Drop the entry of tmux pane `pane_pid` (the key on every OS). Not needed for correctness; makes a
    deliberate teardown immediate."""
    try:
        pid = str(int(str(pane_pid).strip()))
    except (TypeError, ValueError):
        return
    try:
        with _LOCK:
            entries = _load()
            if pid not in entries:
                return
            entries.pop(pid)
            _write(entries)
    except OSError as exc:
        log.debug("runner-registry: could not forget pane %s (%s)", pid, exc)


def _ticks(entry: dict) -> int:
    """The entry's start time, for ordering a worktree's runners: posix boot ticks, Windows the MSYS start
    time — compared only within one node, so the unit need not match."""
    return int(entry["msys_start"] if "msys_start" in entry else entry["pane_start_ticks"])


def _identity(pid: int, entry: dict, task: str) -> RunnerIdentity:
    """`pid` is the KERNEL pid: the key on posix, the entry's current re-mapped Windows pid on Windows."""
    return RunnerIdentity(
        task_id=task,
        pane_pid=pid,
        pane_start_ticks=_ticks(entry),
        worktree=str(entry.get("worktree", "")),
        session_id=str(entry.get("session_id", "")),
        account_dir=str(entry.get("account_dir", "")),
        # A missing field reads as "no API-key account" — fail closed: refused the key.
        api_key_account=str(entry.get("api_key_account", "")),
        report_dir=str(entry.get("report_dir", "")),
        provider=str(entry.get("provider", "")),
        bedrock_session=str(entry.get("bedrock_session", "")),
        # Absent on every posix entry, where tmux's pid IS the kernel's (`__post_init__` fills it in).
        tmux_pane_pid=int(entry.get("tmux_pane_pid") or 0),
        runtime=str(entry.get("runtime", "")),
        dispatch_id=str(entry.get("dispatch_id", "")),
        generation=entry.get("generation") if type(entry.get("generation")) is int else None,
    )


def live_in_worktree(worktree: str) -> RunnerIdentity | None:
    """The newest runner this node launched into `worktree` whose pane is still the recorded process, or None.

    A sensor for `reconcile.observe.observe_slot`: a CLI with no session list (every non-Claude provider) is alive
    exactly as long as the pane it was launched in. Same liveness test as `resolve_peer` (pid AND start
    ticks), so a recycled pid misses."""
    runs = _live_runs_in(worktree)
    return max(runs, key=lambda r: r[0])[1] if runs else None


def live_runners() -> list[RunnerIdentity]:
    """Every worktree's newest live runner (`live_in_worktree`'s answer for each), in one registry read —
    for a sweep over the runners themselves (`vendor_turn_errors`)."""
    return list(_newest_live_by_worktree().values())


def live_all_in_worktree(worktree: str) -> list[RunnerIdentity]:
    """EVERY runner this node launched into `worktree` whose pane is still the recorded process — not just the
    newest: a predecessor that outlived its relaunch is in the set. For the `kill_runner` generation fence
    (`reconcile.enact_signal._kill_generation_is_stale`), which judges the whole set."""
    return [ident for _ticks, ident in _live_runs_in(worktree)]


def _live_runs_in(worktree: str) -> list[tuple[int, RunnerIdentity]]:
    """Every live runner recorded under `worktree`, each with its pane start ticks, matched by `path_key`."""
    root = (worktree or "").rstrip("/")
    if not root:
        return []
    key = host_platform.path_key(root)
    return [run for recorded, runs in _live_by_worktree().items()
            if host_platform.path_key(recorded) == key for run in runs]


def _newest_live_by_worktree() -> dict[str, RunnerIdentity]:
    """Recorded worktree (no trailing slash) -> its newest live runner, by pane start ticks."""
    return {root: max(runners, key=lambda r: r[0])[1] for root, runners in _live_by_worktree().items()}


def _live_by_worktree() -> dict[str, list[tuple[int, RunnerIdentity]]]:
    """Recorded worktree (no trailing slash) -> every live runner in it, each with its pane start ticks."""
    found: dict[str, list[tuple[int, RunnerIdentity]]] = {}
    entries = _load()
    live = _windows_live(entries)
    current = {key: pid for pid, (key, _entry) in live.items()} if live is not None else None
    for key, entry in entries.items():
        if not key.isdigit() or not isinstance(entry, dict):
            continue
        root = str(entry.get("worktree", "")).rstrip("/")
        task = str(entry.get("task_id", "")).strip()
        if not root or not task.isdigit():
            continue
        if current is not None:
            if key not in current:
                continue
            pid = current[key]
        else:
            pid = int(key)
            if not _alive(pid, entry):
                continue
        found.setdefault(root, []).append((_ticks(entry), _identity(pid, entry, task)))
    return found


def resolve_peer(peer_pid: object) -> RunnerIdentity | None:
    """The runner identity owning `peer_pid`, or None when this process is not inside a known runner.

    `peer_pid` MUST come from the kernel (`SO_PEERCRED`), never a request body. The walk starts at the
    peer itself (it may BE the pane). None is a refusal, never a default."""
    try:
        pid = int(str(peer_pid).strip())
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    entries = _load()
    if not entries:
        return None
    live = _windows_live(entries)
    seen: set[int] = set()
    depth = 0
    while pid > 1 and depth < _MAX_WALK_DEPTH and pid not in seen:
        seen.add(pid)
        # Liveness re-checked: the pane may have exited since the last prune and its pid been recycled.
        entry = _match(pid, entries, live)
        if entry is not None:
            task = str(entry.get("task_id", "")).strip()
            if task.isdigit():
                return _identity(pid, entry, task)
        parent = _parent_of(pid)
        if parent is None:
            return None
        pid = parent
        depth += 1
    return None
