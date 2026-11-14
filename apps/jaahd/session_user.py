"""Who fleet sessions run as, and how this process reaches them — the single home for the uid split.

The daemon runs as `jaahd`; runner and console sessions as `jaah`. The switch is a tmux
SERVER owned by `jaah` (`jaah-runner@.service`): a pane is forked by the server, so every window runs
as `jaah` while the daemon is a plain client. Every call names the socket with `-S` — only `-S` binds;
`TMUX_TMPDIR` is ignored inside a session. The daemon's own socket must stay 0700: group `jaah` on it
would hand a runner a pane as `jaahd`. During an in-place upgrade's drain, read/manage ops also cover
the daemon's own server (`manage_sockets`); creation targets only the fleet socket.

Socket mode is not tmux's whole access control: tmux ≥3.3 keeps a per-server ACL whose refusal prints
`access not allowed` and EXITS 0 (root is exempt, so a root-shell probe proves nothing). Admitting the
daemon needs both a socket it may open and a `server-access -aw` grant. On Linux the socket is the
daemon's own (`isolated_socket`), so runner panes cannot reach the server at all; the shared `tmux-<uid>`
socket (`share_own_socket`, `fleet_tmux_server.seed`) is the fallback where tmux cannot adopt it.

Windows: the session user is the local account `jaah`, the daemon `NT SERVICE\\jaahd`. Same
contract, other primitives — identity by SID (`windows.is_runner_account`), the fleet socket named after the
session user (`<TMUX_TMPDIR>\\jaah\\default`), no chmod (the installer's ACLs inherit), no upgrade-drain
second server, and the spool made by `prepare_windows_spool` instead of a unit.
"""
from __future__ import annotations

import logging
import ntpath
import os
import re
import secrets
import shutil
import socket as socketlib
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

try:
    import pwd
except ImportError:  # Windows: there is no passwd database (the Windows branches never reach it)
    pwd = None  # type: ignore[assignment]

import env_helpers  # the tmux-safe env every client command runs under (`tmux_run`)
import environment
import host_platform
import tmux_jail
from host_platform import windows  # importable everywhere: no native call at import


def _on_windows() -> bool:
    """The one OS test here — `host_platform.is_windows`, read per call so the suite can fake it."""
    return host_platform.is_windows()


def _o_binary() -> int:
    """`os.O_BINARY` where it exists (Windows), else 0 — OR'd into EVERY `os.open` here. A raw descriptor
    on Windows defaults to text mode, so `os.fdopen(fd, "w"/"wb")` would translate `\n` to `\r\n` and
    corrupt scripts and binary payloads alike. Read per call so the suite can plant the bit."""
    return getattr(os, "O_BINARY", 0)


log = logging.getLogger("jaahd.session_user")

# An EMPTY override disables the switch (dev hosts, the suite: one uid). Not a config key: the two
# system users are created by `postinst`, so this is a property of the package.
SESSION_USER_ENV = environment.SESSION_USER_ENV

# The `run_as_session_user` spool, made by the unit's `ExecStartPre=+install -d` (root):
#   req/  2750 jaahd:jaah   the daemon writes the script, `jaah` may only READ it
#   res/  2770 jaahd:jaah   `jaah` writes the result back
# So a runner cannot swap a script the daemon is about to run; results are forgeable by an equal uid.
# `EXEC_SPOOL` is the suite's rebind seam; `exec_spool()` is the per-call answer.
_EXEC_SPOOL_DEFAULT = f"{environment.RUN_ROOT_DEFAULT}/exec"
EXEC_SPOOL = _EXEC_SPOOL_DEFAULT


def exec_spool() -> str:
    """The spool directory: a rebound `EXEC_SPOOL` (tests), else `<run root>/exec`."""
    if EXEC_SPOOL is not _EXEC_SPOOL_DEFAULT:
        return EXEC_SPOOL
    return environment.run_path("exec")


def session_user() -> str:
    """The configured session user, or "" when the switch is disabled.

    ABSENT means the fleet default applies; EMPTY is the explicit "no second uid on this host"."""
    return environment.session_user()


# Windows has no uids: Python's `os.stat` reports st_uid/st_gid 0 for every file, so 0 is the one value
# every st_uid/st_gid comparison a caller makes (runner_clis, secret_modes) sees for the
# runner's files. NOT None: callers read None as "no session user" (ctl.py's socket gate, open_term's
# attach hint, reconcile's delegated fetch), which would be a lie here. Identity
# on Windows is the SID (`windows.runner_sid`); `clone_identity.delegated_op` must not compare this value
# with `host_platform.owner_uid` there.
WINDOWS_SESSION_ID = 0


def _session_pw_field(field: str) -> int | None:
    """The session user's passwd `field` (`pw_uid`/`pw_gid`), or None when the switch is disabled or the
    user does not exist here. `WINDOWS_SESSION_ID` on Windows whenever a session user is configured."""
    name = session_user()
    if not name:
        return None
    if _on_windows():
        return WINDOWS_SESSION_ID
    try:
        return getattr(pwd.getpwnam(name), field)
    except KeyError:
        return None


def session_uid() -> int | None:
    """The session user's uid, or None when the switch is disabled or the user does not exist here.
    `WINDOWS_SESSION_ID` on Windows whenever a session user is configured."""
    return _session_pw_field("pw_uid")


def session_gid() -> int | None:
    """The session user's primary gid, or None (same conditions as `session_uid`).

    The one gid both users share (`$JAAH_HOME` is owned by it, setgid). A caller handing a file to a
    session-user process states this group explicitly: a wrong group means the process cannot open it."""
    return _session_pw_field("pw_gid")


def session_home() -> str:
    """The session user's home from passwd, or "" when it does not exist. Not the daemon's own HOME
    (`/var/lib/jaahd`): git credentials, `gh` and the Claude config live under `jaah`'s. On Windows
    the layout's `home_root` (the installer makes it `jaah`'s home folder)."""
    name = session_user()
    if not name:
        return ""
    if _on_windows():
        return host_platform.paths().home_root
    try:
        return pwd.getpwnam(name).pw_dir
    except KeyError:
        return ""


def default_claude_config_dir() -> str:
    """The session user's own `~/.claude` — where a Claude Code runner launched with NO `CLAUDE_CONFIG_DIR`
    (an API-key account: `agents/claude.py::env` omits it) registers its session. `""` with no session user;
    the daemon's own `$HOME/.claude` then already is that dir (`reconcile.accounts_probe._agent_config_dirs`)."""
    home = session_home()
    return f"{home}/.claude" if home else ""


def switch_needed() -> bool:
    """True when this process is NOT the session user and one exists to switch to (on Windows: by SID)."""
    if _on_windows():
        return bool(session_user()) and not windows.is_runner_account()
    uid = session_uid()
    return uid is not None and uid != os.geteuid()


def is_session_user() -> bool:
    """True when this process already IS the session user (the unit's own launcher; an operator shell)."""
    if _on_windows():
        return bool(session_user()) and windows.is_runner_account()
    return session_uid() == os.geteuid()


def tmux_tmpdir(env: dict | None = None) -> str:
    """The tmux socket directory this node uses.

    A caller composing a child's environment passes that child's `env`."""
    return environment.tmux_tmpdir(env)


def socket_dir_for(uid: int, env: dict | None = None) -> str:
    """`$TMUX_TMPDIR/tmux-<uid>` — tmux's own level, which is why widening the parent alone shares
    nothing."""
    return os.path.join(tmux_tmpdir(env), f"tmux-{uid}")


def daemon_user() -> str:
    """The daemon's unix account (`JAAH_DAEMON_USER`, default `jaahd`) — by name, because
    `server-access` takes users, not uids."""
    return environment.daemon_user()


# ── The isolated fleet socket (Linux) ──────────────────────────────────────────────────────────────────
# A runner pane is the server's own uid, and tmux lets its owner do anything: on a socket the session user
# can reach, one runner could `send-keys` into another's pane, `set-hook` the server, or `pipe-pane` its
# output. So on Linux systemd binds the fleet socket (`jaah-fleet-tmux.socket`, `jaah-runner-vn@.socket`): owned
# by the DAEMON user, 0600, in a root-owned directory. The fleet server adopts it (socket activation,
# `fleet_tmux_server.converge_activated`) and admits the daemon from its root-owned config; no `jaah`
# process can connect, unlink it or bind a replacement. Taken only when this node's tmux was built with
# systemd support — a socket nothing accepts on would hang every client. Otherwise the `tmux-<uid>` socket
# below stays the fleet socket (shared, the pre-isolation contract).
#
# RESIDUAL — what one uid for every pane still allows, closed only by a uid per runner: a runner can signal the
# server or another runner's processes (kill them all; SIGUSR1 has the server drop the socket, which the loop
# answers by ending it), read another's `/proc/<pid>/environ`, and open another pane's `/dev/pts/N` (write to
# its screen, or race its program for typed input). Isolation needs Yama `ptrace_scope` >= 1
# (`isolation_available`): below it a runner may ptrace the server itself.
ISOLATED_SOCKET_PARTS = ("fleet", "default")

