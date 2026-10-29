"""The app pool's saturation watch: the one "this task is wedged" signal, and the thread dump that names
the holders.

WHY. On 2026-10-14 the main control plane stalled twice: on one of its two tasks every pooled
connection was checked out and idle in transaction for minutes while the database and the CPU sat idle, every
request timed out at the ALB, and `/api/health` (async, no DB — `app/main.py`) stayed green, so ECS kept the
wedged task until an operator stopped it. Nothing recorded which threads held the connections: `py-spy record`
(`app/cpu_profile.py`) drops idle threads, so the CPU profile cannot show a blocked holder.

WHAT IT MEASURES. A lifespan actor samples `app.db.engine.pool` every `SAMPLE_SECONDS`: the pool is SATURATED
while `checkedout >= pool_size + max_overflow` (every connection the environment allows is out —
`app/config.py::db_pool_sizes`), and WEDGED while it is saturated AND no connection came back since the last
sample (the engine's `checkin` events, counted here). A flood turns connections over and is saturated but never
wedged; the stall held every connection and returned none. `saturated_for()` / `wedged_for()` are how long
each has been continuously true, 0.0 otherwise. A momentary full pool (a deploy's overlap, a console poll
wave) is normal and clears within seconds; the stalls lasted a minute and five.

WHAT IT DOES WITH IT.
 - `wedged()`: wedged for `HEALTH_FAIL_AFTER_SECONDS` or longer. `GET /api/health` answers 503 then, so the
   ALB's probes (5s x 3 misses) take the task out and ECS replaces
   it, as it would a crash. The verdict is a memory read; the probe never touches the pool. Saturation alone
   never fails the probe: a request flood must stay slow, not become a fleet-wide restart.
 - `starved()`: saturated for `STARVED_AFTER_SECONDS` or longer while, over the last `STARVED_WINDOW_SECONDS`,
   at least `STARVED_MIN_TIMEOUTS` checkouts TIMED OUT and more timed out than connections came back
   (`app/db.py::WatchedQueuePool` counts each timeout). The second stall shape (main, 2026-10-17):
   requests held one connection and waited for a second, each gave up after `pool_timeout` and handed its
   connection back, so a connection came back every few seconds and `wedged()` never held while the pool
   stayed full and failed hundreds of checkouts a minute for seven minutes. A busy pool that turns over and
   times out the odd checkout (an `asyncio.to_thread` or DBOS burst outside the request threadpool's bound)
   serves far more than it fails, so it is never starved: one task's 503 must not push its peer over too.
   `unhealthy()` (wedged or starved) is what `/api/health` answers.
 - The backstop, on ECS only (`ECS_CONTAINER_METADATA_URI_V4`, set by the ECS agent — never a test run or a
   local stack): unhealthy for `EXIT_AFTER_SECONDS` without the ALB having replaced the task (a probe that
   cannot reach it, a target group that never acts), the process dumps every thread in-process and exits, so
   ECS starts a fresh task as it would after a crash.
 - After `DUMP_AFTER_SECONDS` of saturation, and then at most once per `DUMP_INTERVAL_SECONDS` whatever the
   episodes, `py-spy dump --pid <self>` is logged line by line under logger `app.pool_dump` (every thread,
   idle ones included: that is the holders' stacks; never `--locals`, so a frame is a function and a line,
   no values), after one JSON header line with the pool's numbers:

     {"pool_dump": "start", "pid": 7, "checkedout": 30, "capacity": 30, "saturated_for": 6.2, "wedged_for": 6.2}

   Query it (CloudWatch Logs Insights, the control plane's `/ecs/<name>-cp` group):
     filter @message like /pool_dump|^Thread |^    / | sort @timestamp asc

OFF-SWITCH: none; a failure here is a log line, never a crash — the one deliberate exit is the backstop above.
The dump prefers the py-spy binary (`apps/api/requirements.txt`; native frames too); where it cannot attach — on
main a child process may not ptrace its parent, so every py-spy dump there failed `Permission Denied` — the
same stacks are taken in-process (`sys._current_frames`, Python frames only, still never locals).

THE OTHER LANES. The control plane draws on three pools (`app/db.py`): the request pool above, the identity
pool (auth and node-token reads) and the background pool (lease-drain, crons, `asyncio.to_thread`). Each is a
`WatchedQueuePool` naming its lane, so every timed-out checkout is counted under its lane, and the sampler
watches each lane with its own tracker: a sample that saw new timeouts logs one `pool watch: the <lane> pool
is starving` line, and a lane saturated past `DUMP_AFTER_SECONDS` gets the same thread dump, its header naming
the lane. Only the request lane's verdict answers `/api/health` and arms the backstop; a starved identity pool
fails every cookie request with a 500 and a starved background pool stalls dispatch, which the health probe
must not turn into a fleet-wide restart.

THE LANE METRICS. Once every `METRICS_INTERVAL_SECONDS` the sampler writes one Embedded Metric Format line per
lane (`app/metrics_emf.py::pool_lines`: the peak checked-out and overflow figures and the timeouts of the
interval, dimension `Lane`; the request lane's also carries the median cost of `release_after_endpoint`'s
round trips) when `METRICS_NAMESPACE` is set, so CloudWatch can alarm on a lane. Per process:
each control-plane task writes its own, and an alarm reads their `Maximum`.

THE THREAD LIMIT lives here too (`apply_thread_limit`): AnyIO's request threadpool is sized to the pool's
capacity (`app/config.py::request_thread_limit`), so no more sync handlers run at once than there are
connections for them. With more threads than connections, a saturated pool parked the surplus threads for
`pool_timeout` on every request; with the bound, a request past capacity waits for a thread instead, holding
nothing.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import shutil
import statistics
import sys
import threading
import functools
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional

import anyio

from app.config import request_thread_limit

SAMPLE_SECONDS = 1.0
#: A dump this soon after saturation begins still catches the holders of a short stall.
DUMP_AFTER_SECONDS = 5.0
#: A wedge that lasts gets one dump a minute (~40 threads x ~30 frames each).
DUMP_INTERVAL_SECONDS = 60.0
#: Twice the ALB's time to declare a target unhealthy (3 x 5s), so only a stall, never a burst, fails the probe.
HEALTH_FAIL_AFTER_SECONDS = 30.0
#: `py-spy dump` of a 40-thread process takes well under a second; this bounds a hung ptrace.
DUMP_TIMEOUT_SECONDS = 20.0
#: Twice `HEALTH_FAIL_AFTER_SECONDS`: a pool full AND failing more checkouts than it serves this long is wedged.
STARVED_AFTER_SECONDS = 60.0
#: The window timeouts and checkins are counted over.
STARVED_WINDOW_SECONDS = 60.0
#: Fewer timeouts than this in the window is noise, whatever the turnover (the incident ran 300+ a minute).
STARVED_MIN_TIMEOUTS = 10
#: Set by the ECS agent in every task; the backstop's exit is armed only where it is.
ECS_MARKER_ENV = "ECS_CONTAINER_METADATA_URI_V4"
#: Unhealthy this long and still serving: the ALB (15s to act) has not replaced the task, so it ends itself.
EXIT_AFTER_SECONDS = 90.0
#: The process's exit status when the backstop fires (EX_SOFTWARE), told apart from a crash in ECS's stop reason.
EXIT_STATUS = 70
#: How often each lane's figures are written as EMF (`app/metrics_emf.py::pool_lines`): CloudWatch's finest
#: standard resolution.
METRICS_INTERVAL_SECONDS = 60.0

#: The connection lanes (`app/db.py`); `WatchedQueuePool.lane` names one of them.
REQUEST, IDENTITY, BACKGROUND = "request", "identity", "background"
LANES = (REQUEST, IDENTITY, BACKGROUND)

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


_tracker = SaturationTracker()
#: The other lanes' trackers; the request lane's is `_tracker`, which the health verdicts read.
_lane_trackers = {IDENTITY: SaturationTracker(), BACKGROUND: SaturationTracker()}


def tracker_for(lane: str) -> SaturationTracker:
    return _tracker if lane == REQUEST else _lane_trackers[lane]


def _now() -> float:
    """The watch's clock (monotonic seconds); its own name so a test can fake it without touching the loop's."""
    return time.monotonic()


_checkins = dict.fromkeys(LANES, 0)


def _checkin_counter(lane: str):
    def count(*_args) -> None:
        """Engine `checkin` listener: one more connection returned to the lane's pool. A bare increment is enough
        for a counter one sampler reads once a second (the GIL keeps it whole)."""
        _checkins[lane] += 1

    return count


#: One listener per lane, built once so `event.contains` recognises it.
_CHECKIN_LISTENERS = {lane: _checkin_counter(lane) for lane in LANES}


def lane_engine(lane: str):
    """The engine behind a lane (`app/db.py`)."""
    from app import db

    return {REQUEST: db.engine, IDENTITY: db.identity_engine, BACKGROUND: db.background_engine}[lane]


def pool_numbers(lane: str = REQUEST) -> tuple[int, int, int]:
    """`(checkedout, capacity, checkins)` of a lane's pool. Capacity is the environment's ceiling
    (`pool_size + max_overflow`); a pool class without overflow counts as its size. `checkins` is the running
    count of connections returned since the listener was installed (`start_pool_watch`)."""
    pool = lane_engine(lane).pool
    capacity = pool.size() + max(0, int(getattr(pool, "_max_overflow", 0)))
    return pool.checkedout(), capacity, _checkins[lane]


def overflow_in_use(lane: str = REQUEST) -> int:
    """Connections a lane's pool has open beyond its `pool_size` right now (QueuePool's `overflow()` counts up
    from `-pool_size`)."""
    return max(0, int(lane_engine(lane).pool.overflow()))


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


def count_checkout_timeout(lane: str = REQUEST) -> None:
    """One checkout of a lane's pool timed out (`app/db.py::WatchedQueuePool`, once per checkout). A lane this
    module does not know is ignored: it runs inside the pool's own `TimeoutError`, which must reach the caller."""
    tracker = _tracker if lane == REQUEST else _lane_trackers.get(lane)
    if tracker is not None:
        tracker.timed_out(_now())


