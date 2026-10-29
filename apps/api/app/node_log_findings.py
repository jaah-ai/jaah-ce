"""Node log findings: daemon trouble read off the journal a node ships (`POST /api/agent/node-logs`).

The single home for the rule. Two daemon warnings mean the machine is not healthy even while it keeps
heartbeating, so the Health tab must say so:

* `nodelink_store_unresponsive` — `apps/jaahd/shared_storage.py`'s "<path> did not answer a mount check in
  …s (a dead NFS server?)": the NodeLink store (an NFS/EFS mount) hangs.
* `step_wedged` — `apps/jaahd/main.py`'s "<step> has been in flight for over …s and is skipping its firings":
  a periodic step or scheduled task has not returned for far longer than its interval.
* `engine_unready` — a Windows node's "container engine not ready: …" (`apps/jaahd/windows_engine_health.py`,
  posted by the daemon itself, as a Windows node has no journal): its `jaah-engine` WSL 2 distro is not answering
  `docker info`, so nothing that needs a container can run there.
* `oom_kill` — the kernel's `oom-kill:…,task_memcg=<cgroup>,task=<name>,pid=<n>,…` line (shipped as
  `unit: "kernel"`, `apps/jaahd/node_logs.py`), or systemd's "<unit>: A process of this unit has been killed by
  the OOM killer.": the machine ran out of memory and killed a process.
* `converge_failed` — the package's converge (its postinst running the node-reprovision script, in the update unit's
  journal) refused its credential layer: "… convergence failed — re-run: …", or the specific refusal
  before it ("git <v> is below the <floor> floor the git credential helper needs"). The helper was never installed,
  so every clone on the node fails. A converge runs only on a package update, so this kind stays active for
  `CONVERGE_WINDOW`, and the next converge's helper install line ("installed …/jaah-git-credential") clears it.

`nodes.log_findings` keeps, per kind, the current episode: `{"first_at", "last_at", "detail"}` (ISO
timestamps from the journal entry). A kind is ACTIVE while its `last_at` is within `FINDING_WINDOW`: the
daemon re-warns every few minutes while the condition lasts, so the window covers several missed
shipments, and a finding clears itself once the warnings stop.

Three more kinds are not journal lines but the node's own host series (`NodeHostMetric`), computed at read
(`pressure`): `memory_critical` and `cpu_critical`, active while the newest bucket at or above
`node_host_metrics.CRITICAL_PCT` is within the same `FINDING_WINDOW` — one "recent trouble" bound for every
kind, so a finding outlasts the placement gate's 10-minute recovery rather than an hour past it — and
`tmp_inodes_high`, the same rule over /tmp's inode use above `node_host_metrics.TMP_INODE_WARN_PCT`.

One more is read off the node's `sessions` rows (`relaunch_storm`): `relaunch_storm`, the same task's runner
ending `vanished` again and again within minutes — the control plane relaunching into a node that keeps losing it.

`record` is the ingest route's one call; it also runs `app/node_log_history.py`'s fold (updates, GHA jobs,
recent errors).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
import math
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import node_host_metrics, node_log_history
from app.clock import as_utc, parse_utc
from app.models.agent_sessions import AgentSession
from app.models.nodes import Node, NodeHostMetric
from app.runner_terminations import RUNNER_LOST_CAUSES

FINDING_WINDOW = timedelta(minutes=30)

NODELINK_STORE_UNRESPONSIVE = "nodelink_store_unresponsive"
STEP_WEDGED = "step_wedged"
OOM_KILL = "oom_kill"
MEMORY_CRITICAL = "memory_critical"
CPU_CRITICAL = "cpu_critical"
RELAUNCH_STORM = "relaunch_storm"
TMP_INODES_HIGH = "tmp_inodes_high"
ENGINE_UNREADY = "engine_unready"
FLEET_ANCHOR_UNVERIFIED = "fleet_anchor_unverified"
CONVERGE_FAILED = "converge_failed"

# The journal kinds, in display order: the store first, as a hung store is the usual cause of a wedged step.
KINDS = (CONVERGE_FAILED, NODELINK_STORE_UNRESPONSIVE, STEP_WEDGED, ENGINE_UNREADY, FLEET_ANCHOR_UNVERIFIED,
         OOM_KILL)
# The host-series kinds (`pressure`), served before the journal kinds: pressure is the usual cause of the rest.
PRESSURE_KINDS = (MEMORY_CRITICAL, CPU_CRITICAL, TMP_INODES_HIGH)

LABELS = {
    NODELINK_STORE_UNRESPONSIVE: "NodeLink store not answering (NFS)",
    STEP_WEDGED: "Daemon step wedged",
    OOM_KILL: "Process OOM-killed",
    MEMORY_CRITICAL: "Memory critical",
    CPU_CRITICAL: "CPU critical",
    RELAUNCH_STORM: "Relaunch storm",
    TMP_INODES_HIGH: "/tmp inodes high",
    ENGINE_UNREADY: "Container engine down",
    FLEET_ANCHOR_UNVERIFIED: "Runners unverifiable",
    CONVERGE_FAILED: "Node converge failed",
}
# A converge runs once per package update, not every few minutes, so its refusal outlives `FINDING_WINDOW`.
CONVERGE_WINDOW = timedelta(days=30)
_WINDOWS = {CONVERGE_FAILED: CONVERGE_WINDOW}


def _window(kind: str) -> timedelta:
    return _WINDOWS.get(kind, FINDING_WINDOW)


# The journal line carries the daemon's own prefix ("<ts> WARNING jaahd…: "), so both are searched, not matched.
# The text is the node's own, so the scan is bounded three ways: only lines carrying the literal mark are searched,
# only their first `_SCAN_CHARS`, and every capture has a length cap — an unanchored `\S+` over a long run of
# non-space would otherwise backtrack quadratically and hold the worker (security review).
_SCAN_CHARS = 2048
_STORE_MARK = " did not answer a mount check"
_STEP_MARK = " has been in flight for over "
_STORE_RE = re.compile(r"(\S{1,512}) did not answer a mount check")
_STEP_RE = re.compile(
    r"(?:periodic step (\S{1,256})|scheduled task '([^']{1,256})') has been in flight for over (\d{1,12})s"
)
# `windows_engine_health.MARK`'s twin.
_ENGINE_MARK = "container engine not ready: "
_ENGINE_RE = re.compile(r"container engine not ready: (.{1,512})")
# `windows_anchor_health.MARK`'s twin.
_ANCHOR_MARK = "fleet server anchor unverified: "
_ANCHOR_RE = re.compile(r"fleet server anchor unverified: (.{1,512})")
_KERNEL_OOM_MARK = "oom-kill:"
_SYSTEMD_OOM_MARK = ": A process of this unit has been killed by the OOM killer"
_OOM_MEMCG_RE = re.compile(r"task_memcg=([^,\s]{1,1024})")
_OOM_TASK_RE = re.compile(r"[,:]task=([^,\s]{1,256})")
_OOM_PID_RE = re.compile(r"[,:]pid=(\d{1,10})")
_SYSTEMD_OOM_UNIT_RE = re.compile(r"(\S{1,256}): A process of this unit has been killed by the OOM killer")
_UNIT_SUFFIXES = (".service", ".scope", ".slice")
# systemd's line names no task; the kernel's line for the same kill does, and stays the detail within this.
_SAME_KILL = timedelta(minutes=2)
_SYSTEMD_OOM_DETAIL = "killed a process in "
# The package postinst's line for a refused credential layer, and `node_reprovision.ensure_git_credential_helper`'s
# refusal, which precedes it and names the cause; the generic line keeps that detail (`record`).
_CONVERGE_MARK = " convergence failed — re-run"
_CONVERGE_RE = re.compile(r"jaah: (\S{1,256}) convergence failed — re-run")
_GIT_FLOOR_MARK = " floor the git credential helper needs"
_GIT_FLOOR_RE = re.compile(r"git (\S{1,64}) is below the (\S{1,32}) floor the git credential helper needs")
# The helper's install line: a later converge got past the refusal.
_CONVERGE_OK_MARK = "[node-reprovision] installed /usr/lib/jaahd/jaah-git-credential "
_GENERIC_CONVERGE_DETAIL = " convergence failed; credential layer refused"


class _Entry(Protocol):
    ts: datetime
    message: str


def _memcg_unit(memcg: str) -> str:
    """The last component of a cgroup path that is a systemd unit (`.service`/`.scope`/`.slice`), else ""."""
    for part in reversed(memcg.split("/")):
        if part.endswith(_UNIT_SUFFIXES):
            return part
    return ""


def _oom_detail(text: str, unit: str) -> str | None:
    if _KERNEL_OOM_MARK in text:
        memcg = _OOM_MEMCG_RE.search(text)
        task = _OOM_TASK_RE.search(text)
        pid = _OOM_PID_RE.search(text)
        where = _memcg_unit(memcg.group(1)) if memcg else ""
        what = task.group(1) if task else "a process"
        if pid:
            what += f" (pid {pid.group(1)})"
        return f"killed {what} in {where}" if where else f"killed {what}"
    if _SYSTEMD_OOM_MARK in text:
        named = _SYSTEMD_OOM_UNIT_RE.search(text)
        return _SYSTEMD_OOM_DETAIL + (named.group(1) if named else unit or "a unit")
    return None


def classify(message: str, unit: str = "") -> tuple[str, str] | None:
    """`(kind, detail)` for a journal line that is a finding, else None. `unit` is the entry's own (systemd's
    OOM line names it in its text too; this is the fallback)."""
    text = message[:_SCAN_CHARS]
    oom = _oom_detail(text, unit)
    if oom is not None:
        return OOM_KILL, oom
    store = _STORE_RE.search(text) if _STORE_MARK in text else None
    if store:
        return NODELINK_STORE_UNRESPONSIVE, f"{store.group(1)} did not answer a mount check"
    step = _STEP_RE.search(text) if _STEP_MARK in text else None
    if step:
        return STEP_WEDGED, f"{step.group(1) or step.group(2)}: in flight over {step.group(3)}s"
    engine = _ENGINE_RE.search(text) if _ENGINE_MARK in text else None
    if engine:
        return ENGINE_UNREADY, engine.group(1)
    anchor = _ANCHOR_RE.search(text) if _ANCHOR_MARK in text else None
    if anchor:
        return FLEET_ANCHOR_UNVERIFIED, anchor.group(1)
    floor = _GIT_FLOOR_RE.search(text) if _GIT_FLOOR_MARK in text else None
    if floor:
        return CONVERGE_FAILED, (f"git {floor.group(1)} is below the {floor.group(2)} floor; "
                                 "git credential helper not installed")
    converge = _CONVERGE_RE.search(text) if _CONVERGE_MARK in text else None
    if converge:
        return CONVERGE_FAILED, converge.group(1) + _GENERIC_CONVERGE_DETAIL
    return None


def record(row: Node, entries: Iterable[_Entry], now: datetime | None = None) -> bool:
    """Fold the findings among `entries` into `row.log_findings`; whether anything changed.

    An episode's `first_at` survives while the previous `last_at` is within `FINDING_WINDOW` of the new
    line, else the episode restarts. The column is reassigned as a new dict (JSONB change tracking is by
    identity). An OOM kill's detail is the kernel's (it names the task) over systemd's generic line for the
    same unit just after it.

    Also folds the batch into `app/node_log_history.py`'s lists; the result says whether either changed."""
    # A line stamped ahead of `now` (a fast node clock) is read as now: it may not keep a finding up past its window.
    ceiling = as_utc(now) if now is not None else datetime.now(timezone.utc)
    current = dict(row.log_findings or {})
    changed = False
    entries = list(entries)
    for entry in entries:
        found = classify(entry.message, str(getattr(entry, "unit", "") or ""))
        if found is None:
            if _CONVERGE_OK_MARK in entry.message[:_SCAN_CHARS] and CONVERGE_FAILED in current:
                failed_last = parse_utc((current[CONVERGE_FAILED] or {}).get("last_at"))
                if failed_last is None or min(as_utc(entry.ts), ceiling) > failed_last:
                    del current[CONVERGE_FAILED]
                    changed = True
            continue
        kind, detail = found
        at = min(as_utc(entry.ts), ceiling)
        prior = current.get(kind) or {}
        prior_last = parse_utc(prior.get("last_at"))
        if prior_last is not None and at <= prior_last:
            continue  # an older or re-sent line: the episode already holds a later one
        same_episode = prior_last is not None and at - prior_last <= _window(kind)
        prior_detail = str(prior.get("detail") or "")
        if (
            kind == CONVERGE_FAILED and detail.endswith(_GENERIC_CONVERGE_DETAIL) and prior_last is not None
            and at - prior_last <= _SAME_KILL and not prior_detail.endswith(_GENERIC_CONVERGE_DETAIL)
        ):
            detail = prior_detail
        if (
            kind == OOM_KILL and detail.startswith(_SYSTEMD_OOM_DETAIL) and prior_last is not None
            and at - prior_last <= _SAME_KILL and not prior_detail.startswith(_SYSTEMD_OOM_DETAIL)
            and prior_detail.endswith(" in " + detail[len(_SYSTEMD_OOM_DETAIL):])
        ):
            detail = prior_detail
        current[kind] = {
            "first_at": prior.get("first_at") if same_episode else at.isoformat(),
            "last_at": at.isoformat(),
            "detail": detail,
        }
        changed = True
    if changed:
        row.log_findings = current
    history_changed = node_log_history.fold(row, entries, now=ceiling)
    return changed or history_changed


