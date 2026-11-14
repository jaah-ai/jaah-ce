"""The control plane's two boot invariants: ONE Granian worker per task, and the GIL OFF (spec R10, issue #897).

ONE WORKER. A peer's address `ip:port` names ONE process to the other control-plane tasks (`app/cp_peers.py`)
and the `LISTEN cockpit_events` fan-out reaches the subscribers of one process (`app/events.py`). Fail-closed at
boot: `entrypoint.sh` calls `assert_single_api_worker(argv)` before `exec granian`; `create_app`'s env-only call is
the backup. Scale is more TASKS, never more workers.

GIL OFF. The image runs free-threaded CPython so the CPU offload threads run in parallel. A C extension built
without free-threading support silently RE-ENABLES the GIL when it is imported, so the check runs twice:
`entrypoint.sh` refuses an interpreter that is not a free-threaded build (`assert_free_threaded_build`), and both
apps' lifespans refuse a process whose imports switched the GIL back on (`assert_gil_off`). `PYTHON_GIL=0` is never
set: it would mask that extension instead of failing the boot.

STDLIB ONLY, and it must stay so: `entrypoint.sh` imports this in an interpreter of its own on every boot, and
importing anything of the app's graph there cost ~4s of each deploy's boot.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
import sysconfig
from typing import Optional

log = logging.getLogger("app.worker_guard")

# Granian reads its worker count from `--workers` or this variable (click's `envvar`); the flag wins.
_WORKER_COUNT_ENV_VARS = ("GRANIAN_WORKERS",)


def _worker_count_from_argv(argv: Optional[list[str]]) -> Optional[int]:
    """The Granian `--workers` value from argv, or None. Last-wins on a repeat (as click does), so
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
    """Configured Granian worker count. Precedence (click): `--workers`, then `GRANIAN_WORKERS`, else 1; a
    malformed value reads as 1 so it never wedges startup (Granian itself refuses it)."""
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
    """Raise `RuntimeError` when configured for more than one Granian worker (a peer address names one
    process to the other control-plane tasks, and the `app/events.py` LISTEN fan-out reaches one process).
    No-op on the single-worker default."""
    workers = resolve_declared_worker_count(argv)
    if workers > 1:
        msg = (
            f"control-plane API is configured for {workers} Granian workers, but a control-plane peer's "
            "address ip:port names ONE process to the other tasks (app/cp_peers.py) and the cockpit_events "
            "LISTEN fan-out (app/events.py) reaches the subscribers of one process, so they require exactly "
            "ONE worker per task — scale by running more tasks, not more workers. Run a single worker "
            "(unset GRANIAN_WORKERS, pass --workers 1)."
        )
        log.error(msg)
        raise RuntimeError(msg)
    log.info("single-API-worker invariant OK (declared Granian workers=%d)", workers)


def is_free_threaded_build() -> bool:
    """True on a free-threaded (`t`) CPython build, GIL on or off."""
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def _python_label() -> str:
    return f"{platform.python_version()}{'t' if is_free_threaded_build() else ''}"


def assert_free_threaded_build() -> None:
    """Raise `RuntimeError` unless this interpreter is a free-threaded build. `entrypoint.sh`'s check: the
    deployed image must never serve from a GIL build, whatever installed it."""
    if not is_free_threaded_build():
        msg = (
            f"control-plane API refuses to boot on python {_python_label()}: it is not a free-threaded build, so "
            "the CPU offload threads would serialize on the GIL (the api image installs python-3.14t)."
        )
        log.error(msg)
        raise RuntimeError(msg)


def assert_gil_off() -> None:
    """On a free-threaded build, raise `RuntimeError` when the GIL is ON — an imported C extension without
    free-threading support switched it back on — and log `free-threaded OK (python X.Y.Zt, GIL off)` when it
    is off. Called from both lifespans, after every import of the serving process. A GIL build (a host-side
    test interpreter) is not this check's business: `assert_free_threaded_build` refuses that at boot."""
    if not is_free_threaded_build():
        log.info("GIL check skipped: python %s is not a free-threaded build", _python_label())
        return
    if sys._is_gil_enabled():  # type: ignore[attr-defined]  # 3.13+
        msg = (
            f"control-plane API refuses to serve: python {_python_label()} is free-threaded but the GIL is ON — "
            "an imported C extension lacks free-threading support (Python warns which one at import). Build it "
            "for free-threading or fall back to python-3.14t; never set PYTHON_GIL=0 to mask it."
        )
        log.error(msg)
        raise RuntimeError(msg)
    log.info("free-threaded OK (python %s, GIL off)", _python_label())


def assert_threads_start_clean() -> None:
    """Raise `RuntimeError` when a new thread would INHERIT its creator's contextvars (`sys.flags.thread_inherit_context`,
    on by default on free-threaded CPython). The tenancy scope is a contextvar (`app/tenancy/seam.py`): inherited, a
    pool thread first spawned inside one org's request would run every later job under that org's scope. The image
    sets `PYTHON_THREAD_INHERIT_CONTEXT=0` (the api image's Dockerfile); this refuses a process started without it.
    Called from both lifespans beside `assert_gil_off`."""
    if getattr(sys.flags, "thread_inherit_context", 0):
        msg = (
            "control-plane API refuses to serve: new threads inherit their creator's contextvars "
            "(sys.flags.thread_inherit_context=1), which would carry one request's tenancy scope into later work on "
            "the same thread. Start it with PYTHON_THREAD_INHERIT_CONTEXT=0, as the api image does."
        )
        log.error(msg)
        raise RuntimeError(msg)
