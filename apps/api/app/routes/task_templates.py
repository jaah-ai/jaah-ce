"""Task templates — `/api/templates`, the gallery, the manage list and the template editor's API.

Two tables answer one library (`app/models/task_templates.py`): the operator's global templates and the caller
org's own. `origin` on the wire is which table a row came from. Who sees and starts what is `app/task_templates.py`;
the task a Start creates is the existing `POST /api/tasks` with a `template_id` (`routes/tasks/create.py`).

Access, every route `ORG_SCOPED`:
* READS are `ALWAYS` — every member browses, searches and stars. `_member_session` binds the caller's own org, so
  another org's template is unreadable (a 404, AC-14); drafts are read by their editors only.
* WRITES are `templates:edit`. A global template is the operator's alone (`Principal.is_operator`); an org template
  needs the key org-wide AND the org's "Templates (org)" plan switch on (`_may_edit_org`), else 403 (AC-13).
* IMAGES (`app/task_template_assets.py`): an editor uploads them (`POST …/assets`); whoever sees the template reads
  them (`GET …/assets/{asset_id}/content`); every delete or card replacement reaps through the one shared reaper.
* `refs` answers, for ids an automation stores, whether each still resolves — `deleted` (no row anywhere) or
  `unavailable` (exists, but unpublished, switched off or another org's; another org's carries no name).
"""
from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from datetime import datetime, time, timezone
from typing import Any, Iterator, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import act_as, attachment_storage, model_catalog, org_quotas, session_transcript_archive
from app import task_template_assets as assets_lib
from app import task_templates as lib
from app.actors import stamp_actor
from app.authz.catalog import TEMPLATES_EDIT_KEY, Tenancy, permission_key
from app.authz.declarations import always, feature
from app.authz.principal import Principal, current_principal
from app.clock import utcnow
from app.db_async import DbDep, on_session, release, run_in_session
from app.entity_shape import conflict_as_409, iso_or_none, or_404, refused_as, slug_taken, unique_slug, partial_of
from app.models.automations import Automation
from app.models.identity import AdminUser
from app.models.task_extras import ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.task_templates import (
    MAX_NAME,
    MAX_SUMMARY,
    PURPOSES,
    OrgTaskTemplate,
    TaskTemplate,
    TaskTemplateAsset,
    TaskTemplateFavorite,
)
from app.models.tasks import Task
from app.persona_registry import CREATABLE_PERSONAS
from app.routes.agent.task_reads import _content_disposition
from app.attachment_storage import reap_unreferenced_objects
from app.routes.attachments import (
    MAX_COMMENT_IMAGE_SIZE,
    _read_bounded,
    _stored_filename,
)
from app.task_template_assets import copy_assets_between_templates
from app.tenancy import seam
from app.tenancy.ids import is_tenant_org

router = APIRouter(prefix="/api/templates", tags=["templates"])

NOT_FOUND_DETAIL = "template not found"
#: The gallery's and the manage list's row cap.
MAX_ROWS = 200
#: How many ids one `refs` call resolves.
MAX_REFS = 100
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SLUG = 64
_SLUG_CONSTRAINTS = ("uq_task_templates_slug", "uq_org_task_templates_org_slug")
_BUILTIN_IMAGE_RE = re.compile(r"^builtin:[a-z0-9]+(?:-[a-z0-9]+)*$")
#: A search query's words: letters and digits only, so the tsquery built from them has no operator of the caller's.
_QUERY_TOKEN_RE = re.compile(r"[^\W_]+")
_MAX_QUERY_TOKENS = 8
_PURPOSE_ORDER = {key: position for position, key in enumerate(PURPOSES)}
_TASKS_CREATE_KEY = permission_key("tasks", "create")
_PROTOTYPER, _ADVISOR, _EPIC = "prototyper", "advisor", "epic"


# ── The wire shape ───────────────────────────────────────────────────────────────────────────────────


class TemplateIn(BaseModel):
    """A create. `slug` is derived from the name when absent; `image_key` only names a built-in image (an upload sets
    its own)."""

    model_config = ConfigDict(extra="forbid")

    origin: Literal["global", "org"]
    slug: Optional[str] = Field(default=None, max_length=_MAX_SLUG)
    name: str = Field(max_length=MAX_NAME * 2)
    summary: str = Field(default="", max_length=MAX_SUMMARY * 2)
    purpose: str
    title: str = Field(default="", max_length=1000)
    body: str = Field(default="", max_length=200_000)
    persona: str
    autonomous: bool = True
    model: Optional[str] = None
    image_key: Optional[str] = None
    featured: bool = False
    published: bool = False


class TemplatePatch(partial_of(TemplateIn, exclude=("origin",))):  # type: ignore[misc,valid-type]  # generated base
    """A partial edit: only the fields sent change."""


# ── Derived fields (the task form's own seeds, `TaskAxisControls.tsx`) ───────────────────────────────


def depth_of(persona: str) -> str:
    """`task` / `sketch` / `epic`, as `depthForPersona` reads a persona."""
    if persona == _EPIC:
        return "epic"
    return "sketch" if persona == _PROTOTYPER else "task"


def involvement_of(persona: str, autonomous: bool) -> str:
    """`unattended` / `guided` / `chat`, as `involvementForTask` reads a row."""
    if persona == _ADVISOR and not autonomous:
        return "chat"
    return "unattended" if autonomous else "guided"


def image_url_of(row: lib.AnyTemplate) -> Optional[str]:
    """A built-in image's static path, or an uploaded card image's content URL. None: the purpose placeholder."""
    if row.image_key and row.image_key.startswith("builtin:"):
        return f"/templates/{row.image_key.removeprefix('builtin:')}.svg"
    asset_id = assets_lib.asset_id_of(row.image_key)
    return _asset_url(row.id, asset_id) if asset_id else None


def _asset_url(template_id: UUID, asset_id: object) -> str:
    return f"/api/templates/{template_id}/assets/{asset_id}/content"


