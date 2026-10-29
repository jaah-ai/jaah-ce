#!/usr/bin/env python3
"""jaahd — the Jaah node daemon.

One always-on process per node: the node-local authority over the worktree pool, the Console PTY
bridge (`console.py`) and in-process bus commands (`commands.py`). Each idle tick projects the complete
node state to the control plane; presence is a property of the CP WebSocket.

Usage:
    python apps/jaahd/main.py [--idle-interval SECONDS]
    python apps/jaahd/main.py --sweep-sessions | --sweep-slot-residue  # one-shot
    python apps/jaahd/main.py --sandbox DIR [--sandbox-cp URL]   # every outward surface under DIR

The last form is the SANDBOX posture: every outward surface under one directory, no node identity, no
control plane unless given one. `environment.py` is the single home of that table.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import signal
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

import re

# ── THE SANDBOX BOOTSTRAP, AND IT MUST STAY ABOVE THE SIBLING IMPORTS BELOW ──────────────────────────
#
# Parsed by hand, not by `main()`'s argparse: `control_plane_api` resolves `NODE_TOKEN` into a module
# constant at IMPORT, so a `--sandbox` process would already hold the host's real node token by the time
# `main()` ran. The import order is the mechanism. Both `--sandbox X` and `--sandbox=X` are accepted;
# argparse's prefix abbreviation is not, so the two readings are compared in `_assert_sandbox_parses_agree`.
import environment as environment_mod
from jaah_env import env_get


def _sandbox_bootstrap(argv: list[str]) -> tuple[str | None, str | None]:
    """Apply the sandbox posture from `argv`, returning `(root, cp_base_url)` as it read them.

    A no-op when `--sandbox` is absent (every packaged invocation). Nothing is created or exported until
    both values are checked: `enter_sandbox` mkdirs and rewrites `os.environ`, and a refusal must leave
    the environment untouched."""
    def _value(flag: str) -> str | None:
        """`flag`'s value in `argv`, or None when it is absent. EXITS on a flag present without one.

        A hard exit here, not left to argparse, because this pre-parse ACTS before argparse runs. A value
        beginning with `-` is refused: the token after a valueless flag is the next flag (`--sandbox
        --sandbox-cp URL` would take `"--sandbox-cp"` as the root), and neither flag takes one.
        `sys.exit(str)`: argparse does not exist yet, and an operator needs one line, not a traceback."""
        for i, arg in enumerate(argv):
            if arg.startswith(f"{flag}="):
                value = arg.split("=", 1)[1]
                if not value.strip():
                    sys.exit(f"jaahd: {arg!r} gives {flag} an empty value; it needs one")
                return value
            if arg == flag:
                if i + 1 >= len(argv):
                    sys.exit(f"jaahd: {flag} needs a value and is the last argument")
                following = argv[i + 1]
                if following.startswith("-"):
                    sys.exit(f"jaahd: {flag} needs a value and got the flag {following!r} — "
                             f"write {flag} VALUE (or {flag}=VALUE)")
                if not following.strip():
                    sys.exit(f"jaahd: {flag} needs a value and got an empty string")
                return following
        return None

    def _checked_cp_base_url(raw: str | None) -> str | None:
        """`raw` if it is an http(s) URL carrying no credential, else EXIT.

        Nothing downstream checks the scheme, and urllib's default `FileHandler` would serve a `file://`
        base out of the local filesystem. Userinfo is REFUSED, not scrubbed: argv is visible in `ps` and
        shell history, so the secret is already disclosed; pass it via `JAAH_CP_BASE_URL` instead."""
        if raw is None:
            return None
        try:
            parts = urlsplit(raw)
        except ValueError as exc:
            sys.exit(f"jaahd: --sandbox-cp {raw!r} is not a parseable URL ({exc})")
        if parts.scheme not in ("http", "https"):
            sys.exit(f"jaahd: --sandbox-cp {raw!r} must be an http:// or https:// URL "
                     f"(got scheme {parts.scheme!r}) — the control plane is reached over HTTP, and "
                     f"urllib would otherwise serve a file:// base out of this machine's filesystem")
        if not parts.hostname:
            sys.exit(f"jaahd: --sandbox-cp {raw!r} names no host")
        if parts.username or parts.password:
            sys.exit("jaahd: --sandbox-cp must not carry a username or password — argv is visible to "
                     "every process on this machine (`ps`) and is written to shell history. Pass the "
                     "credentialed base in the environment instead: JAAH_CP_BASE_URL=...")
        return raw

    root = _value("--sandbox")
    if root is None:
        return None, None
    cp_base_url = _checked_cp_base_url(_value("--sandbox-cp"))
    # `enter_sandbox`'s own refusals (relative root, `/`, group/world-writable, a planted `node.env`) reach
    # the operator as one line. Not narrowed to `ValueError`: this is the operator's first output.
    try:
        environment_mod.enter_sandbox(root, cp_base_url=cp_base_url)
    except (ValueError, RuntimeError, OSError) as exc:
        sys.exit(f"jaahd: {exc}")
    return root, cp_base_url


def _assert_sandbox_parses_agree(args: argparse.Namespace) -> None:
    """Refuse to run when argparse read the sandbox flags differently from the pre-parse above.

    Only the pre-parse acts, so a disagreement is otherwise silent. The live cause is argparse's prefix
    abbreviation: `--sandbox-c URL` dropped the control plane, and one rename away `--sandb /tmp/x` would
    be a daemon that believes it is sandboxed while every surface stays provisioned. Also catches `main()`
    reached through an import, where the bootstrap did not run. Preferred over `allow_abbrev=False`,
    which would change how every other flag parses."""
    if args.sandbox == SANDBOX_ROOT and args.sandbox_cp == SANDBOX_CP_BASE_URL:
        return
    sys.exit(
        "jaahd: the sandbox flags parsed two different ways and the daemon will not guess which is "
        "meant.\n"
        f"  the import-time bootstrap read: --sandbox={SANDBOX_ROOT!r} --sandbox-cp="
        f"{SANDBOX_CP_BASE_URL!r} (and this is the reading the posture was APPLIED from)\n"
        f"  argparse read:                  --sandbox={args.sandbox!r} --sandbox-cp={args.sandbox_cp!r}\n"
        "Spell both flags out in full (argparse accepts abbreviations; the bootstrap does not)."
    )


# Only when this file is the program: the pre-parse is an import-time side effect of the importing
# program's argv (`ctl.py` does `import main`). `__name__` is already `"__main__"` while a script's body
# runs, so this still fires above the sibling imports, which MUST stay below it.
SANDBOX_ROOT: str | None = None
SANDBOX_CP_BASE_URL: str | None = None
if __name__ == "__main__":
    SANDBOX_ROOT, SANDBOX_CP_BASE_URL = _sandbox_bootstrap(sys.argv[1:])

import commands as commands_bridge_mod
import console as console_bridge_mod
import console_direct as console_direct_mod
# The local credential socket: a `jaah`-user process gets a CP-owned credential without
# holding the node's own token.
import cred_broker as cred_broker_mod
# Imported for its STARTUP SWEEP alone (see `_serve`) — the command verbs it registers arrive through
# `commands` above, which already imports it.
import node_bus as node_bus_mod
import node_settings as node_settings_mod   # the CP's node tunables, off each pool tick
import env_helpers   # `share_home_readable`: the node-local data root's share bit
# The host CPU/RAM sampler whose reading rides the observation POST as its `host_metrics` block.
import host_metrics as host_metrics_mod
# Single home for the config_dir join key: the DB roster's `config_dir` and the probe's must
# normalize to the identical string, and collector/reconcile key by this same normalizer.
from env_helpers import _norm_dir
# The managed-settings consent block is single-homed in `plugin_repair`; only its read helpers are used
# here, so the snapshot can REPORT what this node would consent to.
import agent_providers   # the naming the snapshot reports (`NAMING_SNAPSHOT_KEY`)
import node_frame_auth   # the CP frame label the snapshot reports (`WIRE_LABELS_SNAPSHOT_KEY`)
import pkgmgr_loader   # the installed `jaah` version, apt or dnf
import plugin_repair as plugin_repair_mod
from reconcile import tick_wire, accounts_probe, context, orchestrate
# The cloud node's own steps, command handlers and bus callbacks (MicroVM, NodeLink, the GHA pool, the apt
# upgrade, the vnode host) are discovered, not imported: none of them ship in the community edition.
import cloud_modules
import edition_features  # which features this node's edition includes (`packages/editions/features.toml`)
# The uid-split seam; the module docstring is the single home for the requirement.
import session_user as session_user_mod
import state as state_mod
# Bodies of the per-node folded loops, named here for the idle-tick step wiring.
import control_plane_api as control_plane_api_mod
# The `uninstall` bus frame's marker (`jaah-uninstall.path` watches it).
import uninstall_request as uninstall_request_mod
import egress_report as egress_report_mod
# The node disk reclaim's daemon half: the `disk-reclaim` trigger step, the warn cache, the report uploader.
import disk_reclaim_trigger as disk_reclaim_trigger_mod
# Root grow: the observe response's `root_grow` drops the marker a privileged watcher grows `/` on.
import root_grow_request as root_grow_request_mod
import node_logs as node_logs_mod
import nodevault_identity as nodevault_identity_mod
import project_memory_bundle as project_memory_bundle_mod
import provider_models as provider_models_mod
import vendor_turn_errors as vendor_turn_errors_mod
import bedrock_usage_tail as bedrock_usage_tail_mod
import secret_modes as secret_modes_mod
import windows_anchor_health as windows_anchor_health_mod
import windows_engine_health as windows_engine_health_mod
import shared_storage as shared_storage_mod
import host_platform

# After `commands`, which already discovered it, so this is a cache hit and never half-initializes the handler
# registry.
cloud_node = cloud_modules.load("cloud_node")
# The Architectural Guard's bundle refresh and decision spool (the `compliance` feature's; None where it is not).
guard_bundle_mod = cloud_modules.load("guard_bundle")
# The features `cloud_node` wires: an edition including any of them must ship it (`_serve`).
CLOUD_NODE_FEATURES = ("microvm", "nodelink", "gha_runners", "apt_self_upgrade", "multi_node_hosts")

# `datefmt` ends in a literal `Z`, so the clock must be UTC (`Formatter` defaults to localtime).
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("jaahd")

DEFAULT_IDLE_INTERVAL = 5.0
# How long a CP-PUSHED intent's pass waits on the reconcile lock. Every other caller skips on contention;
# this pass carries an intent no in-flight pass has read, so skipping parks it for the idle interval.
# Above a normal reconcile, under the CP's re-serve.
PUSHED_INTENT_LOCK_WAIT = 30.0
# Shutdown grace for `run()`'s in-flight single-flight steps: a clean SIGTERM waits this long,
# then cancels, so a wedged step can never block the exit. Applied TWICE worst-case (drain, then
# post-cancel wait), so it dominates clean-shutdown wall-time.
STEP_SHUTDOWN_GRACE_SECS = 3.0
# A single-flight step wedged forever keeps the loop heartbeating, so it would otherwise be
# invisible. Warn once it has been in flight and skipping firings this long, then at this cadence.
STEP_WEDGE_WARN_SECS = 300.0
BUS_SCHEMA_VERSION = state_mod.SCHEMA_VERSION   # single home: state.SCHEMA_VERSION
# No self-update loop (dpkg/apt owns deploy) and no discovery/presence subjects.

# On a node this is dpkg's read-only package tree: a node holds no clone of this repo.
HUB_ROOT = Path(__file__).resolve().parents[2]


def _linux_only_steps_run() -> bool:
    """Whether this node runs the Linux-only periodic steps (the GHA runner pool and the vnode converge) —
    PRD §8 ports none of them off Linux. Logs the one line that says so on any other OS."""
    if host_platform.is_linux():
        return True
    log.info("host platform %s/%s: the Linux-only subsystems (vnodehelper, GHA runner pool, "
             "disk reclaim) do not run on this node", host_platform.OS,
             host_platform.ARCH)
    return False


def prepend_git_path(environ: dict | None = None) -> str:
    """Put Git for Windows' directories (`windows.git_path_dirs()`, `usr\\bin` first) at the FRONT of
    ENVIRON's PATH, each once, and return the new value. Idempotent.

    Windows only (the caller gates it): `tmux.exe` binds to Git's `msys-2.0.dll`, and the delegated
    `run-shell` scripts call Git's coreutils — `System32\\timeout.exe` must never win `timeout`. Safe on the
    daemon's own PATH: Git's directories are admin-writable only (unlike `~jaah\\.local\\bin`)."""
    from host_platform import windows  # noqa: PLC0415 — only this Windows-gated path needs it

    environ = os.environ if environ is None else environ
    front = windows.git_path_dirs()
    keys = {windows.path_key(d) for d in front}
    rest = [p for p in environ.get("PATH", "").split(";") if p and windows.path_key(p) not in keys]
    environ["PATH"] = ";".join([*front, *rest])
    return environ["PATH"]


def _prepare_windows_host() -> None:
    """The daemon's Windows-only startup: Git on PATH before the first tmux call, and the delegation
    spool (`session_user.prepare_windows_spool`). A no-op elsewhere; logs failures, never raises — a
    node without either still heartbeats and reports why its dispatches refuse."""
    if not host_platform.is_windows():
        return
    try:
        prepend_git_path()
    except Exception:  # noqa: BLE001 — startup must survive a missing Git; tmux's own errors follow
        log.exception("could not put Git for Windows on PATH — tmux and delegated commands will fail")
    try:
        err = session_user_mod.prepare_windows_spool()
    except Exception as exc:  # noqa: BLE001 — same posture
        err = str(exc)
    if err:
        log.error("session-user spool not prepared (%s) — commands delegated to the runner account "
                  "will be refused", err)


def daemon_state_dir() -> Path:
    """The daemon's own writable state root — `env_helpers.daemon_state_dir` is the single home."""
    return env_helpers.daemon_state_dir()

