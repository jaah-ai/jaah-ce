"""A root helper's unix socket, whose MODE is the caller gate.

The socket is handed to `root:<group>` at 0660 — the only gate the kernel enforces before a byte is
parsed. The caller tightens the umask to 0177 before `bind()`, so the socket is 0600 root:root across the
bind→chmod window and only ever WIDENS (`vnodehelper/server.py`).
"""
from __future__ import annotations

import grp
import os
from pathlib import Path


def prepare_socket_path(path: str) -> None:
    """The parent directory, and a stale socket removed. Root-owned, world-traversable.

    0755 on the parent is NOT the gate: the daemon must traverse it to reach a group-only socket.
    """
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    os.chown(parent, 0, 0)
    os.chmod(parent, 0o755)
    Path(path).unlink(missing_ok=True)


def secure_socket(path: str, group: str) -> None:
    """Hand the socket to root:<group> at 0660. The gate, applied."""
    gid = grp.getgrnam(group).gr_gid
    os.chown(path, 0, gid)
    os.chmod(path, 0o660)
