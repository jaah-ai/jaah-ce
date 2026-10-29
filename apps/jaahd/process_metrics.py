"""Per-process and per-session usage off a full `/proc` walk — the `top_processes` and `sessions` keys of the
observation's `host_metrics` block (`host_metrics.HostMetricsSampler` drives and folds it).

A walk reads every `/proc/<pid>/stat`, so it runs at most once per `SCAN_INTERVAL_SECS`, not on every 5s
tick. CPU % is percent of ONE core (top-style): the utime+stime delta between two consecutive walks over
the elapsed time. A process is matched across walks by `(pid, starttime)`, so a recycled pid starts afresh;
a process new since the last walk counts 0. The first walk is only a baseline and yields no reading.

Top processes carry `comm` only — never argv, which holds secrets (tokens on command lines). A session is a
live runner (`runner_registry.live_runners`): its usage is the process tree rooted at the pane pid;
`cpu_seconds` adds the pane's reaped children (`cutime`+`cstime`), so it only grows over a session's life.

Linux only (elsewhere `reading()` is None). Python 3.9, stdlib only. Nothing here may raise into the
daemon — any failure means no reading, logged at debug.
"""
from __future__ import annotations

import logging
import os
import time

try:
    import pwd
except ImportError:   # Windows: no passwd database (and no walk to name users for)
    pwd = None  # type: ignore[assignment]  # absent off Unix; every use is guarded

import host_platform

log = logging.getLogger("jaahd.process_metrics")

# Read at call time so a test can point the walk at a fixture tree.
PROC_ROOT = "/proc"

SCAN_INTERVAL_SECS = 30.0
TOP_N = 5          # per ranking; the union of top-by-CPU and top-by-RSS is at most 2 * TOP_N
MAX_SESSIONS = 64
_NAME_CAP = 32
_SESSION_ID_CAP = 128


def _sysconf(name: str, default: int) -> int:
    try:
        value = int(os.sysconf(name))
    except (AttributeError, ValueError, OSError):
        return default
    return value if value > 0 else default


CLK_TCK = _sysconf("SC_CLK_TCK", 100)
PAGE_KB = _sysconf("SC_PAGE_SIZE", 4096) // 1024 or 4


def _read_stat(pid: int) -> tuple | None:
    """`(comm, ppid, utime, stime, cutime, cstime, starttime, rss_pages)` off `/proc/<pid>/stat`, or None.

    `comm` sits in parentheses and may itself hold spaces and parentheses, so it is cut at the FIRST `(`
    and the LAST `)`."""
    try:
        with open(os.path.join(PROC_ROOT, str(pid), "stat"), "rb") as fh:
            raw = fh.read()
        open_at, close_at = raw.index(b"("), raw.rindex(b")")
        comm = raw[open_at + 1:close_at].decode("utf-8", "replace")
        f = raw[close_at + 1:].split()
        return (comm, int(f[1]), int(f[11]), int(f[12]), int(f[13]), int(f[14]), int(f[19]), int(f[21]))
    except (OSError, ValueError, IndexError):
        return None


_users: dict = {}


def _user_of(pid: int) -> str:
    """The real uid's user name off `/proc/<pid>/status` (cached; the uid string when unnamed), "" when
    unreadable."""
    try:
        with open(os.path.join(PROC_ROOT, str(pid), "status")) as fh:
            uid = next(line.split()[1] for line in fh if line.startswith("Uid:"))
    except (OSError, StopIteration, IndexError, ValueError):
        return ""
    if uid not in _users:
        try:
            _users[uid] = pwd.getpwuid(int(uid)).pw_name
        except (AttributeError, KeyError, ValueError, OverflowError):
            _users[uid] = uid
    return _users[uid][:_NAME_CAP]


def _clean(text: str, cap: int) -> str:
    return "".join(ch for ch in text if ch.isprintable())[:cap]


def _live_runners() -> list:
    import runner_registry   # lazily: the registry is only needed once a walk runs
    return runner_registry.live_runners()


