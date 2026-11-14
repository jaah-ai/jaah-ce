r"""CPU profile of this api process: an in-process sampler attributes each thread's CPU time to the stack it is
running, and the hottest stacks land in CloudWatch as JSON lines.

WHAT IT MEASURES. While the `cpu_profiling` operator setting is on, every api task records one `WINDOW_SECONDS`
window every `PERIOD_SECONDS`. A daemon thread wakes `RATE_HZ` times a second and, for every Python thread,
reads the kernel's scheduler state for it (`/proc/self/task/<tid>/stat`) and the stack it is running now
(`sys._current_frames`). A thread that is RUNNING (`R`: on a CPU or waiting for one) adds one sample to that
stack; any other state (sleeping on I/O, a lock, a timer) adds nothing, so a wait frame is never charged and an
idle pool of 100 threads costs nothing in the profile. The state and the stack are read within microseconds of
each other, so a sample's leaf is the frame on CPU. Unlike a GIL-holder sample this sees every thread of the
free-threaded build at once, and CPU spent in C under a Python frame (libpq, TLS, json) is charged to that frame.
CPU on a thread `threading` does not know (Granian's Rust workers, a native thread never registered) cannot be
attributed; each window's `process_cpu_s - attributed_cpu_s` is that remainder plus sampling error.

WHY NOT PY-SPY. py-spy (0.4.2 and 0.4.3, the newest) cannot read the free-threaded CPython 3.14 the api image runs
(python-3.14t): every window failed "Failed to find python version from target process". An
in-process sampler needs no ptrace, no capability and no binary.

WHAT IT COSTS. Per tick: one `sys._current_frames` (a brief stop of every thread on the free-threaded build),
one small /proc read per thread and a stack walk per RUNNING thread (file names cached). Charged only while a window
runs, so about a fifth of the time; each window's `sampler_cpu_s` is the sampler's own CPU.

WHAT IT LOGS. Per window, one bare JSON line (logger `app.profile`, unprefixed by `app/main.py::_AppFormatter`)
per distinct stack, the `TOP_STACKS` hottest only, then one `cpu-window` summary line:

  {"profile": "cpu", "pid": 7, "samples": 412, "rate_hz": 10, "leaf": "fn (app/x.py:88)", "stack": "outer (a.py:1);...;fn (app/x.py:88)"}
  {"profile": "cpu-window", "pid": 7, "window_s": 60, "wall_s": 60.1, "rate_hz": 10, "samples": 412, "stacks": 311,
   "logged_samples": 400, "process_cpu_s": 45.3, "attributed_cpu_s": 41.2, "sampler_cpu_s": 0.3, "threads": 97}

`samples / rate_hz` is CPU-seconds, as the queries below read it. A stack's frames run outermost first; `leaf` is the frame on CPU.

QUERYING (CloudWatch Logs Insights, the control plane's `/ecs/<name>-cp` group):

  Self time — the lines that burn the CPU:
    filter profile = "cpu" | stats sum(samples) as s by leaf | sort s desc | limit 50
  Self time per function, ignoring line numbers:
    filter profile = "cpu" | parse leaf "* (*:*)" as fn, file, line
    | stats sum(samples) as s by fn, file | sort s desc | limit 50
  Total (inclusive) time of one function, everything it calls counted (the regex anchors the frame, so
  `_run_cron (` does not match):
    filter profile = "cpu" and stack like /(^|;)run_cron \(/ | stats sum(samples / rate_hz) as cpu_s
  What a hot function spends its time on:
    filter profile = "cpu" and stack like /(^|;)run_cron \(/ | stats sum(samples) as s by leaf | sort s desc
  CPU-seconds per minute, per task:
    filter profile = "cpu-window" | stats sum(samples / rate_hz) as cpu_s by bin(1m), @logStream

Inclusive totals are a floor: only the `TOP_STACKS` hottest stacks are logged, and each window's
`samples - logged_samples` is what the cut left out. A stack over `MAX_STACK_CHARS` keeps its leaf-most
frames behind a leading `...;`, so its outermost frames are the ones a query cannot see. Frames name a function,
a file and a line, never a value.

OFF BY DEFAULT, and a failure here is a log line, never a crash: a failed window is logged and retried a period
later.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict
from typing import Callable, Optional

from app import edition_features
from app.operator_settings import reads as settings_reads

KNOB = "cpu_profiling"
WINDOW_SECONDS = 60
# One window per period: a profile ranks lines from many windows, so a 20% duty cycle loses little.
PERIOD_SECONDS = 300
# Ten ticks a second: a thread's CPU is charged to the stack it runs at the tick, so a finer rate only sharpens
# which line a short burst lands on.
RATE_HZ = 10
# Bounds the log volume per window; the summary line's `logged_samples` says how much the cut dropped.
TOP_STACKS = 200
# Under Docker's 16 KB line split, so a deep stack still arrives as one JSON event.
MAX_STACK_CHARS = 8000

# Its own level, like `app.metrics`: a quieter `LOG_LEVEL` must not silence a profile someone switched on.
profile_log = logging.getLogger("app.profile")
profile_log.setLevel(logging.INFO)
log = logging.getLogger(__name__)


def thread_running(native_id: int) -> Optional[bool]:
    """Whether the thread with kernel id `native_id` is runnable (`R`) now, or None when it is gone."""
    try:
        with open(f"/proc/self/task/{native_id}/stat", "rb") as f:
            # The command name (field 2) may hold spaces or parentheses, so read the state after its LAST `)`.
            return f.read().rsplit(b")", 1)[1].split(None, 1)[0] == b"R"
    except (OSError, IndexError):
        return None


def _path_prefixes() -> tuple[str, ...]:
    """`sys.path` entries, longest first, so a frame's file names its module path (`psycopg/cursor.py`,
    `app/x.py`) rather than an absolute install path."""
    return tuple(sorted({os.path.abspath(p) + os.sep for p in sys.path if p}, key=len, reverse=True))


@functools.lru_cache(maxsize=4096)
def _short(filename: str, prefixes: tuple[str, ...]) -> str:
    for prefix in prefixes:
        if filename.startswith(prefix):
            return filename[len(prefix):]
    return filename


def fold(frame, prefixes: tuple[str, ...]) -> str:
    """A frame's stack, outermost first, as `fn (file:line);...`."""
    frames: list[str] = []
    while frame is not None:
        code = frame.f_code
        frames.append(f"{code.co_name} ({_short(code.co_filename, prefixes)}:{frame.f_lineno})")
        frame = frame.f_back
    frames.reverse()
    return ";".join(frames)


