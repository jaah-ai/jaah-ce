"""environment.py — every outward surface this daemon reaches, resolved in ONE place.

The nine surfaces (node-owned, not checkout-owned): the control plane it dials, the credential socket
it binds, the upgrade marker it writes, the shared account tree it reads, the pool root it walks, the
tmux server it drives, the uid it switches to, the node identity file, and the plugin-marketplace root.
(The lint's `SURFACES` also guards `JAAH_CP_WS_URL`, the control plane's websocket spelling.)

STDLIB ONLY, importing nothing from `jaahd` but the stdlib-only `host_platform` seam (which owns each
OS's layout; the defaults below are its values): several resolvers live in stdlib-only modules that
`hooks/` loads by path, so they delegate DOWN to this leaf. Every accessor reads `os.environ` at call
time. Two postures (`classify` has the table): `provisioned` — every surface is the node's own;
`sandbox` — everything under one GIVEN root, a control plane only if given (never a loopback guess),
and the upgrade marker and shared account tree DISABLED, not redirected. Three off-machine dials check
`is_sandbox()` where they live: `oauth_endpoint_check`, `console_direct`, `reconcile.accounts_probe._agent_config_dirs`.

Not owned here (pinned literals that must stay literals): `shared_storage.LEGACY_SHARED_ACCOUNTS_ROOT` and
`plugin_repair.MARKETPLACES["jaah"]` — this module takes them as arguments and decides the posture.
Enforced by the monorepo's environment-surfaces check.
"""
from __future__ import annotations

import logging
import os
import socket
import stat
import urllib.error
from pathlib import Path

from jaah_env import env_get  # stdlib-only leaf beside this one

import host_platform  # stdlib-only seam: the OS's layout, whose Linux values are the literals below

log = logging.getLogger("jaahd.environment")

# ── The postures ────────────────────────────────────────────────────────────────────────────────────
POSTURE_PROVISIONED = "provisioned"
POSTURE_SANDBOX = "sandbox"

# `classify`'s verdicts, finer than postures: two rows share a posture with different things to say.
VERDICT_PROVISIONED = "provisioned"
VERDICT_PROVISIONED_PARTIAL = "provisioned_partial"
VERDICT_SANDBOX = "sandbox"
VERDICT_SANDBOX_PARTIAL = "sandbox_partial"
VERDICT_NODE_ENV_UNREADABLE = "node_env_unreadable"

_VERDICT_POSTURES = {
    VERDICT_PROVISIONED: POSTURE_PROVISIONED,
    VERDICT_PROVISIONED_PARTIAL: POSTURE_PROVISIONED,
    VERDICT_SANDBOX: POSTURE_SANDBOX,
    VERDICT_SANDBOX_PARTIAL: POSTURE_SANDBOX,
    # NOT sandbox: a present `node.env` is a real node, whatever this process can read of it.
    VERDICT_NODE_ENV_UNREADABLE: POSTURE_PROVISIONED,
}


# ── Refusals ────────────────────────────────────────────────────────────────────────────────────────

class UnreadableNodeEnv(RuntimeError):
    """`/etc/jaah/node.env` is present and could not be read. The daemon REFUSES TO START on this.

    Carries the file and the errno (the whole remedy: the file is 0640 root:jaahd by contract)."""

    def __init__(self, path: str, errno: int | None, detail: str) -> None:
        super().__init__(
            f"the node identity file {path} is present but could not be read (errno {errno}: {detail}). "
            f"This node IS provisioned, so starting anyway would run a daemon that holds no node token: "
            f"every lease claim, roster read and registration would take effect for no node, silently. "
            f"It is 0640 root:jaahd by contract - `jaah auth login <node_id>` rewrites it.")
        self.path = path
        self.errno = errno


class SandboxSurfaceDisabled(OSError):
    """A surface that sandbox posture disables outright was asked for anyway.

    Not an error to swallow into a default: these surfaces reach off the machine. An `OSError` so the
    handlers written for an unreachable resource (several with never-raises contracts) catch it; see
    `SandboxControlPlaneUnset` for why that alone is not enough. `errno` stays `None`."""


class SandboxControlPlaneUnset(SandboxSurfaceDisabled, urllib.error.URLError):
    """Sandbox posture was asked which control plane to dial and was never given one (AD-3).

    Also a `urllib.error.URLError`: `URLError` SUBCLASSES `OSError`, so `except URLError` (10 of
    `control_plane_api`'s 12 handlers) does not catch a bare `OSError`. Both bases catch all 12.

    Raises rather than falling back because the loopback default is a LIVE console on a dev box."""

    def __init__(self) -> None:
        super().__init__(
            f"sandbox posture was not given a control plane: set {CP_BASE_URL_ENV} to the one this "
            f"process may dial. There is deliberately no default - the loopback fallback a provisioned "
            f"node uses is a LIVE control plane on any dev box, so guessing it here would aim a sandbox at "
            f"production.")


