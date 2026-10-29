"""One node's host metrics, disk-gate events and live sessions: `GET /api/nodes/{node_id}/metrics`, `/disk-events`
and `/sessions-usage`."""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import node_disk_events, node_host_metrics, node_session_usage, node_tmp_inode_alarm
from app.auth import require_user
from app.authz.object_access import can_access
from app.authz.catalog import Tenancy
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import ObjectAccess
from app.db import get_session
from app.entity_shape import RefOut, from_row, iso_or_none
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.worktrees import VirtualNode
from app.routes.nodes.node_wire import (
    NodeHostMetricsOut,
    _disk_usage_of,
    _host_metrics_out,
    _hosting_facts,
    _hosting_of,
    _on_a_foreign_host,
    _own_disk_only,
)
from app.routes.nodes.reads import _readable_node_or_404

router = APIRouter(tags=["nodes"])


# ── The metrics read: one node's host series, for a chart (`GET /api/nodes/{node}/metrics`) ──────
# A separate route: `NodeOut.host_metrics` carries only the newest reading; a series has its own cost and
# parameter (`hours`), and folding it into the detail payload would make every node page pay for it.


class NodeMetricsPointOut(BaseModel):
    """One plotted point: a bucket stamp and the mean CPU/RAM/disk-used/network over it. `at` is the
    bucket's start (`node_host_metrics.SeriesPoint`), so points are evenly spaced and charts line up. The
    last three are null for a bucket that never carried them."""

    at: str
    # Null, with every figure below, for a tenant reading its node on another org's host (`_own_disk_only`).
    cpu_pct: Optional[float]
    mem_pct: Optional[float]
    disk_pct: Optional[float] = None
    net_rx_bps: Optional[float] = None
    net_tx_bps: Optional[float] = None
    # Sessions running on this node at any moment of the bucket (`node_host_metrics.runner_counts`). The node's
    # own, so kept on another org's host; null only from an older server.
    runners: Optional[int] = None
    # Used share of /tmp's inode table (`node_host_metrics.tmp_inode_used_pct`), so a leak's rate shows.
    tmp_inode_pct: Optional[float] = None
    # Bucket means of the pressure signals (`node_host_metrics.SeriesPoint`); null where unreported, and on
    # another org's host.
    load_1: Optional[float] = None
    swap_pct: Optional[float] = None
    psi_cpu_pct: Optional[float] = None
    psi_mem_pct: Optional[float] = None
    psi_io_pct: Optional[float] = None


class NodeMetricsOut(BaseModel):
    """The Metrics tab's whole payload: the series, the newest reading, and the numbers to draw it against.

    `warn_pct`/`critical_pct` ride the payload so guide lines use the server's thresholds
    (`app/node_host_metrics.py`), agreeing with the `level` badge. `bucket_secs` is served because the
    re-bucketed width (≤ `node_host_metrics.MAX_POINTS`) cannot be inferred from point gaps.

    `latest` is `null` and `points` `[]` for a node that has never reported.

    `window_start`/`window_end` are the x axis, from the DB clock (`node_host_metrics.window_bounds`), so
    the client positions points by `(at - window_start) / (window_end - window_start)` without its own
    clock; laying points out by index misdrew reporting gaps. They are real bounds even with no points."""

    node_id: str
    # Echoed back, so a response that raced a range change is identifiable.
    hours: int
    bucket_secs: float
    warn_pct: float
    critical_pct: float
    # The /tmp inode chart's two lines: the Health finding's (`node_host_metrics.TMP_INODE_WARN_PCT`) and the
    # alarm's (`node_tmp_inode_alarm.ALARM_PCT`).
    tmp_inode_warn_pct: float = node_host_metrics.TMP_INODE_WARN_PCT
    tmp_inode_alarm_pct: float = node_tmp_inode_alarm.ALARM_PCT
    # The domain, iso, oldest first; `window_end` is the series' own `now()` (`window_bounds`). The
    # re-bucketing floors, so the oldest point may precede `window_start` by up to `bucket_secs - 60`; the
    # client clamps it.
    window_start: str
    window_end: str
    points: list[NodeMetricsPointOut]
    latest: Optional[NodeHostMetricsOut] = None


class NodeDiskEventSourceOut(RefOut):
    """The node the event happened on: the node itself, or the host a co-located node shares its disk with."""


