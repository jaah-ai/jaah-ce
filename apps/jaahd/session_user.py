"""Who fleet sessions run as, and how this process reaches them — the single home for the uid split.

The daemon runs as `jaahd`; runner and console sessions as `jaah`. The switch is a tmux
SERVER owned by `jaah` (`jaah-runner@.service`): a pane is forked by the server, so every window runs
as `jaah` while the daemon is a plain client. Every call names the socket with `-S` — only `-S` binds;
`TMUX_TMPDIR` is ignored inside a session. The daemon's own socket must stay 0700: group `jaah` on it
would hand a runner a pane as `jaahd`. During an in-place upgrade's drain, read/manage ops also cover
the daemon's own server (`manage_sockets`); creation targets only the fleet socket.

Socket mode is not tmux's whole access control: tmux ≥3.3 keeps a per-server ACL whose refusal prints
`access not allowed` and EXITS 0 (root is exempt, so a root-shell probe proves nothing). Admitting the
daemon needs both `share_own_socket` and a `server-access -aw` grant (`fleet_tmux_server.seed`).

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
import stat
import subprocess
import sys
import time
from typing import Callable

try:
    import pwd
except ImportError:  # Windows: there is no passwd database (the Windows branches never reach it)
    pwd = None  # type: ignore[assignment]

import environment
import host_platform
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


def fleet_socket(env: dict | None = None) -> str:
    """The socket of the tmux server that owns fleet sessions — the SESSION USER's, not this process's.

    "" when there is no session user: callers keep tmux's default-label resolution, since the daemon's
    own server is then the fleet server. On Windows `<TMUX_TMPDIR>\\<session user>\\default`: there is no
    uid to name tmux's `tmux-<uid>` level after, so the directory is the account's name."""
    if _on_windows():
        name = session_user()
        return ntpath.join(tmux_tmpdir(env), name, "default") if name else ""
    uid = session_uid()
    if uid is None:
        return ""
    return os.path.join(socket_dir_for(uid, env), "default")


def socket_args(env: dict | None = None) -> list[str]:
    """`["-S", <fleet socket>]`, or `[]` when there is no session user.

    Always `-S`, never a label plus `TMUX_TMPDIR`: tmux consults `TMUX_TMPDIR` only with no `-S` and no
    `$TMUX`, so inside a session a command lands on the caller's own server. The default label also
    resolves per-euid, so two uids would silently use two servers."""
    socket = fleet_socket(env)
    return ["-S", socket] if socket else []


def fleet_socket_ready(env: dict | None = None) -> bool:
    """Whether the fleet server's socket exists. A caller creating a window must refuse without it: a
    client with no server starts one itself, as the caller's uid — and reports success."""
    socket = fleet_socket(env)
    return bool(socket) and os.path.exists(socket)


def tmux_cmd(socket: str, *args: str) -> list[str]:
    """`["tmux", "-u", "-S", socket, *args]`, or `["tmux", "-u", *args]` when SOCKET is "" (no session user,
    so tmux's own default label is correct). The one place the `-S` splice is spelled.

    `-u` on every platform: a daemon started with no UTF-8 locale (a Windows service, a systemd unit on a
    minimal image, the local dev-node container) is a non-UTF-8 tmux client, and tmux rewrites every TAB in
    `-F` output to `_` — each tab-split pane listing parsed empty, a kill found no window to tear down (a real
    PC) and the console never attached to a live window."""
    return ["tmux", "-u", "-S", socket, *args] if socket else ["tmux", "-u", *args]


def manage_sockets(env: dict | None = None) -> list[str]:
    """Sockets a read/manage op must cover: the fleet socket, then this process's own when it exists and
    differs (the upgrade drain). `[""]` when there is no session user, meaning "tmux's default". Only
    creation is single-socket, or an upgrade would strand pre-switch runners as invisible orphans."""
    fleet = fleet_socket(env)
    if not fleet:
        # This process's own server IS the fleet server; a second spelling would be a second name.
        return [""]
    if _on_windows():
        # No pre-switch daemon-owned server to drain: Windows nodes started on the split.
        return [fleet]
    sockets = [fleet]
    own = os.path.join(socket_dir_for(os.geteuid(), env), "default")
    if own != fleet and os.path.exists(own):
        sockets.append(own)
    return sockets


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


def _spool_paths(request_id: str) -> tuple[str, str, str, str]:
    req = os.path.join(exec_spool(), "req", f"{request_id}.sh")
    res_dir = os.path.join(exec_spool(), "res")
    return req, os.path.join(res_dir, f"{request_id}.out"), os.path.join(res_dir, f"{request_id}.err"), \
        os.path.join(res_dir, f"{request_id}.rc")