# ── The env var names: the single home of every one of these spellings ──────────────────────────────
CP_BASE_URL_ENV = "JAAH_CP_BASE_URL"
# Not a tenth surface: the control plane's websocket spelling. It OUTRANKS the http base in
# `node_bus.resolve_cp_ws_url()`, so `sandbox_env` DELETES it. This module owns the spelling only.
CP_WS_URL_ENV = "JAAH_CP_WS_URL"
CRED_SOCKET_ENV = "JAAH_CRED_SOCKET"
UPGRADE_MARKER_ENV = "JAAH_UPGRADE_MARKER"
SESSION_USER_ENV = "JAAH_SESSION_USER"
TMUX_TMPDIR_ENV = "TMUX_TMPDIR"
POOL_ROOT_ENV = "JAAH_HOME"
NODE_ENV_FILE_ENV = "JAAH_NODE_ENV_FILE"
# Opt-in overrides for surfaces that otherwise have none; unset never changes a provisioned answer.
SHARED_ACCOUNTS_ROOT_ENV = "JAAH_SHARED_ACCOUNTS_ROOT"
MARKETPLACE_ROOT_ENV = "JAAH_MARKETPLACE_ROOT"
# Virtual-node relocation, all opt-in (unset = a regular node, pinned by `tests/test_environment_vnode.py`):
# the tmpfs runtime tree every `/run/jaah/<x>` derives from, the daemon's unix account, this node's
# `tailscaled` socket, and the vnode flag itself (`is_vnode`).
RUN_ROOT_ENV = "JAAH_RUN_ROOT"
DAEMON_USER_ENV = "JAAH_DAEMON_USER"
TAILSCALE_SOCKET_ENV = "JAAH_TAILSCALE_SOCKET"
VNODE_ENV = "JAAH_VNODE"
# Where the static pool's git mirrors live when the daemon's own state dir is not traversable by its session
# user: a vnode's (`vnodehelper.layout.mirrors_dir`) or a macOS node's (`node_bootstrap.MACOS_POOL_MIRRORS`).
# Unset = `<daemon state>/pool-mirrors` (`git_mirror`).
POOL_MIRRORS_ENV = "JAAH_POOL_MIRRORS"

# NOT a surface: the node identity `enter_sandbox` drops, since a launching shell may export them (e.g.
# the compose env guard exports `NODE_ID`) and a sandbox holding a real id could act AS that node.
# Dropped in `enter_sandbox`, not `sandbox_env` (tests pin their own identity); dropped, not faked
# (`main.resolve_node_id`'s "" is the honest answer).
NODE_IDENTITY_ENV = ("NODE_ID", "NODE_NAME", "NODE_TOKEN")

# ── The hardcoded defaults a provisioned node falls back to ─────────────────────────────────────────
# Made by `jaahd.service`'s `ExecStartPre`; the defaults below derive from `run_root()` at call time.
# Their values are the OS layout's (`host_platform.paths()`): on Linux exactly `/run/jaah/...` and
# `/etc/jaah/...`, which clients and guards pin.
_LAYOUT = host_platform.paths()
RUN_ROOT_DEFAULT = _LAYOUT.run_dir
# The contract path clients hardcode, shared by the credential broker and the vocabulary socket.
CRED_SOCKET_DEFAULT = _LAYOUT.creds_socket
# Watched by `jaahd-upgrade.path`, which starts a ROOT package install within milliseconds of a write.
UPGRADE_MARKER_DEFAULT = f"{_LAYOUT.run_dir}/requests/upgrade"
# The tmux socket directory the unit's ExecStartPre creates; tmux appends its own `tmux-<uid>` level.
TMUX_TMPDIR_DEFAULT = _LAYOUT.tmux
# The node's identity-and-credential file, loaded by the packaged unit as an EnvironmentFile.
NODE_ENV_FILE_DEFAULT = _LAYOUT.node_env
# The uid fleet sessions run as. Empty (not absent) disables the switch by contract — see `session_user`.
# On Windows the local account `jaah` and the daemon's MSYS spelling (`NT SERVICE+jaahd`, what tmux's
# `server-access` takes); imported only there, so Linux/macOS never load the Windows module.
# On macOS the installer's hidden service users `_jaah` / `_jaahd` (`host_platform.macos`).
if host_platform.is_windows():
    from host_platform import windows as _windows
    SESSION_USER_DEFAULT = _windows.RUNNER_ACCOUNT
    DAEMON_USER_DEFAULT = _windows.DAEMON_MSYS_USER
elif host_platform.is_macos():
    from host_platform import macos as _macos
    SESSION_USER_DEFAULT = _macos.RUNNER_ACCOUNT
    DAEMON_USER_DEFAULT = _macos.DAEMON_ACCOUNT
else:
    SESSION_USER_DEFAULT = "jaah"
    # The daemon's own unix account (D10).
    DAEMON_USER_DEFAULT = "jaahd"
# `tailscale`'s own default; `--socket` is passed only when the override is set.
TAILSCALE_SOCKET_DEFAULT = "/var/run/tailscale/tailscaled.sock"

# Suffixes under the run root; the vnode test asserts they join to the constants above.
_RUN_CRED_SOCKET = ("creds", "sock")
_RUN_UPGRADE_MARKER = ("requests", "upgrade")
_RUN_TMUX = ("tmux",)


def _stripped(name: str) -> str:
    return env_get(name, "").strip()


# ── The posture table ───────────────────────────────────────────────────────────────────────────────