# ── Access ───────────────────────────────────────────────────────────────────────────────────────────


def _may_edit_org(principal: Principal, switch_on: bool) -> bool:
    """An org template editor: `templates:edit` held org-wide, while the org's switch is on."""
    return switch_on and principal.has_org(TEMPLATES_EDIT_KEY)


def _may_start(principal: Principal) -> bool:
    """Whether the viewer holds `tasks:create` anywhere: the gallery's Start and Customize (§6a). Start's own create
    checks the chosen project."""
    return principal.has(_TASKS_CREATE_KEY)


class _Viewer:
    """The caller, read once per request: their org, whether its switch is on, and what they may do."""

    def __init__(self, session: Session, principal: Principal) -> None:
        self.principal = principal
        self.org_id = principal.org_id
        self.switch_on = lib.org_templates_on(session, self.org_id)
        self.edits_global = principal.is_operator
        self.edits_org = _may_edit_org(principal, self.switch_on)
        self.may_start = _may_start(principal)

    def editable(self, row: lib.AnyTemplate) -> bool:
        if isinstance(row, TaskTemplate):
            return self.edits_global
        return self.edits_org and row.org_id == self.org_id

    def sees(self, row: lib.AnyTemplate) -> bool:
        """In the gallery (visible and published), or a draft its editor opens."""
        if self.editable(row):
            return True
        return row.published and lib.visible(row, self.org_id, switch_on=self.switch_on)


@contextmanager
def _member_session(session: Session, principal: Optional[Principal]) -> Iterator[tuple[Session, _Viewer]]:
    """The route's session, bound to the caller's own org — 401 without a signed-in person. The guard bound nothing
    (`ALWAYS`)."""
    if principal is None or principal.org_id is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    with seam.bound_to(principal.org_id):
        yield session, _Viewer(session, principal)


def _editor_viewer(session: Session, principal: Optional[Principal]) -> _Viewer:
    if principal is None or principal.org_id is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return _Viewer(session, principal)


def _refuse_edit(viewer: _Viewer, origin: str) -> None:
    """403 unless the viewer edits templates of `origin`, naming what is missing."""
    if origin == lib.ORIGIN_GLOBAL and not viewer.edits_global:
        raise HTTPException(status_code=403, detail="Only the operator edits global templates.")
    if origin == lib.ORIGIN_ORG and not viewer.switch_on:
        raise HTTPException(status_code=403, detail="Templates (org) is off on your plan.")
    if origin == lib.ORIGIN_ORG and not viewer.edits_org:
        raise HTTPException(status_code=403, detail="forbidden")


def _visible_or_404(session: Session, viewer: _Viewer, template_id: UUID) -> lib.AnyTemplate:
    """The row the viewer sees; a draft hidden from them and another org's template are the same 404."""
    row = lib.find(session, template_id)
    if row is None or not viewer.sees(row):
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    return row


def _editable_or_404(session: Session, viewer: _Viewer, template_id: UUID) -> lib.AnyTemplate:
    """The row the viewer edits; one they cannot see is a 404, one they see but cannot edit a 403. The org's own
    template is "seen" while the switch is off, so the refusal names the switch (AC-13), not a missing row."""
    row = lib.find(session, template_id)
    own = isinstance(row, OrgTaskTemplate) and row.org_id == viewer.org_id
    if row is None or not (own or viewer.sees(row)):
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    _refuse_edit(viewer, lib.origin_of(row))
    return row


# ── Rendering ────────────────────────────────────────────────────────────────────────────────────────


def _favorites(session: Session, viewer: _Viewer, rows: list[lib.AnyTemplate]) -> set[UUID]:
    if not rows:
        return set()
    ids = [row.id for row in rows]
    starred = session.execute(
        select(TaskTemplateFavorite.template_id, TaskTemplateFavorite.org_template_id).where(
            TaskTemplateFavorite.user_id == viewer.principal.user_id,
            or_(TaskTemplateFavorite.template_id.in_(ids), TaskTemplateFavorite.org_template_id.in_(ids)),
        )
    ).all()
    return {template_id or org_template_id for template_id, org_template_id in starred}


def _card(row: lib.AnyTemplate, viewer: _Viewer, *, favorite: bool, starts: int) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "origin": lib.origin_of(row),
        "slug": row.slug,
        "name": row.name,
        "summary": row.summary,
        "purpose": row.purpose,
        "persona": row.persona,
        "depth": depth_of(row.persona),
        "involvement": involvement_of(row.persona, row.autonomous),
        "image_url": image_url_of(row),
        "featured": row.featured,
        "published": row.published,
        "favorite": favorite,
        "starts_90d": starts,
        "editable": viewer.editable(row),
        "may_start": viewer.may_start,
        # What Start asks for: the body's input placeholders (`task_templates.inputs_of`).
        "inputs": [{"label": i.label, "required": i.required} for i in lib.inputs_of(row.body)],
    }


def _cards(session: Session, viewer: _Viewer, rows: list[lib.AnyTemplate]) -> list[dict[str, Any]]:
    favorites = _favorites(session, viewer, rows)
    starts = lib.starts_90d(session, rows)
    return [_card(row, viewer, favorite=row.id in favorites, starts=starts.get(row.id, 0)) for row in rows]


def _template_assets(session: Session, row: lib.AnyTemplate) -> list[assets_lib.AnyAsset]:
    if isinstance(row, TaskTemplate):
        return assets_lib.assets_of(session, template_id=row.id, org_template_id=None)
    return assets_lib.assets_of(session, template_id=None, org_template_id=row.id)


def _asset_kind(row: lib.AnyTemplate, asset: assets_lib.AnyAsset) -> str:
    """A global asset's own `kind`; an org template's row has none of its own, so its card is the one `image_key`
    names."""
    if isinstance(asset, TaskTemplateAsset):
        return asset.kind
    return assets_lib.CARD_IMAGE if assets_lib.asset_id_of(row.image_key) == str(asset.id) else assets_lib.BODY_IMAGE