PublishFn = Callable[[], Awaitable[None]]

@dataclass(frozen=True)
class ScheduledTask:
    """One scheduled task folded into the daemon — per-node work, fired on every node daemon.
    `argv[0]`/`argv[1]` are absolute interpreter and script paths, so the daemon's cwd never matters, and
    `timeout` bounds the subprocess wall-clock.

    `DEFAULT_SCHEDULED_TASKS` is built ONCE at import, so a fixed-tuple `argv` freezes anything
    env-derived at daemon start; a task needing per-firing resolution passes a zero-arg callable."""

    name: str
    interval: float  # seconds between fires (approximate — not wall-clock-aligned like cron)
    argv: Sequence[str] | Callable[[], Sequence[str]]
    timeout: float | None = None  # subprocess wall-clock bound in seconds; None = unbounded
    # Log the child's stderr even on exit 0: `run_scheduled_task` otherwise reads stderr only on failure,
    # so a task whose product is a silent decision is invisible.
    log_output: bool = False


# The interpreter running THIS daemon: the node's venv (/usr/lib/jaahd/venv), the bundle's own python, the test
# process. Never the OS layout's `/usr/bin/python3`, which carries none of the pinned libraries main.py imports.
DEFAULT_JAAH_PYTHON = sys.executable

# Per-firing cap on a `log_output` task's stderr in the journal.
_TASK_OUTPUT_LOG_CHARS = 4000


def resolve_console_python() -> str:
    """Resolve `${JAAH_PYTHON:-<this interpreter>}` exactly as the `worktree-agents` task's script does — `or`,
    not a dict default, so a set-but-empty var also falls back (bash's `:-`, not `-`).

    Load-bearing: the sweeps re-run `main.py`, which imports the pinned `websockets`; only the interpreter this
    daemon runs under (the node's venv) is known to carry it, never the ambient or the OS `python3`."""
    return env_get("JAAH_PYTHON") or DEFAULT_JAAH_PYTHON


def build_scheduled_tasks(hub_root: Path = HUB_ROOT) -> tuple[ScheduledTask, ...]:
    """The scheduled tasks this daemon fires, each mapped to a one-shot argv.

    Every task's scope IS the host, so every node daemon fires it, always, with no flag in front:
    per-task arms left node duties registered, tested and never fired on hosts that run the flags off. The
    one surviving arm is `worktree-agents`, which KILLS processes. Portfolio-wide work is not a node duty:
    it belongs on a console cron (DBOS), never here.
    """
    jaahd_main = str(hub_root / "apps" / "jaahd" / "main.py")
    return (
        ScheduledTask(
            name="oauth-endpoint-canary",
            # 6h: a credential-free request against a contract Anthropic may change — soon enough to catch
            # a break before an access token expires.
            interval=21_600.0,
            argv=(sys.executable, str(hub_root / "apps" / "jaahd" / "oauth_endpoint_check.py")),
            # One request with an 8s client timeout; anything beyond this is a wedged socket.
            timeout=60.0,
        ),
        ScheduledTask(
            name="session-transcript-sweep",
            # Node tier: the transcripts live on the disk of the node that ran the sessions.
            interval=60.0,
            # Callable argv: `DEFAULT_SCHEDULED_TASKS` is built once at import, and a fixed tuple would
            # freeze the interpreter choice at daemon start.
            argv=lambda: (resolve_console_python(), jaahd_main, "--sweep-sessions"),
            # A one-shot subprocess: no state across ticks, and it bounds its own `claude
            # agents` probe. 1800s: up to ten serial uploads, each on the CP's 15-minute lease — bounded
            # so a wedged socket dies and retries.
            timeout=1800.0,
            # Its whole output is `sweep_once`'s summary; without it a sweep reporting every session
            # `unavailable` looks like one that uploaded everything.
            log_output=True,
        ),
        ScheduledTask(
            name="slot-residue-sweep",
            # Node tier: what erased slots left on this node's own disk and engine (`slot_residue.py`).
            # Hourly: the erase itself purges its slot, so this only collects what one missed. Not in
            # `FIRE_AT_STARTUP_NAMES`: that set is for checks that mutate nothing.
            interval=3_600.0,
            # Callable argv, as for `session-transcript-sweep`.
            argv=lambda: (resolve_console_python(), jaahd_main, "--sweep-slot-residue"),
            # A one-shot; a killed run costs only the pass. 900s: delegated `rm -rf`s and one
            # compose down per orphan stack.
            timeout=900.0,
            # Its product is a silent decision (what was removed, what was skipped and why).
            log_output=True,
        ),
        # `node-liveness` is an in-process CP job (`node_liveness_job.py`); do not re-add it here.
        ScheduledTask(
            name="worktree-agents",
            interval=300.0,
            # Not in `FIRE_AT_STARTUP_NAMES`: it kills processes, and at boot there is nothing to reap.
            argv=(sys.executable, str(hub_root / "tools" / "worktree_agents.py")),
            timeout=600.0,
        ),
    )


DEFAULT_SCHEDULED_TASKS = build_scheduled_tasks()


# Tasks and steps whose first fire must not wait a full interval (`run()` backdates `next_due`): cheap,
# idempotent, mutate nothing but a too-wide file mode. The canary's 6h interval would otherwise reset on
# every restart and never elapse; `model-catalogs` would leave a freshly started node's catalog-resolved tiers on the CLI default
# for 30 minutes after every deploy; `secret-modes` is the enforce-on-start half of G-22's mode pass;
# `nodevault-identity` registers a fresh node's identity key without a 30s wait.
# `disk-reclaim` only writes an empty marker; a just-restarted node must not wait 5 minutes to ask for a pass
# (a disk near full is why many restarts happen). `fleet-anchor` writes only the daemon's own anchor memo,
# and must pin before an update restarts `jaah-runner` right after this daemon came up.
FIRE_AT_STARTUP_NAMES = ("oauth-endpoint-canary", "model-catalogs", "secret-modes", "nodevault-identity",
                         "disk-reclaim", "fleet-anchor")

def resolve_worktree_agents_enabled() -> bool:
    """`JAAHD_COCKPIT_AGENTS=on` fires the `worktree-agents` reaper on its own; its scope is this machine.
    Default `off`, because this task KILLS processes."""
    return env_get("JAAHD_COCKPIT_AGENTS", "off").strip().lower() == "on"


def resolve_autoupdate_enabled() -> bool:
    """Whether this daemon may ASK for a package upgrade: the CP's answer for this node (Settings ›
    Infrastructure › Updates › Auto-update, overridden on the node's or its host's page). A network call.

    Only a definite `True` opens it — an unreachable CP or a malformed answer is off, since the request
    ends in a ROOT apt transaction. The root updater asks the same question last."""
    return control_plane_api_mod.agent_api_autoupdate_enabled() is True


# card↔DB sync is retired: the `projects` table is the single home for project metadata
# (a render is not a merge).

# ── Per-node folded loops ────────────────────────────────────────────────────────────────────────────
# Each on its own next_due interval timer (`PeriodicStep`).


@dataclass(frozen=True)
class PeriodicStep:
    """A node-tier idle-tick step fired on its own `next_due` interval timer — a native async
    callable, not a `bash <script>` ScheduledTask.

    A raised exception is logged-and-continued, never fatal (the idle-loop resilience contract)."""

    name: str
    step: Callable[[], Awaitable[None]]
    interval: float


async def _vnode_account_converge_step() -> None:
    """A vnode's deploy trigger for its own accounts' plugin layer (`fleet_dispatch.converge_accounts_on_upgrade`):
    starts once per package version, and retries each interval until the start succeeds."""
    # Lazy: `fleet_dispatch` pulls the dispatcher's world in.
    from fleet_dispatch import converge_accounts_on_upgrade
    await asyncio.to_thread(lambda: converge_accounts_on_upgrade(resolve_daemon_version(),
                                                                 stamp_dir=daemon_state_dir()))


async def _upgrade_check_on_connect() -> None:
    """Ask the root path unit to converge when the installed `jaah` package differs from the fleet's
    desired release — the PULL half of the node's deploy. The auto-update gate comes before the
    release query, and this never raises: a convergence check may never cost the node its bus."""
    if not await asyncio.to_thread(resolve_autoupdate_enabled):
        log.debug("upgrade-check: auto-update is off for this node — not asking the CP for a release")
        return
    installed = resolve_daemon_version()
    desired = await asyncio.to_thread(control_plane_api_mod.agent_api_release_desired)
    cloud_node.request_upgrade(installed, desired)


async def run_scheduled_task(task: ScheduledTask, cwd: Path = HUB_ROOT) -> None:
    """Invoke one scheduled task's argv. A failing script, a missing one, or a raising argv-builder is
    logged, never raised. A callable `task.argv` is resolved per call, so env-derived argv is fresh."""
    try:
        argv = task.argv() if callable(task.argv) else task.argv
    except Exception as exc:  # noqa: BLE001 — a bad argv-builder must never kill the idle loop
        log.warning("scheduled task '%s' argv could not be resolved: %s", task.name, exc)
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=task.timeout)
        except asyncio.TimeoutError:
            # A wedged one-shot must be killed, not left to hang and freeze its output forever.
            proc.kill()
            await proc.wait()
            log.warning(
                "scheduled task '%s' timed out after %ss — killed", task.name, task.timeout,
            )
            return
        except asyncio.CancelledError:
            # Kill the child too, or it outlives the daemon. No `await proc.wait()`: under a sticky
            # cancellation any await re-raises; the child watcher reaps it.
            proc.kill()
            raise
        if proc.returncode != 0:
            log.warning(
                "scheduled task '%s' failed (rc=%s): %s",
                task.name,
                proc.returncode,
                stderr.decode(errors="replace").strip(),
            )
        elif task.log_output and (output := stderr.decode(errors="replace").strip()):
            # Truncated to the tail: a one-shot's summary is its last line.
            log.info(
                "scheduled task '%s': %s", task.name, output[-_TASK_OUTPUT_LOG_CHARS:],
            )
        else:
            log.debug("scheduled task '%s' ok", task.name)
    except OSError as exc:
        log.warning("scheduled task '%s' could not start (%s): %s", task.name, argv, exc)


ScheduledTaskRunner = Callable[[ScheduledTask], Awaitable[None]]


async def _noop_publish() -> None:
    """Default no-op publish step; `_serve` wires in the real `NodeStatePublisher`."""
    return None


def resolve_api_base_url() -> str:
    """This network's control-plane API base URL — `control_plane_api.resolve_control_plane_api_base()` is
    the one home. Named here so `node_bus` can call it."""
    return control_plane_api_mod.resolve_control_plane_api_base()


