"""The project credential pool — attach/detach a credential to a project and who can reach it."""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, creates_org_rows, current_principal
from app.authz.visibility import visible_project_clause
from app.db import get_session
from app.entity_shape import get_or_404, UuidRefOut, optional_uuid
from app.identities import display_name
from app.credentials.registry import type_label
from app.models.credentials import (
    Account,
    Credential,
    ProjectCredential,
)
from app.models.identity import AdminUser
from app.models.projects import Project
from app.routes.credentials.guards import _VIEW_KEY, _scoped_row_or_404
from app.routes.credentials.credential_wire import _audit, _parsed_body, _scrubbed_write


router = APIRouter(tags=["credentials"])


# ══ THE PROJECT CREDENTIAL POOL (AC-3.1, AC-3.3) ═══════════════════════════════════════════════════
# A credential attaches to 0..N projects (`project_credentials`); a project role then implies USE of the
# project's ORG credentials (`authz.usable_credential_clause`). Personal stays personal: an attached
# personal credential is still usable by its owner alone. These routes carry no `config` at all — not the
# stripped `_credential_out` shape, nothing — so no binding or attachment can surface a secret.


class CredentialRefOut(BaseModel):
    """A credential named without its document: identity and type only, never `config` or
    `secrets_set`."""

    id: UUID
    name: str
    type: str
    type_label: str
    # Whether it is someone's personal credential (`owner_id` set) — the pool's one ownership fact.
    personal: bool


class ProjectCredentialOut(BaseModel):
    """One attachment, as the attach route answers it."""

    project_id: int
    credential: CredentialRefOut


class CredentialAccessUserOut(UuidRefOut):
    pass


class CredentialAccessProjectOut(BaseModel):
    id: int
    name: str
    # The credential's HOME project (`Credential.home_project_id`): it counts as the pool without an
    # attachment, and is left by changing the row's home, never by a detach (409).
    home: bool = False


class CredentialAccessOut(BaseModel):
    """Who may use a credential (the Credential Access tab, D14): its owner when personal, and its home
    project and the projects it is attached to that this caller can see (a project it cannot see is not
    named)."""

    owner: Optional[CredentialAccessUserOut]
    projects: list[CredentialAccessProjectOut]
    # A SYSTEM row (0325): the control plane's own, never attached to a project.
    system: bool = False


class ProjectCredentialIn(BaseModel):
    """`{credential_id}`. `Any`, per property 2: a 422 would echo the value."""

    model_config = ConfigDict(extra="allow")

    credential_id: Any = None


def _credential_ref_out(row: Credential) -> CredentialRefOut:
    return CredentialRefOut(
        id=row.id, name=row.name, type=row.type, type_label=type_label(row.type),
        personal=row.owner_id is not None,
    )


def _pool_project_or_404(session: Session, principal: Principal | None, project_id: int) -> Project:
    """The project, read under the caller's own visibility (the guard already required `projects:edit`
    on it — a Maintainer's role or an org-wide one)."""
    return get_or_404(session, Project, project_id, detail="unknown project", where=(visible_project_clause(principal),))


def _pool_link(session: Session, project_id: int, credential_id: UUID) -> Optional[ProjectCredential]:
    """The attachment row, through the read seam (a select, never `session.get`'s identity-map read)."""
    return session.scalar(
        select(ProjectCredential).where(
            ProjectCredential.project_id == project_id, ProjectCredential.credential_id == credential_id
        )
    )


def _require_pool_manager(principal: Principal | None, row: Credential, *, attaching: bool) -> None:
    """Only a credential's OWNER or an Org Owner/Admin attaches or detaches it (AC-3.3).

    Its owner always may (on a project they maintain — the route's guard). An Org Owner/Admin
    (`creates_org_rows`) may for any ORG credential, and may DETACH anyone's; they may not ATTACH a
    colleague's personal credential, which would put a row its owner never pooled on a project's page."""
    if principal is None or (principal.user_id is not None and row.owner_id == principal.user_id):
        return
    if creates_org_rows(principal) and (row.owner_id is None or not attaching):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "only the credential's owner or an Org Owner/Admin attaches or detaches it — and a personal "
            "credential is attached only by its owner"
            if attaching else
            "only the credential's owner or an Org Owner/Admin detaches it"
        ),
    )