# Linked into a tmux that can adopt a systemd-passed socket (`--enable-systemd`); absent from one that cannot.
_ACTIVATION_SYMBOL = b"sd_listen_fds"
_activation_support: dict[str, bool] = {}


def tmux_supports_activation() -> bool:
    """True when the `tmux` on PATH can adopt a socket systemd bound (socket activation). Cached per
    binary; False when there is no tmux or it cannot be read."""
    tmux = shutil.which("tmux")
    if not tmux:
        return False
    tmux = os.path.realpath(tmux)
    if tmux not in _activation_support:
        try:
            with open(tmux, "rb") as handle:
                _activation_support[tmux] = _ACTIVATION_SYMBOL in handle.read()
        except OSError:
            _activation_support[tmux] = False
    return _activation_support[tmux]


# Yama's ptrace scope: at 0 any process may ptrace any other of its uid — a runner could drive the server
# directly, so the socket would isolate nothing (and the unit's root first client would trust a server a runner
# can steer). Isolation is taken only at 1 or above; no Yama at all reads as 0.


def isolation_available() -> bool:
    """True when this host can isolate the fleet server: Linux, a tmux that adopts a systemd-bound socket, and
    Yama's ptrace scope at 1 or above. The loop and the daemon both decide by this, so they never disagree."""
    if _on_windows() or host_platform.is_macos():
        return False
    return host_platform.ptrace_scope() >= 1 and tmux_supports_activation()


def isolated_socket(env: dict | None = None) -> str:
    """Where systemd binds the isolated fleet socket: `<run root>/fleet/default` (the unit's `ListenStream=`)."""
    return environment.run_path(*ISOLATED_SOCKET_PARTS, env=env)


def fleet_isolated(env: dict | None = None) -> bool:
    """True when the fleet server is on the isolated socket: a session user, `isolation_available`, and the
    socket systemd bound is there. Its directory is root's, so no `jaah` process can fake it."""
    if _on_windows() or session_uid() is None:
        return False
    try:
        bound = stat.S_ISSOCK(os.stat(isolated_socket(env)).st_mode)
    except OSError:
        return False
    return bound and isolation_available()


def legacy_fleet_socket(env: dict | None = None) -> str:
    """The session user's own `tmux-<uid>/default` — the fleet socket before isolation, and still it where
    isolation is unavailable. "" with no session user."""
    uid = session_uid()
    if uid is None:
        return ""
    return os.path.join(socket_dir_for(uid, env), "default")


def fleet_socket(env: dict | None = None) -> str:
    """The socket of the tmux server that owns fleet sessions — the SESSION USER's, not this process's:
    `isolated_socket` when `fleet_isolated`, else `legacy_fleet_socket`.

    "" when there is no session user: callers keep tmux's default-label resolution, since the daemon's
    own server is then the fleet server. On Windows `<TMUX_TMPDIR>\\<session user>\\default`: there is no
    uid to name tmux's `tmux-<uid>` level after, so the directory is the account's name."""
    if _on_windows():
        name = session_user()
        return ntpath.join(tmux_tmpdir(env), name, "default") if name else ""
    if fleet_isolated(env):
        return isolated_socket(env)
    return legacy_fleet_socket(env)


def fleet_socket_ready(env: dict | None = None) -> bool:
    """Whether the fleet server's socket exists. A caller creating a window must refuse without it: a
    client with no server starts one itself, as the caller's uid — and reports success."""
    socket = fleet_socket(env)
    return bool(socket) and os.path.exists(socket)


def tmux_attach_cmd(socket: str, *args: str) -> list[str]:
    """`["tmux", "-u", "-S", socket, *args]`, or `["tmux", "-u", *args]` when SOCKET is "" (no session user,
    so tmux's own default label is correct). The one place the `-S` splice is spelled. For an ATTACH client
    (or a command line another user runs); the daemon's own command clients use `tmux_cmd`.

    Always `-S`, never a label plus `TMUX_TMPDIR`: tmux consults `TMUX_TMPDIR` only with no `-S` and no
    `$TMUX`, so inside a session a command lands on the caller's own server. The default label also
    resolves per-euid, so two uids would silently use two servers.

    `-u` on every platform: a daemon started with no UTF-8 locale (a Windows service, a systemd unit on a
    minimal image, the local dev-node container) is a non-UTF-8 tmux client, and tmux rewrites every TAB in
    `-F` output to `_` — each tab-split pane listing parsed empty, a kill found no window to tear down (a real
    PC) and the console never attached to a live window."""
    return ["tmux", "-u", "-S", socket, *args] if socket else ["tmux", "-u", *args]


TMUX_JAIL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmux_jail.py")


def _private_socket(socket: str) -> bool:
    """Whether SOCKET's server is the daemon's own beyond a runner's reach: its directory is this uid's and
    closed to others (0700), and no runner can rename any directory on the way to it — every ancestor is
    root's or this uid's, no link, and not group/other-writable unless sticky. A console layout's grid
    (`mkdtemp` under the sticky `/tmp`) passes; the pre-switch `tmux-<uid>` directory does not, since its
    parent is the session user's group-writable run directory, where a runner could swap in its own. A link on
    the way (macOS's `/tmp` → `/private/tmp`) is followed only when `_root_links_only` vouches for it."""
    directory = os.path.dirname(socket)
    if not os.path.isabs(directory):
        return False
    real = os.path.realpath(directory)
    if real != directory and not _root_links_only(directory):
        return False
    directory = real
    try:
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return False
        while directory != os.path.dirname(directory):
            directory = os.path.dirname(directory)
            info = os.lstat(directory)
            if info.st_uid not in (0, os.geteuid()):
                return False
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                return False
    except OSError:
        return False
    return True


def _root_links_only(path: str) -> bool:
    """Whether PATH, as spelled, is one no runner can redirect: normalised (no `..`), every directory on the way is
    root's or this uid's and closed to writes by others unless sticky, and every symbolic link on the way is root's
    (macOS's `/tmp`, `/var`) — so a link it follows cannot be swapped between this check and tmux's connect."""
    if os.path.normpath(path) != path:
        return False
    current = "/"
    try:
        for part in path.strip("/").split("/"):
            above = os.lstat(current)
            if above.st_uid not in (0, os.geteuid()) or (above.st_mode & 0o022 and not above.st_mode & stat.S_ISVTX):
                return False
            current = os.path.join(current, part)
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                if info.st_uid != 0:
                    return False
                current = os.path.realpath(current)
    except OSError:
        return False
    return True


def _runner_reachable(socket: str) -> bool:
    """Whether this process's command client on SOCKET talks to a server a runner pane can reach: this process is
    not the session user, and SOCKET is neither the isolated one nor a `_private_socket` (on Windows every fleet
    socket is reachable). Such a server can make a command client read or write files, or exec a command, as
    THIS uid."""
    if not socket or not switch_needed():
        return False
    if _on_windows():
        return True
    return socket != isolated_socket() and not _private_socket(socket)


def _jail_needed(socket: str) -> bool:
    """Whether this process's command client on SOCKET must be jailed (`tmux_jail`): `_runner_reachable`, on a host
    where the jail runs (Linux with Landlock, macOS, Windows). A jailed client cannot START a server (no bind), so the
    exemptions are also what lets the daemon start its own. Elsewhere the client runs unjailed: `jail_gap`."""
    return tmux_jail.available() and _runner_reachable(socket)


def jail_gap(env: dict | None = None) -> str:
    """"" when every command client this daemon runs on a runner-reachable server is jailed, else why not and on
    which sockets — the detail of the node's `tmux_unjailed` Health finding (`tmux_jail_health`)."""
    if tmux_jail.available():
        return ""
    exposed = [socket for socket in manage_sockets(env) if _runner_reachable(socket)]
    if not exposed:
        return ""
    return f"{tmux_jail.unavailable_reason()}; command clients on {', '.join(exposed)} run unjailed"


def tmux_cmd(socket: str, *args: str) -> list[str]:
    """The daemon's tmux COMMAND client on SOCKET: `tmux_attach_cmd`, behind `tmux_jail` when `_jail_needed`
    (`python3 -I -S tmux_jail.py tmux -u -S <socket> …`). Never for `attach`: a jailed attach client
    cannot read terminfo."""
    argv = tmux_attach_cmd(socket, *args)
    # No tmux: left bare, so the caller still sees `FileNotFoundError` rather than the jail's rc 127.
    if shutil.which("tmux") and _jail_needed(socket):
        # Windows: the client runs as the runner account, logged on from the installer's file.
        logon = ["--logon", tmux_jail.windows_logon_file()] if _on_windows() else []
        return [sys.executable, "-I", "-S", TMUX_JAIL, *logon, *argv]
    return argv


