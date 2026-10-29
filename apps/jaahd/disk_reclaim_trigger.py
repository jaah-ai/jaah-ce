"""jaahd's half of the node DISK RECLAIM: when to ask for a pass, and the warn line it asks against.

  * `DiskReclaimTrigger.step` — the `disk-reclaim` periodic step (every `STEP_INTERVAL_S`). Writes the EMPTY
    marker `jaah-reclaim.path` watches when `SCHEDULED_INTERVAL_S` passed since its last write, or at once
    while the root disk is at or above the cached warn %; then drains the report spool
    (`disk_events.drain_spool`). ONE pass per filesystem: a virtual node never writes the marker (its HOST's
    pass covers every vnode's engines) but still drains; sandbox posture disables the marker.
  * `record_observe_response` — caches the CP's `disk_warn_pct` (observe response) in the daemon's state dir
    and mirrors it to `disk_events.WARN_PCT_FILE` (0644), which the root pass reads to label its trigger.
    Absent ⇒ both files go and every pass is `scheduled` (no node-side default threshold).

The pass itself (`disk_reclaim.py --pass`) needs none of this to run: the reclaim works with the CP down.
Stdlib only; paths resolved lazily.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

import disk_events
import env_helpers
import host_platform
import marker_request

log = logging.getLogger("jaahd.disk_reclaim_trigger")

# The step's period, and the scheduled cadence below warn: one rule, two cadences.
STEP_INTERVAL_S = 300.0
SCHEDULED_INTERVAL_S = 900.0

# Under the node runtime root; the `jaah-reclaim.path` systemd unit watches it and `disk_reclaim.py --pass`
# removes it. EMPTY, and never read.
MARKER_PARTS = ("requests", "disk-reclaim")
_MARKER_ENV = "JAAH_DISK_RECLAIM_MARKER"
# The daemon's own copy of the warn %, so a restart before the first observation still knows it.
_WARN_STATE_NAME = "disk-warn-pct"


def marker_path() -> Path:
    """`$JAAH_DISK_RECLAIM_MARKER`, else `<run root>/requests/disk-reclaim`."""
    return marker_request.marker_path(_MARKER_ENV, MARKER_PARTS)


def triggers_enabled() -> bool:
    """`marker_request.root_request_enabled` for the reclaim marker (one pass per filesystem — the host's)."""
    return marker_request.root_request_enabled(_MARKER_ENV)


def warn_state_path() -> Path:
    return env_helpers.daemon_state_dir(_WARN_STATE_NAME)


def cached_warn_pct(path: Path | None = None) -> float | None:
    return disk_events.read_warn_pct(path or warn_state_path())


def record_observe_response(result: dict | None, *, state: Path | None = None,
                            spool_file: Path | None = None) -> None:
    """Cache the observe response's `disk_warn_pct`. None (the POST did not land) changes nothing; a
    response without a usable value removes both files. Never raises."""
    if result is None or not host_platform.is_linux():
        return
    state = state or warn_state_path()
    spool_file = spool_file or disk_events.WARN_PCT_FILE
    raw = result.get("disk_warn_pct") if isinstance(result, dict) else None
    try:
        value = float(raw) if raw is not None and not isinstance(raw, bool) else None
    except (TypeError, ValueError):
        value = None
    if value is not None and not 0 < value <= 100:
        value = None
    for path, mode in ((state, 0o640), (spool_file, 0o644)):
        try:
            if value is None:
                path.unlink(missing_ok=True)
                continue
            text = f"{value:g}\n"
            try:
                if path.read_text() == text:
                    continue
            except OSError:
                pass
            env_helpers.atomic_write_text(path, text, mode=mode)
        except OSError as exc:
            log.debug("disk-reclaim: could not update %s: %s", path, exc)


class DiskReclaimTrigger:
    """The `disk-reclaim` step: ask for a pass every `SCHEDULED_INTERVAL_S`, and on every step while the root
    disk is at or above the cached warn %; then drain the report spool."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 used: Callable[[], float] = lambda: disk_events.used_pct("/"),
                 warn: Callable[[], float | None] = cached_warn_pct,
                 marker: Callable[[], Path] = marker_path,
                 enabled: Callable[[], bool] = triggers_enabled,
                 drain: Callable[[], int] | None = None) -> None:
        self._clock, self._used, self._warn, self._marker, self._enabled = clock, used, warn, marker, enabled
        self._drain = drain if drain is not None else disk_events.drain_spool
        self._last_write: float | None = None

    def due(self) -> str | None:
        """`warn`, `scheduled`, or None — what a marker written now would be for."""
        try:
            used = self._used()
        except OSError:
            used = None
        warn = self._warn()
        if used is not None and warn is not None and used >= warn:
            return "warn"
        if self._last_write is None or self._clock() - self._last_write >= SCHEDULED_INTERVAL_S:
            return "scheduled"
        return None

    def tick(self) -> str | None:
        """One step, synchronously: maybe write the marker, then drain. Returns what was asked for."""
        asked = None
        if self._enabled():
            asked = self.due()
            if asked is not None:
                target = self._marker()
                try:
                    # `write_bytes(b"")`, not `touch()`: the marker must be EMPTY.
                    target.write_bytes(b"")
                    self._last_write = self._clock()
                    log.info("disk-reclaim: asked for a %s pass (%s)", asked, target)
                except OSError as exc:
                    log.warning("disk-reclaim: could not create %s: %s", target, exc)
                    asked = None
        try:
            self._drain()
        except Exception as exc:  # noqa: BLE001 — an upload failure must never cost the next trigger
            log.warning("disk-reclaim: spool drain failed: %s", exc)
        return asked

    async def step(self) -> None:
        import asyncio  # noqa: PLC0415 — only the async wrapper needs it
        await asyncio.to_thread(self.tick)
