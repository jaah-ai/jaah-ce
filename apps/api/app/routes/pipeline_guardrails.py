"""Settings › Pipeline rules: the org's three built-in pipeline guardrails (rules: `app/releases/guardrails.py`).

`GET /api/org/pipeline-guardrails` — any member who can see projects (an editor needs it to read why a save was
refused): each rule's mode, its G1 approvals, who changed it and when, and the projects that break it today — each
rule evaluated as if on, so an admin sees what turning it on would flag; only projects the caller may view are named.
`PUT /api/org/pipeline-guardrails/{rule}` — an Org Owner/Admin only (`org_admin_writer`): sets the mode and, for
`production_approval`, the approvals. A change touches no stored pipeline; it applies from each project's next save.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.actors import actor_names_for, stamp_actor
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, current_caller, org_admin_writer
from app.db_async import on_session
from app.entity_shape import entity_fields, or_404
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.releases import APPROVALS_MAX, GUARDRAIL_RULES, PipelineGuardrail
from app.releases import guardrails, stages as stages_mod
from app.routes.production_qa import _visible_projects

router = APIRouter(prefix="/api/org/pipeline-guardrails", tags=["releases"])

_require_rules_admin = org_admin_writer("Pipeline rules")


class GuardrailIn(BaseModel):
    mode: Literal["off", "warn", "block"]
    #: `production_approval` only: the approvals the step into production needs. Absent: kept.
    approvals_min: Optional[int] = Field(default=None, ge=1, le=APPROVALS_MAX)


def _org(caller: AsyncCaller) -> UUID:
    """The caller's own org: the rules are its. An org-less caller has none to read or set."""
    return or_404(getattr(caller.principal, "org_id", None), detail="no organization")


def _breaking(session: Session, org_id: UUID, caller: AsyncCaller,
              rows: list[PipelineGuardrail]) -> dict[str, list[dict[str, Any]]]:
    """Per rule, the caller-visible projects with a repository whose stored pipeline breaks it, each rule taken as if
    on (Warn). Pure evaluation of stored rows: no forge call, one pass over the org's projects."""
    projects = session.scalars(
        select(Project).where(Project.org_id == org_id, Project.id.in_(_visible_projects(caller.principal)),
                              Project.repo_url.is_not(None)).order_by(Project.name))
    as_on = [guardrails.As(r.rule, guardrails.WARN, r.approvals_min) for r in rows]
    out: dict[str, list[dict[str, Any]]] = {rule: [] for rule in GUARDRAIL_RULES}
    for project in projects:
        if not stages_mod.has_repo(project):
            continue
        for finding in guardrails.evaluate_stored(session, project, as_on):
            out[finding.rule].append({"project_id": project.id, "name": project.name})
    return out


def _page(session: Session, org_id: UUID, caller: AsyncCaller) -> dict[str, Any]:
    rows = guardrails.for_org(session, org_id)
    session.commit()
    names = actor_names_for(session, rows)
    breaking = _breaking(session, org_id, caller, rows)
    return {"rules": [
        {**entity_fields(row, names), "rule": row.rule, "title": guardrails.TITLES[row.rule], "mode": row.mode,
         "approvals_min": row.approvals_min, "breaking": breaking[row.rule]}
        for row in rows
    ]}


@router.get("")
@feature("projects", "view", collection=True)
@on_session
def get_pipeline_guardrails(session: Session, caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """The org's three rules (written Off on the first read: absence is not a state) and what breaks each today."""
    return _page(session, _org(caller), caller)


@router.put("/{rule}")
@feature("projects", "edit", collection=True)
@on_session
def put_pipeline_guardrail(session: Session, rule: str, body: GuardrailIn,
                           caller: AsyncCaller = Depends(current_caller),
                           admin: AdminUser = Depends(_require_rules_admin)) -> dict[str, Any]:
    """Set one rule's mode (and G1's approvals). Stamped with who and when; no stored pipeline changes."""
    if rule not in GUARDRAIL_RULES:
        raise HTTPException(status_code=404, detail="unknown rule")
    if body.approvals_min is not None and rule != guardrails.PRODUCTION_APPROVAL:
        raise HTTPException(status_code=422, detail="Only Production needs approval takes an approval count.")
    org_id = _org(caller)
    row = next(r for r in guardrails.for_org(session, org_id) if r.rule == rule)
    approvals_min = body.approvals_min if body.approvals_min is not None else row.approvals_min
    if (row.mode, row.approvals_min) != (body.mode, approvals_min):
        # Stamped only on a real change: who changed a rule last is never someone who changed nothing.
        row.mode, row.approvals_min = body.mode, approvals_min
        stamp_actor(row, caller.actor)
    session.commit()
    return _page(session, org_id, caller)
