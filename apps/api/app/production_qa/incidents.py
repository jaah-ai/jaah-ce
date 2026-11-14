"""Incidents: `record` folds an observation onto its fingerprint's row and moves the state machine; deploy failures
are recorded and resolved from deploys.

THE STATE MACHINE (`incidents.state`, one row per `(project_id, fingerprint)`; every move writes an `incident_events`
row, in order):
* first occurrence → `open` (`opened`); a later one on an open or reopened row folds (`occurred`): `last_seen` moves,
  `count` is the larger of stored and sent (`absolute`) or one more (`increment`);
* resolve → `resolved` (`resolved`); a resolve of a fingerprint never seen records nothing (no row);
* an occurrence on a resolved row → `reopened` on the SAME row (`reopened`);
* an event older than the row's last transition is stale: it changes nothing and leaves an `ignored_stale` event.

WHAT A TRANSITION RAISES (`RaiseIntent`, flushed after commit by `events.flush`): `opened`; `reopened` unless it falls
within `REOPEN_COOLDOWN` of the resolve (flap damping, A-14); `occurred` only while no unreleased non-revert
`incident_tasks` claim exists AND the last raise is older than `OCCURRED_THROTTLE` (A-17). `last_raised_at` is stamped
whenever an intent is returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.production_qa import Deployment, Incident, IncidentEvent, IncidentTask
from app.models.projects import Project
from app.production_qa.events import RaiseIntent, RaiseSource
from app.production_qa.vocabulary import COUNT_MODES, INCIDENT_TYPES, SEVERITIES
from app.clock import utcnow

# A reopen this soon after its resolve is recorded but raises nothing (A-14).
REOPEN_COOLDOWN = timedelta(minutes=15)
# An `occurred` raises at most once per this window, and never while a task claim is open (A-17).
OCCURRED_THROTTLE = timedelta(minutes=30)
# `incidents.stack_frames` keeps at most this many frames.
MAX_STACK_FRAMES = 50

Transition = Literal["opened", "occurred", "resolved", "reopened", "ignored"]


@dataclass
class Observation:
    type: str
    source_kind: str
    trigger_id: Optional[UUID]
    fingerprint: str
    title: str
    severity: str
    environment: Optional[str]
    occurred_at: datetime
    count: Optional[int]
    count_mode: Literal["absolute", "increment"]
    action: Literal["occurrence", "resolve"]
    release: Optional[str]
    commit_sha: Optional[str]
    stack_frames: Optional[list[dict[str, Any]]]
    vendor_url: Optional[str]
    vendor_ref: Optional[str]
    facts: dict[str, Any]
    deployment_id: Optional[int]
    # `trigger_deliveries.id`; a GitHub delivery GUID lives only in `branch_pushes.gh_delivery`.
    delivery_id: Optional[int]
    # When the vendor first saw it, if it says (Sentry's `firstSeen`); otherwise the first occurrence's own time.
    first_seen: Optional[datetime] = None


@dataclass
class Recorded:
    # None only for a resolve of a fingerprint never seen: nothing was recorded.
    incident: Optional[Incident]
    transition: Transition


_now = utcnow  # bound per module, so a test freezes this clock without the others'


def _raise_source(incident_type: str) -> RaiseSource:
    """Which CP-raised trigger kind an incident's transitions go to (architecture §6.1)."""
    if incident_type == "deploy_failed":
        return "deploy_failed"
    if incident_type == "health_breach":
        return "health_breach"
    return "incident"


def _check(obs: Observation) -> None:
    if obs.type not in INCIDENT_TYPES:
        raise ValueError(f"unknown incident type {obs.type!r}")
    if obs.severity not in SEVERITIES:
        raise ValueError(f"unknown severity {obs.severity!r}; one of {', '.join(SEVERITIES)}")
    if obs.count_mode not in COUNT_MODES:
        raise ValueError(f"unknown count mode {obs.count_mode!r}")
    if obs.occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")