def tmux_run(socket: str, *args: str, timeout: float = 10, env: dict | None = None,
             run: Callable | None = None, **kwargs) -> subprocess.CompletedProcess | None:
    """`tmux_cmd(socket, *args)` run to completion: both streams captured as text, TIMEOUT, and ENV
    (`env_helpers._tmux_safe_env()` unless given), through RUN (`subprocess.run`, or a test's fake). None when the
    client could not run (OSError, `TimeoutExpired`, any `SubprocessError`), logged — what a non-zero exit means
    stays the caller's decision. KWARGS reach RUN as given (`input=`, `text=False`)."""
    if env is None:
        env = env_helpers._tmux_safe_env()
    kwargs.setdefault("text", True)
    try:
        return (run or subprocess.run)(tmux_cmd(socket, *args), capture_output=True, timeout=timeout, env=env,
                                       **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("tmux %s on %r could not run: %s", args[0] if args else "", socket, exc)
        return None


def tmux_out(socket: str, *args: str, **kwargs) -> str | None:
    """`tmux_run`'s stripped stdout on exit 0; None when it did not run or exited non-zero."""
    proc = tmux_run(socket, *args, **kwargs)
    if proc is None or proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def manage_sockets(env: dict | None = None) -> list[str]:
    """Sockets a read/manage op must cover: the fleet socket, then the pre-isolation shared one and this
    process's own when they exist and differ (the upgrade drains). `[""]` when there is no session user, meaning
    "tmux's default". Only creation is single-socket, or an upgrade would strand earlier runners as invisible
    orphans."""
    fleet = fleet_socket(env)
    if not fleet:
        # This process's own server IS the fleet server; a second spelling would be a second name.
        return [""]
    if _on_windows():
        # No pre-switch daemon-owned server to drain: Windows nodes started on the split.
        return [fleet]
    sockets = [fleet]
    legacy = legacy_fleet_socket(env)
    if legacy != fleet and _drained_legacy_socket(legacy, fleet) and _server_behind(legacy):
        sockets.append(legacy)
    own = os.path.join(socket_dir_for(os.geteuid(), env), "default")
    if own not in sockets and _own_socket_trusted(own) and _server_behind(own):
        sockets.append(own)
    return sockets


def _server_behind(path: str) -> bool:
    """Whether a server still answers on the drain socket at PATH. A drained server that died (retired, killed,
    a reboot of its unit) can leave its socket file behind; every tmux client on it then fails, and one failed
    socket makes the whole pane sensor unknown (`reconcile.context._tmux_once`), so no answer reaches any runner
    on the node. A dead server holds no runner, so dropping its socket hides nothing.

    A bare connect, closed at once: no tmux protocol is spoken, so even an untrusted server cannot act on this
    client. Only a refused connection or a vanished file reads as dead; anything else (a permission error, a
    timeout) keeps the socket, so the tmux client's own failure still reads unknown, fail-closed."""
    probe = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True
    finally:
        probe.close()
    return True


def _drained_legacy_socket(path: str, isolated: str) -> bool:
    """Whether the pre-isolation shared socket at PATH is still the one to drain. After isolation arrives,
    the shared server keeps the runners it already holds until they end (`fleet_tmux_server` retires it), and
    the daemon manages them meanwhile. But that path is the session user's: any runner may bind a server of
    its own there, and a server a runner configures can make a connecting client read or write files as
    the daemon. So it is trusted only while it PREDATES the isolated socket (ISOLATED): a status-change time
    no process can set back (a rename, link, chmod or utimensat all set it to now), and stateless, so a daemon
    restart trusts nothing new. The reference holds still because ISOLATED is not the session user's: tmux's
    own `chmod` of it (on every attach) is refused, and only root or the daemon could touch it — so one that IS
    the session user's is no reference at all. A shared socket rebound after the switch is never trusted (a
    client attaching to the real one moves its time too, ending the drain early — safe)."""
    try:
        found, switched = os.lstat(path), os.lstat(isolated)
    except OSError:
        return False
    if switched.st_uid == session_uid():
        return False
    return stat.S_ISSOCK(found.st_mode) and found.st_ctime_ns <= switched.st_ctime_ns


def _own_socket_trusted(path: str) -> bool:
    """Whether this process's own `tmux-<euid>/default` exists and is ITS: the socket and its directory owned by
    this uid and the directory closed to others — the parent `TMUX_TMPDIR` is group-writable by the session
    user, who could otherwise put a server of its own in the daemon's place."""
    try:
        sock, directory = os.lstat(path), os.lstat(os.path.dirname(path))
    except OSError:
        return False
    me = os.geteuid()
    return (stat.S_ISSOCK(sock.st_mode) and stat.S_ISDIR(directory.st_mode)
            and sock.st_uid == me and directory.st_uid == me and not directory.st_mode & 0o077)


def share_own_socket(env: dict | None = None) -> str | None:
    """Widen THIS process's tmux socket so group `jaah` can use it. Returns the socket path, or None.

    Legal only for a server this process owns as the session identity — the `jaah` fleet server. tmux
    creates both levels 0700 regardless of umask (tmux 3.4):

      $TMUX_TMPDIR/tmux-<euid>/          0700 → 0750   or the group cannot traverse to the socket
      $TMUX_TMPDIR/tmux-<euid>/default   0700 → 0660

    The directory is re-moded only when wrong. Refuses for any other process — a widened daemon socket hands
    `jaah` a pane as `jaahd`. Never raises; a failure is logged at warning.

    Windows: no chmod — the socket's directory inherits the installer's ACL (daemon Modify), and tmux's
    `server-access` grant is the gate. Returns the fleet socket for the session user, else None."""
    if _on_windows():
        if not is_session_user():
            log.warning("share_own_socket: refusing — this process is not the session user %r",
                        session_user())
            return None
        return fleet_socket(env) or None
    if not is_session_user():
        log.warning("share_own_socket: refusing to widen a non-session-user socket (euid=%s, session "
                    "user=%r) — a group-usable daemon socket is an escalation back to the daemon's uid",
                    os.geteuid(), session_user())
        return None
    socket_dir = socket_dir_for(os.geteuid(), env)
    socket_path = os.path.join(socket_dir, "default")
    try:
        if stat.S_IMODE(os.stat(socket_dir).st_mode) & 0o777 != 0o750:
            os.chmod(socket_dir, 0o750)
        os.chmod(socket_path, 0o660)
    except OSError as exc:
        log.warning("share_own_socket: could not widen %s: %s", socket_path, exc)
        return None
    return socket_path


def restrict_own_socket(env: dict | None = None) -> str | None:
    """Converge THIS process's tmux socket back to `0700`. Live, not one-off: the daemon's server
    survives an in-place upgrade (`KillMode=process`) with bits an older build widened.

    A no-op for the session user, whose socket is meant to be shared. Never raises (idle tick). A no-op on
    Windows: no chmod there, and the daemon owns no tmux server to restrict."""
    if _on_windows():
        return None
    if is_session_user() or session_uid() is None:
        return None
    socket_dir = socket_dir_for(os.geteuid(), env)
    socket_path = os.path.join(socket_dir, "default")
    if not os.path.exists(socket_path):
        return None
    try:
        os.chmod(socket_dir, 0o700)
        os.chmod(socket_path, 0o700)
    except OSError as exc:
        log.debug("restrict_own_socket: could not restrict %s: %s", socket_path, exc)
        return None
    return socket_path


def tmux_refusal(proc: subprocess.CompletedProcess) -> str:
    """Why a tmux client call was REFUSED, or "" — the check `returncode` cannot make.

    tmux ≥3.3 refuses a uid missing from `server-access` with `access not allowed` and exit 0, so it is
    otherwise indistinguishable from success. `Permission denied` on the socket is named too."""
    err = (proc.stderr or "").strip()
    lowered = err.lower()
    if "access not allowed" in lowered or "permission denied" in lowered:
        return err
    return ""


# ── Running one command as the session user ──────────────────────────────────────────────────────────
#
# Some daemon shell-outs must not run as `jaahd`:
#   * the `claude agents --json` probe and account CLI calls: the CLI rewrites `.credentials.json` 0600
#     owned by whoever refreshed, so one wrong-uid refresh locks the other user out of the account.
#   * `./dc` / `docker compose` (`stack_ops.py`). The access model: the engine is `jaah`'s own rootless
#     dockerd (provisioned by the node reprovision's `ensure_docker_access`), where container
#     root is uid `jaah`. `jaahd` has no docker access and must gain none — the rootful `docker`
#     group is root-equivalent. Delegating is the answer, not a grant.
# The mechanism is `tmux run-shell` on the `jaah`-owned server: no new privilege. The script lives where
# `jaah` cannot write (EXEC_SPOOL); results are data from an equal uid, and every caller fails closed on
# a probe it cannot trust.

_POLL_INTERVAL_S = 0.025
_DEFAULT_TIMEOUT_S = 120.0

# ── Repeated timeouts: memory or I/O pressure, raised to the Health tab ──────────────────────────────
# On a starved Multi-Node Host vnode every command run here (a file write, a probe, a git step) times out at
# its deadline (rc 124) for many minutes; one timeout is a log line, a run of them is a sick machine. Once
# `STALL_RUN` timeouts land with no `STALL_RUN` commands in a row finishing in time between them (one cheap command
# getting through must not hide the rest), the daemon logs one warning carrying `STALL_MARK`, again every
# `STALL_REWARN_S` while it lasts; the shipped journal reaches the control plane, whose classifier reads it as the
# `session_user_stalled` Health finding (`apps/api/app/node_log_findings.py` holds `STALL_MARK`'s twin).
STALL_MARK = "session-user commands keep timing out: "
STALL_RUN = 3
STALL_REWARN_S = 300.0
_stall_lock = threading.Lock()
_stall_count = 0
_stall_ok_streak = 0
_stall_warned_at: float | None = None


def note_session_user_outcome(argv: list[str], *, timed_out: bool) -> None:
    """Count one command's outcome toward the stall warning (`STALL_MARK`). Thread-safe: callers run on
    worker threads."""
    global _stall_count, _stall_ok_streak, _stall_warned_at
    with _stall_lock:
        if not timed_out:
            _stall_ok_streak += 1
            if _stall_ok_streak < STALL_RUN:
                return
            if _stall_warned_at is not None:
                log.info("session-user commands finish in time again, after %d timeouts", _stall_count)
            _stall_count, _stall_warned_at = 0, None
            return
        _stall_ok_streak = 0
        _stall_count += 1
        now = time.monotonic()
        if _stall_count < STALL_RUN or (_stall_warned_at is not None and now - _stall_warned_at < STALL_REWARN_S):
            return
        _stall_warned_at = now
        count = _stall_count
    log.warning(
        "%s%d timed out, the last %s - the machine is short of memory or disk I/O, or a command hangs (on a "
        "Multi-Node Host vnode, check jaahvnodes.slice)", STALL_MARK, count, os.path.basename(argv[0]) if argv else "?")


def _spool_paths(request_id: str) -> tuple[str, str, str, str]:
    req = os.path.join(exec_spool(), "req", f"{request_id}.sh")
    res_dir = os.path.join(exec_spool(), "res")
    return req, os.path.join(res_dir, f"{request_id}.out"), os.path.join(res_dir, f"{request_id}.err"), \
        os.path.join(res_dir, f"{request_id}.rc")


def spool_ready() -> bool:
    """Whether the request/result spool exists. False on a dev host: run the command directly.

    On Windows the spool, `req/` and `res/` must also be OWNED by the daemon: `\\run` is runner-Modify,
    so a runner could pre-create them with lists of its own choosing. Not daemon-owned (or the owner
    unreadable) is False — delegation then refuses rather than trusting a runner-made spool."""
    spool = exec_spool()
    dirs = (spool, os.path.join(spool, "req"), os.path.join(spool, "res"))
    if not all(os.path.isdir(d) for d in dirs[1:]):
        return False
    if not _on_windows():
        return True
    try:
        daemon = windows.daemon_sid()
        return all(os.path.isdir(d) and windows.path_owner_sid(d) == daemon for d in dirs)
    except (OSError, ValueError):
        return False


# BUILTIN\Administrators — a well-known SID, the same on every Windows (the installer's `$SidAdmins`).
_ADMINISTRATORS_SID = "S-1-5-32-544"


def _run_icacls(argv: list[str]) -> subprocess.CompletedProcess:
    """The one native call `prepare_windows_spool` makes; module-level so the suite can fake it."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def windows_spool_acls(daemon_sid: str, runner_sid: str) -> list[tuple[str, list[str]]]:
    """`(directory, icacls argv)` calls, in order, for the spool's three levels: a PROTECTED list (inheritance cut), SIDs
    only (a localized Windows reads names differently). SYSTEM and Administrators full; the daemon Modify,
    inherited by what it creates. The runner: traverse on the root, READ on `req/` — it must be unable to
    plant an ack or swap a script the daemon is about to run (the Linux `2750`) — and on `res/`, where it
    writes results back (the Linux `2770`), Modify on its CHILDREN only (`(IO)`): on the folder itself just
    read + create files/folders, never DELETE, so it cannot replace `res` with a junction."""
    spool = exec_spool()
    full = [f"*{windows.SYSTEM_SID}:(OI)(CI)F", f"*{_ADMINISTRATORS_SID}:(OI)(CI)F"]
    levels = (
        (spool, [f"*{daemon_sid}:(OI)(CI)M", f"*{runner_sid}:(RX)"]),
        (os.path.join(spool, "req"), [f"*{daemon_sid}:(OI)(CI)M", f"*{runner_sid}:(OI)(CI)RX"]),
        (os.path.join(spool, "res"), [f"*{daemon_sid}:(OI)(CI)M", f"*{runner_sid}:(RX,WD,AD)"]),
    )
    acls = [(path, ["icacls", path, "/inheritance:r", "/grant:r", *full, *grants, "/q"])
            for path, grants in levels]
    # A SECOND call, adding (`/grant`, never `/grant:r`): `:r` replaces a trustee's explicit grants, so the
    # runner's children-only Modify in the same call as its folder grant would undo one of the two.
    res = os.path.join(spool, "res")
    acls.append((res, ["icacls", res, "/grant", f"*{runner_sid}:(OI)(CI)(IO)M", "/q"]))
    return acls


def prepare_windows_spool(run: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> str:
    """Create the `run_as_session_user` spool on Windows and set its access lists — the job Linux's unit
    does with `ExecStartPre=+install -d`. Returns "" or an error string; never raises.

    Run by the daemon at startup (`main._prepare_windows_host`): the daemon creates the directories, so it
    OWNS them and may write their lists. Residual: `\\run` is runner-Modify, so a runner that
    made `exec\\` first would own it — a directory not owned by the daemon is returned as an error naming
    it (never re-owned here: the operator removes it), and `spool_ready` refuses delegation meanwhile. An
    `icacls` refusal (a broken install) is returned the same way. A no-op off Windows."""
    if not _on_windows():
        return ""
    run = run or _run_icacls
    try:
        daemon_sid, runner_sid = windows.daemon_sid(), windows.runner_sid()
    except OSError as exc:
        return f"cannot resolve the spool's accounts: {exc}"
    errors = []
    for path, argv in windows_spool_acls(daemon_sid, runner_sid):
        try:
            os.makedirs(path, exist_ok=True)
            owner = windows.path_owner_sid(path)
            if owner != daemon_sid:
                errors.append(f"{path}: owned by {owner}, not the daemon ({daemon_sid}) — remove it and "
                              "restart jaahd")
                continue
            proc = run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if proc.returncode != 0:
            errors.append(f"{path}: icacls rc {proc.returncode}: "
                          f"{(proc.stderr or proc.stdout or '').strip() or '(no output)'}")
    return "; ".join(errors)


def can_run_as_session_user(env: dict | None = None) -> bool:
    """Whether `run_as_session_user` can deliver: a session user, a fleet socket, and the spool. Callers
    fall back to in-process and must log that fallback on a switched node."""
    if not switch_needed():
        return False
    socket = fleet_socket(env)
    return bool(socket) and os.path.exists(socket) and spool_ready()


def delegated_path() -> str:
    """The PATH a DELEGATED command gets: the session user's `~/.local/bin` (the per-user `claude`
    install) ahead of this process's PATH.

    Never add this prefix to the daemon's own PATH: it is writable by `jaah`, so a daemon searching it
    would exec a runner-planted `git`/`tmux` as `jaahd`. A command already running as `jaah` gains
    nothing from it, which is what makes it safe here only.

    Windows spells it natively (`;`-separated, `<home>\\.local\\bin` — where Claude Code's installer puts
    `claude.exe`); `run_as_session_user` converts it to MSYS form only where a shell searches it."""
    sep, join = (ntpath.pathsep, ntpath.join) if _on_windows() else (os.pathsep, os.path.join)
    inherited = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    home = session_home()
    if not home:
        return inherited
    local_bin = join(home, ".local", "bin")
    parts = inherited.split(sep)
    # Windows: the bundled runner CLIs too (`runner_clis.windows_cli_dirs`, the list launch.sh searches), so
    # a delegated `<cli> mcp add` resolves the same binary a runner execs. In-function: runner_clis imports us.
    extra = [local_bin]
    if _on_windows():
        import runner_clis

        extra += runner_clis.windows_cli_dirs()
    missing = [d for d in extra if d not in parts]
    if not missing:
        return inherited
    return sep.join([*missing, *parts])


_DRIVE_RE = re.compile(r"\A([A-Za-z]):(?:[\\/]|\Z)")


def _shell_path(path: str) -> str:
    """PATH as the delegated `/bin/sh` should read it: unchanged off Windows; on Windows MSYS's own
    spelling (`C:\\x\\y` → `/c/x/y`). Git's runtime accepts `C:\\...` in a syscall, but string tools
    (`dirname`) and `$PATH` search split on `/` and `:`, so every path a script sees is converted. A native
    program the script runs gets Windows paths back from MSYS's own argument conversion."""
    if not _on_windows():
        return path
    match = _DRIVE_RE.match(path)
    if not match:
        return path.replace("\\", "/")
    return f"/{match.group(1).lower()}/" + path[match.end():].replace("\\", "/")