async def run(
    idle_interval: float,
    stop_event: asyncio.Event,
    publish: PublishFn = _noop_publish,
    worktree_agents_enabled: bool = False,
    scheduled_tasks: tuple[ScheduledTask, ...] = DEFAULT_SCHEDULED_TASKS,
    run_task: ScheduledTaskRunner = run_scheduled_task,
    periodic_steps: tuple[PeriodicStep, ...] = (),
) -> None:
    """Idle-tick until stop_event is set.

    Each elapsed tick awaits `publish` INLINE — the one inline step, so a wedged loop stops heartbeating
    and the CP flips the node OFFLINE truthfully. Every other step is a single-flight background task
    (`spawn_step`): skipped, never stacked, when still in flight.

    Per-tick order: elapsed `scheduled_tasks`, then each
    `PeriodicStep`. On shutdown, in-flight steps get `STEP_SHUTDOWN_GRACE_SECS`, then are cancelled
    under the same bound. Slot-intent verbs drain through the `bus-projection` step's own reconcile pass
    (`reconcile.execute._INTENT_EXECUTORS`)."""
    loop = asyncio.get_running_loop()
    # Checked once: downstream "" degrades quietly to "no node". A warning, not a raise — refusing to
    # start would remove the duties needed to fix the provisioning.
    if not resolve_node_id() and environment_mod.is_sandbox():
        # A sandbox has no node identity by design (`environment.enter_sandbox`); nothing to fix.
        log.info(
            "NODE_ID is not set, which is what sandbox posture means: this process adopts no node "
            "identity, so nothing below claims a lease, reads a worktree roster or registers a node")
    elif not resolve_node_id():
        log.error(
            "NODE_ID is not set — this node has no assigned identity, so every lease claim, worktree "
            "roster read and node registration below will run unscoped and take effect for no node. "
            "Assign it in /etc/jaah/node.env (NODE_ID=<name>), which this unit loads as an "
            "EnvironmentFile: `jaah auth login <node_id>` writes it there. It is never derived from "
            "the hostname or the tailnet name — those disagree with the assigned id on every node "
            "measured."
        )
    # Every task fires on every node, always. `worktree-agents` is the one exception: it KILLS
    # processes, which earns a per-host off switch.
    eligible = [t for t in scheduled_tasks if t.name != "worktree-agents" or worktree_agents_enabled]
    # Backdated so the canary is due on the first tick: every deploy restarts the daemon and would reset
    # a 6h `next_due` forever.
    next_due = {
        t.name: loop.time() - (t.interval if t.name in FIRE_AT_STARTUP_NAMES else 0.0)
        for t in eligible
    }
    step_next_due = {
        s.name: loop.time() - (s.interval if s.name in FIRE_AT_STARTUP_NAMES else 0.0)
        for s in periodic_steps
    }

    # One task slot per step name: a step still running when it next comes due is skipped (its timer not
    # advanced), never stacked; past STEP_WEDGE_WARN_SECS it warns.
    inflight: dict[str, asyncio.Task] = {}
    skipping_since: dict[str, float] = {}
    last_wedge_warn: dict[str, float] = {}

    def spawn_step(name: str, step: Callable[[], Awaitable[None]]) -> bool:
        prior = inflight.get(name)
        if prior is not None and not prior.done():
            now = loop.time()
            first_skip = skipping_since.setdefault(name, now)
            stuck_for = now - first_skip
            if (
                stuck_for >= STEP_WEDGE_WARN_SECS
                and now - last_wedge_warn.get(name, 0.0) >= STEP_WEDGE_WARN_SECS
            ):
                last_wedge_warn[name] = now
                log.warning(
                    "%s has been in flight for over %.0fs and is skipping its firings — "
                    "possibly wedged (single-flight keeps the loop alive; investigate the step)",
                    name, stuck_for,
                )
            return False
        skipping_since.pop(name, None)
        last_wedge_warn.pop(name, None)

        async def guarded() -> None:
            try:
                await step()
            except Exception as exc:
                log.warning("%s failed: %s", name, exc)

        inflight[name] = asyncio.create_task(guarded(), name=name)
        return True

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=idle_interval)
        except asyncio.TimeoutError:
            # The heartbeat: the ONE inline await — see the docstring. Everything below is spawned.
            await publish()
            # 0700 on the daemon's own tmux socket, every tick: an in-place upgrade keeps the old server
            # (`KillMode=process`) with an older build's group bits, and a group-usable socket is one
            # `tmux new-window` from a pane as `jaahd`.
            session_user_mod.restrict_own_socket()
            # Applications write `0600` whatever the umask. `account_tree=False`: that tree is the session
            # user's and only its owner may chmod (`fleet_tmux_server` walks it; see the function). Spawned
            # into a thread: an unbounded `os.walk` on this loop would delay the next tick's heartbeat.
            spawn_step("share-home-readable",
                       lambda: asyncio.to_thread(env_helpers.share_home_readable, account_tree=False))
            now = loop.time()
            for task in eligible:
                if now - next_due[task.name] >= task.interval and spawn_step(
                    f"scheduled task '{task.name}'", lambda task=task: run_task(task)
                ):
                    next_due[task.name] = now
            for pstep in periodic_steps:
                if now - step_next_due[pstep.name] >= pstep.interval and spawn_step(
                    f"periodic step {pstep.name}", pstep.step
                ):
                    step_next_due[pstep.name] = now

    # Shutdown drain: a bounded grace, then cancel; a step on an uncancellable thread is logged and
    # abandoned rather than hanging the exit.
    pending = [t for t in inflight.values() if not t.done()]
    if pending:
        _done, still_pending = await asyncio.wait(pending, timeout=STEP_SHUTDOWN_GRACE_SECS)
        for t in still_pending:
            t.cancel()
        if still_pending:
            # `asyncio.wait`, not `wait_for(gather(...))`, which would hang on a step that swallows the
            # cancel.
            _done2, abandoned = await asyncio.wait(still_pending, timeout=STEP_SHUTDOWN_GRACE_SECS)
            if abandoned:
                log.warning(
                    "shutdown: %d step task(s) did not finish cancelling within %.0fs "
                    "(likely stuck in an uncancellable thread) — exiting anyway: %s",
                    len(abandoned), STEP_SHUTDOWN_GRACE_SECS,
                    ", ".join(sorted(t.get_name() for t in abandoned)),
                )


def resolve_node_id() -> str:
    """This node's identity: `$NODE_ID`, and nothing else. "" when unset.

    Assigned, never derived: a hostname or tailnet-name fallback invents a plausible WRONG id exactly
    when the environment is degraded. "" is the honest absent; `run()` logs it once. One home:
    `control_plane_api.this_node_id()`, which cannot import `main` without a cycle."""
    return control_plane_api_mod.this_node_id()


# STRICT ALLOWLIST of the keys published in the read-only config snapshot, daemon-side, so a secret leaks
# only if explicitly added. Never add one; never list a key nothing reads.
CONFIG_SNAPSHOT_ALLOWLIST = (
    "NODE_ID",
    # `node_liveness`'s CONFIG-DRIFT verdict compares `nodes.config_desired` against this snapshot, so a
    # key published here must be one the daemon actually reads.
    "JAAHD_COCKPIT_AGENTS",
    "JAAH_CP_BASE_URL",
    "JAAH_HOME",
)

# The managed-settings consent this node can resolve, a DERIVED field (the env allowlist cannot carry it).
# `"none"` is published, not omitted: absent = a daemon too old to report; `"none"` = no consenting peer and
# no seed, so the first runner wedges at "Managed settings require approval" (`plugin_repair.py`'s block);
# a digest = what this node would consent to, which the CP may seed into the next node
# (absence is not a state). Not a credential: a one-way digest of
# the secret-bearing `remote-settings.json`, identical fleet-wide by design.
CONSENT_SNAPSHOT_KEY = "remote_settings_consent"
CONSENT_NONE = "none"
# Beside a digest: the winning consent record's `updatedAt` (epoch ms, decimal string), which the CP judges
# "newest" by, so a node whose peers still hold a pre-change hash cannot flip its org's value back
# (`apps/api/app/node_config.py::CONSENT_UPDATED_AT_SNAPSHOT_KEY`). Absent with `"none"` or no stamp.
CONSENT_UPDATED_AT_SNAPSHOT_KEY = "remote_settings_consent_updated_at"

# URL-valued keys, scrubbed of userinfo. Explicit, not `endswith("_URL")`: a silent miss leaks a credential.
_URL_VALUED_CONFIG_KEYS = ("JAAH_CP_BASE_URL",)


def _scrub_url_userinfo(value: str) -> str:
    """Strip any userinfo (`user:pass@` / `token@`) from a URL before it is written down anywhere, so a
    credential in `JAAH_CP_BASE_URL` is never published; unchanged when it has no parseable host.
    Single home, three callers: the config snapshot, the startup posture line, the node-bus dial line."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.hostname:
        return value
    netloc = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_remote_settings_consent(jaah_home: str | None = None) -> tuple[str, int | None]:
    """What an account on this node has ACTUALLY consented to — `(digest, updatedAt)`, the 64-hex
    `dangerousSettingsHash` and epoch-ms stamp of the newest peer consent record — or `(CONSENT_NONE,
    None)`, the state that wedges a fresh node's first runner. The stamp is None when the record has none.

    Peers only, never `consent_hash`'s env arm: that reads the CP-written seed from
    `/etc/jaah/jaahd.env`, and publishing it back would make an echo indistinguishable from real
    consent. The seed is an input to consenting, never evidence of it.

    Never raises; an unreadable or half-provisioned tree degrades to `"none"`."""
    try:
        home = plugin_repair_mod.resolve_jaah_home(jaah_home)
        newest = plugin_repair_mod.newest_peer_consent(plugin_repair_mod.discover_config_dirs(home))
    except Exception as exc:   # noqa: BLE001 — an optional field may never break the snapshot
        log.warning("could not resolve the managed-settings consent to publish: %s", exc)
        return CONSENT_NONE, None
    if newest is None:
        return CONSENT_NONE, None
    stamp, digest = newest
    return digest, (stamp if stamp > 0 else None)


# The CP reads it under this name (`apps/api/app/slot_stack.py::TAILNET_DNS_NAME_SNAPSHOT_KEY`).
TAILNET_DNS_NAME_SNAPSHOT_KEY = "tailnet_dns_name"
# Display-only: the node page renders every snapshot key as it comes.
NODELINK_SNAPSHOT_KEY = "nodelink"
NODELINK_NOT_MOUNTED = "not mounted"
# Per-NodeLink mount state (`shared_storage.nodelink_mount_states`, a compact JSON string) and the accounts root
# this node verified (`shared_storage.shared_accounts_root`): the CP's ingest reads both (Architecture §4.2).
NODELINKS_SNAPSHOT_KEY = "nodelinks"
SHARED_ACCOUNTS_ROOT_SNAPSHOT_KEY = "shared_accounts_root"


def nodelink_state() -> str:
    """This node's NodeLink share, in the console's words: `off` (no share root here), `not mounted`, or
    `mounted <fs id>` — the file system OBSERVED at the root (`shared_storage.observed_efs_id`), or
    `mounted (file system unknown)` when it cannot be established. Blocks on a hung NFS server: call it
    off the event loop."""
    if not shared_storage_mod.shared_accounts_surface():
        return "off"
    root = shared_storage_mod.shared_accounts_root()
    if not root or not shared_storage_mod.share_mounted():
        return NODELINK_NOT_MOUNTED
    if host_platform.is_windows():
        # A link into the engine distro, which this daemon cannot open: the file system it mounted, as recorded.
        efs_id = shared_storage_mod.recorded_efs_id(root)
        return f"mounted {efs_id}" if efs_id else "mounted (file system unknown)"
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        efs_id = shared_storage_mod.observed_efs_id(root, fd)
    finally:
        os.close(fd)
    return f"mounted {efs_id}" if efs_id else "mounted (file system unknown)"


def share_reads() -> tuple[str, str, str]:
    """`(nodelink_state(), nodelinks, shared_accounts_root)` — the three snapshot values that ask NFS, read
    together on one thread. `nodelinks` is the compact JSON of `shared_storage.nodelink_mount_states()`."""
    states = shared_storage_mod.nodelink_mount_states()
    return (nodelink_state(), json.dumps(states, separators=(",", ":"), sort_keys=True),
            shared_storage_mod.shared_accounts_root())


def build_config_snapshot(
    env: Mapping[str, str] | None = None,
    *,
    version: str | None = None,
    direct_console_url: str | None = None,
    remote_settings_consent: str | None = None,
    remote_settings_consent_updated_at: int | None = None,
    tailnet_dns_name: str | None = None,
    nodelink: str | None = None,
    nodelinks: str | None = None,
    shared_accounts_root: str | None = None,
) -> dict[str, str]:
    """Build this node's redacted config snapshot — the read-only effective config the console Nodes
    editor renders. Strict allowlist, daemon-side; values are strings, file contents are
    never read, URL-valued keys are userinfo-scrubbed. At most sixteen keys: the four allowlisted env keys
    plus `version`, `direct_console_url`, `remote_settings_consent`, `remote_settings_consent_updated_at`,
    `tailnet_dns_name`, `nodelink`, `nodelinks`, `shared_accounts_root`, `naming`, `env_naming`, `vocabulary` and `wire_labels`."""
    source = os.environ if env is None else env
    snapshot: dict[str, str] = {}
    for key in CONFIG_SNAPSHOT_ALLOWLIST:
        value = env_get(key, None, source)
        if value is not None:
            text = str(value)
            snapshot[key] = _scrub_url_userinfo(text) if key in _URL_VALUED_CONFIG_KEYS else text
    # Derived fields are injected explicitly. Absent when dpkg cannot name the installed package.
    if version is not None:
        snapshot["version"] = str(version)
    # Where a browser may dial this node (`apps/api/app/console_direct.py`); absent when nothing is bound.
    if direct_console_url is not None:
        snapshot["direct_console_url"] = str(direct_console_url)
    # How a remote browser addresses this node — the CP hosts slot stack URLs on it
    # (`apps/api/app/slot_stack.py`). Absent off the tailnet.
    if tailnet_dns_name is not None:
        snapshot[TAILNET_DNS_NAME_SNAPSHOT_KEY] = str(tailnet_dns_name)
    # What this node's token-store mount is right now (`nodelink_state`), shown on the node page. Absent
    # until the first read lands.
    if nodelink is not None:
        snapshot[NODELINK_SNAPSHOT_KEY] = str(nodelink)
    # Each NodeLink's mount (`share_reads`) and the verified accounts root (`""` = none); absent until read.
    if nodelinks is not None:
        snapshot[NODELINKS_SNAPSHOT_KEY] = str(nodelinks)
    if shared_accounts_root is not None:
        snapshot[SHARED_ACCOUNTS_ROOT_SNAPSHOT_KEY] = str(shared_accounts_root)
    # Unlike the three above, "none" is published (see the constants). `None` is only an older caller.
    if remote_settings_consent is not None:
        snapshot[CONSENT_SNAPSHOT_KEY] = str(remote_settings_consent)
        if remote_settings_consent_updated_at is not None and remote_settings_consent != CONSENT_NONE:
            snapshot[CONSENT_UPDATED_AT_SNAPSHOT_KEY] = str(int(remote_settings_consent_updated_at))
    # Always: this daemon speaks the current names.
    snapshot[agent_providers.NAMING_SNAPSHOT_KEY] = agent_providers.CURRENT_NAMING
    snapshot[agent_providers.ENV_NAMING_SNAPSHOT_KEY] = agent_providers.CURRENT_ENV_NAMING
    snapshot[agent_providers.VOCABULARY_SNAPSHOT_KEY] = agent_providers.CURRENT_VOCABULARY
    # Always: this daemon verifies the current CP frame label, so a CP of the previous release signs with it.
    snapshot[node_frame_auth.WIRE_LABELS_SNAPSHOT_KEY] = node_frame_auth.CURRENT_WIRE_LABELS
    return snapshot


def _tailnet_dns_name() -> str | None:
    """`console_direct.resolve_tailnet_dns_name`, never raising: a fault costs this field, not the snapshot."""
    try:
        return console_direct_mod.resolve_tailnet_dns_name()
    except Exception as exc:   # noqa: BLE001 — an optional field may never break the snapshot
        log.warning("could not resolve the tailnet DNS name to publish: %s", exc)
        return None


def resolve_daemon_version() -> str | None:
    """The version of the code this daemon is running, or None when nothing can say: the installed
    `jaah` package's version (apt or dnf, `pkgmgr_loader`), else the repo-root `VERSION` file. A known
    version is cached for the process lifetime (an upgrade restarts the unit); None is asked again, so a
    read that raced the package transaction does not stick. Tests call `.cache_clear()`."""
    version = _known_daemon_version()
    if version is None:
        _known_daemon_version.cache_clear()
    return version


@functools.lru_cache(maxsize=1)
def _known_daemon_version() -> str | None:
    installed = pkgmgr_loader.load().installed_version("jaah")
    if installed:
        return installed
    try:
        text = (HUB_ROOT / "VERSION").read_text().strip()
    except OSError as exc:
        log.warning("resolve_daemon_version: no installed package and no readable %s/VERSION: %s", HUB_ROOT, exc)
        return None
    return text or None


resolve_daemon_version.cache_clear = _known_daemon_version.cache_clear  # type: ignore[attr-defined]  # the tests' handle


def _usable_account_rows(rows: list[dict]) -> list[dict]:
    """RAW roster rows → the ones whose `config_dir` is materialized on THIS host.

    Reuses `fleet_dispatch.account_dir_usable` so bucket membership and dispatch cannot disagree.
    `config_dir` stays the registry's literal string (a fleet-wide-folded column compared exactly).
    Applied every tick over a TTL-cached roster: dir existence is a local fact that must land in one
    tick, so caching this filter's output would be the bug."""
    # Lazy: `fleet_dispatch` pulls the dispatcher's world in.
    from fleet_dispatch import account_dir_usable

    return [
        r for r in rows
        if isinstance(r, dict) and r.get("config_dir")
        # The row's own provider: the credential file differs per vendor, and the default would drop
        # every non-default account from the availability bucket.
        and account_dir_usable(r.get("config_dir"), provider=r.get("provider"))
    ]