def _locked(session: Session, project_id: int, fingerprint: str) -> Optional[Incident]:
    return session.scalar(
        select(Incident)
        .where(Incident.project_id == project_id, Incident.fingerprint == fingerprint)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _event(session: Session, incident: Incident, kind: str, obs: Observation, note: Optional[str] = None) -> None:
    session.add(IncidentEvent(org_id=incident.org_id, incident_id=incident.id, kind=kind, at=obs.occurred_at,
                              delivery_id=obs.delivery_id, note=note))


def _claim_open(session: Session, incident_id: int) -> bool:
    """An unreleased non-revert task claim: a task for this incident is (or is being) filed."""
    return session.scalar(
        select(IncidentTask.incident_id).where(
            IncidentTask.incident_id == incident_id,
            IncidentTask.role != "revert",
            IncidentTask.released_at.is_(None),
        ).limit(1)
    ) is not None


def _fold(incident: Incident, obs: Observation) -> None:
    """An occurrence's facts onto the row: the latest description wins, `last_seen` only moves forward."""
    if obs.occurred_at > incident.last_seen:
        incident.last_seen = obs.occurred_at
    if obs.first_seen is not None and obs.first_seen < incident.first_seen:
        incident.first_seen = obs.first_seen
    if obs.count_mode == "absolute" and obs.count is not None:
        incident.count = max(incident.count or 0, obs.count)
    else:
        incident.count = (incident.count or 0) + 1
    incident.count_mode = obs.count_mode
    incident.title = obs.title
    incident.severity = obs.severity
    # A field the observation leaves out keeps what an earlier one sent.
    incident.environment = obs.environment or incident.environment
    incident.release = obs.release or incident.release
    incident.commit_sha = obs.commit_sha or incident.commit_sha
    incident.vendor_url = obs.vendor_url or incident.vendor_url
    incident.vendor_ref = obs.vendor_ref or incident.vendor_ref
    if obs.deployment_id is not None:
        incident.deployment_id = obs.deployment_id
    if obs.stack_frames:
        incident.stack_frames = obs.stack_frames[:MAX_STACK_FRAMES]
    if obs.facts:
        incident.facts = obs.facts


def _opened(session: Session, project_id: int, obs: Observation) -> Optional[int]:
    """Insert the fingerprint's row unless it exists; its id when this call created it. A concurrent insert of the
    same fingerprint waits on the first and then does nothing, so a fingerprint never holds two rows."""
    org_id = session.scalar(select(Project.org_id).where(Project.id == project_id))
    if org_id is None:
        raise ValueError(f"no project {project_id}")
    first_count = obs.count if obs.count_mode == "absolute" and obs.count is not None else 1
    return session.execute(
        pg_insert(Incident)
        .values(
            org_id=org_id, project_id=project_id, type=obs.type, source_kind=obs.source_kind,
            trigger_id=obs.trigger_id, fingerprint=obs.fingerprint, title=obs.title, severity=obs.severity,
            environment=obs.environment, state="open",
            first_seen=min(obs.first_seen, obs.occurred_at) if obs.first_seen else obs.occurred_at,
            last_seen=obs.occurred_at,
            last_transition_at=obs.occurred_at, count=first_count, count_mode=obs.count_mode, release=obs.release,
            commit_sha=obs.commit_sha,
            stack_frames=obs.stack_frames[:MAX_STACK_FRAMES] if obs.stack_frames else None,
            vendor_url=obs.vendor_url, vendor_ref=obs.vendor_ref, facts=obs.facts or {},
            deployment_id=obs.deployment_id,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "fingerprint"])
        .returning(Incident.id)
    ).scalar()


def _raised(incident: Incident, transition: Transition) -> list[RaiseIntent]:
    incident.last_raised_at = _now()
    return [RaiseIntent(incident_id=incident.id, source_kind=_raise_source(incident.type), transition=transition)]