def _asset_out(row: lib.AnyTemplate, asset: assets_lib.AnyAsset) -> dict[str, Any]:
    return {
        "id": str(asset.id), "kind": _asset_kind(row, asset), "filename": asset.filename,
        "content_type": asset.content_type,
    }


def _assets(session: Session, row: lib.AnyTemplate) -> list[dict[str, Any]]:
    return [_asset_out(row, asset) for asset in _template_assets(session, row)]


def _detail(session: Session, viewer: _Viewer, row: lib.AnyTemplate) -> dict[str, Any]:
    (card,) = _cards(session, viewer, [row])
    return {
        **card,
        "title": row.title,
        "body": row.body,
        "autonomous": row.autonomous,
        "model": row.model,
        "assets": _assets(session, row),
        "created_at": iso_or_none(row.created_at),
        "updated_at": iso_or_none(row.updated_at),
    }


# ── Search ───────────────────────────────────────────────────────────────────────────────────────────


def _tsquery(q: str) -> Optional[str]:
    """The caller's words as a tsquery — every word required, the last one a prefix so a word still being typed
    matches ("flak" finds "flaky"). None when no word is left."""
    tokens = _QUERY_TOKEN_RE.findall(q.lower())[:_MAX_QUERY_TOKENS]
    if not tokens:
        return None
    return " & ".join([*tokens[:-1], f"{tokens[-1]}:*"])


def _sorted_by_purpose(rows: list[lib.AnyTemplate]) -> list[lib.AnyTemplate]:
    return sorted(rows, key=lambda row: (_PURPOSE_ORDER.get(row.purpose, len(_PURPOSE_ORDER)), row.name.lower(), row.id))


def _rows(
    session: Session,
    viewer: _Viewer,
    *,
    q: Optional[str],
    global_where: list[Any],
    org_where: Optional[list[Any]],
) -> list[lib.AnyTemplate]:
    """Both tables under their filters (`org_where` None leaves org rows out), ranked by `ts_rank_cd` over one
    UNION ALL when `q` has words, else in purpose order then name. At most `MAX_ROWS`."""
    query = _tsquery(q) if q else None
    if q and query is None:
        return []
    if query is None:
        rows: list[lib.AnyTemplate] = list(session.scalars(select(TaskTemplate).where(*global_where)).all())
        if org_where is not None:
            rows += session.scalars(select(OrgTaskTemplate).where(*org_where)).all()
        return _sorted_by_purpose(rows)[:MAX_ROWS]
    tsq = func.to_tsquery("english", query)
    ranked = [
        select(
            TaskTemplate.id.label("id"), literal(lib.ORIGIN_GLOBAL).label("origin"),
            func.ts_rank_cd(TaskTemplate.tsv, tsq).label("rank"), TaskTemplate.name.label("name"),
        ).where(TaskTemplate.tsv.op("@@")(tsq), *global_where)
    ]
    if org_where is not None:
        ranked.append(
            select(
                OrgTaskTemplate.id.label("id"), literal(lib.ORIGIN_ORG).label("origin"),
                func.ts_rank_cd(OrgTaskTemplate.tsv, tsq).label("rank"), OrgTaskTemplate.name.label("name"),
            ).where(OrgTaskTemplate.tsv.op("@@")(tsq), *org_where)
        )
    hits = union_all(*ranked).subquery()
    order = session.execute(
        select(hits.c.id, hits.c.origin).order_by(hits.c.rank.desc(), hits.c.name, hits.c.id).limit(MAX_ROWS)
    ).all()
    global_rows = {row.id: row for row in session.scalars(
        select(TaskTemplate).where(TaskTemplate.id.in_([i for i, o in order if o == lib.ORIGIN_GLOBAL]))
    ).all()}
    org_rows = {row.id: row for row in session.scalars(
        select(OrgTaskTemplate).where(OrgTaskTemplate.id.in_([i for i, o in order if o == lib.ORIGIN_ORG]))
    ).all()}
    found = [global_rows.get(i) if o == lib.ORIGIN_GLOBAL else org_rows.get(i) for i, o in order]
    return [row for row in found if row is not None]


def _purpose_or_422(purpose: Optional[str]) -> None:
    if purpose is not None and purpose not in PURPOSES:
        raise HTTPException(status_code=422, detail=f"purpose must be one of {', '.join(PURPOSES)}")


# ── Reads ────────────────────────────────────────────────────────────────────────────────────────────


@router.get("")
@always(Tenancy.ORG_SCOPED)
@on_session
def list_templates(
    session: Session,
    principal: Optional[Principal] = Depends(current_principal),
    q: Optional[str] = Query(None, max_length=200),
    purpose: Optional[str] = Query(None),
    favorites: bool = Query(False),
) -> list[dict[str, Any]]:
    """The gallery: published, visible templates — global, plus the caller org's own while its switch is on."""
    _purpose_or_422(purpose)
    with _member_session(session, principal) as (session, viewer):
        global_where: list[Any] = [TaskTemplate.published.is_(True)]
        org_where: Optional[list[Any]] = (
            [OrgTaskTemplate.published.is_(True), OrgTaskTemplate.org_id == viewer.org_id] if viewer.switch_on else None
        )
        if purpose is not None:
            global_where.append(TaskTemplate.purpose == purpose)
            if org_where is not None:
                org_where.append(OrgTaskTemplate.purpose == purpose)
        if favorites:
            starred = select(TaskTemplateFavorite.id).where(TaskTemplateFavorite.user_id == viewer.principal.user_id)
            global_where.append(starred.where(TaskTemplateFavorite.template_id == TaskTemplate.id).exists())
            if org_where is not None:
                org_where.append(starred.where(TaskTemplateFavorite.org_template_id == OrgTaskTemplate.id).exists())
        return _cards(session, viewer, _rows(session, viewer, q=q, global_where=global_where, org_where=org_where))