def _shell_search_path(value: str) -> str:
    """A `PATH` value for the delegated script's `export`: unchanged off Windows; on Windows each
    `;`-separated entry in MSYS form, joined with `:` — exported as-is, `sh` could find neither `timeout`
    nor anything else (`C:\\...;...` is one entry with a `:` in it)."""
    if not _on_windows():
        return value
    return ":".join(_shell_path(part) for part in value.split(ntpath.pathsep) if part)


#: Absolute, so an isolated job's exec chain does not search the PATH it is about to replace.
_ENV_BINARY = "/usr/bin/env"

#: This daemon's own variables an isolated command carries, where set — what it used to inherit from the server:
#: the vnode layout (a vnode's paths, and a credential helper that asks there and is refused fast rather than
#: probing the host's default socket), and the locale.
_CARRIED_ENV = (environment.CRED_SOCKET_ENV, environment.RUN_ROOT_ENV, environment.VNODE_ENV, "LANG", "LC_ALL")


def _isolates(argv: list[str]) -> bool:
    """Whether ARGV runs from an EMPTY environment plus its own (`run_as_session_user`): a `git` command, off
    Windows. The fleet tmux server is the session user's, so any `jaah` process can `tmux set-environment -g`
    into it, and every `run-shell` job inherits that — `GIT_CONFIG_PARAMETERS` naming a helper, a
    `GIT_SSH_COMMAND`, `GIT_TRACE_CURL`. Other commands (probes, `./dc`) keep the server's environment, which
    they may rely on; Windows is unchanged (a Windows program needs `SystemRoot` and friends)."""
    return bool(argv) and os.path.basename(argv[0]) == "git" and not _on_windows()