def record(session: Session, project_id: int, obs: Observation) -> tuple[Recorded, list[RaiseIntent]]:
    """Upsert the incident for `obs` and apply its transition (module docstring). Flushes, never commits; the caller's
    transaction (the recorder's savepoint) owns the write. Raises ValueError on an observation outside the
    vocabulary."""
    from app.production_qa import linkage

    _check(obs)
    if obs.action == "resolve":
        incident = _locked(session, project_id, obs.fingerprint)
        if incident is None:
            return Recorded(None, "ignored"), []
        created = False
    else:
        created = _opened(session, project_id, obs) is not None
        incident = _locked(session, project_id, obs.fingerprint)
        if incident is None:
            raise RuntimeError(f"incident {obs.fingerprint!r} vanished between its insert and its lock")
    if created:
        _event(session, incident, "opened", obs)
        session.flush()
        recorded = Recorded(incident, "opened")
        linkage.compute(session, recorded)
        return recorded, _raised(incident, "opened")
    if obs.occurred_at < incident.last_transition_at:
        _event(session, incident, "ignored_stale", obs,
               note=f"{obs.action} at {obs.occurred_at.isoformat()} predates the last transition")
        session.flush()
        return Recorded(incident, "ignored"), []
    if obs.action == "resolve":
        if incident.state == "resolved":
            return Recorded(incident, "ignored"), []
        incident.state = "resolved"
        incident.resolved_at = obs.occurred_at
        incident.last_transition_at = obs.occurred_at
        _event(session, incident, "resolved", obs)
        session.flush()
        recorded = Recorded(incident, "resolved")
        linkage.on_resolved(session, recorded)
        return recorded, []
    _fold(incident, obs)
    if incident.state == "resolved":
        resolved_at = incident.resolved_at
        incident.state = "reopened"
        incident.resolved_at = None
        incident.last_transition_at = obs.occurred_at
        _event(session, incident, "reopened", obs)
        session.flush()
        recorded = Recorded(incident, "reopened")
        linkage.compute(session, recorded)
        if resolved_at is not None and obs.occurred_at - resolved_at < REOPEN_COOLDOWN:
            return recorded, []
        return recorded, _raised(incident, "reopened")
    _event(session, incident, "occurred", obs)
    session.flush()
    recorded = Recorded(incident, "occurred")
    if _claim_open(session, incident.id):
        return recorded, []
    if incident.last_raised_at is not None and _now() - incident.last_raised_at < OCCURRED_THROTTLE:
        return recorded, []
    return recorded, _raised(incident, "occurred")


# The deployments a `deploy_failed` incident's facts remember having recorded (the dedup key of a run).
_RUNS_KEPT = 50