#: Never `--locals` (`-l`): with it every frame's variables — request bodies, tokens, SQL parameters — would
#: land in the log. `test_pool_watch.py` pins this argv.
DUMP_ARGV = ("dump", "--nonblocking")


async def dump_threads(binary: str, *, checkedout: int, capacity: int, saturated: float, wedged: float,
                       lane: str = REQUEST) -> None:
    """One `py-spy dump` of this process, logged. Raises on a failed dump."""
    pid = os.getpid()
    proc = await asyncio.create_subprocess_exec(
        binary, *DUMP_ARGV, "--pid", str(pid),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=DUMP_TIMEOUT_SECONDS)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"py-spy exited {proc.returncode}: {stderr.decode(errors='replace').strip()[-500:]}")
    dump_log.info(json.dumps({
        "pool_dump": "start", "pid": pid, "lane": lane, "checkedout": checkedout, "capacity": capacity,
        "saturated_for": round(saturated, 1), "wedged_for": round(wedged, 1),
    }))
    for line in stdout.decode(errors="replace").splitlines():
        if line.strip():
            dump_log.info(line)
    dump_log.info(json.dumps({"pool_dump": "end", "pid": pid}))


def dump_threads_in_process(*, checkedout: int, capacity: int, saturated: float, wedged: float,
                            lane: str = REQUEST) -> None:
    """The same dump as `dump_threads`, taken from inside the process: every Python thread's stack by name and
    id, framed by the same header and footer. Frames only — a file, a line, a function and its source line —
    never a value."""
    pid = os.getpid()
    names = {t.ident: t.name for t in threading.enumerate()}
    dump_log.info(json.dumps({
        "pool_dump": "start", "pid": pid, "lane": lane, "checkedout": checkedout, "capacity": capacity,
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


class PoolWatch:
    """The sampler: one per process in production (`start_pool_watch`), one per test when a test drives it.
    Every dependency is an attribute so a test substitutes its own clock, sleep, pool numbers and dump
    instead of patching module globals a production watcher may be using at the same time (the session
    TestClient runs the lifespan, so one is)."""

    def __init__(self, tracker: SaturationTracker, *, sample_seconds: float = SAMPLE_SECONDS, numbers=None,
                 clock=None, sleep=None, dump=None, which=None, dump_in_process=None, exit=_ON_ECS,
                 lanes=None, overflow=None, emit=None) -> None:
        self.tracker = tracker
        self.sample_seconds = sample_seconds
        self.numbers = numbers or pool_numbers
        #: The other lanes, `{lane: (tracker, numbers)}`: watched for starvation and dumped, never a health verdict.
        self.lanes: dict = {lane: (tracker_for(lane), functools.partial(pool_numbers, lane))
                            for lane in (IDENTITY, BACKGROUND)} if lanes is None else dict(lanes)
        #: `overflow(lane)`: connections open past the lane's `pool_size` now.
        self.overflow = overflow or overflow_in_use
        #: `emit(lines)`: where the lane metrics go; the default writes them to `app.metrics` when a namespace is set.
        self.emit = emit or _emit_pool_metrics
        self.clock = clock or _now
        self.sleep = sleep or asyncio.sleep
        self.dump = dump or dump_threads
        self.which = which or shutil.which
        self.dump_in_process = dump_in_process or dump_threads_in_process
        #: None: the backstop only logs. The default exits only on ECS (`ECS_MARKER_ENV`), never a test run or a
        #: local stack; a test passes its own callable, or None.
        self.exit = (os._exit if os.environ.get(ECS_MARKER_ENV) else None) if exit is _ON_ECS else exit
        self.dump_task: Optional[asyncio.Task] = None
        self.unhealthy_since: Optional[float] = None
        #: Per lane: the timeouts already logged, and the interval's peaks and timeouts for the next metrics line.
        #: Seeded from the trackers' totals: the module's trackers outlive a watch (a test's lifespan, a restart).
        totals = {REQUEST: tracker.timeouts_total,
                  **{lane: lane_tracker.timeouts_total for lane, (lane_tracker, _) in self.lanes.items()}}
        self.logged_timeouts = {lane: totals.get(lane, 0) for lane in LANES}
        self.metrics_from = dict(self.logged_timeouts)
        self.peaks: dict[str, tuple[int, int]] = {}
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
        stale `saturated_since` would otherwise hold a 503 past the pool's recovery). A request pool that cannot
        be read (a pool class without the counters) ends the watch with one warning, not a traceback a second;
        another lane that cannot be read leaves the watch, with one warning."""
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
            self._sampled(REQUEST, self.tracker, checkedout, capacity, now)
            if due:
                self._dump_if_free(REQUEST, self.tracker, checkedout, capacity, now)
            for lane, (tracker, numbers) in list(self.lanes.items()):
                try:
                    lane_out, lane_capacity, lane_checkins = numbers()
                except Exception as exc:  # noqa: BLE001 — see the docstring
                    log.warning("pool watch: cannot read the %s pool (%s) — not watching it", lane, exc)
                    del self.lanes[lane]
                    continue
                lane_due = tracker.observe(lane_out, lane_capacity, lane_checkins, now)
                self._sampled(lane, tracker, lane_out, lane_capacity, now)
                if lane_due:
                    self._dump_if_free(lane, tracker, lane_out, lane_capacity, now)
            self._publish_if_due(now)

    def _sampled(self, lane: str, tracker: SaturationTracker, checkedout: int, capacity: int, now: float) -> None:
        """Log the lane's checkouts that timed out since the last sample (one line a second at most), and keep the
        interval's peaks for the metrics line."""
        new = tracker.timeouts_total - self.logged_timeouts[lane]
        self.logged_timeouts[lane] = tracker.timeouts_total
        if new > 0:
            log.warning(
                "pool watch: the %s pool is starving — %d checkout(s) timed out since the last sample, %d in the "
                "last %.0fs (%d/%d checked out, saturated for %.0fs)",
                lane, new, len(tracker.timeouts), STARVED_WINDOW_SECONDS, checkedout, capacity,
                tracker.saturated_for(now),
            )
        try:
            overflow = self.overflow(lane)
        except Exception:  # noqa: BLE001 — a figure missing from one metrics line is not worth a traceback a second
            overflow = 0
        peak_out, peak_overflow = self.peaks.get(lane, (0, 0))
        self.peaks[lane] = (max(peak_out, checkedout), max(peak_overflow, overflow))

    def _dump_if_free(self, lane: str, tracker: SaturationTracker, checkedout: int, capacity: int, now: float) -> None:
        if self.dump_task is not None and not self.dump_task.done():
            return   # the previous dump is still running; `last_dump_at` keeps the cadence
        tracker.dumped(now)   # before the dump: a slow or failing dump must not fire every sample
        log.warning(
            "pool watch: the %s pool has been saturated for %.0fs (%d/%d checked out, wedged for %.0fs) — "
            "dumping every thread's stack under logger app.pool_dump",
            lane, tracker.saturated_for(now), checkedout, capacity, tracker.wedged_for(now),
        )
        self.dump_task = asyncio.create_task(self._dump_once(
            lane=lane, checkedout=checkedout, capacity=capacity,
            saturated=tracker.saturated_for(now), wedged=tracker.wedged_for(now),
        ), name="pool-dump")

    def _publish_if_due(self, now: float) -> None:
        """Every `METRICS_INTERVAL_SECONDS`, hand each lane's peaks and timeouts of the interval to `emit`."""
        if self.metrics_due_at is None:
            self.metrics_due_at = now + METRICS_INTERVAL_SECONDS
        if now < self.metrics_due_at:
            return
        self.metrics_due_at = now + METRICS_INTERVAL_SECONDS
        trackers = {REQUEST: self.tracker, **{lane: tracker for lane, (tracker, _) in self.lanes.items()}}
        figures = {}
        for lane, tracker in trackers.items():
            peak_out, peak_overflow = self.peaks.get(lane, (0, 0))
            figures[lane] = (peak_out, peak_overflow, tracker.timeouts_total - self.metrics_from[lane])
            self.metrics_from[lane] = tracker.timeouts_total
        self.peaks.clear()
        try:
            self.emit(figures)
        except Exception:  # noqa: BLE001 — see the module docstring: the watch never takes the api down
            log.exception("pool watch: writing the lane metrics failed")

    async def _dump_once(self, **numbers) -> None:
        try:
            binary = self.which("py-spy")
            if binary is None:
                raise RuntimeError("no py-spy binary on PATH (apps/api/requirements.txt pins it)")
            await self.dump(binary, **numbers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — see the module docstring: the watch never takes the api down
            log.warning("pool watch: py-spy dump failed (%s) — dumping in-process instead", str(exc)[:300])
            try:
                self.dump_in_process(**numbers)
            except Exception:  # noqa: BLE001 — as above
                log.exception("pool watch: thread dump failed; next attempt in %.0fs", self.tracker.dump_interval)

    async def cancel(self) -> None:
        task = self.dump_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def apply_thread_limit() -> int:
    """Size AnyIO's default thread limiter (what `starlette.concurrency.run_in_threadpool` draws on) to
    `request_thread_limit()`. Must run on the serving loop: the limiter is the running backend's."""
    limit = request_thread_limit()
    anyio.to_thread.current_default_thread_limiter().total_tokens = limit
    return limit


def apply_to_thread_lane() -> int:
    """Give the serving loop a default executor whose workers are in the background lane (`app/db.py`), so
    an `asyncio.to_thread` DB caller — outside the request threadpool's bound — never draws on the request
    pool. Python's own default size. Must run on the serving loop."""
    from app.db import background_executor

    workers = min(32, (os.cpu_count() or 1) + 4)
    asyncio.get_running_loop().set_default_executor(background_executor(workers, "to-thread"))
    return workers


def take_release_p50_ms() -> Optional[float]:
    """The median of the route-connection releases since the last call, in milliseconds (`app/db.py::
    release_costs`), emptying it; None when nothing was released."""
    from app.db import release_costs

    costs = []
    while release_costs:
        costs.append(release_costs.popleft())
    return statistics.median(costs) * 1000.0 if costs else None


def _emit_pool_metrics(figures: dict[str, tuple[int, int, int]]) -> None:
    """Write the lanes' figures as EMF lines (`app/metrics_emf.py::pool_lines`); nothing without a namespace."""
    from app import metrics_emf
    from app.config import get_settings

    release_p50_ms = take_release_p50_ms()
    namespace = get_settings().metrics_namespace
    if not namespace:
        return
    for event in metrics_emf.pool_lines(namespace, int(time.time() * 1000), figures, release_p50_ms):
        metrics_emf.emf_log.info(event)


def _listen_for_checkins() -> None:
    from sqlalchemy import event

    for lane in LANES:
        engine = lane_engine(lane)
        if not event.contains(engine, "checkin", _CHECKIN_LISTENERS[lane]):
            event.listen(engine, "checkin", _CHECKIN_LISTENERS[lane])


async def start_pool_watch(app) -> None:
    """Lifespan startup hook. Mirrors the other actors in `app/main.py::_lifespan`. The watcher's tracker is
    the module's, so `wedged()` (the health route) reads what this watcher writes."""
    _listen_for_checkins()
    log.info("pool watch: request threadpool bounded to %d threads", apply_thread_limit())
    log.info("pool watch: asyncio.to_thread runs %d background-lane threads", apply_to_thread_lane())
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