def run_as_session_user(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    input: str | None = None,  # noqa: A002 — `subprocess.run`'s own name for it
) -> subprocess.CompletedProcess:
    """Run ARGV as the session user and return a `CompletedProcess` (text mode), the same shape callers
    already handle from `subprocess.run`.

    ENV is the command's whole environment, exported by the generated script: a tmux server hands
    `run-shell` its own frozen environment. `HOME` defaults to the session user's, `PATH` to
    `delegated_path()`. It never locates the fleet socket — that is this process's `TMUX_TMPDIR`. For a
    `git` command off Windows it is ALL the command sees (`_isolates`): the job starts under `env -i` and the
    script exports the rest, so nothing a same-uid `tmux set-environment` put in the server reaches git (the
    values stay out of any argv: a `/proc/<pid>/cmdline` is world-readable).

    Nothing run here is ever served a credential: the command is the session user's tmux server's child, and
    the credential broker serves the daemon's own registered children only (`clone_identity`). A network git op
    runs as the daemon (`git_mirror`), never through this seam.

    `input` is the command's stdin, as `subprocess.run`'s: spooled to `req/<id>.in` (daemon-written, group-
    readable, as the script is) and redirected in, so it never crosses the tmux client's argv.

    Raises `TimeoutExpired` on the deadline and `RuntimeError` when the switch is unavailable, so "could
    not switch" is never mistaken for "ran and failed"."""
    if not can_run_as_session_user():
        raise RuntimeError(
            f"cannot run as {session_user()!r}: "
            f"switch_needed={switch_needed()} socket={fleet_socket()!r} spool={spool_ready()}")
    tmux = shutil.which("tmux")
    if not tmux:
        raise RuntimeError("cannot run as the session user: tmux is not on PATH")

    request_id = f"{int(time.time())}-{secrets.token_hex(6)}"
    script_path, out_path, err_path, rc_path = _spool_paths(request_id)
    isolated = _isolates(argv)
    command_env = dict(env or {})
    if isolated:
        for key in _CARRIED_ENV:
            if os.environ.get(key):
                command_env.setdefault(key, os.environ[key])
        command_env.setdefault("USER", session_user())
    command_env.setdefault("HOME", session_home() or "/tmp")
    command_env.setdefault("PATH", delegated_path())
    command_env["PATH"] = _shell_search_path(command_env["PATH"])
    for key in command_env:
        # The key is interpolated bare, so it must be a shell identifier; a bad key is a programming error.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid environment variable name for the session-user script: {key!r}")
    exports = "".join(
        f"export {key}={_shq(str(value))}\n" for key, value in sorted(command_env.items()))
    # `rc` is renamed into place last, so the poll never reads half-written stdout. rc 111 is this
    # wrapper's own "could not start" (a vanished `cd` target).
    #
    # `timeout` bounds the far side too: the wait's own deadline would leave the command running against
    # unlinked output. Slightly inside the poll deadline, so the daemon usually collects a real rc 124.
    far_deadline = max(1, int(timeout) - 5)
    # A `run-shell` job that prints anything, or exits non-zero, is shown in view mode in the server's current pane —
    # a runner's. So the job's own output goes nowhere, every path reports through the rc file, and the
    # job itself always exits 0.
    body = "#!/bin/sh\nexec >/dev/null 2>&1\n" + exports
    # Every path the shell reads goes through `_shell_path` (identity off Windows).
    sh_out, sh_err, sh_rc = (_shell_path(p) for p in (out_path, err_path, rc_path))
    body += (
        f"__rc() {{ printf '%s' \"$1\" >{_shq(sh_rc + '.tmp')}; mv {_shq(sh_rc + '.tmp')} {_shq(sh_rc)}; exit 0; }}\n"
    )
    if cwd:
        body += f"cd {_shq(_shell_path(cwd))} || __rc 111\n"
    in_path = script_path[:-len(".sh")] + ".in"
    stdin = f" <{_shq(_shell_path(in_path))}" if input is not None else ""
    body += (
        f"timeout -k 3 {far_deadline} {' '.join(_shq(a) for a in argv)}"
        f"{stdin} >{_shq(sh_out)} 2>{_shq(sh_err)}\n"
        f"__rc \"$?\"\n"
    )
    # O_EXCL at the final mode, never open()+chmod: under UMask=0002 the two-step leaves a window in
    # which a runner could rewrite the script. `_O_BINARY`: without it the Windows CRT descriptor is
    # text-mode and writes CRLF even through `newline="\n"` — `sh` reads the `\r` as part of every command.
    fd = os.open(script_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_binary(), 0o750)
    with os.fdopen(fd, "w", newline="\n") as handle:
        handle.write(body)
    if input is not None:
        # The script's own O_EXCL rule; 0640: the session user reads it through the group, as the script.
        fd = os.open(in_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_binary(), 0o640)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(input)

    job = f"/bin/sh {_shq(_shell_path(script_path))}"
    if isolated:
        job = f"exec {_ENV_BINARY} -i {job}"
    try:
        # `-b`: the result file is the completion signal. No `-t`: it would tie the request to a session.
        client = tmux_cmd(fleet_socket(), "run-shell", "-b", job)
        client[client.index("tmux")] = tmux  # the resolved binary, as a Windows `subprocess` needs
        spawn = subprocess.run(
            client,
            capture_output=True, text=True, timeout=30,
        )
        if spawn.returncode != 0:
            raise RuntimeError(
                f"tmux run-shell failed (rc={spawn.returncode}) on {fleet_socket()}: "
                f"{(spawn.stderr or '').strip() or '(no stderr)'}")
        # A refused client exits 0 too, and nothing then runs: named now rather than as a timeout later.
        refused = tmux_refusal(spawn)
        if refused:
            raise RuntimeError(f"tmux run-shell refused on {fleet_socket()}: {refused}")
        deadline = time.monotonic() + timeout
        while not os.path.exists(rc_path):
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            time.sleep(_POLL_INTERVAL_S)
        done = subprocess.CompletedProcess(
            argv, int(_read_result(rc_path) or "111"),
            _read_result(out_path), _read_result(err_path))
        note_session_user_outcome(argv, timed_out=done.returncode == 124)
        return done
    except subprocess.TimeoutExpired:
        note_session_user_outcome(argv, timed_out=True)
        raise
    finally:
        # On timeout the still-running command can recreate one `<id>.rc` after this sweep. Accepted:
        # one byte on tmpfs.
        for path in (script_path, in_path, out_path, err_path, rc_path, rc_path + ".tmp"):
            try:
                os.unlink(path)
            except OSError:
                pass