def _handshake_paths(request_id: str) -> tuple[str, str]:
    """`(pid file, ack file)` for the identity handshake. The pid travels through `res/` (only the
    command knows it); the ack through `req/`, which the session user cannot write — a plantable ack
    would let a command run git before the daemon admitted it."""
    return (
        os.path.join(exec_spool(), "res", f"{request_id}.pid"),
        os.path.join(exec_spool(), "req", f"{request_id}.ack"),
    )


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


def run_as_session_user(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    on_child_pid: Callable[[int], bool] | None = None,
    input: str | None = None,  # noqa: A002 — `subprocess.run`'s own name for it
) -> subprocess.CompletedProcess:
    """Run ARGV as the session user and return a `CompletedProcess` (text mode), the same shape callers
    already handle from `subprocess.run`.

    ENV is the command's whole environment, exported by the generated script: a tmux server hands
    `run-shell` its own frozen environment. `HOME` defaults to the session user's, `PATH` to
    `delegated_path()`. It never locates the fleet socket — that is this process's `TMUX_TMPDIR`.

    `on_child_pid` is the identity handshake: delegation sheds the daemon's identity (no runner pane
    ancestry, so `cred_broker` refuses it). The script reports its own pid and waits; the daemon hands
    the pid to the callback (`clone_identity.delegated_op` registers it) and only then writes the ack —
    so git cannot reach its credential helper before the registration. A `False` still releases the command, which
    runs without a credential rather than hanging.

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
    command_env = dict(env or {})
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
    pid_path, ack_path = _handshake_paths(request_id)
    # A `run-shell` job that prints anything, or exits non-zero, is shown in view mode in the server's current pane —
    # a runner's. So the job's own output goes nowhere, every path reports through the rc file, and the
    # job itself always exits 0.
    body = "#!/bin/sh\nexec >/dev/null 2>&1\n" + exports
    # The handshake goes first, before anything that could ask for a credential; `.tmp`+rename like `rc`.
    # The wait is bounded by a count, so a daemon that died mid-handshake costs 2s, never a parked shell.
    # Every path the shell reads goes through `_shell_path` (identity off Windows).
    sh_pid, sh_ack, sh_out, sh_err, sh_rc = (
        _shell_path(p) for p in (pid_path, ack_path, out_path, err_path, rc_path))
    if on_child_pid is not None:
        body += (
            f"printf '%s' \"$$\" >{_shq(sh_pid + '.tmp')}\n"
            f"mv {_shq(sh_pid + '.tmp')} {_shq(sh_pid)}\n"
            f"__n=0\n"
            f"while [ ! -e {_shq(sh_ack)} ] && [ $__n -lt {_HANDSHAKE_WAIT_TICKS} ]; do\n"
            f"  __n=$((__n+1)); sleep 0.05\n"
            f"done\n"
        )
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

    try:
        # `-b`: the result file is the completion signal. No `-t`: it would tie the request to a session.
        spawn = subprocess.run(
            [tmux, *socket_args(), "run-shell", "-b", f"/bin/sh {_shq(_shell_path(script_path))}"],
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
        if on_child_pid is not None:
            _admit_delegated_child(pid_path, ack_path, on_child_pid, deadline)
        while not os.path.exists(rc_path):
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            time.sleep(_POLL_INTERVAL_S)
        return subprocess.CompletedProcess(
            argv, int(_read_result(rc_path) or "111"),
            _read_result(out_path), _read_result(err_path))
    finally:
        # On timeout the still-running command can recreate one `<id>.rc` after this sweep. Accepted:
        # one byte on tmpfs.
        for path in (script_path, in_path, out_path, err_path, rc_path, rc_path + ".tmp",
                     pid_path, pid_path + ".tmp", ack_path):
            try:
                os.unlink(path)
            except OSError:
                pass


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
    as_session_user: bool = False,
    on_child_pid: Callable[[int], bool] | None = None,
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
    `failure_prefix` names the op. Never retried in-process: that uid is the one the delegation keeps
    off the files."""
    # The optional kwargs ride only when given: the kwarg set is a contract the callers' test fakes spell
    # out, and the defaults are the runners' own.
    extra: dict = {}
    if input is not None:
        extra["input"] = input
    if on_child_pid is not None:
        extra["on_child_pid"] = on_child_pid
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


#: How long the delegated command waits to be admitted before running unauthenticated — 40 × 0.05s = 2s,
#: generous against the 25ms poll and far short of any git timeout.
_HANDSHAKE_WAIT_TICKS = 40


