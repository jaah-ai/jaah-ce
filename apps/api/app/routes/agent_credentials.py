"""The agent-facing credential writes: a runner creates, edits and deletes credentials (secrets) as the human its
task acts for, and reads the type catalogue that says which config each type needs.

The authority is the launcher (`routes/agent/auth.py::launcher_principal`): each route first asks the console
route's own `@feature` declaration of that principal (`_require_console_admission`), then runs the console's ONE
writer (`routes/credentials/writes.py::_create_credential` / `_update_credential` / `_delete_credential`), so every
validation, row scope and audit is the console's and nothing here re-decides one. An existing credential is named
by the path `credential_list` shows (`routes/agent/task_secrets.py::runner_path`) or by its id.

A runner cannot seal under an org's NodeVault key (that happens in the browser), so this door writes KMS-sealed
credentials only: a create never asks for NodeVault, and a secret posted onto a NodeVault row is refused with what
to ask the user. Secret values are never echoed (`CredentialOut` drops secret keys) and never logged: log lines
carry a task id, a credential id and field KEYS. Every write is held by the loop guard
(`routes/agent/auth.py::require_human_started`): a credential decides what later work can reach, so a task a machine
started may read the type catalogue but never create, change or delete one. Auth is `_check_token`, the caller held by this node and live
(`live_agent_caller`); ids travel in the body or the query (the daemon substitutes only `{task_id}`).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app import node_credentials
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import declaration_of, infra
from app.authz.principal import Principal
from app.credentials.registry import secret_keys
from app.db_async import on_session, on_thread
from app.entity_shape import names_of, optional_uuid
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.tasks import Task
from app.tenancy.seam import bound_org

# The one door onto `/api/agent/*` (`test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`).
from app.routes.agent.auth import AgentDoor, launcher_principal, live_agent_caller, require_human_started
from app.routes.agent.task_secrets import _granted_credentials, runner_path
from app.routes.credentials.credential_wire import CredentialOut, CredentialTypeOut, _parsed_body
from app.routes.credentials.guards import _VIEW_KEY, _scoped_row_or_404
from app.routes.credentials.reads import _list_credential_types
from app.routes.credentials.writes import (
    _create_credential,
    _delete_credential,
    _update_credential,
    create_credential,
    delete_credential,
    update_credential,
)

# Log an outcome plus ids and field keys, never a value (decision 2 of the brief: the value is the credential).
log = logging.getLogger("agent-credentials")

# No `prefix=`: the public/private ALB split test reads each `@infra` path off the decorator.
router = APIRouter(tags=["agent-credentials"])


#: `scope` → the console create's `ownership`. `project` is an org row homed in the caller's own project.
_OWNERSHIP_BY_SCOPE = {"me": "personal", "org": "org", "project": "org"}


# ── The wire shapes ───────────────────────────────────────────────────────────────────────────────────
# `Any` fields and extras allowed, parsed by the console's `_parsed_body` (value-free 400s), not a StrictModel:
# pydantic's 422 echoes the rejected input, and a model's misplaced secret would come back in it.


class AgentCredentialCreateIn(BaseModel):
    """One new credential. `config` is the type's fields (`GET /api/agent/tasks/credential-types`), secret
    values included, in the clear: the control plane seals them under KMS as it stores them."""

    model_config = ConfigDict(extra="allow")

    name: Any = None
    type: Any = None
    description: Any = None
    config: Any = None
    # `me` (the launcher's personal row, the default), `org` (shared with the org) or `project` (an org row
    # homed in the caller's own project). `org` and `project` need what the console asks (`_create_credential`).
    scope: Any = None


class AgentCredentialUpdateIn(BaseModel):
    """A partial edit of one credential, named by `credential` (its runner path or id). `config` merges per key
    as the console's PATCH does: an absent key is kept, `null` deletes it."""

    model_config = ConfigDict(extra="allow")

    credential: Any = None
    name: Any = None
    description: Any = None
    config: Any = None


_CREATE_FIELDS = frozenset({"name", "type", "description", "config", "scope"})
_UPDATE_FIELDS = frozenset({"credential", "name", "description", "config"})


class AgentCredentialOut(CredentialOut):
    """The console's own wire shape (secret keys absent, `secrets_set` names which hold a value) plus the path
    `credential_list` and `credential_get` address it by."""

    path: str