def run_as_session_user_or_here(argv: list[str], *, timeout: float, env: dict | None = None,
                                stdin: int | None = None) -> subprocess.CompletedProcess:
    """ARGV as the session user where the uid split applies (`run_as_session_user`: raises when the switch is
    unavailable), else in-process — one text-mode `CompletedProcess`, both streams captured. `env` is ADDED to
    this process's environment in-process (`None` inherits) and is the delegated command's whole environment
    (plus `HOME`/`PATH` defaults). `stdin` is in-process only: a delegated script's stdin is its own."""
    if switch_needed():
        return run_as_session_user(argv, env=env, timeout=timeout)
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=stdin,
                          env=None if env is None else {**os.environ, **env})


def run_or_refuse(argv: list[str], *, env: dict, timeout: float) -> subprocess.CompletedProcess | None:
    """ARGV as the session user, or in-process when this process already IS that user (a dev host, an
    operator shell: that grants nothing a runner does not have); None, logged, when this node cannot switch
    — fail closed, never as the daemon. For a command a runner-writable home can plant config into (a
    vendor CLI): run as `jaahd` it would hand the runner the daemon uid and `NODE_TOKEN`.

    `env` is the command's whole environment on both arms, under `HOME`/`PATH` defaults (the session
    user's; `delegated_path()`). In-process the binary is resolved on that PATH, as the delegated arm's
    shell would; one not on it is a logged None."""
    if is_session_user():
        env = {"HOME": session_home() or "/tmp", **env, "PATH": delegated_path()}
        binary = shutil.which(argv[0], path=env["PATH"])
        if not binary:
            log.warning("%s is not on the session user's PATH", argv[0])
            return None
        return run_bounded([binary, *argv[1:]], env=env, timeout=timeout)
    if not can_run_as_session_user():
        log.warning("this node cannot run a command as the session user — %s is not run (never as the "
                    "daemon)", argv[0])
        return None
    return run_as_session_user(argv, env=env, timeout=timeout)


def run_bounded(argv, *, timeout: float | None, cwd: str | None = None, env: dict | None = None,
                input: str | None = None,  # noqa: A002 — `subprocess.run`'s own name for it
                stdin: int | None = None, kill_group: bool = False) -> subprocess.CompletedProcess:
    """ARGV in-process, both streams captured, text mode, bounded by `timeout` — the ONE
    `subprocess.run(capture_output=True, text=True, timeout=…)` of the daemon. NEVER switches uid: a
    command that must run as the session user goes through `run_as_session_user` (or `_or_here`).
    `env` is the command's whole environment (`None` inherits). `TimeoutExpired`/`OSError` propagate; each
    caller keeps its own mapping. `timeout=None` only where the caller's own supervisor bounds it.

    `kill_group=True`: a timeout kills the whole process group, not just the command — a script is the
    wrapper shell's child, and killing only the shell would leave it running. (Windows has no group to
    kill and ignores `start_new_session`: the timeout kills the command itself.)"""
    argv = list(argv)
    if not kill_group:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env,
                              input=input, stdin=stdin)
    proc = subprocess.Popen(argv, stdin=stdin if input is None else subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=cwd, env=env,
                            start_new_session=hasattr(os, "killpg"))
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, 9)
            else:
                proc.kill()
        except OSError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


# ── The capped `{exit_code, stdout, stderr}` reply ────────────────────────────────────────────────────
#
# Every shell-out a native handler or account verb answers the bus with carries this one shape: the tails
# of both streams, capped so a reply is bounded (the bus frame) yet still names the failure (a gate's last
# lines). One home, so the reply is byte-stable whichever branch ran it — in-process, or delegated here.

STDOUT_CAP = 8000
STDERR_CAP = 2000


def capped_result(proc: subprocess.CompletedProcess) -> dict:
    """`{"exit_code", "stdout", "stderr"}` from a finished process, each stream's TAIL capped. `None`
    streams (a `CompletedProcess` built without capture) read as empty."""
    return {"exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[-STDOUT_CAP:], "stderr": (proc.stderr or "")[-STDERR_CAP:]}


def run_capped(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
    env: dict | None = None,
    input: str | None = None,  # noqa: A002 — `subprocess.run`'s own name for it
    stdin: int | None = None,
    as_session_user: bool = False,
    failure_rc: int = -1,
    failure_prefix: str = "",
) -> dict:
    """One bounded command → `capped_result`, NEVER raising: every caller answers a bus handler or a
    sensing pass that a raise would cost its reply.

    `env` is the command's WHOLE environment on both branches (`None` inherits this process's in-process;
    delegated, the caller decides what the session user sees — never `NODE_TOKEN`). A switch needed but
    unavailable is the CALLER's refusal, checked before this. A deadline is `{-1, "timeout: …"}`; any
    other in-process failure `{-1, str(exc)}`; a delegated one (the switch raced away, an OSError)
    `{failure_rc, "<failure_prefix>session-user delegation failed: …"}` — `failure_rc` lets a handler
    answer `worktree_mechanics.core.SWITCH_UNAVAILABLE_RC` (the seam failed, not the command),
    `failure_prefix` names the op; `stdin` (e.g. `subprocess.DEVNULL`) is in-process only, as
    `run_as_session_user_or_here`'s — with `as_session_user` it is a `ValueError`, never silently dropped (a
    delegated script's stdin is its own). Never retried in-process:
    that uid is the one the delegation keeps off the files."""
    # The optional kwargs ride only when given: the kwarg set is a contract the callers' test fakes spell
    # out, and the defaults are the runners' own.
    extra: dict = {}
    if input is not None:
        extra["input"] = input
    if stdin is not None:
        if as_session_user:
            raise ValueError("run_capped: stdin is in-process only; a delegated script's stdin is its own")
        extra["stdin"] = stdin
    try:
        if as_session_user:
            proc = run_as_session_user(list(argv), cwd=cwd, env=env, timeout=timeout, **extra)
        else:
            proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                                  cwd=cwd, env=env, **extra)
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": -1, "stdout": "", "stderr": f"timeout: {exc}"}
    except Exception as exc:  # noqa: BLE001 — never raises (docstring)
        if as_session_user:
            return {"exit_code": failure_rc, "stdout": "",
                    "stderr": f"{failure_prefix}session-user delegation failed: {exc}"}
        return {"exit_code": -1, "stdout": "", "stderr": str(exc)}
    return capped_result(proc)


def delegation_failure(argv: list[str], exc: BaseException, *, would_own: str) -> dict:
    """The failed-verb reply (`exit_code` -1) for an ACCOUNT verb that could not run as the session user —
    `accounts._claude_cli` and `agents.agent_layer.run_provider_cli`. Names both halves of the seam and
    why there is no in-process retry: that retry is the ownership write the delegation prevents."""
    return {"exit_code": -1, "stdout": "",
            "stderr": f"could not run {argv[0]!r} as {session_user()!r} "
                      f"({type(exc).__name__}: {exc}) — the fleet tmux server "
                      f"(jaah-runner@<node name>.service) and {exec_spool()} must both be up; "
                      f"running it as this uid would take ownership of {would_own}"}


def _read_result(path: str) -> str:
    """Read ONE result-spool file, refusing anything that is not a plain file created in place.

    `res/` is writable by the session user, so a planted SYMLINK would make the daemon read any file
    `jaahd` can open (`node.env`) back as "stdout". So `O_NOFOLLOW`, then regular-file + link-count-one
    on the open descriptor (refuses a hardlink without `fs.protected_hardlinks`). A forged regular file
    stays possible: an equal uid can forge real output anyway; the daemon's read authority never
    amplifies what that uid can see.

    Windows has no `O_NOFOLLOW`, and a junction needs no privilege (Developer Mode lifts it for symlinks
    too), so there the open descriptor is checked instead: the kernel's final path must lie under the
    resolved `res/`, and the file must be OWNED by the runner account — a file the runner did not create
    is not a result. Any failure of either check reads as "" (fail closed)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | _o_binary())
    except OSError:
        return ""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return ""
        if _on_windows() and not _windows_result_is_genuine(fd):
            return ""
        with os.fdopen(fd, errors="replace") as handle:
            fd = -1
            return handle.read()
    except OSError:
        return ""
    finally:
        if fd >= 0:
            os.close(fd)


def _windows_result_is_genuine(fd: int) -> bool:
    """Whether the result open on `fd` is where it claims (`fd_path` under the real `res/`, junctions and
    links already followed by the kernel) and was created by the runner account. Never raises."""
    try:
        res_root = os.path.realpath(os.path.join(exec_spool(), "res"))
        return (host_platform.is_under(host_platform.fd_path(fd), res_root)
                and windows.file_owner_sid(fd) == windows.runner_sid())
    except (OSError, ValueError):
        return False


def _shq(value: str) -> str:
    """POSIX single-quote quoting. Local: this module stays stdlib-only so the unit's launcher can import
    it without the daemon's import graph."""
    return "'" + str(value).replace("'", "'\\''") + "'"