def _read_account_sources() -> tuple[list[dict], bool, list[str]]:
    """BOTH account-derived inputs one projection pass needs, off ONE roster read, in ONE worker thread:
    `(projection_roster, roster_ok, probe_config_dirs)`.

    Not one list: the first is filtered to this host and `_norm_dir`-normalized; the second is UNFILTERED
    (`registry_norm`), since `ReconcileContext.config_dirs` must cover every account the CP knows. Built
    together off one cached `reconcile.accounts_probe.account_roster()` read, on a thread (the read blocks)."""
    raw, ok = accounts_probe.account_roster()
    try:
        return _usable_account_rows(raw), ok, accounts_probe._agent_config_dirs(raw)
    except Exception as exc:  # noqa: BLE001 — a raising stat must not take the tick: it would skip the
        # whole projection while the heartbeat keeps the node ONLINE. `ok=False` keeps the complement
        # DELETE shut.
        log.warning("account projection: roster filter failed, preserving prior account.*: %s", exc)
        return [], False, []


# No node-side carry-forward floor: a node publishes only a quota it observed itself; the CP's COALESCE
# owns carry-forward. The incident is single-homed in `collector._collect_resources`'s docstring.


# The `ObserveAccountIn` typed columns; the rest rides in `extra`. This side of the wire schema, pinned
# by `tests/test_main.py::test_account_observations_split_matches_the_wire_schema` and the CP's
# `tests/test_account_fold.py::test_split_observation_covers_the_wire_schema_exactly`.
_OBSERVED_ACCOUNT_TYPED_FIELDS = ("config_dir", "enabled", "email", "display_name")


def _account_observations(composed: list[tuple[str, dict]]) -> list[dict]:
    """Composed `account.<slug>` records → `ObserveAccountIn` payload dicts for the observation POST.
    Decides typed vs `extra` fields, nothing else. `quota: null` = no first-hand reading this pass."""
    payload: list[dict] = []
    for slug, record in composed:
        quota = record.get("quota")
        payload.append({
            "slug": slug,
            "config_dir": record.get("config_dir"),
            "enabled": record.get("enabled"),
            "email": record.get("email"),
            "display_name": record.get("display_name"),
            "quota": quota if isinstance(quota, dict) else None,
            "extra": {
                key: value
                for key, value in record.items()
                if key not in _OBSERVED_ACCOUNT_TYPED_FIELDS and key not in ("slug", "quota")
            },
        })
    return payload


def _compose_account_records(
    roster: list[dict], collected: list[dict],
    probe_covered: set[str] | None = None,
) -> list[tuple[str, dict]]:
    """Compose the `account.<slug>` records this tick publishes. Precedence is `collected` < `roster`.

    `roster` (DB SSOT, re-read every tick) owns which accounts exist plus `enabled`/`node_id`/
    `config_dir`/`slug`, and wins outright, so a stale probe never resurrects a deleted account or undoes
    a disable. `collected` owns live identity and quota, first-hand and dated.

    `probe_covered` separates "no probe has run" (None) from "a probe ran and produced nothing for this
    slug"; only the second may stamp `uncovered`. A probe record with no roster entry is carried through
    as-is — the mid-login case, where dropping it strands the Add-Account wizard."""
    by_dir = {_norm_dir(rec.get("config_dir")): rec for rec in collected if rec.get("config_dir")}
    records_per_slug = {rec.get("slug"): rec for rec in collected if rec.get("slug")}

    out: list[tuple[str, dict]] = []
    claimed: set[str] = set()
    for entry in roster:
        norm_cd = _norm_dir(entry.get("config_dir"))
        slug = entry.get("slug") or Path(norm_cd).name
        if not slug:
            continue
        probed = by_dir.get(norm_cd) or {}
        record = dict(probed)
        uncovered = probe_covered is not None and not probed and norm_cd not in probe_covered
        if uncovered:
            # Credential-less slugs were already filtered, so this means "has a credential, no record
            # this pass". No quota: a flaky probe must not state a verdict, and sending nothing preserves it.
            record["status"] = "no_credentials"
        elif probe_covered is None and "status" not in record:
            # No opinion must be SAID: the CP merges `extra` with `||`, so an absent key would preserve a
            # stale `no_credentials` forever. The guard keeps the probe's own word winning.
            record["status"] = None
        record["slug"] = slug
        record["config_dir"] = norm_cd
        record["enabled"] = entry.get("enabled", True) is not False
        record["node_id"] = entry.get("node_id")
        out.append((slug, record))
        claimed.add(slug)
        if probed.get("slug"):
            claimed.add(probed["slug"])

    # Probe-only records (mid-login slugs with no roster row yet) — verbatim, nothing to overlay.
    for slug, probed in records_per_slug.items():
        if slug in claimed:
            continue
        out.append((slug, dict(probed)))
    return out



# The node observes nothing about a lease: the CP writes the row before issuing the intent.
# Do not re-add a node-side lease read.


def _microvm_marked() -> bool:
    """Whether this is a marked MicroVM node; never on a community-edition node, which carries no MicroVM."""
    return cloud_node is not None and cloud_node.microvm_marked()


def _microvm_facts():
    """`cloud_node.microvm_facts`: the §4.3 block's collector, only ever asked on a marked node."""
    return cloud_node.microvm_facts()


