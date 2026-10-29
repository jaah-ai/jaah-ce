"""jaahd's half of ROOT GROW on a plain node: the CP enlarged this machine's root volume and asks, on the
observe response (`root_grow: true`), for the partition and filesystem to follow it. This module only drops
the EMPTY marker a privileged watcher acts on; the grow itself is never this unprivileged process's.

  * Linux — `<run root>/requests/root-grow` (override `JAAH_ROOT_GROW_MARKER`), watched by
    the `jaah-root-grow.path` unit, whose root oneshot runs `root_grow.py --pass` (growpart + resize2fs /
    xfs_growfs via `vnodehelper.ops.grow_root`). Gated by `marker_request.root_request_enabled`: never a
    virtual node (its root is the HOST's; the host daemon grows it through the helper's `host.grow_root`),
    never an un-overridden sandbox.
  * Windows — `C:\\ProgramData\\Jaah\\state\\requests\\root-grow`, the upgrade marker's sibling, polled by the
    LocalSystem `jaah-update` role (`JaahService.cs`), which deletes it and runs an
    idempotent `Resize-Partition` of C: to its supported maximum.

At most one write per `THROTTLE_S`: the CP keeps saying `root_grow` until the node's `root_total_kb` (the
observation body, `reconcile/observe.py`) reaches the target, and an idempotent grow needs no faster re-ask.
The marker is EMPTY for `marker_request`'s reason.
"""
from __future__ import annotations

import logging
import ntpath
import time
from pathlib import Path
from typing import Callable

import host_platform
import marker_request

log = logging.getLogger("jaahd.root_grow_request")

THROTTLE_S = 300.0

MARKER_PARTS = ("requests", "root-grow")
_MARKER_ENV = "JAAH_ROOT_GROW_MARKER"

_last_write: float | None = None


def marker_path() -> Path:
    """`$JAAH_ROOT_GROW_MARKER`, else the OS's spelling: `<run root>/requests/root-grow` on Linux,
    `<state>\\requests\\root-grow` (beside the upgrade marker) on Windows."""
    if host_platform.is_windows():
        from jaah_env import env_get  # noqa: PLC0415

        explicit = env_get(_MARKER_ENV)
        if explicit:
            return Path(explicit)
        return Path(ntpath.join(host_platform.paths().state_dir, *MARKER_PARTS))
    return marker_request.marker_path(_MARKER_ENV, MARKER_PARTS)


def requests_enabled() -> bool:
    """Linux: `marker_request.root_request_enabled`. Windows (no vnodes): off only in an un-overridden
    sandbox, for the same reason. Never raises."""
    if host_platform.is_windows():
        try:
            return marker_request.requests_enabled(_MARKER_ENV)
        except RuntimeError:
            return False
    return marker_request.root_request_enabled(_MARKER_ENV)


def record_observe_response(result: dict | None, *, clock: Callable[[], float] = time.monotonic,
                            enabled: Callable[[], bool] = requests_enabled,
                            marker: Callable[[], Path] = marker_path) -> bool:
    """Write the marker when the observe response says `root_grow: true` (absent = false) and the last write
    is `THROTTLE_S` old. Returns whether it wrote. Never raises."""
    global _last_write
    if not isinstance(result, dict) or result.get("root_grow") is not True:
        return False
    now = clock()
    if _last_write is not None and now - _last_write < THROTTLE_S:
        return False
    if not enabled():
        return False
    target = marker()
    try:
        # `write_bytes(b"")`, not `touch()`: the marker must be EMPTY.
        target.write_bytes(b"")
    except OSError as exc:
        log.warning("root-grow: could not create %s: %s", target, exc)
        return False
    _last_write = now
    log.info("root-grow: the control plane enlarged the root volume; asked for a grow (%s)", target)
    return True