@router.get("/manage")
@always(Tenancy.ORG_SCOPED)
@on_session
def manage_templates(
    session: Session,
    principal: Optional[Principal] = Depends(current_principal),
    q: Optional[str] = Query(None, max_length=200),
    origin: Optional[Literal["global", "org"]] = Query(None),
) -> list[dict[str, Any]]:
    """Every template the caller edits, drafts included: the operator's global ones, and the caller org's own for an
    org editor (switch on). 403 for anyone who edits neither."""
    with _member_session(session, principal) as (session, viewer):
        if not (viewer.edits_global or viewer.edits_org):
            raise HTTPException(status_code=403, detail="forbidden")
        global_where: list[Any] = [] if viewer.edits_global and origin != lib.ORIGIN_ORG else [TaskTemplate.id.is_(None)]
        org_where = (
            [OrgTaskTemplate.org_id == viewer.org_id] if viewer.edits_org and origin != lib.ORIGIN_GLOBAL else None
        )
        return _cards(session, viewer, _rows(session, viewer, q=q, global_where=global_where, org_where=org_where))


@router.get("/refs")
@always(Tenancy.ORG_SCOPED)
async def template_refs(
    db: DbDep,
    principal: Optional[Principal] = Depends(current_principal),
    ids: str = Query("", max_length=MAX_REFS * 37, description="Comma-separated template ids."),
) -> list[dict[str, Any]]:
    """Whether each id still resolves for the caller org (`ok`), is gone (`deleted`) or exists but does not resolve
    (`unavailable`: unpublished, switched off or another org's — whose name is withheld)."""
    try:
        wanted = list(dict.fromkeys(UUID(part.strip()) for part in ids.split(",") if part.strip()))
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be comma-separated template ids") from None
    if len(wanted) > MAX_REFS:
        raise HTTPException(status_code=422, detail=f"at most {MAX_REFS} ids")
    globals_, own, viewer = await db.run_sync(_refs_rows, principal, wanted)
    # The fleet read opens a session of its own: the route's hands its connection back first (one per task).
    await release(db)
    others = await _foreign_template_ids([i for i in wanted if i not in globals_ and i not in own])
    out = []
    for template_id in wanted:
        row = globals_.get(template_id) or own.get(template_id)
        if row is not None:
            ok = row.published and lib.visible(row, viewer.org_id, switch_on=viewer.switch_on)
            # A global draft's name is its editors' only; the org's own row is the org's to read.
            named = ok or isinstance(row, OrgTaskTemplate) or viewer.editable(row)
            out.append({
                "id": str(template_id), "status": "ok" if ok else "unavailable", "name": row.name if named else None,
            })
        elif template_id in others:
            out.append({"id": str(template_id), "status": "unavailable", "name": None})
        else:
            out.append({"id": str(template_id), "status": "deleted", "name": None})
    return out


def _refs_rows(
    session: Session, principal: Optional[Principal], wanted: list[UUID]
) -> tuple[dict[UUID, TaskTemplate], dict[UUID, OrgTaskTemplate], _Viewer]:
    """`template_refs`'s org-bound read, over the route's async session (`run_sync`)."""
    with _member_session(session, principal) as (session, viewer):
        globals_ = {row.id: row for row in session.scalars(select(TaskTemplate).where(TaskTemplate.id.in_(wanted)))}
        own = {row.id: row for row in session.scalars(select(OrgTaskTemplate).where(OrgTaskTemplate.id.in_(wanted)))}
    return globals_, own, viewer


async def _foreign_template_ids(ids: list[UUID]) -> set[UUID]:
    """Which of `ids` name another org's template — existence only, never a column of the row: `refs` must tell
    "deleted" from "not yours" for an automation's stored id (AC-15) without leaking the other org's name."""
    if not ids:
        return set()
    with seam.detached(), seam.operator_scope():
        return await run_in_session(_template_ids_in, ids)


def _template_ids_in(fleet: Session, ids: list[UUID]) -> set[UUID]:
    return set(fleet.scalars(select(OrgTaskTemplate.id).where(OrgTaskTemplate.id.in_(ids))).all())


@router.get("/{template_id}")
@always(Tenancy.ORG_SCOPED)
@on_session
def get_template(
    session: Session,
    template_id: UUID,
    principal: Optional[Principal] = Depends(current_principal),
) -> dict[str, Any]:
    """One template with its task fields; a draft for its editors only, another org's for nobody (404)."""
    with _member_session(session, principal) as (session, viewer):
        row = _visible_or_404(session, viewer, template_id)
        return _detail(session, viewer, row)


@router.get("/{template_id}/usage")
@always(Tenancy.ORG_SCOPED)
async def template_usage(
    db: DbDep, template_id: UUID, principal: Optional[Principal] = Depends(current_principal)
) -> dict[str, int]:
    """What a delete would touch, for its confirm (AC-4): the automations naming the template and the tasks started
    from it in the popularity window. A global template's are counted across every org (the operator's read)."""
    org_usage, global_id = await db.run_sync(_template_usage, template_id, principal)
    if org_usage is not None:
        return org_usage
    # The fleet read opens a session of its own: the route's hands its connection back first (one per task).
    await release(db)
    return await _global_usage(global_id)


def _template_usage(
    session: Session, template_id: UUID, principal: Optional[Principal]
) -> tuple[Optional[dict[str, int]], UUID]:
    """`template_usage`'s org-bound part, over the route's async session (`run_sync`): an org template's counts, or
    None and the global template's id (admitted for the operator) for `_global_usage`."""
    with _member_session(session, principal) as (session, viewer):
        row = _editable_or_404(session, viewer, template_id)
        if isinstance(row, OrgTaskTemplate):
            return _usage(session, row.id, Task.org_template_id), row.id
    return None, row.id


def _usage(session: Session, template_id: UUID, task_column: Any) -> dict[str, int]:
    since = datetime.combine(lib.window_start(), time.min, tzinfo=timezone.utc)
    automations = session.scalar(
        select(func.count()).select_from(Automation).where(
            Automation.action_params["template_id"].astext == str(template_id)
        )
    )
    tasks = session.scalar(
        select(func.count()).select_from(Task).where(task_column == template_id, Task.created_at >= since)
    )
    return {"automations": int(automations or 0), "tasks_90d": int(tasks or 0)}


