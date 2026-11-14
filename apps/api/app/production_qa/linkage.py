"""Suspect linkage and the fix loop: which PRs an incident points at, and which task's deploy resolved it.

SUSPECTS (`incident_suspects`), computed on `opened`/`reopened` (`compute`), first basis that finds a PR wins:
* `release` (`high`): the incident's `commit_sha`, or a `release` of 7+ hex characters with a hex letter, contained in a production
  anchor's `deployment_commits` → that anchor's PRs; when the commit is itself a PR's merge SHA, that PR alone.
* `deployment` (`high`): an incident naming its `deployment_id` (deploy_failed, regression, health_breach) → that
  deploy's PRs.
* window: the project's anchors finished in `[first_seen − WINDOW, first_seen]`, the latest `WINDOW_ANCHORS`. A PR
  whose changed files (`task_pr_changes.files`) suffix-match a stack-frame path is `medium` (`window_paths`), any
  other `low` (`window`). The frames read are the `in_app` ones when the monitor flagged any, else all of them.
* nothing found: no row — the console says "No suspect".
`score` only orders the words (`high` above `medium` above `low`); it is not a ranking model.

DECIDED ROWS: an operator's correction (`correct`, `add`) sets `state`, `decided_by`, `decided_at`; a recompute never
touches a decided row and drops only the undecided suggestions it no longer finds. Undo (`correct` back to
`suggested`) clears the decider, handing the row back to the computation; an operator-added row is dismissed instead.

LEDGER EDGES (`production_qa_edges`, pending until the ledger applier stamps `applied_at`):
* `incident_linked` — one per suspect task that is `confirmed`, or `suggested` with `high` confidence, kept in step on
  open, reopen and every correction (`_sync_linked`): a task leaving the set has its edge `revoked_at`; one coming
  back is un-revoked and pending again.
* `resolved_by_fix` — a task filed for the incident (`incident_tasks`, role `fix` or `triage`) whose PR shipped in a
  production anchor finished at or before `resolved_at`. Checked on the resolve (`on_resolved`) and again on every
  anchor (`on_deployment`), so the order the two facts arrive in does not matter.
Neither reads the Production QA switch: linkage is measurement (package docstring).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, delete, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.production_qa import (
    Deployment,
    DeploymentCommit,
    DeploymentPr,
    Incident,
    IncidentSuspect,
    IncidentTask,
    ProductionQaEdge,
)
from app.models.task_extras import TaskPrChanges
from app.production_qa.incidents import Recorded
from app.clock import utcnow

# How far before `first_seen` a deploy may have finished and still be a window suspect, and how many of the latest.
WINDOW = timedelta(hours=24)
WINDOW_ANCHORS = 5
# The words in order; `score` is only this order, so the top suspect is the most confident one.
_SCORES = {"high": 3.0, "medium": 2.0, "low": 1.0}
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
# A `release` must also carry a hex letter: an all-digit one (`2024110`) is a version, not a commit.
_HEX_LETTER = re.compile(r"[a-f]")
_LEADING = re.compile(r"^(?:\./|/)+")
# The conflict target must spell the unique index's `coalesce(…, 0)` with a literal 0: a bound parameter stops
# matching the index once the driver reuses a prepared (generic) plan.
_ZERO = literal_column("0")
# The incident-task roles whose deploy can resolve an incident (a revert is the origin undone, not a fix).
FIX_ROLES = ("fix", "triage")


@dataclass(frozen=True)
class Candidate:
    repo_path: str
    pr_number: int
    task_id: Optional[int]
    deployment_id: int
    basis: str
    confidence: str


_now = utcnow  # bound per module, so a test freezes this clock without the others'


# ── Bases ───────────────────────────────────────────────────────────────────────────────────────────

def _anchor_filter(project_id: int) -> Any:
    return and_(Deployment.project_id == project_id, Deployment.is_anchor.is_(True))


def _prs(session: Session, deployment_ids: Iterable[int]) -> list[DeploymentPr]:
    ids = list(deployment_ids)
    if not ids:
        return []
    return list(session.scalars(
        select(DeploymentPr).where(DeploymentPr.deployment_id.in_(ids))
        .order_by(DeploymentPr.deployment_id, DeploymentPr.repo_path, DeploymentPr.pr_number)
    ))


def _candidates(prs: Iterable[DeploymentPr], basis: str, confidence: str) -> list[Candidate]:
    return [Candidate(p.repo_path, p.pr_number, p.task_id, p.deployment_id, basis, confidence) for p in prs]


def _release_sha(incident: Incident) -> Optional[str]:
    """The commit the monitor named: `commit_sha`, else a SHA-shaped `release`; None for a version-shaped release."""
    commit = (incident.commit_sha or "").strip().lower()
    if _SHA.match(commit):
        return commit
    release = (incident.release or "").strip().lower()
    if _SHA.match(release) and _HEX_LETTER.search(release):
        return release
    return None


def _by_release(session: Session, incident: Incident) -> list[Candidate]:
    sha = _release_sha(incident)
    if sha is None:
        return []
    deployment_ids = list(session.scalars(
        select(DeploymentCommit.deployment_id)
        .join(Deployment, Deployment.id == DeploymentCommit.deployment_id)
        .where(_anchor_filter(incident.project_id), DeploymentCommit.sha.startswith(sha, autoescape=True))
        .distinct()
    ))
    prs = _prs(session, deployment_ids)
    merged = [p for p in prs if p.merge_sha.lower().startswith(sha)]
    return _candidates(merged or prs, "release", "high")


def _by_deployment(session: Session, incident: Incident) -> list[Candidate]:
    if incident.deployment_id is None:
        return []
    own = session.scalar(select(Deployment.id).where(
        Deployment.id == incident.deployment_id, Deployment.project_id == incident.project_id,
    ))
    return _candidates(_prs(session, [own]), "deployment", "high") if own is not None else []


def _norm(path: str) -> str:
    return _LEADING.sub("", path.replace("\\", "/").strip())


def _suffix_match(a: str, b: str) -> bool:
    """Either path is the other's tail on a `/` boundary: a frame's absolute path against a repo-relative file."""
    if not a or not b:
        return False
    short, long = sorted((a, b), key=len)
    return long == short or long.endswith("/" + short)


def frame_paths(stack_frames: Optional[list[dict[str, Any]]]) -> list[str]:
    """The normalised frame paths window linkage reads: the `in_app` frames when any frame is flagged, else all."""
    frames = [f for f in (stack_frames or []) if isinstance(f, dict) and isinstance(f.get("path"), str)]
    in_app = [f for f in frames if f.get("in_app") is True]
    return [p for p in (_norm(f["path"]) for f in (in_app or frames)) if p]


def _changed_files(session: Session, prs: list[DeploymentPr]) -> dict[tuple[int, int], list[str]]:
    """Each delivered PR's changed paths, keyed `(task_id, pr_number)`, in one read."""
    task_ids = {p.task_id for p in prs if p.task_id is not None}
    if not task_ids:
        return {}
    found: dict[tuple[int, int], list[str]] = {}
    for task_id, pr_number, files in session.execute(
        select(TaskPrChanges.task_id, TaskPrChanges.pr_number, TaskPrChanges.files)
        .where(TaskPrChanges.task_id.in_(task_ids))
    ):
        found[(task_id, pr_number)] = [_norm(f) for f in files or [] if isinstance(f, str)]
    return found


