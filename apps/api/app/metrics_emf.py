"""The control plane's own CloudWatch metrics, written as Embedded Metric Format lines (SOC 2 G-15, PRD 5.2).

Two signals no AWS service measures for us, emitted once a minute by the `metrics-emit` system cron
(revision 0321):

  * CRON STARVATION — per enabled system cron, the seconds since its last successful firing
    (`crons.last_succeeded_at`), and `CronsStarved`: how many have let two scheduled firings, plus their own
    timeout and `STARVATION_GRACE_SECONDS`, pass with no success. A scheduler that has stopped altogether
    emits nothing, which the alarm reads as breaching.
  * NODE DISK — per node (by uuid) whose latest reading is fresh, free disk as a percentage of its total
    (`app/node_host_metrics.py::latest_map`), and `NodeDiskFreePercentMin`, the fleet's lowest.

Two more are written by every control-plane PROCESS rather than the cron, because the pool and the loop are per
process:

  * DB POOL — the process's one connection pool (`app/db_async.py`), dimension `Lane=async`: each minute's peak
    checked-out connections, peak overflow past `pool_size`, checkout timeouts, sessions opened on the offline
    engine while the loop served (`app/db.py::offline_while_serving`), and the median cost of
    `release_route_connection`'s xid probe and COMMIT (`pool_line`, called by `app/pool_watch.py`'s sampler).
    Every task writes its own line; an alarm reads the `Maximum`/`Sum`.
  * SERVING LOOP — dimensionless (`loop_line`, written by `app/loop_watch.py`): each minute's mean and peak
    event-loop lag (how late a timer on the serving loop fires: every request waits that long for its turn), and
    the mean and peak wait of a request for its DB admission slot (`app/db_async.py::RequestAdmission`), with how
    many requests were admitted; and the interval's garbage-collector pauses (`app/gc_watch.py`: collections,
    peak and total pause), so a stall minute and its collector pause sit on one datapoint.

EMF, NOT THE SDK: each line is one JSON object on stdout carrying an `_aws` directive. The awslogs driver
ships it into the control plane's log group and CloudWatch extracts the metrics itself — no agent, no IAM
grant, no API call that can fail a firing. A line must be the bare object, so it goes through the
`app.metrics` logger, which `app/main.py::_AppFormatter` writes without a prefix.

THE NAMES BELOW ARE A CONTRACT with the alarms in the monitoring Terraform file: a renamed metric
there would be an alarm that never fires. `tests/test_metrics_emf.py` reads that file and fails on drift.

OFF UNTIL CONFIGURED: no `METRICS_NAMESPACE` is a `noop` naming it (a local stack). Terraform sets it in every
environment; the alarms that page exist inside the SOC 2 boundary only (main).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Optional

from sqlalchemy import select, text

from app.clock import to_epoch_ms
from sqlalchemy.orm import Session

from app.config import get_settings

# The EMF stream: every record is one bare JSON line (`app/main.py::_AppFormatter`). Its own level, so a
# quieter `LOG_LEVEL` cannot silence the alarms' only source.
emf_log = logging.getLogger("app.metrics")
emf_log.setLevel(logging.INFO)
log = logging.getLogger("app.metrics_emf")

# ── The cron's own bounds (revision 0321 seeds the same literals; a test compares them) ───────────────
INTERVAL_SECONDS = 60
TIMEOUT_SECONDS = 30
MIN_GAP_SECONDS = 10

# ── The contract with the deployment's alarms ─────────────────────────────────────────────────────────────
CRON_DIMENSION = "Cron"
NODE_DIMENSION = "Node"
CRON_LAST_SUCCESS_AGE = "CronLastSuccessAgeSeconds"
CRONS_STARVED = "CronsStarved"
NODE_DISK_FREE_PERCENT = "NodeDiskFreePercent"
NODE_DISK_FREE_PERCENT_MIN = "NodeDiskFreePercentMin"
LANE_DIMENSION = "Lane"
DB_POOL_CHECKED_OUT_MAX = "DbPoolCheckedOutMax"
DB_POOL_OVERFLOW_MAX = "DbPoolOverflowMax"
DB_POOL_CHECKOUT_TIMEOUTS = "DbPoolCheckoutTimeouts"
DB_RELEASE_ROUND_TRIPS_P50 = "DbReleaseRoundTripsMsP50"
DB_OFFLINE_WHILE_SERVING = "DbOfflineWhileServing"
#: The pool's `Lane` dimension value (`app/pool_watch.py::ASYNC` is this one).
POOL_LANE = "async"
#: The serving loop's own figures (`app/loop_watch.py`), dimensionless so the alarm and the opt-in scale-out policy
#: (the ECS module's autoscaling terraform) read the tasks' Average.
EVENT_LOOP_LAG_MEAN = "EventLoopLagMsMean"
EVENT_LOOP_LAG_MAX = "EventLoopLagMsMax"
REQUEST_ADMISSION_WAIT_MEAN = "RequestAdmissionWaitMsMean"
REQUEST_ADMISSION_WAIT_MAX = "RequestAdmissionWaitMsMax"
REQUEST_ADMISSIONS = "RequestAdmissions"
#: The collector's pauses in the same interval (`app/gc_watch.py`), on the same line as the lag they may explain.
GC_COLLECTIONS = "GcCollections"
GC_PAUSE_MAX = "GcPauseMsMax"
GC_PAUSE_TOTAL = "GcPauseMsTotal"

# Slack beyond a cron's own timeout before two missed firings count as starvation: a pass may legitimately
# run up to its bound, and a deploy's task swap pauses every cron for about a minute.
STARVATION_GRACE_SECONDS = 300.0


@dataclass(frozen=True)
class CronAge:
    """One system cron's distance from its last success, and whether that distance is starvation."""

    activity_type: str
    age_seconds: float
    starved: bool


