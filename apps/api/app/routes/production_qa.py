"""Production QA reads: Deployments and Incidents, list and detail (rules: `app/production_qa/`); and Connect deploys
(GitHub, GitLab and Azure DevOps: `connect_deploys`, open to `projects:edit` like the Production QA tab that offers it;
Bitbucket: `connect_bitbucket_deploys`, org Admin), the record-only triggers deploy ingestion listens on.

Every read is limited to the projects the caller holds `projects:view` on (`scoped_project_select`), so a row of a
project the caller cannot see is absent from a list and a 404 by id. Every route here is behind the Production QA
plan switch for a tenant (`plan_terms.tenant_switch_required`, 403 while off; the operator passes), except Connect
deploys (`deploys_router`), open on every plan: the record is collected whatever the switch says, and a release waits
for its deploy only once deploys are connected, but only an org whose plan has the switch sees the rest.

Lists are keyset-paged newest first on `id` (`app/keyset.py`): `{items, next_cursor}`, `next_cursor` NULL on the
last page.

The one write: correcting an incident's suspects (`PATCH`/`POST …/suspects`, rules in `app/production_qa/linkage.py`)
needs `projects:edit` on the incident's own project — 404 when the caller cannot view the incident, 403 when they
can view but not edit it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app import jaah_repos, keyset, plan_terms
from app.actors import actor_names
from app.auth import require_user
from app.authz.catalog import PROJECTS_VIEW_KEY, Tenancy, permission_key
from app.authz.declarations import feature, org_admin
from app.authz.object_access import require_project_key
from app.releases import access as release_access
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.authz.visibility import has_project_key, scoped_project_select
from app.db_async import on_session
from app.entity_shape import MAX_NAME, PG_BIGINT_MAX, from_row, or_404, refused_as
from app.models.automations import TaskTrigger
from app.models.identity import AdminUser
from app.models.orgs import in_live_org
from app.models.production_qa import (
    Deployment,
    DeploymentCommit,
    DeploymentEnvironment,
    DeploymentMetric,
    DeploymentPr,
    Incident,
    IncidentEvent,
    IncidentSuspect,
    IncidentTask,
    ProductionQaEdge,
)
from app.models.projects import Project
from app.production_qa import events as production_qa_events
from app.production_qa import connect as deploy_connect, github_hooks, linkage, probes
from app.project_identity import project_or_404
from app.production_qa.vocabulary import DEPLOYMENT_STATUSES, INCIDENT_STATES, INCIDENT_TYPES, SEVERITIES
from app.triggers import SOURCE_BITBUCKET, SOURCE_GITHUB

router = APIRouter(tags=["production-qa"], dependencies=[Depends(plan_terms.tenant_switch_required(plan_terms.PRODUCTION_QA))])
#: Connect deploys, on every plan: a release waits for its deploy only once deploys are recorded (`releases/runs.py::
#: deploy_tracked`), and recording is measurement, never behind the Production QA switch.
deploys_router = APIRouter(tags=["production-qa"])

_LIMIT_DEFAULT = 50
_LIMIT_MAX = 200
_PROJECTS_EDIT_KEY = permission_key("projects", "edit")
_CURSOR_DETAIL = "'cursor' is not one this listing issued — send back a next_cursor unchanged, or leave it out"


# ── Shapes ──────────────────────────────────────────────────────────────────────────────────────────

class ProjectRef(BaseModel):
    id: int
    name: str


class DeploymentOut(BaseModel):
    id: int
    project: ProjectRef
    repo_path: str
    forge: str
    workflow_path: str
    workflow_name: Optional[str]
    run_id: int
    run_attempt: int
    run_event: Optional[str]
    run_url: Optional[str]
    head_branch: Optional[str]
    head_sha: str
    environment: str
    is_production: bool
    shipped: bool
    is_anchor: bool
    status: str
    conclusion_raw: Optional[str]
    failing_job: Optional[str]
    failing_step: Optional[str]
    range_from_sha: Optional[str]
    range_status: Optional[str]
    range_note: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    first_received_at: datetime
    last_received_at: datetime
    regression_due_at: Optional[datetime]
    regression_status: Optional[str]
    regression_note: Optional[str]
    # The PRs this deploy shipped, by number (the detail's `prs` carries each one's repository and task).
    pr_numbers: list[int]


class EnvironmentOut(BaseModel):
    environment: str
    job_name: Optional[str]
    state: str
    received_at: datetime


class PrOut(BaseModel):
    repo_path: str
    pr_number: int
    task_id: Optional[int]
    merge_sha: str
    source: str


class MetricOut(BaseModel):
    id: int
    metric: str
    trigger_id: Optional[UUID]
    baseline: Optional[float]
    post: Optional[float]
    delta_abs: Optional[float]
    delta_pct: Optional[float]
    events_post: Optional[int]
    window_min: int
    breached: bool
    note: Optional[str]
    measured_at: datetime


class EdgeOut(BaseModel):
    id: int
    task_id: int
    kind: str
    deployment_id: Optional[int]
    incident_id: Optional[int]
    occurred_at: datetime
    revoked_at: Optional[datetime]
    applied_at: Optional[datetime]


class ProbeOut(BaseModel):
    """One native probe's standing on this deploy: its `health_breach` incident (open = failing, resolved =
    passing again) and the facts the probe reported."""

    incident_id: int
    title: str
    state: str
    environment: Optional[str]
    last_seen: datetime
    facts: dict[str, Any]


class ProbeRunOut(BaseModel):
    """The `deploy_probe` run filed for this deploy (`production_qa/probes.py`): its task, state and report time."""

    task_id: int
    state: str
    reported_at: Optional[str]
    # Why the run probed nothing (or moved no incident): `production_qa_off`, `project_not_opted_in`, `superseded`.
    skipped: Optional[str] = None


class ProbeResultOut(BaseModel):
    """One probe's result in that run — passes too (`probes.ingest`); a failure names its incident."""

    probe_id: str
    kind: str
    name: str
    ok: bool
    status: Optional[int] = None
    exit_code: Optional[int] = None
    duration_s: Optional[float] = None
    output_tail: str = ""
    incident_id: Optional[int] = None