def active(findings: dict | None, now: datetime) -> list[tuple[str, dict]]:
    """`(kind, finding)` for every kind whose `last_at` is within its window (`FINDING_WINDOW`, or
    `CONVERGE_WINDOW`) of `now`, in `KINDS` order."""
    out: list[tuple[str, dict]] = []
    for kind in KINDS:
        finding = (findings or {}).get(kind)
        last = parse_utc(finding.get("last_at")) if isinstance(finding, dict) else None
        if last is not None and now - last <= _window(kind):
            out.append((kind, finding))
    return out


def recent(findings: dict | None, now: datetime, within: timedelta = timedelta(hours=24)) -> list[tuple[str, dict]]:
    """`(kind, finding)` for every kind whose episode ENDED inside `within` but past its window, in `KINDS`
    order: the trouble that cleared before the reader looked, which `active` no longer serves.

    Exactly the complement of `active` up to `within`, so a finding is in one list or the other, never both.
    Only the newest episode per kind exists (`record` keeps one), so an earlier one that day is not here."""
    out: list[tuple[str, dict]] = []
    for kind in KINDS:
        finding = (findings or {}).get(kind)
        if not isinstance(finding, dict):
            continue
        last = parse_utc(finding.get("last_at"))
        if last is not None and _window(kind) < now - last <= within:
            out.append((kind, finding))
    return out