def _by_window(session: Session, incident: Incident) -> list[Candidate]:
    anchors = list(session.scalars(
        select(Deployment.id)
        .where(
            _anchor_filter(incident.project_id),
            Deployment.finished_at.is_not(None),
            Deployment.finished_at >= incident.first_seen - WINDOW,
            Deployment.finished_at <= incident.first_seen,
        )
        .order_by(Deployment.finished_at.desc(), Deployment.id.desc())
        .limit(WINDOW_ANCHORS)
    ))
    prs = _prs(session, anchors)
    paths = frame_paths(incident.stack_frames)
    files = _changed_files(session, prs) if paths else {}
    out: list[Candidate] = []
    for p in prs:
        changed = files.get((p.task_id, p.pr_number), []) if p.task_id is not None else []
        overlap = any(_suffix_match(f, path) for f in changed for path in paths)
        out.append(Candidate(p.repo_path, p.pr_number, p.task_id, p.deployment_id,
                             "window_paths" if overlap else "window", "medium" if overlap else "low"))
    return out


def suspects_for(session: Session, incident: Incident) -> list[Candidate]:
    """The incident's computed suspects, one per PR (the first basis that names a PR wins; within it, the most
    confident row for a PR two anchors share)."""
    found = _by_release(session, incident) or _by_deployment(session, incident) or _by_window(session, incident)
    best: dict[tuple[str, int], Candidate] = {}
    for c in found:
        key = (c.repo_path, c.pr_number)
        if key not in best or _SCORES[c.confidence] > _SCORES[best[key].confidence]:
            best[key] = c
    return list(best.values())


# ── Writes ──────────────────────────────────────────────────────────────────────────────────────────

