"""The parts of the seam Linux and macOS answer identically: advisory locks and the AF_UNIX endpoint.

Stdlib only; imports nothing outside this package. `fcntl` does not exist on Windows, yet Windows DOES
import this module: `node_paths` reads `linux.paths()` (the Linux layout it aliases) on every OS. So `fcntl`
is guarded, and the lock functions below are never bound into the seam there (`windows.py` has its own)."""
from __future__ import annotations

import asyncio
import socket

try:
    import fcntl
except ImportError:  # Windows: see the module docstring — only `linux.paths()` is read there
    fcntl = None


def lock(fh, *, exclusive: bool = True, blocking: bool = True) -> None:
    """`flock` the whole file behind `fh` (a file object or a raw fd).

    Non-blocking and already held raises `BlockingIOError` (an `OSError`), exactly as `fcntl.flock`
    does — every caller's `except OSError` arm keeps meaning "someone else holds it"."""
    fd = fh if isinstance(fh, int) else fh.fileno()
    flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        flags |= fcntl.LOCK_NB
    fcntl.flock(fd, flags)


def unlock(fh) -> None:
    """Release a lock taken by `lock`."""
    fd = fh if isinstance(fh, int) else fh.fileno()
    fcntl.flock(fd, fcntl.LOCK_UN)


async def serve_socket(addr: str, handler, **kwargs):
    """Listen on the local IPC endpoint `addr` (a filesystem path) — an `asyncio` server."""
    return await asyncio.start_unix_server(handler, path=str(addr), **kwargs)


def connect_socket(addr: str, timeout: float | None = None) -> socket.socket:
    """A connected stream socket to the local IPC endpoint `addr`. The caller owns (and closes) it.

    Raises `OSError` (incl. `FileNotFoundError`/`ConnectionRefusedError`/`socket.timeout`) like a bare
    `connect` — nothing is swallowed here."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        sock.connect(str(addr))
    except BaseException:
        sock.close()
        raise
    return sock


def pane_os_pid(pane_pid, *, msys_start=None) -> int | None:
    """On posix tmux's `#{pane_pid}` IS the OS pid. None for anything that is not a positive pid.
    `msys_start` is the Windows pane-identity check (`host_platform.windows.pane_os_pid`); unused here."""
    del msys_start
    try:
        pid = int(pane_pid)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def path_key(path: str) -> str:
    """A path in its comparable spelling: posix paths are compared exactly as written (whitespace is a
    filename character), less a trailing `/`."""
    text = path or ""
    return text.rstrip("/") or ("/" if text else "")


def is_under(path: str, root: str) -> bool:
    """Whether `path` is `root` or inside it — a string rule, never a filesystem lookup."""
    p, r = path_key(path), path_key(root)
    if not p or not r:
        return False
    return p == r or p.startswith(r.rstrip("/") + "/")