class Sampler:
    """One window's ticks: samples per folded stack. `running` and `threads` are injectable for a test."""

    def __init__(self, *, running: Callable[[int], Optional[bool]] = thread_running,
                 threads: Optional[Callable[[], dict[int, int]]] = None) -> None:
        self.running = running
        self.threads = threads or _python_threads
        self.prefixes = _path_prefixes()
        self.samples_by_stack: dict[str, int] = defaultdict(int)
        self.seen: set[int] = set()

    def tick(self, skip_ident: Optional[int] = None) -> None:
        """One sample for each thread running now, on the stack it runs."""
        frames = sys._current_frames()
        for ident, native_id in self.threads().items():
            if ident == skip_ident or ident not in frames:
                continue
            running = self.running(native_id)
            if running is None:
                continue
            self.seen.add(native_id)
            if running:
                self.samples_by_stack[fold(frames[ident], self.prefixes)] += 1


def _python_threads() -> dict[int, int]:
    """`{ident: native_id}` of every Python thread this process knows."""
    return {t.ident: t.native_id for t in threading.enumerate() if t.ident is not None and t.native_id is not None}


def window_lines(samples_by_stack: dict[str, int], *, pid: int, wall_s: float, process_cpu_s: float,
                 sampler_cpu_s: float, threads: int) -> list[str]:
    """One window's samples per stack as the JSON lines to log (the module docstring's WHAT IT LOGS)."""
    top = sorted(samples_by_stack.items(), key=lambda kv: kv[1], reverse=True)[:TOP_STACKS]
    lines = [
        json.dumps({"profile": "cpu", "pid": pid, "samples": n, "rate_hz": RATE_HZ,
                    "leaf": stack.rsplit(";", 1)[-1], "stack": _clip(stack)})
        for stack, n in top
    ]
    total = sum(samples_by_stack.values())
    lines.append(json.dumps({
        "profile": "cpu-window", "pid": pid, "window_s": WINDOW_SECONDS, "wall_s": round(wall_s, 1), "rate_hz": RATE_HZ,
        "samples": total, "stacks": len(samples_by_stack), "logged_samples": sum(n for _, n in top),
        "process_cpu_s": round(process_cpu_s, 2), "attributed_cpu_s": round(total / RATE_HZ, 2),
        "sampler_cpu_s": round(sampler_cpu_s, 3), "threads": threads,
    }))
    return lines


