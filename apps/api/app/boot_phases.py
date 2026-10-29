"""The deployed api's boot, split into phases: one JSON line per phase on stdout, so the CloudWatch log of a
new task says where its boot went.

The deployed boot is two interpreters under one shell (the ECS task definition's
`api_command`): `python -c` migrates and seeds, then the shell execs `./entrypoint.sh`, which execs uvicorn.
The shell stamps `/proc/uptime` into `JAAH_BOOT_T0` before the first and `JAAH_BOOT_SEEDED` before the
second, so every phase is the difference of two readings of ONE clock (`CLOCK_BOOTTIME`, which
`/proc/uptime` reads) and the phases chain end to start: they sum to the `boot_seconds` the ready line
prints, with nothing absorbed and nothing double-counted. The one gap is the first interpreter's own exit
(last `app.seed.main` end -> `JAAH_BOOT_SEEDED`), which the deploy's phase report names.

  JAAH_BOOT_T0 ─ boot.import ─ migrate.upgrade ─ app.seed.main ┊ JAAH_BOOT_SEEDED ─ api.exec ─ app.import
      ─ app.startup ─ dbos.launch ─▶ app.ready

Each line also carries `at` (epoch seconds at the phase's end), which is what lets rollout_phases.py place
the boot between the ECS task's `startedAt` and its healthy mark.

OFF unless the shell set the stamps: tests, the local stack and `python -m app.seed` print nothing. Never
raises: a timing line must not be what stops a boot.
"""
from __future__ import annotations

import json
import os
import time

EVENT_PHASE = "api_boot_phase"
EVENT_READY = "api_boot_ready"

_T0_ENV = "JAAH_BOOT_T0"
_SEEDED_ENV = "JAAH_BOOT_SEEDED"

# The end of the last phase this process reported: the next phase starts here. Set on first use from the
# shell stamp that precedes this interpreter.
_mark: float | None = None


def _stamp(name: str) -> float | None:
    """A `/proc/uptime` reading the shell exported, or None when it did not (or exported garbage)."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return None


def _now() -> float | None:
    """The `/proc/uptime` clock, or None off Linux (where nothing stamps anyway)."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return None


def _emit(record: dict) -> None:
    try:
        print(json.dumps(record), flush=True)
    except Exception:  # noqa: BLE001 — see the module docstring: never the reason a boot stops
        pass


def done(phase: str) -> None:
    """Report `phase` as ending now, starting where this process's previous phase ended.

    The first phase of an interpreter starts at the shell's stamp for it: `JAAH_BOOT_SEEDED` once the
    migrate-and-seed interpreter has run, else `JAAH_BOOT_T0`."""
    global _mark
    t0 = _stamp(_T0_ENV)
    if t0 is None:
        return
    start = _mark if _mark is not None else (_stamp(_SEEDED_ENV) or t0)
    end = _now()
    if end is None:
        return
    _mark = end
    _emit({"event": EVENT_PHASE, "phase": phase, "seconds": round(end - start, 3),
           "since_boot": round(end - t0, 3), "at": round(time.time(), 3)})


def ready() -> None:
    """The last line: the whole boot, shell stamp to the app's startup complete (uvicorn binds next)."""
    t0, end = _stamp(_T0_ENV), _now()
    if t0 is None or end is None:
        return
    _emit({"event": EVENT_READY, "phase": "app.ready", "boot_seconds": round(end - t0, 3),
           "at": round(time.time(), 3)})