async def _global_usage(template_id: UUID) -> dict[str, int]:
    """CALL ONLY AFTER `_editable_or_404` HAS ADMITTED AN OPERATOR: it opens a fleet session. `_usage` for a global template, on a fleet session: its automations and tasks live in every org. Reached only
    by the operator (`_editable_or_404` refused everyone else); only two counts leave."""
    with seam.detached(), seam.operator_scope():
        return await run_in_session(_usage, template_id, Task.template_id)


# ── Mutations ────────────────────────────────────────────────────────────────────────────────────────


def _slug_scope(origin: str, org_id: Optional[UUID]) -> tuple[type, tuple]:
    """The table a template's slug is unique in, and the rows it is unique among: the whole global table, or one
    org's rows of the org table (`entity_shape.slug_taken` / `unique_slug` take both); a suffixed slug must still
    fit the column, so `unique_slug` is asked with `max_len=_MAX_SLUG`."""
    if origin == lib.ORIGIN_GLOBAL:
        return TaskTemplate, ()
    return OrgTaskTemplate, (OrgTaskTemplate.org_id == org_id,)


def _clean(fields: dict[str, Any], origin: str) -> dict[str, Any]:
    """Trim and check the submitted fields; a 422 names the field."""
    out = dict(fields)
    for name in ("name", "summary", "title"):
        if name in out and out[name] is not None:
            out[name] = " ".join(out[name].split())
    if "name" in out:
        if not out["name"]:
            raise HTTPException(status_code=422, detail="name is required")
        if len(out["name"]) > MAX_NAME:
            raise HTTPException(status_code=422, detail=f"name: at most {MAX_NAME} characters")
    if "summary" in out and len(out["summary"] or "") > MAX_SUMMARY:
        raise HTTPException(status_code=422, detail=f"summary: at most {MAX_SUMMARY} characters")
    if "purpose" in out and out["purpose"] not in PURPOSES:
        raise HTTPException(status_code=422, detail=f"purpose must be one of {', '.join(PURPOSES)}")
    if "persona" in out and out["persona"] not in CREATABLE_PERSONAS:
        raise HTTPException(status_code=422, detail=f"persona must be one of {', '.join(CREATABLE_PERSONAS)}")
    if "slug" in out and out["slug"] is not None and not _SLUG_RE.match(out["slug"]):
        raise HTTPException(status_code=422, detail="slug: lowercase letters, digits and single hyphens")
    if "model" in out:
        with refused_as(422, ValueError, chain=True, prefix="model: "):
            out["model"] = model_catalog.validate_model(out["model"]) or None
    if out.get("image_key") is not None:
        if origin != lib.ORIGIN_GLOBAL or not _BUILTIN_IMAGE_RE.match(out["image_key"]):
            raise HTTPException(status_code=422, detail="image_key: only a built-in image (builtin:<slug>)")
    for name in ("name", "summary", "purpose", "title", "body", "persona", "autonomous", "featured", "published"):
        if name in out and out[name] is None:
            raise HTTPException(status_code=422, detail=f"{name} cannot be null")
    return out


def _model_bar_or_403(viewer: _Viewer, fields: dict[str, Any]) -> None:
    """A template's model pin is applied to every task started from it without the starter's own bar
    (`routes/tasks/create.py::_apply_template`), so whoever pins it clears that bar here: an org admin in a tenant
    org, a superadmin on the operator plane (`_model_pin_or_refuse`'s rule)."""
    if fields.get("model") is None:
        return
    principal = viewer.principal
    if not (principal.is_org_admin if is_tenant_org(principal.org_id) else principal.is_superadmin):
        raise HTTPException(status_code=403, detail="Only an administrator sets a template's model.")


def _publishable_or_422(row: lib.AnyTemplate) -> None:
    """A published template starts a task in one click, so it needs what a task needs."""
    if not row.published:
        return
    for name in ("summary", "title", "body"):
        if not (getattr(row, name) or "").strip():
            raise HTTPException(status_code=422, detail=f"{name} is required to publish")


def _actor(session: Session, principal: Principal) -> AdminUser:
    user = act_as.own_row(session, principal)
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def _commit_or_slug_taken(session: Session) -> None:
    """Commit, a slug lost to a concurrent save being the same 409 as one seen before it."""
    with conflict_as_409(session, "slug: already used", constraints=_SLUG_CONSTRAINTS):
        session.commit()


@router.post("", status_code=201)
@feature("templates", "edit")
@on_session
def create_template(
    session: Session,
    body: TemplateIn,
    principal: Optional[Principal] = Depends(current_principal),
) -> dict[str, Any]:
    """A new template, a draft unless sent `published`: global (the operator) or the caller org's (an org editor)."""
    viewer = _editor_viewer(session, principal)
    _refuse_edit(viewer, body.origin)
    fields = _clean(body.model_dump(exclude={"origin"}), body.origin)
    _model_bar_or_403(viewer, fields)
    org_id = viewer.org_id if body.origin == lib.ORIGIN_ORG else None
    model, where = _slug_scope(body.origin, org_id)
    if fields["slug"] is None:
        fields["slug"] = unique_slug(
            session, model, fields["name"], default="template", where=where, limit=_MAX_SLUG, max_len=_MAX_SLUG,
        )
    elif slug_taken(session, model, fields["slug"], where=where):
        raise HTTPException(status_code=409, detail="slug: already used")
    row: lib.AnyTemplate = TaskTemplate(**fields) if org_id is None else OrgTaskTemplate(org_id=org_id, **fields)
    _publishable_or_422(row)
    stamp_actor(row, _actor(session, viewer.principal), created=True)
    session.add(row)
    _commit_or_slug_taken(session)
    session.refresh(row)
    return _detail(session, viewer, row)