@dataclass(frozen=True)
class NodeDisk:
    # The node's uuid, never its name: a CloudWatch dimension value cannot be deleted for 15 months, and a
    # node name is tenant prose that must leave with its org.
    node_id: str
    free_percent: float


@dataclass(frozen=True)
class Report:
    crons: list[CronAge]
    nodes: list[NodeDisk]

    @property
    def starved(self) -> list[str]:
        return [c.activity_type for c in self.crons if c.starved]

    def summary(self) -> str:
        lowest = min((n.free_percent for n in self.nodes), default=None)
        disk = f"lowest node disk free {lowest:.1f}%" if lowest is not None else "no fresh node disk reading"
        starved = ", ".join(self.starved) or "none"
        return f"{len(self.crons)} system cron(s), starved: {starved}; {disk} across {len(self.nodes)} node(s)"


def line(namespace: str, timestamp_ms: int, dimensions: dict[str, str], metrics: dict[str, tuple[float, str]]) -> str:
    """One EMF event: `metrics` is `{name: (value, unit)}`, all under the one dimension set `dimensions`.

    An empty `dimensions` is the dimensionless set `[[]]` — legal EMF, and how the fleet-wide aggregates are
    published. Dimension values sit at the root beside the metric values, as the specification requires."""
    directive = {
        "Timestamp": timestamp_ms,
        "CloudWatchMetrics": [{
            "Namespace": namespace,
            "Dimensions": [sorted(dimensions)],
            "Metrics": [{"Name": name, "Unit": unit} for name, (_, unit) in metrics.items()],
        }],
    }
    body: dict[str, object] = {"_aws": directive, **dimensions}
    body.update({name: value for name, (value, _) in metrics.items()})
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def is_starved(
    *,
    schedule_kind: str,
    cron_expression: Optional[str],
    interval_seconds: Optional[int],
    last_success: datetime,
    timeout_seconds: float,
    now: datetime,
    tz: tzinfo,
) -> bool:
    """True once the SECOND scheduled firing after `last_success` is more than its timeout plus the grace in
    the past. Exact for any schedule: one missed firing is a slow pass, two are a scheduler not running it."""
    from app.cron_schedule import next_fire_for_row

    first = next_fire_for_row(schedule_kind, cron_expression, interval_seconds, last_success, tz)
    second = next_fire_for_row(schedule_kind, cron_expression, interval_seconds, first, tz)
    return (now - second).total_seconds() > timeout_seconds + STARVATION_GRACE_SECONDS


def cron_ages(session: Session, now: datetime, tz: tzinfo) -> list[CronAge]:
    """Every ENABLED SYSTEM cron. A user cron failing on its own terms is its owner's alarm, not a page.

    The clock starts at the later of the last success (else the row's creation) and `schedule_anchor_at`,
    which a resume or a schedule-zone change re-stamps: a cron re-enabled after days off is not starved for
    the hours until its next firing."""
    from app.cron_runtime import DEFAULT_TIMEOUT_SECONDS
    from app.cron_schedule import CronScheduleError
    from app.models.automations import Cron

    rows = session.execute(
        select(Cron).where(Cron.is_system.is_(True), Cron.enabled.is_(True)).order_by(Cron.activity_type)
    ).scalars()
    ages: list[CronAge] = []
    for cron in rows:
        succeeded = cron.last_succeeded_at or cron.created_at
        baseline = max(succeeded, cron.schedule_anchor_at or succeeded)
        timeout = float(cron.timeout_seconds) if cron.timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS
        try:
            starved = is_starved(
                schedule_kind=cron.schedule_kind, cron_expression=cron.cron_expression,
                interval_seconds=cron.interval_seconds, last_success=baseline, timeout_seconds=timeout,
                now=now, tz=tz,
            )
        except CronScheduleError:
            # The runtime refuses the same row on every tick, so it never fires either: that IS starvation.
            log.warning("cron %s: schedule unreadable; counted as starved", cron.activity_type)
            starved = True
        ages.append(CronAge(cron.activity_type, max((now - succeeded).total_seconds(), 0.0), starved))
    return ages


