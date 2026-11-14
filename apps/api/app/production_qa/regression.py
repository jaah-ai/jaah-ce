"""Post-deploy regression windows (architecture §8): metric pulls, `deployment_metrics`, breach and resolve.

THE WINDOW
* `schedule`: an anchor (shipped to production from a push) that has finished is due `regression_window_min` after
  its finish, `regression_status = pending`. Measurement, so it ignores the Production QA gate.
* The `regression_due` cron job, per due anchor × each of its project's monitor triggers with `metrics` (not paused,
  not failed): post window `[finished_at, finished_at + W]`, baseline `[started_at − W, started_at]` (the same length
  before the deploy started, A4). A later anchor of the same project and environment that finished inside the post
  window cuts it there; a cut window under `MIN_WINDOW` is `not_measured: superseded by deploy #N`.
* Each monitor's pull (`metric_pulls.pull`) reads both windows. Error rate = errors / requests; p95 latency where the
  monitor gives one. Every measured metric is stored in `deployment_metrics` (breached or not, with its delta); a
  metric with no value is stored with its `note`.

BREACH AND RESOLVE (the project's thresholds, defaults A3)
* error rate ≥ `regression_error_ratio` × baseline with ≥ `regression_min_events` post-window errors, or p95 ≥
  `regression_p95_ratio` × baseline → `incidents.record(type='regression', fingerprint=regression:{env}:{metric},
  severity='error', deployment_id, facts={metric, baseline, post, delta_abs, delta_pct, window, …})`. Its raise goes
  through `events` like any incident's: filed only by an `incident` automation with the gate on (AC-26).
* A metric measured with no breach on any monitor resolves that metric's open regression incident.

NOT MEASURED — nothing recorded as an incident, the deploy row says why (`regression_note`):
* no monitor with `metrics` on the project, or a monitor's credential missing / unusable (`ConfigError`): at once;
* a failed pull (`PullError`): retried `RETRIES` times `RETRY_AFTER` apart (the note reads `retry N of 2: <reason>`
  meanwhile), then `not_measured: <reason>`.
Several monitors: the deploy is `measured` when any one measured; the others' reasons are kept in its note.

Every write is the subject deployment's org (`cron.py`): rows are stamped with `deployment.org_id`.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.automations import TaskTrigger
from app.models.production_qa import Deployment, DeploymentMetric
from app.models.projects import Project
from app.production_qa import cron, metric_pulls
from app.production_qa.events import RaiseIntent

log = logging.getLogger("cockpit-production-qa")

# A post window cut shorter than this by the next deploy is not measured.
MIN_WINDOW = timedelta(minutes=15)
# A failed pull is retried this many times, this far apart.
RETRIES = 2
RETRY_AFTER = timedelta(minutes=5)
# One pass stops picking new deploys after this many seconds (each pull is bounded by `metric_pulls`' timeout);
# the rest are measured on the next minute. Inside `cron.TIMEOUT_SECONDS`.
PASS_BUDGET_SECONDS = 25.0
# Every pull of one deploy ends by this many seconds into the pass (a slow monitor is a failed pull, retried), so the
# row lock and the pass never outlive `cron.TIMEOUT_SECONDS`.
DEPLOY_DEADLINE_SECONDS = 40.0
_RETRY_NOTE = re.compile(r"^retry (\d+) of \d+: ")

METRIC_ERROR_RATE = "error_rate"
METRIC_P95 = "p95_latency_ms"
_TITLES = {METRIC_ERROR_RATE: "Error rate", METRIC_P95: "p95 latency"}


def schedule(session: Session, deployment: Deployment) -> None:
    """Make a finished anchor's regression window due (module docstring); idempotent — a deploy already pending or
    settled keeps its state."""
    if not deployment.is_anchor or deployment.finished_at is None or deployment.regression_status is not None:
        return
    window = session.scalar(select(Project.regression_window_min).where(Project.id == deployment.project_id)) or 60
    deployment.regression_due_at = deployment.finished_at + timedelta(minutes=window)
    deployment.regression_status = "pending"


# ── The windows ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Windows:
    post_start: datetime
    post_end: datetime
    baseline_start: datetime
    baseline_end: datetime

    @property
    def minutes(self) -> int:
        return round((self.post_end - self.post_start).total_seconds() / 60)

    def facts(self) -> dict[str, Any]:
        return {"minutes": self.minutes, "post": [self.post_start.isoformat(), self.post_end.isoformat()],
                "baseline": [self.baseline_start.isoformat(), self.baseline_end.isoformat()]}


def windows(session: Session, deployment: Deployment, minutes: int,
            now: datetime) -> tuple[Optional[Windows], Optional[str]]:
    """The post and baseline windows, or the reason there are none (superseded). The post window never runs past
    `now` (a window widened after the deploy was scheduled)."""
    assert deployment.finished_at is not None
    width = timedelta(minutes=minutes)
    started = deployment.started_at or deployment.finished_at
    post_end = min(deployment.finished_at + width, now)
    successor = session.execute(
        select(Deployment.id, Deployment.finished_at).where(
            Deployment.project_id == deployment.project_id, Deployment.environment == deployment.environment,
            Deployment.is_anchor.is_(True), Deployment.id != deployment.id,
            Deployment.finished_at > deployment.finished_at, Deployment.finished_at < post_end,
        ).order_by(Deployment.finished_at, Deployment.id).limit(1)
    ).first()
    if successor is not None:
        if successor.finished_at - deployment.finished_at < MIN_WINDOW:
            return None, f"superseded by deploy #{successor.id}"
        post_end = successor.finished_at
    return Windows(deployment.finished_at, post_end, started - width, started), None


# ── One measurement ─────────────────────────────────────────────────────────────────────────────────

@dataclass
class Measured:
    """One metric of one monitor for one deploy, before it is stored."""

    metric: str
    baseline: Optional[float]
    post: Optional[float]
    events_post: Optional[int]
    breached: bool
    note: Optional[str]

    @property
    def delta_abs(self) -> Optional[float]:
        return None if self.baseline is None or self.post is None else self.post - self.baseline

    @property
    def delta_pct(self) -> Optional[float]:
        if self.delta_abs is None or not self.baseline:
            return None
        return self.delta_abs / self.baseline * 100


def _rate(sample: metric_pulls.Sample) -> Optional[float]:
    if sample.errors is None or not sample.requests:
        return None
    return sample.errors / sample.requests


def compare(project: Project, baseline: metric_pulls.Sample, post: metric_pulls.Sample) -> list[Measured]:
    """Both metrics of one monitor against the project's thresholds (module docstring)."""
    out: list[Measured] = []
    before, after = _rate(baseline), _rate(post)
    events = int(post.errors) if post.errors is not None else None
    if before is None or after is None:
        side = "post-deploy" if after is None else "baseline"
        out.append(Measured(METRIC_ERROR_RATE, before, after, events, False, f"no requests in the {side} window"))
    else:
        # `after > before` too: a zero baseline and a zero post window is no regression whatever the ratio.
        breached = (after > before and after >= project.regression_error_ratio * before
                    and (events or 0) >= project.regression_min_events)
        out.append(Measured(METRIC_ERROR_RATE, before, after, events, breached, None))
    if baseline.p95_ms is not None and post.p95_ms is not None:
        breached = baseline.p95_ms > 0 and post.p95_ms >= project.regression_p95_ratio * baseline.p95_ms
        out.append(Measured(METRIC_P95, baseline.p95_ms, post.p95_ms, None, breached, None))
    elif baseline.p95_ms is not None or post.p95_ms is not None:
        side = "post-deploy" if post.p95_ms is None else "baseline"
        out.append(Measured(METRIC_P95, baseline.p95_ms, post.p95_ms, None, False, f"no latency in the {side} window"))
    return out


