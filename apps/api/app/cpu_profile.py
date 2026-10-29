r"""CPU profile of this api process: py-spy samples it, and its hottest stacks land in CloudWatch as JSON lines.

WHAT IT MEASURES. While the `cpu_profiling` operator setting is on, every api task runs `py-spy record`
against its own pid for one `WINDOW_SECONDS` window at `RATE_HZ` every `PERIOD_SECONDS`. py-spy is an
out-of-process sampling profiler: it reads the interpreter's stacks from outside (ptrace + process_vm_readv,
hence the api container's SYS_PTRACE capability in its deployment's task definition).
`--gil` samples only the thread holding the GIL — whichever it is: a request handler on the threadpool, the
event loop, a DBOS or cron thread — so a sample is Python CPU time. CPU spent in C with the GIL released (libpq,
TLS) is not seen, and neither is a thread waiting on I/O, which all-thread sampling miscounted as CPU once the
task was CPU-starved. `--nonblocking` keeps it from pausing the process.

WHAT IT COSTS. Unwinding every thread (~100 in the deployed api) at 20 Hz could not keep pace: py-spy's
`--duration` counts samples, not seconds, so each window ran ~95 s flat out and every task sat at 99% CPU,
which the CPU autoscaler answered by scaling to its ceiling. Unwinding only the GIL holder costs
a fraction of that, and the duty cycle bounds the rest: py-spy runs at most `WINDOW_SECONDS +
OVERRUN_SECONDS` of every `PERIOD_SECONDS`, is told to stop (SIGINT, which still writes what it has) once it
overruns, and each window's `wall_s` shows how far it lagged.

WHAT IT LOGS. Per window, one bare JSON line (logger `app.profile`, unprefixed by `app/main.py::_AppFormatter`)
per distinct stack, the `TOP_STACKS` hottest only, then one `cpu-window` summary line:

  {"profile": "cpu", "pid": 7, "samples": 412, "leaf": "fn (app/x.py:88)", "stack": "outer (a.py:1);...;fn (app/x.py:88)"}
  {"profile": "cpu-window", "pid": 7, "window_s": 60, "wall_s": 60.4, "rate_hz": 10, "samples": 412, "stacks": 311, "logged_samples": 400}

`samples / rate_hz` is CPU-seconds. A stack's frames run outermost first; `leaf` is the frame on CPU.

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
frames behind a leading `...;`, so its outermost frames are the ones a query cannot see.

THE NON-ROOT ATTACH. The deployed api runs as uid 10001 (Security Hub ECS.20), which keeps no added
capability, so SYS_PTRACE does not help it. py-spy is this process's CHILD tracing its PARENT, and Yama's
`ptrace_scope = 1` refuses that ("Permission Denied") unless the parent names it as its ptracer. py-spy
attaches the moment it starts, so it starts behind a gate (`_GATE`): the gate's pid is named
(`_ptracer_open`), then the gate is told to exec py-spy under that same pid. The exception names that one
pid for one window and is closed again after it. Nothing else in the container may attach.

OFF BY DEFAULT, and a failure here is a log line, never a crash: no py-spy binary, a refused attach (a
kernel without Yama's prctl, a stack that drops the capability) or a bad window is logged and retried a
period later.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time

from app.operator_settings import reads as settings_reads

KNOB = "cpu_profiling"
WINDOW_SECONDS = 60
# One window per period: a profile ranks lines from many windows, so a 20% duty cycle loses little.
PERIOD_SECONDS = 300
# How long past its window py-spy may run before it is told to stop (it writes what it has).
OVERRUN_SECONDS = 15
# 10 Hz of GIL-holder samples: up to 600 a busy minute, enough to rank lines across a profile's windows.
RATE_HZ = 10
# Bounds the log volume per window; the summary line's `logged_samples` says how much the cut dropped.
TOP_STACKS = 200
# Under Docker's 16 KB line split, so a deep stack still arrives as one JSON event.
MAX_STACK_CHARS = 8000

# Its own level, like `app.metrics`: a quieter `LOG_LEVEL` must not silence a profile someone switched on.
profile_log = logging.getLogger("app.profile")
profile_log.setLevel(logging.INFO)
log = logging.getLogger(__name__)


def window_lines(raw: str, *, pid: int, wall_s: float) -> list[str]:
    """py-spy's raw (folded) output — `frame;frame;frame <count>` per line — as the JSON lines to log."""
    counts: dict[str, int] = {}
    for row in raw.splitlines():
        stack, _, count = row.rpartition(" ")
        if stack and count.isdigit():
            counts[stack] = counts.get(stack, 0) + int(count)
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:TOP_STACKS]
    lines = [
        json.dumps({"profile": "cpu", "pid": pid, "samples": n, "rate_hz": RATE_HZ,
                    "leaf": stack.rsplit(";", 1)[-1], "stack": _clip(stack)})
        for stack, n in top
    ]
    lines.append(json.dumps({
        "profile": "cpu-window", "pid": pid, "window_s": WINDOW_SECONDS, "wall_s": round(wall_s, 1), "rate_hz": RATE_HZ,
        "samples": sum(counts.values()), "stacks": len(counts), "logged_samples": sum(n for _, n in top),
    }))
    return lines