# ── Host pressure: the node's own series, read at request time ─────────────────────────────────


# The run may span far more than the window (the 2026-10-17 h2 hour); a day of minute buckets bounds the walk.
PRESSURE_LOOKBACK = timedelta(hours=24)
# Missing buckets between two critical ones do not end a run: a node stops reporting when it is starved, which
# is the peak itself (h2 went silent for 9 minutes at 93%). Only a sub-critical bucket ends it, or a silence
# longer than the finding window (the node was gone, not busy).
MAX_SILENCE = FINDING_WINDOW


@dataclass(frozen=True)
class Bucket:
    at: datetime
    cpu_pct: Optional[float]
    mem_pct: Optional[float]
    cpu_count: Optional[int]
    mem_total_kb: Optional[int]
    # Used share of /tmp's inode table (`node_host_metrics.tmp_inode_used_pct`); None when unreported.
    tmp_inode_pct: Optional[float] = None


def _critical(pct: Optional[float]) -> bool:
    return node_host_metrics.classify(pct) == node_host_metrics.LEVEL_CRITICAL


def _tmp_inodes_high(pct: Optional[float]) -> bool:
    return node_host_metrics.classify_tmp_inodes(pct) == node_host_metrics.LEVEL_WARN


def _run(
    buckets: Sequence[Bucket], field: str, now: datetime, judge: Callable[[Optional[float]], bool] = _critical,
) -> Optional[list[Bucket]]:
    """The contiguous run `judge` flags (critical by default) ending at the newest flagged bucket on `field`,
    newest first, when that bucket is within `FINDING_WINDOW` of `now`; else None. `buckets` is newest first."""
    newest = next((b for b in buckets if judge(getattr(b, field))), None)
    if newest is None or now - as_utc(newest.at) > FINDING_WINDOW:
        return None
    run = [newest]
    for bucket in buckets[buckets.index(newest) + 1:]:
        if not judge(getattr(bucket, field)) or as_utc(run[-1].at) - as_utc(bucket.at) > MAX_SILENCE:
            break
        run.append(bucket)
    return run


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def pressure_from(buckets: Sequence[Bucket], now: datetime) -> list[tuple[str, dict]]:
    """`(kind, finding)` for `memory_critical` / `cpu_critical` / `tmp_inodes_high`, in `PRESSURE_KINDS` order,
    off `buckets` (newest first). Pure."""
    out: list[tuple[str, dict]] = []
    memory = _run(buckets, "mem_pct", now)
    if memory is not None:
        newest, peak = memory[0], max(b.mem_pct for b in memory if b.mem_pct is not None)
        detail = f"memory {_pct(newest.mem_pct)} (peak {_pct(peak)})"
        total_kb = next((b.mem_total_kb for b in memory if b.mem_total_kb), None)
        if total_kb:
            detail += f" of {total_kb / node_host_metrics.KB_PER_GB:.1f} GB"
        out.append((MEMORY_CRITICAL, _finding(memory, detail)))
    cpu = _run(buckets, "cpu_pct", now)
    if cpu is not None:
        detail = f"CPU {_pct(cpu[0].cpu_pct)}"
        cores = next((b.cpu_count for b in cpu if b.cpu_count), None)
        if cores:
            detail += f" across {cores} cores"
        out.append((CPU_CRITICAL, _finding(cpu, detail)))
    tmp = _run(buckets, "tmp_inode_pct", now, _tmp_inodes_high)
    if tmp is not None:
        detail = f"/tmp inodes {_pct(tmp[0].tmp_inode_pct)} used — mktemp fails at 100%"
        out.append((TMP_INODES_HIGH, _finding(tmp, detail)))
    return out


