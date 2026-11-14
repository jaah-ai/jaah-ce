"""file_io.py — the ONE open of a file somebody else may have planted: a regular file, never through a
link, never blocking on a FIFO, and a bounded whole-document read on top of it.

Every daemon read of a path a runner (or a VM guest, or another uid) can write goes through `open_regular`,
`read_bounded` or `read_json_or(max_bytes=)`; a new shape is added here, not spelled at a call site. Stdlib only at import time (a
hook-merge leaf at the account's uid imports this too); the Windows containment guard imports its daemon
helpers lazily and refuses when they are not there.
"""
from __future__ import annotations

import errno
import json
import os
import stat
from typing import Any, Callable


# `getattr`: Windows has neither `O_NOFOLLOW` nor `O_NONBLOCK` (so `_regular_fd` refuses a reparse point
# before the open there), and wants `O_BINARY`.
_REGULAR_READ_FLAGS = os.O_RDONLY | sum(
    getattr(os, flag, 0) for flag in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "O_BINARY"))


def _windows_local(path: str) -> bool:
    """`windows.local_path_under(path, jaah_home())`, False when either cannot answer. Imported here: daemon
    modules, and this package stays importable without them."""
    try:
        from env_helpers import jaah_home
        from host_platform import windows

        return windows.local_path_under(path, jaah_home())
    except Exception:  # noqa: BLE001 — an unanswerable guard refuses, never opens
        return False


def final_path_under(fd: int, root: str) -> bool:
    """Whether the handle FD's final path (links and junctions resolved by the kernel, `host_platform.fd_path`)
    lies under ROOT's real path. False when it cannot be read (fail closed). The ONE home of that Windows check."""
    import ntpath

    import host_platform  # a daemon module: local, so this package stays importable without it

    real_root = ntpath.normcase(os.path.realpath(root)).rstrip("\\") + "\\"
    try:
        return ntpath.normcase(host_platform.fd_path(fd)).startswith(real_root)
    except OSError:
        return False


def _windows_final_local(fd: int) -> bool:
    """`final_path_under(fd, jaah_home())`: `_windows_local` asked of the name, this of what actually opened, so a
    junction swapped in between is refused. False when either cannot answer."""
    try:
        from env_helpers import jaah_home

        return final_path_under(fd, jaah_home())
    except Exception:  # noqa: BLE001 — an unanswerable guard refuses
        return False


def _regular_fd(path: str, *, single_link: bool = False) -> int:
    """A read fd on `path`, or an OSError: `os.open`'s own, untouched (so a caller can tell
    `PermissionError` apart), `EISDIR`/`ENXIO` when what opened is not a regular file (the classification
    `session_sweep.open_no_follow` uses), `EMLINK` when `single_link` and the inode has a second name — a
    hardlink may be someone else's file, linked in. `O_NONBLOCK` is load-bearing (FIFO); `O_NOFOLLOW`
    because the path was chosen by someone else. Windows has no `O_NOFOLLOW`: a path that is a link, or
    leads off the local disks or out of `<jaah_home>` (`windows.local_path_under`), is refused `ELOOP`
    before the open — an open through a link to `\\\\host\\share` authenticates the daemon to that host —
    and after it the opened file must be the one `lstat` saw (a link swapped in between, where Developer Mode
    lets a runner create one, is refused rather than followed) with a final path still under `<jaah_home>`
    (`_windows_final_local`)."""
    windows = not hasattr(os, "O_NOFOLLOW")
    if windows:
        if not _windows_local(path):
            raise OSError(errno.ELOOP, "a link, or a path off the local disks or out of the jaah home", path)
        named = os.lstat(path)
    fd = os.open(path, _REGULAR_READ_FLAGS)
    try:
        info = os.fstat(fd)
        if windows and ((info.st_dev, info.st_ino) != (named.st_dev, named.st_ino) or not _windows_final_local(fd)):
            raise OSError(errno.ELOOP, "swapped between the check and the open, or off the jaah home", path)
        if stat.S_ISDIR(info.st_mode):
            raise OSError(errno.EISDIR, "a directory wears the file's name", path)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.ENXIO, "not a regular file", path)
        if single_link and info.st_nlink != 1:
            raise OSError(errno.EMLINK, "the inode has a second name", path)
    except OSError:
        os.close(fd)
        raise
    return fd