def compute(session: Session, recorded: Recorded) -> None:
    """Store the suspects of an opened or reopened incident and bring its `incident_linked` edges in step."""
    incident = recorded.incident
    if incident is None or recorded.transition not in ("opened", "reopened"):
        return
    found = suspects_for(session, incident)
    for c in found:
        session.execute(
            pg_insert(IncidentSuspect)
            .values(org_id=incident.org_id, incident_id=incident.id, deployment_id=c.deployment_id,
                    task_id=c.task_id, repo_path=c.repo_path, pr_number=c.pr_number, basis=c.basis,
                    confidence=c.confidence, score=_SCORES[c.confidence])  # `state` defaults to `suggested`
            .on_conflict_do_update(
                index_elements=["incident_id", "repo_path", "pr_number"],
                set_={"deployment_id": c.deployment_id, "task_id": c.task_id, "basis": c.basis,
                      "confidence": c.confidence, "score": _SCORES[c.confidence]},
                where=IncidentSuspect.decided_by.is_(None) & (IncidentSuspect.state == "suggested"),
            )
        )
    keep = {(c.repo_path, c.pr_number) for c in found}
    stale = [
        s.id for s in session.scalars(select(IncidentSuspect).where(
            IncidentSuspect.incident_id == incident.id, IncidentSuspect.state == "suggested",
            IncidentSuspect.decided_by.is_(None),
        )) if (s.repo_path, s.pr_number) not in keep
    ]
    if stale:
        session.execute(delete(IncidentSuspect).where(IncidentSuspect.id.in_(stale)))
    session.flush()
    _sync_linked(session, incident, incident.last_transition_at)


def _edge(session: Session, incident: Incident, task_id: int, kind: str, at: datetime,
          deployment_id: Optional[int] = None) -> None:
    """Write one fact, or bring a revoked one back (pending again, so the applier re-applies it)."""
    session.execute(
        pg_insert(ProductionQaEdge)
        .values(org_id=incident.org_id, task_id=task_id, kind=kind, deployment_id=deployment_id,
                incident_id=incident.id, occurred_at=at)
        .on_conflict_do_update(
            index_elements=[ProductionQaEdge.task_id, ProductionQaEdge.kind,
                            func.coalesce(ProductionQaEdge.deployment_id, _ZERO), func.coalesce(ProductionQaEdge.incident_id, _ZERO)],
            set_={"revoked_at": None, "applied_at": None, "occurred_at": at},
            where=ProductionQaEdge.revoked_at.is_not(None),
        )
    )


def linked_task_ids(session: Session, incident_id: int) -> set[int]:
    """The origin tasks an incident links: confirmed suspects, and suggested ones of `high` confidence."""
    return set(session.scalars(
        select(IncidentSuspect.task_id).where(
            IncidentSuspect.incident_id == incident_id,
            IncidentSuspect.task_id.is_not(None),
            (IncidentSuspect.state == "confirmed")
            | ((IncidentSuspect.state == "suggested") & (IncidentSuspect.confidence == "high")),
        )
    ))


def _sync_linked(session: Session, incident: Incident, at: datetime) -> None:
    want = linked_task_ids(session, incident.id)
    for task_id in sorted(want):
        _edge(session, incident, task_id, "incident_linked", at)
    session.execute(
        update(ProductionQaEdge)
        .where(
            ProductionQaEdge.incident_id == incident.id, ProductionQaEdge.kind == "incident_linked",
            ProductionQaEdge.revoked_at.is_(None),
            ProductionQaEdge.task_id.not_in(want) if want else ProductionQaEdge.task_id.is_not(None),
        )
        .values(revoked_at=at)
    )
    session.flush()


class SuspectError(ValueError):
    """A correction the incident cannot take (the route answers it 422/409)."""


def correct(session: Session, incident: Incident, suspect: IncidentSuspect, state: str, actor_id: UUID) -> None:
    """An operator's decision on one suspect: `confirmed` or `dismissed` (or back to `suggested`), stamped with who
    and when; the incident's `incident_linked` edges follow."""
    if state not in ("confirmed", "dismissed", "suggested"):
        raise SuspectError("state must be one of: confirmed, dismissed, suggested")
    if state == "suggested" and suspect.basis == "operator":
        raise SuspectError("a suspect an operator added is dismissed, not undone")
    now = _now()
    suspect.state = state
    # Undo hands the row back to the computation: no decider, so the next recompute refreshes or drops it.
    suspect.decided_by = None if state == "suggested" else actor_id
    suspect.decided_at = None if state == "suggested" else now
    session.flush()
    _sync_linked(session, incident, now)
    if suspect.task_id is not None:
        # The ledger's link carries whether an operator confirmed it: a decision on an applied edge applies it anew.
        session.execute(
            update(ProductionQaEdge)
            .where(ProductionQaEdge.incident_id == incident.id, ProductionQaEdge.task_id == suspect.task_id,
                   ProductionQaEdge.kind == "incident_linked", ProductionQaEdge.revoked_at.is_(None),
                   ProductionQaEdge.applied_at.is_not(None))
            .values(applied_at=None)
        )
        session.flush()