def _finding(run: list[Bucket], detail: str) -> dict:
    return {"first_at": as_utc(run[-1].at).isoformat(), "last_at": as_utc(run[0].at).isoformat(), "detail": detail}


def pressure(session: Session, node_uuid, now: datetime) -> list[tuple[str, dict]]:
    """`pressure_from` over the node's buckets of the last `PRESSURE_LOOKBACK` (`now` is the DB's clock)."""
    rows = session.execute(
        select(
            NodeHostMetric.bucket_at, NodeHostMetric.cpu_pct, NodeHostMetric.mem_pct,
            NodeHostMetric.cpu_count, NodeHostMetric.mem_total_kb,
            NodeHostMetric.tmp_inodes_free, NodeHostMetric.tmp_inodes_total,
        )
        .where(NodeHostMetric.node_id == node_uuid, NodeHostMetric.bucket_at >= now - PRESSURE_LOOKBACK)
        .order_by(NodeHostMetric.bucket_at.desc())
    ).all()
    buckets = [
        Bucket(at, cpu if cpu is None or math.isfinite(cpu) else None,
               mem if mem is None or math.isfinite(mem) else None, count, total,
               node_host_metrics.tmp_inode_used_pct(inodes_free, inodes_total))
        for at, cpu, mem, count, total, inodes_free, inodes_total in rows
    ]
    return pressure_from(buckets, as_utc(now))


