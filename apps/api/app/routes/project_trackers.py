"""A project's issue-tracker links — the project page's "Issue trackers" section. The rules are
`app/trackers/project_links.py`'s; this module is the doors, and names no tracker.

- `GET /api/projects/{project_id}/trackers` (`projects:view`): the linked connections and their containers;
- `GET /api/projects/{project_id}/trackers/available` (`projects:edit`): the org's connections to link to;
- `GET /api/projects/{project_id}/trackers/{connection_id}/containers` (`projects:edit`): the tracker's containers
  now, or 502 with the tracker's sentence;
- `PUT`, `DELETE /api/projects/{project_id}/trackers/{connection_id}` and
  `POST /api/projects/{project_id}/trackers/{connection_id}/primary` (`projects:edit`): link, unlink, Make primary;
- `GET /api/projects/{project_id}/tracker-issues?q=&limit=` (`tasks:create`): the task form's issue picker, across
  every linked connection (`{results, errors}`: one tracker's failure is its entry, the rest still list);
- `GET /api/projects/{project_id}/tracker-issues/{connection_id}/{key}` (`tasks:create`): one picked issue with its
  description; 404 (`NOT_LINKED_ISSUE`) outside the project's linked tracker projects, 502 with the tracker's sentence.

A connection outside the project's org is 404, indistinguishable from none. Connecting, configuring and disconnecting
a tracker stay an org admin's (`routes/trackers.py`); `can_connect` says whether the caller is one.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path as PathParam, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db_async
from app.actors import set_request_actor
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, current_caller
from app.authz.visibility import has_project_key
from app.db_async import DbDep, on_session
from app.models.projects import Project
from app.models.trackers import TrackerConnection
from app.project_identity import project_or_404
from app.routes.trackers import list_containers_recorded, queue_settings_saved
from app.trackers import project_links, protocol, writeback

log = logging.getLogger("project-tracker-routes")

router = APIRouter(prefix="/api/projects/{project_id}", tags=["trackers"])

_PROJECTS_EDIT_KEY = permission_key("projects", "edit")
_MAX_CONTAINERS = 200


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_ids: list[Annotated[str, Field(max_length=255)]] = Field(max_length=_MAX_CONTAINERS)


class PrimaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_id: str = Field(min_length=1, max_length=255)


def _links_body(session: Session, project: Project, caller: AsyncCaller) -> dict[str, Any]:
    return {
        "links": project_links.links_for(session, project),
        "can_edit": has_project_key(caller.principal, _PROJECTS_EDIT_KEY, project.id),
        "can_connect": bool(caller.principal is not None and caller.principal.is_org_admin),
    }


def _connection_or_404(session: Session, project: Project, connection_id: UUID) -> TrackerConnection:
    conn = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
    if conn is None or conn.org_id != project.org_id:
        raise HTTPException(status_code=404, detail=project_links.NO_CONNECTION)
    return conn


@router.get("/trackers")
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def list_project_trackers(
    session: Session, project_id: int, caller: AsyncCaller = Depends(current_caller),
) -> dict[str, Any]:
    """The project's tracker links. `connection.error` is the connection's last outbound failure (no tracker call
    per read)."""
    return _links_body(session, project_or_404(session, project_id), caller)


@router.get("/trackers/available")
@feature("projects", "edit", object_type="project", object_params=("project_id",))
@on_session
def list_available_trackers(session: Session, project_id: int) -> dict[str, Any]:
    """The project's org's tracker connections a link may name: labels only."""
    return {"connections": project_links.available_connections(session, project_or_404(session, project_id))}


@router.get("/trackers/{connection_id}/containers")
@feature("projects", "edit", object_type="project", object_params=("project_id",))
async def list_project_tracker_containers(project_id: int, connection_id: UUID, *, db: DbDep) -> dict[str, Any]:
    """The connection's tracker projects as the tracker lists them now; 502 with the tracker's sentence when it
    cannot (kept as the connection's `error` until a later call succeeds)."""

    def load(session: Session) -> TrackerConnection:
        conn = _connection_or_404(session, project_or_404(session, project_id), connection_id)
        session.expunge(conn)
        return conn

    conn = await db.run_sync(load)
    await db_async.release(db)
    try:
        pairs = await list_containers_recorded(db, conn)
    except protocol.TrackerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"containers": [{"container_id": cid, "name": name} for cid, name in pairs]}