class NodeDiskEventOut(BaseModel):
    """One row of the node's disk history (`app/node_disk_events.py`). `freed_bytes` carries only the
    categories the reclaim reported, `total_freed_bytes` their sum; `detail` only the keys it reported
    (`used_pct_before`, `engines`, `removed_users`, `remaining_bytes`). Gate rows have neither."""

    id: int
    at: str
    kind: Literal["reclaim", "pause", "resume"]
    trigger: Optional[Literal["scheduled", "warn", "converge"]] = None
    used_pct: Optional[float] = None
    freed_bytes: dict[str, int] = {}
    total_freed_bytes: int = 0
    source_node: NodeDiskEventSourceOut
    detail: dict[str, Any] = {}


class NodeDiskEventsOut(BaseModel):
    events: list[NodeDiskEventOut]


def _disk_event_out(event: Any, names: dict[UUID, str], *, show_pct: bool = True) -> NodeDiskEventOut:
    freed = {key: int(value) for key, value in (event.freed or {}).items()}
    detail = {key: value for key, value in (event.detail or {}).items() if key in node_disk_events.DETAIL_KEYS}
    return NodeDiskEventOut(
        id=event.id, at=iso_or_none(event.occurred_at), kind=event.kind, trigger=event.trigger,
        used_pct=event.used_pct if show_pct else None, freed_bytes=freed, total_freed_bytes=sum(freed.values()),
        source_node=NodeDiskEventSourceOut(id=str(event.node_id), name=names.get(event.node_id, "")),
        detail=detail if show_pct else {},
    )