@router.patch("/{template_id}")
@feature("templates", "edit")
@on_session
def update_template(
    session: Session,
    template_id: UUID,
    body: TemplatePatch,
    principal: Optional[Principal] = Depends(current_principal),
) -> dict[str, Any]:
    viewer = _editor_viewer(session, principal)
    row = _editable_or_404(session, viewer, template_id)
    origin = lib.origin_of(row)
    fields = _clean(body.model_dump(exclude_unset=True), origin)
    _model_bar_or_403(viewer, fields)
    model, where = _slug_scope(origin, getattr(row, "org_id", None))
    if fields.get("slug") is None:
        fields.pop("slug", None)
    elif slug_taken(session, model, fields["slug"], where=where, exclude=row.id):
        raise HTTPException(status_code=409, detail="slug: already used")
    for name, value in fields.items():
        setattr(row, name, value)
    _publishable_or_422(row)
    stamp_actor(row, _actor(session, viewer.principal))
    _commit_or_slug_taken(session)
    session.refresh(row)
    return _detail(session, viewer, row)


@router.delete("/{template_id}", status_code=204)
@feature("templates", "edit")
async def delete_template(
    db: DbDep,
    template_id: UUID,
    principal: Optional[Principal] = Depends(current_principal),
) -> Response:
    """A hard delete. Stars, counters and images go with it; tasks started from it keep its name (D15) and its
    images, whose bytes the reaper spares while a task still names them."""
    doomed = await db.run_sync(_delete_template, template_id, principal)
    await asyncio.to_thread(reap_unreferenced_objects, doomed)
    return Response(status_code=204)


def _delete_template(
    session: Session,
    template_id: UUID,
    principal: Optional[Principal],
) -> list[tuple[Optional[str], str]]:
    """`delete_template`'s body, over the route's async session (`run_sync`): commits the delete and returns the
    stored keys `reap_unreferenced_objects` takes after it."""
    viewer = _editor_viewer(session, principal)
    row = _editable_or_404(session, viewer, template_id)
    doomed = _stored_keys(_template_assets(session, row))
    session.delete(row)
    session.commit()
    return doomed


@router.post("/{template_id}/duplicate", status_code=201)
@feature("templates", "edit")
@on_session
def duplicate_template(
    session: Session,
    template_id: UUID,
    principal: Optional[Principal] = Depends(current_principal),
) -> dict[str, Any]:
    """A draft copy in the caller's org of a global template or one of the org's own (D11), images included."""
    viewer = _editor_viewer(session, principal)
    source = _visible_or_404(session, viewer, template_id)
    _refuse_edit(viewer, lib.ORIGIN_ORG)
    suffix = " copy"
    copy = OrgTaskTemplate(
        org_id=viewer.org_id,
        slug=unique_slug(session, OrgTaskTemplate, source.slug, default="template",
                         where=(OrgTaskTemplate.org_id == viewer.org_id,), limit=_MAX_SLUG, max_len=_MAX_SLUG),
        name=source.name[: MAX_NAME - len(suffix)].rstrip() + suffix,
        summary=source.summary,
        purpose=source.purpose,
        title=source.title,
        body=source.body,
        persona=source.persona,
        autonomous=source.autonomous,
        model=source.model,
        image_key=source.image_key if (source.image_key or "").startswith("builtin:") else None,
        featured=False,
        published=False,
    )
    stamp_actor(copy, _actor(session, viewer.principal), created=True)
    session.add(copy)
    session.flush()
    copy_assets_between_templates(session, source_id=source.id, dest_org_template_id=copy.id)
    _commit_or_slug_taken(session)
    session.refresh(copy)
    return _detail(session, viewer, copy)


@router.put("/{template_id}/favorite", status_code=204)
@always(Tenancy.ORG_SCOPED)
@on_session
def favorite_template(
    session: Session,
    template_id: UUID,
    principal: Optional[Principal] = Depends(current_principal),
) -> Response:
    """Star a template the caller sees; idempotent."""
    with _member_session(session, principal) as (session, viewer):
        row = _visible_or_404(session, viewer, template_id)
        column = "template_id" if isinstance(row, TaskTemplate) else "org_template_id"
        session.execute(
            insert(TaskTemplateFavorite)
            .values(org_id=viewer.org_id, user_id=viewer.principal.user_id, **{column: row.id})
            .on_conflict_do_nothing()
        )
        session.commit()
    return Response(status_code=204)


@router.delete("/{template_id}/favorite", status_code=204)
@always(Tenancy.ORG_SCOPED)
@on_session
def unfavorite_template(
    session: Session,
    template_id: UUID,
    principal: Optional[Principal] = Depends(current_principal),
) -> Response:
    """Unstar; idempotent. No visibility check: a star on a template since unpublished or switched off still comes
    off, and the delete reaches the caller's own stars only."""
    with _member_session(session, principal) as (session, viewer):
        session.execute(
            delete(TaskTemplateFavorite).where(
                TaskTemplateFavorite.user_id == viewer.principal.user_id,
                or_(TaskTemplateFavorite.template_id == template_id, TaskTemplateFavorite.org_template_id == template_id),
            )
        )
        session.commit()
    return Response(status_code=204)


# ── Images ───────────────────────────────────────────────────────────────────────────────────────────


def _stored_keys(rows: list[assets_lib.AnyAsset]) -> list[tuple[Optional[str], str]]:
    """`(bucket, key)` of each row with bytes, read BEFORE the delete (the instances expire on commit)."""
    return [(row.s3_bucket, row.s3_key) for row in rows if row.s3_key]