class DeploymentDetailOut(DeploymentOut):
    environments: list[EnvironmentOut]
    commits: list[str]
    prs: list[PrOut]
    metrics: list[MetricOut]
    edges: list[EdgeOut]
    probes: list[ProbeOut]
    probe_run: Optional[ProbeRunOut] = None
    probe_results: list[ProbeResultOut] = []


class DeploymentPageOut(BaseModel):
    items: list[DeploymentOut]
    next_cursor: Optional[str]


class TopSuspectOut(BaseModel):
    """An incident's leading suspect: the highest-scored one an operator has not dismissed."""

    repo_path: str
    pr_number: Optional[int]
    task_id: Optional[int]
    confidence: str
    state: str


class IncidentOut(BaseModel):
    id: int
    project: ProjectRef
    type: str
    source_kind: str
    trigger_id: Optional[UUID]
    fingerprint: str
    title: str
    severity: str
    environment: Optional[str]
    state: str
    first_seen: datetime
    last_seen: datetime
    last_transition_at: datetime
    count: int
    count_mode: str
    release: Optional[str]
    commit_sha: Optional[str]
    vendor_url: Optional[str]
    vendor_ref: Optional[str]
    deployment_id: Optional[int]
    resolved_at: Optional[datetime]
    created_at: datetime
    top_suspect: Optional[TopSuspectOut]


class SuspectOut(BaseModel):
    id: int
    deployment_id: Optional[int]
    task_id: Optional[int]
    repo_path: str
    pr_number: Optional[int]
    basis: str
    confidence: str
    score: float
    state: str
    decided_by: Optional[UUID]
    # Who decided, as the console names a person; NULL for a computed suggestion.
    decided_by_name: Optional[str] = None
    decided_at: Optional[datetime]
    created_at: datetime


class IncidentEventOut(BaseModel):
    id: int
    kind: str
    at: datetime
    delivery_id: Optional[int]
    actor_id: Optional[UUID]
    note: Optional[str]


class IncidentTaskOut(BaseModel):
    task_id: Optional[int]
    role: str
    delivery_id: Optional[int]
    claimed_at: datetime
    released_at: Optional[datetime]


class StackFrameOut(BaseModel):
    """The pinned frame shape; any other key a writer stored is dropped on the way out."""

    path: Optional[str] = None
    function: Optional[str] = None
    lineno: Optional[int] = None
    in_app: Optional[bool] = None


class IncidentDetailOut(IncidentOut):
    stack_frames: Optional[list[StackFrameOut]]
    facts: dict[str, Any]
    suspects: list[SuspectOut]
    events: list[IncidentEventOut]
    tasks: list[IncidentTaskOut]
    edges: list[EdgeOut]
    # The incident's deployment's measurements and environments; empty when it names no deployment.
    metrics: list[MetricOut]
    environments: list[EnvironmentOut]
    # Whether the caller may correct the suspects (`projects:edit` on the incident's project).
    can_edit: bool
    # Why no revert was filed where the project's policy asks for one (`events.revert_note`), else None.
    revert_note: Optional[str] = None


class SuspectPatchIn(BaseModel):
    state: str = Field(description="confirmed, dismissed, or suggested (undo a decision)")


class SuspectAddIn(BaseModel):
    repo_path: str = Field(min_length=1, max_length=512)
    pr_number: int = Field(ge=1)


class IncidentPageOut(BaseModel):
    items: list[IncidentOut]
    next_cursor: Optional[str]


# ── Scope and paging ────────────────────────────────────────────────────────────────────────────────

def _visible_projects(principal: Principal | None) -> Select:
    """The ids of the live-org projects `principal` holds `projects:view` on."""
    return (
        scoped_project_select(principal, key=PROJECTS_VIEW_KEY)
        .where(in_live_org(Project.org_id))
        .with_only_columns(Project.id)
    )


def _page(session: Session, stmt: Select, id_column: Any, cursor: Optional[str], limit: int) -> tuple[list, Optional[str]]:
    """One newest-first page of `stmt` (rows of `(entity, project_name)`) and the cursor to the next. The cursor's
    shape names the table, so one list's cursor is refused by the other."""
    shape = [f"{id_column.table.name}.id"]
    if cursor:
        (after,) = keyset.decode_cursor(
            cursor, shape, is_stamp=lambda _name: False, int_range=(1, PG_BIGINT_MAX), status=422,
            detail=_CURSOR_DETAIL,
        )
        stmt = stmt.where(id_column < after)
    rows, more = keyset.page(session, stmt.order_by(id_column.desc()), limit, scalars=False)
    return rows, keyset.encode_cursor(shape, [rows[-1][0].id]) if more else None