def _pool_credential_or_404(
    session: Session, principal: Principal | None, credential_id: Any
) -> Credential:
    """The credential named by the body or path, through the module's one id lookup."""
    if not isinstance(credential_id, (str, UUID)):
        raise HTTPException(status_code=400, detail="credential_id must be a uuid")
    parsed = credential_id if isinstance(credential_id, UUID) else optional_uuid(credential_id)
    if parsed is None:
        raise HTTPException(status_code=400, detail="credential_id must be a uuid")
    return _scoped_row_or_404(session, principal, parsed, _VIEW_KEY)


@router.post(
    "/api/projects/{project_id}/credentials", response_model=ProjectCredentialOut, status_code=201
)
# `projects:edit` on THIS project: a Maintainer there, or an Org Owner/Admin. Which credential they may
# attach is the handler's (`_require_pool_manager`).
@feature("projects", "edit", object_type="project", object_params=("project_id",))
def attach_project_credential(
    project_id: int,
    response: Response,
    raw_body: Any = Body(default=None),
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> ProjectCredentialOut:
    """Attach a credential to a project's pool. 201 on a new attachment, 200 when already attached."""
    body = _parsed_body(raw_body, ProjectCredentialIn, frozenset({"credential_id"}))
    project = _pool_project_or_404(session, principal, project_id)
    row = _pool_credential_or_404(session, principal, body.credential_id)
    _require_pool_manager(principal, row, attaching=True)
    if row.system:
        # A system row is the control plane's own (0325): a pool would hand it to every task there.
        raise HTTPException(
            status_code=422, detail="a system credential cannot be attached to a project"
        )
    if session.scalar(select(Account.id).where(Account.credential_id == row.id)) is not None:
        # An account's own API key is never a pool member: no roster serves it (`task_roster_select`),
        # so the attachment would promise a use that never happens.
        raise HTTPException(
            status_code=422, detail="an account's own API key cannot be attached to a project"
        )
    attached = _pool_link(session, project.id, row.id) is not None
    if not attached:
        # Its org is the project's, stamped by the write seam (`tenancy/manifest.py`, same-org refused).
        link = ProjectCredential(project_id=project.id, credential_id=row.id, created_by_id=user.id)
        with _scrubbed_write(session, "attach"):
            session.add(link)
            session.flush()
            _audit(
                session, user, "credential_attached",
                f"Attached credential {row.name} to project {project.name}", row,
            )
            session.commit()
    else:
        response.status_code = 200
    return ProjectCredentialOut(project_id=project.id, credential=_credential_ref_out(row))


@router.delete("/api/projects/{project_id}/credentials/{credential_id}", status_code=204)
@feature("projects", "edit", object_type="project", object_params=("project_id",))
def detach_project_credential(
    project_id: int,
    credential_id: UUID,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Detach a credential from a project's pool. 404 when it is not attached there."""
    project = _pool_project_or_404(session, principal, project_id)
    row = _pool_credential_or_404(session, principal, credential_id)
    _require_pool_manager(principal, row, attaching=False)
    if row.home_project_id == project.id:
        raise HTTPException(
            status_code=409, detail="this is the credential's home project; change its home instead"
        )
    link = _pool_link(session, project.id, row.id)
    if link is None:
        raise HTTPException(status_code=404, detail="that credential is not attached to this project")
    with _scrubbed_write(session, "detach"):
        session.delete(link)
        _audit(
            session, user, "credential_detached",
            f"Detached credential {row.name} from project {project.name}", row,
        )
        session.commit()
    return Response(status_code=204)


@router.get("/api/credentials/{credential_id}/access", response_model=CredentialAccessOut)
@feature("credentials", "view", own_credential=True)
def get_credential_access(
    credential_id: UUID,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CredentialAccessOut:
    """The credential's owner (personal), its home project and the projects it is attached to — never its
    config."""
    row = _scoped_row_or_404(session, principal, credential_id, _VIEW_KEY)
    owner = None
    if row.owner_id is not None:
        owner_user = session.scalar(select(AdminUser).where(AdminUser.id == row.owner_id))
        if owner_user is not None:
            owner = CredentialAccessUserOut(id=owner_user.id, name=display_name(owner_user))
    attached = select(ProjectCredential.project_id).where(ProjectCredential.credential_id == row.id)
    projects = session.execute(
        select(Project.id, Project.name)
        .where(
            or_(Project.id.in_(attached), Project.id == row.home_project_id),
            visible_project_clause(principal),
        )
        .order_by(Project.name.asc(), Project.id.asc())
    ).all()
    return CredentialAccessOut(
        owner=owner,
        system=row.system,
        projects=[
            CredentialAccessProjectOut(id=pid, name=name, home=pid == row.home_project_id)
            for pid, name in projects
        ],
    )
