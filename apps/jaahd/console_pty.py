"""The Windows console bridge: one tmux attach client behind a RELAY, streamed to the browser.

WHY A RELAY, NOT A CONPTY (measured on a real PC). Git's MSYS runtime passes no file descriptors
over a unix socket (the server logs `IDENTIFY_STDIN -1`), so tmux's server reopens its client's terminal BY
NAME. A client on a ConPTY has the console `/dev/cons0` as that name, which no other process can open, so the
server drops it at once ("server exited unexpectedly") — whichever account attaches. A client on an MSYS pty
(`/dev/ptyN`) is reopened fine; the G1 probe only ever attached from mintty. So the attach client runs INSIDE
a pane of the fleet server itself (`TmuxRelay`): a hidden `_view_rly…` session whose one pane execs
`tmux attach` on its own pty. `pipe-pane -O` appends everything that client draws to a file the daemon tails,
`send-keys -H` types the browser's bytes into it, and `resize-window` sizes it. The bytes on the websocket are
the same raw terminal stream as on Linux.

THE OUTPUT FILE lives in `console` under the daemon-only state directory, created by the daemon with a DACL that
lets the runner account append to that one file and nothing else (`windows.create_runner_writable_file`): the
runner cannot replace it with a link to something the daemon would then stream. MSYS opens no Windows named
pipe (`//./pipe/…` is ENOENT to it), which is why this is a file.

The Linux bridge (`console.py`) owns a pty master fd and drives it from the event loop with `add_reader`/
`add_writer`. Windows has no pollable fd, so `ConPtyAttach` wraps the relay with two daemon threads instead:
a READER that blocks on `read` and hands each chunk to the loop with `call_soon_threadsafe`, and a WRITER
that drains a bounded queue of keystrokes and resizes, so neither direction ever parks the event loop.
`TmuxRelay` is shaped like pywinpty's `PtyProcess`, which the wrapper was first built around: `pid`, a
blocking `read` → `str` (never splitting a code point; `EOFError` at the end), `write(str)`, `setwinsize`,
`isalive`, `close(force)`.

DETACH, NEVER KILL: what `close` ends is the tmux ATTACH CLIENT, nothing else — exactly what
closing the master fd is on Linux. The session, its windows and the runner inside keep running on the tmux
server, which this never addresses.
"""
from __future__ import annotations

import codecs
import itertools
import logging
import ntpath
import os
import queue
import secrets
import shlex
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable

log = logging.getLogger("jaahd.console")

# One read's ceiling.
_READ_SIZE = 65536
# Bounds `close()`'s wait for the writer thread, so a wedged write cannot hold teardown.
_WRITER_JOIN_SECONDS = 1.0
_STOP = object()


class _Resize:
    __slots__ = ("rows", "cols")

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols


def _default_factory(argv: list[str], rows: int, cols: int, env: dict):
    return TmuxRelay.start(argv, rows, cols, env)


# The relay session's name prefix: under console.py's `_view_` prefix, so every pane-listing reader already
# skips it (`is_ephemeral_view_session`) and the start-up sweep reaps one a crash left. A live relay has no
# client of its own by design, so the sweep skips the ones this process is running (`is_live_relay`).
RELAY_SESSION_PREFIX = "_view_rly"
#: In a relay's argv, what follows this runs in the pane instead of a tmux attach (`TmuxRelay.start`).
RUN = "--jaah-relay-run"
_LIVE: set[str] = set()
_LIVE_LOCK = threading.Lock()
_PIDS = itertools.count(1 << 30)
# A relay's `read` polls its file this often while idle, and asks tmux whether the relay lives every
# `_LIVENESS_EVERY_S` of silence: its end is the session's end, which nothing else signals.
_POLL_S = 0.03
_LIVENESS_EVERY_S = 1.0
# Past this many bytes read, the output moves to a fresh file (`_rotate`), so an open console does not
# grow one without bound; the old one is drained until it has been quiet for `_ROTATE_QUIET_S`.
_ROTATE_BYTES = 8 * 1024 * 1024
_ROTATE_QUIET_S = 0.3
# Keystrokes per `send-keys -H` call: bounds one argv; a paste is several calls, in order.
_KEYS_PER_CALL = 1024
_TMUX_TIMEOUT_S = 10
_run = subprocess.run


def is_live_relay(name: str) -> bool:
    with _LIVE_LOCK:
        return name in _LIVE


def _relay_of(entry: str) -> str:
    """The relay a file in the output directory belongs to (`<relay>.out`, `<relay>.<n>.out`), or ""."""
    return entry.split(".", 1)[0] if entry.endswith(".out") and entry.startswith(RELAY_SESSION_PREFIX) else ""


