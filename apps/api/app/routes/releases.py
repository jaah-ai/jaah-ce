"""Releases: a project's Release view and the org's release lists (rules: `app/releases/`).

Reads need `projects:view` on the project (a project the caller cannot see is a 404); a deploy, hotfix, approval,
cancel or retry needs the stage's may-deploy (`app/releases/access.py`); editing stages and freezes, Restore and the
impact preview need `projects:edit`, and every edit is one row of the pipeline's history (`app/releases/history.py`).
A refusal is a bare 403 — the read's `can_*` flags let the UI omit the action. No route here is plan-gated: releases
are on every plan.

Lists are keyset-paged newest first on `id` (`app/keyset.py`): `{items, next_cursor}`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Literal, Optional, Sequence
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import forge_ci, keyset
from app.actors import actor_names, stamp_actor
from app.clock import utcnow
from app.authz.declarations import feature
from app.authz.object_access import require_project_key
from app.authz.catalog import PROJECTS_VIEW_KEY
from app.authz.visibility import has_project_key
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.db_async import on_session, wait_off_loop
from app.entity_shape import PG_BIGINT_MAX, iso_or_none, or_404
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.releases import (
    APPROVALS_MAX,
    COMMENT_MAX,
    FREEZE_REPEATS,
    PipelineRevision,
    PipelineTemplate,
    PipelineTemplateVersion,
    Release,
    ReleaseFreeze,
    ReleaseFreezeStage,
    ReleaseStage,
)
from app import user_activity
from app.project_identity import project_or_404
from app.releases import (
    access, arrivals, basics, freezes as freezes_mod, health, history, impact, lifecycle, linking, override, pipeline, pipeline_doc,
    service, stages as stages_mod, templates,
)
from app.releases.views import Reader
from app.pipeline_gates import results as gate_results, tickets as gate_tickets
from app.pipeline_gates.schema import GateIn, TargetIn
from app.routes.production_qa import _visible_projects

router = APIRouter(tags=["releases"])

_LIMIT_DEFAULT = 50
_LIMIT_MAX = 200
_CURSOR_DETAIL = "'cursor' is not one this listing issued — send back a next_cursor unchanged, or leave it out"
_NAME_MAX = 60
_TEXT_MAX = 500
_FREEZES_MAX = 50
_CHANGES_MAX = 500


# ── Bodies ──────────────────────────────────────────────────────────────────────────────────────────

class DeployIn(BaseModel):
    from_stage_id: UUID
    scheduled_for: Optional[datetime] = None
    #: Some of the step's waiting changes: a pick when short of all of them. Absent: everything waiting.
    change_ids: Optional[list[UUID]] = Field(default=None, max_length=_CHANGES_MAX)
    #: Run this request through a freeze (R7.5): at least 10 characters (`app/releases/override.py`).
    override_reason: Optional[str] = Field(default=None, max_length=override.REASON_MAX)
    #: The ticket a step's "ask when pressed" ticket gate checks (D8); required on such a step.
    ticket: Optional[str] = Field(default=None, max_length=64)
    #: Ship the source's head without the full gate (`service.deploy`): at least 10 characters, a whole promote only.
    untested_reason: Optional[str] = Field(default=None, max_length=override.REASON_MAX)


class HotfixIn(BaseModel):
    change_id: UUID
    override_reason: Optional[str] = Field(default=None, max_length=override.REASON_MAX)


class RetryIn(BaseModel):
    override_reason: Optional[str] = Field(default=None, max_length=override.REASON_MAX)


class FreezeIn(BaseModel):
    """One freeze. `repeat` replaces `weekly`; a body with only `weekly` (an old client) keeps the stored repeat,
    scope and let-through options (`_freeze_fields`)."""

    id: Optional[UUID] = None
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    starts_at: datetime
    ends_at: datetime
    weekly: bool = False
    timezone: str = Field(default="UTC", max_length=64)
    repeat: Optional[Literal["never", "daily", "weekly", "monthly"]] = None
    scope: Optional[Literal["after_integration", "stages"]] = None
    stage_ids: Optional[list[UUID]] = Field(default=None, max_length=_FREEZES_MAX)
    let_pressed: Optional[bool] = None
    let_queued: Optional[bool] = None


class FreezeNowIn(BaseModel):
    on: bool


# ── Helpers ─────────────────────────────────────────────────────────────────────────────────────────

def _refused(exc: service.Refused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.body or None)


def _project_with_repo(session: Session, project_id: int) -> Project:
    project = project_or_404(session, project_id)
    if not stages_mod.has_repo(project):
        raise HTTPException(status_code=404, detail="no repository")
    return project


def _page_after(cursor: Optional[str], shape: list[str]) -> Optional[int]:
    if not cursor:
        return None
    (after,) = keyset.decode_cursor(
        cursor, shape, is_stamp=lambda _name: False, int_range=(1, PG_BIGINT_MAX), status=422, detail=_CURSOR_DETAIL,
    )
    return after


def _one_release(session: Session, project: Project, principal: Optional[Principal], release: Release,
                 status_code: int = 200) -> JSONResponse:
    body = Reader(session, project, principal).release_out(release)
    session.commit()
    return JSONResponse(body, status_code=status_code)


def _empty_tab() -> dict[str, Any]:
    """A repository-less project's tab (`stages.has_repo`): no stages, nothing to deploy."""
    return {
        "stages": [], "freezes": [], "waiting": {}, "extra_commits": {}, "open": [], "can_deploy": False,
        "executor": {"state": "idle", "node_online": False, "task_id": None, "wait_reason": None},
        "deploy_events": "not_connected", "promote_pin": None,
    }


