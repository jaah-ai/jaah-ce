"""The node's disk-event spool: one JSON report per pass that freed disk, for jaahd to upload.

    /var/lib/jaah/disk-events/<reclaim_id>.json     # root:jaahd 0770, one file per report
    /var/lib/jaah/disk-events/warn_pct              # the CP's warn line, cached by jaahd (0644)

The schema is the pinned contract `POST /api/agent/disk-reclaims` serves (`reclaim_id` is its idempotency
key); each writer sends only the keys it owns. The WRITERS are root: the disk reclaim pass
(`disk_reclaim.py`) and the GHA pool's user removal (its host installer and the vnode helper) — so this
module imports nothing but the standard library at import time, and every write failure is swallowed: a
report is best-effort, the work it describes is not, and a node without the spool yet simply gets no report.

The READER is jaahd: `drain_spool` uploads every report oldest-first and deletes each on a 2xx, keeping at
most `SPOOL_MAX_FILES` while the CP is unreachable; `post_disk_report` is THE ONE uploader any in-process
producer calls. The daemon's half of the reclaim (trigger, warn cache) is `disk_reclaim_trigger.py`.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Callable

# 0 where the platform lacks the flag (a POSIX-only flag must not break Windows).
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

log = logging.getLogger("jaahd.disk_events")

SPOOL = Path("/var/lib/jaah/disk-events")
# The spool's bound while the CP is unreachable: the newest this many reports survive.
SPOOL_MAX_FILES = 50
# The warn line jaahd caches from the CP's observe response (`disk_reclaim_trigger.record_observe_response`).
# A number the root pass validates: an unprivileged writer may only move a pass's trigger LABEL, never its rule.
WARN_PCT_FILE = SPOOL / "warn_pct"


def utc_now() -> str:
    """ISO-8601 UTC, second precision — the contract's `started_at`/`finished_at` spelling."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# A walk of a home its (untrusted) user may still be filling stops here: the figure is a report, never a
# reason to hold a converge — past either bound the bytes counted so far are what is reported.
WALK_ENTRY_CAP = 2_000_000
WALK_SECONDS = 60.0


def tree_bytes(path: Path | str, entry_cap: int = WALK_ENTRY_CAP, seconds: float = WALK_SECONDS) -> int:
    """Bytes allocated under `path`, never following a link; 0 when it is absent or unreadable.

    What the removal will free, measured BEFORE it runs. Allocated blocks rather than apparent size, so a
    sparse file (a rootless docker store holds some) counts what the disk actually gets back. `os.fwalk`
    descends by DESCRIPTOR, so a directory its owner swaps for a link mid-walk is never entered by name;
    bounded by `entry_cap` entries and `seconds`."""
    try:
        top = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(top.st_mode):
        return top.st_blocks * 512
    total, seen, deadline = top.st_blocks * 512, 0, time.monotonic() + seconds
    counted: set[int] = set()
    try:
        for _root, dirs, files, dir_fd in os.fwalk(path, follow_symlinks=False):
            # Stay on the home's filesystem; a mount below it frees nothing when the home goes.
            dirs[:] = [d for d in dirs if _same_dev(d, dir_fd, top.st_dev)]
            for name in (*dirs, *files):
                try:
                    entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    continue
                if entry.st_dev == top.st_dev and entry.st_ino not in counted:   # a hard link counts once
                    counted.add(entry.st_ino)
                    total += entry.st_blocks * 512
                seen += 1
                if seen >= entry_cap or time.monotonic() >= deadline:
                    return total
    except OSError:
        pass
    return total


def _same_dev(name: str, dir_fd: int, dev: int) -> bool:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_dev == dev
    except OSError:
        return False


def removed_users_report(removed: dict[str, int], started_at: str, finished_at: str) -> dict:
    """The contract's `converge` report for the users a GHA pool converge removed: name -> bytes freed."""
    return {
        "reclaim_id": str(uuid.uuid4()),
        "started_at": started_at,
        "finished_at": finished_at,
        "trigger": "converge",
        "freed_bytes": {"orphan_users": sum(removed.values())},
        "removed_users": sorted(removed),
        "errors": [],
    }