def _one_of(name: str, value: Optional[str], allowed: tuple[str, ...]) -> Optional[str]:
    """`value` when it is one of `allowed` (or absent); a 422 naming the choices otherwise."""
    if value is not None and value not in allowed:
        raise HTTPException(status_code=422, detail=f"'{name}' must be one of: {', '.join(allowed)}")
    return value


def _pr_numbers(session: Session, deployment_ids: list[int]) -> dict[int, list[int]]:
    """Each deploy's shipped PR numbers, ascending, in one read."""
    found: dict[int, list[int]] = {}
    if deployment_ids:
        for deployment_id, number in session.execute(
            select(DeploymentPr.deployment_id, DeploymentPr.pr_number)
            .where(DeploymentPr.deployment_id.in_(deployment_ids))
            .order_by(DeploymentPr.deployment_id, DeploymentPr.pr_number)
        ):
            found.setdefault(deployment_id, []).append(number)
    return found


def _top_suspects(session: Session, incident_ids: list[int]) -> dict[int, TopSuspectOut]:
    """Each incident's leading suspect (highest score, then oldest; dismissed ones never lead), in one read."""
    if not incident_ids:
        return {}
    rows = session.scalars(
        select(IncidentSuspect)
        .where(IncidentSuspect.incident_id.in_(incident_ids), IncidentSuspect.state != "dismissed")
        .order_by(IncidentSuspect.incident_id, IncidentSuspect.score.desc(), IncidentSuspect.id)
        .distinct(IncidentSuspect.incident_id)
    )
    return {r.incident_id: from_row(TopSuspectOut, r) for r in rows}


def _edges(session: Session, column: Any, value: int) -> list[EdgeOut]:
    rows = session.scalars(select(ProductionQaEdge).where(column == value).order_by(ProductionQaEdge.id))
    return [from_row(EdgeOut, r) for r in rows]


def _metrics(session: Session, deployment_id: int) -> list[MetricOut]:
    rows = session.scalars(
        select(DeploymentMetric).where(DeploymentMetric.deployment_id == deployment_id).order_by(DeploymentMetric.id)
    )
    return [from_row(MetricOut, r) for r in rows]


def _environments(session: Session, deployment_id: int) -> list[EnvironmentOut]:
    rows = session.scalars(
        select(DeploymentEnvironment)
        .where(DeploymentEnvironment.deployment_id == deployment_id)
        .order_by(DeploymentEnvironment.received_at, DeploymentEnvironment.environment)
    )
    return [from_row(EnvironmentOut, r) for r in rows]


# ── Deployments ─────────────────────────────────────────────────────────────────────────────────────

@router.get("/api/deployments", response_model=DeploymentPageOut)
@feature("projects", "view", collection=True)
@on_session
def list_deployments(
    session: Session,
    project_id: Optional[int] = None,
    environment: Optional[str] = None,
    status: Optional[str] = Query(default=None),
    cursor: Optional[str] = None,
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    caller: AsyncCaller = Depends(current_caller),
):
    stmt = (
        select(Deployment, Project.name)
        .join(Project, Project.id == Deployment.project_id)
        .where(Deployment.project_id.in_(_visible_projects(caller.principal)))
    )
    if project_id is not None:
        stmt = stmt.where(Deployment.project_id == project_id)
    if environment is not None:
        stmt = stmt.where(Deployment.environment == environment)
    if _one_of("status", status or None, DEPLOYMENT_STATUSES) is not None:
        stmt = stmt.where(Deployment.status == status)
    else:
        # The default view hides runs that shipped no environment (`skipped` + the `unknown` sentinel); asking for
        # `status=skipped` still lists every one of them.
        stmt = stmt.where(~((Deployment.status == "skipped") & (Deployment.environment == "unknown")))
    rows, next_cursor = _page(session, stmt, Deployment.id, cursor, limit)
    numbers = _pr_numbers(session, [d.id for d, _name in rows])
    return DeploymentPageOut(
        items=[
            from_row(DeploymentOut, d, project=ProjectRef(id=d.project_id, name=name), pr_numbers=numbers.get(d.id, []))
            for d, name in rows
        ],
        next_cursor=next_cursor,
    )


