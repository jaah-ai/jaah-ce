"""Settings › Pipeline templates: the org's shared pipelines (rules: `app/releases/templates.py`).

Reads — any member who can see projects (a project editor picks a template to link to):
`GET /api/org/pipeline-templates` lists them (name, stage chain, linked-project count, state);
`GET /api/org/pipeline-templates/{id}` is one, with its draft and published version;
`GET /api/org/pipeline-templates/{id}/publish-preview` is what a publish would change, update and be refused over.

Writes — an Org Owner/Admin only (`org_admin_writer`): create, `PATCH` (name, description, draft — the draft is
checked by `pipeline_doc.check_doc` and never reaches a project), `DELETE` (409 `template_linked` while projects are
linked), `POST …/publish`, and `POST …/apply` (the published version to the linked projects still on an older one).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated, Any, Iterator, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.actors import actor_names, actor_names_for, stamp_actor
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, current_caller, org_admin_writer
from app.db_async import on_session
from app.entity_shape import add_with_org_slug, entity_fields, iso_or_none, new_uuid7, or_404
from app.models.identity import AdminUser
from app.models.releases import APPROVALS_MAX, PipelineTemplate, PipelineTemplateVersion
from app.releases import pipeline, pipeline_doc, templates
from app.routes.production_qa import _visible_projects

router = APIRouter(prefix="/api/org/pipeline-templates", tags=["releases"])

_require_templates_admin = org_admin_writer("Pipeline templates")
_NAME_MAX = 120
_DESCRIPTION_MAX = 2000


class WhoIn(BaseModel):
    roles: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=10)
    users: list[UUID] = Field(default_factory=list, max_length=50)


class StageLocksIn(BaseModel):
    branch: bool = False
    url: bool = False


class StepLocksIn(BaseModel):
    who: bool = True
    approvals: bool = True
    auto: bool = True


class StageDocIn(BaseModel):
    #: A stage's key across versions; absent on a new stage (one is minted).
    key: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(max_length=_NAME_MAX)
    #: `""` no branch, `"*"` each project's work branches, `"$integration"` each project's integration branch, else
    #: a named branch.
    branch: str = Field(default="", max_length=255)
    url: Optional[str] = Field(default=None, max_length=2048)
    locks: StageLocksIn = Field(default_factory=StageLocksIn)


class StepDocIn(BaseModel):
    who: Optional[WhoIn] = None
    approvals: int = Field(default=0, ge=0, le=APPROVALS_MAX)
    auto: bool = False
    locks: StepLocksIn = Field(default_factory=StepLocksIn)


class DocIn(BaseModel):
    stages: list[StageDocIn] = Field(max_length=pipeline.STAGES_MAX)
    steps: list[StepDocIn] = Field(max_length=pipeline.STAGES_MAX)


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    description: str = Field(default="", max_length=_DESCRIPTION_MAX)
    #: Absent: the Standard preset with the default lock marks.
    draft: Optional[DocIn] = None


class TemplatePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=_NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX)
    draft: Optional[DocIn] = None


def _org(caller: AsyncCaller) -> UUID:
    """The caller's own org: the templates are its. An org-less caller has none."""
    return or_404(getattr(caller.principal, "org_id", None), detail="no organization")


def _template_or_404(session: Session, template_id: UUID, org_id: UUID, *, lock: bool = False) -> PipelineTemplate:
    query = select(PipelineTemplate).where(PipelineTemplate.id == template_id, PipelineTemplate.org_id == org_id)
    if lock:
        query = query.with_for_update()
    return or_404(session.scalar(query), detail="unknown template")


def _draft_or_422(doc: DocIn) -> dict[str, Any]:
    """The sent draft in the stored shape, refused 422 like a save when its structure is wrong."""
    draft = templates.normalise(doc.model_dump(mode="json"))
    pipeline_doc.check_doc(draft)
    templates.check_roles(draft)
    return draft


def _refuse_name_taken(session: Session, org_id: UUID, name: str, own: Optional[UUID] = None) -> None:
    query = select(PipelineTemplate.id).where(PipelineTemplate.org_id == org_id,
                                              func.lower(PipelineTemplate.name) == name.strip().lower())
    if own is not None:
        query = query.where(PipelineTemplate.id != own)
    if session.scalar(query) is not None:
        raise HTTPException(status_code=409, detail={
            "code": "name_taken", "message": f"A template named {name.strip()} already exists. Pick another name.",
        })