# ── Session-user-aware filesystem writes ─────────────────────────────────────────────────────────────────
# The ownership contract: everything the daemon creates under `jaah_home` (or in a clone/worktree) must
# be OWNED by the session user — git distrusts a `jaahd`-owned repo for `jaah` (safe.directory), and
# only the owner can chmod. New dirs/files go through these helpers; rewriting an existing file in place
# keeps its owner. All return "" or an error string, never raise; when the switch is required but
# unavailable they fail rather than fall back in-process, which would recreate the ownership bug.
#
# Windows: the same delegation, for the same reason — a file the daemon creates in `jaah`'s home is OWNED
# by `NT SERVICE\jaahd`, and git's safe.directory refuses it for `jaah`. Paths reach the shell in MSYS
# form (`_shell_path`); `chmod` there is Git's MSYS chmod, which ignores mode bits on NTFS (rc 0), and
# the in-process arm's `os.chmod` only toggles the read-only flag.


def ensure_dir_as_session_user(path: str, timeout: float = 30, *, mode: int | None = None) -> str:
    """`mkdir -p PATH` as the session user where the uid split applies, else `os.makedirs` in-process.

    `mode` is applied as a separate `chmod`, never as mkdir's own mode: that is umask-masked, applies to
    the final path only, and is skipped for an existing directory (a redispatch reuses `_meta`).
    `mode=None` keeps the umask's bits. A failed chmod only warns: "" means the directory exists and is
    usable, not that the mode landed."""
    if switch_needed():
        if not can_run_as_session_user():
            return ("session-user switch required but unavailable — refusing to create "
                    f"{path} as the daemon uid; check jaah-runner@<node name>.service")
        try:
            if mode is None:
                proc = run_as_session_user(["mkdir", "-p", "--", _shell_path(path)], timeout=timeout)
            else:
                # One delegated round trip; path and mode ride `env=` so the script stays a fixed literal.
                # The mkdir decides the return code; a failed chmod only warns (only an owner may chmod,
                # so a dir an earlier build left foreign-owned must not fail every dispatch into it).
                proc = run_as_session_user(
                    ["/bin/sh", "-c",
                     'mkdir -p -- "$SU_DIR" || exit 1\n'
                     'chmod -- "$SU_MODE" "$SU_DIR" || '
                     'echo "warning: $SU_DIR exists but could not be chmod\'d to $SU_MODE — it keeps '
                     'the mode it had" >&2\n'],
                    env={"SU_DIR": _shell_path(path), "SU_MODE": format(mode, "o")},
                    timeout=timeout,
                )
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired, OSError
            return f"session-user delegation failed for mkdir {path}: {exc}"
        if proc.returncode != 0:
            return (proc.stderr or f"mkdir {path} rc {proc.returncode}").strip()
        if (proc.stderr or "").strip():
            # rc 0: the directory landed; stderr is the chmod that did not.
            log.warning("%s", proc.stderr.strip())
        return ""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return str(exc)
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError as exc:
            # Warned, not returned (see the delegated branch).
            log.warning("%s exists but could not be chmod'd to %o (%s) — it keeps the mode it had",
                        path, mode, exc)
    return ""


def chmod_as_session_user(path: str, mode: int, timeout: float = 30) -> str:
    """`chmod MODE PATH` as the session user where the uid split applies, else `os.chmod` in-process.

    Re-asserts a mode the daemon cannot set itself: only the owner may chmod, and an application
    rewriting a shared file with a private mode (claude's `~/.claude.json` 0600) locks the daemon out;
    see `fleet_launch.vis_trust`."""
    # `--` BEFORE the mode: BSD chmod (macOS) stops reading options at the mode, so a later `--` is a path.
    return _one_path_as_session_user(["chmod", "--", format(mode, "o")], path, timeout,
                                     lambda: os.chmod(path, mode), refusing="chmod")


def unlink_as_session_user(path: str, timeout: float = 30) -> str:
    """`rm -f -- PATH` as the session user where the uid split applies, else `os.unlink` in-process.

    The delete-side counterpart of the ownership contract: the parent is setgid and group-writable, so
    the daemon could unlink, but one uid creates and the same uid deletes, so ownership stays auditable.
    `rm -f`: an already-gone file is not a failure (racing sweeps, a manual clear). `--` before the path.
    No glob, one path per call — the argv is fixed options plus one path, so nothing can widen. Returns
    "" or an error string, never raising; the caller decides what a failed delete means."""
    # `rm -f` semantics on the in-process arm too: an absent file is not a failure. `os.unlink`, not
    # `Path.unlink`: the path reaches the kernel as spelled (no normalisation of "" or a trailing slash).
    return _one_path_as_session_user(["rm", "-f", "--"], path, timeout,
                                     lambda: _unlink_if_present(path), refusing="delete")