def _find_asset(session: Session, row: lib.AnyTemplate, asset_id: str) -> Optional[assets_lib.AnyAsset]:
    """`row`'s confirmed asset `asset_id` (a uuid for a global template's, an integer for an org template's), else
    None — a malformed id included."""
    try:
        if isinstance(row, TaskTemplate):
            return session.scalars(select(TaskTemplateAsset).where(
                TaskTemplateAsset.id == UUID(asset_id), TaskTemplateAsset.template_id == row.id,
                TaskTemplateAsset.uploaded_at.is_not(None),
            )).first()
        number = int(asset_id)
        if not 0 < number < 2**63:
            return None
        return session.scalars(select(TaskAttachment).where(
            TaskAttachment.id == number, TaskAttachment.org_template_id == row.id,
            TaskAttachment.uploaded_at.is_not(None),
        )).first()
    except ValueError:
        return None


def _free_filename(taken: set[str], filename: str) -> str:
    """`filename`, else `name-2.ext`, `name-3.ext`, … — a template's body names an image by filename, so two may
    not share one."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    candidate, number = filename, 1
    while candidate in taken:
        number += 1
        candidate = f"{stem}-{number}{dot}{ext}"
    return candidate


def _target_or_503(bucket: Optional[str]) -> attachment_storage.Target:
    where = attachment_storage.target(bucket)
    if where is None:
        raise HTTPException(status_code=503, detail="Image storage is not configured on this control plane.")
    return where


@router.post("/{template_id}/assets", status_code=201)
@feature("templates", "edit")
async def upload_template_asset(
    db: DbDep,
    template_id: UUID,
    kind: str = Form(...),
    file: UploadFile = File(...),
    principal: Optional[Principal] = Depends(current_principal),
) -> dict[str, Any]:
    """Store one image on a template the caller edits: its card image (at most `MAX_CARD_IMAGE_SIZE`; png, jpeg,
    webp or svg — it replaces the previous one, whose bytes are reaped) or one pasted into its body (at most
    `MAX_COMMENT_IMAGE_SIZE`; raster). The type is read from the bytes, never the claim. Buffered in-process: both
    caps sit far under `MAX_BUFFERED_ATTACHMENT_SIZE`."""
    viewer, row, where, cap, existing_names = await db.run_sync(_upload_admission, template_id, kind, principal)
    # Every check so far was a read: its connection goes back before the body is buffered.
    await release(db)
    data = await asyncio.to_thread(_read_bounded, file.file, cap)
    pending, user = await db.run_sync(_upload_pending, viewer, row, kind, file.filename, data, where, existing_names)
    try:
        etag = await asyncio.to_thread(attachment_storage.put, where, pending.s3_key, data)
    except attachment_storage.StorageUnavailable as exc:
        # The pending row names no bytes; nothing else would ever remove it.
        await db.run_sync(_drop_pending, pending)
        raise HTTPException(
            status_code=502, detail=f"Image storage did not accept the upload ({type(exc).__name__}). Try again.",
        ) from None
    out, replaced = await db.run_sync(_upload_confirm, row, kind, pending, etag, user)
    await asyncio.to_thread(reap_unreferenced_objects, replaced)
    return out


def _upload_admission(
    session: Session,
    template_id: UUID,
    kind: str,
    principal: Optional[Principal],
) -> tuple[_Viewer, lib.AnyTemplate, attachment_storage.Target, int, set[str]]:
    """`upload_template_asset`'s checks before the body is read, over the route's async session (`run_sync`): the
    viewer, the template, the target bucket, the byte cap, and the filenames already taken. The instances stay in
    the session's identity map for the later steps (`expire_on_commit=False`)."""
    viewer = _editor_viewer(session, principal)
    row = _editable_or_404(session, viewer, template_id)
    if kind not in (assets_lib.CARD_IMAGE, assets_lib.BODY_IMAGE):
        raise HTTPException(status_code=422, detail=f"kind must be {assets_lib.CARD_IMAGE} or {assets_lib.BODY_IMAGE}")
    card = kind == assets_lib.CARD_IMAGE
    org_id = row.org_id if isinstance(row, OrgTaskTemplate) else None
    # Asked before reading the body, so a bucketless plane refuses without buffering.
    bucket = session_transcript_archive.org_bucket(session, org_id) if org_id is not None else None
    where = _target_or_503(bucket)
    existing = _template_assets(session, row)
    # A new card replaces the current one, so it does not add to the count.
    replacing = card and assets_lib.asset_id_of(row.image_key) is not None
    if len(existing) - int(replacing) >= assets_lib.MAX_ASSETS_PER_TEMPLATE:
        raise HTTPException(
            status_code=409, detail=f"A template holds at most {assets_lib.MAX_ASSETS_PER_TEMPLATE} images. Delete one first.",
        )
    cap = assets_lib.MAX_CARD_IMAGE_SIZE if card else MAX_COMMENT_IMAGE_SIZE
    # An org's own file size cap binds its template images too (`org_quotas.file_cap_bytes`).
    cap = cap if org_id is None else min(cap, org_quotas.file_cap_bytes(session, org_id))
    return viewer, row, where, cap, {a.filename for a in existing}


def _upload_pending(
    session: Session,
    viewer: _Viewer,
    row: lib.AnyTemplate,
    kind: str,
    upload_filename: Optional[str],
    data: bytes,
    where: attachment_storage.Target,
    existing_names: set[str],
) -> tuple[assets_lib.AnyAsset, AdminUser]:
    """`upload_template_asset`'s pending row, over the route's async session (`run_sync`): the type read from the
    bytes, then the row committed with `uploaded_at` NULL (read by nothing) so no transaction is held across the
    PUT."""
    card = kind == assets_lib.CARD_IMAGE
    allowed = assets_lib.CARD_IMAGE_TYPES if card else assets_lib.BODY_IMAGE_TYPES
    content_type = assets_lib.sniffed_type(data)
    if content_type not in allowed:
        names = ", ".join(t.removeprefix("image/").removesuffix("+xml") for t in allowed)
        raise HTTPException(status_code=415, detail=f"A {kind.replace('_', ' ')} must be one of: {names}.")
    org_id = row.org_id if isinstance(row, OrgTaskTemplate) else None
    filename = _free_filename(existing_names, _stored_filename(upload_filename, "image"))
    user = _actor(session, viewer.principal)
    asset: assets_lib.AnyAsset
    if org_id is None:
        asset = TaskTemplateAsset(
            template_id=row.id, kind=kind, filename=filename, content_type=content_type, size_bytes=len(data),
            uploaded_by_id=user.id,
        )
        session.add(asset)
        session.flush()
        asset.s3_key = attachment_storage.template_asset_key(template_id=row.id, asset_id=asset.id)
    else:
        asset = TaskAttachment(
            org_id=org_id, org_template_id=row.id, kind=ATTACHMENT_KIND_FILE, filename=filename,
            content_type=content_type, size_bytes=len(data), uploaded_by_id=user.id,
        )
        session.add(asset)
        session.flush()
        asset.s3_key = attachment_storage.object_key(org_template_id=row.id, attachment_id=asset.id)
    asset.s3_bucket = where.bucket
    session.commit()
    return asset, user