def write_report(report: dict, spool: Path = SPOOL) -> Path | None:
    """Drop `report` into the spool as `<reclaim_id>.json`; the path, or None when there is no spool or the
    write failed. The spool is opened ONCE (`O_NOFOLLOW`) and everything after is relative to that
    descriptor, so a spool swapped for a link is refused rather than written through. Written to a dot-file
    and renamed, so the uploader never reads half a report; group-owned like the spool (0640), so the
    unprivileged daemon that uploads it can read and then delete it."""
    try:
        dir_fd = os.open(spool, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError:
        return None
    try:
        final = f"{report['reclaim_id']}.json"
        partial = f".{report['reclaim_id']}.json.partial"
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o640, dir_fd=dir_fd)
        try:
            with contextlib.suppress(OSError):   # only root may hand it to a group it is not in
                os.fchown(fd, -1, os.fstat(dir_fd).st_gid)
            os.fchmod(fd, 0o640)
            os.write(fd, json.dumps(report, sort_keys=True).encode())
        finally:
            os.close(fd)
        os.replace(partial, final, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return spool / final
    except (OSError, KeyError, TypeError, ValueError):
        return None
    finally:
        os.close(dir_fd)


def report_removed_users(removed: dict[str, int], started_at: str, spool: Path = SPOOL) -> Path | None:
    """Write the `converge` report for `removed` — nothing at all when no user was removed."""
    if not removed:
        return None
    return write_report(removed_users_report(removed, started_at, utc_now()), spool)


def used_pct(path: str = "/", statvfs: Callable | None = None) -> float:
    """How full the filesystem holding PATH is, in percent (one decimal) — the figure the warn line is against.
    `os.statvfs` is resolved per call: Windows has none, and the daemon imports this module on every OS."""
    st = (statvfs or os.statvfs)(path)
    total = st.f_blocks * st.f_frsize
    if total <= 0:
        return 0.0
    return round(100.0 * (st.f_blocks - st.f_bfree) * st.f_frsize / total, 1)


def read_warn_pct(path: Path = WARN_PCT_FILE) -> float | None:
    """The cached warn %, or None (no file, unreadable, not a number in (0, 100]) — no node-side default.

    Root reads a file the unprivileged daemon writes: never through a link, never blocking, 32 bytes at most."""
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        value = float(os.read(fd, 32).decode().strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    return value if 0 < value <= 100 else None


def spool_files(spool: Path = SPOOL) -> list[Path]:
    """The spooled reports, OLDEST first (a dot-file is a write in progress, never a report)."""
    try:
        files = [p for p in spool.iterdir() if p.suffix == ".json" and not p.name.startswith(".")]
    except OSError:
        return []
    stamped = []
    for p in files:
        with contextlib.suppress(OSError):
            stamped.append((p.stat().st_mtime, p.name, p))
    return [p for _m, _n, p in sorted(stamped)]


def trim_spool(spool: Path = SPOOL, max_files: int = SPOOL_MAX_FILES) -> None:
    """Drop the OLDEST reports past MAX_FILES. Never raises."""
    files = spool_files(spool)
    for old in files[:max(len(files) - max_files, 0)]:
        with contextlib.suppress(OSError):
            old.unlink()


def _default_post(report: dict) -> bool:
    import control_plane_api  # noqa: PLC0415 — daemon-only; the root writers never reach this
    return control_plane_api.agent_api_disk_reclaim(report)


def post_disk_report(report: dict, *, post: Callable[[dict], bool] | None = None,
                     spool: Path | None = None) -> bool:
    """Upload ONE disk report. True on a 2xx; otherwise spool it (when the spool is writable) so the next
    drain retries it, and return False. Never raises."""
    post = post or _default_post
    try:
        if post(report):
            return True
    except Exception as exc:  # noqa: BLE001 — "never raises": a raising uploader is a failed upload
        log.warning("disk-events: report upload failed: %s", exc)
    spool = spool or SPOOL
    if os.access(spool, os.W_OK) and write_report(report, spool) is not None:
        trim_spool(spool)
    return False


def drain_spool(*, spool: Path | None = None, post: Callable[[dict], bool] | None = None) -> int:
    """Upload spooled reports oldest-first, deleting each on 2xx; stop at the first failure (the CP is down
    — the rest wait). Bounds the spool to `SPOOL_MAX_FILES` either way. Returns how many were uploaded."""
    spool = spool or SPOOL
    post = post or _default_post
    sent = 0
    for path in spool_files(spool):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        try:
            ok = post(report)
        except Exception as exc:  # noqa: BLE001 — the CP being down is the normal reason to stop
            log.warning("disk-events: upload of %s failed: %s", path.name, exc)
            ok = False
        if not ok:
            break
        try:
            path.unlink()
        except OSError as exc:
            log.warning("disk-events: uploaded %s but could not delete it: %s", path.name, exc)
        sent += 1
    trim_spool(spool)
    return sent
