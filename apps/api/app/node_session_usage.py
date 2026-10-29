"""node_session_usage.py — the single home of every rule over a runner session's CPU/RAM.

The node reports each live runner session's process tree (rooted at the pane pid) as the optional `sessions`
list inside `host_metrics` (`apps/jaahd/host_metrics.py`; wire plausibility: `app/fleet_observe.py`). Two
tables hold it (`app/models/nodes.py::NodeSessionUsage`, `::TaskSessionUsagePeak`):

  * the minute series, on `node_host_metrics`' grain and retention (never re-spelled here) — the host's live
    Sessions table (a session is live for `LIVE_BOUND_SECONDS`) and a task's per-session chart;
  * the peaks, one row per (task, node, session), `GREATEST`-upserted and never pruned — run telemetry.

Both are written under the NODE's org, and only for a task the CP itself places on that node (`_tasks_on`), at
most `MAX_SESSIONS_PER_TASK` sessions a task a pass: `task_id` is node-attested, so the write is bounded to what
the node runs. Every read that names a task still joins it only where the task's org equals the row's. Ages are
the database's clock, as for the host series. The builders are statements only; `app/fleet_observe.py` runs them as
CTEs riding the host-metrics upsert, so a pass pays no extra round trip.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger, Float, Integer, Text, and_, cast, column, delete, extract, func, literal, select, union,
)
from sqlalchemy import values as sql_values
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import node_host_metrics
from app.models.agent_sessions import JaahSession
from app.models.nodes import NodeSessionUsage, TaskSessionUsagePeak
from app.models.tasks import Task
from app.models.worktrees import Worktree


# ── The writers ──────────────────────────────────────────────────────────────────

# The most distinct sessions one task may report on one node in one pass. A task runs one runner at a time, so a
# few cover a relaunch's overlap; the cap bounds the rows a hostile node can mint under a task it does run.
MAX_SESSIONS_PER_TASK = 4


def _keyed(entries: Iterable[Any]) -> list[Any]:
    """`entries` with one per `(task_id, session_id)` and at most `MAX_SESSIONS_PER_TASK` per task, the first
    kept: a repeated key in one multi-row `ON CONFLICT DO UPDATE` raises `21000` and would roll back the whole
    pass. Duck-typed callers bypass the wire model, so the builder enforces it."""
    seen: set[tuple[int, str]] = set()
    per_task: dict[int, int] = {}
    kept = []
    for entry in entries:
        key = (entry.task_id, entry.session_id)
        if key in seen or per_task.get(entry.task_id, 0) >= MAX_SESSIONS_PER_TASK:
            continue
        seen.add(key)
        per_task[entry.task_id] = per_task.get(entry.task_id, 0) + 1
        kept.append(entry)
    return kept


def _tasks_on(node_uuid: UUID):
    """The tasks the CP itself places on this node: a slot it holds, or an open Jaah session it was dispatched
    under. A security bound, not a nicety: `task_id` is node-attested, so without it a node could mint rows under
    any id. Written by the CP alone, so a node cannot widen it."""
    return union(
        select(Worktree.task_id).where(Worktree.node_id == node_uuid, Worktree.task_id.is_not(None)),
        select(JaahSession.task_id).where(
            JaahSession.node_id == node_uuid, JaahSession.ended_at.is_(None), JaahSession.task_id.is_not(None)
        ),
    )


def _reported(entries: list[Any]):
    """The kept entries as a `VALUES` relation, so the inserts select from it WHERE the task runs here — one
    statement either way, so the observe pass's budget holds."""
    return sql_values(
        column("task_id", BigInteger), column("session_id", Text), column("pid", Integer),
        column("cpu_pct", Float(precision=53)), column("rss_kb", BigInteger), column("procs", Integer),
        column("cpu_seconds", Float(precision=53)),
        name="reported",
    ).data([
        (entry.task_id, entry.session_id, entry.pid, entry.cpu_pct, entry.rss_kb, entry.procs, entry.cpu_seconds)
        for entry in entries
    ])