def _tab(session: Session, project: Project, principal: Optional[Principal]) -> dict[str, Any]:
    """Read only: the GET serves it, so a writer commits before it reads."""
    return Reader(session, project, principal).tab()


# ── The project tab ─────────────────────────────────────────────────────────────────────────────────

@router.get("/api/projects/{project_id}/release")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_project_release(session: Session, project_id: int, caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """The project's stages, what is waiting on each step, the open releases and what the caller may do. A project
    with no repository has no release yet: an empty tab (no stages written), so the Tasks page's poll reads quietly."""
    project = project_or_404(session, project_id)
    if not stages_mod.has_repo(project):
        return _empty_tab()
    return _tab(session, project, caller.principal)


@router.post("/api/projects/{project_id}/release/deploy", status_code=201)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def deploy_release(session: Session, project_id: int, body: DeployIn,
           caller: AsyncCaller = Depends(current_caller)) -> JSONResponse:
    """Deploy one step: `from_stage_id` → the next stage (`change_ids`: only those of its waiting changes). 201 with the
    new release; 200 with the step's open one."""
    project = _project_with_repo(session, project_id)
    try:
        release, created = service.deploy(session, project, caller.principal, body.from_stage_id, body.scheduled_for,
                                          change_ids=body.change_ids, override_reason=body.override_reason,
                                          ticket=body.ticket, untested_reason=body.untested_reason)
    except service.Refused as exc:
        raise _refused(exc) from None
    return _one_release(session, project, caller.principal, release, 201 if created else 200)


@router.post("/api/projects/{project_id}/stages/{stage_id}/rollback", status_code=201)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def rollback_stage(session: Session, project_id: int, stage_id: UUID,
                   caller: AsyncCaller = Depends(current_caller)) -> JSONResponse:
    """Roll a stage whose change failed to deploy or verify back to its previous version (`app/releases/arrivals.py`):
    201 with the roll back release. Who may press the step into the stage may; 409 when it is not offered or a
    release into the stage is open."""
    project = _project_with_repo(session, project_id)
    try:
        release = arrivals.rollback(session, project, caller.principal, stage_id)
    except service.Refused as exc:
        raise _refused(exc) from None
    return _one_release(session, project, caller.principal, release, 201)


@router.post("/api/projects/{project_id}/release/survey")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def survey_release(session: Session, project_id: int, caller: AsyncCaller = Depends(current_caller)) -> dict[str, bool]:
    """The tab asks once it opens: count what each stage's branch holds when the last count is stale
    (`tick.survey_on_look`), so commits merged on the forge show as waiting. Only for a caller who may deploy
    (a survey costs an executor run); `filed` says whether one was."""
    from app.releases import tick  # noqa: PLC0415 — as `service.wake_tick`

    project = _project_with_repo(session, project_id)
    filed = Reader(session, project, caller.principal).may_deploy_any() and tick.survey_on_look(session, project, utcnow())
    session.commit()
    return {"filed": filed}


@router.post("/api/projects/{project_id}/release/hotfix", status_code=201)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def hotfix_release(session: Session, project_id: int, body: HotfixIn,
           caller: AsyncCaller = Depends(current_caller)) -> JSONResponse:
    """Ship one change from Dev straight to Production."""
    project = _project_with_repo(session, project_id)
    try:
        release, created = service.hotfix(session, project, caller.principal, body.change_id,
                                          override_reason=body.override_reason)
    except service.Refused as exc:
        raise _refused(exc) from None
    return _one_release(session, project, caller.principal, release, 201 if created else 200)


class ApprovalIn(BaseModel):
    """Approve or reject: an optional comment, stored with it (R3.6)."""

    comment: Optional[str] = Field(default=None, max_length=COMMENT_MAX)


def _act(session: Session, project_id: int, release_id: int, caller: AsyncCaller, verb: Any,
         *args: Any) -> JSONResponse:
    project = _project_with_repo(session, project_id)
    try:
        release = verb(session, project, caller.principal, release_id, *args)
    except service.Refused as exc:
        raise _refused(exc) from None
    return _one_release(session, project, caller.principal, release)


@router.post("/api/projects/{project_id}/release/{release_id}/approve")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def approve_release(session: Session, project_id: int, release_id: int, body: Optional[ApprovalIn] = None,
            caller: AsyncCaller = Depends(current_caller)) -> JSONResponse:
    """Approve a release waiting for approval, as one of its step's approvers (never its requester while the step
    says so); `comment` optional."""
    return _act(session, project_id, release_id, caller, service.approve, _comment(body))


@router.post("/api/projects/{project_id}/release/{release_id}/reject")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def reject_release(session: Session, project_id: int, release_id: int, body: Optional[ApprovalIn] = None,
           caller: AsyncCaller = Depends(current_caller)) -> JSONResponse:
    """Reject a release waiting for approval, as one of its step's approvers: it is canceled; `comment` optional."""
    return _act(session, project_id, release_id, caller, service.reject, _comment(body))


def _comment(body: Optional[ApprovalIn]) -> Optional[str]:
    text = (body.comment or "").strip() if body is not None else ""
    return text or None


@router.post("/api/projects/{project_id}/release/{release_id}/cancel")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def cancel_release(session: Session, project_id: int, release_id: int,
           caller: AsyncCaller = Depends(current_caller)) -> JSONResponse:
    """Cancel a release that has not finished."""
    return _act(session, project_id, release_id, caller, service.cancel)


@router.post("/api/projects/{project_id}/release/{release_id}/retry")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def retry_release(session: Session, project_id: int, release_id: int, body: Optional[RetryIn] = None,
          caller: AsyncCaller = Depends(current_caller)) -> JSONResponse:
    """Queue a failed release again (`override_reason`: through a freeze)."""
    reason = body.override_reason if body is not None else None
    return _act(session, project_id, release_id, caller,
                lambda s, p, principal, rid: service.retry(s, p, principal, rid, override_reason=reason))


@router.get("/api/projects/{project_id}/release/history")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def release_history(session: Session, project_id: int, cursor: Optional[str] = None,
                    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
                    caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """The project's releases, newest first."""
    project = _project_with_repo(session, project_id)
    shape = ["releases.id"]
    stmt = select(Release).where(Release.project_id == project.id)
    after = _page_after(cursor, shape)
    if after is not None:
        stmt = stmt.where(Release.id < after)
    rows, more = keyset.page(session, stmt.order_by(Release.id.desc()), limit)
    items = Reader(session, project, caller.principal).releases_out(rows)
    return {"items": items, "next_cursor": keyset.encode_cursor(shape, [rows[-1].id]) if more else None}


# ── Settings: freezes (projects:edit) ───────────────────────────────────────────────────────────────

def _check_freezes(session: Session, project: Project, body: Sequence[FreezeIn]) -> None:
    """A freezes body's 422s and 404, before anything is written (the per-freeze rules: `_freeze_fields`)."""
    if len(body) > _FREEZES_MAX:
        raise HTTPException(status_code=422, detail=f"At most {_FREEZES_MAX} freezes.")
    for item in body:
        if item.starts_at.tzinfo is None or item.ends_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="Times need a time zone offset.")
        if item.ends_at <= item.starts_at:
            raise HTTPException(status_code=422, detail="End must be after start.")
        try:
            freezes_mod.zone(item.timezone)
        except ValueError:
            raise HTTPException(status_code=422, detail="Unknown time zone.") from None
    sent = {item.id for item in body if item.id is not None}
    if sent:
        known = set(session.scalars(select(ReleaseFreeze.id).where(
            ReleaseFreeze.project_id == project.id, ReleaseFreeze.id.in_(sent))))
        if sent - known:
            raise HTTPException(status_code=404, detail="unknown freeze")


def _write_freezes(session: Session, project: Project, body: Sequence[FreezeIn], actor: AdminUser) -> None:
    """Replace the project's freezes with `body` (checked by `_check_freezes`; each freeze's rules by
    `_freeze_fields`, all before anything is written); the caller commits."""
    existing = {f.id: f for f in session.scalars(select(ReleaseFreeze).where(ReleaseFreeze.project_id == project.id))}
    branch = {s.id: s for s in stages_mod.branch_stages(stages_mod.stages_of(session, project.id))}
    fields = [_freeze_fields(index, item, existing.get(item.id) if item.id is not None else None, branch)
              for index, item in enumerate(body)]
    kept = {item.id for item in body if item.id is not None}
    for freeze_id, freeze in existing.items():
        if freeze_id not in kept:
            session.delete(freeze)
    for item, (repeat, scope, stage_ids, let_pressed, let_queued) in zip(body, fields, strict=True):
        freeze = existing.get(item.id) if item.id is not None else None
        if freeze is None:
            freeze = ReleaseFreeze(org_id=project.org_id, project_id=project.id)
            stamp_actor(freeze, actor, created=True)
            session.add(freeze)
        elif (freeze.name, freeze.starts_at, freeze.ends_at, freeze.repeat, freeze.timezone, freeze.scope,
              freeze.let_pressed, freeze.let_queued) != (
                item.name.strip(), item.starts_at, item.ends_at, repeat, item.timezone, scope, let_pressed,
                let_queued) or stage_ids is not None:
            stamp_actor(freeze, actor)
        freeze.name = item.name.strip()
        freeze.starts_at = item.starts_at
        freeze.ends_at = item.ends_at
        # `weekly` stays the old spelling of `repeat = 'weekly'`, for a previous release still reading it.
        freeze.repeat, freeze.weekly = repeat, repeat == freezes_mod.WEEKLY
        freeze.timezone = item.timezone
        freeze.scope, freeze.let_pressed, freeze.let_queued = scope, let_pressed, let_queued
        session.flush()
        if stage_ids is not None:
            for old in session.scalars(select(ReleaseFreezeStage).where(ReleaseFreezeStage.freeze_id == freeze.id)):
                session.delete(old)
            session.flush()
            for stage_id in stage_ids:
                session.add(ReleaseFreezeStage(freeze_id=freeze.id, stage_id=stage_id, org_id=project.org_id))
    session.flush()


def _lock(session: Session, project: Project) -> Project:
    """The project row, locked for this save's transaction: a concurrent save waits, so each history row's before
    and after are the setup this save alone changed (`pipeline.save` takes the same lock)."""
    return session.scalar(select(Project).where(Project.id == project.id).with_for_update()
                          .execution_options(populate_existing=True)) or project


@router.put("/api/projects/{project_id}/release/freezes")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def put_release_freezes(session: Session, project_id: int, body: list[FreezeIn],
                caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Replace the project's freezes; recorded in the pipeline's history (`app/releases/history.py`)."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = _project_with_repo(session, project_id)
    _check_freezes(session, project, body)
    project = _lock(session, project)
    before = history.snapshot(session, project)
    _write_freezes(session, project, body, caller.actor)
    history.add(session, project, caller.actor, before, history.snapshot(session, project))
    session.commit()
    return _tab(session, project, caller.principal)


def _freeze_refuse(code: str, message: str, index: int, field_name: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message, "index": index,
                                                  "field": field_name})


def _freeze_fields(index: int, item: FreezeIn, stored: Optional[ReleaseFreeze],
                   branch: dict[UUID, Any]) -> tuple[str, str, Optional[list[UUID]], bool, bool]:
    """`(repeat, scope, stage_ids, let_pressed, let_queued)` to write, or a 422 naming the field. A field the body
    leaves out keeps the stored value (a new freeze: its default); `stage_ids` None keeps the stored stages. A body
    with `weekly` and no `repeat` (an old client) maps it onto the stored repeat, so the two never disagree."""
    stored_repeat = (stored.repeat if stored.repeat not in (None, freezes_mod.NEVER)
                     else (freezes_mod.WEEKLY if stored.weekly else freezes_mod.NEVER)) if stored is not None \
        else freezes_mod.NEVER
    repeat: str
    if item.repeat is not None:
        repeat = item.repeat
    elif item.weekly:
        repeat = freezes_mod.WEEKLY if stored_repeat == freezes_mod.NEVER else stored_repeat
    else:
        repeat = freezes_mod.NEVER if stored_repeat == freezes_mod.WEEKLY else stored_repeat
    assert repeat in FREEZE_REPEATS
    if freezes_mod.too_long(repeat, item.ends_at - item.starts_at):
        period = {freezes_mod.DAILY: "a day", freezes_mod.WEEKLY: "a week", freezes_mod.MONTHLY: "28 days"}[repeat]
        raise _freeze_refuse("window_too_long", f"A {repeat} freeze must be shorter than {period}. "
                             "Move the end earlier or pick a longer repeat.", index, "ends_at")
    scope = item.scope or (stored.scope if stored is not None else "after_integration")
    stage_ids = list(dict.fromkeys(item.stage_ids)) if item.stage_ids is not None else None
    if scope == "stages":
        if stage_ids is None and stored is not None and stored.scope == "stages":
            pass  # keeps its stored stages
        elif not stage_ids:
            raise _freeze_refuse("stages_required", "Pick at least one stage for this freeze.", index, "stage_ids")
        elif any(stage_id not in branch for stage_id in stage_ids):
            raise _freeze_refuse("stage_unknown", "Pick stages of this pipeline that deploy to a branch.", index,
                                 "stage_ids")
    else:
        stage_ids = [] if stored is not None and stored.scope == "stages" else None
    let_pressed = item.let_pressed if item.let_pressed is not None else (
        bool(stored.let_pressed) if stored is not None else False)
    let_queued = item.let_queued if item.let_queued is not None else (
        stored.let_queued is not False if stored is not None else True)
    return repeat, scope, stage_ids, let_pressed, let_queued


@router.put("/api/projects/{project_id}/release/freeze-now")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def put_freeze_now(session: Session, project_id: int, body: FreezeNowIn,
                   caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Freeze now (R7.6): on freezes every stage after the integration stage from this moment until switched off,
    with no let-through. Each switch writes one audit row (`freeze_now_on` / `freeze_now_off`)."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = _project_with_repo(session, project_id)
    project = session.scalar(select(Project).where(Project.id == project.id).with_for_update()
                             .execution_options(populate_existing=True)) or project
    if body.on != (project.freeze_now_at is not None):
        if body.on:
            project.freeze_now_at, project.freeze_now_by_id = utcnow(), caller.actor.id
        else:
            project.freeze_now_at = project.freeze_now_by_id = None
        kind = "freeze_now_on" if body.on else "freeze_now_off"
        summary = f"{'Freeze now on' if body.on else 'Freeze now off'} · {project.name}"
        user_activity.record(session, caller.actor, "write", kind, summary,
                             path=f"/api/projects/{project.id}/release/freeze-now", method="PUT",
                             data={"project_id": project.id})
        service.wake_tick(session)
        session.commit()
    names = {str(u): n for u, n in actor_names(session, {project.freeze_now_by_id}).items()} \
        if project.freeze_now_by_id is not None else {}
    shown = pipeline.freeze_now_out(project, names)
    return {"on": shown is not None, "by_name": shown["by_name"] if shown else None,
            "at": shown["at"] if shown else None}


# ── Settings: the pipeline (Project settings › Pipeline) ───────────────────────────────────────────────

class StepWhoIn(BaseModel):
    roles: list[str] = []
    users: list[UUID] = []


class PipelineStageIn(BaseModel):
    id: Optional[UUID] = None
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    branch: str = Field(default="", max_length=255)
    url: Optional[str] = Field(default=None, max_length=_TEXT_MAX)
    # Left out: kept as stored (`app/pipeline_gates/`). `after` is the stage's verify-on-arrival list.
    target: Optional[TargetIn] = None
    after: Optional[list[GateIn]] = Field(default=None, max_length=100)


class PipelineStepIn(BaseModel):
    """One step. Every rule is optional with today's behaviour as its default; ranges are checked by
    `pipeline.validate_rules` (a 422 naming the field)."""

    who: Optional[StepWhoIn] = None
    approvals: int = Field(default=0, ge=0, le=APPROVALS_MAX)
    auto: bool = False
    approvers: Optional[StepWhoIn] = None
    requester_cant_approve: bool = True
    approval_timeout_minutes: Optional[int] = None
    on_timeout: Literal["cancel", "remind"] = "cancel"
    instructions: str = Field(default="", max_length=10_000)
    soak_minutes: int = 0
    allowed_hours: Optional[dict[str, Any]] = None
    concurrency: Literal["newest", "in_order"] = "newest"
    # Left out: kept as stored. The step's gates, run before a change goes into its stage.
    gates: Optional[list[GateIn]] = Field(default=None, max_length=100)


class PipelineIn(BaseModel):
    stages: list[PipelineStageIn]
    steps: list[PipelineStepIn]


class PipelineSaveIn(PipelineIn):
    """The console's Save: the pipeline and, when sent, the freezes too, as ONE save and one history row."""

    freezes: Optional[list[FreezeIn]] = Field(default=None, max_length=_FREEZES_MAX)


@router.get("/api/projects/{project_id}/pipeline")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_pipeline(session: Session, project_id: int, caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """The project's stages and the step into each (`app/releases/pipeline.py::read`). Marks the stages looked at,
    so their health dots stay live while this is open (`app/releases/health.py::looked_at`)."""
    body = pipeline.read(session, project_or_404(session, project_id))
    health.looked_at(session, [s["id"] for s in body["stages"]])
    return body


@router.get("/api/projects/{project_id}/pipeline/ci-workflows")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_pipeline_ci_workflows(session: Session, project_id: int,
                              caller: AsyncCaller = Depends(current_caller)) -> list[dict[str, str]]:
    """The project's CI workflows on its own forge, as the caller's own forge account reads them, for a CI gate's
    picker (`app/forge_ci.py::workflows`): `[{id, name, path}]`, `[]` when they cannot be read (type the name)."""
    project = project_or_404(session, project_id)
    planned = forge_ci.plan_workflows(session, project, caller.actor)
    # The forge reads (several files, seconds each on a slow forge) never run on the serving loop.
    found = wait_off_loop(session, forge_ci.read_workflows, planned) if planned is not None else []
    return [{"id": w.id, "name": w.name, "path": w.path} for w in found]


@router.put("/api/projects/{project_id}/pipeline")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def put_pipeline(session: Session, project_id: int, body: PipelineSaveIn,
                 caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Replace the project's pipeline (and its freezes, when sent): sent id → updated, no id → added, left out →
    removed, in the order sent. Never refused for a running release: it applies from the next one. 422
    `pipeline_order`, `name_taken`, `branch_taken`, `guardrail` (an org rule set to Block; its `rule` beside the
    message). Recorded in the pipeline's history. `warnings` carries the org rules set to Warn the save breaks."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = _project_with_repo(session, project_id)
    basics.require_editor(session, project)
    return replace_pipeline(session, project, body, caller.actor, caller.principal, freezes=body.freezes)


def pipeline_specs(body: PipelineIn) -> tuple[list[pipeline.StageSpec], list[pipeline.StepSpec]]:
    """The body as the save's stage and step specs (an absent `url` keeps the stored one)."""
    def sent(model: BaseModel, name: str) -> Any:
        return getattr(model, name) if name in model.model_fields_set and getattr(model, name) is not None \
            else pipeline.KEEP

    specs = [pipeline.StageSpec(s.id, s.name, s.branch, s.url if "url" in s.model_fields_set else pipeline.KEEP,
                                target=sent(s, "target"), after=sent(s, "after"))
             for s in body.stages]
    steps = [pipeline.StepSpec(
        s.who.model_dump(mode="json") if s.who is not None else None, s.approvals, s.auto,
        approvers=s.approvers.model_dump(mode="json") if s.approvers is not None else None,
        requester_cant_approve=s.requester_cant_approve, approval_timeout_minutes=s.approval_timeout_minutes,
        on_timeout=s.on_timeout, instructions=s.instructions, soak_minutes=s.soak_minutes,
        allowed_hours=s.allowed_hours, concurrency=s.concurrency, gates=sent(s, "gates"),
    ) for s in body.steps]
    return specs, steps


def replace_pipeline(session: Session, project: Project, body: PipelineIn, actor: AdminUser,
                     principal: Principal, *, freezes: Optional[Sequence[FreezeIn]] = None,
                     restored_from: Optional[int] = None,
                     then: Optional[Callable[[Project], None]] = None) -> dict[str, Any]:
    """`put_pipeline`'s write, after its authority check: shared with the runner's
    `PUT /api/agent/tasks/{task_id}/project/pipeline` (`routes/agent_project_settings.py`, under the launcher),
    Restore and a basic's save (`put_basic_pipeline`). `freezes` None leaves them as stored. One history row for the
    whole save (`app/releases/history.py`); `then(project)` writes in the same commit."""
    specs, steps = pipeline_specs(body)
    if freezes is not None:
        _check_freezes(session, project, freezes)
    project = _lock(session, project)
    before = history.snapshot(session, project)

    def also_write(fields: dict[str, Any]) -> None:
        # Inside the save's transaction: the freezes and the history row commit with the stages, or not at all.
        if freezes is not None:
            _write_freezes(session, project, freezes, actor)
        history.add(session, project, actor, before, history.snapshot(session, project, fields),
                    restored_from=restored_from)
        if then is not None:
            then(project)

    warnings = pipeline.write(session, project, actor, principal, specs, steps, before_commit=also_write)
    return {**pipeline.read(session, project), "warnings": [f.out() for f in warnings]}


@router.get("/api/projects/{project_id}/gate-runs")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_gate_runs(session: Session, project_id: int,
                  subject_kind: Optional[Literal["lane_batch", "release", "arrival"]] = None,
                  subject_id: Optional[str] = Query(default=None, max_length=200),
                  task_id: Optional[int] = Query(default=None, ge=1, le=PG_BIGINT_MAX),
                  stage_id: Optional[UUID] = None,
                  caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """The project's newest gate runs (of one subject when named, or of the merge lane batches that carried one task's
    pull request), each with its gates' results (`app/pipeline_gates/results.py`). Reports are plain text."""
    project = project_or_404(session, project_id)
    if task_id is not None:
        return gate_results.task_runs_out(session, project, task_id)
    return gate_results.runs_out(session, project, subject_kind, subject_id, stage_id)


def _ticket_connection(session: Session, project: Project, caller: AsyncCaller, system: str,
                       credential: str) -> gate_tickets.Connection:
    """The ticket system, signed in with `credential` as the caller may use it on `project` (the broker's rule)."""
    try:
        row = gate_tickets.resolve(session, caller.principal, project.id, credential, system)
        return gate_tickets.connect(session, row, system)
    except gate_tickets.TicketError as exc:
        raise HTTPException(status_code=422, detail={"code": "ticket_credential", "message": str(exc)}) from None


@router.get("/api/projects/{project_id}/pipeline/ticket-states")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_pipeline_ticket_states(session: Session, project_id: int, system: Literal["jira", "servicenow"],
                               credential: str = Query(min_length=1, max_length=300),
                               project: Optional[str] = Query(default=None, max_length=50, alias="jira_project"),
                               caller: AsyncCaller = Depends(current_caller)) -> list[dict[str, str]]:
    """The states a ticket gate can wait for, read from the ticket system with a credential the caller may use on
    this project (AC7.4): Jira's statuses (one project's when the credential or `jira_project` names one), or
    ServiceNow's change request states. 422 `ticket_credential` / `ticket_system` with a sentence when they can't
    be read."""
    target = project_or_404(session, project_id)
    conn = _ticket_connection(session, target, caller, system, credential)
    try:
        return wait_off_loop(session, gate_tickets.states, conn, project)
    except gate_tickets.TicketError as exc:
        raise HTTPException(status_code=422, detail={"code": "ticket_system", "message": str(exc)}) from None


@router.get("/api/projects/{project_id}/pipeline/ticket")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_pipeline_ticket(session: Session, project_id: int, stage_id: UUID,
                        key: str = Query(min_length=1, max_length=64),
                        caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Look up the ticket typed for the step into `stage_id`, whose ticket gate asks when pressed (the Deploy
    dialog's check): `{found, key, status, url}`. 404 when that step asks for no ticket."""
    target = project_or_404(session, project_id)
    gate = or_404(gate_tickets.ask_gates(session, target.id).get(stage_id), detail="this step asks for no ticket")
    system = str((gate.config or {}).get("system") or "")
    conn = _ticket_connection(session, target, caller, system, gate.credential or "")
    try:
        found = wait_off_loop(session, gate_tickets.lookup, conn, key)
    except gate_tickets.TicketError as exc:
        raise HTTPException(status_code=422, detail={"code": "ticket_system", "message": str(exc)}) from None
    if found is None:
        return {"found": False, "key": key.strip(), "status": None, "url": None}
    url = found.url if found.url and found.url.startswith("https://") else None
    return {"found": True, "key": found.key, "status": found.status_name or None, "url": url}


@router.post("/api/projects/{project_id}/pipeline/preview")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def preview_pipeline(session: Session, project_id: int, body: PipelineSaveIn,
                     caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """What saving this draft would change for work already on the pipeline (`app/releases/impact.py`). Writes
    nothing. A draft the save would refuse answers its 422."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = _project_with_repo(session, project_id)
    specs, steps = pipeline_specs(body)
    pipeline.validate(specs, steps)
    if body.freezes is not None:
        _check_freezes(session, project, body.freezes)
    windows = None if body.freezes is None else [
        (f.name.strip(), freezes_mod.Window(f.starts_at, f.ends_at, f.weekly, f.timezone, repeat=f.repeat))
        for f in body.freezes]
    return {"impacts": impact.preview(session, project, caller.principal, specs, steps, windows)}


@router.get("/api/projects/{project_id}/pipeline/history")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def pipeline_history(session: Session, project_id: int, cursor: Optional[str] = None,
                     limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
                     caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """The project's pipeline saves, newest first: who, when, and what each changed."""
    project = project_or_404(session, project_id)
    shape = ["pipeline_revisions.id"]
    stmt = select(PipelineRevision).where(PipelineRevision.project_id == project.id)
    after = _page_after(cursor, shape)
    if after is not None:
        stmt = stmt.where(PipelineRevision.id < after)
    rows, more = keyset.page(session, stmt.order_by(PipelineRevision.id.desc()), limit)
    return {"items": history.revisions_out(session, rows),
            "next_cursor": keyset.encode_cursor(shape, [rows[-1].id]) if more else None}


@router.post("/api/projects/{project_id}/pipeline/history/{revision_id}/restore")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def restore_pipeline(session: Session, project_id: int, revision_id: int,
                     caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Bring back the pipeline and freezes as that save left them (`history.restore_body`), as one new save."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = _project_with_repo(session, project_id)
    basics.require_editor(session, project)
    revision = or_404(session.scalar(select(PipelineRevision).where(
        PipelineRevision.id == revision_id, PipelineRevision.project_id == project.id)), detail="unknown save")
    body = PipelineSaveIn.model_validate(history.restore_body(session, project, revision))
    return replace_pipeline(session, project, body, caller.actor, caller.principal, freezes=body.freezes,
                            restored_from=revision.id)


# ── The basic pipelines (Project settings › Customize › CI Pipeline) ────────────────────────────────────

class BasicAddressesIn(BaseModel):
    """Each live stage's address by role; a key left out keeps the stored one, null clears it."""

    dev: Optional[str] = Field(default=None, max_length=_TEXT_MAX)
    production: Optional[str] = Field(default=None, max_length=_TEXT_MAX)


class BasicPipelineIn(BaseModel):
    mode: Literal["pr_integration", "integration_release", "custom"]
    release_branch: Optional[str] = Field(default=None, max_length=255)
    addresses: BasicAddressesIn = Field(default_factory=BasicAddressesIn)


@router.get("/api/projects/{project_id}/pipeline/basic")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_basic_pipeline(session: Session, project_id: int,
                       caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """The project's pipeline mode and its basic's settings (`app/releases/basics.py::out`): `{mode, custom_offered,
    read_only, integration_branches, release_branch, addresses, reset_at}`. Never writes."""
    return basics.out(session, project_or_404(session, project_id))


@router.put("/api/projects/{project_id}/pipeline/basic")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def put_basic_pipeline(session: Session, project_id: int, body: BasicPipelineIn,
                       caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Pick the project's pipeline: a basic rewrites its stages to that preset (`basics.basic_body`) as one pipeline
    save, unlinking an org template; `custom` keeps the stages and opens the Pipeline tab's editor. 422 "Add a
    repository first." with no basic to pick; 409 `custom` off the plan; a save's own 422 as `PUT /pipeline`'s."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = project_or_404(session, project_id)
    if basics.effective_mode(session, project) == basics.NONE:
        raise HTTPException(status_code=422, detail=basics.NO_REPO)
    if body.mode == basics.CUSTOM:
        if not basics.custom_offered(session, project):
            raise HTTPException(status_code=409, detail=basics.NOT_ON_PLAN)
        project.pipeline_mode = basics.CUSTOM
        stamp_actor(project, caller.actor)
        session.commit()
        return basics.out(session, project)
    if linking.linked(session, project) is not None:
        # A basic is the project's own: the template link goes with the custom pipeline it held.
        project.pipeline_template_id = None
        project.pipeline_template_version_id = None
        session.flush()
    sent = body.addresses.model_dump(include=body.addresses.model_fields_set)
    save = PipelineIn.model_validate(basics.basic_body(session, project, body.mode, body.release_branch, sent))

    def set_mode(locked: Project) -> None:
        locked.pipeline_mode = body.mode

    replace_pipeline(session, project, save, caller.actor, caller.principal, then=set_mode)
    return basics.out(session, project)


# ── Starting from a preset or another project (Project settings › Pipeline › Start from…) ───────────────

class DraftSourceIn(BaseModel):
    """Exactly one: a preset's slug, or the project to copy from."""

    preset: Optional[str] = Field(default=None, max_length=64)
    project_id: Optional[int] = Field(default=None, ge=1, le=PG_BIGINT_MAX)


class DraftIn(BaseModel):
    source: DraftSourceIn


@router.get("/api/pipeline/presets")
@feature("projects", "view", collection=True)
@on_session
def list_presets(session: Session, caller: AsyncCaller = Depends(current_caller)) -> list[dict[str, Any]]:
    """The presets a pipeline can start from (`app/releases/pipeline_doc.py::PRESETS`), each with its stage chain."""
    return pipeline_doc.presets_out()


@router.get("/api/projects/pipeline-chains")
@feature("projects", "view", collection=True)
@on_session
def list_pipeline_chains(session: Session, exclude: Optional[int] = Query(default=None, ge=1, le=PG_BIGINT_MAX),
                         caller: AsyncCaller = Depends(current_caller)) -> list[dict[str, Any]]:
    """Every project of the caller's org they may view, but `exclude`, with its stage chain: the projects a pipeline
    can be copied from. One read for them all."""
    if exclude is not None and not has_project_key(caller.principal, PROJECTS_VIEW_KEY, exclude):
        raise HTTPException(status_code=404, detail=f"unknown project {exclude}")
    org_id = project_or_404(session, exclude).org_id if exclude is not None else caller.actor.org_id
    projects = list(session.scalars(
        select(Project).where(Project.id.in_(_visible_projects(caller.principal)), Project.org_id == org_id,
                              *([Project.id != exclude] if exclude is not None else []))
        .order_by(Project.name)))
    stored: dict[int, list[Any]] = {p.id: [] for p in projects}
    if projects:
        for stage in session.scalars(select(ReleaseStage).where(ReleaseStage.project_id.in_(list(stored)))
                                     .order_by(ReleaseStage.project_id, ReleaseStage.position)):
            stored[stage.project_id].append(stage)
    return [{"project_id": p.id, "name": p.name,
             "chain": pipeline_doc.chain([s.name for s in pipeline.shown(p, stored[p.id])])} for p in projects]


@router.post("/api/projects/{project_id}/pipeline/draft")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def draft_pipeline(session: Session, project_id: int, body: DraftIn,
                   caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """What the project's pipeline would be if started from a preset or copied from another project of its org:
    `{draft, changes, problems}`. `draft` is a save body for `PUT /pipeline` (each address the project's own);
    `changes` what differs from the saved pipeline; `problems` what that save would refuse. Writes nothing. A source
    project the caller cannot view, or in another org, is a 404 like an unknown one."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = project_or_404(session, project_id)
    link = linking.linked(session, project)
    if link is not None:
        raise HTTPException(status_code=409, detail={
            "code": "template_linked", "message": f"Linked to template {link[0].name}; unlink to start over.",
        })
    source = body.source
    if (source.preset is None) == (source.project_id is None):
        raise HTTPException(status_code=422, detail="Send one source: a preset or a project.")
    if source.preset is not None:
        doc = pipeline_doc.preset_or_422(source.preset)
    else:
        assert source.project_id is not None
        unknown = f"unknown project {source.project_id}"
        if not has_project_key(caller.principal, PROJECTS_VIEW_KEY, source.project_id):
            raise HTTPException(status_code=404, detail=unknown)
        origin = project_or_404(session, source.project_id)
        if origin.org_id != project.org_id or origin.id == project.id:
            raise HTTPException(status_code=404, detail=unknown)
        doc = pipeline_doc.from_project(session, origin)
    current = pipeline.stages_for(session, project)
    draft = pipeline_doc.instantiate(doc, project, current)
    addresses = {str(s.id): s.url for s in current}
    for stage in draft["stages"]:
        if "url" not in stage:
            stage["url"] = addresses.get(stage.get("id") or "")
    problems = pipeline_doc.problems(draft, project)
    if not problems and pipeline.upgrade_pending(project, current) and not stages_mod.old_version_gone(session):
        # The save would wait for the update to finish (`pipeline._refuse_unupgraded_edits`): say so now.
        problems = [{"code": "pipeline_upgrading",
                     "message": "Ideas, Building and Merge can be changed in a few minutes, once the update finishes."}]
    return {
        "draft": draft,
        "changes": pipeline_doc.diff(pipeline_doc.from_project(session, project), pipeline_doc.as_doc(draft, project)),
        "problems": problems,
    }


# ── Linking to an org pipeline template (Project settings › Pipeline › Link to template…) ────────────────

class TemplateLinkIn(BaseModel):
    template_id: UUID


def _published_template(session: Session, project: Project,
                        template_id: UUID) -> tuple[PipelineTemplate, PipelineTemplateVersion]:
    """The project's org's template and its published version: 404 unknown, 409 `template_unpublished`."""
    template = or_404(session.scalar(select(PipelineTemplate).where(
        PipelineTemplate.id == template_id, PipelineTemplate.org_id == project.org_id)), detail="unknown template")
    version = templates.published(session, template)
    if version is None:
        raise HTTPException(status_code=409, detail={
            "code": "template_unpublished", "message": f"{template.name} isn't published yet. Publish it first.",
        })
    return template, version


@router.get("/api/projects/{project_id}/pipeline/template-preview")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def preview_pipeline_template_link(session: Session, project_id: int, template_id: UUID,
                                   caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """What linking to the template's published version would change (`changes`) and refuse (`problems`). Writes
    nothing."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = project_or_404(session, project_id)
    template, version = _published_template(session, project, template_id)
    return {
        "template": {"id": str(template.id), "name": template.name, "version_number": version.number,
                     "published_at": iso_or_none(version.created_at)},
        **linking.preview(session, project, version),
    }


@router.put("/api/projects/{project_id}/pipeline/template")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def link_pipeline_template(session: Session, project_id: int, body: TemplateLinkIn,
                           caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Link the project to the template: its pipeline becomes the published version (`app/releases/linking.py`), as
    one save. 409 `template_unpublished`; a 422 the save would give (an org rule set to Block: `guardrail`) leaves
    the project unchanged."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = project_or_404(session, project_id)
    basics.require_editor(session, project)
    _, version = _published_template(session, project, body.template_id)
    warnings = linking.write_version(session, project, version, caller.actor, caller.principal)
    return {**pipeline.read(session, project), "warnings": [f.out() for f in warnings]}


@router.delete("/api/projects/{project_id}/pipeline/template")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def unlink_pipeline_template(session: Session, project_id: int,
                             caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Unlink: every value stays and every field becomes editable."""
    require_project_key(caller.principal, access.PROJECTS_EDIT, project_id, detail=None)
    project = project_or_404(session, project_id)
    basics.require_editor(session, project)
    linking.unlink(session, project)
    return pipeline.read(session, project)


# ── The org lists ───────────────────────────────────────────────────────────────────────────────────

@router.get("/api/releases")
@feature("projects", "view", collection=True)
@on_session
def list_releases(session: Session, cursor: Optional[str] = None, project_id: Optional[int] = None,
                  state: Optional[str] = None, limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
                  caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Every release of the projects the caller can see, newest first."""
    if state is not None and state not in lifecycle.STATES:
        raise HTTPException(status_code=422, detail=f"'state' is one of {', '.join(lifecycle.STATES)}")
    shape = ["releases.id"]
    stmt = select(Release).where(Release.project_id.in_(_visible_projects(caller.principal)))
    if project_id is not None:
        stmt = stmt.where(Release.project_id == project_id)
    if state is not None:
        stmt = stmt.where(Release.state == state)
    after = _page_after(cursor, shape)
    if after is not None:
        stmt = stmt.where(Release.id < after)
    rows, more = keyset.page(session, stmt.order_by(Release.id.desc()), limit)
    readers: dict[int, Reader] = {}
    items: list[dict[str, Any]] = []
    for release in rows:
        reader = readers.get(release.project_id)
        if reader is None:
            project = session.scalar(select(Project).where(Project.id == release.project_id))
            if project is None:
                continue
            reader = readers[release.project_id] = Reader(session, project, caller.principal)
        items.append({**reader.release_out(release), "project": {"id": reader.project.id, "name": reader.project.name}})
    return {"items": items, "next_cursor": keyset.encode_cursor(shape, [rows[-1].id]) if more else None}


@router.get("/api/releases/projects")
@feature("projects", "view", collection=True)
@on_session
def list_release_projects(session: Session, cursor: Optional[str] = None,
                          limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
                          caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """One row per visible project with a repository: Production's status and what is waiting for it."""
    shape = ["projects.id"]
    stmt = select(Project).where(
        Project.id.in_(_visible_projects(caller.principal)), Project.repo_url.is_not(None),
    )
    after = _page_after(cursor, shape)
    if after is not None:
        stmt = stmt.where(Project.id < after)
    rows, more = keyset.page(session, stmt.order_by(Project.id.desc()), limit)
    items = [Reader(session, project, caller.principal).overview() for project in rows]
    return {"items": items, "next_cursor": keyset.encode_cursor(shape, [rows[-1].id]) if more else None}
