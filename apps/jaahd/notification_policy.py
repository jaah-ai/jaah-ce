"""notification_policy.py — the ONE writer of the two per-vendor notification policy files, `.omc-config.json`
(`accounts._notification_setup`) and `.omx-config.json` (`omx_layer.notification_policy`). Both carry the
same shape — `notifications.enabled` plus `notifications.events.<event>.enabled` — so one merge-and-write
serves both; each caller owns its event set and its file name, nothing vendor-specific lives here.

Channel-agnostic: it gates which events fire, never a destination. The file sits in a home the runner
controls, so the target is never followed: a symlink planted at the policy path is a refused write
(`O_NOFOLLOW`), reported and left where it is, never a write to wherever it points. Stdlib only (runs
from the packaged tree); never raises.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import file_io  # stdlib-only leaf: the one no-follow, non-blocking, bounded read

# 0 where the platform lacks the flag (it is POSIX-only).
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_MODE = 0o644
# The most of an existing policy file that is read back for the merge: it is a handful of keys, and an
# unbounded read of a runner-writable file is a hang the daemon must never risk; one past it reads as `{}`.
_READ_CAP = 256 * 1024


def _read_existing(path: Path) -> dict:
    """The current top-level object at `path`, or `{}` when it is absent, not a regular file (a symlink or
    FIFO is not followed or waited on), unreadable, over `_READ_CAP` or not a JSON object."""
    try:
        current = json.loads(file_io.read_bounded(str(path), _READ_CAP).decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return current if isinstance(current, dict) else {}


def write_notification_policy(path, events: dict) -> dict:
    """Merge `events` into the policy file at `path`: unrelated top-level keys are preserved,
    `notifications.enabled` becomes true and `notifications.events` becomes `events`. `{"applied", "error"}`;
    never raises."""
    target_path = Path(path).expanduser()
    try:
        target = _read_existing(target_path)
        notifications = target.setdefault("notifications", {})
        if not isinstance(notifications, dict):
            notifications = {}
            target["notifications"] = notifications
        notifications["enabled"] = True
        notifications["events"] = {k: dict(v) for k, v in events.items()}
        payload = (json.dumps(target, indent=2) + "\n").encode("utf-8")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Through the fd, never a `Path.write_text` (which follows a link at `path`). O_NONBLOCK so a
        # FIFO with no reader is ENXIO rather than a hang (no effect on a regular file); the fstat
        # refuses any other non-regular file the open accepted.
        fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW | _O_NONBLOCK
                     | _O_CLOEXEC, _MODE)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {"applied": False, "error": f"{target_path} is not a regular file"}
            os.write(fd, payload)
        finally:
            os.close(fd)
        return {"applied": True, "error": ""}
    except Exception as exc:
        return {"applied": False, "error": str(exc)}