@contextmanager
def _name_race(name: str) -> Iterator[None]:
    """A concurrent create or rename to the same name loses on `uq_pipeline_templates_org_name`: the same 409."""
    try:
        yield
    except IntegrityError as exc:
        if "uq_pipeline_templates_org_name" not in str(exc.orig):
            raise
        raise HTTPException(status_code=409, detail={
            "code": "name_taken", "message": f"A template named {name.strip()} already exists. Pick another name.",
        }) from None


def _version_out(version: Optional[PipelineTemplateVersion], names: dict[UUID, str]) -> Optional[dict[str, Any]]:
    if version is None:
        return None
    return {"id": str(version.id), "number": version.number, "published_at": iso_or_none(version.created_at),
            "published_by": names.get(version.created_by_id) if version.created_by_id else None}


def _summary(session: Session, row: PipelineTemplate, version: Optional[PipelineTemplateVersion],
             names: dict[UUID, str]) -> dict[str, Any]:
    return {
        **entity_fields(row, names), "slug": row.slug,
        "chain": pipeline_doc.chain([s["name"] for s in row.draft["stages"]]),
        "linked_count": templates.linked_count(session, row),
        "lagging_count": templates.lagging_count(session, row) if row.published_version_id else 0,
        "state": templates.state(row, version),
        "published": _version_out(version, names),
    }


