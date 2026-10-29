"""The single-API-worker invariant, refused before uvicorn starts (spec R10, issue #897).

One uvicorn worker per control-plane task, because a peer's address `ip:port` names ONE process to the other
control-plane tasks (`app/cp_peers.py`) and the `LISTEN cockpit_events` fan-out reaches the subscribers of one
process (`app/events.py`). Fail-closed at boot: `entrypoint.sh` calls `assert_single_api_worker(argv)` before
`exec uvicorn`; `create_app`'s env-only call is the backup.

STDLIB ONLY, and it must stay so: `entrypoint.sh` imports this in an interpreter of its own on every boot, and
importing anything of the app's graph there cost ~4s of each deploy's boot.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("app.worker_guard")

_WORKER_COUNT_ENV_VARS = ("UVICORN_WORKERS", "WEB_CONCURRENCY")


def _worker_count_from_argv(argv: Optional[list[str]]) -> Optional[int]:
    """The uvicorn `--workers` value from argv, or None. Last-wins on a repeat (as click does), so
    `--workers 1 --workers 4` never fails OPEN to 1."""
    if not argv:
        return None
    count: Optional[int] = None
    for i, token in enumerate(argv):
        if token == "--workers":
            raw = argv[i + 1] if i + 1 < len(argv) else None
        elif token.startswith("--workers="):
            raw = token.split("=", 1)[1]
        else:
            continue
        if raw is None or not raw.strip():
            continue
        try:
            count = int(raw.strip())
        except ValueError:
            continue
    return count


def resolve_declared_worker_count(argv: Optional[list[str]] = None) -> int:
    """Configured uvicorn worker count. Precedence (uvicorn 0.30.x): `--workers`, `UVICORN_WORKERS`,
    `WEB_CONCURRENCY`, else 1; a malformed value reads as 1 so it never wedges startup."""
    from_argv = _worker_count_from_argv(argv)
    if from_argv is not None:
        return from_argv
    for name in _WORKER_COUNT_ENV_VARS:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            continue
        try:
            return int(raw.strip())
        except ValueError:
            log.warning(
                "ignoring non-integer %s=%r while checking the single-worker invariant", name, raw
            )
            continue
    return 1


def assert_single_api_worker(argv: Optional[list[str]] = None) -> None:
    """Raise `RuntimeError` when configured for more than one uvicorn worker (a peer address names one
    process to the other control-plane tasks, and the `app/events.py` LISTEN fan-out reaches one process).
    No-op on the single-worker default."""
    workers = resolve_declared_worker_count(argv)
    if workers > 1:
        msg = (
            f"control-plane API is configured for {workers} uvicorn workers, but a control-plane peer's "
            "address ip:port names ONE process to the other tasks (app/cp_peers.py) and the cockpit_events "
            "LISTEN fan-out (app/events.py) reaches the subscribers of one process, so they require exactly "
            "ONE worker per task — scale by running more tasks, not more workers. Run a single worker "
            "(unset UVICORN_WORKERS/WEB_CONCURRENCY, drop --workers)."
        )
        log.error(msg)
        raise RuntimeError(msg)
    log.info("single-API-worker invariant OK (declared uvicorn workers=%d)", workers)
