"""The root helpers' one durable file write (`vnodehelper` and `rootlib.accounts` call it).

Stdlib only: the helpers run as their own root processes and import nothing of the daemon's, so this is
not `env_helpers.atomic_write_text` (which also skips the directory fsync these stores need).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional


def write_durably(
    path: Path | str, text: str, *, mode: int, uid: Optional[int] = None, gid: Optional[int] = None
) -> None:
    """Replace `path` with `text` so a crash leaves the old file or the new one, never a torn one.

    A `mkstemp` file in the same directory, owned (when `uid`/`gid` are given) and moded on its descriptor
    BEFORE the rename so the path never exists wider, fsynced, renamed over `path`, then the directory
    fsynced so the rename itself survives a power loss. On failure the temp file is removed and `path`
    is untouched.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w") as fh:
            if uid is not None or gid is not None:
                os.fchown(fh.fileno(), -1 if uid is None else uid, -1 if gid is None else gid)
            os.fchmod(fh.fileno(), mode)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