# `collection=True` on the detail routes: there is no project in the path for an object check to read, so the
# handler scopes by `_visible_projects` itself, exactly as the lists do.
@router.get("/api/deployments/{deployment_id:int}", response_model=DeploymentDetailOut)
@feature("projects", "view", collection=True)
@on_session
def get_deployment(
    session: Session,
    deployment_id: int,
    caller: AsyncCaller = Depends(current_caller),
):
    """One deployment with its environments, commits, PRs, metrics and ledger edges; 404 outside the caller's
    projects."""
    row, project_name = or_404(session.execute(
        select(Deployment, Project.name)
        .join(Project, Project.id == Deployment.project_id)
        .where(Deployment.id == deployment_id, Deployment.project_id.in_(_visible_projects(caller.principal)))
    ).first(), detail="Release not found")
    commits = session.scalars(
        select(DeploymentCommit.sha).where(DeploymentCommit.deployment_id == row.id).order_by(DeploymentCommit.sha)
    ).all()
    prs = list(session.scalars(
        select(DeploymentPr).where(DeploymentPr.deployment_id == row.id)
        .order_by(DeploymentPr.repo_path, DeploymentPr.pr_number)
    ))
    return from_row(
        DeploymentDetailOut, row,
        project=ProjectRef(id=row.project_id, name=project_name),
        pr_numbers=sorted(p.pr_number for p in prs),
        environments=_environments(session, row.id),
        commits=list(commits),
        prs=[from_row(PrOut, p) for p in prs],
        metrics=_metrics(session, row.id),
        edges=_edges(session, ProductionQaEdge.deployment_id, row.id),
        probes=[
            from_row(ProbeOut, i, incident_id=i.id)
            for i in session.scalars(
                select(Incident)
                .where(Incident.deployment_id == row.id, Incident.project_id == row.project_id,
                       Incident.type == "health_breach")
                .order_by(Incident.title, Incident.id)
            )
        ],
        **_probe_run(session, row),
    )


def _probe_run(session: Session, row: Deployment) -> dict[str, Any]:
    """The deploy's probe run and its per-probe results (`production_qa/probes.py`), none when none was filed."""
    task = probes.run_of(session, row.id, row.project_id)
    if task is None:
        return {"probe_run": None, "probe_results": []}
    metrics = task.metrics or {}
    return {
        "probe_run": ProbeRunOut(task_id=task.id, state=task.state, reported_at=metrics.get(probes.REPORTED_KEY),
                                 skipped=metrics.get(probes.SKIPPED_KEY)),
        "probe_results": [ProbeResultOut(**r) for r in metrics.get(probes.RESULTS_KEY) or [] if isinstance(r, dict)],
    }


# ── Incidents ───────────────────────────────────────────────────────────────────────────────────────

@router.get("/api/incidents", response_model=IncidentPageOut)
@feature("projects", "view", collection=True)
@on_session
def list_incidents(
    session: Session,
    project_id: Optional[int] = None,
    severity: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    type: Optional[str] = Query(default=None),
    task_id: Optional[int] = Query(default=None, description="Only incidents this task was filed for"),
    cursor: Optional[str] = None,
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    caller: AsyncCaller = Depends(current_caller),
):
    stmt = (
        select(Incident, Project.name)
        .join(Project, Project.id == Incident.project_id)
        .where(Incident.project_id.in_(_visible_projects(caller.principal)))
    )
    if project_id is not None:
        stmt = stmt.where(Incident.project_id == project_id)
    if _one_of("severity", severity, SEVERITIES) is not None:
        stmt = stmt.where(Incident.severity == severity)
    if _one_of("state", state, INCIDENT_STATES) is not None:
        stmt = stmt.where(Incident.state == state)
    if _one_of("type", type, INCIDENT_TYPES) is not None:
        stmt = stmt.where(Incident.type == type)
    if task_id is not None:
        stmt = stmt.where(Incident.id.in_(select(IncidentTask.incident_id).where(IncidentTask.task_id == task_id)))
    rows, next_cursor = _page(session, stmt, Incident.id, cursor, limit)
    tops = _top_suspects(session, [i.id for i, _name in rows])
    return IncidentPageOut(
        items=[
            from_row(IncidentOut, i, project=ProjectRef(id=i.project_id, name=name), top_suspect=tops.get(i.id))
            for i, name in rows
        ],
        next_cursor=next_cursor,
    )


@router.get("/api/incidents/{incident_id:int}", response_model=IncidentDetailOut)
@feature("projects", "view", collection=True)
@on_session
def get_incident(
    session: Session,
    incident_id: int,
    caller: AsyncCaller = Depends(current_caller),
):
    """One incident with its suspects, history, tasks, ledger edges, and its deployment's metrics and
    environments; 404 outside the caller's projects."""
    return _incident_detail(session, caller.principal, *_visible_incident(session, caller.principal, incident_id))


class LaneStageOut(BaseModel):
    stage: str
    seconds: float
    outcome: str


class LaneRunOut(BaseModel):
    lane_task_id: Optional[int]
    tree_hash: Optional[str]
    recorded_at: datetime
    pr_numbers: list[int]
    stages: list[LaneStageOut]


class TaskLaneStagesOut(BaseModel):
    runs: list[LaneRunOut]


@router.get("/api/tasks/{task_id:int}/lane-stages", response_model=TaskLaneStagesOut)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
@on_session
def get_task_lane_stages(session: Session, task_id: int, _user: AdminUser = Depends(require_user)):
    """The merge-lane gate runs that covered this task's PRs, newest first, with each stage's seconds and outcome
    (`production_qa/lane_timings.py::for_task`)."""
    from app.production_qa import lane_timings
    from app.task_lookup import get_task_or_404

    return {"runs": lane_timings.for_task(session, get_task_or_404(task_id, session))}


