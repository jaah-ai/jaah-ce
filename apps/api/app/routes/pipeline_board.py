"""The daily Pipeline page's API (rules: `app/pipeline/`): the stage strip and steps (`view`), a stage's items, a step's
verbs on many items (`act`), and archive/restore (`bulk`); the all-projects pair is secondary (D7).

Reads need `projects:view` on the project (a project the caller cannot see is a 404); each item is one the caller sees
on the task board. A step's verb is checked per step (`app/pipeline/gate.py::may_act`) and per item; archive and
restore per task (`tasks:edit`), delete-drafts with `tasks:delete` too. Setup is `GET`/`PUT …/pipeline`
(`app/routes/releases.py`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import adopt_user
from app.authz.catalog import PROJECTS_VIEW_KEY
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, current_caller
from app.authz.visibility import scoped_project_select
from app.db_async import on_session
from app.models.orgs import in_live_org
from app.models.projects import Project
from app.models.releases import COMMENT_MAX
from app.pipeline import actions, board as board_mod
from app.releases import health
from app.project_identity import project_or_404
from app.releases.override import REASON_MAX as OVERRIDE_REASON_MAX

router = APIRouter(tags=["pipeline"])

_NEXT_CURSOR_HEADER = "X-Next-Cursor"
#: The most projects the all-projects view reads in one call (secondary, D7).
ALL_PROJECTS_MAX = 50


class ItemFilters:
    """`GET …/pipeline/items`' board filters (`routes/tasks/listing.py`'s names), declared once for both item routes."""

    def __init__(
        self,
        q: Optional[str] = Query(None, max_length=500),
        created_by_id: Optional[list[UUID]] = Query(None, max_length=64),
        assigned_to_id: Optional[list[UUID]] = Query(None, max_length=64),
        source: Optional[list[str]] = Query(None, max_length=16),
        state: Optional[str] = None,
        updated_after: Optional[datetime] = None,
        updated_before: Optional[datetime] = None,
        focus: bool = False,
        feature_id: Optional[str] = None,
        persona: Optional[str] = Query(None, max_length=64),
        include_hidden: bool = False,
        exclude_project_id: Optional[list[int]] = Query(None, max_length=64),
        exclude_feature_id: Optional[list[UUID]] = Query(None, max_length=64),
    ) -> None:
        self.filters = board_mod.Filters(
            q=q, created_by_id=created_by_id, assigned_to_id=assigned_to_id, source=source, state=state,
            updated_after=updated_after, updated_before=updated_before, focus=focus, feature_id=feature_id,
            persona=persona, exclude_feature_id=exclude_feature_id)
        self.include_hidden = include_hidden
        #: Read by the all-projects routes alone (a project's own routes show it: it is the one picked): the user's
        #: hidden projects leave the totals and the stage lists.
        self.exclude_project_id = exclude_project_id


class FiltersIn(BaseModel):
    q: Optional[str] = Field(default=None, max_length=500)
    created_by_id: Optional[list[UUID]] = Field(default=None, max_length=64)
    assigned_to_id: Optional[list[UUID]] = Field(default=None, max_length=64)
    source: Optional[list[str]] = Field(default=None, max_length=16)
    state: Optional[str] = None
    updated_after: Optional[datetime] = None
    updated_before: Optional[datetime] = None
    focus: bool = False
    feature_id: Optional[str] = None
    persona: Optional[str] = Field(default=None, max_length=64)
    exclude_feature_id: Optional[list[UUID]] = Field(default=None, max_length=64)


class SubjectsIn(BaseModel):
    task_ids: list[int] = Field(default_factory=list, max_length=actions.SUBJECTS_MAX)
    shas: list[str] = Field(default_factory=list, max_length=actions.SUBJECTS_MAX)
    all_in_stage: bool = False
    #: `bulk` only: the stage (id) or bucket (`not_started`, `done`, `archived`) `all_in_stage` covers.
    stage: Optional[str] = None
    #: With `all_in_stage`: only the stage's items in this group (the list a step's count narrowed to).
    group: Optional[str] = Field(default=None, max_length=64)
    #: With `all_in_stage`: False leaves out what the list folds under Automatic (its fold is closed).
    folded: bool = True
    filters: Optional[FiltersIn] = None
    #: The list's Show hidden: the subjects resolve against the board it drew.
    include_hidden: bool = False

    def subjects(self) -> actions.Subjects:
        filters = board_mod.Filters(**self.filters.model_dump()) if self.filters is not None else None
        return actions.Subjects(task_ids=self.task_ids, shas=self.shas, all_in_stage=self.all_in_stage,
                                stage=self.stage, group=self.group, folded=self.folded, filters=filters,
                                include_hidden=self.include_hidden)


class ActIn(BaseModel):
    verb: Literal["go", "approve", "cancel", "retry", "reject"]
    subjects: SubjectsIn = Field(default_factory=SubjectsIn)
    scheduled_for: Optional[datetime] = None
    hotfix_sha: Optional[str] = Field(default=None, max_length=64)
    #: Approve or reject: an optional comment, stored with it (R3.6).
    comment: Optional[str] = Field(default=None, max_length=COMMENT_MAX)
    #: Run this press through a freeze (R7.5): at least 10 characters (`app/releases/override.py`).
    override_reason: Optional[str] = Field(default=None, max_length=OVERRIDE_REASON_MAX)
    #: A release step's `go`: the ticket its "ask when pressed" ticket gate checks (D8).
    ticket: Optional[str] = Field(default=None, max_length=64)


class BulkIn(BaseModel):
    verb: Literal["archive", "restore", "delete_drafts"]
    subjects: SubjectsIn


def _defer(stage: Optional[str]) -> frozenset[str]:
    """The buckets a read counts without loading (`Board.load`'s `defer`): each one it does not list."""
    return board_mod.DEFERRABLE - {stage}


def _items_body(board: board_mod.Board, response: Optional[Response], stage: Optional[str], group: Optional[str],
                after: Optional[str], limit: int) -> dict[str, Any]:
    stage_id, bucket = board_mod.stage_param(board, stage)
    picked = board_mod.select_items(board, stage_id, bucket, group)
    rows, cursor = board_mod.page(board, picked, after, limit)
    # What each stage (or bucket) of this pick folds under Automatic: a tick-all over a closed fold counts without it.
    folded: dict[str, int] = {}
    for item in picked:
        if board_mod.folded(item):
            place = board.places[item.key]
            key = str(place.stage_id) if place.stage_id is not None else place.bucket
            if key:
                folded[key] = folded.get(key, 0) + 1
    body: dict[str, Any] = {"rows": board_mod.rows_out(board, rows), "total": len(picked), "folded": folded}
    if cursor is not None:
        body["next_cursor"] = cursor
        if response is not None:
            response.headers[_NEXT_CURSOR_HEADER] = cursor
    if stage_id is not None and not after:
        target = next(s for s in board.stages if s.id == stage_id)
        releases = board_mod.releases_of(board, target)
        if releases is not None:
            body["releases"] = releases
    return body


@router.get("/api/projects/{project_id}/pipeline/view")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_pipeline_view(session: Session, project_id: int, filters: ItemFilters = Depends(ItemFilters),
                      caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """The stage strip and the steps between stages, with counts that equal each stage's items under the same
    filters as the items read (`include_hidden` too)."""
    project = project_or_404(session, project_id)
    view = board_mod.view(board_mod.Board.load(session, project, caller.principal,
                                               include_hidden=filters.include_hidden, filters=filters.filters,
                                               defer=board_mod.DEFERRABLE))
    # The view's stages are on screen: their health dots stay live (one throttled write per stage, never a probe of
    # its own). Not the all-projects view: its totals draw no dot.
    health.looked_at(session, [s["id"] for s in view["stages"]])
    return view


@router.get("/api/projects/{project_id}/pipeline/items")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_pipeline_items(
    session: Session, project_id: int, response: Response,
    stage: Optional[str] = None, group: Optional[str] = None, after: Optional[str] = None,
    limit: int = Query(default=board_mod.ITEMS_LIMIT_DEFAULT, ge=1, le=board_mod.ITEMS_LIMIT_MAX),
    filters: ItemFilters = Depends(ItemFilters),
    caller: AsyncCaller = Depends(current_caller),
) -> dict[str, Any]:
    """One stage's items (`stage=<id>|not_started|done|archived`), or every stage's in screen order."""
    project = project_or_404(session, project_id)
    board = board_mod.Board.load(session, project, caller.principal, include_hidden=filters.include_hidden,
                                 filters=filters.filters, defer=_defer(stage))
    return _items_body(board, response, stage, group, after, limit)


@router.get("/api/projects/{project_id}/pipeline/releases/{release_id}/items")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_pipeline_release_items(session: Session, project_id: int, release_id: int,
                               filters: ItemFilters = Depends(ItemFilters),
                               caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """A folded release's items, grouped by feature: those the filters keep."""
    from app.models.releases import Release  # noqa: PLC0415

    project = project_or_404(session, project_id)
    release = session.scalar(select(Release).where(Release.id == release_id, Release.project_id == project.id))
    if release is None:
        from fastapi import HTTPException  # noqa: PLC0415

        raise HTTPException(status_code=404, detail=f"unknown release {release_id}")
    board = board_mod.Board.load(session, project, caller.principal, include_hidden=filters.include_hidden,
                                 filters=filters.filters, defer=board_mod.DEFERRABLE)
    features = board_mod.release_features(board, release.id)
    by_key = {i.key: i for i in board.release_items}
    keys = [k for f in features for k in f["items"] if k in by_key]
    return {"id": release.id, "version": board_mod.version_of(release), "features": features,
            "parts": board_mod.release_parts(board, release.id),
            "run": board_mod.runs_of(session, project.id, [release]).get(release.id),
            "rows": board_mod.rows_out(board, [by_key[k] for k in keys])}


@router.post("/api/projects/{project_id}/pipeline/steps/{to_stage_id}/act")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session(deferring=True)
def act_pipeline_step(session: Session, project_id: int, to_stage_id: UUID, body: ActIn,
                      caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """A step's verb on many items (`go`, `approve`, `reject`, `cancel`, `retry`); a release step's on its release."""
    project = project_or_404(session, project_id)
    user = adopt_user(session, caller.actor)
    comment = (body.comment or "").strip() or None if body.verb in ("approve", "reject") else None
    return actions.act(session, project, caller.principal, user, to_stage_id, body.verb, body.subjects.subjects(),
                       scheduled_for=body.scheduled_for, hotfix_sha=body.hotfix_sha,
                       override_reason=body.override_reason, comment=comment, ticket=body.ticket)


@router.post("/api/projects/{project_id}/pipeline/steps/{to_stage_id}/resume", status_code=204)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session(deferring=True)
def resume_pipeline_step(session: Session, project_id: int, to_stage_id: UUID,
                         caller: AsyncCaller = Depends(current_caller)) -> Response:
    """Resume a by-itself release step paused after an off-path release (R8.3); a step not paused is left as is."""
    project = project_or_404(session, project_id)
    user = adopt_user(session, caller.actor)
    actions.resume(session, project, caller.principal, user, to_stage_id)
    return Response(status_code=204)


@router.post("/api/projects/{project_id}/pipeline/bulk")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session(deferring=True)
def bulk_pipeline_items(session: Session, project_id: int, body: BulkIn,
                        caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Archive, restore or delete drafts, per item; a live task is refused with its reason."""
    project = project_or_404(session, project_id)
    user = adopt_user(session, caller.actor)
    return actions.bulk(session, project, caller.principal, user, body.verb, body.subjects.subjects())


# ── All projects (secondary, D7) ─────────────────────────────────────────────────────────────────────

def _visible_projects(session: Session, caller: AsyncCaller, ids: Optional[list[int]],
                      excluded: Optional[list[int]] = None) -> list[Project]:
    stmt = scoped_project_select(caller.principal, key=PROJECTS_VIEW_KEY).where(in_live_org(Project.org_id))
    if ids:
        stmt = stmt.where(Project.id.in_(sorted(set(ids))))
    if excluded:
        stmt = stmt.where(Project.id.not_in(sorted(set(excluded))))
    return list(session.scalars(stmt.order_by(Project.name, Project.id).limit(ALL_PROJECTS_MAX)))


@router.get("/api/pipeline/view")
@feature("projects", "view", collection=True)
@on_session
def get_all_pipeline_view(session: Session, project_id: Optional[list[int]] = Query(None, max_length=64),
                          filters: ItemFilters = Depends(ItemFilters),
                          caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """Every visible project's view, and stage totals summed by stage name (case-insensitive, first-seen order),
    under the same filters as `GET /api/pipeline/items`."""
    projects = []
    totals: dict[str, dict[str, Any]] = {}
    for project in _visible_projects(session, caller, project_id, filters.exclude_project_id):
        view = board_mod.view(board_mod.Board.load(session, project, caller.principal,
                                                   include_hidden=filters.include_hidden, filters=filters.filters,
                                                   defer=board_mod.DEFERRABLE))
        projects.append({"project_id": project.id, "name": project.name, "view": view})
        for stage in view["stages"]:
            total = totals.setdefault(stage["name"].casefold(), {"name": stage["name"], "count": 0})
            total["count"] += stage["count"]
    return {"projects": projects, "totals": list(totals.values())}


@router.get("/api/pipeline/items")
@feature("projects", "view", collection=True)
@on_session
def get_all_pipeline_items(
    session: Session, project_id: Optional[list[int]] = Query(None, max_length=64),
    stage: Optional[str] = None, group: Optional[str] = None,
    limit: int = Query(default=board_mod.ITEMS_LIMIT_DEFAULT, ge=1, le=board_mod.ITEMS_LIMIT_MAX),
    filters: ItemFilters = Depends(ItemFilters),
    caller: AsyncCaller = Depends(current_caller),
) -> dict[str, Any]:
    """Items grouped by project: `stage` is a stage name (any case) or a bucket; each project's first page."""
    out = []
    for project in _visible_projects(session, caller, project_id, filters.exclude_project_id):
        board = board_mod.Board.load(session, project, caller.principal, include_hidden=filters.include_hidden,
                                     filters=filters.filters, defer=_defer(stage))
        stage_key = stage
        if stage and stage not in ("not_started", "done", "archived"):
            match = next((s for s in board.stages if s.name.casefold() == stage.casefold()), None)
            if match is None:
                continue
            stage_key = str(match.id)
        out.append({"project_id": project.id, "name": project.name,
                    **_items_body(board, None, stage_key, group, None, limit)})
    return {"projects": out}