def _monitors(session: Session, project_id: int) -> list[TaskTrigger]:
    """The project's live monitor triggers that name a metrics source (A-12: a paused or failed one measures
    nothing)."""
    rows = session.scalars(select(TaskTrigger).where(
        TaskTrigger.project_id == project_id, TaskTrigger.source_kind.in_(tuple(metric_pulls.CREDENTIAL_TYPES)),
        TaskTrigger.paused_at.is_(None), TaskTrigger.failed_at.is_(None),
    ).order_by(TaskTrigger.id))
    return [t for t in rows if (t.source_config or {}).get("metrics")]


def _retries_taken(note: Optional[str]) -> int:
    found = _RETRY_NOTE.match(note or "")
    return int(found.group(1)) if found else 0


def _incident_facts(deployment: Deployment, m: Measured, win: Windows, trigger: TaskTrigger) -> dict[str, Any]:
    return {"metric": m.metric, "baseline": m.baseline, "post": m.post, "delta_abs": m.delta_abs,
            "delta_pct": m.delta_pct, "events_post": m.events_post, "window": win.facts(),
            "monitor": trigger.source_kind, "trigger_id": str(trigger.id), "deployment_id": deployment.id}


def format_value(metric: str, value: Optional[float]) -> str:
    """A metric's value as people read it: an error rate in percent, a latency in ms."""
    if value is None:
        return "—"
    return f"{value * 100:.2f}%" if metric == METRIC_ERROR_RATE else f"{value:.0f} ms"