def _unlink_if_present(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _one_path_as_session_user(argv: list[str], path: str, timeout: float, in_process: Callable[[], object], *,
                              refusing: str) -> str:
    """`argv` + PATH (fixed options plus the one path, in `_shell_path` spelling, so nothing can widen) as the
    session user where the uid split applies, else `in_process()` here. "" or an error string, never raising.
    A switch that is needed but unavailable FAILS, never falls back in-process: that would act as the daemon uid."""
    if switch_needed():
        if not can_run_as_session_user():
            return (f"session-user switch required but unavailable — refusing to {refusing} "
                    f"{path} as the daemon uid; check jaah-runner@<node name>.service")
        try:
            proc = run_as_session_user([*argv, _shell_path(path)], timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired, OSError
            return f"session-user delegation failed for {argv[0]} {path}: {exc}"
        return "" if proc.returncode == 0 else (proc.stderr or f"{argv[0]} {path} rc {proc.returncode}").strip()
    try:
        in_process()
    except OSError as exc:
        return str(exc)
    return ""


def remove_tree_as_session_user(path: str, timeout: float = 120) -> str:
    """`rm -rf -- PATH` as the session user — the tree counterpart of `unlink_as_session_user`, for a
    caller on Windows, where the daemon deletes nothing under the runner-writable home itself:
    a runner can shape that tree with junctions, which need no privilege there. One path, fixed options.
    Returns "" or an error string, never raising; refuses rather than deleting as the daemon."""
    if not can_run_as_session_user():
        return (f"session-user switch unavailable — refusing to delete {path} as the daemon; check the "
                f"runner service")
    try:
        proc = run_as_session_user(["rm", "-rf", "--", _shell_path(path)], timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired, OSError
        return f"session-user delegation failed for rm -rf {path}: {exc}"
    return "" if proc.returncode == 0 else (proc.stderr or f"rm -rf {path} rc {proc.returncode}").strip()


def _staged_copy(path: str, produce, mode: int, timeout: float) -> str:
    """The delegated leg of the two writers below: `produce(handle)` fills a `0640` file staged in `req/`,
    then a session-user `cp` creates PATH from it. Raises propagate (staged file unlinked first)."""
    if not can_run_as_session_user():
        return ("session-user switch required but unavailable — refusing to write "
                f"{path} as the daemon uid; check jaah-runner@<node name>.service")
    staged = os.path.join(exec_spool(), "req",
                          f"file-{int(time.time())}-{secrets.token_hex(6)}.dat")
    script = ('umask 0002 && mkdir -p -- "$(dirname -- "$SU_DEST")" && '
              'cp -- "$SU_SRC" "$SU_DEST" && chmod -- "$SU_MODE" "$SU_DEST"')
    try:
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_binary(), 0o640)
        with os.fdopen(fd, "wb") as handle:
            produce(handle)
        proc = run_as_session_user(
            ["/bin/sh", "-c", script],
            env={"SU_SRC": _shell_path(staged), "SU_DEST": _shell_path(path), "SU_MODE": format(mode, "o")},
            timeout=timeout,
        )
    finally:
        try:
            os.unlink(staged)
        except OSError:
            pass
    return "" if proc.returncode == 0 else (proc.stderr or f"write {path} rc {proc.returncode}").strip()


def stream_file_as_session_user(path: str, produce, mode: int = 0o664,
                                timeout: float = 30) -> str:
    """`write_file_as_session_user`, for content too large to hold: `produce(handle)` WRITES it.

    Same seam as the sibling below — staged into `req/`, a delegated `cp`, only path/mode strings on
    the env — but the caller writes straight into the staged descriptor, so a large attachment is never
    held in memory. A raise from `produce` never delegates: the staged file is unlinked and the exception
    propagates, so a partial body never becomes a complete-looking file. Returns "" or an error string;
    the caller owns the byte count (`control_plane_api._CountingSink`)."""
    if switch_needed():
        return _staged_copy(path, produce, mode, timeout)
    # No uid split: a raise from `produce` leaves a partial file. The launch callers wipe and recreate
    # their directory on the next attempt; the warm-answer stager re-fetches a file whose size is wrong.
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as handle:
        produce(handle)
    os.chmod(path, mode)
    return ""


def write_file_as_session_user(path: str, content: str | bytes, mode: int = 0o664,
                                timeout: float = 30) -> str:
    """Write CONTENT to PATH (parent dirs created, mode applied) as the session user where the uid split
    applies, else in-process.

    The content crosses the spool as a FILE, never an env value: execve caps one env string at ~128KiB
    (MAX_ARG_STRLEN → E2BIG) and NULs cannot cross at all. Staged `0640` into `req/`, then a delegated
    `cp` creates the destination as the session user. In-process, mkdir → write → chmod."""
    data = content.encode() if isinstance(content, str) else content
    if switch_needed():
        try:
            return _staged_copy(path, lambda handle: handle.write(data), mode, timeout)
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired, OSError
            return f"session-user delegation failed for write {path}: {exc}"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, mode)
    except OSError as exc:
        return str(exc)
    return ""


def mv_over_script() -> str:
    """The shell fragment that renames `$SU_SRC` over `$SU_DST` as `rename(2)` does: GNU's `mv -fT`. BSD mv (macOS)
    has no `-T`, so there `-h` (a link to a directory is replaced, not entered) behind the refusal of a real
    directory that `-T` makes too. `/bin/mv` by path: a GNU `mv` earlier on the session user's PATH rejects `-h`.
    Unlike `rename(2)`, a directory the session user makes between the test and the move is entered; that is the
    session user racing its own files, no boundary crossed."""
    if host_platform.is_macos():
        return ('{ [ ! -d "$SU_DST" ] || [ -L "$SU_DST" ] || { echo "mv: $SU_DST is a directory" >&2; false; }; } '
                '&& /bin/mv -fh -- "$SU_SRC" "$SU_DST"')
    return 'mv -fT -- "$SU_SRC" "$SU_DST"'


def new_session_cmd(*, fork: bool = False) -> str:
    """The shell-word prefix that runs the command after it in a NEW SESSION, as util-linux `setsid` does
    (`fork=True`: `setsid -f`, always fork and return at once). macOS ships no `setsid` binary, so there the
    bundled Python makes the call: `setsid(2)` then `execvp` of the rest of the line, keeping the pid a pid
    file records. As util-linux, it forks first only when it is already a group leader (where `setsid(2)`
    fails) or when told to. The interpreter is the bundle's, which the session user can run."""
    if host_platform.is_macos():
        code = (f"import os,sys; ({fork} or os.getpgrp()==os.getpid()) and os.fork() and os._exit(0); "
                "os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])")
        return f"{_shq(host_platform.paths().python)} -I -c {_shq(code)}"
    return "setsid -f" if fork else "setsid"


def replace_file_as_session_user(path: str, content: str | bytes, mode: int = 0o664,
                                 timeout: float = 30) -> str:
    """Replace PATH with CONTENT atomically, as the session user where the uid split applies
    (`write_file_as_session_user` to a sibling temp file, then `rename(2)` over PATH), else in-process
    (`env_helpers.atomic_write_text`), so a reader sees the old file or the new one and never a half-written
    one. Returns "" or an error string (the temp file is removed on a failed rename)."""
    if not switch_needed():
        try:
            # Bytes, so a text payload keeps its LF line endings on a Windows node (text mode would CRLF them).
            env_helpers.atomic_write_text(Path(path), content.encode() if isinstance(content, str) else content,
                                          mode=mode)
        except OSError as exc:
            return str(exc)
        return ""
    staged = f"{path}.tmp-{secrets.token_hex(6)}"
    err = write_file_as_session_user(staged, content, mode=mode, timeout=timeout)
    if err:
        return err
    try:
        proc = run_as_session_user(
            ["/bin/sh", "-c", mv_over_script() + ' || { rm -f -- "$SU_SRC"; exit 1; }'],
            env={"SU_SRC": _shell_path(staged), "SU_DST": _shell_path(path)}, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired, OSError
        return f"session-user delegation failed for replace {path}: {exc}"
    return "" if proc.returncode == 0 else (proc.stderr or f"replace {path} rc {proc.returncode}").strip()


# ── Symlinks, made and removed as the session user ────────────────────────────────────────────────────────
# Windows: the runner account holds `SeCreateSymbolicLinkPrivilege` (JaahService.cs grants it),
# the daemon does not, and the trees these links live in are runner-writable — so the account that owns
# them makes and removes them. The scripts are fixed literals, run by the daemon's own interpreter with
# `-I` (nothing from the cwd or the environment is imported); each takes its paths as argv and prints one
# line, never a secret. `_LINK_COMMON_PY`'s prefix list mirrors `shared_storage.link_target_of`'s: an `-I -c`
# script has no daemon tree on `sys.path` to import it from. No `/` anywhere in them: MSYS rewrites an
# argument that looks like a POSIX path before a native program sees it.
_LINK_COMMON_PY = r"""import os, secrets, stat, sys
def plain(raw):
    for prefix, spelled in (("\\\\?\\UNC\\", "\\\\"), ("\\??\\UNC\\", "\\\\"), ("\\\\?\\", ""), ("\\??\\", "")):
        if raw.startswith(prefix):
            return spelled + raw[len(prefix):]
    return raw
def same(raw, target):
    return os.path.normcase(plain(raw)) == os.path.normcase(plain(target))
def refuse(why):
    print(why)
    sys.exit(1)
def info_of(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
def odd(info):
    return not stat.S_ISLNK(info.st_mode) and bool(getattr(info, "st_file_attributes", 0) & 0x400)
def failed(what, exc):
    if getattr(exc, "winerror", None) == 1314:
        refuse(what + ": this account lacks SeCreateSymbolicLinkPrivilege (Create symbolic links)")
    refuse(what + " (" + type(exc).__name__ + ": " + str(exc).replace("\n", " ") + ")")
"""
# argv: target, link, directory "1"/"0", replace_file "1"/"0". A regular file is replaced only on request,
# through a link made at a random sibling then renamed over it, so a reader never sees the name missing.
_SYMLINK_PY = _LINK_COMMON_PY + r"""target, link, directory, replace = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4] == "1"
info = info_of(link)
try:
    if info is None:
        os.symlink(target, link, target_is_directory=directory)
    elif stat.S_ISLNK(info.st_mode):
        if not same(os.readlink(link), target):
            refuse(link + " is a link to somewhere else - refusing to replace it")
    elif stat.S_ISREG(info.st_mode) and replace and not odd(info):
        staged = os.path.join(os.path.dirname(link), "." + os.path.basename(link) + ".link-" + secrets.token_hex(6))
        os.symlink(target, staged, target_is_directory=directory)
        try:
            os.replace(staged, link)
        except OSError:
            os.unlink(staged)
            raise
    else:
        refuse(link + " is neither absent nor a link - refusing to replace it")
except OSError as exc:
    failed("could not link " + link + " to " + target, exc)
"""
# argv: link, expected target.
_UNLINK_LINK_PY = _LINK_COMMON_PY + r"""link, expected = sys.argv[1], sys.argv[2]
info = info_of(link)
if info is not None:
    if not stat.S_ISLNK(info.st_mode) or not same(os.readlink(link), expected):
        refuse(link + " is not a link to " + expected + " - refusing to remove it")
    try:
        os.unlink(link)
    except FileNotFoundError:
        pass
    except OSError as exc:
        failed("could not remove " + link, exc)
"""


def _run_link_script(script: str, args: list[str], what: str, timeout: float) -> str:
    """Run one of the link scripts as the session user where the uid split applies, else as this process
    (the same script, so both arms share one logic). "" on rc 0, else its one-line reason."""
    argv = [sys.executable, "-I", "-c", script, *args]
    try:
        if switch_needed():
            if not can_run_as_session_user():
                return (f"session-user switch required but unavailable — refusing to {what} as the daemon; "
                        f"check the runner service")
            proc = run_as_session_user(argv, timeout=timeout)
        else:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired, OSError
        return f"session-user delegation failed to {what}: {exc}"
    if proc.returncode == 0:
        return ""
    reason = " ".join(((proc.stdout or "").strip() or (proc.stderr or "").strip()).split())
    return reason[:500] or f"{what}: rc {proc.returncode}"


def symlink_as_session_user(target: str, link: str, *, directory: bool, replace_file: bool = False,
                            timeout: float = 30) -> str:
    """Make LINK a symlink to TARGET as the session user. "" on success, and when LINK already is a symlink
    reading exactly TARGET (`\\\\?\\`, `\\??\\` and `\\\\?\\UNC\\` spellings compared as the plain path);
    else a one-line reason. A regular file at LINK is replaced only with `replace_file`; anything else —
    another link, a junction, a directory — is refused. Never raises."""
    return _run_link_script(_SYMLINK_PY, [target, link, "1" if directory else "0", "1" if replace_file else "0"],
                            f"link {link} to {target}", timeout)


def unlink_link_as_session_user(link: str, expected_target: str, timeout: float = 30) -> str:
    """Remove the symlink LINK as the session user. "" when it is absent or was removed; refuses (a one-line
    reason) anything that is not a symlink reading EXPECTED_TARGET, so nothing a runner put there instead is
    ever deleted. Never raises."""
    return _run_link_script(_UNLINK_LINK_PY, [link, expected_target], f"remove the link {link}", timeout)