def upsert_statement(node_uuid: UUID, org_id: UUID, entries: Iterable[Any]):
    """This minute's row per session: INSERT … SELECT … ON CONFLICT (node, task, session, bucket) DO UPDATE, last
    write wins (each post is the latest figure), for sessions of a task running on this node (`_tasks_on`) only.
    `org_id` is the node's, as the observe pass's stamp returned it (`stamp_org` cannot see a `pg_insert`; the
    composite key accepts only that value). `None` for no entries — there is nothing to execute. The bucket is the database's `now()`, never the node's
    clock (`node_host_metrics.upsert_statement` says why)."""
    kept = _keyed(entries)
    if not kept:
        return None
    reported = _reported(kept)
    insert = pg_insert(NodeSessionUsage).from_select(
        ["node_id", "org_id", "task_id", "session_id", "bucket_at", "cpu_pct", "rss_kb", "procs", "pid", "updated_at"],
        select(
            _literal_node(node_uuid), _literal_org(org_id), reported.c.task_id, reported.c.session_id,
            node_host_metrics._bucket_expr(func.now(), node_host_metrics.BUCKET_SECS), reported.c.cpu_pct,
            reported.c.rss_kb, reported.c.procs, reported.c.pid, func.now(),
        ).where(reported.c.task_id.in_(_tasks_on(node_uuid))),
    )
    return insert.on_conflict_do_update(
        index_elements=[
            NodeSessionUsage.node_id, NodeSessionUsage.task_id, NodeSessionUsage.session_id,
            NodeSessionUsage.bucket_at,
        ],
        set_={
            "cpu_pct": insert.excluded.cpu_pct,
            "rss_kb": insert.excluded.rss_kb,
            "procs": insert.excluded.procs,
            "pid": insert.excluded.pid,
            "updated_at": func.now(),
        },
    )


def peaks_statement(node_uuid: UUID, org_id: UUID, entries: Iterable[Any]):
    """Each session's peaks: INSERT … SELECT … ON CONFLICT (task, node, session) DO UPDATE with `GREATEST` per
    figure and `last_seen_at = now()`, for a task running on this node only (`_tasks_on`). `cpu_seconds` is
    monotone per session, so its max is its latest. `None` for no entries. The org is never re-stamped: a row
    keeps the org that wrote it."""
    kept = _keyed(entries)
    if not kept:
        return None
    reported = _reported(kept)
    insert = pg_insert(TaskSessionUsagePeak).from_select(
        ["org_id", "task_id", "node_id", "session_id", "peak_rss_kb", "peak_cpu_pct", "cpu_seconds"],
        select(
            _literal_org(org_id), reported.c.task_id, _literal_node(node_uuid), reported.c.session_id,
            reported.c.rss_kb, reported.c.cpu_pct, reported.c.cpu_seconds,
        ).where(reported.c.task_id.in_(_tasks_on(node_uuid))),
    )
    return insert.on_conflict_do_update(
        index_elements=[TaskSessionUsagePeak.task_id, TaskSessionUsagePeak.node_id, TaskSessionUsagePeak.session_id],
        set_={
            "peak_rss_kb": func.greatest(TaskSessionUsagePeak.peak_rss_kb, insert.excluded.peak_rss_kb),
            "peak_cpu_pct": func.greatest(TaskSessionUsagePeak.peak_cpu_pct, insert.excluded.peak_cpu_pct),
            "cpu_seconds": func.greatest(TaskSessionUsagePeak.cpu_seconds, insert.excluded.cpu_seconds),
            "last_seen_at": func.now(),
        },
    )


def _literal_node(node_uuid: UUID):
    """The node's uuid as a typed bound value for a `SELECT` list."""
    return literal(node_uuid, PG_UUID(as_uuid=True))


def _literal_org(org_id: UUID):
    """The node's org as a typed bound value for a `SELECT` list."""
    return literal(org_id, PG_UUID(as_uuid=True))