class AgentCredentialDeleteOut(BaseModel):
    """A 200 with a body, not a 204: the caller is a model reading a tool result."""

    credential_id: str
    credential: str
    deleted: bool


# ── Who acts, and on what ─────────────────────────────────────────────────────────────────────────────


def _acting(
    session: Session, agent: node_credentials.AgentCaller, x_agent_task_id: str, console_route: Callable[..., Any],
    *, what: str,
) -> tuple[Task, AdminUser, Principal]:
    """The caller, its launcher, and the launcher admitted as `console_route`'s guard would admit them; refused
    first when a machine started the caller's chain (`what` names the write)."""
    caller = live_agent_caller(session, agent, x_agent_task_id)
    require_human_started(session, caller, what=what)
    if bound_org() is None:
        # A fleet-wide arm binds no tenant, and the writer stamps and scopes by the bound org: refuse before it.
        raise HTTPException(
            status_code=409, detail="credential writes come from a node of an org; this caller is bound to none",
        )
    user, principal = launcher_principal(session, caller)
    _require_console_admission(principal, console_route)
    return caller, user, principal


def _require_console_admission(principal: Principal, console_route: Callable[..., Any]) -> None:
    """403 unless `principal` passes `console_route`'s own declaration as `authz/enforce.py::_enforce` decides a
    FEATURE route: the declared key, else the own-credential fallback (`Declaration.own_credential`), which admits
    any session but never an API key. Read off the route, so a change to its declaration changes this door too.
    The fallback widens the door, never the rows: the writer's scoped select narrows to the launcher's own."""
    declaration = declaration_of(console_route)
    if declaration is None:
        raise HTTPException(status_code=403, detail="this credential write is not declared")
    key = permission_key(declaration.feature or "", declaration.action or "")
    if principal.has(key) or (declaration.own_credential and principal.api_key_id is None):
        return
    raise HTTPException(
        status_code=403,
        detail=f"{key} is required — the user this task acts for may not do this in the console either",
    )


def _addressed(session: Session, caller: Task, principal: Principal, credential: Any) -> tuple[Credential, str]:
    """The credential `credential` names — an id, or the runner path `credential_list` shows — with its path.

    A path resolves over the task's own roster (`_granted_credentials`, the list tool's rows); an id reaches any
    credential the launcher may read (`_scoped_row_or_404`). Whether they may CHANGE it is the writer's check."""
    if not isinstance(credential, str) or not credential.strip():
        raise HTTPException(status_code=400, detail="credential is required: its path from credential_list, or its id")
    named = credential.strip()
    credential_id = optional_uuid(named)
    if credential_id is not None:
        row = _scoped_row_or_404(session, principal, credential_id, _VIEW_KEY)
        return row, runner_path(row, principal.user_id, names_of(session, Project, [row.home_project_id]))
    matches = _granted_credentials(session, caller, named)
    if not matches:
        raise HTTPException(
            status_code=404,
            detail=f"no credential at {named!r} on this task's roster — use a path credential_list shows, or an id",
        )
    if len(matches) > 1:
        ids = ", ".join(str(row.id) for _path, row in matches)
        raise HTTPException(status_code=409, detail=f"{named!r} names {len(matches)} credentials ({ids}); pass an id")
    path, row = matches[0]
    return row, path


def _refuse_a_secret_onto_nodevault(row: Credential, posted: Any) -> None:
    """409 when a secret is posted onto a NodeVault row: its secrets are sealed in the user's browser under the
    org's NodeVault key, which a runner cannot do (the writer would 422 the plaintext). Names the keys only."""
    if not row.nodevault or not isinstance(posted, dict):
        return
    secrets = secret_keys(row.type, posted)
    sent = sorted(key for key, value in posted.items() if key in secrets and isinstance(value, str) and value.strip())
    if sent:
        raise HTTPException(
            status_code=409,
            detail=(
                f"credential {row.name} is a NodeVault credential, whose secrets ({', '.join(sent)}) are sealed in "
                "the browser — a runner cannot set them. Ask the user to enter them in the console (Credentials)."
            ),
        )


def _with_path(session: Session, out: CredentialOut, principal: Principal) -> AgentCredentialOut:
    row = _scoped_row_or_404(session, principal, out.id, _VIEW_KEY)
    path = runner_path(row, principal.user_id, names_of(session, Project, [row.home_project_id]))
    return AgentCredentialOut(**out.model_dump(), path=path)


