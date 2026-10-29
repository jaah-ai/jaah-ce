#!/usr/bin/env python3
"""fleet_tmux_server.py — ExecStart of `jaah-runner@<session>.service`: seeds the tmux server every
fleet session runs on, as user `jaah` (systemd's `User=` switches uid). Idempotent. Stdlib-only leaves.

Inside that uid: create the detached session; keep it alive (holder window + `exit-empty off` — the
server dies with its last window); widen its own socket for the daemon (only the owner may — the daemon
doing it would be self-escalation); and converge the account tree group-readable
(`env_helpers.share_home_readable` — `chmod` needs ownership). Usage:
`fleet_tmux_server.py [--loop] <session-name>`; the instance name is this node's NAME.

On Windows it is the `jaah-runner` service's loop, as the local account `jaah`: the socket is
`session_user.fleet_socket`, the grant names `NT SERVICE+jaahd`, the default shell is Git's bash, and there
is no chmod (ACLs inherit) — so no socket widening and no readability pass.

On macOS it is `ai.jaah.runner`'s loop (the bundle's `jaah-runner-launch`), as the hidden user
`_jaah`: the Linux contract unchanged (a `tmux-<uid>` socket widened for the daemon's group, the
`server-access` grant, the readability pass), with a holder BSD `sleep` accepts (`holder_command`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import env_helpers  # noqa: E402 (the sys.path bootstrap above has to run first)
import environment   # noqa: E402 (same) — every outward surface's env var name has one home
import host_platform  # noqa: E402 (same) — the OS seam
import session_user  # noqa: E402 (same)

# Keeps the session (and server) alive across runner churn. `sleep infinity`, not a shell: one stray
# `exit` in an attached console would take the session down. macOS's BSD `sleep` refuses `infinity` and
# exits at once — taking the server with it — so there it is the largest interval it takes (~68 years).
HOLDER_WINDOW = "_holder"
HOLDER_COMMAND = "sleep infinity"
HOLDER_COMMAND_MACOS = "sleep 2147483647"


def holder_command() -> str:
    """The holder window's command on this OS."""
    return HOLDER_COMMAND_MACOS if host_platform.is_macos() else HOLDER_COMMAND


# Matches `fleet_launch.vis_window`; a detached session is otherwise 80x24 until a client attaches.
SESSION_WIDTH = "220"
SESSION_HEIGHT = "50"

_TMUX_TIMEOUT_S = 30

# Bounds how long a lost session stays down: `Restart=` watches the process, not the session, and the
# daemon must not create it (it would own the server as `jaahd`).
CONVERGE_INTERVAL_S = 30

# Windows panes run Git's bash, by its MSYS path (the runtime tmux.exe binds to). Without it tmux falls
# back to $SHELL / the account's shell, which Windows has no notion of.
WINDOWS_DEFAULT_SHELL = "/usr/bin/bash"

# A pane's TERM on Windows. The runtime starts a native child (claude.exe) on a pseudo console only when
# TERM has a terminfo entry under Git's own /usr/share/terminfo, and Git ships none for tmux's default
# (tmux-256color/screen): the child then sees a pipe, Claude Code drops to --print and exits. Measured on
# Windows Server 2025 with Git 2.56.
WINDOWS_DEFAULT_TERMINAL = "xterm-256color"


def own_socket(env: dict | None = None) -> str:
    """This process's OWN tmux socket path — `$TMUX_TMPDIR/tmux-<euid>/default`.

    From `os.geteuid()`, not `session_user.fleet_socket()`: never hand tmux another uid's path. Never a
    bare label — it resolves per-euid.

    Windows has no euid: the socket is named after the session user, which `seed` has already proven
    this process is (`switch_needed`), so it IS `session_user.fleet_socket` ("" with no session user)."""
    if host_platform.is_windows():
        return session_user.fleet_socket(env)
    return os.path.join(session_user.socket_dir_for(os.geteuid(), env), "default")