def classify() -> str:
    """Which of the five verdicts describes this process's environment, read fresh on every call.

    Keyed on `JAAH_HOME` and the node identity file, never a self-declared marker (a misconfigured
    node could assert one). `IS_SANDBOX` is unrelated: Claude Code's root escape, set on cloud nodes.

      | `JAAH_HOME` | `node.env`            | verdict               | posture     |
      |---------------|-----------------------|-----------------------|-------------|
      | set           | present + readable    | `provisioned`         | provisioned |
      | unset         | absent                | `sandbox`             | sandbox     |
      | set           | absent                | `sandbox_partial`     | sandbox     |
      | unset         | present + readable    | `provisioned_partial` | provisioned |
      | either        | present + UNREADABLE  | `node_env_unreadable` | provisioned |

    The last row matters: a provisioned node that cannot read its config must not sandbox (it would start,
    look healthy and claim nothing), so it keeps the provisioned posture and `require_startable()`
    refuses startup. The refusal is not here: session-user processes cannot read the 0640 file on a
    healthy node and must not die for it. `_partial` verdicts are logged by `posture()`, not refused."""
    home_set = bool(_stripped(POOL_ROOT_ENV).rstrip("/"))
    path = node_env_path()
    try:
        with open(path, "rb"):
            pass
    except (FileNotFoundError, NotADirectoryError):
        return VERDICT_SANDBOX_PARTIAL if home_set else VERDICT_SANDBOX
    except IsADirectoryError:
        # Something created the path: broken provisioning, not absence — refuse rather than sandbox.
        return VERDICT_NODE_ENV_UNREADABLE
    except OSError:
        return VERDICT_NODE_ENV_UNREADABLE
    return VERDICT_PROVISIONED if home_set else VERDICT_PROVISIONED_PARTIAL


_logged_partial: set[str] = set()

# Set by `enter_sandbox`. NEVER an input to `classify()` (posture must not be assertable); it only changes
# the `sandbox_partial` log line, which a documented `--sandbox ROOT` start lands on by construction and
# whose fleet-node repair advice would be wrong there.
_entered_sandbox_deliberately = False

# Announce-once state for the shared-tree override, keyed by VALUE (a repoint is worth saying again).
_logged_shared_override: set[str] = set()


def posture() -> str:
    """`provisioned` or `sandbox` — the two words the rest of this daemon branches on.

    A `_partial` verdict is logged ONCE per process (read from hot paths). `provisioned_partial` is INFO
    (every dev checkout). `sandbox_partial` is a WARNING: a fleet node reaches it only by a deleted or
    dangling `node.env`, and systemd's tolerant `EnvironmentFile=-` starts it anyway, claiming nothing."""
    verdict = classify()
    if verdict in (VERDICT_SANDBOX_PARTIAL, VERDICT_PROVISIONED_PARTIAL) \
            and verdict not in _logged_partial:
        _logged_partial.add(verdict)
        if verdict == VERDICT_SANDBOX_PARTIAL and _entered_sandbox_deliberately:
            # The documented `--sandbox ROOT` path: INFO, no repair advice.
            log.info(
                "posture: sandbox (entered deliberately) - every outward surface is under %s, and the "
                "node identity file %s is absent because this process is meant to have no node identity",
                _stripped(POOL_ROOT_ENV), node_env_path())
        elif verdict == VERDICT_SANDBOX_PARTIAL:
            log.warning(
                "posture: sandbox, partially provisioned - %s is set (%s) but the node identity file %s "
                "is absent, so this process adopts no node identity. If this IS a fleet node, that file "
                "was deleted, renamed or is a dangling symlink: the daemon will start (the unit's "
                "EnvironmentFile is the tolerant `-` form) and claim nothing - no node token, no shared "
                "account tree, no upgrade channel - until `jaah auth login <node_id>` rewrites it",
                POOL_ROOT_ENV, _stripped(POOL_ROOT_ENV), node_env_path())
        else:
            log.info(
                "posture: provisioned, partially configured - the node identity file %s is present but "
                "%s is unset, so nothing here can resolve where node-managed state lives",
                node_env_path(), POOL_ROOT_ENV)
    return _VERDICT_POSTURES[verdict]


def is_sandbox() -> bool:
    """Shorthand for `posture() == POSTURE_SANDBOX`."""
    return posture() == POSTURE_SANDBOX


def require_startable() -> None:
    """Raise `UnreadableNodeEnv` when this node's identity file is present but unreadable; else return.

    The daemon's startup gate and nobody else's (see `classify`), called first in `main._serve`. Also
    refuses a half-relocated vnode and a malformed `JAAH_RUN_ROOT`: a vnode that fails OPEN lands on the
    HOST's socket, markers and tailnet identity."""
    run_root()
    require_vnode_relocated()
    if classify() != VERDICT_NODE_ENV_UNREADABLE:
        return
    path = node_env_path()
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        raise UnreadableNodeEnv(path, exc.errno, exc.strerror or str(exc)) from exc
    # Readable now: fixed between the classify and here.


# ── The nine surfaces ───────────────────────────────────────────────────────────────────────────────

def node_env_path() -> str:
    """The node-level identity/token file: `$JAAH_NODE_ENV_FILE` when set, else `/etc/jaah/node.env`.

    Posture-independent: it is the INPUT to the posture table."""
    return env_get(NODE_ENV_FILE_ENV) or NODE_ENV_FILE_DEFAULT