def _visible_incident(session: Session, principal: Principal | None, incident_id: int) -> tuple[Incident, str]:
    row, project_name = or_404(session.execute(
        select(Incident, Project.name)
        .join(Project, Project.id == Incident.project_id)
        .where(Incident.id == incident_id, Incident.project_id.in_(_visible_projects(principal)))
    ).first(), detail="Incident not found")
    return row, project_name


def _incident_detail(session: Session, principal: Principal | None, row: Incident, project_name: str) -> IncidentDetailOut:
    suspects = list(session.scalars(
        select(IncidentSuspect).where(IncidentSuspect.incident_id == row.id)
        .order_by(IncidentSuspect.score.desc(), IncidentSuspect.id)
    ))
    top = next((from_row(TopSuspectOut, s) for s in suspects if s.state != "dismissed"), None)
    events = session.scalars(
        select(IncidentEvent).where(IncidentEvent.incident_id == row.id).order_by(IncidentEvent.id)
    )
    # The deploy's measurements only when it is the incident's own project's: the reference carries no key, so a
    # deploy of a project the caller cannot view is never read through it.
    deployment_id = session.scalar(select(Deployment.id).where(
        Deployment.id == row.deployment_id, Deployment.project_id == row.project_id
    )) if row.deployment_id is not None else None
    claims = session.scalars(
        select(IncidentTask).where(IncidentTask.incident_id == row.id).order_by(IncidentTask.claimed_at)
    )
    names = actor_names(session, [s.decided_by for s in suspects])
    return from_row(
        IncidentDetailOut, row,
        project=ProjectRef(id=row.project_id, name=project_name),
        top_suspect=top,
        stack_frames=[StackFrameOut(**f) for f in row.stack_frames if isinstance(f, dict)]
        if row.stack_frames is not None else None,
        facts=row.facts,
        suspects=[
            from_row(SuspectOut, s, decided_by_name=names.get(s.decided_by) if s.decided_by else None)
            for s in suspects
        ],
        events=[from_row(IncidentEventOut, e) for e in events],
        tasks=[from_row(IncidentTaskOut, t) for t in claims],
        edges=_edges(session, ProductionQaEdge.incident_id, row.id),
        metrics=_metrics(session, deployment_id) if deployment_id is not None else [],
        environments=_environments(session, deployment_id) if deployment_id is not None else [],
        can_edit=has_project_key(principal, _PROJECTS_EDIT_KEY, row.project_id),
        revert_note=production_qa_events.revert_note(session, row),
    )


# ── Suspect corrections ─────────────────────────────────────────────────────────────────────────────

def _editable_incident(session: Session, principal: Principal | None, incident_id: int) -> tuple[Incident, str]:
    """The incident, locked for the correction: 404 when the caller cannot view it, 403 when they cannot edit its
    project."""
    row, project_name = _visible_incident(session, principal, incident_id)
    require_project_key(principal, _PROJECTS_EDIT_KEY, row.project_id,
                        detail="Correcting suspects needs edit access to this project")
    session.execute(select(Incident.id).where(Incident.id == row.id).with_for_update())
    return row, project_name


# `collection=True`: the project is the incident's, not a path param, so the handler checks `projects:edit` on it
# itself (`_editable_incident`) after scoping the read exactly as the detail does.
@router.patch("/api/incidents/{incident_id:int}/suspects/{suspect_id:int}", response_model=IncidentDetailOut)
@feature("projects", "edit", collection=True)
@on_session
def correct_suspect(
    session: Session,
    incident_id: int,
    suspect_id: int,
    body: SuspectPatchIn,
    caller: AsyncCaller = Depends(current_caller),
):
    """Confirm, dismiss or un-decide one suspect; who and when are stored, and the incident's ledger edges follow.
    Answers the incident as the detail does."""
    row, project_name = _editable_incident(session, caller.principal, incident_id)
    suspect = or_404(session.scalar(select(IncidentSuspect).where(
        IncidentSuspect.id == suspect_id, IncidentSuspect.incident_id == row.id,
    )), detail="Suspect not found")
    with refused_as(422, linkage.SuspectError, chain=True):
        linkage.correct(session, row, suspect, body.state, caller.actor.id)
    session.commit()
    return _incident_detail(session, caller.principal, row, project_name)


@router.post("/api/incidents/{incident_id:int}/suspects", response_model=IncidentDetailOut, status_code=201)
@feature("projects", "edit", collection=True)
@on_session
def add_suspect(
    session: Session,
    incident_id: int,
    body: SuspectAddIn,
    caller: AsyncCaller = Depends(current_caller),
):
    """Name a suspect PR by hand: `operator` basis, `high`, confirmed (an existing row for the PR is confirmed in
    place). Answers the incident as the detail does."""
    row, project_name = _editable_incident(session, caller.principal, incident_id)
    with refused_as(422, linkage.SuspectError, chain=True):
        linkage.add(session, row, body.repo_path, body.pr_number, caller.actor.id)
    session.commit()
    return _incident_detail(session, caller.principal, row, project_name)


# ── Connect deploys (architecture §5.1 step 1; the writer: `app/production_qa/connect.py`) ─────────

class ConnectDeploysIn(BaseModel):
    deploy_workflows: list[str]


class ConnectBitbucketDeploysIn(BaseModel):
    production_branch: str
    # Build-status keys or names that deploy; empty: every build status on the production branch.
    statuses: list[str] = Field(default_factory=list)