def _record_metric(session: Session, deployment: Deployment, metric: str, breaches: list[tuple[Measured, TaskTrigger]],
                   measured: bool, win: Windows) -> list[RaiseIntent]:
    """The incident side of one metric: a breach records (or folds onto) its regression incident; a measured
    non-breach resolves it."""
    from app.production_qa import fingerprints, incidents

    fingerprint = fingerprints.regression(deployment.environment, metric)
    if breaches:
        worst, trigger = max(breaches, key=lambda b: b[0].delta_pct if b[0].delta_pct is not None else float("inf"))
        title = (f"{_TITLES[metric]} regressed after deploy #{deployment.id} ({deployment.environment}): "
                 f"{format_value(metric, worst.baseline)} → {format_value(metric, worst.post)}")
        _, intents = incidents.record(session, deployment.project_id, incidents.Observation(
            type="regression", source_kind=trigger.source_kind, trigger_id=trigger.id, fingerprint=fingerprint,
            title=title, severity="error", environment=deployment.environment, occurred_at=win.post_end, count=None,
            count_mode="increment", action="occurrence", release=None, commit_sha=deployment.head_sha,
            stack_frames=None, vendor_url=None, vendor_ref=None, facts=_incident_facts(deployment, worst, win, trigger),
            deployment_id=deployment.id, delivery_id=None,
        ))
        return intents
    if measured:
        incidents.record(session, deployment.project_id, incidents.resolution(
            "regression", "regression", fingerprint, title="", severity="error", deployment=deployment, at=win.post_end,
        ))
    return []