# ── Relaunch storm: the node's own sessions, read at request time ──────────────────────────────


# A storm is `STORM_MIN_SESSIONS` or more sessions of ONE task ending `vanished` within `STORM_SPAN`; one
# vanished runner is a reclaim, three in a quarter hour is the control plane relaunching into a node that keeps
# losing it. `STORM_LOOKBACK` bounds the read (`end_reason` is not indexed; `node_id` is).
STORM_MIN_SESSIONS = 3
STORM_SPAN = timedelta(minutes=15)
STORM_LOOKBACK = timedelta(hours=24)


@dataclass(frozen=True)
class StormFinding:
    """`(first_at, last_at, detail)` as every finding, plus the tasks and the count the console links."""

    first_at: datetime
    last_at: datetime
    task_ids: tuple[int, ...]
    count: int

    def as_finding(self) -> dict:
        tasks = ", ".join(f"#{t}" for t in self.task_ids)
        noun = "task" if len(self.task_ids) == 1 else "tasks"
        return {
            "first_at": self.first_at.isoformat(), "last_at": self.last_at.isoformat(),
            "detail": f"{self.count} runners vanished · {noun} {tasks}",
            "task_ids": list(self.task_ids), "count": self.count,
        }


def relaunch_storm_from(ends: Iterable[tuple[int, datetime]]) -> Optional[StormFinding]:
    """The storm among `(task_id, ended_at)` of vanished sessions, else None. Pure.

    A session is in a storm when it and at least `STORM_MIN_SESSIONS - 1` others of its task ended within one
    `STORM_SPAN`; the count is those sessions, the span their first and last end, over every storming task."""
    by_task: dict[int, list[datetime]] = {}
    for task_id, ended in ends:
        by_task.setdefault(task_id, []).append(as_utc(ended))
    members: dict[int, list[datetime]] = {}
    for task_id, times in by_task.items():
        times.sort()
        hit: set[int] = set()
        start = 0
        for end in range(len(times)):
            while times[end] - times[start] > STORM_SPAN:
                start += 1
            if end - start + 1 >= STORM_MIN_SESSIONS:
                hit.update(range(start, end + 1))
        if hit:
            members[task_id] = [times[i] for i in sorted(hit)]
    if not members:
        return None
    every = [t for times in members.values() for t in times]
    return StormFinding(
        first_at=min(every), last_at=max(every), task_ids=tuple(sorted(members)), count=len(every)
    )


def relaunch_storm(
    session: Session, node_uuid, now: datetime, *, org_id=None
) -> list[tuple[str, dict]]:
    """`[(RELAUNCH_STORM, finding)]` off the node's sessions that ended `vanished` in the last `STORM_LOOKBACK`
    (`now` is the DB's clock), else `[]`. `org_id` narrows to one org's sessions (a tenant reader is never told
    another org's task ids)."""
    query = select(AgentSession.task_id, AgentSession.ended_at).where(
        AgentSession.node_id == node_uuid,
        AgentSession.end_reason.in_(sorted(RUNNER_LOST_CAUSES)),
        AgentSession.task_id.is_not(None),
        AgentSession.ended_at >= now - STORM_LOOKBACK,
    )
    if org_id is not None:
        query = query.where(AgentSession.org_id == org_id)
    storm = relaunch_storm_from(session.execute(query).all())
    return [] if storm is None else [(RELAUNCH_STORM, storm.as_finding())]
