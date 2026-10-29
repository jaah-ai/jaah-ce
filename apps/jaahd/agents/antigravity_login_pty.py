"""agents/antigravity_login_pty.py — the terminal `agy`'s sign-in needs, driven over the daemon's FIFO + log.

Run as a SCRIPT (`python3 <this file>`), never imported by the daemon: it is the login argv
`AntigravityLoginAdapter.login_argv` returns, spawned on either leg of `accounts._spawn_login_*` with stdin
the code FIFO and stdout the login log. Stdlib only (the session user's `/usr/bin/python3`).

Everything here was measured against agy 1.2.11 (linux amd64), in scratch HOMEs:
  1. With no TTY the CLI prints NOTHING and waits for ever; `-p` does print a URL but gives up 60s later
     ("Waiting for authentication (timeout 60s)"), too short for a console round trip. So the full TUI
     sign-in is used, on a pty this driver owns — the role `script -qfec` would play.
  2. The pty needs a window size (a 0x0 pty renders nothing) and a WIDE one: the authorize URL is
     ~700 chars and is wrapped at the terminal width, which splits it for the scraper.
  3. The TUI opens on a "Select login method" menu whose default is Google OAuth; it needs one Enter
     (CR — the TUI puts the pty in raw mode, so the daemon's LF would be Ctrl-J, not Enter). An Enter
     written before the menu is drawn is swallowed, so it is sent after the menu text is seen.
  4. The TUI DRAWS the typed code into its input box, character by character between cursor escapes, so
     the pty's own `-echo` does not help. Once the code prompt is on screen NOTHING the CLI prints reaches
     the log again; only this module's fixed status lines do (`STATUS_*`).
  5. The daemon's in-process leg hands stdin over with O_NONBLOCK set; stdin is re-opened blocking.
A helper process forked alongside the CLI would be reaped as `script`'s own child and stall it, which is
one more reason this is a single process with no shell.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows: the agy login driver runs on Linux/macOS only
    fcntl = None
import os
import re
import select
import struct
import sys
try:
    import termios
except ImportError:  # Windows: the agy login driver runs on Linux/macOS only
    termios = None
import time

# The binary this driver runs, resolved on the login's PATH (`session_user.delegated_path()`).
BINARY = "agy"
# Rows x cols of the pty. 4000 columns keeps the measured ~700-char URL on one line with room for growth.
ROWS, COLS = 50, 4000

# Screen text, matched on escape-stripped output (measured, agy 1.2.11).
_MENU = "select login method"
_CODE_PROMPT = "paste the authorization code"
# A rejected code: the token exchange's `invalid_grant` surfaces as this wording.
_REJECTED = ("invalid_grant", "malformed auth code", "authentication failed")

# The ONLY lines this driver writes to the log after the code prompt appears. Fixed text, never anything
# read from the CLI. `AntigravityLoginAdapter.login_rejected_in_log` matches `STATUS_REJECTED`.
STATUS_AWAITING = "agy-login: waiting for the authorization code (CLI output is withheld from here on)"
STATUS_CODE_SENT = "agy-login: authorization code submitted"
STATUS_REJECTED = "agy-login: the authorization code was rejected"

# How much raw output is kept to look for the markers above; bounded so a chatty CLI cannot grow it. Wide
# enough for one full redraw of the measured TUI, and markers are checked on the whole tail each read.
_SCAN_TAIL = 64 * 1024
# Delay between seeing the menu and pressing Enter (an Enter sent too early is swallowed).
_MENU_ENTER_DELAY = 1.0
_ESCAPES = re.compile(rb"\x1b\[[0-9;?<>=$ ]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[_P^][^\x1b]*\x1b\\|\x1b.")


def _strip(raw: bytes) -> str:
    return _ESCAPES.sub(b"", raw).decode("utf-8", "replace").lower()


def _blocking_stdin() -> int:
    """A blocking descriptor on the same FIFO (point 5). `/proc/self/fd/0` re-opens it, O_RDWR so a read
    blocks rather than seeing EOF when the daemon's writer closes."""
    try:
        return os.open("/proc/self/fd/0", os.O_RDWR)
    except OSError:
        flags = fcntl.fcntl(0, fcntl.F_GETFL)
        fcntl.fcntl(0, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        return 0


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def _say(line: str) -> None:
    _write_all(1, (line + "\n").encode())


def _exit_code(status: int) -> int:
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code   # a signalled CLI exits as the shell would report it


def main(argv: list[str]) -> int:
    import pty
    import signal

    stdin = _blocking_stdin()
    pid, master = pty.fork()
    if pid == 0:  # the child: the pty is already its stdin/stdout/stderr and controlling terminal
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        try:
            os.execvp(BINARY, [BINARY, *argv])
        except OSError as exc:
            os.write(2, f"{BINARY}: not found or not executable ({exc.strerror})\n".encode())
        os._exit(127)

    def _stop(signum, _frame):
        # The CLI has a session of its own (the pty), so the machine's `killpg` reaches only this driver:
        # pass the stop on, so the CLI is gone before the machine's finalize runs.
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)

    relaying = True           # False once a code can be on screen: nothing from the CLI is logged after
    menu_seen_at: float | None = None
    enter_sent = False
    rejected_reported = False
    raw_tail = b""            # stripped as a WHOLE, so an escape split across two reads cannot hide a marker
    pending = b""             # a partial code line (no newline yet)
    watch = [master, stdin]

    def _withhold() -> None:
        nonlocal relaying, raw_tail
        relaying = False
        raw_tail = b""
        _say("")
        _say(STATUS_AWAITING)

    while True:
        timeout = None
        if menu_seen_at is not None and not enter_sent:
            timeout = max(0.0, menu_seen_at + _MENU_ENTER_DELAY - time.monotonic())
        ready, _, _ = select.select(watch, [], [], timeout)
        if menu_seen_at is not None and not enter_sent and time.monotonic() >= menu_seen_at + _MENU_ENTER_DELAY:
            _write_all(master, b"\r")
            enter_sent = True
        if master in ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:   # EIO: the CLI closed the pty (it exited)
                chunk = b""
            if not chunk:
                break
            raw_tail = (raw_tail + chunk)[-_SCAN_TAIL:]
            scan = _strip(raw_tail)
            if relaying:
                # Everything up to and including the prompt is public (banner, menu, the URL).
                _write_all(1, chunk)
                if _CODE_PROMPT in scan:
                    _withhold()
                    scan = ""
            elif not rejected_reported and any(word in scan for word in _REJECTED):
                _say(STATUS_REJECTED)
                rejected_reported = True
            if menu_seen_at is None and _MENU in scan:
                menu_seen_at = time.monotonic()
        if stdin in ready:
            data = os.read(stdin, 4096)
            if not data:      # not the daemon's FIFO (it never EOFs): stop watching rather than spin
                watch = [master]
                continue
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if line:
                    # FAIL CLOSED: a code is about to be typed, so whatever the screen says (a prompt worded
                    # differently from `_CODE_PROMPT`), nothing the CLI draws from here on reaches the log.
                    if relaying:
                        _withhold()
                    # CR, not LF: the TUI runs the pty raw (point 3). The code itself is never logged.
                    _write_all(master, line + b"\r")
                    _say(STATUS_CODE_SENT)
                    rejected_reported = False
                    raw_tail = b""
    _, status = os.waitpid(pid, 0)
    return _exit_code(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
