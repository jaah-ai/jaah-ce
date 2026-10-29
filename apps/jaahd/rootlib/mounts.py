"""Mount discovery, unmount and tree removal for a root teardown — never deleting through a live mount.

`vnodehelper` (its destroy, its NodeLink staging) is the caller; stdlib only, like every `rootlib` module.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .runner import run

# /proc/self/mountinfo's octal escape (`\040` for a space, …).
MOUNT_ESCAPE = re.compile(r"\\(\d{3})")


def remove_tree(path: str, failures: list[str]) -> None:
    """Remove `path` as root, collecting every failure — the caller's verification decides whether the
    teardown stands. `rmtree` unlinks symlinks rather than following them."""
    try:
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.lexists(path):
            shutil.rmtree(path, onexc=lambda _fn, p, exc: failures.append(f"{p}: {exc}"))
    except OSError as exc:
        failures.append(f"{path}: {exc}")


def mounts_below(root: str) -> list[str]:
    """Every mount point at or under `root`, from /proc/self/mountinfo."""
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text()
    except FileNotFoundError:  # pragma: no cover — /proc is always there on a node
        return []
    points = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        # Field 4 escapes space/tab/newline/backslash as octal; un-escape, or `umount` silently misses
        # the point and the rmtree deletes through it.
        point = MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4])
        if point == root or point.startswith(root.rstrip("/") + "/"):
            points.append(point)
    return points


def unmount_below(root: str) -> list[str]:
    """Unmount everything mounted at or under `root`, deepest first. RETURNS the points it unmounted.

    `rmtree` over a live mount (rootless docker's overlay/shm) deletes through it; deepest first because
    a parent cannot unmount under a mounted child.
    """
    points = sorted(mounts_below(root), key=lambda p: p.count("/"), reverse=True)
    for point in points:
        run("umount", "--lazy", "--recursive", point, check=False)
    return points