class NodeStatePublisher:
    """Owns this node's observation projection and its presence heartbeat, published each idle tick.
    Every sink is HTTP or the node-bus socket; a failed sink retries next tick.

    Each `tick` maintains the complete projection from ground truth with per-key revision CAS, which
    requires this tick to be each family's single writer; keys no longer desired are pruned. Blind-read
    guard: a failed or partial agents probe preserves `runner.*` and `meta.runners_updated`, and a failed
    roster read preserves `account.*`, so a blip never looks like "they all died"."""

    def __init__(
        self,
        node_id: str,
        agents_probe=None,
        reconcile_lock: asyncio.Lock | None = None,
        idle_interval: float = DEFAULT_IDLE_INTERVAL,
    ) -> None:
        self._node_id = node_id
        # "The same pass" window for `_pool_tick_body`: the loop's actual interval, so an override holds.
        self._idle_interval = idle_interval
        # The in-flight pool-tick GET for this pass and when it STARTED (`_begin_pool_tick`), shared so
        # the projection and the slot-intent drain make one GET per pass.
        self._pool_tick_flight: asyncio.Task | None = None
        self._pool_tick_flight_at: float = 0.0
        # Injectable `claude agents` probe, shared so a tick forks exactly one. None ⇒ the real probe.
        self._agents_probe = agents_probe
        # Daemon-lifetime: a probe result must outlive the per-tick context, or every account forks
        # `claude agents` every 5s.
        self._agents_cache = accounts_probe.AgentsProbeCache(agents_probe=agents_probe)
        # Daemon-lifetime: the CPU figure is a delta against the previous read, so a per-pass sampler
        # would never produce one (and so it cannot live on the per-tick `ReconcileContext`).
        self._host_metrics = host_metrics_mod.HostMetricsSampler()
        # Live identity and quota. Empty until the first refresh: a restart publishes quota-less records
        # for one interval, which is correct (`quota: null` = no first-hand reading).
        self._account_probe: list[dict] = []
        # config_dirs the last SUCCESSFUL probe covered; None until one completes (see
        # `_compose_account_records`).
        self._account_probe_covered: set[str] | None = None
        # Monotonic stamp of the last probe FIRING (not completion). None ⇒ never fired.
        self._account_probe_at: float | None = None
        self._account_probe_task: asyncio.Task | None = None
        # The NodeLink share's mount state as last read, None ⇒ not yet read; and the read in flight
        # (`_poll_share_mount`).
        self._share_mounted_seen: bool | None = None
        self._share_mount_task: asyncio.Task | None = None
        # The last `nodelink_state` read, published in the config snapshot; None until one lands.
        self._nodelink_state: str | None = None
        # The `nodelinks` and `shared_accounts_root` values of that same read (`share_reads`).
        self._nodelinks_value: str | None = None
        self._shared_accounts_root: str | None = None
        # Wired to `ConsoleBridge.has_live_session` after construction; None ⇒ `console_live()` is False.
        self._console_live_probe = None
        # Wired to `DirectConsoleListener.published_url`; a callable, asked at each snapshot tick.
        self._direct_console_url_probe = None
        # Serializes this projection against the CommandBridge's `reconcile` verb (no concurrent CAS).
        # Shared when `_serve` injects one; else created lazily so it binds to the running loop.
        self._reconcile_lock = reconcile_lock
        # Intents the CP pushed over the node bus not yet handed to a pass, verbatim pool-tick shape (one
        # parser, one executor table). Event-loop-only, so unlocked. Loss is harmless: the `slot_intents`
        # row stays pending and the next pool tick re-serves it.
        self._pushed_intents: list[dict] = []
        # A LEVEL ("a pass is owed"), so N pushes during a pass coalesce into one follow-up.
        self._pushed_pass_pending = False
        # Set from when a pushed pass commits to its I/O until it acquires the lock or abandons — spanning
        # the pool-tick fetch and roster read, not the locked body (`_project_pushed_pass`). A second pass
        # seeing it stands down losslessly: the first has not yet run `_take_pushed_intents`.
        self._pushed_pass_waiting = False
        # The launch lane's own lock, separate from the reconcile lock; lazily created.
        self._launch_lock: asyncio.Lock | None = None
        # The pane-write lane's own lock (`orchestrate.SIGNAL_LANE_VERBS`): a launch that hangs holds the launch
        # lock, and an answer must not wait behind it. Lazily created.
        self._signal_lock: asyncio.Lock | None = None
        # The in-flight `reconcile.accounts_probe.start_agents_refresh` task, so `close()` can cancel it on SIGTERM.
        self._agents_refresh_task: asyncio.Task | None = None
        # Publish-on-change gate. `None` = never published; `{}` is a distinct, publishable snapshot.
        self._last_config_snapshot: dict[str, str] | None = None
        # The last §4.3 MicroVM block that landed, and when (`microvm_facts.due`). None on an unmarked node.
        self._last_microvm: dict | None = None
        self._last_microvm_at: float | None = None
        # The node-bus client whose `heartbeat` frame IS presence; injected by `_serve`.
        self._node_bus = None

    # Every store is keyed by the node id VERBATIM: no sanitizing, or `node.a` and `node_a` collide.

    @property
    def agents_cache(self) -> "accounts_probe.AgentsProbeCache":
        """This daemon's one cross-tick `claude agents` cache, handed to the CommandBridge's `reconcile`
        verb. Read-only: this publisher's tick owns the refresh."""
        return self._agents_cache

    async def publish_heartbeat(self) -> None:
        """The presence liveness signal, and ONLY that: one `heartbeat` frame to the CP, awaited INLINE
        by `main.run` so a wedged loop stops heartbeating truthfully. Logged, never raised.

        Nothing on the presence path may await anything that can stall. The CP stamps `last_seen` off
        this, not `observed_at`: alive vs SENSED, and occupancy trust (`app/pool_state.py`) needs the distinction."""
        client = self._node_bus
        if client is None:
            log.warning(
                "presence heartbeat skipped — no node-bus client wired on this publisher, so the control "
                "plane will flip this node OFFLINE on TTL expiry"
            )
            return
        try:
            await client.send_heartbeat()
        except Exception as exc:
            log.warning(
                "presence heartbeat failed — node-bus frame did not send, so the control plane will "
                "flip this node OFFLINE on TTL expiry: %s", exc
            )

    async def project_state(self, *, wait_for_lock: bool = False) -> None:
        """One observation pass: probe reality, execute the CP's intents, and POST the lot. Heavy (up to
        15s per slot probe), so `_serve` runs it as a single-flight `bus-projection` step, never inline.

        `wait_for_lock` waits out the serialization lock instead of skipping: False for the periodic step
        (an in-flight pass covers the same keys), True for a CP-pushed intent's pass, which carries work no
        other pass is doing."""
        # Sampled above the pass, so ticks abandoned on the lock are covered too. Once per pass that
        # runs, so intervals are unequal (longest when busiest) — hence `host_metrics.reading()`'s
        # time-weighting. Never raises. Off the loop: every 30s it walks all of /proc (`process_metrics`), which
        # on a build-heavy host is long enough to stall lease pushes and the credential socket.
        await asyncio.to_thread(self._host_metrics.sample)
        try:
            await self._project_node_state(wait_for_lock=wait_for_lock)
        except Exception as exc:
            log.warning("node-state projection for %s failed: %s", self._node_id, exc)

    def _direct_console_url(self) -> str | None:
        """The direct console origin to publish, or None when nothing is bound. Never raises: a fault
        costs this field, not the snapshot."""
        probe = self._direct_console_url_probe
        if probe is None:
            return None
        try:
            return probe()
        except Exception as exc:   # noqa: BLE001 — an optional field may never break the snapshot
            log.warning("could not resolve the direct console URL to publish: %s", exc)
            return None

    async def publish_config_snapshot(self) -> bool:
        """Publish this node's redacted config snapshot to `POST /api/agent/nodes/config-snapshot` at
        startup and whenever it CHANGES (CP side: `app/node_config.py`). `None` from the client means the
        POST did not land; latching it would stop this node's config being reported for the process life."""
        if _microvm_marked():
            # Before the read below: a consent a VM guest approved is published on this same pass, so
            # `resolve_remote_settings_consent` can publish what a guest approved.
            await asyncio.to_thread(cloud_node.fold_back_vm_consents)
        # Cheap: a readdir plus one small JSON per account, behind the publish-on-change latch.
        consent, consent_updated_at = resolve_remote_settings_consent()
        snapshot = build_config_snapshot(
            version=resolve_daemon_version(),
            # What actually bound, asked of the listener — the CP hands it straight to a browser.
            direct_console_url=self._direct_console_url(),
            # Always passed, so the key is always published (absent = "too old to say").
            remote_settings_consent=consent,
            remote_settings_consent_updated_at=consent_updated_at,
            # Cached after the first ask; off the loop because that first `tailscale status` is bounded at 10s.
            tailnet_dns_name=await asyncio.to_thread(_tailnet_dns_name),
            # The latest off-loop read (`_poll_share_mount`); never read here, where it could block.
            nodelink=self._nodelink_state,
            nodelinks=self._nodelinks_value,
            shared_accounts_root=self._shared_accounts_root,
        )
        # A MARKED node's §4.3 block rides every POST it makes: the CP stores the snapshot whole, so a POST
        # without it would erase the node's last report. Its volatile free-RAM/disk figures alone publish at
        # most once a minute.
        microvm = None
        if _microvm_marked():
            microvm = await asyncio.to_thread(_microvm_facts().collect)
        microvm_due = microvm is not None and _microvm_facts().due(
            microvm, self._last_microvm, self._last_microvm_at, time.monotonic())
        unmarked_now = microvm is None and self._last_microvm is not None
        if snapshot == self._last_config_snapshot and not microvm_due and not unmarked_now:
            return False
        # Blocking urllib, so off the event loop.
        delivered = await asyncio.to_thread(
            control_plane_api_mod.agent_api_config_snapshot,
            self._node_id, snapshot, env_helpers.now_iso(), microvm,
        )
        if delivered is None:
            log.warning("config snapshot publish failed (POST did not land) — will retry next call")
            return False
        self._last_config_snapshot = snapshot
        self._last_microvm = microvm
        self._last_microvm_at = time.monotonic() if microvm is not None else None
        return True

    async def tick(self) -> None:
        """Heartbeat + projection in one call, for standalone callers and tests; `_serve` wires the
        halves separately."""
        await self.publish_heartbeat()
        await self.project_state()

    def _serialization_lock(self) -> asyncio.Lock:
        """The shared reconcile-serialization lock, created lazily so it binds to the running loop."""
        if self._reconcile_lock is None:
            self._reconcile_lock = context.new_reconcile_lock()
        return self._reconcile_lock

    async def _project_node_state(self, *, wait_for_lock: bool = False) -> None:
        """Serialize the per-tick projection against the CommandBridge's `reconcile` verb, then run it.

        A reconcile in flight means this tick is SKIPPED, not queued: it already refreshes the same keys
        (`reconcile.context.new_reconcile_lock`). `wait_for_lock` inverts that for a CP-pushed intent's pass, whose
        intent the in-flight pass read too early to carry; waiting on the same lock never runs two passes
        together. That path is `_project_pushed_pass`; this only routes to it."""
        lock = self._serialization_lock()
        if wait_for_lock:
            await self._project_pushed_pass(lock)
            return
        if lock.locked():
            log.debug(
                "bus tick: a reconcile is already in flight — skipping this tick's projection "
                "(avoids concurrent CAS writers on worktree.*/fetch_stamp.*)"
            )
            return
        roster, roster_ok, config_dirs = await self._pass_inputs()
        if lock.locked():
            log.debug("bus tick: a reconcile started while reading the account roster — skipping")
            return
        async with lock:
            await self._project_node_state_locked(roster, roster_ok, config_dirs)

    async def _pass_inputs(self) -> tuple[list[dict] | None, bool | None, list[str] | None]:
        """What every projection pass needs before taking the lock, in the order both callers require.

        The pool tick starts before the first `await`, flying in parallel with the account read, and
        `_project_node_state_locked` JOINS that flight — one pool-tick GET per pass. The accounts are read
        OUTSIDE the lock: a blocking call of up to 60s that needs nothing the lock protects, and holding
        it would stall every other lock consumer. One threaded read (`_read_account_sources`)."""
        self._begin_pool_tick()
        return await asyncio.to_thread(_read_account_sources)

    async def _project_pushed_pass(self, lock: asyncio.Lock) -> None:
        """The projection pass a CP-PUSHED intent spawns — the one that waits on the lock.

        The stand-downs and the flag sit above the I/O: with the flag set only at the wait, a burst of
        pushes each spent its own pool-tick GET and executor thread during the roster read, starving the
        executor that acks intents. One waiter at a time, and standing down is lossless (a parked pass has
        not read the queue yet). Re-checked after the acquire: FIFO hands the lock on in turn, and an
        empty queue then means our intent was already enacted."""
        if self._pushed_pass_waiting:
            log.debug("bus tick: a pushed-intent pass is already waiting on the lock — it takes ours")
            return
        if not self._pushed_intents:
            log.debug("bus tick: the push queue was drained before this pass started — standing down")
            return
        self._pushed_pass_waiting = True
        try:
            # Both, in order (`_pass_inputs`), claimed under the flag above.
            roster, roster_ok, config_dirs = await self._pass_inputs()
            # Bounded, and the handler wraps the wait and nothing else. FIFO acquire serves this pass on
            # the next release; the bound stops a wedged reconcile parking it forever (the intent stays
            # queued and pending CP-side). Narrow on purpose: since 3.11 `asyncio.TimeoutError` is the
            # builtin `TimeoutError` (as is `socket.timeout`), so a wider `try` would misreport a CP read
            # timeout as lock contention.
            try:
                await asyncio.wait_for(lock.acquire(), PUSHED_INTENT_LOCK_WAIT)
            except asyncio.TimeoutError:
                log.warning(
                    "bus tick: a reconcile has held the serialization lock for %.0fs — abandoning this "
                    "pushed-intent pass; the periodic projection still drains it",
                    PUSHED_INTENT_LOCK_WAIT,
                )
                return
        finally:
            # Cleared on every exit from the wait: left set it would stand every later push down;
            # held through the work it would stand down a push arriving after the drain.
            self._pushed_pass_waiting = False
        try:
            if not self._pushed_intents:
                log.debug("bus tick: the queue was drained by the pass ahead of this one — standing down")
                return
            await self._project_node_state_locked(roster, roster_ok, config_dirs)
        finally:
            lock.release()

    async def _project_node_state_locked(
        self,
        roster: list[dict] | None = None,
        roster_ok: bool | None = None,
        config_dirs: list[str] | None = None,
    ) -> None:
        """Maintain the COMPLETE `node_state_<nodeId>` projection each tick. `roster`/`roster_ok` are
        read by the caller outside the serialization lock; omitted ⇒ read here.

        This tick is the single writer of `account.*` — `state.py`'s D2 invariant (one writer per node)
        is what makes lock-free per-key revision CAS safe. Never move this write to another process."""
        # The fallback read first: it also produces `config_dirs`, which the context needs, or the
        # context would make its own event-loop-blocking read.
        if roster_ok is None:
            roster, roster_ok, config_dirs = await asyncio.to_thread(_read_account_sources)
        # Fetched here and handed to the pass as `tick=`; `join=True` picks up the flight started before
        # the account read — one pool-tick call per pass.
        pool_tick = tick_wire.parse_pool_tick(await self._pool_tick_body(join=True))
        # One shared probe cache for the slot observations and the runner projection.
        ctx = context.ReconcileContext(
            # The UNFILTERED list off the same cached roster (`_read_account_sources`); `None` = read it.
            config_dirs=config_dirs,
            # A successful read is known coverage whatever it returned; a failed one passes `None` and
            # `reconcile.accounts_probe._roster_coverage_known` decides from stale rows without a read.
            coverage_known=roster_ok or None,
            agents_probe=self._agents_probe,
            console_live_probe=self._console_live_probe,
            node_id=self._node_id,
            agents_cache=self._agents_cache,
        )
        # Refreshes the shared `claude agents` snapshot to its freshness floors. STARTED, NOT AWAITED:
        # awaiting put the per-account fan-out in front of `reconcile_pass`'s execute arm, i.e. in front
        # of every runner launch. The pass joins it between its execute arm and its probe loop.
        agents_refresh = accounts_probe.start_agents_refresh(ctx)
        # Held on the publisher so `close()` can cancel it on SIGTERM if something raises before the
        # join. A prior handle is cancelled, never overwritten into an uncancellable orphan.
        stranded = self._agents_refresh_task
        if stranded is not None and not stranded.done():
            stranded.cancel()
        self._agents_refresh_task = agents_refresh

        # (0) account.* is composed before the pass, which POSTs it (feeding `account_fold`'s coverage gate).
        self._maybe_refresh_account_probe()
        composed_accounts: list[tuple[str, dict]] = []
        if roster_ok:
            composed_accounts = _compose_account_records(
                roster or [], self._account_probe,
                probe_covered=self._account_probe_covered,
            )

        # (1) The one sink: the observation POST, inside the pass. `accounts_ok` is the COMPLEMENT-DELETE
        # gate and must never be a constant True, or a blind roster read deletes every account row.
        observed = await orchestrate.reconcile_pass(
            node_id=self._node_id,
            ctx=ctx,
            tick=pool_tick,
            accounts=_account_observations(composed_accounts),
            accounts_ok=bool(roster_ok),
            # None omits the wire KEY, so the CP stores nothing and ERASES nothing — unlike the three
            # families above, absence here asserts nothing.
            host_metrics=self._host_metrics.reading(),
            # Pushed intents, merged ahead of the polled ones and de-duplicated by id. Drained here, past
            # every early return, so an abandoned pass leaves them queued.
            extra_intents=self._take_pushed_intents(),
            # The backstop lane: an intent landing mid-pass is enacted between slot probes.
            drain_pushed=self._take_pushed_intents,
            # Joined between the execute arm and the probe loop.
            agents_refresh=agents_refresh,
        )
        # The CP's warn line rides the observe response (`disk_warn_pct`); the reclaim trigger asks against it.
        disk_reclaim_trigger_mod.record_observe_response(observed)
        # `root_grow`: the CP enlarged this machine's root volume; ask for the partition to follow it.
        root_grow_request_mod.record_observe_response(observed)

    # A FAILED probe is retried this soon instead of after a whole interval: until one succeeds, every drain
    # pass declines this node ("no credential account ... is usable"), so a first probe that raced a CP
    # still starting would otherwise idle a fresh node for the full Settings interval (600s by default).
    ACCOUNT_PROBE_FAILED_RETRY_SECS = 30.0
    # The probe cadence while this node is provisioning accounts (`AgentsProbeCache.provisioned_since`).
    PROVISIONING_WINDOW_SECS = 300.0
    PROVISIONING_PROBE_SECS = 60.0

    def _maybe_refresh_account_probe(self) -> None:
        """Fire the account probe if its interval has elapsed and no refresh is already in flight.

        Not awaited: a live OAuth call per account under the projection lock would stall every tick.
        The in-flight guard matters because a slow probe can outlive several interval checks."""
        self._poll_share_mount()
        if self._account_probe_task is not None and not self._account_probe_task.done():
            return
        now = time.monotonic()
        # The cadence is a live API fan-out per account, read per call (Settings > Infrastructure > Daemon).
        interval = node_settings_mod.get("node_account_probe_interval_secs")
        # While accounts are being provisioned here (a new node's first minutes), each becomes usable as its
        # detached converge ends: probe often enough that it shows within a minute, not a whole interval.
        if self._agents_cache.provisioned_since(self.PROVISIONING_WINDOW_SECS):
            interval = min(interval, self.PROVISIONING_PROBE_SECS)
        if self._account_probe_at is not None and (now - self._account_probe_at) < interval:
            return
        self._account_probe_at = now
        self._account_probe_task = asyncio.create_task(self.refresh_account_probe())

    def _poll_share_mount(self) -> None:
        """Re-probe accounts the tick after the token store mounts or unmounts. A store that comes up after
        start changes which accounts' tokens this node can read, and nothing else tells the daemon (a
        co-located node's share is mounted by its HOST), so every one read `missing` for a whole probe
        interval.

        The read is `ismount` on an NFS path, which blocks without a timeout on a hung server: so it runs on
        a thread, one at a time, and its answer lands a tick later. A read that raised (a bad override) is
        no answer. Only the probe floor drops — the roster is filtered per read, so it needs no refresh."""
        task = self._share_mount_task
        if task is not None:
            if not task.done():
                return
            self._share_mount_task = None
            if not task.cancelled() and task.exception() is None:
                self._nodelink_state, self._nodelinks_value, self._shared_accounts_root = task.result()
                mounted = self._nodelink_state.startswith("mounted")
                if self._share_mounted_seen is not None and mounted != self._share_mounted_seen:
                    log.info("NodeLink share %s: re-probing accounts now", "mounted" if mounted else "unmounted")
                    self._account_probe_at = None
                self._share_mounted_seen = mounted
        self._share_mount_task = asyncio.create_task(asyncio.to_thread(share_reads))

    def nudge_account_probe(self) -> None:
        """Drop both account freshness floors so the next tick re-reads: the account-probe interval and
        `reconcile.accounts_probe.ACCOUNT_ROSTER_TTL_SECS`. Called after a successful account command, closing the
        Add-Account wizard hang: the roster decides whether the slug exists, the probe whether it has an
        identity and quota. Synchronous; the last good data survives until a fresh read replaces it."""
        self._account_probe_at = None
        accounts_probe.invalidate_account_roster()

    async def refresh_account_probe(self) -> None:
        """Refresh `self._account_probe` from the live account probe on its own cadence (a live OAuth call
        per account). Off the event loop; it publishes only what it read itself (the incident single-homed
        in `collector._collect_resources`'s docstring). The tick stays the single writer of `account.*`.
        A failed probe keeps the last good cache and covered set; the whole body is guarded because a bare
        `create_task`'s exception surfaces only as a GC warning."""
        try:
            import collector  # lazy: keeps collector.py off the daemon's normal import path

            resources = await asyncio.to_thread(collector._collect_resources)
        except Exception as exc:  # noqa: BLE001 — a failed probe keeps the LAST good cache, never clears it
            log.warning("account probe refresh failed, keeping prior probe data: %s", exc)
            # Due again in ACCOUNT_PROBE_FAILED_RETRY_SECS (`_maybe_refresh_account_probe` ages it out).
            interval = node_settings_mod.get("node_account_probe_interval_secs")
            self._account_probe_at = time.monotonic() - interval + self.ACCOUNT_PROBE_FAILED_RETRY_SECS
            return
        accounts = (resources or {}).get("accounts") or []
        if isinstance(accounts, list):
            self._account_probe = [a for a in accounts if isinstance(a, dict)]
            self._account_probe_covered = {
                _norm_dir(a.get("config_dir")) for a in self._account_probe if a.get("config_dir")
            }

    def _begin_pool_tick(self) -> asyncio.Task:
        """START this pass's one `GET /api/agent/pool-tick` for the pass's other steps to JOIN.
        Synchronous: it must run before the caller's first `await`. A shared future, not a cached body:
        the second half looks for it before any body exists, so a body cache would never hit."""
        task = asyncio.ensure_future(
            asyncio.to_thread(control_plane_api_mod.agent_api_pool_tick, self._node_id or "")
        )
        # Nobody is guaranteed to await it (the caller may bail on the lock), so retrieve the exception
        # here rather than leave a GC-time "never retrieved" warning.
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        self._pool_tick_flight = task
        self._pool_tick_flight_at = time.monotonic()
        return task

    async def _pool_tick_body(self, *, join: bool) -> dict:
        """This pass's pool-tick body — `{}` when the CP could not be reached. Never raises.

        `join=True` reuses a fetch from THIS pass; anything older than one idle interval is refused and
        a fresh one started. The bound is about enacting against a stale tick: the body carries intents
        with `decided_on_observed_at` tokens, and a tick from an earlier pass would be stale."""
        flight = self._pool_tick_flight
        if (
            join
            and flight is not None
            and not flight.cancelled()
            and (time.monotonic() - self._pool_tick_flight_at) < self._idle_interval
        ):
            task = flight
        else:
            task = self._begin_pool_tick()
        try:
            # Shielded: two coroutines await this task, and cancelling one must not cancel the other's.
            body = await asyncio.shield(task)
        except Exception as exc:  # noqa: BLE001 — an unreachable CP must never wedge the daemon's tick
            log.warning("pool-tick unavailable (%s) — no instruction this pass", exc)
            return {}
        return body if isinstance(body, dict) else {}

    async def _fresh_pool_tick_body(self) -> dict:
        """A pool-tick body fetched now, outside the pass's shared flight (`_pool_tick_flight`), which it
        leaves alone — `{}` when the CP could not be reached. Never raises."""
        try:
            body = await asyncio.to_thread(control_plane_api_mod.agent_api_pool_tick, self._node_id or "")
        except Exception as exc:  # noqa: BLE001 — an unreachable CP must never wedge a lane
            log.warning("pool-tick unavailable (%s) — no instruction this lane", exc)
            return {}
        return body if isinstance(body, dict) else {}

    def push_slot_intent(self, intent: dict) -> None:
        """"Here is a slot intent that is pending for you" — enact it NOW rather than on the next tick.

        Called inline from the node-bus read loop on a `send_intent` frame (e.g. an operator answering
        a gate on a runner frozen in `AskUserQuestion`). The intent is QUEUED, not enacted here: it is one
        pool-tick entry verbatim, so the pass parses and executes it like a polled one and
        `execute_intents` de-duplicates it against its polled twin.

        Spawn-and-return: anything awaited here blocks this node's one socket. Level-triggered and
        coalescing; the level is cleared where the work begins (`_serve_pushed_intents`), not here.
        Never raises: off a running loop the level is reset and the periodic pass covers it.

        A `microvm` rail frame is not a slot intent: it goes straight to that rail's executor (which never
        blocks), and only on a marked node; elsewhere the pool tick's copy is ignored the same way."""
        if isinstance(intent, dict) and intent.get("rail") == "microvm":
            if _microvm_marked() and host_platform.is_windows():
                bridge = cloud_modules.load("microvm_bridge")
                client = bridge.windows_client() if bridge is not None else None
                if client is not None:
                    client.push_rail(intent)   # the WSL2 backend's rail executor (`microvm_bridge`)
            elif _microvm_marked():
                rail = tick_wire._microvm_rail()
                rail.enact(rail.intents_from_tick({rail.TICK_KEY: [intent]}))
            return
        if isinstance(intent, dict) and intent:
            self._pushed_intents.append(intent)
        if self._pushed_pass_pending:
            return
        self._pushed_pass_pending = True
        try:
            asyncio.ensure_future(self._serve_pushed_intents())
        except RuntimeError as exc:
            self._pushed_pass_pending = False
            log.warning(
                "pushed slot intent could not be scheduled (%s) — the periodic projection pass still "
                "drains it", exc,
            )

    async def _serve_pushed_intents(self) -> None:
        """One pushed projection pass, guarded. Spawned by `push_slot_intent`, never awaited by it.

        The level is cleared as the work begins: everything this pass reads comes after this line, so a
        later push earns its own pass, while one that landed while this was queued is absorbed. The guard
        exists because a bare `create_task`'s exception surfaces only at shutdown, unattributed."""
        self._pushed_pass_pending = False
        try:
            # The fast lanes first, and for most pushes they are all of this method: neither a `lease_slot`
            # nor a pane write touches the keys the serialization lock protects, and waiting on a long
            # projection pass delayed launches by tens of seconds and answers by minutes. Outside the lock is
            # the point. The pane-write lane goes first and under its own lock: an answer never waits on a
            # launch.
            took = False
            for verbs, lock, pane_write in ((orchestrate.SIGNAL_LANE_VERBS, self._signal_lane_lock(), True),
                                            (orchestrate.LAUNCH_LANE_VERBS, self._launch_lane_lock(), False)):
                lane = self._take_lane_intents(verbs)
                if not lane:
                    continue
                took = True
                try:
                    # A declined intent (an older one still pending for its slot) is requeued for the
                    # projection pass, which can order both channels.
                    declined = list(await self._run_launch_lane(lane, lock, pane_write=pane_write))
                except Exception as exc:  # noqa: BLE001 — one lane's fault must not strand the next lane's work
                    log.warning("pushed slot-intent lane failed: %s — its intents stay pending for the next "
                                "tick", exc)
                    continue
                if declined:
                    self._pushed_intents[0:0] = declined
            # Skipped only when the lanes took everything; an empty queue still runs, per the level
            # contract (this cannot tell a late push from a drained queue).
            if took and not self._pushed_intents:
                return
            # The one caller that waits on the lock (see `_project_node_state`); same lock, so it
            # serialises and no slot is launched into twice.
            await self.project_state(wait_for_lock=True)
        except Exception as exc:  # noqa: BLE001 — a latency pass may never take the daemon with it
            log.warning("pushed slot-intent pass failed: %s — the periodic projection will retry", exc)

    def _take_lane_intents(self, verbs: frozenset[str]) -> "list[tuple[object, dict]]":
        """Take one fast lane's intents (those of VERBS) OFF the push queue, leaving every other entry on it.

        In place, not drain-and-requeue: the projection drains inside the lock, which is what makes
        `_pushed_pass_waiting`'s stand-down lossless; draining out here would make it a race."""
        if not self._pushed_intents:
            return []
        lane, rest = orchestrate.launch_lane_intents(self._pushed_intents, verbs)
        # `rest` keeps arrival order; anything the parser refused is dropped here.
        self._pushed_intents = list(rest)
        return list(lane)

    def _launch_lane_lock(self) -> asyncio.Lock:
        """The launch lane's own mutual exclusion, created lazily so it binds to the running loop.

        Not the serialization lock — a lease writes none of the projection keys. This only keeps two
        launch passes from interleaving; per-tree exclusion is `reconcile.execute.slot_lock`, inside
        `execute_intent`."""
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()
        return self._launch_lock

    def _signal_lane_lock(self) -> asyncio.Lock:
        """The pane-write lane's mutual exclusion, as `_launch_lane_lock` and created lazily for the same reason."""
        if self._signal_lock is None:
            self._signal_lock = asyncio.Lock()
        return self._signal_lock

    async def _run_launch_lane(self, intents: "list", lock: asyncio.Lock, *,
                               pane_write: bool = False) -> "tuple[dict, ...]":
        """Enact one fast lane's pushed intents under that lane's LOCK, without touching the projection.

        The launch lane joins the pool tick when this pass already started one. A PANE_WRITE lane fetches its
        own: the tick's pending set is what orders the write behind an older intent for its slot
        (`orchestrate.launch_pass`), and a joined tick up to one idle interval old misses a `kill_runner`
        minted since — the answer would be typed into a session about to die, and typing cannot be undone. It
        reads no accounts either; a pane write uses none. Nothing here publishes."""
        async with lock:
            if pane_write:
                roster_ok, config_dirs = None, None
                tick = tick_wire.parse_pool_tick(await self._fresh_pool_tick_body())
            else:
                # Off the loop, as in `_pass_inputs`: a blocking call of up to 60s against a hung CP, on the
                # path a human is waiting on.
                _roster, roster_ok, config_dirs = await asyncio.to_thread(_read_account_sources)
                tick = tick_wire.parse_pool_tick(await self._pool_tick_body(join=True))
            ctx = context.ReconcileContext(
                config_dirs=config_dirs,
                # Same reading, same reason as the projection pass.
                coverage_known=roster_ok or None,
                agents_probe=self._agents_probe,
                console_live_probe=self._console_live_probe,
                node_id=self._node_id,
                agents_cache=self._agents_cache,
            )
            return await orchestrate.launch_pass(ctx, tick, intents)

    def _take_pushed_intents(self) -> list[dict]:
        """Drain the push queue into the pass that is about to run.

        Emptied in one step and never re-queued on failure: a pushed intent is an early copy of a row
        the CP still holds, served again by the next pool tick; re-queueing would re-enact acked work."""
        if not self._pushed_intents:
            return []
        pushed = self._pushed_intents
        self._pushed_intents = []
        return pushed

    # No GitHub-issue monitors: the console Needs-attention view and the DB parked-set surface humans.

    async def close(self) -> None:
        # Cancel an in-flight account probe; only the coroutine's unwind is awaited (a blocked
        # `to_thread` worker cannot be cancelled).
        task = self._account_probe_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # The probe's own cancellation is expected; OUR cancellation belongs to the caller's
                # shutdown and must propagate.
                if not task.cancelled():
                    raise
            except Exception as exc:  # noqa: BLE001 — a probe error must not abort teardown
                log.warning("account probe raised during shutdown: %s", exc)
        # Same for the in-flight agents-cache refresh, or SIGTERM leaves "Task was destroyed but it is
        # pending!" at exit.
        refresh = self._agents_refresh_task
        if refresh is not None and not refresh.done():
            refresh.cancel()
            try:
                await refresh
            except asyncio.CancelledError:
                if not refresh.cancelled():
                    raise
            except Exception as exc:  # noqa: BLE001 — a refresh error must not abort teardown
                log.warning("agents-cache refresh raised during shutdown: %s", exc)
        # Same for an in-flight agents full scan.
        scan = self._agents_cache.full_scan_task
        if scan is not None and not scan.done():
            scan.cancel()
            try:
                await scan
            except asyncio.CancelledError:
                if not scan.cancelled():
                    raise