def add(session: Session, incident: Incident, repo_path: str, pr_number: int, actor_id: UUID) -> IncidentSuspect:
    """An operator names a suspect PR: `operator` basis, `high`, `confirmed`. Its task and deploy are the latest
    anchor of the incident's project that shipped it, when one did. An existing row for the PR is confirmed in place."""
    repo_path = repo_path.strip()
    if not repo_path or pr_number < 1:
        raise SuspectError("repo_path and a positive pr_number are required")
    shipped = session.execute(
        select(DeploymentPr.deployment_id, DeploymentPr.task_id)
        .join(Deployment, Deployment.id == DeploymentPr.deployment_id)
        .where(_anchor_filter(incident.project_id), DeploymentPr.repo_path == repo_path,
               DeploymentPr.pr_number == pr_number)
        .order_by(Deployment.finished_at.desc().nulls_last(), Deployment.id.desc())
        .limit(1)
    ).first()
    deployment_id, task_id = shipped if shipped is not None else (None, None)
    now = _now()
    suspect = session.scalar(select(IncidentSuspect).where(
        IncidentSuspect.incident_id == incident.id, IncidentSuspect.repo_path == repo_path,
        IncidentSuspect.pr_number == pr_number,
    ))
    if suspect is None:
        suspect = IncidentSuspect(org_id=incident.org_id, incident_id=incident.id, repo_path=repo_path,
                                  pr_number=pr_number)
        session.add(suspect)
    suspect.deployment_id = deployment_id if deployment_id is not None else suspect.deployment_id
    suspect.task_id = task_id if task_id is not None else suspect.task_id
    suspect.basis, suspect.confidence, suspect.score = "operator", "high", _SCORES["high"]
    suspect.state, suspect.decided_by, suspect.decided_at = "confirmed", actor_id, now
    session.flush()
    _sync_linked(session, incident, now)
    return suspect


# ── The fix loop ────────────────────────────────────────────────────────────────────────────────────

def _fixes(session: Session, incident: Incident, deployment_id: Optional[int] = None) -> list[tuple[int, int]]:
    """`(task_id, deployment_id)` for each fix/triage task of the incident whose PR shipped in a production anchor
    finished at or before the resolve — the earliest such anchor per task; only `deployment_id` when given."""
    if incident.state != "resolved" or incident.resolved_at is None:
        return []
    stmt = (
        select(IncidentTask.task_id, Deployment.id)
        .join(DeploymentPr, DeploymentPr.task_id == IncidentTask.task_id)
        .join(Deployment, Deployment.id == DeploymentPr.deployment_id)
        .where(
            IncidentTask.incident_id == incident.id, IncidentTask.role.in_(FIX_ROLES),
            IncidentTask.task_id.is_not(None),
            _anchor_filter(incident.project_id),
            Deployment.finished_at.is_not(None), Deployment.finished_at <= incident.resolved_at,
        )
        .order_by(IncidentTask.task_id, Deployment.finished_at, Deployment.id)
    )
    if deployment_id is not None:
        stmt = stmt.where(Deployment.id == deployment_id)
    first: dict[int, int] = {}
    for task_id, dep_id in session.execute(stmt):
        first.setdefault(task_id, dep_id)
    return sorted(first.items())


def _write_fixes(session: Session, incident: Incident, fixes: list[tuple[int, int]]) -> None:
    for task_id, deployment_id in fixes:
        exists = session.scalar(select(ProductionQaEdge.id).where(
            ProductionQaEdge.task_id == task_id, ProductionQaEdge.kind == "resolved_by_fix",
            ProductionQaEdge.incident_id == incident.id,
        ).limit(1))
        if exists is None and incident.resolved_at is not None:
            _edge(session, incident, task_id, "resolved_by_fix", incident.resolved_at, deployment_id)
    if fixes:
        session.flush()


def on_resolved(session: Session, recorded: Recorded) -> None:
    """Write `resolved_by_fix` for each fix task deployed before the resolve."""
    incident = recorded.incident
    if incident is None or recorded.transition != "resolved":
        return
    _write_fixes(session, incident, _fixes(session, incident))


def on_deployment(session: Session, deployment: Deployment) -> None:
    """Re-check the project's resolved incidents whose fix this anchor shipped (a deploy recorded after the resolve
    it preceded). Idempotent."""
    if not deployment.is_anchor or deployment.finished_at is None:
        return
    incidents = session.scalars(
        select(Incident)
        .join(IncidentTask, IncidentTask.incident_id == Incident.id)
        .join(DeploymentPr, DeploymentPr.task_id == IncidentTask.task_id)
        .where(
            DeploymentPr.deployment_id == deployment.id, IncidentTask.role.in_(FIX_ROLES),
            Incident.project_id == deployment.project_id, Incident.state == "resolved",
            Incident.resolved_at >= deployment.finished_at,
        )
        .distinct()
    ).all()
    for incident in incidents:
        _write_fixes(session, incident, _fixes(session, incident, deployment.id))