def _tmux(socket: str, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        session_user.tmux_cmd(socket, *args),
        env=env, capture_output=True, text=True, timeout=_TMUX_TIMEOUT_S,
    )


def _stderr(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or "").strip() or "(no stderr)"


def seed(session: str, env: dict | None = None, *, announce: bool = True) -> int:
    """Create-or-converge session SESSION on this process's own tmux server. Returns an exit code.

    `announce=False` (the loop's steady state) is silent when already converged; repairs and failures
    always print."""
    env = dict(os.environ if env is None else env)

    # Refuse rather than seed a fleet server on the daemon's uid. False on a host with no `jaah` user.
    if session_user.switch_needed():
        who = ("another account" if host_platform.is_windows()
               else f"euid {os.geteuid()}")
        print(f"fleet_tmux_server: REFUSING to seed session {session!r} — fleet sessions must be "
              f"owned by user {session_user.session_user()!r} (uid {session_user.session_uid()}), but "
              f"this process is {who}. A server seeded here would fork every runner pane "
              f"as the wrong uid.", file=sys.stderr)
        return 1

    if not shutil.which("tmux", path=env.get("PATH")):
        print("fleet_tmux_server: tmux is not on PATH — there can be no fleet server, so every runner "
              "dispatch, the Web Console and session_user.run_as_session_user will refuse. tmux is a "
              "`recommends:` in packaging/nfpm.yaml; install it.", file=sys.stderr)
        return 1

    socket = own_socket(env)
    if not socket:
        # Windows with JAAH_SESSION_USER emptied: no account to name the socket directory after.
        print("fleet_tmux_server: no session user is configured (JAAH_SESSION_USER is empty), so there "
              "is no fleet socket to seed.", file=sys.stderr)
        return 1
    # Ours to create: with `-S <path>` and the dir missing, tmux EXITS 0 with no server. 0700 as tmux
    # would make it; `share_own_socket()` widens to 0750 after the socket exists.
    socket_dir = os.path.dirname(socket)
    try:
        os.makedirs(socket_dir, mode=0o700, exist_ok=True)
    except OSError as exc:
        print(f"fleet_tmux_server: cannot create the tmux socket directory {socket_dir}: {exc} — "
              f"its parent is the shared TMUX_TMPDIR, created 2770 jaahd:jaah by "
              f"jaah-runner@.service's own ExecStartPre.", file=sys.stderr)
        return 1

    if host_platform.is_windows():
        rc = _ensure_foreground_server(socket, env)
        if rc != 0:
            return rc
    rc, created = _ensure_session(socket, session, env)
    if rc != 0:
        return rc
    # The ACL grant: tmux >=3.3 refuses non-owner clients and EXITS 0, so socket mode is only half.
    # Only the owner can grant; converged every run. The package postinst applies the same grant — keep in step.
    if not _grant(socket, session_user.daemon_user(), env):
        return 1
    _converge_options(socket, env)

    if host_platform.is_windows():
        # No chmod on Windows: the directory inherits the installer's list (daemon Modify); the
        # `server-access` grant above is the whole gate.
        if announce or created:
            print(f"fleet_tmux_server: session {session!r} {'created' if created else 'already running'} "
                  f"on {socket}; granted {session_user.daemon_user()}")
        return 0
    shared = session_user.share_own_socket(env)
    if shared is None and session_user.is_session_user():
        # Otherwise silent and total: every dispatch refuses "socket not ready" with no cause.
        print(f"fleet_tmux_server: could not widen {socket} for group `jaah` — the daemon cannot "
              f"connect as a client, so NOTHING can be dispatched onto this server. See "
              f"session_user.share_own_socket's log line for the errno.", file=sys.stderr)
        return 1

    if announce or created:
        print(f"fleet_tmux_server: session {session!r} {'created' if created else 'already running'} on "
              f"{socket}; " + (f"widened {shared} to 0750/0660 for group `jaah`" if shared else
                               "socket left as tmux created it (no session user on this host — one uid, "
                               "nothing to share with)"))
    return 0


