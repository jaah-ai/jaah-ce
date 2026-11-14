"""How long the garbage collector stops the process: every collection's pause, totalled and peaked per interval.

WHY. The API's serving loop shows a stall of roughly 0.6 s about once a minute (`EventLoopLagMsMax`) while its mean
lag stays near 2 ms. The hypothesis is the free-threaded collector: under CPython's free-threaded build a
collection stops every thread, so a full pass over a large heap stops the serving loop for its whole duration. This
module measures that pause so a stall minute and its collector pause sit on one datapoint.

WHAT IT MEASURES. A `gc.callbacks` hook (`GcWatch.callback`) reads `time.perf_counter` at each collection's
`start` and `stop` phase and adds the difference to the interval: collections, total and peak pause, and the
`collected`/`uncollectable` counts the `stop` phase reports. The hook runs on whichever thread triggered the
collection, so it accumulates under a lock it never waits on (`GcWatch.record`) and never raises: a failure or a
skipped record inside it is counted, not propagated.

WHAT IT DOES WITH IT. `app/loop_watch.py::_emit` takes the interval once per loop-metrics interval and writes it in
the SAME EMF line as the loop lag (`app/metrics_emf.py::loop_line`: `GcCollections`, `GcPauseMsMax`,
`GcPauseMsTotal`). The interval's slowest pause of at least `SLOW_PAUSE_MS` also logs one WARNING when the interval
is taken (pause, objects collected, generation), at most once per `SLOW_PAUSE_LOG_SECONDS`, so Logs Insights can
line it up with a stall.

WHAT IT TUNES. `tune` runs once at the end of lifespan startup (`app/main.py::_lifespan`), before the first
request: one `gc.collect` so startup garbage is not kept forever, then `gc.freeze` so the startup heap (modules,
the app, its routes) is not traced by later passes, then the thresholds below. Under the free-threaded 3.14 build
every collection is a full stop-the-world pass, and its pause is a fixed cost (visiting the whole heap, frozen
objects included) plus a cost per unreachable object.

WHERE THE GARBAGE COMES FROM. Cyclic garbage is SQLAlchemy statement construction, per request: a fresh
`aliased(Model)` (≈1,300-2,100 objects each: every column copied onto an alias that points back at it), a
`pg_insert(...).excluded` (an alias of the whole table, ≈300+), and any fresh expression an operator was applied
to (its memoized `comparator` points back at it, keeping the subtree alive until a pass). Smaller sources: the
three `BaseHTTPMiddleware` subclasses (task group and cancelled-scope tracebacks, ≈50 per request) and
`app/db_async.py`'s per-statement task, whose context holds a `_Hold` pointing back at the task.

WHY THE THRESHOLDS. The default trigger (`Python/gc_free_threading.c::gc_should_collect`) waits until net new
containers exceed a quarter of the last pass's heap count (frozen and untracked objects included, so freezing
does not lower it), and then for RSS +10% or deferred containers past 40 x threshold0. With a ~1.2M-object heap
that is a pass per ~700k garbage objects, one long stall. A threshold1 of 0 skips that scaling: a pass starts
whenever net new containers exceed threshold0, so each pass frees about `GC_THRESHOLD0` objects. Measured on the
local stack, one driver (node observe, pool-tick, accounts, node-logs and the pipeline board, 250 rounds):

    thresholds          passes  max pause  total pause  freed per pass
    (10_000, 10, 10)    1-2     303-494 ms 303-515 ms   ~700k
    (200_000, 0, 10)    6       137-165 ms 742-915 ms   ~180k
    (100_000, 0, 10)    13      146 ms     1,733 ms     ~90k
    (50_000, 0, 10)     27      124 ms     2,603 ms     ~43k

The fixed cost (an empty pass: ~50 ms after `tune`, ~80 ms once the unfrozen heap has grown ~58k objects)
dominates below 200k, so a smaller threshold0 buys little pause and multiplies the total. A second `gc.freeze`
after warm-up brings that empty pass to ~65 ms but a loaded pass only from 131 to 124 ms, inside the run-to-run
spread, so the heap is frozen once, at startup.

THE TRADE-OFF. These thresholds buy a shorter max pause with more total pause (more, smaller passes). Watch
`GcPauseMsMax` for the stall a request feels and `GcCollections` (with `GcPauseMsTotal`) for the CPU spent.
"""
from __future__ import annotations

import gc
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

#: A pause this long is a stall a request feels; it logs a WARNING.
SLOW_PAUSE_MS = 250.0
#: At most one slow-pause WARNING per this many seconds.
SLOW_PAUSE_LOG_SECONDS = 60.0
#: Containers allocated (net of those freed) between collections: about the garbage each pass frees, so it bounds
#: the pause. 200,000 is the measured knee (module docstring); raise it only with `GcPauseMs*` data.
GC_THRESHOLD0 = 200_000
#: 0 turns off the free-threaded collector's heap-proportional delay (module docstring), so `GC_THRESHOLD0` alone
#: decides when a pass starts. The cost: a heap that genuinely grows pays one pass per `GC_THRESHOLD0` of growth.
GC_THRESHOLD1 = 0

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GcInterval:
    """One interval's collections: how many, their total and peak pause in ms, what they freed, and hook errors."""

    collections: int = 0
    pause_total_ms: float = 0.0
    pause_max_ms: float = 0.0
    collected: int = 0
    uncollectable: int = 0
    errors: int = 0  # hook failures plus collections skipped because the lock was held