def prune_statement(node_uuid: UUID):
    """Drop this node's session minutes older than `node_host_metrics.RETENTION_DAYS` — one node's rows, on the
    ingest path, as `node_host_metrics.prune_statement`. The peaks are never pruned."""
    return delete(NodeSessionUsage).where(
        NodeSessionUsage.node_id == node_uuid,
        NodeSessionUsage.bucket_at < node_host_metrics._db_now_minus(node_host_metrics.RETENTION_SECONDS),
    )


# ── The readers ──────────────────────────────────────────────────────────────────

# How old a session's newest minute may be and still count as running: two buckets (the minute floor plus one
# missed minute) and 30s of slack. Tighter than `node_host_metrics.FRESH_BOUND_SECONDS`, which bounds a WINDOWED
# mean; a session that ended should leave the live table within ~2 minutes, not 5.
LIVE_BOUND_SECONDS = 2 * node_host_metrics.BUCKET_SECS + 30.0

@dataclass(frozen=True)
class LiveSession:
    """One session's newest minute on one node. `task_title` is None when the task is gone or not the row's
    org's."""

    node_id: UUID
    task_id: int
    task_title: Optional[str]
    session_id: str
    pid: Optional[int]
    cpu_pct: float
    rss_kb: int
    procs: int
    bucket_at: datetime
    age_seconds: float

    @property
    def stale(self) -> bool:
        """Past `LIVE_BOUND_SECONDS` (strictly greater, as `LatestReading.stale`)."""
        return self.age_seconds > LIVE_BOUND_SECONDS


def live_sessions(session: Session, node_uuids: Iterable[UUID]) -> list[LiveSession]:
    """The newest minute of every session on `node_uuids` that is still fresh, largest RSS first.

    One statement: `DISTINCT ON (node, task, session)` over the last `LIVE_BOUND_SECONDS` only (down
    `ix_node_session_usage_node_bucket`), so it reads a few minutes of rows, never the week. A session whose newest minute is older has ended (or its node went
    quiet) and is not listed."""
    wanted = list(node_uuids)
    if not wanted:
        return []
    age = cast(extract("epoch", func.now() - NodeSessionUsage.bucket_at), Float(precision=53))
    newest = (
        select(
            NodeSessionUsage.node_id, NodeSessionUsage.task_id, NodeSessionUsage.session_id, NodeSessionUsage.pid,
            NodeSessionUsage.cpu_pct, NodeSessionUsage.rss_kb, NodeSessionUsage.procs, NodeSessionUsage.bucket_at,
            NodeSessionUsage.org_id, age.label("age_seconds"),
        )
        .where(
            NodeSessionUsage.node_id.in_(wanted),
            NodeSessionUsage.bucket_at >= node_host_metrics._db_now_minus(LIVE_BOUND_SECONDS),
        )
        .distinct(NodeSessionUsage.node_id, NodeSessionUsage.task_id, NodeSessionUsage.session_id)
        .order_by(
            NodeSessionUsage.node_id, NodeSessionUsage.task_id, NodeSessionUsage.session_id,
            NodeSessionUsage.bucket_at.desc(),
        )
        .subquery("newest")
    )
    rows = session.execute(
        select(newest, Task.title)
        # The title only for a task of the row's own org.
        .outerjoin(Task, and_(Task.id == newest.c.task_id, Task.org_id == newest.c.org_id))
    ).all()
    found = [
        LiveSession(
            node_id=row.node_id, task_id=row.task_id, task_title=row.title, session_id=row.session_id,
            pid=row.pid, cpu_pct=row.cpu_pct, rss_kb=row.rss_kb, procs=row.procs, bucket_at=row.bucket_at,
            age_seconds=row.age_seconds,
        )
        for row in rows
    ]
    return sorted(
        (live for live in found if not live.stale), key=lambda live: (-live.rss_kb, str(live.node_id), live.task_id)
    )


