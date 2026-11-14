"""How busy the serving loop is: event-loop lag, and how long a request waits for its DB admission slot.

WHY. The API serves every request from ONE asyncio loop in ONE process per task (`apps/api/entrypoint.sh`). A
process near its one-core ceiling answers a trivial route as slowly as a heavy one, because every request waits its
turn on the loop — and ECS's CPU figure, averaged over the task's vCPUs, never shows it. Lag is that wait, measured
directly; the admission wait is the other queue a request can sit in (`app/db_async.py::RequestAdmission`).

WHAT IT MEASURES.
 - LAG: a lifespan actor sleeps `SAMPLE_SECONDS` on the serving loop and records how much later than asked it woke
   (`LagSampler`). A free loop wakes within a millisecond; a busy one wakes when the callbacks queued ahead of it
   have run.
 - ADMISSION WAIT: every request-path checkout records how long it waited on the admission semaphore, a timed-out
   one included (`record_admission`, called by `RequestAdmission._do_get`). Two clock reads per checkout.

WHAT IT DOES WITH IT. Once every `METRICS_INTERVAL_SECONDS` it writes one Embedded Metric Format line
(`app/metrics_emf.py::loop_line`: mean and peak lag, mean and peak admission wait, admissions, and the interval's
garbage-collector pauses from `app/gc_watch.py`) when
`METRICS_NAMESPACE` is set. Per process: every task writes its own, and CloudWatch's Average over them is what the
loop-lag alarm and the opt-in scale-out policy read (the ECS module's autoscaling terraform).

OFF-SWITCH: none; a failure here is a log line, never a crash.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app import gc_watch

#: Often enough that a minute holds 240 samples, rarely enough to cost nothing: one timer a quarter second.
SAMPLE_SECONDS = 0.25
#: CloudWatch's finest standard resolution, as `app/pool_watch.py`'s.
METRICS_INTERVAL_SECONDS = 60.0

log = logging.getLogger(__name__)


@dataclass
class Interval:
    """A running count, total and peak, in seconds; read and reset once per metrics interval."""

    count: int = 0
    total: float = 0.0
    peak: float = 0.0

    def add(self, seconds: float) -> None:
        seconds = max(seconds, 0.0)
        self.count += 1
        self.total += seconds
        self.peak = max(self.peak, seconds)

    def take(self) -> "Interval":
        taken = Interval(self.count, self.total, self.peak)
        self.count, self.total, self.peak = 0, 0.0, 0.0
        return taken

    @property
    def mean_ms(self) -> float:
        return self.total / self.count * 1000.0 if self.count else 0.0

    @property
    def peak_ms(self) -> float:
        return self.peak * 1000.0


#: The process's admission waits, written on the serving loop by every request-path checkout.
_admissions = Interval()


def record_admission(waited_seconds: float) -> None:
    """One request-path checkout waited this long for its admission slot (`app/db_async.py::RequestAdmission`)."""
    _admissions.add(waited_seconds)


class LagSampler:
    """Sleeps `sample_seconds` at a time and records how late each wake was; publishes each interval through `emit`
    (`(lag, admissions)`, both `Interval`s). The clock and sleep are injectable so a test drives it without a loop
    of its own."""

    def __init__(self, *, sample_seconds: float = SAMPLE_SECONDS, interval_seconds: float = METRICS_INTERVAL_SECONDS,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep,
                 emit: Optional[Callable[[Interval, Interval], None]] = None,
                 admissions: Optional[Interval] = None) -> None:
        self.sample_seconds = sample_seconds
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.sleep = sleep
        self.emit = emit or _emit
        self.admissions = admissions if admissions is not None else _admissions
        self.lag = Interval()
        self.due_at: Optional[float] = None

    def sampled(self, asked_at: float, woke_at: float) -> None:
        """One sample: asked to wake `sample_seconds` after `asked_at`, woke at `woke_at`."""
        self.lag.add(woke_at - asked_at - self.sample_seconds)
        if self.due_at is None:
            self.due_at = woke_at + self.interval_seconds
        if woke_at < self.due_at:
            return
        self.due_at = woke_at + self.interval_seconds
        try:
            self.emit(self.lag.take(), self.admissions.take())
        except Exception:  # noqa: BLE001 — see the module docstring: the watch never takes the api down
            log.exception("loop watch: writing the loop metrics failed")

    async def run(self) -> None:
        while True:
            asked_at = self.clock()
            await self.sleep(self.sample_seconds)
            self.sampled(asked_at, self.clock())


def _emit(lag: Interval, admissions: Interval) -> None:
    """Write one interval as an EMF line (`app/metrics_emf.py::loop_line`) with the collector's pauses over the same
    interval (`app/gc_watch.py`); nothing without a namespace, though the pauses are taken either way."""
    from app import metrics_emf
    from app.config import get_settings

    try:
        collector = gc_watch.take()
    except Exception:  # noqa: BLE001 — a broken collector watch must not cost the loop metrics
        log.exception("gc watch: reading the interval failed; its collector figures are reported as zero")
        collector = gc_watch.GcInterval()
    if collector.errors:
        log.warning("gc watch: %d collection(s) not recorded this interval (hook failed or lock held)", collector.errors)
    namespace = get_settings().metrics_namespace
    if not namespace:
        return
    metrics_emf.emf_log.info(metrics_emf.loop_line(
        namespace, int(time.time() * 1000), lag_mean_ms=lag.mean_ms, lag_max_ms=lag.peak_ms,
        admission_mean_ms=admissions.mean_ms, admission_max_ms=admissions.peak_ms, admissions=admissions.count,
        gc_collections=collector.collections, gc_pause_max_ms=collector.pause_max_ms,
        gc_pause_total_ms=collector.pause_total_ms,
    ))


async def start_loop_watch(app) -> None:
    """Lifespan startup hook (`app/main.py::_lifespan`), on the serving loop."""
    gc_watch.install()
    sampler = LagSampler()
    app.state.loop_watch_task = asyncio.create_task(sampler.run(), name="loop-watch")


async def stop_loop_watch(app) -> None:
    gc_watch.uninstall()
    task = getattr(app.state, "loop_watch_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