def _visible(session: Session, caller: AsyncCaller, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The linked-project rows the caller may view (a project's name is per-project visibility); counts stay whole."""
    if not rows:
        return rows
    visible = set(session.scalars(_visible_projects(caller.principal)))
    return [r for r in rows if r["project_id"] in visible]


def _out(session: Session, row: PipelineTemplate, caller: AsyncCaller) -> dict[str, Any]:
    version = templates.published(session, row)
    names = actor_names_for(session, [row])
    if version is not None:
        names = {**names, **_publisher_names(session, [version])}
    return {
        **_summary(session, row, version, names),
        "draft": row.draft,
        "published_body": version.body if version is not None else None,
        "linked": _visible(session, caller, templates.linked_out(session, row)),
        # Each step's default roles (`who: null`), as a project's pipeline read gives them.
        "defaults": pipeline.default_roles(),
    }


def _publisher_names(session: Session, versions: list[PipelineTemplateVersion]) -> dict[UUID, str]:
    return actor_names(session, [v.created_by_id for v in versions])


@router.get("")
@feature("projects", "view", collection=True)
@on_session
def list_pipeline_templates(session: Session, caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """The org's templates by name: each one's stage chain, linked-project count, state and published version."""
    rows = list(session.scalars(select(PipelineTemplate).where(PipelineTemplate.org_id == _org(caller))
                                .order_by(func.lower(PipelineTemplate.name))))
    versions = {v.id: v for v in session.scalars(select(PipelineTemplateVersion).where(
        PipelineTemplateVersion.org_id == _org(caller), PipelineTemplateVersion.id.in_([r.published_version_id for r in rows if r.published_version_id])))}
    names = {**actor_names_for(session, rows), **_publisher_names(session, list(versions.values()))}
    return {"templates": [_summary(session, row, versions.get(row.published_version_id), names)  # type: ignore[arg-type]
                          for row in rows]}


@router.post("", status_code=201)
@feature("projects", "edit", collection=True)
@on_session
def create_pipeline_template(session: Session, body: TemplateIn, caller: AsyncCaller = Depends(current_caller),
                             admin: AdminUser = Depends(_require_templates_admin)) -> dict[str, Any]:
    """A new template, never published: its draft as sent, else the Standard preset with the default lock marks."""
    org_id = _org(caller)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name required.")
    _refuse_name_taken(session, org_id, name)
    draft = _draft_or_422(body.draft) if body.draft is not None else templates.starting_draft()
    row = PipelineTemplate(id=new_uuid7(), org_id=org_id, name=name, description=body.description.strip(),
                           draft=draft)
    stamp_actor(row, caller.actor, created=True)
    with _name_race(name):
        add_with_org_slug(session, row, name, default="template")
        session.commit()
    session.refresh(row)
    return _out(session, row, caller)


@router.get("/{template_id}")
@feature("projects", "view", collection=True)
@on_session
def get_pipeline_template(session: Session, template_id: UUID,
                          caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """One template: its draft, its published version and the projects linked to it."""
    return _out(session, _template_or_404(session, template_id, _org(caller)), caller)


@router.patch("/{template_id}")
@feature("projects", "edit", collection=True)
@on_session
def patch_pipeline_template(session: Session, template_id: UUID, body: TemplatePatch,
                            caller: AsyncCaller = Depends(current_caller),
                            admin: AdminUser = Depends(_require_templates_admin)) -> dict[str, Any]:
    """Rename, describe, or save the draft. The draft is the template's alone: no linked project changes until a
    publish."""
    org_id = _org(caller)
    row = _template_or_404(session, template_id, org_id, lock=True)
    changed = False
    if body.name is not None and body.name.strip() != row.name:
        if not body.name.strip():
            raise HTTPException(status_code=422, detail="Name required.")
        _refuse_name_taken(session, org_id, body.name, own=row.id)
        row.name, changed = body.name.strip(), True
    if body.description is not None and body.description.strip() != row.description:
        row.description, changed = body.description.strip(), True
    if body.draft is not None:
        draft = _draft_or_422(body.draft)
        if draft != row.draft:
            row.draft, changed = draft, True
    if changed:
        stamp_actor(row, caller.actor)
    with _name_race(row.name):
        session.commit()
    session.refresh(row)
    return _out(session, row, caller)


@router.delete("/{template_id}", status_code=204)
@feature("projects", "edit", collection=True)
@on_session
def delete_pipeline_template(session: Session, template_id: UUID, caller: AsyncCaller = Depends(current_caller),
                             admin: AdminUser = Depends(_require_templates_admin)) -> Response:
    """Delete a template and its versions; refused 409 `template_linked` while any project is linked to it."""
    row = _template_or_404(session, template_id, _org(caller), lock=True)
    count = templates.linked_count(session, row)
    if count:
        noun = "project is" if count == 1 else "projects are"
        raise HTTPException(status_code=409, detail={
            "code": "template_linked", "count": count,
            "message": f"{count} {noun} linked to this template. Unlink them first.",
        })
    row.published_version_id = None
    session.flush()
    session.delete(row)
    session.commit()
    return Response(status_code=204)


@router.get("/{template_id}/publish-preview")
@feature("projects", "view", collection=True)
@on_session
def preview_pipeline_template_publish(session: Session, template_id: UUID,
                                      caller: AsyncCaller = Depends(current_caller)) -> dict[str, Any]:
    """What a publish would do: the draft's changes from the published version, the linked projects it would update,
    and the org rules set to Block the template breaks (any one refuses the publish)."""
    out = templates.preview(session, _template_or_404(session, template_id, _org(caller)))
    return {**out, "linked": _visible(session, caller, out["linked"])}


@router.post("/{template_id}/publish")
@feature("projects", "edit", collection=True)
@on_session
def publish_pipeline_template(session: Session, template_id: UUID, caller: AsyncCaller = Depends(current_caller),
                              admin: AdminUser = Depends(_require_templates_admin)) -> dict[str, Any]:
    """Publish the draft as the next version and update the linked projects: `{version, updated, not_updated}`."""
    row = _template_or_404(session, template_id, _org(caller), lock=True)
    return templates.publish(session, row, caller.actor, caller.principal)



@router.post("/{template_id}/apply")
@feature("projects", "edit", collection=True)
@on_session
def apply_pipeline_template(session: Session, template_id: UUID, caller: AsyncCaller = Depends(current_caller),
                            admin: AdminUser = Depends(_require_templates_admin)) -> dict[str, Any]:
    """Update again: the published version to every linked project still on an older one (one refused before, or a
    publish cut short): `{version, updated, not_updated}` as a publish."""
    row = _template_or_404(session, template_id, _org(caller), lock=True)
    return templates.apply_lagging(session, row, caller.actor, caller.principal)