def pool_root() -> str:
    """The root directory under which THIS node keeps every jaah-managed thing on disk. RAISES when
    `JAAH_HOME` is unset (no fallback; see `env_helpers.jaah_home`). Checked after the rstrip: `"/"`
    would otherwise yield `""` and filesystem-root-anchored paths."""
    stripped = _stripped(POOL_ROOT_ENV).rstrip("/")
    if not stripped:
        raise RuntimeError(
            "JAAH_HOME is not set - this process cannot resolve where jaah-managed state "
            "(accounts, fleet config, logs) lives. Set it via /etc/jaah/env, sourced by the "
            "daemon's systemd unit and every fleet launcher - there is no default.")
    return stripped


def pool_root_or_empty() -> str:
    """The pool root, or `""` when unset — for the callers whose whole design is to degrade rather than
    raise (a containment check that must not turn a real slot invisible, an announce-once log line).

    A second accessor, not a `default=` flag: raising and degrading are different contracts."""
    return _stripped(POOL_ROOT_ENV)


def pool_root_raw() -> str:
    """The pool root exactly as the environment spells it — unstripped, `""` when absent.

    For the two callers that do their own normalisation and would be CHANGED by ours
    (`worktree_mechanics`' realpath containment and its `rstrip`-then-fall-back-to-a-derived-root)."""
    return env_get(POOL_ROOT_ENV) or ""


def hook_log_dir() -> Path:
    """`$FLEET_LOG_DIR`, else `<pool root>/log`, else `~/.jaah/log`, under the `hook/` subdir — the runner hooks' log directory, homed here because every
    hook already imports this leaf and nothing else stdlib-only holds it.

    Mirrors `env_helpers.log_dir` rather than importing it: a hook that fails to import kills its
    session. A SOFT default that never raises, with a `~` fallback for unprovisioned hosts."""
    fallback = os.path.expanduser("~/.jaah")
    root = os.environ.get("FLEET_LOG_DIR") or os.path.join(pool_root_raw() or fallback, "log")
    return Path(root) / "hook"


def control_plane_base(loopback_port: object) -> str:
    """The control plane every HTTP call is built on: `$JAAH_CP_BASE_URL`, else — PROVISIONED ONLY —
    `http://127.0.0.1:<loopback_port>`. In SANDBOX posture with no override, raises
    `SandboxControlPlaneUnset` (AD-3).

    `loopback_port` is passed in: tests monkeypatch `control_plane_api.CONTROL_PLANE_API_PORT`. The
    loopback fallback serves local dev stacks and pooled worktrees. Trailing slashes are stripped."""
    url = env_get(CP_BASE_URL_ENV)
    if url:
        return url.rstrip("/")
    if is_sandbox():
        raise SandboxControlPlaneUnset()
    return f"http://127.0.0.1:{loopback_port}"


def control_plane_base_explicit() -> str:
    """The EXPLICITLY configured control plane, or `""` — no loopback fallback, no posture branch.

    Callers (`node_bus`, `jaah_cli`) read "" as "nothing configured" and try their own next arm; a
    loopback guess would break that."""
    return _stripped(CP_BASE_URL_ENV)


def control_plane_ws_url_explicit() -> str:
    """The EXPLICITLY configured control-plane WEBSOCKET url, or `""` - no default, no posture branch.

    Its own accessor: it OUTRANKS the http base in `node_bus.resolve_cp_ws_url`. No posture branch —
    containment is `sandbox_env` DELETING the variable."""
    return _stripped(CP_WS_URL_ENV)


def credential_socket() -> str:
    """The local credential/vocabulary socket: `$JAAH_CRED_SOCKET`, else `/run/jaah/creds/sock`.

    One socket, two request shapes, one resolution. Overridable so a test or sandbox binds its own. On
    Windows the endpoint is a named pipe, which lives in the pipe namespace, never under the run tree:
    `paths().creds_socket` (`\\\\.\\pipe\\jaah-creds`)."""
    if host_platform.is_windows():
        return env_get(CRED_SOCKET_ENV) or CRED_SOCKET_DEFAULT
    return env_get(CRED_SOCKET_ENV) or run_path(*_RUN_CRED_SOCKET)


def credential_socket_explicit() -> str:
    """The EXPLICITLY configured credential socket, or `""` — no default.

    For the `override or explicit or SOCKET_PATH` shape, which lets a test's rebound attribute win when no
    env var is set."""
    return env_get(CRED_SOCKET_ENV) or ""


def upgrade_requests_enabled() -> bool:
    """May this process ask the root path unit to converge the node's package at all?

    False in sandbox posture with no explicit marker: a redirected marker would report a request no
    path unit watches. Gate the request, then resolve the path. Never on a community-edition node: it is upgraded by
    its installer (`jaah-ce-node install --upgrade`), never from the control plane's apt route."""
    import edition  # noqa: PLC0415

    if edition.IS_CE:
        return False
    return bool(env_get(UPGRADE_MARKER_ENV)) or not is_sandbox()