class DeployTriggerOut(BaseModel):
    id: UUID
    name: str
    # `github`, `gitlab`, `azure_devops` or `bitbucket`: which forge's webhook form it fills.
    source_kind: str
    events: list[str]
    # The repository it listens to (`owner/repo`, a GitLab project path, an Azure DevOps `organization/project/repo`;
    # the project's primary at create); '' for any.
    repository: str
    ingress_url: Optional[str]
    paused: bool
    # A Bitbucket trigger's production branch and deploy statuses; null / empty on any other.
    production_branch: Optional[str] = None
    statuses: list[str] = Field(default_factory=list)
    # Answered once, by the connect that created the trigger; null on every read.
    secret: Optional[str] = None


class DeployTriggersOut(BaseModel):
    # The forge Connect deploys creates webhooks for (`github`, `gitlab`, `azure_devops`, `bitbucket`), or null: no
    # connected repository on any.
    forge: Optional[str] = None
    deploy_workflows: list[str]
    triggers: list[DeployTriggerOut]
    # Whether the caller may connect this forge (Bitbucket's webhook is the org Admin's).
    can_connect: bool = True
    # GitHub: a deploy trigger no installed hook carries yet (its secret's reveal still owed) — Connect is unfinished.
    setup_owed: bool = False


class ConnectAnswerOut(DeployTriggersOut):
    # GitHub: `connected` (the webhooks installed; no secret answered) or `needs_setup` (nothing could install them:
    # `reason`, and the App's install page); null on any other forge, whose answer carries each new secret once.
    state: Optional[str] = None
    reason: Optional[str] = None
    install_url: Optional[str] = None
    # The repository owner `install_url` installs on.
    owner: Optional[str] = None


class DeployWorkflowsOut(BaseModel):
    # The workflow files that deploy the Production branch, prefilled (`github_hooks.detect_workflows`).
    workflows: list[str]
    # Other workflows that may deploy it, offered but not prefilled.
    optional: list[str] = []
    # The repository publishes GitHub Pages from the branch and no workflow matched: connectable with none named.
    pages_branch_built: bool
    # False when the repository could not be read (no login reaches it, or GitHub failed).
    readable: bool


def _trigger_out(trigger: TaskTrigger, secret: Optional[str] = None) -> DeployTriggerOut:
    from app.routes.triggers import _url_for

    config = trigger.source_config or {}
    return DeployTriggerOut(id=trigger.id, name=trigger.name, source_kind=trigger.source_kind,
                            events=deploy_connect.events_of(trigger), repository=str(config.get("repository") or ""),
                            ingress_url=_url_for(trigger), paused=trigger.paused_at is not None,
                            production_branch=config.get("production_branch"),
                            statuses=list(config.get("statuses") or []), secret=secret)


def _connected_out(connected: deploy_connect.Connected) -> ConnectAnswerOut:
    """The connect's answer. A GitHub connect that kept its secrets sealed answers none of them, installed or not."""
    installed = connected.installed
    triggers = [_trigger_out(t, None if installed is not None else secret) for t, secret in connected.triggers]
    setup: dict[str, Any] = {}
    if installed is not None and installed.state == "installed":
        setup = {"state": "connected"}
    elif installed is not None:
        from app.git_reach import app_install_url

        setup = {"state": "needs_setup", "reason": installed.reason, "install_url": app_install_url(),
                 "owner": installed.owner_repo.split("/")[0] if installed.owner_repo else None}
    return ConnectAnswerOut(forge=connected.forge, deploy_workflows=connected.deploy_workflows, triggers=triggers,
                            **setup)


def _editable_project(session: Session, project_id: int, caller: AsyncCaller) -> Project:
    """The project, for a caller holding `projects:edit` on it (the Production QA tab's Connect, as before the release
    tab's own connect folded into this one) and not a member editing a Jaah-managed project."""
    require_project_key(caller.principal, release_access.PROJECTS_EDIT, project_id, detail=None)
    project = project_or_404(session, project_id)
    jaah_repos.refuse_member_edit(session, project_id, caller.actor, caller.principal)
    return project