def _admit_delegated_child(
    pid_path: str, ack_path: str, on_child_pid: Callable[[int], bool], deadline: float,
) -> None:
    """Read the delegated shell's own pid, offer it to `on_child_pid`, then release the command.

    The ack is written on every path: this handshake must never stop a command from running (without
    admission it runs with no credential, a git failure callers handle). O_EXCL, so a planted ack is a
    loud failure rather than mistaken for ours. Bounded by the caller's `deadline`."""
    limit = min(deadline, time.monotonic() + _HANDSHAKE_WAIT_TICKS * 0.05)
    reported = ""
    while time.monotonic() < limit:
        reported = _read_result(pid_path)
        if reported.strip():
            break
        time.sleep(_POLL_INTERVAL_S)
    try:
        if reported.strip().isdigit():
            if not on_child_pid(_os_pid(int(reported.strip()))):
                log.warning(
                    "session-user delegation: the command reported pid %s but it was not admitted as "
                    "this daemon's own git op — it will run without a credential",
                    reported.strip(),
                )
        else:
            log.warning(
                "session-user delegation: no usable pid was reported for %s, so the command runs "
                "without a credential (git ops needing one will fail to authenticate)", pid_path,
            )
    finally:
        try:
            os.close(os.open(ack_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_binary(), 0o640))
        except OSError as exc:
            log.warning("session-user delegation: could not write the ack at %s: %s", ack_path, exc)


def _os_pid(shell_pid: int) -> int:
    """The kernel pid behind the delegated shell's `$$`: itself off Windows; on Windows `$$` is an MSYS
    pid, and only the Windows pid may be registered or walked (`host_platform.pane_os_pid`). 0 — never a
    live process, so `on_child_pid` refuses it — when the mapping has no answer."""
    if not _on_windows():
        return shell_pid
    return host_platform.pane_os_pid(shell_pid) or 0


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
    if switch_needed():
        if not can_run_as_session_user():
            return ("session-user switch required but unavailable — refusing to chmod "
                    f"{path} as the daemon uid; check jaah-runner@<node name>.service")
        try:
            # `--` BEFORE the mode: BSD chmod (macOS) stops reading options at the mode, so a later `--` is a path.
            proc = run_as_session_user(["chmod", "--", format(mode, "o"), _shell_path(path)], timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired, OSError
            return f"session-user delegation failed for chmod {path}: {exc}"
        return "" if proc.returncode == 0 else (proc.stderr or f"chmod {path} rc {proc.returncode}").strip()
    try:
        os.chmod(path, mode)
    except OSError as exc:
        return str(exc)
    return ""


def unlink_as_session_user(path: str, timeout: float = 30) -> str:
    """`rm -f -- PATH` as the session user where the uid split applies, else `os.unlink` in-process.

    The delete-side counterpart of the ownership contract: the parent is setgid and group-writable, so
    the daemon could unlink, but one uid creates and the same uid deletes, so ownership stays auditable.
    `rm -f`: an already-gone file is not a failure (racing sweeps, a manual clear). `--` before the path.
    No glob, one path per call — the argv is fixed options plus one path, so nothing can widen. Returns
    "" or an error string, never raising; the caller decides what a failed delete means."""
    if switch_needed():
        if not can_run_as_session_user():
            # Fail, never fall back in-process: that would delete as the daemon uid.
            return ("session-user switch required but unavailable — refusing to delete "
                    f"{path} as the daemon uid; check jaah-runner@<node name>.service")
        try:
            proc = run_as_session_user(["rm", "-f", "--", _shell_path(path)], timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired, OSError
            return f"session-user delegation failed for rm {path}: {exc}"
        return "" if proc.returncode == 0 else (proc.stderr or f"rm {path} rc {proc.returncode}").strip()
    try:
        os.unlink(path)
    except FileNotFoundError:
        # `rm -f` semantics on this arm too.
        return ""
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
    """Replace PATH with CONTENT atomically, as the session user where the uid split applies, else in-process:
    `write_file_as_session_user` to a sibling temp file, then `rename(2)` over PATH, so a reader sees the old
    file or the new one and never a half-written one. Returns "" or an error string (the temp file is removed
    on a failed rename)."""
    staged = f"{path}.tmp-{secrets.token_hex(6)}"
    err = write_file_as_session_user(staged, content, mode=mode, timeout=timeout)
    if err:
        return err
    if switch_needed():
        try:
            proc = run_as_session_user(
                ["/bin/sh", "-c", mv_over_script() + ' || { rm -f -- "$SU_SRC"; exit 1; }'],
                env={"SU_SRC": _shell_path(staged), "SU_DST": _shell_path(path)}, timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch raced away), TimeoutExpired, OSError
            return f"session-user delegation failed for replace {path}: {exc}"
        return "" if proc.returncode == 0 else (proc.stderr or f"replace {path} rc {proc.returncode}").strip()
    try:
        os.replace(staged, path)
    except OSError as exc:
        try:
            os.unlink(staged)
        except OSError:
            pass
        return str(exc)
    return ""


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