# Windows: the server this loop started, held so it is started once (`_ensure_foreground_server`).
_SERVER: list = [None]
_SERVER_READY_S = 10.0
# What a tmux client prints when nothing listens on its socket (the socket file absent, or left behind).
_NO_SERVER = ("no server running", "error connecting to")


def _server_listening(socket: str, env: dict) -> bool:
    probe = _tmux(socket, "list-sessions", env=env)
    return probe.returncode == 0 or not any(text in (probe.stderr or "") for text in _NO_SERVER)


def _ensure_foreground_server(socket: str, env: dict) -> int:
    """Windows: start the fleet server IN THE FOREGROUND (`tmux -D`) as this loop's own child, unless one
    already listens on SOCKET.

    The daemon's account cannot read a runner process's token, so its only proof that a process is the
    runner account's is ancestry to the `jaah-runner` service process (`host_platform.windows`'
    `fleet_server_anchor`). A daemonised server (what `new-session -d` starts) is the child of a fork that
    exits at once, which cuts every pane off from the service (measured on a real PC); `-D`
    keeps the server a child of this loop, itself the service's child. A server already listening is left
    alone here: after a restart of the service it is the previous loop's, which the daemon may have verified
    (`fleet-anchor`), and `_recycle_stale_windows_server` replaces it once it is idle. No output pipe is shared: the service's would stay held by the server for its whole life."""
    child = _SERVER[0]
    if child is not None and child.poll() is None:
        return 0
    if _server_listening(socket, env):
        return 0
    try:
        _SERVER[0] = subprocess.Popen(session_user.tmux_cmd(socket, "-D"), env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        print(f"fleet_tmux_server: could not start the tmux server on {socket}: {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + _SERVER_READY_S
    while time.monotonic() < deadline:
        if _SERVER[0].poll() is not None:
            print(f"fleet_tmux_server: the tmux server on {socket} exited at once "
                  f"(rc={_SERVER[0].returncode})", file=sys.stderr)
            return 1
        if _server_listening(socket, env):
            print(f"fleet_tmux_server: started the tmux server on {socket} in the foreground "
                  f"(pid {_SERVER[0].pid})")
            _attest_server(socket, _SERVER[0].pid)
            return 0
        time.sleep(0.2)
    print(f"fleet_tmux_server: the tmux server on {socket} did not answer within {_SERVER_READY_S:.0f}s",
          file=sys.stderr)
    return 1


def _attest_server(socket: str, pid: int) -> None:
    """Record the server this loop just started, as the runner account (`windows.attest_fleet_server`): once
    `jaah-runner` restarts, that record is the daemon's only proof the server is the runner account's. A failed
    record is logged and costs only that proof."""
    from host_platform import windows  # noqa: PLC0415 — the Windows-only seam
    try:
        windows.attest_fleet_server(socket, pid)
    except (OSError, ValueError, TypeError) as exc:
        print(f"fleet_tmux_server: could not record the tmux server (pid {pid}) for the daemon: {exc}",
              file=sys.stderr)


def _ensure_session(socket: str, session: str, env: dict) -> tuple[int, bool]:
    """Create SESSION on SOCKET's server unless it is there, and confirm the server survived. Returns
    `(exit code, created)`."""
    # `=NAME` is tmux's exact-match target; bare `-t fleet` also matches `fleet-smoketest`.
    target = f"={session}"

    created = False
    if _tmux(socket, "has-session", "-t", target, env=env).returncode != 0:
        new = _tmux(socket, "new-session", "-d", "-s", session, "-n", HOLDER_WINDOW,
                    "-x", SESSION_WIDTH, "-y", SESSION_HEIGHT, holder_command(), env=env)
        if new.returncode != 0:
            print(f"fleet_tmux_server: tmux new-session FAILED (rc={new.returncode}) for session "
                  f"{session!r} on {socket}: {_stderr(new)}", file=sys.stderr)
            return 1, False
        created = True
        # rc=0 does not mean the server survived: with SHELL unset tmux uses the passwd shell (nologin),
        # which exits and collapses the server while `new-session -d` still exits 0.
        gone = _tmux(socket, "has-session", "-t", target, env=env)
        if gone.returncode != 0:
            print(f"fleet_tmux_server: tmux reported success creating session {session!r} but the "
                  f"server is already gone ({_stderr(gone)}). The usual cause is $SHELL: this process "
                  f"has SHELL={env.get('SHELL') or '(unset)'}, and with it unset tmux runs the pane "
                  f"through the passwd shell, which is /usr/sbin/nologin for user "
                  f"{session_user.session_user() or 'jaah'} on every pre-package node. The unit's "
                  f"Environment=SHELL=/bin/bash exists for exactly this.", file=sys.stderr)
            return 1, created
    return 0, created


def _grant(socket: str, user: str, env: dict) -> bool:
    """Converge `server-access -w USER` on SOCKET's server. True when USER holds a WRITE grant (or the
    tmux predates the ACL). `-w` because a client creates, kills, pipes and resizes."""
    grant = _tmux(socket, "server-access", "-aw", user, env=env)
    if grant.returncode == 0:
        return True
    if "unknown command" in (grant.stderr or ""):
        # pre-3.3 tmux: there is no ACL, so there is nothing to grant and nothing denying anyone.
        return True
    if "already added" in (grant.stderr or ""):
        # `-aw` fails on an existing entry; `-w` re-asserts write on it, keeping this a convergence.
        rewrite = _tmux(socket, "server-access", "-w", user, env=env)
        if rewrite.returncode == 0:
            return True
        print(f"fleet_tmux_server: {user} is already on {socket}'s access "
              f"list but re-asserting WRITE failed (rc={rewrite.returncode}): {_stderr(rewrite)} "
              f"— a read-only grant refuses the daemon's new-window/kill-window with exit 0, i.e. "
              f"every dispatch silently does nothing. Refusing to report this server as up.",
              file=sys.stderr)
        return False
    print(f"fleet_tmux_server: tmux server-access -aw {user} FAILED "
          f"(rc={grant.returncode}) on {socket}: {_stderr(grant)} — the daemon's client calls "
          f"will be refused with exit 0, i.e. every dispatch and console open silently does "
          f"nothing. Refusing to report this server as up.", file=sys.stderr)
    return False


def _converge_options(socket: str, env: dict) -> None:
    # Every run: a server surviving an upgrade keeps its old options. `exit-empty off` lets the socket
    # outlive a lost session; `mouse on` mirrors `vis_window`, `-g` for lack of a `-t` target.
    options = [("set-option", "-s", "exit-empty", "off"),
               ("set-option", "-g", "mouse", "on")]
    if host_platform.is_windows():
        options.append(("set-option", "-g", "default-shell", WINDOWS_DEFAULT_SHELL))
        options.append(("set-option", "-g", "default-terminal", WINDOWS_DEFAULT_TERMINAL))
    for option in options:
        proc = _tmux(socket, *option, env=env)
        if proc.returncode != 0:
            # Not fatal, but losing `exit-empty off` turns "last runner finished" into a restart cycle.
            print(f"fleet_tmux_server: warning: tmux {' '.join(option)} failed (rc={proc.returncode}) "
                  f"on {socket}: {_stderr(proc)}", file=sys.stderr)


# Once-per-process latch for `_converge_shared_readability`'s warning.
_announced_no_account_tree = False


def _converge_shared_readability() -> int:
    """Widen this uid's own files back to group-readable, and report how many moved. Never raises.

    Here because `chmod` needs ownership (`env_helpers.share_home_readable`'s docstring). Its own 60s floor
    means at most one walk a minute on the 30s tick. Skipped on Windows: mode bits are not its access
    control (ACLs inherit), so there is nothing to widen."""
    if host_platform.is_windows():
        return 0
    # Said once, loudly: with `JAAH_HOME` unset `share_home_readable` skips the tree at DEBUG only.
    global _announced_no_account_tree
    if not environment.pool_root_or_empty() and not _announced_no_account_tree:
        _announced_no_account_tree = True
        print("fleet_tmux_server: JAAH_HOME is unset — the account tree is NOT being converged to "
              "group-readable, so session transcripts will stay unreadable to the daemon",
              file=sys.stderr)
    try:
        widened = env_helpers.share_home_readable()
    except Exception as exc:  # noqa: BLE001 — an operator convenience must never take the unit down
        print(f"fleet_tmux_server: share_home_readable failed ({exc})", file=sys.stderr)
        return 0
    if widened:
        print(f"fleet_tmux_server: widened {widened} path(s) to group-readable")
    return widened


def recycle_stale_server(session: str, env: dict | None = None) -> bool:
    """Kill this loop's tmux server when it runs in an OLDER mount namespace than the loop and holds nothing
    but the holder. Returns True when it killed it; the next `seed` starts a fresh one.

    `KillMode=process` keeps the server (and so every pane) across a unit restart, and the holder keeps it
    alive forever, so a fence change in the unit's drop-in never reaches a session: the server, and every
    pane it forks, keeps the mounts of the unit instance that first started it. That is how a co-located
    node kept its old `/proc` overmounts after the fix and every merge-lane gate's `bwrap --proc` failed
    ("Can't mount proc"). The loop itself starts in the CURRENT namespace, so a mismatch means
    stale. "Nothing but the holder": no window but the holder, no attached client, and no server child but
    the holder's pane — a `run-shell -b` job (`session_user.run_as_session_user`) is a child, not a window.
    A busy server is retried next tick. On Windows the staleness is a token, not a namespace
    (`_recycle_stale_windows_server`); anything unreadable is a no-op."""
    if host_platform.is_windows():
        return _recycle_stale_windows_server(session, env)
    mine = host_platform.mount_namespace("self")
    if mine is None:
        return False
    env = dict(os.environ if env is None else env)
    socket = own_socket(env)
    shown = _tmux(socket, "display-message", "-p", "-t", f"={session}", "#{pid}", env=env)
    pid = (shown.stdout or "").strip()
    if shown.returncode != 0 or not pid.isdigit():
        return False
    theirs = host_platform.mount_namespace(pid)
    if theirs is None or mine == theirs:
        return False
    panes = _tmux(socket, "list-panes", "-a", "-F", "#{pane_pid} #{window_name}", env=env)
    clients = _tmux(socket, "list-clients", env=env)
    if panes.returncode != 0 or clients.returncode != 0 or (clients.stdout or "").strip():
        return False
    holders = set()
    for line in (panes.stdout or "").splitlines():
        pane_pid, _, window = line.partition(" ")  # tmux prints a tab as `_`
        if window != HOLDER_WINDOW:
            return False
        holders.add(pane_pid)
    children = host_platform.child_pids(pid)
    if children is None or children - holders:
        return False
    # A launch landing between these reads and the kill loses its pane: a seconds-wide, one-off window.
    killed = _tmux(socket, "kill-server", env=env)
    if killed.returncode != 0:
        return False
    print(f"fleet_tmux_server: recycled the tmux server (pid {pid}) on {socket}: it ran in an older mount "
          f"namespace than this unit and held no session")
    return True


# Run through the server by `_server_can_symlink`: create and remove a symlink in a fresh temp dir. Prints
# the verdict (a sync `run-shell`'s exit status is tmux's to report, its output is ours); exit 3 is 1314.
# No `#` anywhere: `run-shell` expands formats in its command.
_SYMLINK_PROBE = """import os, shutil, sys, tempfile
d = tempfile.mkdtemp()
try:
    open(os.path.join(d, "t"), "w").close()
    os.symlink(os.path.join(d, "t"), os.path.join(d, "l"))
except OSError as e:
    print("symlink:" + str(getattr(e, "winerror", None)))
    sys.exit(3 if getattr(e, "winerror", None) == 1314 else 1)
finally:
    shutil.rmtree(d, ignore_errors=True)
print("symlink:ok")
"""

# Latched once a probe shows the server can symlink: the token never loses the right in this loop's life.
_windows_server_fresh = False
# The probe's consecutive non-answers; at the cap it is never asked again in this loop's life.
_windows_probe_silences = 0
_WINDOWS_PROBE_SILENCE_CAP = 10


def _can_symlink_here() -> bool:
    """True when THIS process may create a symlink (SeCreateSymbolicLinkPrivilege). Never raises."""
    try:
        d = tempfile.mkdtemp()
    except OSError:
        return False
    try:
        target = os.path.join(d, "t")
        open(target, "w").close()
        os.symlink(target, os.path.join(d, "l"))
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _server_can_symlink(socket: str, env: dict) -> bool | None:
    """Whether work delegated through the server (`session_user.run_as_session_user` is a `run-shell`)
    may create a symlink: True, False on winerror 1314, None when the probe did not answer."""
    command = (f"{session_user._shq(session_user._shell_path(sys.executable))} -I -c "
               f"{session_user._shq(_SYMLINK_PROBE)}")
    try:
        proc = _tmux(socket, "run-shell", command, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (proc.stdout or "").split()
    if "symlink:ok" in lines:
        return True
    return False if "symlink:1314" in lines else None


def _delegated_job_in_flight() -> bool:
    """True when a `session_user.run_as_session_user` job may still be running: a `req/<id>.sh` whose
    `res/<id>.rc` is not there yet (the daemon unlinks both once it has the result). An unreadable spool
    counts as in flight."""
    spool = session_user.exec_spool()
    try:
        scripts = [name[:-len(".sh")] for name in os.listdir(os.path.join(spool, "req")) if name.endswith(".sh")]
        done = set(os.listdir(os.path.join(spool, "res")))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return any(f"{request_id}.rc" not in done for request_id in scripts)


def _windows_server_orphaned(socket: str, env: dict) -> bool:
    """True when a server listens on SOCKET that this loop did not start (`_ensure_foreground_server`): one
    left from before `jaah-runner` last restarted. The daemon can vouch for it only if it verified it before
    that restart (`host_platform.windows.fleet_server_anchor`); a node whose first runner came later refused
    every runner as `not_a_runner` (seen on a Windows node)."""
    child = _SERVER[0]
    if child is not None and child.poll() is None:
        return False
    return _server_listening(socket, env)


def _windows_server_stale(socket: str, env: dict) -> str | None:
    """Why this loop's tmux server should be replaced, or None: not this loop's child, or its token lacks the
    symlink right this loop's has. The symlink probe runs until it passes once (`_windows_server_fresh`) or
    stays silent `_WINDOWS_PROBE_SILENCE_CAP` times running."""
    global _windows_server_fresh, _windows_probe_silences
    if _windows_server_orphaned(socket, env):
        return "it is not this loop's child, so the daemon may be unable to tie its panes to jaah-runner"
    if (_windows_server_fresh or _windows_probe_silences >= _WINDOWS_PROBE_SILENCE_CAP
            or not _can_symlink_here()):
        return None
    verdict = _server_can_symlink(socket, env)
    _windows_probe_silences = _windows_probe_silences + 1 if verdict is None else 0
    if verdict is not False:
        _windows_server_fresh = verdict is True
        return None
    return "its token lacked the symlink right this unit has"


def _recycle_stale_windows_server(session: str, env: dict | None) -> bool:
    """Windows: kill this loop's tmux server when it is stale (`_windows_server_stale`) and holds nothing but
    the holder. Returns True when it killed it; the next `seed` starts a fresh one, as this loop's child.

    `KillMode=process` keeps the server across a restart of `jaah-runner`, so after one the server is no
    longer under the service (the daemon's only proof its panes are the runner account's), and its token is
    the one from before: jaah-update grants `jaah` SeCreateSymbolicLinkPrivilege and restarts the service, yet
    NodeLink and credential links (`session_user.symlink_as_session_user`) failed with winerror 1314 until a
    reboot. "Nothing but the holder": no window but the holder and no attached client; the server's children
    are not readable here (`host_platform.child_pids` is None on Windows), so a `run-shell -b` job in flight
    is read from the exec spool instead (`_delegated_job_in_flight`) — killing it would park its waiter until
    the timeout. A busy server is retried next tick; anything unreadable is a no-op."""
    global _windows_server_fresh
    env = dict(os.environ if env is None else env)
    socket = own_socket(env)
    try:
        reason = _windows_server_stale(socket, env)
    except (OSError, subprocess.SubprocessError):
        return False
    if reason is None:
        return False
    if _delegated_job_in_flight():
        return False
    try:
        shown = _tmux(socket, "display-message", "-p", "-t", f"={session}", "#{pid}", env=env)
        panes = _tmux(socket, "list-panes", "-a", "-F", "#{window_name}", env=env)
        clients = _tmux(socket, "list-clients", env=env)
        if panes.returncode != 0 or clients.returncode != 0 or (clients.stdout or "").strip():
            return False
        if any(window != HOLDER_WINDOW for window in (panes.stdout or "").splitlines()):
            return False
        killed = _tmux(socket, "kill-server", env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    if killed.returncode != 0:
        return False
    # The next server is this loop's child, with this loop's token.
    _windows_server_fresh = True
    print(f"fleet_tmux_server: recycled the tmux server (pid {(shown.stdout or '').strip() or '?'}) on "
          f"{socket}: {reason}, and it held no session")
    return True


def converge_forever(session: str, interval: float = CONVERGE_INTERVAL_S) -> int:
    """`seed` once loudly, then re-converge every INTERVAL seconds, forever — the `--loop` ExecStart.

    The first seed's verdict is the unit's start verdict; after that a failed pass never exits the loop
    (`Restart=always` backstops a dead process). The readability pass rides the same tick — the only
    place it can succeed (`_converge_shared_readability`)."""
    rc = seed(session)
    if rc != 0:
        return rc
    _converge_shared_readability()
    while True:
        time.sleep(interval)
        try:
            recycled = recycle_stale_server(session)
            seed(session, announce=not recycled)
            _converge_shared_readability()
        except Exception as exc:  # noqa: BLE001 — the loop IS the recovery mechanism; log and re-tick
            print(f"fleet_tmux_server: converge pass failed ({exc}); retrying in {interval}s",
                  file=sys.stderr)


# Line-buffer stdout before anything prints: under systemd stdout is a pipe (block-buffered), the loop
# never fills it, and a SIGTERM discards the buffer — the journal gets none of this module's stdout, and
# the re-seed after a substrate death is recorded nowhere else. In code rather than `PYTHONUNBUFFERED=1`
# so hand runs and tests behave the same and the pinned unit file is untouched.
def _line_buffer_stdout() -> None:
    """Make stdout line-buffered so a resident loop's output reaches the journal as it happens.

    Guarded: a replaced `sys.stdout` (`StringIO`) has no `reconfigure`."""
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — a substrate keeper never dies over its own logging setup
        pass


def main(argv: list[str] | None = None) -> int:
    _line_buffer_stdout()
    args = list(sys.argv[1:] if argv is None else argv)
    loop = "--loop" in args
    if loop:
        args.remove("--loop")
    if len(args) != 1 or not args[0].strip():
        print("fleet_tmux_server: usage: fleet_tmux_server.py [--loop] <session-name>   "
              "(jaah-runner@<session>.service passes `--loop %i`; bare = one-shot converge)",
              file=sys.stderr)
        return 2
    return converge_forever(args[0].strip()) if loop else seed(args[0].strip())


if __name__ == "__main__":
    sys.exit(main())