def _remove(path: str) -> None:
    """Best-effort delete. The read-only attribute is cleared first: the runner may write attributes."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        os.remove(path)
    except OSError:
        pass


def _prune_stale(out_dir: str) -> None:
    """Remove the output files of relays this process is not running: a relay a crash left behind (its
    session the start-up sweep reaps) leaves its file here. Best-effort — a busy file is retried next time."""
    try:
        names = os.listdir(out_dir)
    except OSError:
        return
    for entry in names:
        relay = _relay_of(entry)
        if relay and not is_live_relay(relay):
            _remove(os.path.join(out_dir, entry))


def _msys(path: str) -> str:
    drive, rest = ntpath.splitdrive(path)
    rest = rest.replace("\\", "/")
    return f"/{drive[0].lower()}{rest}" if len(drive) == 2 and drive[1] == ":" else drive.replace("\\", "/") + rest


class TmuxRelay:
    """One attach client running in a relay pane of the fleet server, shaped like pywinpty's `PtyProcess`
    (see the module docstring). DETACH, NEVER KILL: `close` kills only the relay session, which ends the
    nested client — the session it was attached to, and the runner in it, are never addressed.

    Every method BLOCKS on tmux: the console calls them only off its event loop (spawn in an executor,
    `read` in the reader thread, `write`/`setwinsize` in the writer thread)."""

    def __init__(self, head: list[str], name: str, out_dir: str, env: dict) -> None:
        self._head, self.name, self._out_dir, self._env = head, name, out_dir, env
        self.pid = next(_PIDS)
        self._generation = 0
        self._out_path = ""
        self._file = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._closed = False

    @classmethod
    def start(cls, argv: list[str], rows: int, cols: int, env: dict, *, out_dir: str | None = None) -> TmuxRelay:
        """`argv` is the attach command (`session_user.tmux_cmd(sock, "attach", …)`): what precedes
        `attach` addresses the server, and the whole of it runs in the relay pane. Or `tmux_cmd(sock)` +
        `[RUN, *command]`: the pane runs `command` itself (a MicroVM console's relay, `console.py`). Raises
        `OSError` when tmux refuses a step, after undoing what it made, so the caller's spawn-failure teardown
        runs."""
        from host_platform import windows

        command = list(argv[argv.index(RUN) + 1:]) if RUN in argv else None
        at = argv.index(RUN) if command is not None else argv.index("attach")
        head, attach = list(argv[:at]), list(argv[at:])
        # Resolved on the CHILD's PATH, as pywinpty did: `subprocess` on Windows searches the parent's.
        exe = shutil.which(head[0], path=env.get("PATH")) or head[0]
        head[0] = exe
        socket = head[head.index("-S") + 1] if "-S" in head else ""
        nested = ["env", "-u", "TMUX", _msys(exe), *head[1:]]
        if socket:
            nested[nested.index(socket)] = _msys(socket)
        nested = ["env", "-u", "TMUX", *command] if command is not None else nested + attach
        name = f"{RELAY_SESSION_PREFIX}{secrets.token_hex(8)}"
        out_dir = out_dir or os.path.join(windows.paths().state_dir, "console")
        relay = cls(head, name, out_dir, env)
        # Live BEFORE its file exists, so a concurrent start's prune never takes it.
        with _LIVE_LOCK:
            _LIVE.add(name)
        try:
            os.makedirs(out_dir, exist_ok=True)
            _prune_stale(out_dir)
            relay._open_next()
            # The pane waits for one Enter before it execs the client, so the client's first full redraw
            # lands AFTER `pipe-pane` is in place. `status off`: the relay's own status line would take a row.
            relay._tmux("new-session", "-d", "-s", name, "-x", str(cols), "-y", str(rows), "-c", "/",
                        "read _; exec " + shlex.join(nested))
            relay._tmux("set-option", "-t", f"={name}:", "status", "off")
            relay._pipe_to(relay._out_path)
            relay._tmux("send-keys", "-t", f"={name}:", "Enter")
        except BaseException:
            relay.close()
            raise
        return relay

    def _open_next(self) -> None:
        """Create the next output file and open the daemon's read handle on it AT ONCE, before its name
        reaches tmux: a later reparse point cannot redirect a handle already open."""
        from host_platform import windows

        suffix = f".{self._generation}" if self._generation else ""
        path = os.path.join(self._out_dir, f"{self.name}{suffix}.out")
        windows.create_runner_writable_file(path)
        self._file, self._out_path = open(path, "rb"), path
        self._generation += 1

    def _pipe_to(self, path: str) -> None:
        # A second `pipe-pane` closes the pane's current pipe before it starts this one.
        self._tmux("pipe-pane", "-O", "-t", f"={self.name}:", "exec cat >> " + shlex.quote(_msys(path)))

    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = _run([*self._head, *args], capture_output=True, text=True, env=self._env,
                        timeout=_TMUX_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError(f"console relay {self.name}: tmux {args[0]} failed: {exc}") from exc
        if check and proc.returncode != 0:
            raise OSError(f"console relay {self.name}: tmux {args[0]} exited {proc.returncode}: "
                          f"{(proc.stderr or '').strip()}")
        return proc

    def isalive(self) -> bool:
        if self._closed:
            return False
        try:
            return self._tmux("has-session", "-t", f"={self.name}", check=False).returncode == 0
        except OSError:
            return False

    def _rotate(self) -> bytes:
        """Move the output to a fresh file and return what was still arriving in the old one. The old file
        is closed and removed whatever fails on the way (a failure is the relay's end: EOF follows)."""
        old, old_path = self._file, self._out_path
        tail: list[bytes] = []
        try:
            self._open_next()
            self._pipe_to(self._out_path)
            quiet_from = time.monotonic()
            while time.monotonic() - quiet_from < _ROTATE_QUIET_S and not self._closed:
                chunk = old.read(_READ_SIZE)
                if chunk:
                    tail.append(chunk)
                    quiet_from = time.monotonic()
                else:
                    time.sleep(_POLL_S)
            # Re-piping drops what tmux had queued for the old pipe and not yet handed to it — a torn escape
            # sequence at worst — so the nested client redraws its whole screen into the new one.
            tty = self._tmux("display-message", "-p", "-t", f"={self.name}:", "#{pane_tty}",
                             check=False).stdout.strip()
            if tty:
                self._tmux("refresh-client", "-t", tty, check=False)
        finally:
            old.close()
            _remove(old_path)
        return b"".join(tail)

    def read(self, size: int = 65536) -> str:
        """Block until the client draws something; `str`, never splitting a code point. EOFError once the
        relay session is gone (the client detached or the target ended) or the relay was closed."""
        quiet_since = time.monotonic()
        while not self._closed:
            data = self._file.read(size)
            if not data and self._file.tell() >= _ROTATE_BYTES:
                data = self._rotate()
            if data:
                text = self._decoder.decode(data)
                if text:
                    return text
                continue
            # The runner may write the file, so it may also truncate it: what is there now is newer than what
            # was read, so read it from the start rather than wait past an EOF that moved.
            if os.fstat(self._file.fileno()).st_size < self._file.tell():
                self._file.seek(0)
            if time.monotonic() - quiet_since >= _LIVENESS_EVERY_S:
                if not self.isalive():
                    break
                quiet_since = time.monotonic()
            time.sleep(_POLL_S)
        raise EOFError("console relay closed")

    def write(self, text: str) -> int:
        data = text.encode("utf-8")
        for start in range(0, len(data), _KEYS_PER_CALL):
            chunk = data[start:start + _KEYS_PER_CALL]
            try:
                self._tmux("send-keys", "-t", f"={self.name}:", "-H", *(format(b, "02x") for b in chunk))
            except OSError as exc:
                raise EOFError(str(exc)) from exc
        return len(text)

    def setwinsize(self, rows: int, cols: int) -> None:
        self._tmux("resize-window", "-t", f"={self.name}:", "-x", str(cols), "-y", str(rows), check=False)

    def close(self, force: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._tmux("kill-session", "-t", f"={self.name}", check=False)
        except OSError as exc:
            log.warning("console: relay %s not killed (%s) — the start-up sweep reaps it", self.name, exc)
        if self._file is not None:
            self._file.close()
        # Every generation, not only the current one: an earlier one a rotation could not delete stays here.
        try:
            leftovers = [entry for entry in os.listdir(self._out_dir) if _relay_of(entry) == self.name]
        except OSError:
            leftovers = []
        for entry in leftovers:
            _remove(os.path.join(self._out_dir, entry))
        with _LIVE_LOCK:
            _LIVE.discard(self.name)


class ConPtyAttach:
    """One tmux attach client behind its process object (`TmuxRelay`): the Windows stand-in for console.py's
    (pid, master_fd) pair.

    Every method is safe to call from the event loop: none blocks beyond a bounded wait, and none raises
    (a dead console reads as EOF, a write to it is dropped — the EOF path owns teardown, as on Linux). The
    process's own calls block, so input AND resizes go through the writer thread, never the loop."""

    def __init__(self, proc, *, max_pending: int) -> None:
        self._proc = proc
        self._max_pending = max_pending
        self._pending = 0
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        # Set by `close()` and read on the loop thread before delivering anything: once detached, a late
        # chunk or EOF from the reader must never reach a session that may already be a NEW one under the
        # same key (Linux gets this from `remove_reader`).
        self._closed = False
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None

    @classmethod
    def spawn(cls, argv: list[str], rows: int, cols: int, env: dict, *, max_pending: int,
              factory: Callable | None = None) -> ConPtyAttach:
        """Start `argv` behind a new relay sized `rows`x`cols`. Raises what the factory raises (a missing tmux is
        `FileNotFoundError`) so the caller's spawn-failure teardown runs, exactly as a failed `pty.fork`."""
        return cls((factory or _default_factory)(list(argv), rows, cols, env), max_pending=max_pending)

    @property
    def pid(self) -> int:
        return self._proc.pid

    def isalive(self) -> bool:
        try:
            return bool(self._proc.isalive())
        except Exception:
            return False

    def start(self, loop, on_data: Callable[[bytes], None], on_eof: Callable[[], None]) -> None:
        """Start the reader and writer threads. `on_data`/`on_eof` run ON THE LOOP THREAD; `on_eof` at most
        once, and neither after `close()`."""

        def _deliver(chunk: bytes) -> None:
            if not self._closed:
                on_data(chunk)

        def _deliver_eof() -> None:
            if not self._closed:
                on_eof()

        def _read() -> None:
            while True:
                try:
                    text = self._proc.read(_READ_SIZE)
                except Exception:   # EOFError is the normal end; any other failure is the same EOF
                    break
                if not text:
                    continue   # nothing decodable yet, not data
                try:
                    loop.call_soon_threadsafe(_deliver, text.encode("utf-8", "replace"))
                except RuntimeError:
                    return   # the loop is closed — nobody is left to tell
            try:
                loop.call_soon_threadsafe(_deliver_eof)
            except RuntimeError:
                pass

        self._reader = threading.Thread(target=_read, name=f"conpty-read-{self.pid}", daemon=True)
        self._writer = threading.Thread(target=self._drain, name=f"conpty-write-{self.pid}", daemon=True)
        self._reader.start()
        self._writer.start()

    def write(self, data: bytes) -> bool:
        """Queue client input for the writer thread; never blocks. False only when the backlog would pass
        `max_pending` and the input was DROPPED (keystrokes, so dropping the overflow is right — console.py's
        own rule), which the caller logs. After `close()` input is dropped silently: there is no client."""
        if not data or self._closed:
            return True
        with self._lock:
            if self._pending + len(data) > self._max_pending:
                return False
            self._pending += len(data)
        self._queue.put(data)
        return True

    def _drain(self) -> None:
        # Incremental: a browser frame may end mid-code-point, and the process's `write` takes `str`.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            batch = [self._queue.get()]
            # Everything already queued goes in one pass: consecutive input is joined into one `write` (each
            # costs a process spawn) and a resize superseded by a later one is skipped. Order is kept.
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            pending = bytearray()
            for at, item in enumerate(batch):
                if item is _STOP:
                    self._send(decoder, pending)
                    return
                if isinstance(item, _Resize):
                    self._send(decoder, pending)
                    pending = bytearray()
                    later = batch[at + 1:]
                    if not any(isinstance(other, _Resize) for other in later) and not self._closed:
                        try:
                            self._proc.setwinsize(item.rows, item.cols)
                        except Exception:
                            pass   # a resize of a dead console is moot
                    continue
                with self._lock:
                    self._pending -= len(item)
                pending += item
            self._send(decoder, pending)

    def _send(self, decoder, data: bytes) -> None:
        if not data:
            return
        text = decoder.decode(bytes(data))
        if not text or self._closed:
            return
        try:
            self._proc.write(text)
        except Exception:
            pass   # client gone — the reader's EOF owns teardown; later writes are dropped the same way

    def resize(self, rows: int, cols: int) -> None:
        if not self._closed:
            self._queue.put(_Resize(rows, cols))

    def close(self) -> None:
        """Detach: end the attach CLIENT and nothing else (see the module docstring). Idempotent; blocks for
        the process's own close plus `_WRITER_JOIN_SECONDS`, so the caller runs it in an executor."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)
        try:
            self._proc.close(force=True)
        except Exception as exc:
            log.warning("console: attach client %s did not close cleanly: %s", self.pid, exc)
        if self._writer is not None:
            self._writer.join(_WRITER_JOIN_SECONDS)
