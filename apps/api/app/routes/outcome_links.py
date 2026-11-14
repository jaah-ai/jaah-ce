"""The outcome ledger's console writes (Architecture §9.2): defect-link decisions, Scan now, the scan settings.
The ledger's reads (profiles, Compare, Leaderboard, a task's rows) are `routes/outcomes.py`.

Every route is an Org Owner/Admin's (`@org_admin`, an operator fleet-wide): a link decision changes how an agent's
work is judged, and Scan now spends a runner. A tenant also needs the Agent performance plan switch
(`plan_terms.tenant_switch_required`, 403 while off). Rules live in `app/outcome_ledger/` (`writer.decide_link`,
`writer.add_caused_by`, `scan.file_scan`, `scan.status`, `settings`); this module is the HTTP shell.

* `POST /api/outcome-links/{link_id}/confirm` · `/dismiss` — the link and its origin row's new state.
* `POST /api/tasks/{task_id}/outcome-links` — "caused by": `task_id` fixes a defect the origin (`origin_task_id`, or
  the dispatch that merged `pr_url`) caused; created `confirmed` at 1.0.
* `GET`/`POST /api/projects/{project_id}/outcome-scan` — the scan's state; Scan now files the hidden task at once.
* `GET`/`PATCH /api/orgs/{org_id}/outcome-scan-interval` — the org's own interval override.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jaah_repos, plan_terms
from app.actors import stamp_actor
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin
from app.authz.principal import AsyncCaller, Principal, current_caller, current_principal_async
from app.db_async import DbDep, on_session
from app.entity_shape import StrictModel, administered_org_or_404, get_or_404, or_404, row_by_uuid_or_404
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.outcomes import OutcomeLink, TaskOutcome
from app.models.tasks import Task
from app.outcome_ledger import scan, settings, states, writer
from app.project_identity import project_or_404

router = APIRouter(
    tags=["outcome-links"], dependencies=[Depends(plan_terms.tenant_switch_required(plan_terms.AGENT_PERFORMANCE))],
)

_NO_LINK = "unknown outcome link"
_NO_TASK = "unknown task"


class LinkOut(BaseModel):
    id: UUID
    origin_outcome_id: UUID
    kind: str
    evidence_kind: str
    evidence_ref: str
    evidence_title: Optional[str]
    confidence: Decimal
    defect_state: str
    state: str
    decided_by_id: Optional[UUID]
    decided_at: Optional[datetime]
    created_by_id: Optional[UUID]


class LinkDecisionOut(BaseModel):
    link: LinkOut
    #: The origin row's outcome state after the decision.
    origin_state: Optional[str]


def _decision(session: Session, link: OutcomeLink, origin_state: Optional[str]) -> LinkDecisionOut:
    session.flush()
    task_id = _origin_task_id(session, link)
    if task_id is not None:
        emit_console_event(session, entity="task", id=task_id, action="outcome_judged")
    session.commit()
    session.refresh(link)
    return LinkDecisionOut(link=LinkOut.model_validate(link, from_attributes=True), origin_state=origin_state)


def _origin_task_id(session: Session, link: OutcomeLink) -> Optional[int]:
    return session.scalar(select(TaskOutcome.task_id).where(TaskOutcome.id == link.origin_outcome_id))


def _decide(session: Session, link_id: str, state: str, user: AdminUser) -> LinkDecisionOut:
    link = row_by_uuid_or_404(session, OutcomeLink, link_id, detail=_NO_LINK)
    origin_state = writer.decide_link(session, link, state=state, actor_id=user.id)
    return _decision(session, link, origin_state)


@router.post("/api/outcome-links/{link_id}/confirm", response_model=LinkDecisionOut)
@org_admin(Tenancy.CALLER_PLANE)
async def confirm_outcome_link(
    link_id: str, db: DbDep, user: AdminUser = Depends(require_user),
) -> LinkDecisionOut:
    """Confirm a defect link: it counts (confidence 1.0) and its origin moves to the link's defect state."""
    return await db.run_sync(_decide, link_id, "confirmed", user)


@router.post("/api/outcome-links/{link_id}/dismiss", response_model=LinkDecisionOut)
@org_admin(Tenancy.CALLER_PLANE)
async def dismiss_outcome_link(
    link_id: str, db: DbDep, user: AdminUser = Depends(require_user),
) -> LinkDecisionOut:
    """Dismiss a defect link: it never counts again (a detector re-run never re-proposes it), and its origin returns
    to the state its remaining links leave — `merged`, or `survived` once its window is over."""
    return await db.run_sync(_decide, link_id, "dismissed", user)


class CausedByIn(StrictModel):
    #: The task whose merged work caused the defect; or the PR that merged it.
    origin_task_id: Optional[int] = None
    pr_url: Optional[str] = Field(default=None, max_length=500)