def record_deploy_failure(session: Session, deployment: Deployment) -> list[RaiseIntent]:
    """Record a failed deploy (any environment, D5) as a `deploy_failed` incident keyed on its workflow, environment
    and failing job (`fingerprints.deploy`): a second failure of the same job folds onto it, another job is another
    incident. A run is recorded once: its id stays in `facts.deployment_ids`. A run first recorded as job `run` (or
    under a guessed environment) whose job or environment arrives later is re-keyed in place while the incident is
    its alone — one run, one incident. The facts are what the triage task carries; the runner fetches the log tail
    itself (R2)."""
    from app.models.production_qa import DeploymentPr
    from app.production_qa import fingerprints

    fingerprint = fingerprints.deploy(deployment.environment, deployment.workflow_path, deployment.failing_job)
    prs = sorted(session.scalars(select(DeploymentPr.pr_number).where(DeploymentPr.deployment_id == deployment.id)))
    span = (f"{deployment.range_from_sha[:7]}..{deployment.head_sha[:7]}" if deployment.range_from_sha
            else deployment.head_sha[:7])
    workflow = deployment.workflow_name or deployment.workflow_path
    title = f"Release failed: {workflow} ({deployment.environment})"
    if deployment.failing_job:
        title += f" — {deployment.failing_job}"
    facts = {"run_url": deployment.run_url, "run_id": deployment.run_id, "run_attempt": deployment.run_attempt,
             "run_event": deployment.run_event, "workflow_path": deployment.workflow_path,
             "head_branch": deployment.head_branch, "failing_job": deployment.failing_job,
             "failing_step": deployment.failing_step, "range": span, "prs": prs}
    recorded = session.scalars(select(Incident).where(
        Incident.project_id == deployment.project_id, Incident.type == "deploy_failed",
        Incident.facts["deployment_ids"].contains([deployment.id]),
    ).order_by(Incident.id).with_for_update()).all()
    same = next((i for i in recorded if i.fingerprint == fingerprint), None)
    if same is not None:
        # Recorded already; while this run is still the incident's latest, its facts follow the run (a range that
        # resolved after the failure was filed brings its PRs).
        if same.deployment_id == deployment.id:
            same.facts = {**facts, "deployment_ids": (same.facts or {}).get("deployment_ids") or [deployment.id]}
            session.flush()
        return []
    taken = session.scalar(select(Incident.id).where(
        Incident.project_id == deployment.project_id, Incident.fingerprint == fingerprint)) is not None
    alone = [i for i in recorded if i.count == 1 and i.state != "resolved"]
    if alone and not taken:
        incident = alone[0]
        incident.fingerprint, incident.title, incident.environment = fingerprint, title, deployment.environment
        incident.facts = {**facts, "deployment_ids": [deployment.id]}
        session.flush()
        return []
    earlier = session.scalar(select(Incident.facts).where(
        Incident.project_id == deployment.project_id, Incident.fingerprint == fingerprint))
    runs = [*((earlier or {}).get("deployment_ids") or []), deployment.id][-_RUNS_KEPT:]
    _, intents = record(session, deployment.project_id, Observation(
        type="deploy_failed", source_kind=deployment.forge, trigger_id=None, fingerprint=fingerprint, title=title,
        severity="error", environment=deployment.environment,
        occurred_at=deployment.finished_at or deployment.last_received_at, count=None, count_mode="increment",
        action="occurrence", release=None, commit_sha=deployment.head_sha, stack_frames=None,
        vendor_url=deployment.run_url, vendor_ref=str(deployment.run_id), facts={**facts, "deployment_ids": runs},
        deployment_id=deployment.id, delivery_id=None,
    ))
    return intents


def resolve_deploy_failures(session: Session, deployment: Deployment) -> None:
    """A successful anchor resolves every open `deploy_failed` incident of its workflow and environment, each as of
    the anchor's finish (an older failure arriving later stays resolved: the stale rule)."""
    from app.production_qa.deployments import open_deploy_failures

    at = deployment.finished_at or deployment.last_received_at
    for incident in open_deploy_failures(session, deployment):
        record(session, deployment.project_id, resolution(
            "deploy_failed", deployment.forge, incident.fingerprint, title=incident.title, severity=incident.severity,
            deployment=deployment, at=at, vendor_url=deployment.run_url, vendor_ref=str(deployment.run_id),
        ))


def resolution(type: str, source_kind: str, fingerprint: str, *, title: str, severity: str, deployment: Deployment,
               at: datetime, vendor_url: Optional[str] = None, vendor_ref: Optional[str] = None) -> Observation:
    """A `resolve` of `fingerprint` as of `at`, stamped with `deployment`'s environment and head: what every
    deploy-driven resolver records (`resolve_deploy_failures`, `probes._resolve_removed`, `regression._record_metric`)."""
    return Observation(
        type=type, source_kind=source_kind, trigger_id=None, fingerprint=fingerprint, title=title, severity=severity,
        environment=deployment.environment, occurred_at=at, count=None, count_mode="increment", action="resolve",
        release=None, commit_sha=deployment.head_sha, stack_frames=None, vendor_url=vendor_url, vendor_ref=vendor_ref,
        facts={}, deployment_id=None, delivery_id=None,
    )
