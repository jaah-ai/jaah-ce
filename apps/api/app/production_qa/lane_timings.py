"""Merge-lane stage timings (architecture §9, C11): every full gate run of the lane reports how long each named stage
took and how it ended (`gate_timings`, the lane script's `stage_timings`); `store` keeps one
`merge_lane_stage_timings` row per stage, naming the PRs the gated tree held, so a PR's task shows the runs that
covered it (`for_task`).

THE SLOWDOWN RULE (the `lane_slowdown` cron job, per `(project, stage)`, over `passed` rows only — a failed stage
stopped early and a flaked one ran twice): the median of the last `RECENT` runs at least `SLOWDOWN_RATIO` × the
median of the `BASELINE` runs before them records a `lane_slowdown` incident on `lane:{stage}` (severity `error`, so
the default incident automation files it); its facts name the stage, both medians and the PRs since the shift. While
it is open nothing more is recorded (the dedup per suite); once each of the last `RECENT` runs is under
`RECOVERED_RATIO` × the baseline it was opened on, it resolves. Recording happens whatever the Production QA gate
says: the gate decides only whether a task is filed (`events.admission`).
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models.merge_lane import MergeLaneRow
from app.models.production_qa import Incident, MergeLaneStageTiming
from app.models.projects import Project
from app.models.tasks import Task
from app.production_qa import cron, fingerprints

log = logging.getLogger("cockpit-production-qa")

RECENT = 5
BASELINE = 20
SLOWDOWN_RATIO = 1.3
RECOVERED_RATIO = 1.1
# Only a stage with a passed run this recent is evaluated: its verdict changes only when a run lands, and a pass that
# missed a minute still sees it (the job runs every 60 s).
SCAN_WINDOW = timedelta(minutes=15)
# The most PR numbers an incident's facts list.
MAX_PRS = 50
# The most gate runs a task's lane area lists, newest first.
TASK_RUNS = 10

_OPEN = ("open", "reopened")


def store(session: Session, lane_task: Task, project: Project, body: Mapping[str, Any]) -> int:
    """One row per stage of a `gate_timings` event; how many were written (none for an event without `stages`)."""
    stages = body.get("stages") or []
    now = utcnow()
    for stage in stages:
        session.add(MergeLaneStageTiming(
            org_id=project.org_id, project_id=project.id, lane_task_id=lane_task.id, tree_hash=body.get("tree_hash"),
            pr_numbers=list(body.get("pr_numbers") or []), stage=stage["label"], seconds=float(stage["seconds"]),
            outcome=stage["outcome"], recorded_at=now,
        ))
    session.flush()
    return len(stages)


# ── The per-PR view ─────────────────────────────────────────────────────────────────────────────────────────────


def for_task(session: Session, task: Task) -> list[dict[str, Any]]:
    """The lane gate runs whose tree held one of this task's PRs, newest first: each run's stages in gate order."""
    prs = list(session.scalars(
        select(MergeLaneRow.pr_number).where(MergeLaneRow.task_id == task.id).distinct()
    ))
    if not prs or task.project_id is None:
        return []
    runs = session.execute(
        select(MergeLaneStageTiming.lane_task_id, MergeLaneStageTiming.tree_hash, MergeLaneStageTiming.recorded_at)
        .where(MergeLaneStageTiming.project_id == task.project_id, MergeLaneStageTiming.pr_numbers.overlap(prs))
        .group_by(MergeLaneStageTiming.lane_task_id, MergeLaneStageTiming.tree_hash, MergeLaneStageTiming.recorded_at)
        .order_by(MergeLaneStageTiming.recorded_at.desc())
        .limit(TASK_RUNS)
    ).all()
    out: list[dict[str, Any]] = []
    for lane_task_id, tree_hash, recorded_at in runs:
        rows = list(session.scalars(
            select(MergeLaneStageTiming).where(
                MergeLaneStageTiming.project_id == task.project_id,
                MergeLaneStageTiming.lane_task_id.is_(None) if lane_task_id is None
                else MergeLaneStageTiming.lane_task_id == lane_task_id,
                MergeLaneStageTiming.tree_hash == tree_hash, MergeLaneStageTiming.recorded_at == recorded_at,
            ).order_by(MergeLaneStageTiming.id)
        ))
        out.append({
            "lane_task_id": lane_task_id, "tree_hash": tree_hash, "recorded_at": recorded_at,
            "pr_numbers": rows[0].pr_numbers if rows else [],
            "stages": [{"stage": r.stage, "seconds": r.seconds, "outcome": r.outcome} for r in rows],
        })
    return out


# ── The slowdown rule ───────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """What one `(project, stage)`'s passed runs say: `slow` (the rule fires) with both medians."""

    slow: bool
    recent_median: float
    baseline_median: float


def judge(seconds_newest_first: list[float]) -> Optional[Verdict]:
    """The slowdown rule over a stage's passed runs, newest first; None until `RECENT + BASELINE` runs exist."""
    if len(seconds_newest_first) < RECENT + BASELINE:
        return None
    recent = statistics.median(seconds_newest_first[:RECENT])
    baseline = statistics.median(seconds_newest_first[RECENT:RECENT + BASELINE])
    return Verdict(recent >= SLOWDOWN_RATIO * baseline, recent, baseline)