def _origin_row(session: Session, body: CausedByIn) -> TaskOutcome:
    """The merged dispatch the operator names: the origin task's newest landed row, or the row that merged
    `pr_url`. 404 when none is landed — a link judges only merged work."""
    query = select(TaskOutcome).where(TaskOutcome.excluded.is_(False),
                                      TaskOutcome.outcome_state.in_(states.LANDED_STATES))
    if body.origin_task_id is not None:
        get_or_404(session, Task, body.origin_task_id, detail=_NO_TASK)
        query = query.where(TaskOutcome.task_id == body.origin_task_id)
    else:
        query = query.where(TaskOutcome.pr_url == (body.pr_url or "").strip())
    return or_404(session.scalars(query.order_by(TaskOutcome.dispatch_seq.desc()).limit(1)).first(),
                  detail="no merged work to link: the origin has no merged dispatch")


@router.post("/api/tasks/{task_id}/outcome-links", response_model=LinkDecisionOut)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def add_caused_by_link(
    session: Session,
    task_id: int,
    body: CausedByIn,
    user: AdminUser = Depends(require_user),
) -> LinkDecisionOut:
    """"Caused by": this task fixes a defect the origin's merged work caused. Confirmed at once (1.0)."""
    if (body.origin_task_id is None) == (not (body.pr_url or "").strip()):
        raise HTTPException(status_code=422, detail="name the origin: exactly one of origin_task_id and pr_url")
    if body.origin_task_id == task_id:
        raise HTTPException(status_code=422, detail="a task cannot have caused its own defect")
    evidence = get_or_404(session, Task, task_id, detail=_NO_TASK)
    origin = _origin_row(session, body)
    if origin.project_id != evidence.project_id:
        raise HTTPException(status_code=422, detail="the origin's merged work is in another project")
    link = writer.add_caused_by(session, origin, evidence_task=evidence, actor_id=user.id)
    session.flush()
    return _decision(session, link, session.get(TaskOutcome, origin.id).outcome_state)  # type: ignore[union-attr]


class ScanStatusOut(BaseModel):
    last_filed_at: Optional[str]
    cursor_sha: Optional[str]
    cursor_at: Optional[str]
    interval_minutes: int
    interval_source: str
    git_person: bool
    open_task_id: Optional[int]
    stale: bool


class ScanNowOut(BaseModel):
    filed: bool
    task_id: Optional[int]
    #: Why nothing was filed (`open`, `no_git_person`, `no_repository`), worded; NULL when filed.
    reason: Optional[str]
    status: ScanStatusOut


@router.get("/api/projects/{project_id}/outcome-scan", response_model=ScanStatusOut)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def get_outcome_scan(session: Session, project_id: int) -> ScanStatusOut:
    """The project's outcome scan: last filed, cursor, interval and its source, git person, the open scan."""
    return ScanStatusOut(**scan.status(session, project_or_404(session, project_id)))


@router.post("/api/projects/{project_id}/outcome-scan", response_model=ScanNowOut)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def scan_now(session: Session, project_id: int, caller: AsyncCaller = Depends(current_caller)) -> ScanNowOut:
    """Scan now: file the project's hidden `outcome_scan` task at once, due or not (one open per project)."""
    project = project_or_404(session, project_id)
    jaah_repos.refuse_member_edit(session, project_id, caller.actor, caller.principal)
    task, reason = scan.file_scan(session, project)
    session.commit()
    return ScanNowOut(
        filed=not reason, task_id=task.id if task is not None else None,
        reason=scan.REASONS.get(reason) if reason else None, status=ScanStatusOut(**scan.status(session, project)),
    )


class ScanIntervalOut(BaseModel):
    #: The org's own override (NULL = the operator default).
    own: Optional[int]
    #: The operator default.
    global_: int = Field(serialization_alias="global")
    effective: int
    minimum: int
    maximum: int


class ScanIntervalIn(StrictModel):
    own: Optional[int] = None


def _interval_out(own: Optional[int]) -> ScanIntervalOut:
    default = settings.global_interval()
    return ScanIntervalOut(own=own, global_=default, effective=own if own is not None else default,
                           minimum=settings.MINIMUM, maximum=settings.MAXIMUM)


@router.get("/api/orgs/{org_id}/outcome-scan-interval", response_model=ScanIntervalOut, response_model_by_alias=True)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def get_outcome_scan_interval(
    session: Session,
    org_id: str,
    principal: Optional[Principal] = Depends(current_principal_async),
) -> ScanIntervalOut:
    """The org's outcome scan interval: its own override, the operator default and the one in force."""
    return _interval_out(administered_org_or_404(session, org_id, principal).outcome_scan_interval_minutes)


@router.patch("/api/orgs/{org_id}/outcome-scan-interval", response_model=ScanIntervalOut,
              response_model_by_alias=True)
@org_admin(Tenancy.CALLER_PLANE)
@on_session
def patch_outcome_scan_interval(
    session: Session,
    org_id: str,
    body: ScanIntervalIn,
    caller: AsyncCaller = Depends(current_caller),
) -> ScanIntervalOut:
    """Set or clear the org's own interval (NULL = the operator default; 15..1440 minutes)."""
    row = administered_org_or_404(session, org_id, caller.principal)
    problem = settings.check(body.own)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if row.outcome_scan_interval_minutes != body.own:
        row.outcome_scan_interval_minutes = body.own
        stamp_actor(row, caller.actor)
        session.flush()
        emit_console_event(session, entity="org", id=row.id, action="upserted", commit=True)
    return _interval_out(row.outcome_scan_interval_minutes)