def upgrade_marker() -> str:
    """The marker file whose creation asks for a package converge: `$JAAH_UPGRADE_MARKER`, else
    `/run/jaah/requests/upgrade`. RAISES `SandboxSurfaceDisabled` in sandbox posture with neither.

    Resolved on call, never at import."""
    explicit = env_get(UPGRADE_MARKER_ENV)
    if explicit:
        return explicit
    if is_sandbox():
        raise SandboxSurfaceDisabled(
            f"sandbox posture disables the package-upgrade marker outright: creating it would ask a "
            f"root path unit to run a package install on a machine this process does not own. Set "
            f"{UPGRADE_MARKER_ENV} to exercise the write against a path of your own.")
    return run_path(*_RUN_UPGRADE_MARKER)


def pool_mirrors_root() -> str:
    """`$JAAH_POOL_MIRRORS` with any trailing `/` stripped, or `""` when unset (`git_mirror.mirrors_root`)."""
    return _stripped(POOL_MIRRORS_ENV).rstrip("/")


def shared_accounts_root(provisioned_default: str) -> str:
    """The shared account tree on this node, or `""` when there is none to read.

    `provisioned_default` is the pinned `shared_storage.LEGACY_SHARED_ACCOUNTS_ROOT`. `""` is the fail-closed
    "no shared token here" every consumer handles, so sandbox disabling it is a no-op for callers.

    The override is VALIDATED (absolute, no `..`, not `/`), not normalised: it decides where every
    account's OAuth token is read and where a refreshed token is written back. In force on
    a provisioned node it warns once."""
    explicit = _stripped(SHARED_ACCOUNTS_ROOT_ENV)
    if not explicit:
        return "" if is_sandbox() else provisioned_default
    trimmed = explicit.rstrip("/")
    if not explicit.startswith("/") or ".." in Path(explicit).parts or not trimmed:
        raise RuntimeError(
            f"{SHARED_ACCOUNTS_ROOT_ENV}={explicit!r} is not usable as the shared account tree: it must "
            f"be an absolute path below `/` with no `..` component. This is where every account's OAuth "
            f"token is read from and where a rotated refresh token is written, so a relative root would "
            f"resolve against whichever cwd the caller holds, a `..` component would let a path that "
            f"looks contained reach the real fleet tree, and `/` strips to nothing - which every consumer "
            f"reads as `this node mounts no share` rather than as an error.")
    if not is_sandbox() and trimmed not in _logged_shared_override:
        _logged_shared_override.add(trimmed)
        log.warning(
            "shared accounts: this PROVISIONED node's fleet credential tree is repointed by %s to %s - "
            "every account's OAuth token is read from and written to there, not to the packaged mount",
            SHARED_ACCOUNTS_ROOT_ENV, trimmed)
    return trimmed


def session_user() -> str:
    """The unix account fleet sessions run as: `$JAAH_SESSION_USER`, default `jaah`, and an EMPTY
    value disables the uid switch by contract.

    Absent and empty differ here: empty is the explicit "no second uid" (dev, CI, sandbox); absent means
    the fleet default. A packaged node does not keep it: its update removes an empty value
    (the Linux package's postinst; the macOS/Windows `jaah-update` re-assert the installer's)."""
    value = env_get(SESSION_USER_ENV)
    if value is None:
        return SESSION_USER_DEFAULT
    return value.strip()


def tmux_tmpdir(env: dict | None = None) -> str:
    """The tmux socket directory this node uses: `$TMUX_TMPDIR`, else `/run/jaah/tmux`.

    Takes `env` so a caller composing a CHILD's environment resolves against it."""
    source = env or os.environ
    return env_get(TMUX_TMPDIR_ENV, "", source).strip() or run_path(*_RUN_TMUX, env=source)


def run_root(env: dict | None = None) -> str:
    """The node's tmpfs runtime tree: `$JAAH_RUN_ROOT`, else `/run/jaah`. Trailing `/` stripped.

    The base of every daemon-owned runtime path, so a vnode setting only this still lands in its own tree.
    Validated like `shared_accounts_root`'s override (it places the credential socket and root-watched
    markers). Takes `env` for `tmux_tmpdir`'s reason."""
    explicit = env_get(RUN_ROOT_ENV, "", env or os.environ).strip()
    if not explicit:
        return RUN_ROOT_DEFAULT
    trimmed = explicit.rstrip("/")
    if not explicit.startswith("/") or ".." in Path(explicit).parts or not trimmed:
        raise RuntimeError(
            f"{RUN_ROOT_ENV}={explicit!r} is not usable as this node's runtime root: it must be an "
            f"absolute path below `/` with no `..` component. Every daemon-owned socket and request marker "
            f"is derived from it, so a relative or escaping root would put them somewhere nobody named.")
    return trimmed


def marketplace_roots(provisioned_defaults: dict[str, str]) -> dict[str, str]:
    """The plugin marketplaces a repair pass may add, with any LOCAL PATH among them contained.

    `provisioned_defaults` is the pinned `plugin_repair.MARKETPLACES`. Remote sources pass through; the
    local path is relocated by `$JAAH_MARKETPLACE_ROOT`.

    A relocated entry whose directory does not exist is DROPPED: `plugin_repair.converge` re-adds on a
    missing directory, so it would fail on every pass forever (and an empty directory would load
    nothing). The un-relocated provisioned path is not existence-checked: missing means a broken package,
    which `plugin_repair`'s own error reports. A sandbox with no root drops the local entry."""
    root = _stripped(MARKETPLACE_ROOT_ENV).rstrip("/")
    out: dict[str, str] = {}
    for name, source in provisioned_defaults.items():
        if not source.startswith("/"):
            out[name] = source
            continue
        if root:
            entry = f"{root}/{Path(source).name}"
            if os.path.isdir(entry):
                out[name] = entry
        elif not is_sandbox():
            out[name] = source
    return out


