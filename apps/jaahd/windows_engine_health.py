"""Tell the control plane when a Windows node's container engine is not serving, so its Health verdict says so.

The `jaah-engine` service (`JaahService.cs`) runs `docker info` through its pipe
every 60 s and writes `run\\engine-ready` (`ok <version> <time>`) on success, deleting it on failure. A file
missing, not `ok`, or older than `STALE_SECONDS` is an engine that is not serving: WSL 2 could not start its
VM, the distro import failed, dockerd died.

A Windows node has no journal for `node_logs.py` to ship, so this step posts its one warning line itself to
the same `POST /api/agent/node-logs`. The control plane reads it as the `engine_unready` finding
(`apps/api/app/node_log_findings.py`, which holds `MARK`'s twin), active while the line keeps arriving and
cleared once it stops. Posted every pass while the condition lasts, inside that finding's window; nothing is
posted while the engine serves, nor in a fresh daemon's first `GRACE_SECONDS` before the engine has served once
(a first start imports the distro and downloads dockerd, as long as the installer's own 20-minute wait). A failed
POST is retried by the next pass; a 503 (no log group on the control plane) still recorded the finding.
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import control_plane_api as control_plane_api_mod
import host_platform
from host_platform import windows as host_platform_windows

log = logging.getLogger("jaahd.windows_engine_health")

INTERVAL_SECONDS = 300.0
# Three missed `docker info` passes of the engine service's 60 s loop.
STALE_SECONDS = 180.0
# The API's classifier matches this text (`node_log_findings._ENGINE_MARK`).
MARK = "container engine not ready: "
# The installer's engine wait, plus a pass.
GRACE_SECONDS = 1500.0
UNIT = "jaah-engine"
ENGINE_LOG = f"{host_platform_windows.paths().log_dir}\\engine.log"


def ready_file() -> Path:
    return Path(host_platform.paths().run_dir) / "engine-ready"


def problem(path: Path, now: float) -> Optional[str]:
    """Why the engine is not serving, or None when `path` says it answered within `STALE_SECONDS`."""
    try:
        age = now - path.stat().st_mtime
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return f"docker info has not answered (no {path.name}) - see {ENGINE_LOG}"
    except OSError as exc:
        return f"{path.name} is unreadable ({exc.strerror or exc}) - see {ENGINE_LOG}"
    if not text.startswith("ok "):
        return f"{path.name} does not say ok - see {ENGINE_LOG}"
    if age > STALE_SECONDS:
        return f"docker info last answered {int(age)}s ago - see {ENGINE_LOG}"
    return None


def post_warning(post: Callable[[dict], dict], unit: str, message: str, now: float) -> bool:
    """Log `message` and post it as one warning line of `unit` to the node-log route, where the control plane's
    classifier (`node_log_findings`) reads it as a Health finding. Whether it was recorded; a failure is retried by
    the caller's next pass."""
    log.warning(message)
    stamp = datetime.fromtimestamp(now, tz=timezone.utc)
    # The cursor is this line's own: the route only echoes it back, and nothing here advances on it.
    batch = {"cursor": f"{unit}-{int(now)}",
             "entries": [{"ts": stamp.isoformat(), "unit": unit, "priority": 4, "message": message}]}
    try:
        post(batch)
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            return True
        log.warning("%s: POST failed (HTTP %s); retrying next pass", unit, exc.code)
        return False
    except Exception as exc:  # noqa: BLE001 — any refusal or transport failure: the next pass re-sends
        log.warning("%s: POST failed (%s); retrying next pass", unit, exc)
        return False
    return True


class EngineHealthReporter:
    """One `PeriodicStep`: post the warning line while the engine is not serving."""

    def __init__(
        self,
        *,
        path: Callable[[], Path] = ready_file,
        post: Callable[[dict], dict] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._post = post or control_plane_api_mod.agent_api_node_logs
        self._clock = clock
        self._started = clock()
        self._served = False

    def pass_once(self) -> bool:
        """True when a warning was posted."""
        now = self._clock()
        why = problem(self._path(), now)
        if why is None:
            self._served = True
            return False
        if not self._served and now - self._started < GRACE_SECONDS:
            return False
        return post_warning(self._post, UNIT, MARK + why, now)

    async def step(self) -> None:
        await asyncio.to_thread(self.pass_once)
