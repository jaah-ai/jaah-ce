"""The kernel's mount table, read in ONE place — and mount discovery, unmount and tree removal for a root
teardown, never deleting through a live mount.

`rows` is the one parser of `/proc/self/mountinfo` (`vnodehelper`, `shared_storage`, the root filesystem
reader; `nodelink_mount.py` keeps its own under `-I`, where it may import nothing). Stdlib only, like every
`rootlib` module.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Iterator, NamedTuple

from .runner import run

MOUNTINFO = "/proc/self/mountinfo"

# mountinfo(5) escapes a space, tab, newline or backslash in a path as octal (`\040` for a space, …).
MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


class Mount(NamedTuple):
    """One mountinfo(5) line: the fields before the ` - ` separator a reader uses, and the three after it."""
    id: str        # mount id
    dev: str       # `major:minor` of the mounted device (field 3), what `os.stat(...).st_dev` names
    point: str     # the mount point, its octal escapes undone
    fstype: str
    source: str
    options: str   # the super options


def rows(table: str = "") -> Iterator[Mount]:
    """Every entry of the mountinfo TABLE (`MOUNTINFO` unless given), in the kernel's order — a stacked mount
    is the LAST line naming its point. A line short of the fields is skipped; an unreadable table raises
    `OSError`, since each caller has its own "no table" answer."""
    with open(table or MOUNTINFO, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    for line in lines:
        head, sep, tail = line.partition(" - ")
        fields, after = head.split(), tail.split()
        if not sep or len(fields) < 5 or len(after) < 3:
            continue
        point = MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4])
        yield Mount(fields[0], fields[2], point, after[0], after[1], after[2])


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
    """Every mount point at or under `root`, from the mount table (escapes undone by `rows`, or `umount`
    silently misses the point and the rmtree deletes through it)."""
    try:
        points = [m.point for m in rows()]
    except OSError:  # pragma: no cover — /proc is always there on a node
        return []
    return [p for p in points if p == root or p.startswith(root.rstrip("/") + "/")]


def unmount_below(root: str) -> list[str]:
    """Unmount everything mounted at or under `root`, deepest first. RETURNS the points it unmounted.

    `rmtree` over a live mount (rootless docker's overlay/shm) deletes through it; deepest first because
    a parent cannot unmount under a mounted child.
    """
    points = sorted(mounts_below(root), key=lambda p: p.count("/"), reverse=True)
    for point in points:
        run("umount", "--lazy", "--recursive", point, check=False)
    return points