def node_disks(session: Session) -> list[NodeDisk]:
    """Free disk per ENABLED node whose latest reading is fresh and carries both disk figures. A stale or silent
    node is the liveness alarm's (`app/node_liveness.py`), never a disk reading of 0."""
    from app import node_host_metrics
    from app.models.nodes import Node

    ids = session.execute(select(Node.id).where(Node.enabled.is_(True))).scalars().all()
    disks: list[NodeDisk] = []
    for node_id, reading in node_host_metrics.latest_map(session, ids).items():
        if reading.stale or not reading.disk_total_kb or reading.disk_free_kb is None:
            continue
        disks.append(NodeDisk(str(node_id), 100.0 * reading.disk_free_kb / reading.disk_total_kb))
    return sorted(disks, key=lambda d: d.node_id)


def lines(namespace: str, timestamp_ms: int, report: Report) -> list[str]:
    """The EMF events for one report: a line per cron, a line per node, then the two fleet aggregates.

    `CronsStarved` is always published (0 is the healthy value the alarm needs to see); the disk minimum only
    when some node reported, since a minimum over nothing is not 100%."""
    out = [
        line(namespace, timestamp_ms, {CRON_DIMENSION: c.activity_type},
             {CRON_LAST_SUCCESS_AGE: (round(c.age_seconds, 1), "Seconds")})
        for c in report.crons
    ]
    out += [
        line(namespace, timestamp_ms, {NODE_DIMENSION: n.node_id},
             {NODE_DISK_FREE_PERCENT: (round(n.free_percent, 2), "Percent")})
        for n in report.nodes
    ]
    out.append(line(namespace, timestamp_ms, {}, {CRONS_STARVED: (float(len(report.starved)), "Count")}))
    if report.nodes:
        lowest = min(n.free_percent for n in report.nodes)
        out.append(line(namespace, timestamp_ms, {}, {NODE_DISK_FREE_PERCENT_MIN: (round(lowest, 2), "Percent")}))
    return out


def pool_line(namespace: str, timestamp_ms: int, checked_out: int, overflow: int, timeouts: int, *,
              release_p50_ms: Optional[float] = None, offline_while_serving: Optional[int] = None) -> str:
    """The pool's EMF event for one interval, dimension `Lane=async`: peak checked out, peak overflow and checkout
    timeouts, zeros included (an alarm needs to see the healthy value). `release_p50_ms`, the interval's median cost
    of `release_route_connection`'s round trips (`app/db.py::release_costs`), rides along when the interval released
    anything; `offline_while_serving`, the interval's sessions on the offline engine, when given."""
    metrics = {
        DB_POOL_CHECKED_OUT_MAX: (float(checked_out), "Count"),
        DB_POOL_OVERFLOW_MAX: (float(overflow), "Count"),
        DB_POOL_CHECKOUT_TIMEOUTS: (float(timeouts), "Count"),
    }
    if release_p50_ms is not None:
        metrics[DB_RELEASE_ROUND_TRIPS_P50] = (round(release_p50_ms, 3), "Milliseconds")
    if offline_while_serving is not None:
        metrics[DB_OFFLINE_WHILE_SERVING] = (float(offline_while_serving), "Count")
    return line(namespace, timestamp_ms, {LANE_DIMENSION: POOL_LANE}, metrics)


def loop_line(namespace: str, timestamp_ms: int, *, lag_mean_ms: float, lag_max_ms: float,
              admission_mean_ms: float, admission_max_ms: float, admissions: int, gc_collections: int, gc_pause_max_ms: float,
              gc_pause_total_ms: float) -> str:
    """The serving loop's EMF event for one interval, dimensionless; zeros included (an alarm reads the healthy
    value, and an interval that admitted nothing waited 0). The collector's pauses ride on the same line so a lag
    peak and the pause behind it correlate on one datapoint."""
    return line(namespace, timestamp_ms, {}, {
        EVENT_LOOP_LAG_MEAN: (round(lag_mean_ms, 3), "Milliseconds"),
        EVENT_LOOP_LAG_MAX: (round(lag_max_ms, 3), "Milliseconds"),
        REQUEST_ADMISSION_WAIT_MEAN: (round(admission_mean_ms, 3), "Milliseconds"),
        REQUEST_ADMISSION_WAIT_MAX: (round(admission_max_ms, 3), "Milliseconds"),
        REQUEST_ADMISSIONS: (float(admissions), "Count"),
        GC_COLLECTIONS: (float(gc_collections), "Count"),
        GC_PAUSE_MAX: (round(gc_pause_max_ms, 3), "Milliseconds"),
        GC_PAUSE_TOTAL: (round(gc_pause_total_ms, 3), "Milliseconds"),
    })


def run(session: Session) -> Optional[Report]:
    """Measure, then write every line. None when `METRICS_NAMESPACE` is unset (the caller's `noop`)."""
    namespace = get_settings().metrics_namespace
    if not namespace:
        return None
    from app.cron_runtime import cron_timezone

    # The database's clock: `last_succeeded_at` is stamped by its `now()`.
    now = session.execute(text("SELECT now()")).scalar_one()
    report = Report(crons=cron_ages(session, now, cron_timezone()), nodes=node_disks(session))
    session.rollback()
    for event in lines(namespace, to_epoch_ms(now), report):
        emf_log.info(event)
    return report
