"""Hourly ops history: `ops_snapshots`, the point-in-time series the operator dashboard trends over a year.

Their sources keep no usable history: `node_host_metrics` is pruned at 7 days (`app/node_host_metrics.py`), and
worktree occupancy, the account quota fold and `token_wallets.floor_state` are current-state only. The
`ops-snapshot` system cron (`app/cron_system_activities.py`) calls `take_snapshots` once an hour; the retention
sweep (`app/cron_retention.py::sweep_ops_snapshots`) prunes at the operational bound (365 days). Read-only on
every source table.

One row per (kind, subject, hour). `taken_at` is the hour the row describes, in UTC: the pass's own hour for the
point-in-time kinds, the last COMPLETE hour for `node_util` (a mean needs a closed window, so it does not move with
the minute the pass ran — Run now included). The unique index
`uq_ops_snapshots_subject_hour` (NULLS NOT DISTINCT) makes a second pass in the same hour a no-op, so the
cron's Run now and a replica race are both safe. The `values` shapes, per kind:

  node_util           {cpu_pct, mem_pct}          node_id: mean of the node's minute buckets in the hour
                                                  [taken_at, taken_at + 1h)
  worktree_occupancy  {free, task, user}          node_id + project_id: slot count per state
  account_quota       {five_hour_pct, seven_day_pct}  account_id: the fleet-wide fold's fresh reading
                                                  (`app/account_fold.py::quota_readings`; 7d = `weekly_percent`)
  wallet_floor        {floor_state}               the org: `token_wallets.floor_state` (`ok`/`low`/`floor`)

`org_id` is always the subject's own org (an operator-owned account or node is org zero), so the dashboard can
slice every kind by org. A subject with nothing to say this hour (no metric minute, no fresh quota reading, no
wallet) writes no row: absence is "not measured", never zero.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

log = logging.getLogger("cockpit-cron")

OPS_SNAPSHOTS_TABLE = "ops_snapshots"

KIND_NODE_UTIL = "node_util"
KIND_WORKTREE_OCCUPANCY = "worktree_occupancy"
KIND_ACCOUNT_QUOTA = "account_quota"
KIND_WALLET_FLOOR = "wallet_floor"
#: The `ck_ops_snapshots_kind` vocabulary, in the order a pass writes them.
OPS_SNAPSHOT_KINDS: tuple[str, ...] = (KIND_NODE_UTIL, KIND_WORKTREE_OCCUPANCY, KIND_ACCOUNT_QUOTA, KIND_WALLET_FLOOR)

#: One snapshot hour; the `node_util` window is the complete hour before the pass's own.
HOUR = timedelta(hours=1)


@dataclass
class SnapshotReport:
    """One pass: the hour it stamped and the rows each kind wrote (0 = already taken this hour, or nothing)."""

    taken_at: datetime
    written: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.written.values())

    def summary(self) -> str:
        counts = ", ".join(f"{kind} {self.written.get(kind, 0)}" for kind in OPS_SNAPSHOT_KINDS)
        return f"{self.total} snapshot row(s) for {self.taken_at.isoformat()}: {counts}"


def _node_util(session: Session, hour: datetime) -> list[dict[str, Any]]:
    """`hour` is the window's start. The org is the node's own, so a node that moved org mid-hour is one row."""
    from app.models.nodes import Node, NodeHostMetric

    rows = session.execute(
        select(Node.id, Node.org_id, func.avg(NodeHostMetric.cpu_pct), func.avg(NodeHostMetric.mem_pct))
        .join(Node, Node.id == NodeHostMetric.node_id)
        .where(NodeHostMetric.bucket_at >= hour, NodeHostMetric.bucket_at < hour + HOUR)
        .group_by(Node.id, Node.org_id)
    ).all()
    return [
        {
            "kind": KIND_NODE_UTIL, "node_id": node_id, "org_id": org_id, "taken_at": hour,
            "values": {"cpu_pct": round(float(cpu), 2), "mem_pct": round(float(mem), 2)},
        }
        for node_id, org_id, cpu, mem in rows
    ]


def _worktree_occupancy(session: Session) -> list[dict[str, Any]]:
    from app.models.worktrees import Worktree
    from app.slot_state import STATES  # every state, zero included; `FREE` first

    counts: dict[tuple[Any, int, Any], dict[str, int]] = {}
    for node_id, project_id, org_id, state, n in session.execute(
        select(Worktree.node_id, Worktree.project_id, Worktree.org_id, Worktree.state, func.count())
        .group_by(Worktree.node_id, Worktree.project_id, Worktree.org_id, Worktree.state)
    ).all():
        slot = counts.setdefault((node_id, project_id, org_id), dict.fromkeys(STATES, 0))
        slot[state] = int(n)
    return [
        {"kind": KIND_WORKTREE_OCCUPANCY, "node_id": node_id, "project_id": project_id, "org_id": org_id,
         "values": values}
        for (node_id, project_id, org_id), values in counts.items()
    ]


def _account_quota(session: Session, now: datetime) -> list[dict[str, Any]]:
    from app import account_fold
    from app.models.credentials import Account

    readings = account_fold.quota_readings(session)
    if not readings:
        return []
    accounts = session.execute(
        select(Account.slug, Account.id, Account.org_id).where(Account.slug.in_(list(readings)))
    ).all()
    rows = []
    for slug, account_id, org_id in accounts:
        reading = readings[slug]
        five, weekly = reading.get("five_hour_percent"), reading.get("weekly_percent")
        # A stale reading would repeat last hour's numbers as this hour's; absent is the honest answer.
        if (five is None and weekly is None) or account_fold.quota_is_stale(reading.get("quota_updated_at"), now=now):
            continue
        rows.append({
            "kind": KIND_ACCOUNT_QUOTA, "account_id": account_id, "org_id": org_id,
            "values": {"five_hour_pct": five, "seven_day_pct": weekly},
        })
    return rows


def _wallet_floor(session: Session) -> list[dict[str, Any]]:
    from app.models.metering import TokenWallet

    return [
        {"kind": KIND_WALLET_FLOOR, "org_id": org_id, "values": {"floor_state": floor_state}}
        for org_id, floor_state in session.execute(select(TokenWallet.org_id, TokenWallet.floor_state)).all()
    ]


def take_snapshots(session: Session) -> SnapshotReport:
    """Write this hour's snapshot of every kind into `session`'s transaction; the cron wrapper commits it, so a
    firing lands or does not. Fleet-wide: the caller holds `seam.operator_scope()` (every system cron does).
    Idempotent within the hour; never updates or deletes a source row."""
    from app.models.operator_dashboard import OpsSnapshot

    # The 3-argument `date_trunc` truncates in UTC whatever the session's TimeZone, so an hour is an hour.
    now, taken_at = session.execute(select(func.now(), func.date_trunc("hour", func.now(), "UTC"))).one()
    report = SnapshotReport(taken_at=taken_at)
    gathered = {
        KIND_NODE_UTIL: _node_util(session, taken_at - HOUR),
        KIND_WORKTREE_OCCUPANCY: _worktree_occupancy(session),
        KIND_ACCOUNT_QUOTA: _account_quota(session, now),
        KIND_WALLET_FLOOR: _wallet_floor(session),
    }
    for kind, rows in gathered.items():
        if not rows:
            report.written[kind] = 0
            continue
        for row in rows:
            row.setdefault("taken_at", taken_at)
            for column in ("node_id", "account_id", "project_id"):
                row.setdefault(column, None)
        # Counted off RETURNING: a conflicting (already taken) row returns nothing, and `rowcount` on a
        # multi-row VALUES insert is not reliable through the driver.
        inserted = session.execute(
            pg_insert(OpsSnapshot).values(rows).on_conflict_do_nothing().returning(OpsSnapshot.id)
        ).all()
        report.written[kind] = len(inserted)
    log.info("ops snapshot: %s", report.summary())
    return report