# ── The virtual-node seam: the helpers and flags a relocated instance reads ──────────────────────────
# Helpers joining onto `run_root`; none is a surface of its own.

def run_path(*parts: str, env: dict | None = None) -> str:
    """`run_root()` joined with PARTS — the one way a module spells a path under the runtime tree."""
    return "/".join((run_root(env), *parts))


def daemon_user() -> str:
    """The daemon's unix account: `$JAAH_DAEMON_USER`, else `jaahd`. Empty means the default — unlike
    the session user, there is no "no daemon user" contract to express."""
    return _stripped(DAEMON_USER_ENV) or DAEMON_USER_DEFAULT


def tailscale_socket() -> str:
    """The `tailscaled` socket this node's tailnet identity belongs to: `$JAAH_TAILSCALE_SOCKET`, else
    tailscale's own default. For DISPLAY and tests; a caller that RUNS tailscale uses `tailscale_command`."""
    return _stripped(TAILSCALE_SOCKET_ENV) or TAILSCALE_SOCKET_DEFAULT


def tailscale_askable() -> bool:
    """May this daemon talk to `tailscaled` at all? Not on Windows. There, the first LocalAPI request from
    any account other than LocalSystem makes tailscaled switch to THAT account's profile when the running
    one has no owner, which is the case for any `tailscale up` run from SYSTEM (a device-management or RMM agent).
    jaahd runs as `NT SERVICE\\jaahd`, so its `tailscale status` dropped the node off the tailnet each
    minute. When an interactive admin owns the profile, the same request is refused instead, so
    there is nothing to lose by not asking."""
    return not host_platform.is_windows()


def tailscale_command(*args: str) -> list[str]:
    """The argv for `tailscale ARGS...` against THIS node's `tailscaled`. A caller asks `tailscale_askable` first.

    `--socket` only when the override is set; a vnode without it would report the HOST's identity."""
    explicit = _stripped(TAILSCALE_SOCKET_ENV)
    return ["tailscale", *(("--socket", explicit) if explicit else ()), *args]


_VNODE_TRUE = ("1", "true", "yes")
_VNODE_FALSE = ("", "0", "false", "no")


def is_vnode() -> bool:
    """Is this daemon a VIRTUAL NODE — one of several jaahd instances on a host that runs no work itself?

    `$JAAH_VNODE` in a CLOSED set (`1`/`true`/`yes` vs unset/`0`/`false`/`no`); anything else RAISES.
    A vnode's package is host-managed (no upgrade marker) and it reports `NODE_NAME` as its hostname."""
    value = _stripped(VNODE_ENV).lower()
    if value in _VNODE_TRUE:
        return True
    if value in _VNODE_FALSE:
        return False
    raise RuntimeError(f"{VNODE_ENV}={value!r} is neither of {_VNODE_TRUE} nor of {_VNODE_FALSE}")


# What a vnode MUST relocate, each paired with the host value it must not resolve to.
_VNODE_REQUIRED = (
    (RUN_ROOT_ENV, RUN_ROOT_DEFAULT),
    (NODE_ENV_FILE_ENV, NODE_ENV_FILE_DEFAULT),
    (TAILSCALE_SOCKET_ENV, TAILSCALE_SOCKET_DEFAULT),
    (POOL_ROOT_ENV, ""),
)


def require_vnode_relocated() -> None:
    """Raise when this is a vnode whose unit left a host surface in place; no-op on a regular node.

    FAIL CLOSED: the defaults are the HOST's socket, markers, tailnet identity and token file. Each must
    be set AND differ from the host's value."""
    if not is_vnode():
        return
    missing = [name for name, host in _VNODE_REQUIRED
               if (_stripped(name).rstrip("/") or host) == host]
    # A run root inside the host's is still the host's tree.
    if RUN_ROOT_ENV not in missing and (run_root() + "/").startswith(RUN_ROOT_DEFAULT + "/"):
        missing.append(RUN_ROOT_ENV)
    if missing:
        raise RuntimeError(
            f"{VNODE_ENV} is set but {', '.join(missing)} still resolve to the host's own value - a "
            f"virtual node must relocate each of them, or it would use the host's surfaces. Refusing "
            f"to start.")


def reported_hostname() -> str:
    """The name this node reports wherever it would report its kernel hostname.

    Regular node: `socket.gethostname()`. Vnode: `$NODE_NAME`, or `""` — never the host's hostname."""
    if is_vnode():
        return _stripped("NODE_NAME")
    return socket.gethostname()


# ── Constructing the sandbox posture ────────────────────────────────────────────────────────────────