def log_startup_posture() -> str:
    """Say out loud which posture this process resolved and which control plane it will dial.

    First, because it distinguishes a node idle because contained from one idle because its own
    configuration could not be read. The control plane is reported as RESOLVED: in sandbox posture with
    none given that is the refusal (AD-3), not a loopback guess. The URL goes through
    `_scrub_url_userinfo`, the same rule as the config snapshot."""
    posture = environment_mod.posture()
    try:
        target = _scrub_url_userinfo(control_plane_api_mod.resolve_control_plane_api_base())
    except environment_mod.SandboxControlPlaneUnset:
        target = "none — refused to guess one (set JAAH_CP_BASE_URL, or --sandbox-cp)"
    log.info(
        "posture: %s — pool root %s, node identity file %s, control plane %s%s",
        posture, environment_mod.pool_root_or_empty() or "(unset)",
        environment_mod.node_env_path(), target,
        f", sandbox root {SANDBOX_ROOT}" if SANDBOX_ROOT else "")
    return posture


async def _serve(idle_interval: float) -> None:
    # The startup gate, called only here: a `node.env` present but unreadable means a provisioned node
    # that would heartbeat, look healthy, and claim nothing. Never called as the session user, which
    # legitimately cannot read that 0640 root:jaahd file (`env_helpers.resolve_node_token`).
    environment_mod.require_startable()
    # The edition and the tree agree: a node whose edition includes a feature `cloud_node` wires (the MicroVM
    # listener, NodeLink converge, GHA pool, upgrade path, vnode-host converge) but lacks the module is a broken
    # install, never a quiet community-edition posture.
    if cloud_node is None and any(edition_features.enabled(name) for name in CLOUD_NODE_FEATURES):
        raise SystemExit("jaahd: this cloud node's tree lacks cloud_node.py — a broken install; reinstall the package")
    log_startup_posture()
    # FIRST, before any tmux client: one hands the runner-owned server its whole environment
    # (`env_helpers.scrub_node_token_from_environ`).
    if env_helpers.scrub_node_token_from_environ():
        log.info("NODE_TOKEN removed from the daemon's environment; it is read from node.env from here on")
    _prepare_windows_host()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(sig: signal.Signals) -> None:
        log.info("received %s — shutting down", sig.name)
        stop_event.set()

    # Windows' proactor loop has no signal handlers; the service host ends the daemon process outright
    # (`JaahService.cs` OnStop), so there is no signal to catch there.
    if not host_platform.is_windows():
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _request_stop, sig)

    node_id = resolve_node_id()
    # The cloud node's own subsystems (`cloud_node.CloudNode`); None on a community-edition node.
    cloud = cloud_node.CloudNode() if cloud_node is not None else None
    if cloud is not None:
        cloud.request_nodelink_converge()
    # One reconcile lock shared with the CommandBridge's `reconcile` verb, created inside the running loop.
    reconcile_lock = context.new_reconcile_lock()
    # The publisher's "same pass" window tracks the loop's actual interval.
    publisher = NodeStatePublisher(node_id, reconcile_lock=reconcile_lock, idle_interval=idle_interval)
    worktree_agents_enabled = resolve_worktree_agents_enabled()

    async def _config_snapshot_step() -> None:
        # Publish-on-change is internal, so firing every tick is a cheap compare that also retries a
        # startup publish lost to a not-yet-up transport.
        await publisher.publish_config_snapshot()

    linux_only = _linux_only_steps_run()
    periodic_steps: list[PeriodicStep] = [
        # The node-state projection — single-flight in the background, never delaying the heartbeat.
        PeriodicStep("bus-projection", publisher.project_state, 0.0),
        # The redacted config snapshot — publishes only on change.
        PeriodicStep("config-snapshot", _config_snapshot_step, 0.0),
        # The cloud node's GHA runner pool, NodeLink re-ask and vnode-host converge (`cloud_node`).
        *(cloud.periodic_steps(PeriodicStep, linux_only=linux_only) if cloud is not None else []),
        *([
            # A Windows node's container engine not serving: a warning line the CP reads as a Health finding
            # (`windows_engine_health`); Linux and macOS engines are watched by their own units.
            PeriodicStep("engine-health", windows_engine_health_mod.EngineHealthReporter().step,
                         windows_engine_health_mod.INTERVAL_SECONDS),
            # The fleet tmux server's anchor, verified while its chain to `jaah-runner` is alive, before an
            # update's restart of that service cuts it; a stale server is replaced, and one still unverified is a
            # Health finding (`windows_anchor_health`).
            PeriodicStep("fleet-anchor", windows_anchor_health_mod.AnchorHealthReporter().step,
                         windows_anchor_health_mod.INTERVAL_SECONDS),
        ] if host_platform.is_windows() else []),
        # Linux-only subsystems (`_linux_only_steps_run`): skipped on macOS/Windows nodes.
        *([
            # A vnode's accounts, once per package version: the host's postinst converges only its own.
            *([PeriodicStep("vnode-account-converge", _vnode_account_converge_step, 300.0)]
              if environment_mod.is_vnode() else []),
            # The disk reclaim: ask the root `jaah-reclaim.path` for a pass every 15 min, every 5 while the
            # disk is at or above the CP's warn line; then upload the spooled reports. A vnode never asks
            # (its host's pass covers it) but still uploads.
            PeriodicStep("disk-reclaim", disk_reclaim_trigger_mod.DiskReclaimTrigger().step,
                         disk_reclaim_trigger_mod.STEP_INTERVAL_S),
        ] if linux_only else []),
        # Project memory: re-fetch each ON runner's bundle file (`project_memory_bundle`); a no-op with none. The
        # community edition serves no project memory.
        *([PeriodicStep("project-memory", project_memory_bundle_mod.refresh_step,
                        project_memory_bundle_mod.REFRESH_INTERVAL_S)]
          if edition_features.enabled("project_memory") else []),
        # The Architectural Guard: re-fetch each guarded session's bundle file and ship its spool
        # (`guard_bundle`); a no-op with none.
        *([PeriodicStep("guard", guard_bundle_mod.refresh_step, guard_bundle_mod.REFRESH_INTERVAL_S)]
          if guard_bundle_mod is not None else []),
        # This node's public IPv4 for the headscale edge's allowlist; tailnet-joined nodes only (D13), so never a
        # community-edition node.
        *([PeriodicStep("egress-report", egress_report_mod.EgressReporter().step,
                        egress_report_mod.INTERVAL_SECONDS)] if edition_features.enabled("tailnet") else []),
        # This node's own journal, redacted, to CloudWatch Logs via the CP (SOC2 G-18); no-op on a vnode. A cloud
        # feature: a local node has no CloudWatch behind its CP, and its container no journalctl.
        *([PeriodicStep("node-logs", node_logs_mod.NodeLogForwarder().step, node_logs_mod.INTERVAL_SECONDS)]
          if edition_features.enabled("node_log_archive") else []),
        # Vendor-CLI model catalogues for tier resolution of providers without moving aliases.
        PeriodicStep("model-catalogs", provider_models_mod.ModelCatalogReporter().step,
                     provider_models_mod.INTERVAL_SECONDS),
        # A non-Claude runner's `StopFailure`: those CLIs fire no hook on an API-error turn, so the node
        # reads how each runner's transcript ends and files the break for it. Every tick: this is the
        # latency that recovery pays over Claude's own hook.
        PeriodicStep("vendor-turn-errors", vendor_turn_errors_mod.VendorTurnErrorSensor().step, 0.0),
        # Live Jaah-account metering (K4): each Bedrock session's new model calls, posted every 10s.
        PeriodicStep("bedrock-usage-tail", bedrock_usage_tail_mod.Tail().step,
                     bedrock_usage_tail_mod.INTERVAL_SECONDS),
        # Narrows every too-wide secret file (node.env audited, account credentials, worktree `.env`) back
        # to its allowed mode: at startup and on an interval, because vendor CLIs and checkouts rewrite them.
        PeriodicStep("secret-modes", secret_modes_mod.step, secret_modes_mod.INTERVAL_SECONDS),
        # NodeVault: mint this node's identity key on first start and register its public half until the CP
        # acknowledges it; backs off on a CP without the route, then idles for the life of the process.
        PeriodicStep("nodevault-identity", nodevault_identity_mod.IdentityRegistrar().step,
                     nodevault_identity_mod.INTERVAL_SECONDS),
        # No `slot-intent-drain` step: every slot-intent verb drains through `bus-projection`'s pass.
    ]
    # No self-update step (dpkg/apt owns deploy); instead the connect-time ASK below. No arrival
    # announcement: the node-bus `hello` frame on every (re)connect is the registration.

    console_bridge = None
    if console_bridge_mod.CONSOLE_ENABLED:
        try:
            # Opens no connection: the bridge dials the CP only when a viewer asks for a console.
            console_bridge = console_bridge_mod.ConsoleBridge(node_id, HUB_ROOT)
            await console_bridge.start()
        except Exception as exc:
            # Optional: the daemon's other loops keep running without it.
            log.error("console bridge failed to start (%s) — Console disabled, other loops unaffected", exc)
            console_bridge = None

    # The direct console listener (`console_direct.py`): gated on having a tailnet identity, and it needs
    # the console bridge.
    direct_listener = None
    if console_bridge is not None:
        try:
            direct_listener = await console_direct_mod.build_listener(
                # `daemon_state_dir()`, not `HUB_ROOT` (dpkg's read-only tree on a node).
                console_bridge, node_id=node_id, cert_dir=daemon_state_dir() / "direct-console",
            )
        except Exception as exc:
            # Not fatal, but loud: a browser with the direct toggle ON does not quietly relay, it reports
            # this and keeps retrying, so this line is the explanation.
            log.error(
                "direct console listener failed to start (%s) — direct mode unavailable on this node; "
                "consoles with the direct toggle ON will report this and keep retrying rather than "
                "relaying, so fix the listener or have them switch it off", exc,
            )
            direct_listener = None

    # The reconcile pass's DONE-close gate; None ⇒ `console_live()` reports False.
    publisher._console_live_probe = console_bridge.has_live_session if console_bridge is not None else None
    # Only a listener that actually BOUND advertises itself — see `_direct_console_url`.
    publisher._direct_console_url_probe = (
        direct_listener.published_url if direct_listener is not None else None)

    # The credential broker's unix socket, living exactly as long as the daemon (its cache is
    # process-lifetime state). Non-fatal: a bind failure costs local callers, not the pool tick.
    cred_server = await cred_broker_mod.start()
    # The MicroVM guests' control listener (on this same broker) and the egress relay. Optional: a failure costs
    # MicroVM sessions, never the daemon (a raise here would crash-loop the node off the control plane).
    if cloud is not None:
        try:
            cloud.start(asyncio.get_running_loop(), stop_event)
        except Exception:  # noqa: BLE001 — logged with its traceback; the node stays up without MicroVM
            log.exception("microvm: control listener / egress relay did not start — MicroVM sessions unavailable")

    commands_bridge = None

    def _new_command_bridge() -> "commands_bridge_mod.CommandBridge":
        return commands_bridge_mod.CommandBridge(
            node_id, HUB_ROOT,
            console_live_probe=console_bridge.has_live_session if console_bridge is not None else None,
            reconcile_lock=reconcile_lock,
            # Ungated: the account probe runs on every node.
            account_probe_nudge=publisher.nudge_account_probe,
            # The projection's cache, so the `reconcile` verb (fired by every runner Stop hook) reads a
            # warm snapshot.
            agents_cache=publisher.agents_cache,
        )

    if commands_bridge_mod.COMMANDS_ENABLED:
        # Fed by `deliver()` from the node-bus client; `start()` only binds the semaphore.
        commands_bridge = _new_command_bridge()
        await commands_bridge.start()

    # The WebSocket node bus: built unconditionally because it carries PRESENCE, which must
    # not depend on whether this node accepts commands.
    def _refuse_command(msg) -> None:
        async def _reply() -> None:
            await msg.respond(json.dumps({
                "v": BUS_SCHEMA_VERSION, "status": "rejected",
                "result": {"reason": "command execution is disabled on this node"},
            }).encode())

        asyncio.create_task(_reply())

    node_bus_task = None
    node_bus_client = node_bus_mod.NodeBusClient(
        node_id,
        deliver_command=commands_bridge.deliver if commands_bridge is not None else _refuse_command,
        open_console=console_bridge.handle_console_open if console_bridge is not None else None,
        # The PULL trigger for package convergence; the opt-in is resolved inside the callback.
        on_connected=_upgrade_check_on_connect if cloud is not None else None,
        # The node is the authority on direct-console grants (`DirectGrantStore`); None with no listener
        # makes a grant frame answer "disabled" instead of vanishing.
        grant_direct_console=direct_listener.grants.grant if direct_listener is not None else None,
        revoke_direct_console=direct_listener.grants.revoke if direct_listener is not None else None,
        # The latency push for slot intents: ungated, and loss-tolerant — the row stays
        # pending for the next poll.
        on_send_intent=publisher.push_slot_intent,
        on_vnode_converge=cloud.on_vnode_converge if cloud is not None else None,
        on_uninstall=uninstall_request_mod.request_uninstall,
    )
    if direct_listener is not None:
        # Post-construction: the listener is built before the bus. Audit logging is local regardless.
        direct_listener.report_use = node_bus_client.send_direct_used
    # Presence goes through this client, so a wedged idle loop stops it.
    publisher._node_bus = node_bus_client
    # Resolved once, before the task starts: in sandbox posture with none given it RAISES (AD-3), and
    # inside the forever-reconnecting client that would be a traceback per attempt. So: not started,
    # one line.
    try:
        cp_ws_url = node_bus_mod.resolve_cp_ws_url()
    except environment_mod.SandboxControlPlaneUnset as exc:
        cp_ws_url = ""
        log.info("node-bus: not dialling — %s", exc)
    node_bus_task = (asyncio.create_task(node_bus_client.run_forever(stop_event))
                     if cp_ws_url else None)
    if cp_ws_url:
        # Scrubbed like the snapshot and posture line: derived from `JAAH_CP_BASE_URL`.
        log.info("node-bus: dialling %s", _scrub_url_userinfo(cp_ws_url))

    # Operator alerts POST to `POST /api/agent/alerts`; the CP owns dedup and relay. No latency
    # wake here: dispatch is CP-side, and the pool tick executing `lease_slot` runs every tick.
    log.info(
        "jaahd started (idle_interval=%ss, node_id=%s, cp=%s, "
        "worktree_agents_enabled=%s, console=%s, commands=%s)",
        idle_interval,
        node_id,
        cp_ws_url or "none (sandbox posture, no control plane given)",
        worktree_agents_enabled,
        console_bridge is not None,
        commands_bridge is not None,
    )
    try:
        await run(
            idle_interval,
            stop_event,
            # The inline liveness signal only; the projection rides `bus-projection`.
            publish=publisher.publish_heartbeat,
            worktree_agents_enabled=worktree_agents_enabled,
            periodic_steps=tuple(periodic_steps),
        )
    finally:
        await publisher.close()
        if cloud is not None:
            await cloud.stop()
        if node_bus_task is not None:
            # Closing the socket IS the deregister on this transport.
            node_bus_task.cancel()
            await asyncio.gather(node_bus_task, return_exceptions=True)
        if cred_server is not None:
            # Stops clients connecting to a socket whose daemon has gone.
            await cred_broker_mod.stop(cred_server)
        if direct_listener is not None:
            # Before the console bridge: stop accepting, so no connection arrives looking for a gone
            # bridge; the bridge's stop() closes the sessions.
            await direct_listener.stop()
        if console_bridge is not None:
            await console_bridge.stop()
        if commands_bridge is not None:
            await commands_bridge.stop()
    log.info("jaahd exiting cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="jaahd — the Jaah node daemon")
    parser.add_argument(
        "--idle-interval",
        type=float,
        default=DEFAULT_IDLE_INTERVAL,
        help="seconds between idle ticks (default: %(default)s)",
    )
    parser.add_argument(
        "--sandbox",
        default=None,
        metavar="ROOT",
        help="run in SANDBOX posture with every outward surface under ROOT: the pool root, the "
        "credential socket, the tmux socket dir, the node identity file and the plugin marketplace root "
        "all move inside it, the session-uid switch is disabled, and the package-upgrade marker and the "
        "shared account tree are disabled OUTRIGHT rather than redirected (a redirected marker is worse "
        "than none — the write succeeds and is reported as an upgrade request while no root path unit is "
        "watching it). No control plane is assumed: without --sandbox-cp every control-plane resolution "
        "RAISES rather than falling back to a loopback that on a dev box is a live control plane. This is how "
        "the daemon is exercised on a machine it does not own; it adopts no node identity, so it is by "
        "construction not a second node daemon. NOTE: this flag is also parsed BEFORE the module imports "
        "(see the sandbox bootstrap at the top of this file), because three of the surfaces bind at "
        "import; it is declared here so --help documents it and a misspelling is refused.",
    )
    parser.add_argument(
        "--sandbox-cp",
        default=None,
        metavar="URL",
        help="the control plane a --sandbox process may dial. Sandbox posture is GIVEN its control plane "
        "or has none at all — there is deliberately no default, because the loopback a provisioned node "
        "falls back to is a LIVE control plane on any dev box.",
    )
    parser.add_argument(
        "--sweep-sessions",
        action="store_true",
        help="run one session-transcript sweep and exit without starting the daemon — the node-tier "
        "`session-transcript-sweep` ScheduledTask shells this each tick. It drains "
        "`GET /api/agent/sessions/work` serially (redact, gzip and PUT the main transcript, every "
        "subagent transcript and the prompts that exist, each through its own CP-issued presigned "
        "URL), RECONCILES this node's own project directories against the control plane's rows via "
        "`POST /api/agent/sessions/observed` (horizon-bounded, so it is a sweep and not the deferred "
        "backfill), and reports any session it can prove is abandoned. Best-effort: exceptions are "
        "logged per session and the tick continues.",
    )
    parser.add_argument(
        "--sweep-slot-residue",
        action="store_true",
        help="run one slot-residue sweep and exit without starting the daemon — the node-tier "
        "`slot-residue-sweep` ScheduledTask shells this. It removes what erased slots left outside their "
        "trees (scratch dirs, task staging dirs, stale pytest runs, compose stacks), AS THE SESSION USER; "
        "`slot_residue.py` holds the fences.",
    )
    args = parser.parse_args()
    if host_platform.is_windows():
        # Inherited by every tmux client and Git shell this daemon starts (`windows.with_msys_noglob`).
        from host_platform import windows  # noqa: PLC0415 — Windows-only
        os.environ["MSYS"] = windows.with_msys_noglob(os.environ.get("MSYS"))
    # Before any one-shot acts: they run under whatever posture the bootstrap applied.
    _assert_sandbox_parses_agree(args)
    if args.sweep_sessions:
        # Lazy: path-loads `hooks/break_cause.py`, only reached through this one-shot.
        import session_sweep

        # Exceptions propagate (non-zero exit); per-session failures stay inside `sweep_once`.
        session_sweep.sweep_once()
        return
    if args.sweep_slot_residue:
        # Lazy so import cost and import-time failure land in this one-shot's exit code.
        import slot_residue

        # Exceptions propagate; a skipped kind (an unreadable listing) exits zero with its reason logged.
        slot_residue.sweep()
        return
    asyncio.run(_serve(args.idle_interval))


if __name__ == "__main__":
    main()