class ProcessSampler:
    """The latest walk's `top_processes` and `sessions`. Long-lived, one per daemon; `sample()` is safe on
    every tick (it throttles itself), `reading()` is pure. Neither raises."""

    def __init__(self, clock=time.monotonic, runners=_live_runners,
                 interval_secs: float = SCAN_INTERVAL_SECS) -> None:
        self._clock = clock
        self._runners = runners
        self._interval = float(interval_secs)
        # When the last walk ran (the throttle) vs when the deltas' baseline was read: a failed walk keeps
        # the first and clears the second, so the next walk is a baseline rather than a 0% reading.
        self._last_at: float | None = None
        self._prev_at: float | None = None
        # (pid, starttime) -> utime+stime ticks at the previous walk.
        self._prev: dict = {}
        # (task_id, session_id, pane pid, pane starttime) -> the session's cumulative ticks at the previous walk.
        self._prev_sessions: dict = {}
        self._ceiling = 100.0 * max(1, os.cpu_count() or 1)
        self._latest: dict | None = None

    def sample(self) -> None:
        if not host_platform.is_linux():
            return
        now = self._clock()
        if self._last_at is not None and now - self._last_at < self._interval:
            return
        try:
            self._scan(now)
        except Exception as exc:   # noqa: BLE001 — one optional wire field may never break the pass
            log.debug("process_metrics: walk failed, no reading: %s", exc)
            self._latest = None
            self._prev, self._prev_sessions, self._prev_at, self._last_at = {}, {}, None, now

    def _scan(self, now: float) -> None:
        procs = {}
        for entry in os.listdir(PROC_ROOT):
            if entry.isdigit():
                stat = _read_stat(int(entry))
                if stat is not None:
                    procs[int(entry)] = stat
        prev, prev_at = self._prev, self._prev_at
        self._prev = {(pid, s[6]): s[2] + s[3] for pid, s in procs.items()}
        self._last_at = self._prev_at = now
        elapsed = None if prev_at is None else now - prev_at
        cpu = {}
        if elapsed is not None and elapsed > 0:
            for pid, s in procs.items():
                before = prev.get((pid, s[6]))
                delta = 0 if before is None else (s[2] + s[3]) - before
                cpu[pid] = self._pct(delta, elapsed)
        # Sessions walk even on the baseline pass, so their own cumulative baseline is laid down with it.
        sessions = self._sessions(procs, elapsed)
        if not procs or elapsed is None or elapsed <= 0:
            self._latest = None   # the baseline walk
            return
        self._latest = {"top_processes": self._top(procs, cpu), "sessions": sessions}

    def _pct(self, ticks: float, elapsed: float) -> float:
        """Ticks over `elapsed` seconds as percent of one core, clamped to 0..all cores: the walk reads each
        process at a slightly different instant, so a pegged host can compute a hair over its ceiling."""
        return min(self._ceiling, max(0.0, 100.0 * ticks / CLK_TCK / elapsed))

    def _top(self, procs: dict, cpu: dict) -> list:
        by_cpu = sorted(procs, key=lambda p: cpu[p], reverse=True)[:TOP_N]
        by_rss = sorted(procs, key=lambda p: procs[p][7], reverse=True)[:TOP_N]
        chosen = list(dict.fromkeys(by_cpu + by_rss))
        chosen.sort(key=lambda p: (cpu[p], procs[p][7]), reverse=True)
        return [{"name": _clean(procs[p][0], _NAME_CAP), "user": _user_of(p),
                 "cpu_pct": round(cpu[p], 1), "rss_kb": max(0, procs[p][7]) * PAGE_KB} for p in chosen]

    def _sessions(self, procs: dict, elapsed: float | None) -> list:
        """Each live runner's process tree. CPU is the change in the tree's CUMULATIVE ticks (live utime+stime
        plus every member's reaped children) since the previous walk, so a compiler or test worker that lived
        and died between two walks is still counted; a pid's own delta would miss it."""
        try:
            runners = self._runners()
        except Exception as exc:   # noqa: BLE001 — the registry is best-effort here
            log.debug("process_metrics: cannot read the runner registry: %s", exc)
            return []
        prev_sessions, self._prev_sessions = self._prev_sessions, {}
        children: dict = {}
        for pid, s in procs.items():
            children.setdefault(s[1], []).append(pid)
        out = []
        for runner in runners:
            pane = int(getattr(runner, "pane_pid", 0) or 0)
            task = str(getattr(runner, "task_id", ""))
            if pane <= 0 or pane not in procs or not task.isdigit() or int(task) <= 0:
                continue
            tree, stack = [], [pane]
            seen = set()
            while stack:
                pid = stack.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                tree.append(pid)
                stack.extend(children.get(pid, ()))
            # Every member's own and reaped-children time: a tool call's pytest lands in its bash's cutime,
            # which is in the tree, so nothing is counted twice and nothing reaped below the pane is lost.
            ticks = sum(procs[p][2] + procs[p][3] + procs[p][4] + procs[p][5] for p in tree)
            session_id = _clean(str(getattr(runner, "session_id", "") or ""), _SESSION_ID_CAP)
            key = (int(task), session_id, pane, procs[pane][6])
            self._prev_sessions[key] = ticks
            before = prev_sessions.get(key)
            if elapsed is None or elapsed <= 0 or before is None:
                continue   # no baseline for this session yet
            out.append({
                "task_id": int(task),
                "session_id": session_id,
                "pid": pane,
                # Negative only when a member is reparented away (its time leaves the tree): no rate, not < 0.
                "cpu_pct": round(self._pct(ticks - before, elapsed), 1),
                "rss_kb": sum(max(0, procs[p][7]) for p in tree) * PAGE_KB,
                "procs": len(tree),
                "cpu_seconds": round(max(0, ticks) / CLK_TCK, 2),
            })
            if len(out) >= MAX_SESSIONS:
                break
        return out

    def reading(self) -> dict | None:
        """`{"top_processes": [...], "sessions": [...]}` from the latest walk, or None before the second
        walk, after a failed one, or off Linux."""
        return self._latest
