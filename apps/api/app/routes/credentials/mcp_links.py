"""Project MCP links - a project's explicit opt-in to an MCP connection (`app/mcp_links.py` says why one is needed).

`PUT`, `DELETE` and `GET /api/projects/{project_id}/mcp-links/{credential_id}`, the project's list and the
connection's own (`GET /api/credentials/{credential_id}/mcp-links`, under `credentials:view`), under
the project credential pool's authorization (`project_pool.py`): `projects:edit` on the project to link or unlink,
and only the credential's owner or an Org Owner/Admin may; `projects:view` to read. A credential the caller
cannot see is a 404, whatever its org. Linking also puts the credential in the project's pool when it is not
there yet, so the project's tasks are granted it (`authz.usable_credential_clause`); unlinking leaves the pool
alone (the attachment may predate the link). No route here carries `config`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app import jaah_repos, mcp_links
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, current_caller
from app.authz.visibility import visible_project_clause
from app.db_async import on_session
from app.entity_shape import or_404
from app.models.credentials import Account, Credential, ProjectCredential, ProjectMcpLink
from app.models.projects import Project
from app.routes.credentials.credential_wire import _audit, _scrubbed_write
from app.routes.credentials.project_pool import (
    CredentialRefOut,
    _credential_ref_out,
    _pool_credential_or_404,
    _pool_link,
    _pool_project_or_404,
    _require_pool_manager,
)

router = APIRouter(tags=["credentials"])


class McpLinkOut(BaseModel):
    """One link: the connection named without its document, and the server it reaches (URL and transport are not
    secret; a URL carrying one is refused at dispatch, `mcp_links.connection_url_problem`)."""

    project_id: int
    credential: CredentialRefOut
    url: Optional[str]
    transport: Optional[str]
    created_at: datetime


def _link_out(project_id: int, row: Credential, link: ProjectMcpLink) -> McpLinkOut:
    server = mcp_links.server_of(row)
    return McpLinkOut(
        project_id=project_id, credential=_credential_ref_out(row),
        url=server[0] if server else None, transport=server[1] if server else None, created_at=link.created_at,
    )


def _mcp_link(session: Session, project_id: int, credential_id: UUID) -> Optional[ProjectMcpLink]:
    return session.scalar(select(ProjectMcpLink).where(
        ProjectMcpLink.project_id == project_id, ProjectMcpLink.credential_id == credential_id,
    ))


@router.get("/api/projects/{project_id}/mcp-links", response_model=list[McpLinkOut])
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def list_project_mcp_links(
    session: Session,
    project_id: int,
    caller: AsyncCaller = Depends(current_caller),
) -> list[McpLinkOut]:
    """The project's linked MCP connections the caller can see, by name."""
    project = _pool_project_or_404(session, caller.principal, project_id)
    out = []
    for link in session.scalars(select(ProjectMcpLink).where(ProjectMcpLink.project_id == project.id)):
        try:
            row = _pool_credential_or_404(session, caller.principal, link.credential_id)
        except HTTPException:
            continue  # a link to a credential this caller cannot see is not named
        out.append(_link_out(project.id, row, link))
    return sorted(out, key=lambda item: (item.credential.name.lower(), str(item.credential.id)))


class CredentialMcpLinkOut(BaseModel):
    """One project an MCP connection is linked to, by name (the connection's own MCP tab)."""

    project_id: int
    project_name: str
    created_at: datetime


@router.get("/api/credentials/{credential_id}/mcp-links", response_model=list[CredentialMcpLinkOut])
@feature("credentials", "view", own_credential=True)
@on_session
def list_credential_mcp_links(
    session: Session,
    credential_id: UUID,
    caller: AsyncCaller = Depends(current_caller),
) -> list[CredentialMcpLinkOut]:
    """The projects the caller can see that link this connection, by name; a project hidden from the caller is
    not named (as `GET /api/credentials/{id}/access`)."""
    row = _pool_credential_or_404(session, caller.principal, credential_id)
    linked = select(ProjectMcpLink.project_id).where(ProjectMcpLink.credential_id == row.id)
    rows = session.execute(
        select(Project.id, Project.name, ProjectMcpLink.created_at)
        .join(ProjectMcpLink, and_(ProjectMcpLink.project_id == Project.id, ProjectMcpLink.credential_id == row.id))
        .where(Project.id.in_(linked), visible_project_clause(caller.principal))
        .order_by(Project.name.asc(), Project.id.asc())
    ).all()
    return [CredentialMcpLinkOut(project_id=pid, project_name=name, created_at=at) for pid, name, at in rows]