@dataclass(frozen=True)
class SessionPoint:
    """One plotted bucket: its start and the session's mean CPU/RAM over it."""

    at: datetime
    cpu_pct: float
    rss_kb: int


@dataclass
class TaskSession:
    """One session of a task on one node: its chart and its lifetime peaks (None when no peak row exists)."""

    node_id: UUID
    session_id: str
    points: list[SessionPoint] = field(default_factory=list)
    peak_rss_kb: Optional[int] = None
    peak_cpu_pct: Optional[float] = None
    cpu_seconds: Optional[float] = None


def task_sessions(session: Session, task_id: int, task_org_id: UUID, *, hours: float) -> list[TaskSession]:
    """Every session of `task_id` with minutes in the last `hours` or a peak row, its series re-bucketed by
    `node_host_metrics.bucket_secs_for` (the host chart's width) — only rows written under `task_org_id`, the
    task's own org. Ordered by first point, then node and session, so the chart's legend is stable."""
    width = node_host_metrics.bucket_secs_for(hours * 60.0 * 60.0)
    bucket = node_host_metrics._bucket_expr(NodeSessionUsage.bucket_at, width).label("at")
    rows = session.execute(
        select(
            NodeSessionUsage.node_id, NodeSessionUsage.session_id, bucket,
            func.avg(NodeSessionUsage.cpu_pct).label("cpu_pct"), func.avg(NodeSessionUsage.rss_kb).label("rss_kb"),
        )
        .where(
            NodeSessionUsage.task_id == task_id,
            NodeSessionUsage.org_id == task_org_id,
            NodeSessionUsage.bucket_at >= node_host_metrics._db_now_minus(hours * 60.0 * 60.0),
        )
        .group_by(NodeSessionUsage.node_id, NodeSessionUsage.session_id, bucket)
        .order_by(bucket)
    ).all()
    found: dict[tuple[UUID, str], TaskSession] = {}
    for row in rows:
        entry = found.setdefault((row.node_id, row.session_id), TaskSession(row.node_id, row.session_id))
        entry.points.append(SessionPoint(at=row.at, cpu_pct=float(row.cpu_pct), rss_kb=int(row.rss_kb)))
    peaks = session.execute(
        select(TaskSessionUsagePeak).where(
            TaskSessionUsagePeak.task_id == task_id, TaskSessionUsagePeak.org_id == task_org_id
        ).order_by(TaskSessionUsagePeak.first_seen_at)
    ).scalars()
    for peak in peaks:
        entry = found.setdefault((peak.node_id, peak.session_id), TaskSession(peak.node_id, peak.session_id))
        entry.peak_rss_kb = peak.peak_rss_kb
        entry.peak_cpu_pct = peak.peak_cpu_pct
        entry.cpu_seconds = peak.cpu_seconds
    return list(found.values())


def task_peaks(session: Session, task_id: int) -> dict[str, Optional[float]]:
    """The run-metrics figures over this task's own sessions: the largest `peak_rss_kb` and `peak_cpu_pct`,
    and the summed `cpu_seconds`. All None when no session of the task's org reported."""
    row = session.execute(
        select(
            func.max(TaskSessionUsagePeak.peak_rss_kb).label("peak_rss_kb"),
            func.max(TaskSessionUsagePeak.peak_cpu_pct).label("peak_cpu_pct"),
            func.sum(TaskSessionUsagePeak.cpu_seconds).label("cpu_seconds"),
        )
        .join(Task, and_(Task.id == TaskSessionUsagePeak.task_id, Task.org_id == TaskSessionUsagePeak.org_id))
        .where(TaskSessionUsagePeak.task_id == task_id)
    ).one()
    return {
        "peak_rss_kb": None if row.peak_rss_kb is None else int(row.peak_rss_kb),
        "peak_cpu_pct": row.peak_cpu_pct,
        "cpu_seconds": None if row.cpu_seconds is None else float(row.cpu_seconds),
    }
