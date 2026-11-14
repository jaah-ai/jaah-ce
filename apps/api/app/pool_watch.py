"""The pool's saturation watch: the one "this task is wedged" signal, and the thread dump that names the holders.

THE POOL. The control plane has one connection pool per process: the async engine's (`app/db_async.py`, built on
the serving loop by `db_async.start()` in `app/main.py::_lifespan` before this watch starts; lane `async`). Every
request, cron body, bridged sync session and DBOS step draws on it. Its `WatchedAsyncQueuePool` counts each
timed-out checkout here (`app/db.py::WatchedPoolMixin`). Before the pool is built — or in a process where it is
sized 0 — the watch reads zeros, which are never saturated.

WHY. When every pooled connection is checked out and none comes back, every request times out at the ALB while
`/api/health` (async, no DB — `app/main.py`) would stay green, so ECS would keep a wedged task until an operator
stopped it. And the CPU profile (`app/cpu_profile.py`) charges only threads that used CPU, so it cannot show a
blocked holder; a thread dump can.

WHAT IT MEASURES. A lifespan actor samples the pool every `SAMPLE_SECONDS`: the pool is SATURATED while
`checkedout >= pool_size + max_overflow` (every connection the environment allows is out —
`app/config.py::db_pool_sizes`), and WEDGED while it is saturated AND no connection came back since the last
sample (the engine's `checkin` events, counted here). A flood turns connections over and is saturated but never
wedged; a stall holds every connection and returns none. `saturated_for()` / `wedged_for()` are how long each has
been continuously true, 0.0 otherwise. A momentary full pool (a deploy's overlap, a console poll wave) is normal
and clears within seconds.

WHAT IT DOES WITH IT.
 - `wedged()`: wedged for `HEALTH_FAIL_AFTER_SECONDS` or longer. `GET /api/health` answers 503 then, so the
   ALB's probes (5s x 3 misses) take the task out and ECS replaces it, as it would a crash. The verdict is a
   memory read; the probe never touches the pool. Saturation alone never fails the probe: a request flood must
   stay slow, not become a fleet-wide restart.
 - `starved()`: saturated for `STARVED_AFTER_SECONDS` or longer while, over the last `STARVED_WINDOW_SECONDS`,
   at least `STARVED_MIN_TIMEOUTS` checkouts TIMED OUT and more timed out than connections came back. The other
   stall shape: holders each wait for a second connection, give up after `pool_timeout` and hand theirs back, so
   a connection comes back every few seconds and `wedged()` never holds while the pool stays full and fails
   hundreds of checkouts a minute. A busy pool that turns over and times out the odd checkout serves far more
   than it fails, so it is never starved: one task's 503 must not push its peer over too.
   `unhealthy()` (wedged or starved) is what `/api/health` answers.
 - Every sample that saw new timeouts logs one `pool watch: the async pool is starving` line.
 - The backstop, on ECS only (`ECS_CONTAINER_METADATA_URI_V4`, set by the ECS agent — never a test run or a
   local stack): unhealthy for `EXIT_AFTER_SECONDS` without the ALB having replaced the task (a probe that
   cannot reach it, a target group that never acts), the process dumps every thread in-process and exits, so
   ECS starts a fresh task as it would after a crash.
 - After `DUMP_AFTER_SECONDS` of saturation, and then at most once per `DUMP_INTERVAL_SECONDS` whatever the
   episodes, every Python thread's stack is logged line by line under logger `app.pool_dump`
   (`dump_threads_in_process`: idle threads included, since that is the holders' stacks; a frame is a file, a
   line and a function, never a value), after one JSON header line with the pool's numbers:

     {"pool_dump": "start", "pid": 7, "lane": "async", "checkedout": 30, "capacity": 30, "saturated_for": 6.2, "wedged_for": 6.2}

   Query it (CloudWatch Logs Insights, the control plane's `/ecs/<name>-cp` group):
     filter @message like /pool_dump|^Thread |^    / | sort @timestamp asc

OFF-SWITCH: none; a failure here is a log line, never a crash — the one deliberate exit is the backstop above.
The dump is taken in-process (`sys._current_frames`, Python frames only): py-spy cannot read the free-threaded
CPython the api runs (`app/cpu_profile.py` says why).

THE METRICS. Once every `METRICS_INTERVAL_SECONDS` the sampler writes one Embedded Metric Format line
(`app/metrics_emf.py::pool_line`, dimension `Lane=async`: the interval's peak checked-out and overflow figures,
its timeouts, the median cost of `release_route_connection`'s round trips, and the sessions that had to open on
`app/db.py::offline_engine` while the loop served) when `METRICS_NAMESPACE` is set, so CloudWatch can alarm on
the pool. Per process: each control-plane task writes its own, and an alarm reads their `Maximum`/`Sum`.

THE THREADS live here too. `apply_thread_limit` sizes AnyIO's default threadpool to `API_THREAD_LIMIT`
(`app/config.py::request_thread_limit`, default 200): it runs sync dependencies (`current_principal`) and
`run_in_threadpool` callers, whose sessions reach the pool through the bridge, so a thread does not stand for a
connection. `apply_to_thread_lane` sizes and names the loop's default executor for `asyncio.to_thread`.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import statistics
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional

import anyio

from app.config import request_thread_limit
from app.metrics_emf import POOL_LANE

SAMPLE_SECONDS = 1.0
#: A dump this soon after saturation begins still catches the holders of a short stall.
DUMP_AFTER_SECONDS = 5.0
#: A wedge that lasts gets one dump a minute (~40 threads x ~30 frames each).
DUMP_INTERVAL_SECONDS = 60.0
#: Twice the ALB's time to declare a target unhealthy (3 x 5s), so only a stall, never a burst, fails the probe.
HEALTH_FAIL_AFTER_SECONDS = 30.0
#: Twice `HEALTH_FAIL_AFTER_SECONDS`: a pool full AND failing more checkouts than it serves this long is wedged.
STARVED_AFTER_SECONDS = 60.0
#: The window timeouts and checkins are counted over.
STARVED_WINDOW_SECONDS = 60.0
#: Fewer timeouts than this in the window is noise, whatever the turnover (a real stall runs 300+ a minute).
STARVED_MIN_TIMEOUTS = 10
#: Set by the ECS agent in every task; the backstop's exit is armed only where it is.
ECS_MARKER_ENV = "ECS_CONTAINER_METADATA_URI_V4"
#: Unhealthy this long and still serving: the ALB (15s to act) has not replaced the task, so it ends itself.
EXIT_AFTER_SECONDS = 90.0
#: The process's exit status when the backstop fires (EX_SOFTWARE), told apart from a crash in ECS's stop reason.
EXIT_STATUS = 70
#: How often the pool's figures are written as EMF (`app/metrics_emf.py::pool_line`): CloudWatch's finest
#: standard resolution.
METRICS_INTERVAL_SECONDS = 60.0

#: The pool's lane (`app/db.py::WatchedPoolMixin.lane`): the EMF `Lane` dimension value and the dump header's
#: `lane`, a contract with the monitoring terraform and its dashboards (`app/metrics_emf.py::POOL_LANE`).
ASYNC = POOL_LANE

dump_log = logging.getLogger("app.pool_dump")
dump_log.setLevel(logging.INFO)
log = logging.getLogger(__name__)


@dataclass
class SaturationTracker:
    """The pure part: fed one sample at a time, it says how long the pool has been saturated, how long it
    has been wedged (saturated with nothing returned between samples), and when a dump is due. Monotonic
    seconds throughout. `last_dump_at` outlives an episode: the dump rate is a global bound, so pulsed
    saturation cannot dump every few seconds."""

    #: The bounds, per tracker: a test shortens its own without touching the lifespan watcher's.
    dump_after: float = DUMP_AFTER_SECONDS
    dump_interval: float = DUMP_INTERVAL_SECONDS
    saturated_since: Optional[float] = None
    wedged_since: Optional[float] = None
    last_dump_at: Optional[float] = None
    #: When each recent checkout timed out (`count_checkout_timeout`), oldest first, kept for the window.
    timeouts: "collections.deque[float]" = field(default_factory=collections.deque, repr=False)
    #: `(when, running checkin count)` per sample, kept for the window.
    checkin_marks: "collections.deque[tuple[float, int]]" = field(default_factory=collections.deque, repr=False)
    _last_checkins: int = field(default=0, repr=False)
    #: Every timeout ever counted, never trimmed: the sampler logs and publishes the ones new since it last looked.
    timeouts_total: int = 0

    def observe(self, checkedout: int, capacity: int, checkins: int, now: float) -> bool:
        """Record one sample (`checkins` is the engine's running checkin count); True when a dump is due now
        (the caller then calls `dumped(now)`)."""
        returned = checkins - self._last_checkins
        self._last_checkins = checkins
        self.checkin_marks.append((now, checkins))
        self._trim(now)
        if capacity <= 0 or checkedout < capacity:
            self.saturated_since = None
            self.wedged_since = None
            return False
        if self.saturated_since is None:
            self.saturated_since = now
        if returned > 0:
            self.wedged_since = None
        elif self.wedged_since is None:
            self.wedged_since = now
        if now - self.saturated_since < self.dump_after:
            return False
        return self.last_dump_at is None or now - self.last_dump_at >= self.dump_interval

    def dumped(self, now: float) -> None:
        self.last_dump_at = now

    def saturated_for(self, now: float) -> float:
        return 0.0 if self.saturated_since is None else max(0.0, now - self.saturated_since)

    def wedged_for(self, now: float) -> float:
        return 0.0 if self.wedged_since is None else max(0.0, now - self.wedged_since)

    def _trim(self, now: float) -> None:
        horizon = now - STARVED_WINDOW_SECONDS
        while self.timeouts and self.timeouts[0] < horizon:
            self.timeouts.popleft()
        # Keep the newest mark at or before the horizon: the window's checkins are counted from it.
        while len(self.checkin_marks) > 1 and self.checkin_marks[1][0] <= horizon:
            self.checkin_marks.popleft()

    def timed_out(self, now: float) -> None:
        self.timeouts.append(now)
        self.timeouts_total += 1

    def starved(self, now: float) -> bool:
        """Saturated for `STARVED_AFTER_SECONDS`, with the window's timeouts at least `STARVED_MIN_TIMEOUTS` and
        more than the connections that came back in it (the module docstring)."""
        if self.saturated_for(now) < STARVED_AFTER_SECONDS:
            return False
        self._trim(now)
        failed = len(self.timeouts)
        served = self.checkin_marks[-1][1] - self.checkin_marks[0][1] if self.checkin_marks else 0
        return failed >= STARVED_MIN_TIMEOUTS and failed > served

    def unhealthy(self, now: float) -> bool:
        """The one verdict `/api/health` and the backstop share: wedged past the threshold, or starved."""
        return self.wedged_for(now) >= HEALTH_FAIL_AFTER_SECONDS or self.starved(now)


#: The pool's tracker: what the lifespan watcher writes and the health verdicts read.
_tracker = SaturationTracker()


def _now() -> float:
    """The watch's clock (monotonic seconds); its own name so a test can fake it without touching the loop's."""
    return time.monotonic()


class _Counter:
    """A running count. Connections come back from the loop and from bridged threads at once, and on free-threaded
    CPython a bare `+=` from two of them loses increments (there is no GIL to keep the read-modify-write whole)."""

    def __init__(self) -> None:
        self.value = 0
        self._lock = threading.Lock()

    def add(self) -> None:
        with self._lock:
            self.value += 1


_checkins = _Counter()


def _count_checkin(*_args) -> None:
    """Engine `checkin` listener: one more connection returned to the pool."""
    _checkins.add()


def _pool_engine():  # type: ignore[no-untyped-def]
    """The pool's sync engine (`db_async.pooled_engine().sync_engine`) with the checkin counter on it, or None
    while the pool is not built (before the lifespan's `db_async.start()`, or sized 0). Listens on whichever engine
    is current, so a pool built after the watch started, or rebuilt, is still counted. The app's own pool arrives
    already counted (`db_async._build_pooled` wires it before publishing), so this listen only fires for an engine
    built elsewhere (a test's own pool)."""
    from sqlalchemy import event

    from app import db_async

    pooled = db_async.pooled_engine()
    if pooled is None:
        return None
    engine = pooled.sync_engine
    if not event.contains(engine, "checkin", _count_checkin):
        event.listen(engine, "checkin", _count_checkin)
    return engine


def pool_numbers() -> tuple[int, int, int]:
    """`(checkedout, capacity, checkins)` of the pool. Capacity is the environment's ceiling
    (`pool_size + max_overflow`). `checkins` is the running count of connections returned since the listener was
    installed. `(0, 0, checkins)` while the pool is not built: capacity 0 is never saturated."""
    engine = _pool_engine()
    if engine is None:
        return 0, 0, _checkins.value
    pool = engine.pool
    capacity = pool.size() + max(0, int(getattr(pool, "_max_overflow", 0)))
    return pool.checkedout(), capacity, _checkins.value


def overflow_in_use() -> int:
    """Connections the pool has open beyond its `pool_size` right now (QueuePool's `overflow()` counts up from
    `-pool_size`); 0 while the pool is not built."""
    engine = _pool_engine()
    return 0 if engine is None else max(0, int(engine.pool.overflow()))


def saturated_for() -> float:
    """Seconds the pool has been continuously saturated; 0.0 when it is not."""
    return _tracker.saturated_for(_now())


def wedged_for() -> float:
    """Seconds the pool has been continuously saturated with no connection returned; 0.0 when it is not."""
    return _tracker.wedged_for(_now())


def wedged() -> bool:
    """Wedged for `HEALTH_FAIL_AFTER_SECONDS` or longer."""
    return wedged_for() >= HEALTH_FAIL_AFTER_SECONDS


def starved() -> bool:
    """Saturated for `STARVED_AFTER_SECONDS` or longer while checkouts keep timing out (the module docstring)."""
    return _tracker.starved(_now())


def unhealthy() -> bool:
    """The health verdict `/api/health` answers: wedged or starved (`SaturationTracker.unhealthy`)."""
    return wedged() or starved()


def count_checkout_timeout(lane: str = ASYNC) -> None:
    """One checkout of the pool timed out (`app/db.py::WatchedPoolMixin`, once per checkout). A lane other than
    `ASYNC` is ignored: this runs inside the pool's own `TimeoutError`, which must reach the caller."""
    if lane == ASYNC:
        _tracker.timed_out(_now())


def dump_threads_in_process(*, checkedout: int, capacity: int, saturated: float, wedged: float) -> None:
    """The pool dump, taken from inside the process: every Python thread's stack by name and id, framed by a
    JSON header and footer. Frames only — a file, a line, a function and its source line —
    never a value."""
    pid = os.getpid()
    names = {t.ident: t.name for t in threading.enumerate()}
    dump_log.info(json.dumps({
        "pool_dump": "start", "pid": pid, "lane": ASYNC, "checkedout": checkedout, "capacity": capacity,
        "saturated_for": round(saturated, 1), "wedged_for": round(wedged, 1), "via": "in-process",
    }))
    for ident, frame in sys._current_frames().items():
        dump_log.info(f"Thread {ident} ({names.get(ident, '?')})")
        for entry in traceback.format_stack(frame):
            for line in entry.rstrip().splitlines():
                dump_log.info(f"    {line.strip()}")
    dump_log.info(json.dumps({"pool_dump": "end", "pid": pid}))


#: `PoolWatch(exit=...)`'s default: `os._exit` on ECS, else no exit.
_ON_ECS = object()


def _offline_while_serving() -> int:
    from app import db

    return db.offline_while_serving


class PoolWatch:
    """The sampler: one per process in production (`start_pool_watch`), one per test when a test drives it.
    Every dependency is an attribute so a test substitutes its own clock, sleep, pool numbers and dump
    instead of patching module globals a production watcher may be using at the same time (the session
    TestClient runs the lifespan, so one is)."""

    def __init__(self, tracker: SaturationTracker, *, sample_seconds: float = SAMPLE_SECONDS, numbers=None,
                 clock=None, sleep=None, dump_in_process=None, exit=_ON_ECS,
                 overflow=None, offline=None, emit=None) -> None:
        self.tracker = tracker
        self.sample_seconds = sample_seconds
        self.numbers = numbers or pool_numbers
        #: `overflow()`: connections open past the pool's `pool_size` now.
        self.overflow = overflow or overflow_in_use
        #: `offline()`: the running count of sessions opened on the offline engine while the loop served.
        self.offline = offline or _offline_while_serving
        #: `emit(figures)`: where the pool metrics go; the default writes them to `app.metrics` under a namespace.
        self.emit = emit or _emit_pool_metrics
        self.clock = clock or _now
        self.sleep = sleep or asyncio.sleep
        self.dump_in_process = dump_in_process or dump_threads_in_process
        #: None: the backstop only logs. The default exits only on ECS (`ECS_MARKER_ENV`), never a test run or a
        #: local stack; a test passes its own callable, or None.
        self.exit = (os._exit if os.environ.get(ECS_MARKER_ENV) else None) if exit is _ON_ECS else exit
        self.dump_task: Optional[asyncio.Task] = None
        self.unhealthy_since: Optional[float] = None
        #: The timeouts already logged, and where the next metrics interval's timeouts and offline sessions count
        #: from. Seeded from the running totals: the module's tracker outlives a watch (a test's lifespan, a restart).
        self.logged_timeouts = tracker.timeouts_total
        self.metrics_from = tracker.timeouts_total
        self.offline_from = self.offline()
        #: The interval's `(peak checked out, peak overflow)`.
        self.peak = (0, 0)
        self.metrics_due_at: Optional[float] = None

    def _backstop(self, checkedout: int, capacity: int, now: float) -> None:
        """Unhealthy for `EXIT_AFTER_SECONDS`: dump in-process (synchronously, the loop is about to end) and exit.
        `os._exit`, not `sys.exit`: a wedged process's shutdown would wait on the very threads that hold the
        pool."""
        if not self.tracker.unhealthy(now):
            self.unhealthy_since = None
            return
        if self.unhealthy_since is None:
            self.unhealthy_since = now
        if now - self.unhealthy_since < EXIT_AFTER_SECONDS:
            return
        if self.exit is None:
            if now - self.unhealthy_since < EXIT_AFTER_SECONDS + self.sample_seconds:
                log.critical("pool watch: unhealthy for %.0fs — not on ECS, so not exiting", now - self.unhealthy_since)
            return
        log.critical(
            "pool watch: unhealthy for %.0fs (%d/%d checked out, saturated %.0fs, wedged %.0fs) and still serving "
            "— exiting %d so ECS replaces this task",
            now - self.unhealthy_since, checkedout, capacity, self.tracker.saturated_for(now),
            self.tracker.wedged_for(now), EXIT_STATUS,
        )
        with contextlib.suppress(Exception):
            self.dump_in_process(checkedout=checkedout, capacity=capacity,
                                 saturated=self.tracker.saturated_for(now), wedged=self.tracker.wedged_for(now))
        for handler in logging.getLogger().handlers:
            with contextlib.suppress(Exception):
                handler.flush()
        self.exit(EXIT_STATUS)

    async def run(self) -> None:
        """Sample forever. The dump runs as its own task so sampling never pauses behind a slow ptrace (a
        stale `saturated_since` would otherwise hold a 503 past the pool's recovery). A pool that cannot be read
        (a pool class without the counters) ends the watch with one warning, not a traceback a second."""
        while True:
            await self.sleep(self.sample_seconds)
            try:
                checkedout, capacity, checkins = self.numbers()
            except Exception as exc:  # noqa: BLE001 — see the docstring
                log.warning("pool watch: cannot read the app pool (%s) — the watch is off for this process", exc)
                return
            now = self.clock()
            due = self.tracker.observe(checkedout, capacity, checkins, now)
            self._backstop(checkedout, capacity, now)
            self._sampled(checkedout, capacity, now)
            if due:
                self._dump_if_free(checkedout, capacity, now)
            self._publish_if_due(now)

    def _sampled(self, checkedout: int, capacity: int, now: float) -> None:
        """Log the checkouts that timed out since the last sample (one line a second at most), and keep the
        interval's peaks for the metrics line."""
        tracker = self.tracker
        new = tracker.timeouts_total - self.logged_timeouts
        self.logged_timeouts = tracker.timeouts_total
        if new > 0:
            log.warning(
                "pool watch: the %s pool is starving — %d checkout(s) timed out since the last sample, %d in the "
                "last %.0fs (%d/%d checked out, saturated for %.0fs)",
                ASYNC, new, len(tracker.timeouts), STARVED_WINDOW_SECONDS, checkedout, capacity,
                tracker.saturated_for(now),
            )
        try:
            overflow = self.overflow()
        except Exception:  # noqa: BLE001 — a figure missing from one metrics line is not worth a traceback a second
            overflow = 0
        self.peak = (max(self.peak[0], checkedout), max(self.peak[1], overflow))

    def _dump_if_free(self, checkedout: int, capacity: int, now: float) -> None:
        if self.dump_task is not None and not self.dump_task.done():
            return   # the previous dump is still running; `last_dump_at` keeps the cadence
        tracker = self.tracker
        tracker.dumped(now)   # before the dump: a slow or failing dump must not fire every sample
        log.warning(
            "pool watch: the %s pool has been saturated for %.0fs (%d/%d checked out, wedged for %.0fs) — "
            "dumping every thread's stack under logger app.pool_dump",
            ASYNC, tracker.saturated_for(now), checkedout, capacity, tracker.wedged_for(now),
        )
        self.dump_task = asyncio.create_task(self._dump_once(
            checkedout=checkedout, capacity=capacity,
            saturated=tracker.saturated_for(now), wedged=tracker.wedged_for(now),
        ), name="pool-dump")

    def _publish_if_due(self, now: float) -> None:
        """Every `METRICS_INTERVAL_SECONDS`, hand the interval's `(peak checked out, peak overflow, timeouts,
        offline sessions)` to `emit`."""
        if self.metrics_due_at is None:
            self.metrics_due_at = now + METRICS_INTERVAL_SECONDS
        if now < self.metrics_due_at:
            return
        self.metrics_due_at = now + METRICS_INTERVAL_SECONDS
        try:
            offline = self.offline()
        except Exception:  # noqa: BLE001 — as `_sampled`'s overflow
            offline = self.offline_from
        figures = (*self.peak, self.tracker.timeouts_total - self.metrics_from, offline - self.offline_from)
        self.metrics_from = self.tracker.timeouts_total
        self.offline_from = offline
        self.peak = (0, 0)
        try:
            self.emit(figures)
        except Exception:  # noqa: BLE001 — see the module docstring: the watch never takes the api down
            log.exception("pool watch: writing the pool metrics failed")

    async def _dump_once(self, **numbers) -> None:
        try:
            self.dump_in_process(**numbers)
        except Exception:  # noqa: BLE001 — see the module docstring: the watch never takes the api down
            log.exception("pool watch: thread dump failed; next attempt in %.0fs", self.tracker.dump_interval)

    async def cancel(self) -> None:
        task = self.dump_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def apply_thread_limit() -> int:
    """Size AnyIO's default thread limiter (what `starlette.concurrency.run_in_threadpool` and FastAPI's sync
    dependencies draw on) to `request_thread_limit()`. A thread does not stand for a connection: its sessions
    reach the async pool through the bridge (`app/db_async.py::sync_bind`). Must run on the serving loop: the
    limiter is the running backend's."""
    limit = request_thread_limit()
    anyio.to_thread.current_default_thread_limiter().total_tokens = limit
    return limit


def apply_to_thread_lane() -> int:
    """Give the serving loop a default executor of Python's own default size, sized here once and named, for its
    `asyncio.to_thread` callers. Their sessions draw on the async pool through the bridge
    (`app/db_async.py::sync_bind`). Must run on the serving loop."""
    from concurrent.futures import ThreadPoolExecutor

    workers = min(32, (os.cpu_count() or 1) + 4)
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=workers, thread_name_prefix="to-thread")
    )
    return workers


def take_release_p50_ms() -> Optional[float]:
    """The median of the route-connection releases since the last call, in milliseconds (`app/db.py::
    release_costs`), emptying it; None when nothing was released."""
    from app.db import release_costs

    costs = []
    while release_costs:
        costs.append(release_costs.popleft())
    return statistics.median(costs) * 1000.0 if costs else None


def _emit_pool_metrics(figures: tuple[int, int, int, int]) -> None:
    """Write the pool's interval figures as one EMF line (`app/metrics_emf.py::pool_line`); nothing without a
    namespace."""
    from app import metrics_emf
    from app.config import get_settings

    release_p50_ms = take_release_p50_ms()
    namespace = get_settings().metrics_namespace
    if not namespace:
        return
    checked_out, overflow, timeouts, offline = figures
    metrics_emf.emf_log.info(metrics_emf.pool_line(
        namespace, int(time.time() * 1000), checked_out, overflow, timeouts,
        release_p50_ms=release_p50_ms, offline_while_serving=offline,
    ))


async def start_pool_watch(app) -> None:
    """Lifespan startup hook, after `db_async.start()` built the pool (`app/main.py::_lifespan`). Mirrors the other
    actors there. The watcher's tracker is the module's, so `unhealthy()` (the health route) reads what this
    watcher writes."""
    _pool_engine()   # the checkin counter, from the first connection back
    log.info("pool watch: request threadpool bounded to %d threads", apply_thread_limit())
    log.info("pool watch: asyncio.to_thread runs %d threads", apply_to_thread_lane())
    watch = PoolWatch(_tracker)
    app.state.pool_watch = watch
    app.state.pool_watch_task = asyncio.create_task(watch.run(), name="pool-watch")


async def stop_pool_watch(app) -> None:
    """Lifespan shutdown hook: cancel the sampler and any dump in flight."""
    task = getattr(app.state, "pool_watch_task", None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    watch = getattr(app.state, "pool_watch", None)
    if watch is not None:
        await watch.cancel()