def measure(session: Session, deployment: Deployment, now: datetime,
            deadline: float = float("inf")) -> list[RaiseIntent]:
    """Measure one due deploy (module docstring): store `deployment_metrics`, record or resolve regressions, and
    settle `regression_status` — or leave it pending for a retry. Flushes, never commits."""
    project = session.scalar(select(Project).where(Project.id == deployment.project_id))
    if project is None or deployment.finished_at is None:
        deployment.regression_status, deployment.regression_note = "not_measured", "the deploy has no finish time"
        return []
    monitors = _monitors(session, project.id)
    if not monitors:
        deployment.regression_status = "not_measured"
        deployment.regression_note = "no monitor with metrics on this project"
        return []
    win, reason = windows(session, deployment, project.regression_window_min, now)
    if win is not None and win.post_end - win.post_start < MIN_WINDOW:
        win, reason = None, "the post-deploy window is shorter than 15 minutes"
    if win is None:
        deployment.regression_status, deployment.regression_note = "not_measured", reason
        return []
    results: list[tuple[TaskTrigger, list[Measured]]] = []
    config_reasons: list[str] = []
    pull_reasons: list[str] = []
    for trigger in monitors:
        try:
            baseline = metric_pulls.pull(session, trigger, win.baseline_start, win.baseline_end, deadline)
            post = metric_pulls.pull(session, trigger, win.post_start, win.post_end, deadline)
        except metric_pulls.ConfigError as exc:
            config_reasons.append(f"{trigger.name}: {exc}")
            continue
        except metric_pulls.PullError as exc:
            pull_reasons.append(f"{trigger.name}: {exc}")
            continue
        results.append((trigger, compare(project, baseline, post)))
    if not results and pull_reasons:
        taken = _retries_taken(deployment.regression_note)
        if taken < RETRIES:
            deployment.regression_note = f"retry {taken + 1} of {RETRIES}: " + "; ".join(pull_reasons + config_reasons)
            deployment.regression_due_at = now + RETRY_AFTER
            return []
    if not results:
        deployment.regression_status = "not_measured"
        deployment.regression_note = "; ".join(pull_reasons + config_reasons)
        return []
    intents: list[RaiseIntent] = []
    measured_at = now
    by_metric: dict[str, tuple[list[tuple[Measured, TaskTrigger]], bool]] = {}
    for trigger, rows in results:
        for m in rows:
            session.add(DeploymentMetric(
                org_id=deployment.org_id, deployment_id=deployment.id, trigger_id=trigger.id, metric=m.metric,
                baseline=m.baseline, post=m.post, delta_abs=m.delta_abs, delta_pct=m.delta_pct,
                events_post=m.events_post, window_min=win.minutes, breached=m.breached, note=m.note,
                measured_at=measured_at,
            ))
            breaches, any_measured = by_metric.get(m.metric, ([], False))
            if m.breached:
                breaches.append((m, trigger))
            by_metric[m.metric] = (breaches, any_measured or m.note is None)
    session.flush()
    for metric, (breaches, any_measured) in by_metric.items():
        intents += _record_metric(session, deployment, metric, breaches, any_measured, win)
    deployment.regression_status = "measured"
    deployment.regression_note = "; ".join(pull_reasons + config_reasons) or None
    return intents


# ── The cron job ────────────────────────────────────────────────────────────────────────────────────

# Due deploys one pass lists at most.
DUE_BATCH = 20


def regression_due(session: Session, now: datetime) -> None:
    """The `regression_due` job: measure every deploy whose window is due, each in its own commit, its raises
    flushed after it (the recorder's doctrine). Stops picking new ones after `PASS_BUDGET_SECONDS`."""
    from app.production_qa import events

    started = time.monotonic()
    due = list(session.scalars(
        select(Deployment.id).where(Deployment.regression_status == "pending", Deployment.regression_due_at <= now)
        .order_by(Deployment.regression_due_at, Deployment.id).limit(DUE_BATCH)
    ))
    for deployment_id in due:
        if time.monotonic() - started > PASS_BUDGET_SECONDS:
            break
        deployment = session.scalar(select(Deployment).where(
            Deployment.id == deployment_id, Deployment.regression_status == "pending",
        ).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if deployment is None:
            continue
        try:
            with session.begin_nested():
                intents = measure(session, deployment, now, started + DEPLOY_DEADLINE_SECONDS)
        except Exception:  # noqa: BLE001 — one deploy's fault must not stop the pass, nor recur every minute
            log.exception("production-qa: measuring deploy %s failed", deployment_id)
            deployment.regression_status, deployment.regression_note = "not_measured", "measurement failed"
            intents = []
        session.commit()
        if intents:
            events.flush(intents)


cron.register("regression_due", regression_due)