def _drop_pending(session: Session, asset: assets_lib.AnyAsset) -> None:
    """Delete the pending row a failed PUT left, over the route's async session (`run_sync`)."""
    session.delete(asset)
    session.commit()


def _upload_confirm(
    session: Session,
    row: lib.AnyTemplate,
    kind: str,
    asset: assets_lib.AnyAsset,
    etag: Optional[str],
    user: AdminUser,
) -> tuple[dict[str, Any], list[tuple[Optional[str], str]]]:
    """`upload_template_asset`'s confirm after the PUT, over the route's async session (`run_sync`): the row marked
    uploaded (a card swapped in), committed; returns the response and the replaced card's keys for
    `reap_unreferenced_objects`."""
    asset.uploaded_at = utcnow()
    if isinstance(asset, TaskAttachment):
        asset.etag = etag or None
    replaced: list[tuple[Optional[str], str]] = []
    if kind == assets_lib.CARD_IMAGE:
        # Locked, so two concurrent card uploads swap one after the other and the loser's old card is the winner's.
        session.refresh(row, with_for_update=True)
        old = _find_asset(session, row, assets_lib.asset_id_of(row.image_key) or "")
        if old is not None:
            replaced = _stored_keys([old])
            session.delete(old)
        row.image_key = f"{assets_lib.ASSET_PREFIX}{asset.id}"
        stamp_actor(row, user)
    session.commit()
    return {**_asset_out(row, asset), "url": _asset_url(row.id, asset.id)}, replaced


@router.delete("/{template_id}/assets/{asset_id}", status_code=204)
@feature("templates", "edit")
async def delete_template_asset(
    db: DbDep,
    template_id: UUID,
    asset_id: str,
    principal: Optional[Principal] = Depends(current_principal),
) -> Response:
    """Remove one image; removing the card image leaves the purpose placeholder. Tasks that copied it keep it."""
    doomed = await db.run_sync(_delete_template_asset, template_id, asset_id, principal)
    await asyncio.to_thread(reap_unreferenced_objects, doomed)
    return Response(status_code=204)


def _delete_template_asset(
    session: Session,
    template_id: UUID,
    asset_id: str,
    principal: Optional[Principal],
) -> list[tuple[Optional[str], str]]:
    """`delete_template_asset`'s body, over the route's async session (`run_sync`): commits the delete and returns the
    stored keys `reap_unreferenced_objects` takes after it."""
    viewer = _editor_viewer(session, principal)
    row = _editable_or_404(session, viewer, template_id)
    asset = or_404(_find_asset(session, row, asset_id), detail="image not found")
    if assets_lib.asset_id_of(row.image_key) == str(asset.id):
        row.image_key = None
        stamp_actor(row, _actor(session, viewer.principal))
    doomed = _stored_keys([asset])
    session.delete(asset)
    session.commit()
    return doomed


@router.get("/{template_id}/assets/{asset_id}/content", response_model=None)
@always(Tenancy.ORG_SCOPED)
async def template_asset_content(
    db: DbDep, template_id: UUID, asset_id: str, principal: Optional[Principal] = Depends(current_principal)
) -> Response:
    """One image's bytes, for whoever sees the template (another org's is a 404). Raster goes by a presigned
    `inline` redirect signed with its type (`routes/attachments.py::user_task_attachment_content`'s shape); SVG is
    proxied under `SVG_HEADERS`, which S3 cannot attach."""
    key, bucket, filename, content_type = await db.run_sync(_asset_content_ref, template_id, asset_id, principal)
    # A read: its connection goes back before the S3 GET.
    await release(db)
    where = attachment_storage.target(bucket)
    if where is None:
        raise HTTPException(status_code=502, detail="This control plane cannot read image storage.")
    if content_type == assets_lib.SVG:
        try:
            data = await asyncio.to_thread(attachment_storage.fetch, where, key)
        except attachment_storage.StorageUnavailable:
            raise HTTPException(status_code=502, detail="Image storage could not be read. Try again.") from None
        return Response(
            content=data, media_type=assets_lib.SVG,
            headers={
                **assets_lib.SVG_HEADERS, "Content-Disposition": _content_disposition(filename, "inline"),
                "Cache-Control": "private, max-age=300",
            },
        )
    url = attachment_storage.presign_download(where, key, filename=filename, content_type=content_type, inline=True)
    return RedirectResponse(
        url=attachment_storage.browser_url(where, url), status_code=307, headers={"Cache-Control": "no-store"},
    )


def _asset_content_ref(
    session: Session, template_id: UUID, asset_id: str, principal: Optional[Principal]
) -> tuple[str, Optional[str], str, str]:
    """`template_asset_content`'s read, over the route's async session (`run_sync`): the image's key, bucket,
    filename and type."""
    with _member_session(session, principal) as (session, viewer):
        row = _visible_or_404(session, viewer, template_id)
        asset = _find_asset(session, row, asset_id)
        if asset is None or not asset.s3_key:
            raise HTTPException(status_code=404, detail="image not found")
        return asset.s3_key, asset.s3_bucket, asset.filename, asset.content_type or ""