@router.get("/api/nodes/{node_id}/disk-events", response_model=NodeDiskEventsOut)
@feature("nodes", "view", tenancy=Tenancy.CALLER_PLANE)
def get_node_disk_events(
    node_id: str,
    limit: int = Query(50, ge=1, le=200),
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> NodeDiskEventsOut:
    """This node's disk history, newest first: each reclaim it reported and each pause/resume of its disk
    gate (`app/node_disk_events.py`).

    Node-grant scoped through `_readable_node_or_404`, as the Metrics tab is. A co-located node shares its
    host's filesystem, so its host's reclaims are listed too (`source_node` names the host) — except for a
    tenant reading its node on another org's host, who is never told that host (`_on_a_foreign_host`) nor its
    usage: such a reader gets the node's own rows with every `used_pct` and `detail` blanked."""
    _presence, row = _readable_node_or_404(session, principal, node_id)
    if row is None:
        return NodeDiskEventsOut(events=[])
    _kind, hosted_on, host_org_id = _hosting_of(row, _hosting_facts(session, [row.id]).get(row.id))
    foreign = hosted_on is not None and _on_a_foreign_host(user, "co_located", host_org_id)
    host_uuid = None
    if hosted_on is not None and not foreign:
        host_uuid = session.scalar(select(VirtualNode.host_node_id).where(VirtualNode.node_id == row.id))
    events = node_disk_events.events_for(session, row.id, host_uuid=host_uuid, limit=limit)
    names = {row.id: row.name}
    if host_uuid is not None:
        names[host_uuid] = hosted_on
    # On another org's host the node's own gate figures are the host's disk (see `_narrow_for_tenant`).
    return NodeDiskEventsOut(events=[_disk_event_out(event, names, show_pct=not foreign) for event in events])


@router.get("/api/nodes/{node_id}/metrics", response_model=NodeMetricsOut)
@feature("nodes", "view", tenancy=Tenancy.CALLER_PLANE)
def get_node_metrics(
    node_id: str,
    # 6h default; the ceiling is `RETENTION_DAYS * 24`, derived so it moves with the horizon. `ge=1`: a
    # non-positive range is malformed, and a 422 costs only a chart.
    hours: int = Query(6, ge=1, le=int(node_host_metrics.RETENTION_DAYS * 24)),
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> NodeMetricsOut:
    """This node's CPU/RAM/disk/network/runners series over the last `hours`, plus its newest judged reading.

    Node-grant scoped through `_readable_node_or_404` (D13), with the same refusals as the detail read.

    Read-only, as `get_node_health` is: four SELECTs and pure classification; the ingest path is the only
    writer of this table.

    Every rule is `app/node_host_metrics.py`'s, including `bucket_secs_for`, called so the payload's width
    is the width `series` grouped by.

    A presence-only host answers 200 with an empty series (no uuid, so no metrics), as a registered node
    that never reported does.

    A tenant reading its node on another org's host is narrowed as the roster narrows it
    (`_on_a_foreign_host`): `latest` keeps only the node's own disk, and every point's figures are null."""
    _presence, row = _readable_node_or_404(session, principal, node_id)
    # Computed once for both `series` and the payload, so axis and rows agree.
    range_secs = float(hours) * 60.0 * 60.0
    # The axis, unconditionally, even for a presence-only host: the window belongs to the request, so an
    # empty series still spans the right range.
    window_start, window_end = node_host_metrics.window_bounds(session, hours=float(hours))
    points = node_host_metrics.series(session, row.id, hours=float(hours)) if row is not None else []
    latest = _host_metrics_out(
        node_host_metrics.latest_map(session, [row.id], with_processes=True).get(row.id) if row is not None else None
    )
    hosting, _hosted_on, host_org_id = _hosting_of(
        row, _hosting_facts(session, [row.id]).get(row.id) if row is not None else None
    )
    own_disk_only = row is not None and _on_a_foreign_host(user, hosting, host_org_id)
    if own_disk_only and latest is not None:
        latest = _own_disk_only(latest, _disk_usage_of(row.id))
    bucket_secs = node_host_metrics.bucket_secs_for(range_secs)
    runners = (
        node_host_metrics.runner_counts(session, row.id, [p.at for p in points], bucket_secs) if points else []
    )
    return NodeMetricsOut(
        node_id=row.name if row is not None else node_id,
        hours=hours,
        bucket_secs=bucket_secs,
        warn_pct=node_host_metrics.WARN_PCT,
        critical_pct=node_host_metrics.CRITICAL_PCT,
        # `or ""` only for `iso_or_none`'s Optional signature; `window_bounds` never yields None.
        window_start=iso_or_none(window_start) or "",
        window_end=iso_or_none(window_end) or "",
        points=[
            NodeMetricsPointOut(at=point.at.isoformat(), cpu_pct=None, mem_pct=None, runners=count)
            if own_disk_only
            else from_row(NodeMetricsPointOut, point, at=point.at.isoformat(), runners=count)
            for point, count in zip(points, runners)
        ],
        latest=latest,
    )


# ── The live sessions on a node and its co-located nodes (`GET /api/nodes/{node}/sessions-usage`) ──────


class SessionUsageNodeOut(RefOut):
    """The node a session runs on: this node, or one co-located on it."""


class SessionUsageTaskOut(BaseModel):
    """The task a session runs, named only when the task is of the row's own org (`app/node_session_usage.py`)."""

    id: int
    title: str


class SessionUsageOut(BaseModel):
    """One live session's newest minute: CPU in percent of one core, RSS, process count of its tree."""

    node: SessionUsageNodeOut
    # Null when the task is gone or not the row's org's.
    task: Optional[SessionUsageTaskOut] = None
    session_id: str
    pid: Optional[int] = None
    cpu_pct: float
    rss_kb: int
    procs: int
    sampled_at: str
    age_seconds: float
    stale: bool


class SessionsUsageOut(BaseModel):
    # Largest RSS first; only sessions whose newest minute is live (`node_session_usage.LIVE_BOUND_SECONDS`).
    sessions: list[SessionUsageOut]


@router.get("/api/nodes/{node_id}/sessions-usage", response_model=SessionsUsageOut)
@feature("nodes", "view", tenancy=Tenancy.CALLER_PLANE)
def get_node_sessions_usage(
    node_id: str,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> SessionsUsageOut:
    """The live runner sessions on this node AND every node co-located on it that the caller may read, each its
    newest fresh minute (`app/node_session_usage.py`), largest RSS first.

    The door is `_readable_node_or_404`, as the Metrics tab's. A session is its node's own, so a tenant reading
    its node on another org's host keeps it (no `_own_disk_only` narrowing); a co-located node the caller
    cannot read is left out. A presence-only host has no uuid, so no sessions."""
    _presence, row = _readable_node_or_404(session, principal, node_id)
    if row is None:
        return SessionsUsageOut(sessions=[])
    hosted = session.scalars(
        select(VirtualNode.node_id).where(VirtualNode.host_node_id == row.id, VirtualNode.node_id.is_not(None))
    ).all()
    readable = [row.id] + [
        uuid for uuid in hosted if can_access(session, principal, "node", uuid, mode=ObjectAccess.READ)
    ]
    names = {row.id: row.name}
    if len(readable) > 1:
        names.update(session.execute(select(Node.id, Node.name).where(Node.id.in_(readable[1:]))).tuples().all())
    return SessionsUsageOut(sessions=[
        SessionUsageOut(
            node=SessionUsageNodeOut(id=str(live.node_id), name=names.get(live.node_id, "")),
            task=None if live.task_title is None else SessionUsageTaskOut(id=live.task_id, title=live.task_title),
            session_id=live.session_id, pid=live.pid, cpu_pct=live.cpu_pct, rss_kb=live.rss_kb, procs=live.procs,
            sampled_at=live.bucket_at.isoformat(), age_seconds=live.age_seconds, stale=live.stale,
        )
        for live in node_session_usage.live_sessions(session, readable)
    ])