def recovered(seconds_newest_first: list[float], baseline: float) -> bool:
    """Each of the last `RECENT` passed runs is back under `RECOVERED_RATIO` × the baseline the incident opened on."""
    last = seconds_newest_first[:RECENT]
    return len(last) == RECENT and all(s < RECOVERED_RATIO * baseline for s in last)


def _passed(session: Session, project_id: int, stage: str) -> list[MergeLaneStageTiming]:
    return list(session.scalars(
        select(MergeLaneStageTiming).where(
            MergeLaneStageTiming.project_id == project_id, MergeLaneStageTiming.stage == stage,
            MergeLaneStageTiming.outcome == "passed",
        ).order_by(MergeLaneStageTiming.recorded_at.desc(), MergeLaneStageTiming.id.desc()).limit(RECENT + BASELINE)
    ))


def _prs_since(session: Session, project_id: int, stage: str, since: datetime) -> list[int]:
    """Every PR a run of `stage` covered since `since` (any outcome), in the order first seen."""
    seen: dict[int, None] = {}
    for numbers in session.scalars(
        select(MergeLaneStageTiming.pr_numbers).where(
            MergeLaneStageTiming.project_id == project_id, MergeLaneStageTiming.stage == stage,
            MergeLaneStageTiming.recorded_at >= since,
        ).order_by(MergeLaneStageTiming.recorded_at, MergeLaneStageTiming.id)
    ):
        seen.update(dict.fromkeys(numbers or []))
    return list(seen)[:MAX_PRS]


def _observation(stage: str, action: str, at: datetime, title: str, facts: dict[str, Any]):
    from app.production_qa.incidents import Observation

    return Observation(
        type="lane_slowdown", source_kind="merge_lane", trigger_id=None, fingerprint=fingerprints.lane(stage),
        title=title, severity="error", environment=None, occurred_at=at, count=None, count_mode="increment",
        action=action,  # type: ignore[arg-type]
        release=None, commit_sha=None, stack_frames=None, vendor_url=None, vendor_ref=None, facts=facts,
        deployment_id=None, delivery_id=None,
    )


def evaluate(session: Session, project_id: int, stage: str, now: datetime) -> list:
    """Apply the slowdown rule to one `(project, stage)`; the raise intents to flush after the commit."""
    from app.production_qa import incidents

    rows = _passed(session, project_id, stage)
    seconds = [float(r.seconds) for r in rows]
    open_incident = session.scalar(select(Incident).where(
        Incident.project_id == project_id, Incident.fingerprint == fingerprints.lane(stage),
        Incident.state.in_(_OPEN),
    ))
    if open_incident is not None:
        baseline = (open_incident.facts or {}).get("baseline_median_seconds")
        if isinstance(baseline, (int, float)) and recovered(seconds, float(baseline)):
            _, intents = incidents.record(session, project_id, _observation(
                stage, "resolve", now, open_incident.title, open_incident.facts or {}))
            return intents
        return []
    verdict = judge(seconds)
    if verdict is None or not verdict.slow:
        return []
    shift = rows[RECENT - 1].recorded_at
    facts = {
        "stage": stage, "baseline_median_seconds": round(verdict.baseline_median, 1),
        "recent_median_seconds": round(verdict.recent_median, 1), "recent_runs": RECENT, "baseline_runs": BASELINE,
        "prs_since_shift": _prs_since(session, project_id, stage, shift), "shift_at": shift.isoformat(),
    }
    title = (f"Merge-lane stage {stage} slowed down: median {verdict.recent_median:.0f}s over the last {RECENT} runs, "
             f"was {verdict.baseline_median:.0f}s")
    _, intents = incidents.record(session, project_id, _observation(stage, "occurrence", now, title, facts))
    return intents


def lane_slowdown(session: Session, now: datetime) -> None:
    """The `lane_slowdown` job: every `(project, stage)` with a passed run in `SCAN_WINDOW`, each in its own commit,
    its raises flushed after it; one pair's fault is logged and the rest go on. Every write is the project's own org
    (`incidents.record` reads it off the project)."""
    from app.production_qa import events

    pairs = session.execute(
        select(MergeLaneStageTiming.project_id, MergeLaneStageTiming.stage)
        .where(MergeLaneStageTiming.outcome == "passed", MergeLaneStageTiming.recorded_at >= now - SCAN_WINDOW)
        .group_by(MergeLaneStageTiming.project_id, MergeLaneStageTiming.stage)
        .order_by(MergeLaneStageTiming.project_id, MergeLaneStageTiming.stage)
    ).all()
    for project_id, stage in pairs:
        try:
            intents = evaluate(session, project_id, stage, now)
            session.commit()
        except Exception:  # noqa: BLE001 — one stage's fault must not stop the others
            session.rollback()
            log.exception("production-qa: lane slowdown of project %s stage %r failed", project_id, stage)
            continue
        if intents:
            events.flush(intents)


cron.register("lane_slowdown", lane_slowdown)