@router.get("/api/projects/{project_id}/mcp-links/{credential_id}", response_model=McpLinkOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_project_mcp_link(
    session: Session,
    project_id: int,
    credential_id: UUID,
    caller: AsyncCaller = Depends(current_caller),
) -> McpLinkOut:
    """One link; 404 when the project does not link that connection."""
    project = _pool_project_or_404(session, caller.principal, project_id)
    row = _pool_credential_or_404(session, caller.principal, credential_id)
    link = or_404(_mcp_link(session, project.id, row.id), detail="that MCP connection is not linked to this project")
    return _link_out(project.id, row, link)


@router.put("/api/projects/{project_id}/mcp-links/{credential_id}", response_model=McpLinkOut)
@feature("projects", "edit", object_type="project", object_params=("project_id",))
@on_session
def put_project_mcp_link(
    session: Session,
    project_id: int,
    credential_id: UUID,
    response: Response,
    caller: AsyncCaller = Depends(current_caller),
) -> McpLinkOut:
    """Link an MCP connection to the project (201; 200 when already linked), putting it in the project's pool if
    it is not there yet. Refused (422) for a system credential, an account's own key and a non-MCP credential."""
    project = _pool_project_or_404(session, caller.principal, project_id)
    row = _pool_credential_or_404(session, caller.principal, credential_id)
    jaah_repos.refuse_member_edit(session, project_id, caller.actor, caller.principal)
    _require_pool_manager(caller.principal, row, attaching=True)
    if row.system:
        # The control plane's own row (0325) never reaches a session.
        raise HTTPException(status_code=422, detail="a system credential cannot be linked to a project")
    if session.scalar(select(Account.id).where(Account.credential_id == row.id)) is not None:
        raise HTTPException(status_code=422, detail="an account's own API key cannot be linked to a project")
    if not mcp_links.is_mcp_type(row.type):
        raise HTTPException(status_code=422, detail="only an MCP connection can be linked - this credential is not one")
    link = _mcp_link(session, project.id, row.id)
    if link is not None:
        response.status_code = 200
        return _link_out(project.id, row, link)
    with _scrubbed_write(session, "link"):
        if row.home_project_id != project.id and _pool_link(session, project.id, row.id) is None:
            # The grant (D5): a linked connection the project's tasks could not fetch would never start.
            session.add(ProjectCredential(project_id=project.id, credential_id=row.id, created_by_id=caller.actor.id))
            _audit(session, caller.actor, "credential_attached",
                   f"Attached credential {row.name} to project {project.name} (linked as an MCP connection)", row)
        link = ProjectMcpLink(project_id=project.id, credential_id=row.id, created_by_id=caller.actor.id)
        session.add(link)
        session.flush()
        _audit(session, caller.actor, "credential_mcp_linked", f"Linked MCP connection {row.name} to project {project.name}", row)
        session.commit()
    response.status_code = 201
    return _link_out(project.id, row, link)


@router.delete("/api/projects/{project_id}/mcp-links/{credential_id}", status_code=204)
@feature("projects", "edit", object_type="project", object_params=("project_id",))
@on_session
def delete_project_mcp_link(
    session: Session,
    project_id: int,
    credential_id: UUID,
    caller: AsyncCaller = Depends(current_caller),
) -> Response:
    """Unlink; 404 when not linked. The pool attachment stays."""
    project = _pool_project_or_404(session, caller.principal, project_id)
    row = _pool_credential_or_404(session, caller.principal, credential_id)
    jaah_repos.refuse_member_edit(session, project_id, caller.actor, caller.principal)
    _require_pool_manager(caller.principal, row, attaching=False)
    link = or_404(_mcp_link(session, project.id, row.id), detail="that MCP connection is not linked to this project")
    with _scrubbed_write(session, "unlink"):
        session.delete(link)
        _audit(session, caller.actor, "credential_mcp_unlinked",
               f"Unlinked MCP connection {row.name} from project {project.name}", row)
        session.commit()
    return Response(status_code=204)