def _clip(stack: str) -> str:
    """The stack, or its leaf-most `MAX_STACK_CHARS` behind `...;` (the frames a hot line needs most)."""
    if len(stack) <= MAX_STACK_CHARS:
        return stack
    return "...;" + stack[-MAX_STACK_CHARS:].split(";", 1)[-1]


# Yama's prctl (include/uapi/linux/prctl.h): who may ptrace this process besides its ancestors.
PR_SET_PTRACER = 0x59616D61
PR_SET_PTRACER_NONE = 0
# py-spy starts behind this gate: it is exec'd (keeping the pid this process names as its ptracer) only once
# the gate reads the go byte, so it never tries to attach before it is named. An empty read (the window gave
# up before naming it) exits without running py-spy at all.
_GATE = "import os, sys\nif os.read(0, 1) != b'x':\n    sys.exit(1)\nos.execv(sys.argv[1], sys.argv[1:])"


def _set_ptracer(value: int) -> bool:
    """`prctl(PR_SET_PTRACER, value)`; False when the kernel refuses it. A kernel without Yama refuses it (EINVAL)
    and needs no exception either, so a refused OPEN is a debug line and py-spy's own refusal, if any, reports
    the window. A refused CLOSE is an error: the named pid would keep its exception unseen."""
    prctl = getattr(ctypes.CDLL(None, use_errno=True), "prctl", None)
    if prctl is None:  # not Linux: no Yama, nothing to open
        return False
    if prctl(PR_SET_PTRACER, ctypes.c_ulong(value), 0, 0, 0) == 0:
        return True
    log.log(logging.ERROR if value == PR_SET_PTRACER_NONE else logging.DEBUG,
            "cpu profile: prctl(PR_SET_PTRACER, %#x) failed: errno %d", value, ctypes.get_errno())
    return False


@contextlib.contextmanager
def _ptracer_open(tracer_pid: int):
    """Let `tracer_pid` (py-spy, behind `_GATE`) attach for one window, and nobody else (the module docstring's
    NON-ROOT ATTACH). Closed again on every exit, a cancel included."""
    _set_ptracer(tracer_pid)
    try:
        yield
    finally:
        _set_ptracer(PR_SET_PTRACER_NONE)


async def _record_window(binary: str) -> None:
    """One `WINDOW_SECONDS` py-spy recording of this process, logged. Raises on a failed recording."""
    pid = os.getpid()
    started = time.monotonic()
    with tempfile.NamedTemporaryFile(prefix="cpu-profile-", suffix=".txt") as out:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", _GATE,
            binary, "record", "--pid", str(pid), "--duration", str(WINDOW_SECONDS), "--rate", str(RATE_HZ),
            "--format", "raw", "--nonblocking", "--gil", "--output", out.name,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            with _ptracer_open(proc.pid):
                # The go byte, after the pid is named; `communicate` then closes stdin and waits.
                _, stderr = await _communicate_bounded(proc)
        except asyncio.CancelledError:
            # py-spy may have exited on its own meanwhile; a ProcessLookupError here would replace the
            # cancel and leave the loop running through shutdown.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)  # reap it; bounded, we are shutting down
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"py-spy exited {proc.returncode}: {stderr.decode(errors='replace').strip()[-500:]}")
        # By path, not `out.read()`: py-spy writes its own file at that path.
        with open(out.name, errors="replace") as f:
            raw = f.read()
    for line in window_lines(raw, pid=pid, wall_s=time.monotonic() - started):
        profile_log.info(line)


async def _communicate_bounded(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """`proc.communicate(b"x")`, but a py-spy still running `OVERRUN_SECONDS` past its window is sent SIGINT —
    it writes the samples it has and exits 0 — and killed if even that does not end it."""
    talk = asyncio.ensure_future(proc.communicate(input=b"x"))
    try:
        done, _ = await asyncio.wait({talk}, timeout=WINDOW_SECONDS + OVERRUN_SECONDS)
        if not done:
            log.warning("cpu profile: py-spy overran its %ds window by %ds; stopping it",
                        WINDOW_SECONDS, OVERRUN_SECONDS)
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGINT)
            done, _ = await asyncio.wait({talk}, timeout=OVERRUN_SECONDS)
            if not done:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        return await talk
    finally:
        # `asyncio.wait` never cancels what it waits on: a cancelled window must not leave the pipes' reader behind.
        talk.cancel()


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
            binary = shutil.which("py-spy")
            if binary is None:
                raise RuntimeError("no py-spy binary on PATH (apps/api/requirements.txt pins it)")
            await _record_window(binary)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — see the module docstring: a profiler never takes the api down
            log.exception("cpu profile: window failed; retrying next period")
        await asyncio.sleep(max(0.0, PERIOD_SECONDS - (time.monotonic() - started)))


async def start_cpu_profiler(app) -> None:
    """Lifespan startup hook. Mirrors the other actors in `app/main.py::_lifespan`."""
    app.state.cpu_profiler_task = asyncio.create_task(_profile_forever(), name="cpu-profile")


async def stop_cpu_profiler(app) -> None:
    """Lifespan shutdown hook: cancel the loop, which kills a recording in flight."""
    task = getattr(app.state, "cpu_profiler_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutting down; the loop logged its own
        pass