@router.put("/trackers/{connection_id}")
@feature("projects", "edit", object_type="project", object_params=("project_id",))
async def put_project_tracker(
    project_id: int, connection_id: UUID, body: LinkIn, background: BackgroundTasks,
    caller: AsyncCaller = Depends(current_caller), *, db: DbDep,
) -> dict[str, Any]:
    """Link the project to the connection's `container_ids` (replacing its links to that connection; an empty list
    links it with none picked). The containers' names are then read from the tracker, best effort, and the adapter's
    `on_settings_saved` runs after the answer (`routes/trackers.py::queue_settings_saved`)."""

    def write(session: Session) -> TrackerConnection:
        set_request_actor(session, caller.actor.id)
        project = project_or_404(session, project_id)
        try:
            project_links.set_link(session, caller.actor, project, connection_id, body.container_ids)
        except project_links.LinkNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        session.commit()
        conn = _connection_or_404(session, project, connection_id)
        session.expunge(conn)
        return conn

    conn = await db.run_sync(write)
    await db_async.release(db)
    if body.container_ids:
        queue_settings_saved(background, conn)
    if body.container_ids and protocol.adapter_for(conn.tracker) is not None:
        try:
            await asyncio.wait_for(list_containers_recorded(db, conn), writeback.CALL_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — the links are saved; a name stays the container id until the next read
            log.info("tracker connection %s: container names not refreshed", conn.id, exc_info=True)

    def read(session: Session) -> dict[str, Any]:
        return _links_body(session, project_or_404(session, project_id), caller)

    return await db.run_sync(read)


@router.delete("/trackers/{connection_id}", status_code=204)
@feature("projects", "edit", object_type="project", object_params=("project_id",))
@on_session
def delete_project_tracker(
    session: Session, project_id: int, connection_id: UUID, caller: AsyncCaller = Depends(current_caller),
) -> Response:
    """Unlink the project from the connection. Tasks already linked to an issue stay linked."""
    set_request_actor(session, caller.actor.id)
    try:
        project_links.remove_link(session, caller.actor, project_or_404(session, project_id), connection_id)
    except project_links.LinkNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    session.commit()
    return Response(status_code=204)


@router.post("/trackers/{connection_id}/primary")
@feature("projects", "edit", object_type="project", object_params=("project_id",))
@on_session
def make_project_tracker_primary(
    session: Session, project_id: int, connection_id: UUID, body: PrimaryIn,
    caller: AsyncCaller = Depends(current_caller),
) -> dict[str, Any]:
    """Make this project the one the container's tracker-started tasks land in."""
    set_request_actor(session, caller.actor.id)
    project = project_or_404(session, project_id)
    try:
        project_links.make_primary(session, caller.actor, project, connection_id, body.container_id)
    except project_links.LinkNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    session.commit()
    return _links_body(session, project, caller)


@router.get("/tracker-issues")
@feature("tasks", "create", object_type="project", object_params=("project_id",))
async def search_project_tracker_issues(
    project_id: int, q: str = Query("", max_length=200),
    limit: int = Query(15, ge=1, le=project_links.MAX_SEARCH_RESULTS), *, db: DbDep,
) -> dict[str, Any]:
    """Issues matching `q` (a key or title words) in the project's linked tracker projects, every linked connection
    asked at once. `errors` names each connection that failed, with its sentence."""
    await db.run_sync(lambda session: project_or_404(session, project_id))
    found = await project_links.search(db, project_id, q, limit)
    return {"results": found.results, "errors": found.errors}


@router.get("/tracker-issues/{connection_id}/{key}")
@feature("tasks", "create", object_type="project", object_params=("project_id",))
async def get_project_tracker_issue(
    project_id: int, connection_id: UUID, key: str = PathParam(max_length=100), *, db: DbDep,
) -> dict[str, Any]:
    """One issue of the project's linked tracker projects, with its description (the form fills the task's from it)."""
    await db.run_sync(lambda session: project_or_404(session, project_id))
    try:
        picked = await project_links.issue(db, project_id, connection_id, key)
    except project_links.IssueNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except protocol.TrackerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return picked.wire()