class GcWatch:
    """The accumulator behind the `gc.callbacks` hook; read and reset with `take`. Clocks are injectable for tests."""

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter,
                 wall: Callable[[], float] = time.monotonic,
                 slow_pause_ms: float = SLOW_PAUSE_MS, log_every_seconds: float = SLOW_PAUSE_LOG_SECONDS) -> None:
        self.clock = clock
        self.wall = wall
        self.slow_pause_ms = slow_pause_ms
        self.log_every_seconds = log_every_seconds
        self._lock = threading.Lock()
        self._local = threading.local()
        self._last_warned_at: Optional[float] = None
        # The interval's slowest pause past `slow_pause_ms`: (pause_ms, collected, generation), logged by `take`.
        self._slowest: Optional[tuple[float, int, object]] = None
        # Collections the hook could not record (the lock was held, or the hook failed); read by `take`. A plain
        # counter, off the lock on purpose: a lost increment under a race only under-counts a diagnostic.
        self._dropped = 0
        self._reset()

    def _reset(self) -> None:
        # Constants only: this runs under the lock, where an allocation could start a collection (below).
        self._collections = 0
        self._total_ms = 0.0
        self._max_ms = 0.0
        self._collected = 0
        self._uncollectable = 0
        self._errors = 0

    def callback(self, phase: str, info: dict) -> None:
        """The `gc.callbacks` entry: `start` stamps this thread's clock, `stop` records the pause. Never raises."""
        try:
            if phase == "start":
                self._local.started_at = self.clock()
                return
            started_at = getattr(self._local, "started_at", None)
            if started_at is None:
                return
            self._local.started_at = None
            self.record((self.clock() - started_at) * 1000.0, info)
        except Exception:  # noqa: BLE001 — a hook that raises would surface inside an arbitrary allocation
            self._dropped += 1

    def record(self, pause_ms: float, info: dict) -> None:
        """One finished collection of `pause_ms`; `info` is the `stop` phase's dict.

        NEVER BLOCKS. A collection can start inside any allocation on any thread — including this watch's own
        `take` on the same thread, while it holds the lock — so a blocking acquire here could wait on itself
        forever and freeze the serving loop. A collection that finds the lock taken is counted as dropped instead.
        No logging here either (a collection can start while a logging handler's lock is held): a slow pause is
        logged by `take`."""
        pause_ms = max(pause_ms, 0.0)
        if not self._lock.acquire(blocking=False):
            self._dropped += 1
            return
        try:
            self._collections += 1
            self._total_ms += pause_ms
            self._max_ms = max(self._max_ms, pause_ms)
            self._collected += int(info.get("collected", 0) or 0)
            self._uncollectable += int(info.get("uncollectable", 0) or 0)
            if pause_ms >= self.slow_pause_ms and (self._slowest is None or pause_ms > self._slowest[0]):
                self._slowest = (pause_ms, int(info.get("collected", 0) or 0), info.get("generation"))
        finally:
            self._lock.release()

    def take(self) -> GcInterval:
        """This interval's figures; the next interval starts from nothing. Logs the interval's slowest pause past
        `slow_pause_ms`, at most once per `log_every_seconds`.

        Under the lock it only copies and resets: building the result there could start a collection whose hook
        then finds the lock held (`record`)."""
        with self._lock:
            collections, total_ms, max_ms = self._collections, self._total_ms, self._max_ms
            collected, uncollectable, errors = self._collected, self._uncollectable, self._errors
            slowest, self._slowest = self._slowest, None
            self._reset()
        dropped, self._dropped = self._dropped, 0
        if slowest is not None:
            now = self.wall()
            if self._last_warned_at is None or now - self._last_warned_at >= self.log_every_seconds:
                self._last_warned_at = now
                log.warning("gc watch: slow collection pause_ms=%.1f collected=%d generation=%s", *slowest)
        return GcInterval(collections, total_ms, max_ms, collected, uncollectable, errors + dropped)


#: The process's watch, installed by `install` at lifespan startup.
watch = GcWatch()


def install() -> None:
    """Add the hook to `gc.callbacks` once (`app/main.py::_lifespan` startup); a second call is a no-op."""
    if watch.callback not in gc.callbacks:
        gc.callbacks.append(watch.callback)


def uninstall() -> None:
    """Remove the hook (lifespan shutdown); a no-op when it is absent."""
    while watch.callback in gc.callbacks:
        gc.callbacks.remove(watch.callback)


def tune() -> None:
    """Collect once, freeze the survivors, set `GC_THRESHOLD0`/`GC_THRESHOLD1` (module docstring). Once, at the
    end of lifespan startup: freezing later would also pin request-scoped objects. threshold2 is kept."""
    gc.collect()
    gc.freeze()
    _, _, threshold2 = gc.get_threshold()
    gc.set_threshold(GC_THRESHOLD0, GC_THRESHOLD1, threshold2)
    log.info("gc watch: froze %d objects, threshold=%s", gc.get_freeze_count(), gc.get_threshold())


def take() -> GcInterval:
    return watch.take()