def sandbox_env(root: str | os.PathLike, *, cp_base_url: str | None = None) -> dict[str, str | None]:
    """The environment that puts every surface under ONE root — the sandbox posture, as a mapping.

    A mapping, not an object, so it crosses process boundaries (fixtures, launchers, subprocesses). A
    `None` value means DELETE: `JAAH_CP_BASE_URL` unless `cp_base_url` is given; `JAAH_CP_WS_URL`
    ALWAYS (it outranks the http base, and left ambient hands a sandbox a control plane); the upgrade
    marker (disabled, not redirected); the shared account tree.

    `node.env` is pointed inside the root and NOT created: that absence is the posture table's row."""
    # Pure, so `/` is kept alive here; `enter_sandbox` refuses it.
    base = str(root).rstrip("/") or str(root)
    return {
        POOL_ROOT_ENV: f"{base}/jaah-home",
        NODE_ENV_FILE_ENV: f"{base}/node.env",
        CRED_SOCKET_ENV: f"{base}/creds.sock",
        TMUX_TMPDIR_ENV: f"{base}/tmux",
        MARKETPLACE_ROOT_ENV: f"{base}/marketplaces",
        # EMPTY, not absent: disables the uid switch (absent means the fleet default).
        SESSION_USER_ENV: "",
        CP_BASE_URL_ENV: cp_base_url.rstrip("/") if cp_base_url else None,
        CP_WS_URL_ENV: None,
        UPGRADE_MARKER_ENV: None,
        SHARED_ACCOUNTS_ROOT_ENV: None,
    }


# Probe base for inverting `sandbox_env`, so the suffixes are read off the mapping, never re-spelled.
_SANDBOX_PROBE_BASE = "/-sandbox-probe-"


def sandbox_root() -> str:
    """The ONE root this sandbox posture was built on — `sandbox_env`'s inverse — or `""`.

    `""` for every doubt: the caller is DESTRUCTIVE (the fleet doctor's `--reset-dev`), so no guess.
    Every entry must match `sandbox_env(root)` — paths, deleted vars absent, the empty session user —
    since `JAAH_HOME` alone is assertable and a "sandbox" holding the real upgrade marker is not one.
    `JAAH_CP_BASE_URL` is exempt (both absent and set are legitimate)."""
    if not is_sandbox():
        return ""
    probe = sandbox_env(_SANDBOX_PROBE_BASE)
    suffixes = {
        name: value[len(_SANDBOX_PROBE_BASE):]
        for name, value in probe.items()
        if isinstance(value, str) and value.startswith(_SANDBOX_PROBE_BASE)
    }
    pool = _stripped(POOL_ROOT_ENV).rstrip("/")
    pool_suffix = suffixes[POOL_ROOT_ENV]
    if not pool.endswith(pool_suffix):
        return ""
    root = pool[: -len(pool_suffix)]
    if not root.startswith("/") or root.rstrip("/") in ("", "/"):
        return ""
    for name, value in probe.items():
        if name == CP_BASE_URL_ENV:
            continue
        if name in suffixes:
            if env_get(name) != f"{root}{suffixes[name]}":
                return ""
        elif value is None:
            if os.environ.get(name):
                return ""
        elif env_get(name) != value:
            return ""
    return root


# 0700: the units' `UMask=0002` + `Group=jaah` would make a default mkdir runner-writable, and a
# socket's parent mode is its access control (`cred_broker.start()`).
_SANDBOX_DIR_MODE = 0o700


def _sandbox_root(root: str | os.PathLike) -> str:
    """Validate ROOT and return the ONE normalised absolute path the whole sandbox tree hangs off.

    Normalised once with `realpath`, so a later symlink swap cannot relocate the created tree. A
    relative root (cwd-dependent) and `/` (chmod of the filesystem root) are refused."""
    raw = str(root)
    resolved = os.path.realpath(raw) if os.path.isabs(raw) else raw
    if not os.path.isabs(raw) or resolved.rstrip("/") in ("", "/"):
        raise ValueError(
            f"--sandbox {raw!r} is not usable as a sandbox root: it must be an absolute path BELOW `/` "
            f"(it resolves to {resolved!r}). A relative root would land wherever the caller's cwd happens "
            f"to point, and `/` would have this process take ownership of the filesystem root and chmod "
            f"it 0700 while reporting that it was contained. Pass a private directory - `--sandbox "
            f"\"$(mktemp -d)\"` is the shortest correct spelling.")
    return resolved


# `exclusive_dir_problem`'s verdicts.
DIR_NOT_A_DIRECTORY = "not_a_directory"
DIR_FOREIGN_OWNER = "foreign_owner"
DIR_GROUP_OR_WORLD_WRITABLE = "group_or_world_writable"


def exclusive_dir_problem(info: os.stat_result, euid: int) -> str:
    """Why a directory is NOT exclusively this process's, or `""` when it is. A PURE predicate over an
    already-taken `lstat`, so the caller keeps the `stat_result` its own message needs.

    Two callers: `_own_sandbox_root` ADOPTS (whoever can write a root can plant `node.env` and decide
    the posture) and the fleet doctor's `--reset-dev` CLEARS (a writable root is a delete target
    somebody else chose).

    `lstat`, never `stat`: a symlink must fail the directory test rather than be resolved through."""
    if not stat.S_ISDIR(info.st_mode):
        return DIR_NOT_A_DIRECTORY
    if info.st_uid != euid:
        return DIR_FOREIGN_OWNER
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return DIR_GROUP_OR_WORLD_WRITABLE
    return ""