@deploys_router.get("/api/projects/{project_id:int}/production-qa/deploy-triggers", response_model=DeployTriggersOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_deploy_triggers(session: Session, project_id: int,
                        caller: AsyncCaller = Depends(current_caller)) -> DeployTriggersOut:
    """The project's deploy workflows, the forge Connect deploys would use, and the record-only triggers that record
    its deploys (no secret)."""
    require_project_key(caller.principal, release_access.PROJECTS_EDIT, project_id, detail=None)
    project = project_or_404(session, project_id)
    triggers = deploy_connect.deploy_triggers(session, project.id)
    forge, _repository = deploy_connect.deploy_forge(session, project)
    can_connect = forge != SOURCE_BITBUCKET or bool(caller.principal and caller.principal.is_org_admin)
    setup_owed = forge == SOURCE_GITHUB and any(
        t.source_kind == SOURCE_GITHUB and t.secret_sealed is not None and t.secret_revealed_at is None
        for t in triggers.values())
    return DeployTriggersOut(forge=forge, deploy_workflows=list(project.deploy_workflows or []),
                             triggers=[_trigger_out(t) for t in triggers.values()], can_connect=can_connect,
                             setup_owed=setup_owed)


@deploys_router.get("/api/projects/{project_id:int}/production-qa/deploy-workflows", response_model=DeployWorkflowsOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_deploy_workflows(session: Session, project_id: int, response: Response,
                         caller: AsyncCaller = Depends(current_caller)) -> DeployWorkflowsOut:
    """GitHub: the workflows Connect prefills (`github_hooks.detect_workflows`); empty and unreadable elsewhere. Never
    an error: a repository nobody can read answers `readable: false`."""
    project = _editable_project(session, project_id, caller)
    response.headers["Cache-Control"] = "no-store"
    if github_hooks.github_repository(session, project) is None:
        return DeployWorkflowsOut(workflows=[], pages_branch_built=False, readable=False)
    found = github_hooks.detect_workflows(session, project, deploy_connect.production_branch_of(session, project),
                                          actor_id=caller.actor.id if caller.actor else None)
    return DeployWorkflowsOut(workflows=found.workflows, optional=found.optional,
                              pages_branch_built=found.pages_branch_built, readable=found.readable)


@deploys_router.post("/api/projects/{project_id:int}/production-qa/connect-deploys", response_model=ConnectAnswerOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def connect_deploys(session: Session, project_id: int, body: ConnectDeploysIn, response: Response,
                    caller: AsyncCaller = Depends(current_caller)) -> Any:
    """Add the deploy workflow paths (GitLab: the CI config path; Azure DevOps: pipeline or release names) to the
    project and create whichever of its forge's record-only triggers it lacks. GitHub: installs the webhooks itself
    (`github_hooks`) and answers `connected`, or `needs_setup` with no secret — the secrets stay sealed for
    `connect-deploys/manual`. Any other forge answers each new trigger's ingress URL and secret once. Not gated:
    deploys are measurement."""
    project = _editable_project(session, project_id, caller)
    forge, _repository = deploy_connect.deploy_forge(session, project)
    connected = deploy_connect.connect(session, project, caller.actor, workflows=body.deploy_workflows,
                                       reveal=forge != SOURCE_GITHUB)
    session.commit()
    out = _connected_out(connected)
    if connected.installed is None:
        response.headers["Cache-Control"] = "no-store"
        return out
    # A GitHub answer carries no `secret` key at all, installed or not: the secrets stay sealed.
    return JSONResponse(jsonable_encoder(out.model_dump(exclude={"triggers": {"__all__": {"secret"}}})),
                        headers={"Cache-Control": "no-store"})


@deploys_router.post("/api/projects/{project_id:int}/production-qa/connect-deploys/manual", response_model=ConnectAnswerOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def connect_deploys_manually(session: Session, project_id: int, body: ConnectDeploysIn, response: Response,
                             caller: AsyncCaller = Depends(current_caller)) -> Any:
    """The last resort when nothing could install the webhooks: answers each trigger's ingress URL and its secret while
    its one reveal is still owed, for the person to add on the forge. On GitHub it tries the install first and, when
    that works, answers `connected` with no secret: a live installed hook's secret is never revealed."""
    project = _editable_project(session, project_id, caller)
    forge, _repository = deploy_connect.deploy_forge(session, project)
    if forge == SOURCE_GITHUB:
        tried = deploy_connect.connect(session, project, caller.actor, workflows=body.deploy_workflows, reveal=False)
        if tried.installed is not None and tried.installed.state == "installed":
            session.commit()
            return JSONResponse(jsonable_encoder(_connected_out(tried).model_dump(
                exclude={"triggers": {"__all__": {"secret"}}})), headers={"Cache-Control": "no-store"})
    connected = deploy_connect.reveal_owed(session, deploy_connect.connect(
        session, project, caller.actor, workflows=body.deploy_workflows))
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return _connected_out(connected)


@deploys_router.post("/api/projects/{project_id:int}/production-qa/connect-bitbucket-deploys",
             response_model=DeployTriggersOut)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def connect_bitbucket_deploys(session: Session, project_id: int, body: ConnectBitbucketDeploysIn, response: Response,
                              caller: AsyncCaller = Depends(current_caller)) -> DeployTriggersOut:
    """Create the project's one record-only Bitbucket deploy trigger on its first connected Bitbucket repository,
    answering its ingress URL and secret once; a second connect updates its production branch and statuses and
    answers no secret. Not gated: deploys are measurement."""
    project = project_or_404(session, project_id)
    jaah_repos.refuse_member_edit(session, project_id, caller.actor, caller.principal)
    connected = deploy_connect.connect(session, project, caller.actor, forge=SOURCE_BITBUCKET,
                                       production_branch=body.production_branch, statuses=body.statuses)
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return _connected_out(connected)


# ── Incident automations (architecture §6.5) ────────────────────────────────────────────────────────

# The defaults "Create default automations" adds, owned by the admin who asks: one per incident route (§6.1), each
# filing a ready Task in this project. A coder, not the `triage` persona: that one has no runner charter and no
# automation may file it (`persona_registry.py`); the body asks to triage first (`templates.DEFAULT_BODY`), and the
# claim's role is `fix` (`events._role`). Keyed by kind and types, so a second click adds only what is missing.
DEFAULT_INCIDENT_AUTOMATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Release failed", "deploy_failed", ()),
    ("Monitor incidents", "incident", ("monitor",)),
    ("Health breach", "health_breach", ()),
    ("Regressions and slowdowns", "incident", ("lane_slowdown", "regression")),
)


# Open tasks one default automation may hold at once: each incident's own claim already allows it one task, so the
# cap only bounds how many distinct incidents run together (the trigger default of 1 would queue every second one).
DEFAULT_INCIDENT_CONCURRENCY = 10


class IncidentAutomationOut(BaseModel):
    id: UUID
    name: str
    source_kind: str
    status: str
    # Owned from another org: refused at every firing until a member takes ownership (`automation_service`).
    foreign_owner: bool = False


class IncidentAutomationsOut(BaseModel):
    items: list[IncidentAutomationOut]
    total: int


def _incident_triggers(session: Session, project_id: int) -> list[TaskTrigger]:
    from app.triggers import INCIDENT_SOURCE_KINDS

    return list(session.scalars(
        select(TaskTrigger).where(TaskTrigger.project_id == project_id,
                                  TaskTrigger.source_kind.in_(INCIDENT_SOURCE_KINDS),
                                  TaskTrigger.automation_id.is_not(None))
        .order_by(TaskTrigger.created_at, TaskTrigger.id)
    ))


def _incident_automations(session: Session, project_id: int) -> IncidentAutomationsOut:
    from app import automation_service
    from app.models.automations import Automation

    items = []
    for trigger in _incident_triggers(session, project_id):
        automation = session.scalar(select(Automation).where(Automation.id == trigger.automation_id))
        if automation is None:
            continue
        shown = automation_service.out(session, automation)
        items.append(IncidentAutomationOut(id=automation.id, name=automation.name, source_kind=trigger.source_kind,
                                           status=shown["status"], foreign_owner=shown["foreign_owner"]))
    return IncidentAutomationsOut(items=items, total=len(items))


@router.get("/api/projects/{project_id:int}/production-qa/automations", response_model=IncidentAutomationsOut)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def get_incident_automations(session: Session, project_id: int,
                             caller: AsyncCaller = Depends(current_caller)) -> IncidentAutomationsOut:
    """The automations that turn this project's incidents into tasks (the incident kinds, targeting it)."""
    project = project_or_404(session, project_id)
    return _incident_automations(session, project.id)


@router.post("/api/projects/{project_id:int}/production-qa/default-automations",
             response_model=IncidentAutomationsOut)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def create_default_automations(session: Session, project_id: int, background: BackgroundTasks,
                               caller: AsyncCaller = Depends(current_caller)) -> IncidentAutomationsOut:
    """Create whichever default incident automations the project lacks, owned by the caller; answers the project's
    incident automations. A caller from another org — an operator acting as this one included — is refused (409,
    naming the org to switch to): each
    automation runs as its owner, and an owner outside the project's org is refused at every firing. Ordinary automations: each is edited, paused or deleted like any other, and files nothing
    while Production QA is off for the org or the project (`production_qa/gate.py`)."""
    from app import automation_service
    from app.production_qa.templates import DEFAULT_BODY, DEFAULT_TITLE

    from sqlalchemy import func

    from app.production_qa.events import lock_key

    project = project_or_404(session, project_id)
    jaah_repos.refuse_member_edit(session, project_id, caller.actor, caller.principal)
    if automation_service.actor_outside_org(caller.actor, project.org_id):
        # Owned from another org, every one would be refused at its first incident: never mint a set that cannot fire.
        raise HTTPException(status_code=409,
                            detail=automation_service.switch_org_detail(session, project.org_id, "create these"))
    # Two clicks at once create the defaults once: the second waits, then sees the first's.
    session.execute(select(func.pg_advisory_xact_lock(lock_key("defaults", project.id))))
    have = {(t.source_kind, tuple(sorted((t.source_config or {}).get("types") or ())))
            for t in _incident_triggers(session, project.id)}
    for name, kind, types in DEFAULT_INCIDENT_AUTOMATIONS:
        if (kind, types) in have:
            continue
        # The project's name cut short with "…" when the whole name would pass MAX_NAME.
        room = MAX_NAME - len(f"Production QA/: {name}")
        project_name = project.name if len(project.name) <= room else project.name[:room - 1] + "…"
        automation_service.create(session, org_id=project.org_id, actor=caller.actor, principal=caller.principal,
                                  background=background, body={
            # Filed in one Production QA folder in the Automations tree (`lib/nameTree.ts`), not at its root.
            "name": f"Production QA/{project_name}: {name}",
            "description": "Files a task for each new production incident of this kind.",
            "when": {"kind": "event", "source_kind": kind,
                     "source_config": {"min_severity": "error", "types": list(types)},
                     "concurrency_cap": DEFAULT_INCIDENT_CONCURRENCY},
            "target": {"kind": "projects", "mode": "specific", "ids": [project.id]},
            "action": {"type": "task", "params": {"persona": "coder", "body": DEFAULT_BODY,
                                                  "title_template": DEFAULT_TITLE, "start": True}},
        })
    session.commit()
    return _incident_automations(session, project.id)
