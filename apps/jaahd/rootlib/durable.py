"""The root helpers' one durable file write (`vnodehelper` and `rootlib.accounts` call it).

Stdlib plus two daemon leaf files loaded by path (`path_module`, and `agents/staged_write.py` executed
through it so `agents/__init__` — every adapter — never loads here). Safe for a root helper because both are
stdlib-only, have no import-time side effect, and sit in the root-owned tree the helper runs from. Not
`env_helpers.atomic_write_text`, which skips the directory fsync these stores need.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import path_module

_staged_write = path_module.load(Path(__file__).resolve().parents[1] / "agents" / "staged_write.py",
                                 "_rootlib_staged_write")


def write_durably(
    path: Path | str, text: str, *, mode: int, uid: Optional[int] = None, gid: Optional[int] = None
) -> None:
    """Replace `path` with `text` so a crash leaves the old file or the new one, never a torn one.

    `staged_write.replace_file` owned (when `uid`/`gid` are given) and moded on its descriptor BEFORE the
    rename so the path never exists wider, fsynced, renamed over `path`; then the directory is fsynced so
    the rename itself survives a power loss. On failure the temp file is removed and `path` is untouched.
    """
    path = Path(path)
    _staged_write.replace_file(str(path), text, mode=mode, uid=uid, gid=gid, fsync=True,
                               owner_required=uid is not None or gid is not None)
    dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