def _clip(stack: str) -> str:
    """The stack, or its leaf-most `MAX_STACK_CHARS` behind `...;` (the frames a hot line needs most)."""
    if len(stack) <= MAX_STACK_CHARS:
        return stack
    return "...;" + stack[-MAX_STACK_CHARS:].split(";", 1)[-1]


def record_window(stop: threading.Event, *, sampler: Optional[Sampler] = None) -> list[str]:
    """Tick for `WINDOW_SECONDS` (or until `stop` is set) on the calling thread; return the lines to log."""
    sampler = sampler or Sampler()
    me = threading.get_ident()
    started, process_started, own_started = time.monotonic(), time.process_time(), time.thread_time()
    period = 1.0 / RATE_HZ
    deadline = started + WINDOW_SECONDS
    sampler.tick(skip_ident=me)
    while not stop.wait(max(0.0, min(period, deadline - time.monotonic()))):
        sampler.tick(skip_ident=me)
        if time.monotonic() >= deadline:
            break
    return window_lines(
        sampler.samples_by_stack, pid=os.getpid(), wall_s=time.monotonic() - started,
        process_cpu_s=time.process_time() - process_started, sampler_cpu_s=time.thread_time() - own_started,
        threads=len(sampler.seen),
    )


async def _record_window() -> None:
    """One window on a daemon thread of its own, logged: never the serving loop, whose lag would skew the ticks,
    and never the shared `asyncio.to_thread` lane (`app/pool_watch.py::apply_to_thread_lane`), which a minute-long
    window would hold a worker of. A cancel stops the ticking at once."""
    stop = threading.Event()
    done: concurrent.futures.Future[list[str]] = concurrent.futures.Future()

    def run() -> None:
        # RUNNING from here, so the awaiting loop's cancel (`wrap_future`) cannot cancel it under this thread.
        if not done.set_running_or_notify_cancel():
            return
        try:
            done.set_result(record_window(stop))
        except BaseException as exc:  # noqa: BLE001 — handed to the awaiting loop, which logs it
            done.set_exception(exc)

    threading.Thread(target=run, name="cpu-profile", daemon=True).start()
    try:
        lines = await asyncio.wrap_future(done)
    except asyncio.CancelledError:
        stop.set()
        raise
    for line in lines:
        profile_log.info(line)


async def _profile_forever() -> None:
    """Re-read the setting each window; while it is on, record one window per `PERIOD_SECONDS`. Every failure
    is logged, then waited out like a window."""
    while True:
        try:
            if not await asyncio.to_thread(settings_reads.get_bool, KNOB):
                await asyncio.sleep(WINDOW_SECONDS)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — see the module docstring: a profiler never takes the api down
            log.exception("cpu profile: reading the setting failed; retrying in %ds", WINDOW_SECONDS)
            await asyncio.sleep(WINDOW_SECONDS)
            continue
        started = time.monotonic()
        try:
            await _record_window()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — see the module docstring: a profiler never takes the api down
            log.exception("cpu profile: window failed; retrying next period")
        await asyncio.sleep(max(0.0, PERIOD_SECONDS - (time.monotonic() - started)))


async def start_cpu_profiler(app) -> None:
    """Lifespan startup hook. Mirrors the other actors in `app/main.py::_lifespan`. An edition without
    `cpu_profiling` never starts the loop, whatever the setting says."""
    if not edition_features.enabled("cpu_profiling"):
        return
    app.state.cpu_profiler_task = asyncio.create_task(_profile_forever(), name="cpu-profile")


async def stop_cpu_profiler(app) -> None:
    """Lifespan shutdown hook: cancel the loop, which stops a recording in flight."""
    task = getattr(app.state, "cpu_profiler_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutting down; the loop logged its own
        pass