def open_regular(path: str, *, single_link: bool = False) -> int | None:
    """`_regular_fd`, with every failure collapsed to None: a reader has no third state to report."""
    try:
        return _regular_fd(path, single_link=single_link)
    except OSError:
        return None


def _bounded_bytes(path: str | os.PathLike, limit: int, *, single_link: bool = False) -> bytes:
    """`read_bounded`'s read, raising: `_regular_fd`'s OSError untouched, `EFBIG` for a document past the
    bound (never truncated)."""
    fd = _regular_fd(os.fspath(path), single_link=single_link)
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            # `limit + 1`: a document at the bound is kept, one past it refused (not truncated).
            payload = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(payload) > limit:
        raise OSError(errno.EFBIG, f"past the {limit}-byte bound", os.fspath(path))
    return payload


def read_bounded(path: str, limit: int, *, single_link: bool = False) -> bytes:
    """At most `limit` bytes of `path`, or `b""` for anything that is not a plain readable regular file
    (or, with `single_link`, one with a second name).

    For a whole small document a runner writes. Bounded: the runner chooses the size (a 300 MB registry
    drove a 620 MB peak RSS). A file over the bound is `b""`, never truncated."""
    try:
        return _bounded_bytes(path, limit, single_link=single_link)
    except OSError:
        return b""


def tail_bytes(handle, limit: int) -> bytes:
    """The last `limit` bytes of the open binary `handle` (the whole file when it is shorter): a seek to
    `size - limit`, then one read. For a caller that opened the file its own way (`open_no_follow`, a
    pinned fd); `read_tail` is the plain-path form."""
    size = os.fstat(handle.fileno()).st_size
    handle.seek(max(0, size - limit))
    return handle.read(limit)


def read_tail(path: "str | os.PathLike", limit: int) -> bytes | None:
    """`tail_bytes` of the file at `path`, or None when it cannot be opened or read — the ONE tail read of
    a log or transcript (a build's console, a pane log, an agent transcript). A plain `open`: the
    reader's own or its runner's log, at a path the reader chose; a path somebody else may have planted
    goes through `open_regular` first and reads with `tail_bytes`."""
    try:
        with open(path, "rb") as handle:
            return tail_bytes(handle, limit)
    except OSError:
        return None


def read_json_or(path: str | os.PathLike, default: Any, *, expect: type | tuple[type, ...] | None = None,
                 max_bytes: int | None = None, warn: Callable[[Exception], None] | None = None) -> Any:
    """The JSON document at `path`, or `default` when the file is missing, unreadable, not JSON (deep nesting
    included) or, with `expect`, not of that top-level type — the one home of "a state file, absent-or-broken
    reads as the default". A plain `open` for the daemon's OWN state file; with `max_bytes`, a path another
    uid can write: `_bounded_bytes` (never through a link, never a FIFO, a document past the bound is the
    default). `warn(exc)` is called when a PRESENT file is unreadable or not JSON — a missing file is the
    ordinary default and stays silent."""
    try:
        if max_bytes is None:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        else:
            doc = json.loads(_bounded_bytes(path, max_bytes))
    except FileNotFoundError:
        return default
    except (OSError, ValueError, RecursionError) as exc:
        if warn is not None:
            warn(exc)
        return default
    return doc if expect is None or isinstance(doc, expect) else default


def json_object(line: bytes | str) -> dict | None:
    """One line parsed as a JSON object, or None for anything else — unparseable, or a document that is
    not an object. THE ONE preamble of every per-line read (transcripts, the broker's request lines, the
    guard spool).

    `RecursionError` (deep nesting in a runner-written line; ten KB of `[` trips the C scanner) is not a
    `ValueError`, and escaping from a read called once per line of a runner-written file would flood the
    journal. `UnicodeDecodeError` is already a `ValueError`."""
    try:
        record = json.loads(line)
    except (ValueError, TypeError, RecursionError):
        return None
    return record if isinstance(record, dict) else None