# ── The routes ────────────────────────────────────────────────────────────────────────────────────────


@router.post("/api/agent/tasks/credential-writes", response_model=AgentCredentialOut, status_code=201)
@infra(Tenancy.ORG_SCOPED)
@on_session
def create_agent_credential(
    session: Session,
    door: AgentDoor,
    raw_body: Any = Body(default=None),
) -> AgentCredentialOut:
    """Create one credential as the launcher: theirs (`me`), the org's, or the org's homed in this project."""
    caller, user, principal = _acting(session, *door, create_credential, what="creating a credential")
    body = _parsed_body(raw_body, AgentCredentialCreateIn, _CREATE_FIELDS)
    scope = "me" if body.scope is None else body.scope
    if scope not in _OWNERSHIP_BY_SCOPE:
        raise HTTPException(status_code=422, detail=f"scope must be one of {sorted(_OWNERSHIP_BY_SCOPE)}")
    home_project_id: Optional[int] = None
    if scope == "project":
        if caller.project_id is None:
            raise HTTPException(status_code=409, detail=f"task {caller.id} names no project to home it in")
        home_project_id = caller.project_id
    write: dict[str, Any] = {
        "name": body.name, "type": body.type, "description": body.description, "config": body.config,
        "ownership": _OWNERSHIP_BY_SCOPE[scope], "home_project_id": home_project_id,
    }
    out = _with_path(session, _create_credential(session, write, user, principal), principal)
    log.info(
        "agent credential: task=%s created credential=%s scope=%s (as launcher=%s)",
        caller.id, out.id, scope, user.id,
    )
    return out


@router.patch("/api/agent/tasks/credential-writes", response_model=AgentCredentialOut)
@infra(Tenancy.ORG_SCOPED)
# `deferring`: the writer queues the Telegram webhook re-registration a new bot token needs.
@on_session(deferring=True)
def update_agent_credential(
    session: Session,
    door: AgentDoor,
    raw_body: Any = Body(default=None),
) -> AgentCredentialOut:
    """Edit one credential the launcher may edit: its name, description, or config keys (merged)."""
    caller, user, principal = _acting(session, *door, update_credential, what="editing a credential")
    body = _parsed_body(raw_body, AgentCredentialUpdateIn, _UPDATE_FIELDS)
    row, _path = _addressed(session, caller, principal, body.credential)
    _refuse_a_secret_onto_nodevault(row, body.config)
    write = {key: getattr(body, key) for key in ("name", "description", "config") if key in body.model_fields_set}
    out = _with_path(session, _update_credential(session, row.id, write, user, principal), principal)
    config_keys = sorted(body.config) if isinstance(body.config, dict) else []
    log.info(
        "agent credential: task=%s updated credential=%s fields=%s config_keys=%s (as launcher=%s)",
        caller.id, row.id, sorted(write), config_keys, user.id,
    )
    return out


@router.delete("/api/agent/tasks/credential-writes", response_model=AgentCredentialDeleteOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def delete_agent_credential(
    session: Session,
    door: AgentDoor,
    credential: str = Query(..., description="the credential's path from credential_list, or its id"),
) -> AgentCredentialDeleteOut:
    """Delete one credential the launcher may delete. Its id is retired with it."""
    caller, user, principal = _acting(session, *door, delete_credential, what="deleting a credential")
    row, path = _addressed(session, caller, principal, credential)
    credential_id = row.id
    _delete_credential(session, credential_id, user, principal)
    log.info("agent credential: task=%s deleted credential=%s (as launcher=%s)", caller.id, credential_id, user.id)
    return AgentCredentialDeleteOut(credential_id=str(credential_id), credential=path, deleted=True)


@router.get("/api/agent/tasks/credential-types", response_model=list[CredentialTypeOut])
@infra(Tenancy.ORG_SCOPED)
# On a thread: the catalogue reads operator settings, whose cache miss opens a sync session of its own.
@on_thread
def list_agent_credential_types(
    session: Session,
    door: AgentDoor,
) -> list[CredentialTypeOut]:
    """The console's type catalogue (`GET /api/credentials/types`): each type's slug and its fields — which are
    required, which secret — so a model knows what `config` a create needs."""
    live_agent_caller(session, *door)
    return _list_credential_types()