def unsafe_ancestor(resolved: str, euid: int) -> str:
    """The first ancestor of RESOLVED that a second uid could rename it out from under, or `""`.

    Whoever can write a PARENT can `rename()` the directory away and plant their own, so the directory
    check alone is not enough (the fleet doctor's `--reset-dev`). Uid 0 is accepted as an owner, and
    a sticky-bit directory (`/tmp`) lets only an entry's owner rename it."""
    for parent in [Path(resolved), *Path(resolved).parents]:
        try:
            info = os.lstat(parent)
        except OSError as exc:
            return f"{parent} ({exc.strerror})"
        if info.st_uid not in (0, euid):
            return f"{parent} (owned by uid {info.st_uid})"
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not info.st_mode & stat.S_ISVTX:
            return f"{parent} (mode {stat.S_IMODE(info.st_mode):#o}, writable beyond its owner)"
    return ""


def _own_sandbox_root(resolved: str) -> None:
    """Create ROOT itself 0700, or ADOPT an existing one only when this process exclusively owns it.

    Whoever can write ROOT decides the posture (a planted `node.env` makes a `--sandbox` run provisioned,
    reaching a live CP, the credential tree and the upgrade channel). So: `exist_ok=False`, else adopt
    only a real directory owned by this euid and not group/world-writable. The `chmod` is unconditional:
    it tightens an adopted root and removes umask dependence. Missing parents take the default mode."""
    try:
        Path(resolved).mkdir(mode=_SANDBOX_DIR_MODE, parents=True, exist_ok=False)
    except FileExistsError as exc:
        info = os.lstat(resolved)
        problem = exclusive_dir_problem(info, os.geteuid())
        if problem == DIR_NOT_A_DIRECTORY:
            raise RuntimeError(
                f"the sandbox root {resolved} exists and is not a directory (mode {info.st_mode:#o}) - "
                f"refusing to build a sandbox tree under it") from exc
        if problem == DIR_FOREIGN_OWNER:
            raise RuntimeError(
                f"the sandbox root {resolved} already exists and is owned by uid {info.st_uid}, not by "
                f"this process (euid {os.geteuid()}). Whoever can write this directory decides this "
                f"process's POSTURE - a `node.env` planted there makes the posture table answer "
                f"`provisioned`, and every surface then resolves to the real node's: a live control "
                f"plane, the fleet credential tree, and the root path unit's upgrade channel. Pass a "
                f"root of your own.") from exc
        if problem == DIR_GROUP_OR_WORLD_WRITABLE:
            raise RuntimeError(
                f"the sandbox root {resolved} already exists and is group/world-writable (mode "
                f"{stat.S_IMODE(info.st_mode):#o}). Every runner on this node is in group `jaah`, so "
                f"that is an invitation to plant `{Path(resolved).name}/node.env` and turn this "
                f"`--sandbox` run into a provisioned one. Pass a private directory - `--sandbox "
                f"\"$(mktemp -d)\"` is the shortest correct spelling.") from exc
    os.chmod(resolved, _SANDBOX_DIR_MODE)


def enter_sandbox(root: str | os.PathLike, *, cp_base_url: str | None = None) -> dict[str, str | None]:
    """Apply `sandbox_env` to `os.environ`, CREATE the directories it names, and return what was applied.

    For a process configuring ITSELF; tests use `sandbox_env` with monkeypatch. Creates the directories
    a unit's `ExecStartPre` would (else the broker's `bind()` fails `ENOENT`), but NOT `node.env`. Drops
    the ambient node identity (`NODE_IDENTITY_ENV`). Validates and owns the root first (`_sandbox_root`,
    `_own_sandbox_root`), and finally ASSERTS the resulting posture is sandbox, refusing to start
    otherwise."""
    global _entered_sandbox_deliberately
    resolved = _sandbox_root(root)
    _own_sandbox_root(resolved)
    # Set only once both refusals above have passed: a rejected root is not an entered sandbox.
    _entered_sandbox_deliberately = True
    applied = sandbox_env(resolved, cp_base_url=cp_base_url)
    for name, value in applied.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    for name in NODE_IDENTITY_ENV:
        os.environ.pop(name, None)
    # 0700 like the root (`_SANDBOX_DIR_MODE`).
    for path in (Path(applied[POOL_ROOT_ENV]), Path(applied[TMUX_TMPDIR_ENV]),
                 Path(applied[MARKETPLACE_ROOT_ENV]), Path(applied[CRED_SOCKET_ENV]).parent):
        path.mkdir(mode=_SANDBOX_DIR_MODE, parents=True, exist_ok=True)
    # `classify()`, not `is_sandbox()`: `posture()` would consume its announce-once log before logging
    # is configured.
    if _VERDICT_POSTURES[classify()] != POSTURE_SANDBOX:
        raise RuntimeError(
            f"--sandbox {resolved} did not produce sandbox posture: classify() says "
            f"{classify()!r} with the node identity file resolving to {node_env_path()}. A `node.env` "
            f"present there means the posture table reads this process as a real fleet node, so every "
            f"surface is the node's own - a live control plane, the fleet credential tree, the root path "
            f"unit's upgrade channel - and nothing here is contained. Refusing to start.")
    return applied
