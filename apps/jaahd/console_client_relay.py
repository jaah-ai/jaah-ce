"""The console viewer's tmux client, run as the SESSION USER, not as this daemon (POSIX).

A tmux server can make any attached client exec a command: `detach-client -t <client> -E '<cmd>'` runs
`<cmd>` in that client's own process. Any process that can talk to a server may send it, and on a
NON-ISOLATED fleet server every runner pane can (it is the server's own uid). A viewer client spawned by the
daemon would run that command as the daemon: its node token, credential socket and NodeVault keys.

So `console._spawn_console_pty` runs THIS script in its PTY child instead of `tmux attach` whenever the target
server is one the session user can reach (`console._client_relay_argv`). The script stays the daemon, but
only as a byte pump: it opens a second pty, hands its slave to the session user's group, and asks the fleet
server (`run-shell -b`) to start the attach client on it in a new session. That client is the server's own
child, so a `detach-client -E` sent to it runs as the server's uid, never the daemon's.

Everything the console builds on stays as it was: the pid it waits for is this process, the fd it closes is
this process's pty, and a resize of it reaches the client (SIGWINCH is forwarded as a winsize on the second
pty, whose foreground group the client is). DETACH, NEVER KILL: closing the console's fd ends this pump, which
closes the second pty's master; the client sees the hangup and detaches. Nothing here signals anything.

The slave is readable and writable by the session user's group: a runner may open it. On a non-isolated server
a runner already owns every client and pane outright (switch-client, send-keys, pipe-pane), so that grants
nothing new; an attach on the isolated server is never relayed. Stdlib only, run with `python -I`.

Invoked as `python -I console_client_relay.py <json config>`; keys:
  sockets       the sockets to try in order (the first that holds `target` is used)
  relay_socket  the session user's own server: an attach there is relayed, any other runs here directly
  target        the tmux target to attach
  read_only     add tmux's `-r`
  group         the gid the second pty's slave is handed to
  new_session   shell words that run the rest of the line in a new session (`session_user.new_session_cmd`)
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import termios
import time
import tty

# The client draws on attach. Until its first byte the pump keeps its own slave fd open, so a client still
# starting is not mistaken for one that left; past this deadline it lets go, and a client that never came
# (the job did not run) then reads as EIO.
_FIRST_OUTPUT_DEADLINE_S = 15.0
_CHUNK = 65536
_TERM = "xterm-256color"


def tmux_head(socket: str) -> list[str]:
    """`session_user.tmux_cmd(socket)`'s spelling (`-u`: see there)."""
    return ["tmux", "-u", "-S", socket] if socket else ["tmux", "-u"]


def attach_argv(socket: str, target: str, read_only: bool) -> list[str]:
    return [*tmux_head(socket), "attach", *(["-r"] if read_only else []), "-t", target]


def holding_socket(sockets: list[str], target: str) -> str:
    """The first socket whose server resolves `target`; the last one when none answers, so its attach reports
    the error as the console's `||` chain always did."""
    for socket in sockets[:-1]:
        try:
            probe = subprocess.run([*tmux_head(socket), "display-message", "-p", "-t", target, ""],
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return socket
    return sockets[-1]


def client_job(new_session: str, pts: str, attach: list[str]) -> str:
    """The `run-shell` job: in a NEW session, open `pts` (so it becomes the client's controlling tty and a
    winsize change signals it), drop `$TMUX` (attach refuses inside a pane), and exec the client. The job's own
    output goes nowhere and it exits 0: a `run-shell` job that prints is shown in the server's current pane."""
    inner = 'exec 0<>"$1" 1>&0 2>&0; shift; unset TMUX; export TERM=' + _TERM + '; exec "$@"'
    line = shlex.join(["/bin/sh", "-c", inner, "sh", pts, *attach])
    # `run-shell` expands formats in its command: `##` keeps any `#` literal, so no value here becomes `#(...)`.
    return f"{{ {new_session} {line}; }} </dev/null >/dev/null 2>&1; exit 0".replace("#", "##")


def _copy_winsize(src: int, dst: int) -> None:
    try:
        fcntl.ioctl(dst, termios.TIOCSWINSZ, fcntl.ioctl(src, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        pass


def _write_all(fd: int, data: bytes) -> bool:
    while data:
        try:
            data = data[os.write(fd, data):]
        except InterruptedError:
            continue
        except OSError:
            return False
    return True


def pump(master: int, slave: int | None) -> None:
    """Copy stdin → `master` and `master` → stdout until either side ends; forward SIGWINCH."""
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    _copy_winsize(0, master)
    deadline = time.monotonic() + _FIRST_OUTPUT_DEADLINE_S
    seen_output = False
    try:
        while True:
            timeout = None if slave is None else max(0.0, deadline - time.monotonic())
            try:
                ready, _, _ = select.select([0, master, wake_r], [], [], timeout)
            except InterruptedError:
                continue
            if slave is not None and not ready:
                os.close(slave)
                slave = None
                continue
            if wake_r in ready:
                os.read(wake_r, 512)
                _copy_winsize(0, master)
            if master in ready:
                try:
                    data = os.read(master, _CHUNK)
                except OSError as exc:   # EIO: every slave fd closed — the client is gone
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    if not seen_output:
                        _write_all(1, b"the console client did not start on the session user's tmux server\r\n")
                    return
                seen_output = True
                if not _write_all(1, data):
                    return
                if slave is not None:
                    os.close(slave)
                    slave = None
            if 0 in ready:
                try:
                    data = os.read(0, _CHUNK)
                except OSError:
                    data = b""
                if not data or not _write_all(master, data):
                    return
    finally:
        if slave is not None:
            os.close(slave)


def relay(config: dict) -> int:
    sockets = list(config["sockets"])
    target, read_only = config["target"], bool(config.get("read_only"))
    socket = holding_socket(sockets, target)
    attach = attach_argv(socket, target, read_only)
    if socket != config["relay_socket"]:
        # Not the session user's server (the daemon's own pre-switch one): the client is no more privileged
        # than that server, so it attaches from here, as it always did.
        env = dict(os.environ, TERM=_TERM)
        env.pop("TMUX", None)
        os.execvpe(attach[0], attach, env)
    master, slave = os.openpty()
    os.set_inheritable(master, False)
    os.set_inheritable(slave, False)
    # Raw from the start: a cooked pty would echo a keystroke typed before the client opens it, and that echo
    # would read as the client's first output. The client sets its own mode once it attaches.
    tty.setraw(slave)
    os.fchown(slave, -1, int(config["group"]))
    os.fchmod(slave, 0o660)
    _copy_winsize(0, slave)
    job = client_job(config["new_session"], os.ttyname(slave), attach)
    started = subprocess.run([*tmux_head(socket), "run-shell", "-b", job],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    refusal = (started.stderr or started.stdout or "").strip()
    if started.returncode != 0 or refusal:
        os.write(1, f"cannot start the console client on {socket}: {refusal or started.returncode}\r\n".encode())
        return 1
    try:
        tty.setraw(0)
    except termios.error:
        pass
    pump(master, slave)
    os.close(master)   # the client's hangup: it detaches, the session stays
    return 0


if __name__ == "__main__":
    sys.exit(relay(json.loads(sys.argv[1])))
