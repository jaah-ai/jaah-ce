"""subids — the ONE home of the `/etc/subuid` + `/etc/subgid` logic (subordinate uid/gid ranges).

Those files are root-writable only, so this belongs to the privileged root side (`rootlib`) and nowhere else.
The GHA runner installer imports it rather than duplicating it, which is why it is
stdlib-only: install.py is a standalone host script with no virtualenv.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, ContextManager

from vnodehelper import layout as vn_layout

# 65536: the conventional span for a user-namespace mapping.
SUBID_COUNT = 65536
#: The real uids Jaah hands out itself: every virtual node's span, every slot up to `SLOT_MAX`
#: (`vnodehelper/layout.py`). A subordinate range covering one lets its owner
#: `newuidmap` itself into that uid, so the vnode helper refuses to build a node beside it
#: (`vnodehelper/ops.py::_refuse_collisions`). A first-free range never lands here, and `ensure_subids` moves one
#: that already does when its caller can rewrite the files.
RESERVED_UIDS = range(vn_layout.UID_BASE, vn_layout.UID_BASE + vn_layout.UID_STRIDE * (vn_layout.SLOT_MAX + 1))


def parse_subid_ranges(text: str) -> list[tuple[str, int, int]]:
    """Parse /etc/subuid or /etc/subgid. Malformed lines are skipped, not fatal: these files are
    shared with useradd and other tooling, and refusing to run because some unrelated line is odd
    would be worse than ignoring it."""
    ranges = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3:
            continue
        try:
            ranges.append((parts[0], int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return ranges


def next_subid_start(text: str, floor: int = 100000) -> int:
    """First start offset at or above `floor` that overlaps no existing allocation and no `RESERVED_UIDS`."""
    taken = [(begin, count) for _owner, begin, count in parse_subid_ranges(text)]
    taken.append((RESERVED_UIDS.start, len(RESERVED_UIDS)))
    start = floor
    for begin, count in sorted(taken):
        if start < begin + count and begin < start + SUBID_COUNT:
            start = begin + count
    return start


def on_reserved_uids(text: str, user: str) -> list[range]:
    """`user`'s ranges in this file that cover a `RESERVED_UIDS` uid. Pure."""
    return [range(begin, begin + count) for owner, begin, count in parse_subid_ranges(text)
            if owner == user and begin < RESERVED_UIDS.stop and RESERVED_UIDS.start < begin + count]


def without_range(text: str, user: str, held: range) -> str:
    """The file's text without `user`'s line for `held`, every other line kept as it was. Pure."""
    gone = f"{user}:{held.start}:{len(held)}"
    return "".join(f"{line}\n" for line in text.splitlines() if line.strip() != gone)


def subid_line_needed(text: str, user: str, start: int | None = None) -> str | None:
    """The line to append so `user` has a mapping range, or None when one already exists. `start`
    pins the range (a caller-allocated block); without it the
    first free range at or above the floor is taken (the gha pool's shape)."""
    if any(owner == user for owner, _b, _c in parse_subid_ranges(text)):
        return None
    return f"{user}:{next_subid_start(text) if start is None else int(start)}:{SUBID_COUNT}"


def ensure_subids(user: str, check: bool, read_text: Callable[[Path], str],
                  append_text: Callable[[Path, str], None],
                  lock: Callable[[], ContextManager[None]] | None = None,
                  start: int | None = None,
                  move: Callable[[Path, str, range, range], None] | None = None) -> tuple[bool, list[str]]:
    """Subordinate uid/gid ranges for rootless docker's user-namespace mapping.

    A first-free range already on `RESERVED_UIDS` — handed out before the allocator skipped them — blocks every
    virtual node on the host (the vnode helper refuses it). Check mode reports it; with `move(path, user, old,
    new)` it is MOVED to a free range, the user's other ranges kept. Only a caller that knows nothing of the user
    maps or owns its old range passes `move` (`gha_job.py prepare`, between the slot's wipe and its re-provision):
    a live namespace or a file owned inside the old range would otherwise keep the old mapping. Without it the
    range is left for that caller. A pinned `start` is never moved.

    The read-compute-append is one critical section, locked HERE: this helper and the gha installer are
    separate processes, and two readers of the same highest range would append OVERLAPPING mappings,
    breaking the isolation this file exists for. Check mode takes no lock: it writes nothing, and the lock
    lives in a root-only dir, so the unprivileged drift report would fail on permissions. Override `lock`
    only with `contextlib.nullcontext` (tests) or when already holding it: `flock` is per open file
    description, so a nested acquire in one process deadlocks.
    """
    if check:
        return _ensure_subids(user, check, read_text, append_text, start, move)
    if lock is None:
        from .locking import node_lock as lock
    with lock():
        return _ensure_subids(user, check, read_text, append_text, start, move)


def _ensure_subids(user: str, check: bool, read_text: Callable[[Path], str],
                   append_text: Callable[[Path, str], None],
                   start: int | None = None,
                   move: Callable[[Path, str, range, range], None] | None = None) -> tuple[bool, list[str]]:
    lines, ok = [], True
    for path in (Path("/etc/subuid"), Path("/etc/subgid")):
        try:
            text = read_text(path)
        except FileNotFoundError:
            text = ""
        for held in on_reserved_uids(text, user) if start is None else ():
            rest = without_range(text, user, held)
            first = next_subid_start(rest)
            new = range(first, first + SUBID_COUNT)
            what = f"{path} range {held.start}-{held.stop - 1} of {user} covers Jaah's uids"
            if check:
                lines.append(f"  DRIFT {what} (would move it to {new.start}-{new.stop - 1})")
                ok = False
            elif move is None:
                lines.append(f"  held  {what}: the slot's next reset moves it")
            else:
                move(path, user, held, new)
                text = rest + f"{user}:{new.start}:{SUBID_COUNT}\n"
                lines.append(f"  moved {what} to {new.start}-{new.stop - 1}")
        needed = subid_line_needed(text, user, start)
        if needed is None:
            lines.append(f"  ok    {path} has a range for {user}")
            continue
        if check:
            lines.append(f"  DRIFT {path} has no range for {user} (would add {needed})")
            ok = False
            continue
        append_text(path, needed + "\n")
        lines.append(f"  added {needed} to {path}")
    return ok, lines
