"""SDK agents and the connector catalog — `/api/agents` and `/api/agent-catalog` (architecture §3.9).

An org registers an agent from a GitHub repo (+ sub-path and ref); the row is a commit-pinned snapshot
(`app/agent_bundles.py`) usable by that org only. The catalog is the same table: a connector is a row of
the operator org flagged `is_catalog`, published by the operator and opted into per org
(`agent_enablements`). Every rule over a manifest is `app/agents.py`'s.

Authz: `agents:view` reads, `agents:edit` writes — held by exactly the roles that carry `tasks:edit`
(DD13). Publishing and withdrawing a connector is the operator's alone. Org agents are read through the
read seam, so another org's id is a 404; catalog rows live in the operator org, which the seam hides from a
tenant, so they are read on a fleet session of their own (`_catalog_rows`) and never written there.

Every route here answers 403 `agent_resolution.agents_off` while the caller's org's provider grid has no agent row on
(`_agents_on_plan`: neither `sdk` nor any `acp:*`); registering an agent and enabling or publishing a connector also
need the `sdk`/`own` cell (`_sdk_on_plan`) — the operator publishing to the catalog reads its own org's grid. The
picker's `choices` still lists the built-ins and offers only the agents whose own cell is on
(`agent_resolution.cell_refusal`).

Refusals carry sentences: a failed snapshot answers `{"detail": "<joined>", "errors": [sentence, …]}` so
the console can list each sentence while `detail` stays the string every other error carries.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import acp_registry, agent_bundles, agent_providers, agent_resolution, provider_auth_policy, secret_redact
from app.actors import stamp_actor
from app.agents import (
    SDK_PERSONAS,
    STATUS_ACTIVE,
    STATUS_REMOVED,
    is_commit_sha,
    unsupported,
)
from app.auth import require_operator, require_user
from app.clock import utcnow as _now
from app.credential_keys import NodeVaultSealed, WrappingKeyUnavailable
from app.authz.catalog import PROJECTS_VIEW_KEY, Tenancy
from app.authz.declarations import feature, superadmin_only
from app.authz.principal import Principal, current_principal
from app.authz.visibility import scoped_project_select, scoped_task_select
from app.db import get_session
from app.entity_shape import (
    StrictModel, conflict_as_409, constraint_name, iso_or_none, new_uuid7, optional_uuid, row_by_uuid_or_404,
)
from app.models.agent_sessions import Agent, AgentEnablement
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.projects import PROJECT_ENV_VAR_NAME_RE, Project
from app.models.tasks import Task
from app.tenancy import seam
from app.tenancy.labels import OrgRef, org_ref_of, org_refs

log = logging.getLogger("agents")

router = APIRouter(tags=["agents"])

_REGISTER_RETRY = "then register the agent again"
_NOT_FOUND = "no such agent"
_EDIT_KEY = "agents:edit"
#: How many tasks and projects a delete's 409 names (`delete_agent`); the counts stay whole.
_IN_USE_LISTED = 10

# An agent's `env`: names in the project environment's grammar, values plain text. Not secrets — credentials
# carry those. Capped so a row stays a settings row.
_ENV_NAME = re.compile(PROJECT_ENV_VAR_NAME_RE)
_ENV_MAX_VARS = 50
_ENV_MAX_VALUE = 4096
# Names the agent supervisor sets itself; an agent's env may not shadow them. Other
# `JAAH_` names stay open: a connector's own settings live there.
_ENV_RESERVED_PREFIX = "JAAH_AGENT_"
_ENV_RESERVED = frozenset({"JAAH_WORKTREE", "PYTHONPATH"})


def _checked_env(env: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
    """`env` as stored, or a ValueError (a 422) naming the first bad entry."""
    if env is None:
        return None
    if len(env) > _ENV_MAX_VARS:
        raise ValueError(f"At most {_ENV_MAX_VARS} environment variables")
    for name, value in env.items():
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not an environment variable name (letters, digits, _; not starting with a digit)")
        if name in _ENV_RESERVED or name.startswith(_ENV_RESERVED_PREFIX):
            raise ValueError(f"{name} is set by the agent supervisor and cannot be overridden")
        if len(value) > _ENV_MAX_VALUE or "\x00" in value:
            raise ValueError(f"{name}'s value must be under {_ENV_MAX_VALUE} characters, with no NUL")
        if secret_redact.redact(value)[1]:
            raise ValueError(f"{name} looks like a secret — use a credential")
    return dict(env)


# ── Wire models ─────────────────────────────────────────────────────────────────────────────────────


class AgentIn(StrictModel):
    repo_url: str
    subpath: str = ""
    ref: str
    env: Optional[dict[str, str]] = None

    @field_validator("env")
    @classmethod
    def _env_ok(cls, env: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
        return _checked_env(env)


class AgentPatch(StrictModel):
    ref: Optional[str] = None
    subpath: Optional[str] = None
    status: Optional[Literal["active", "disabled"]] = None
    # Replaces the whole map; `{}` clears it.
    env: Optional[dict[str, str]] = None
    # A registry agent's key account (`acp_registry.KEY_ACCOUNT`): an API-key account of the org; an explicit null
    # unbinds it. The org admin's alone, as the install is.
    key_account_id: Optional[UUID] = None

    @field_validator("env")
    @classmethod
    def _env_ok(cls, env: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
        return _checked_env(env)


class CatalogPatch(StrictModel):
    ref: Optional[str] = None
    status: Optional[Literal["active", "removed"]] = None


class AgentOut(BaseModel):
    id: UUID
    slug: str
    display_name: str
    description: str
    agent_type: str
    language: str
    entrypoint: str
    version: str
    maintainer: str
    personas: list[str]
    repo_url: str
    subpath: str
    ref: str
    resolved_commit: Optional[str]
    bundle_sha256: Optional[str]
    bundle_bytes: Optional[int]
    status: str
    resolve_error: Optional[str]
    resolved_at: Optional[str]
    is_catalog: bool
    # Where the row came from: a repo snapshot, or an ACP registry install (`app/acp_registry.py`).
    source: Literal["repo", "acp_registry"] = "repo"
    # Set on the agent's process at launch (`fleet_verdicts.agent_launch`); not secrets.
    env: dict[str, str] = {}
    # A registry agent's bound API-key account (its key reaches the agent through ACP `authenticate`), or None.
    key_account_id: Optional[UUID] = None
    # The owning org: an operator's list spans orgs (`list_agents`). `org` labels an org agent there only.
    org_id: UUID
    org: Optional[OrgRef] = None
    # Catalog rows only: whether the caller's org has it enabled.
    enabled: Optional[bool] = None
    # Why the row can no longer run (`agents.unsupported`: an SDK v1 agent), or None.
    unsupported: Optional[str] = None
    created_at: Optional[str]
    updated_at: Optional[str]


class ChoiceOut(BaseModel):
    """One picker entry: a built-in provider (`kind="builtin"`) or an agent (`kind="agent"`). A disabled
    entry is listed with the sentence saying why, never silently dropped. `removed` marks a connector withdrawn
    from the catalog: the picker offers it only to the row that already names it, so the value does not vanish."""

    kind: Literal["builtin", "agent"]
    display_name: str
    provider: Optional[str] = None
    id: Optional[UUID] = None
    agent_type: Optional[str] = None
    version: Optional[str] = None
    is_catalog: bool = False
    disabled: bool = False
    removed: bool = False
    reason: Optional[str] = None


def _out(row: Agent, *, enabled: Optional[bool] = None, org: Optional[OrgRef] = None) -> AgentOut:
    return AgentOut(
        id=row.id, slug=row.slug, display_name=row.display_name, description=row.description or "",
        agent_type=row.agent_type, language=row.language, entrypoint=row.entrypoint, version=row.version,
        maintainer=row.maintainer, personas=list(row.personas or []), repo_url=row.repo_url,
        subpath=row.subpath or "", ref=row.ref, resolved_commit=row.resolved_commit,
        bundle_sha256=row.bundle_sha256, bundle_bytes=row.bundle_bytes, status=row.status,
        resolve_error=row.resolve_error, resolved_at=iso_or_none(row.resolved_at), is_catalog=row.is_catalog,
        source="acp_registry" if acp_registry.is_registry(row) else "repo",
        env=dict(row.env or {}), key_account_id=acp_registry.key_account_id(row), org_id=row.org_id, org=org, enabled=enabled,
        unsupported=unsupported(row.manifest), created_at=iso_or_none(row.created_at), updated_at=iso_or_none(row.updated_at),
    )


def _refused(exc: agent_bundles.SnapshotRefused) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": "; ".join(exc.errors), "errors": exc.errors})


#: Faults `main.py` already answers with a status and sentence of their own; never re-mapped to a 500.
_HANDLED_ELSEWHERE = (HTTPException, WrappingKeyUnavailable, NodeVaultSealed)


def _unexpected(session: Session, what: str) -> JSONResponse:
    """A fault no refusal sentence covers, as a 500 the console can show: a sentence carrying an error id
    that is also in the API log beside the traceback, never Starlette's bare `Internal Server Error`.
    Called from an `except` block, before anything was committed."""
    error_id = uuid4().hex[:12]
    log.exception("agents: %s failed unexpectedly (error %s)", what, error_id)
    try:
        session.rollback()
    except Exception:  # a broken connection must not turn the sentence back into a bare 500
        log.warning("agents: rollback after error %s failed", error_id, exc_info=True)
    sentence = (
        f"{what} failed on the control plane (error {error_id}); try again, and quote that id if it "
        "keeps failing"
    )
    return JSONResponse(status_code=500, content={"detail": sentence, "errors": [sentence], "error_id": error_id})


# ── Row lookups ─────────────────────────────────────────────────────────────────────────────────────


def _org_agent(session: Session, principal: Principal, agent_id: str) -> Agent:
    """The caller's org's own (non-catalog) agent, or 404. Through the read seam, so another org's is a miss."""
    row = row_by_uuid_or_404(session, Agent, agent_id, detail=_NOT_FOUND)
    if row.is_catalog or row.org_id != principal.org_id:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return row


def _require_editor(principal: Principal, user: AdminUser, row: Agent) -> None:
    """An agent is the org's, but `agents:edit` may be held on one project only (DD13: whoever edits tasks
    registers agents). Such a holder changes only the agents they registered; changing anyone else's — whose
    code then runs in every project picking it — takes the key org-wide."""
    if principal.has_org(_EDIT_KEY) or row.created_by_id == user.id:
        return
    raise HTTPException(
        status_code=403,
        detail=f"Only whoever registered {row.display_name}, or an org-wide agent editor, can change it",
    )


def _require_org_editor(principal: Principal) -> None:
    """A connector is turned on or off for the whole org, so it takes `agents:edit` org-wide."""
    if not principal.has_org(_EDIT_KEY):
        raise HTTPException(status_code=403, detail="Turning a catalog connector on or off needs agents:edit for the whole organization")


def _catalog_rows(ids: Optional[Iterable[UUID]] = None) -> list[Agent]:
    """Catalog connectors (all, or those in `ids`), read on a fleet session: they live in the operator org,
    which a tenant's bound session cannot see. Read-only and returned detached — never written through."""
    from app.db import SessionLocal

    with seam.detached(), seam.operator_scope(), SessionLocal() as fleet:
        query = select(Agent).where(Agent.is_catalog.is_(True))
        if ids is not None:
            wanted = list(ids)
            if not wanted:
                return []
            query = query.where(Agent.id.in_(wanted))
        rows = list(fleet.scalars(query.order_by(Agent.display_name)))
        fleet.expunge_all()
        return rows


def _enabled_ids(session: Session, principal: Principal, *, listed: bool = False) -> set[UUID]:
    """The connectors the caller's org enabled. A `listed` read (the Agents and Catalog lists) honours the
    operator's org filter instead: the orgs it selected. All stays the caller's own org, the one a toggle
    writes — "enabled in any org" would be no answer."""
    orgs = (seam.request_org_filter() if listed else None) or {principal.org_id}
    return set(session.scalars(select(AgentEnablement.agent_id).where(AgentEnablement.org_id.in_(orgs))))


def _visible_catalog(session: Session, principal: Principal, *, listed: bool = False) -> list[tuple[Agent, bool]]:
    """Every catalog connector the caller's org may see, with its enabled flag: the active ones, plus a
    removed one ONLY where this org enabled it (AC-13) — it is marked, never silently gone."""
    enabled = _enabled_ids(session, principal, listed=listed)
    return [
        (row, row.id in enabled)
        for row in _catalog_rows()
        if row.status == STATUS_ACTIVE or (row.status == STATUS_REMOVED and row.id in enabled)
    ]


def _catalog_row(session: Session, principal: Principal, agent_id: str) -> tuple[Agent, bool]:
    key = optional_uuid(agent_id)
    if key is None:
        raise HTTPException(status_code=404, detail="no such connector")
    for row, enabled in _visible_catalog(session, principal):
        if row.id == key:
            return row, enabled
    raise HTTPException(status_code=404, detail="no such connector")


def _agents_on_plan(
    principal: Principal = Depends(current_principal), session: Session = Depends(get_session),
) -> None:
    """403 unless the caller's own org's provider grid has an agent row on (`agent_resolution.agents_on`). A
    principal with no org is refused (fail closed), never answered from the operator default."""
    if principal.org_id is None or not agent_resolution.agents_on(session, principal.org_id):
        raise HTTPException(status_code=403, detail=agent_resolution.agents_off(principal.org_id))


def _sdk_on_plan(
    principal: Principal = Depends(current_principal), session: Session = Depends(get_session),
) -> None:
    """403 naming the cell unless the caller's own org's provider grid runs SDK agents (`sdk`/`own`)."""
    row, auth = provider_auth_policy.SDK_ROW, provider_auth_policy.OWN
    if principal.org_id is None or not provider_auth_policy.is_allowed(session, principal.org_id, row, auth):
        raise HTTPException(status_code=403, detail=provider_auth_policy.refusal(session, principal.org_id, row, auth))


def _require_cell(session: Session, principal: Principal, row: Agent) -> None:
    """403 naming the cell unless the org's provider grid runs `row` (`agent_resolution.cell_refusal`): what a
    re-snapshot or an environment edit needs. Disabling, deleting and binding stay open, so an off agent can be
    cleaned up."""
    off = agent_resolution.cell_refusal(session, row, principal.org_id)
    if off is not None:
        raise HTTPException(status_code=403, detail=off)


_ON_PLAN = [Depends(_agents_on_plan)]
#: Adding an SDK agent or connector: the `sdk`/`own` cell, not merely any agent row.
_SDK_ON_PLAN = [Depends(_sdk_on_plan)]


# ── Org agents ──────────────────────────────────────────────────────────────────────────────────────


@router.get("/api/agents", response_model=list[AgentOut], dependencies=_ON_PLAN)
@feature("agents", "view")
def list_agents(
    _: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[AgentOut]:
    """The org's own agents, then the catalog connectors it enabled (a removed one flagged by `status`).

    No org predicate of its own: the read seam narrows it — to the caller's org, or for an operator to the
    orgs their filter selected (`authz._principal_scope`), as every Directory list."""
    own = list(session.scalars(
        select(Agent).where(Agent.is_catalog.is_(False)).order_by(Agent.display_name, Agent.slug)
    ))
    catalog = [row for row, enabled in _visible_catalog(session, principal, listed=True) if enabled]
    refs = org_refs(session, [row.org_id for row in own])
    out = [_out(row, org=org_ref_of(refs, row.org_id)) for row in own]
    out += [_out(row, enabled=True) for row in catalog]
    return out


@router.post("/api/agents", response_model=AgentOut, status_code=201, dependencies=_SDK_ON_PLAN)
@feature("agents", "edit")
def register_agent(
    body: AgentIn,
    user: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """Register an agent: snapshot the repo as the caller (their own GitHub connection), validate it, store
    the bundle, write the row. Any failure is a 422 of sentences and writes nothing (AC-9, AC-15)."""
    return _create(session, user, body, org_id=principal.org_id, is_catalog=False)


def _create(session: Session, user: AdminUser, body: AgentIn, *, org_id: UUID, is_catalog: bool):
    what = "Publishing the connector" if is_catalog else "Registering the agent"
    try:
        snapshot = agent_bundles.take_snapshot(
            session, user_id=user.id, repo_url=body.repo_url, subpath=body.subpath, ref=body.ref,
            retry=_REGISTER_RETRY,
        )
        if agent_bundles.slug_taken(session, org_id, snapshot.manifest.name):
            raise agent_bundles.SnapshotRefused([agent_bundles.slug_taken_sentence(snapshot.manifest.name)])
        agent_bundles.refuse_secret_env(snapshot.manifest.to_json(), body.env)
        agent_id = new_uuid7()
        key = agent_bundles.store_bundle(agent_id, snapshot)
        row = Agent(
            id=agent_id, org_id=org_id, is_catalog=is_catalog,
            repo_url=agent_bundles.parse_repo(body.repo_url).web_url(),
            subpath=agent_bundles.normalised_subpath(body.subpath), ref=body.ref.strip(),
            repo_access_user_id=user.id, status=STATUS_ACTIVE, env=body.env or {},
        )
        agent_bundles.apply_snapshot(row, snapshot, key, now=_now())
        stamp_actor(row, user, created=True)
        session.add(row)
        committed = _committed(session, row)
    except agent_bundles.SnapshotRefused as exc:
        return _refused(exc)
    except _HANDLED_ELSEWHERE:
        raise
    except Exception:  # nothing was committed: a sentence with an error id, never a bare 500
        return _unexpected(session, what)
    if not committed:
        return _refused(agent_bundles.SnapshotRefused([agent_bundles.slug_taken_sentence(snapshot.manifest.name)]))
    session.refresh(row)
    return JSONResponse(status_code=201, content=_out(row).model_dump(mode="json"))


@router.patch("/api/agents/{agent_id}", response_model=AgentOut, dependencies=_ON_PLAN)
@feature("agents", "edit")
def update_agent(
    agent_id: str,
    body: AgentPatch,
    user: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """Change `ref`/`subpath` (re-snapshotted now, as the caller; a failure is a 422 and changes nothing),
    `status` active|disabled, `env` (the whole map) and/or a registry agent's `key_account_id`."""
    row = _org_agent(session, principal, agent_id)
    _require_editor(principal, user, row)
    if body.ref is not None or body.subpath is not None or body.env is not None:
        _require_cell(session, principal, row)
    rebind = "key_account_id" in body.model_fields_set
    if rebind:
        refusal = _key_account_refusal(session, principal, row, body.key_account_id)
        if refusal is not None:
            return refusal
    if acp_registry.is_registry(row) and (body.ref is not None or body.subpath is not None):
        return _refused(agent_bundles.SnapshotRefused([acp_registry.pinned_sentence(row)]))
    if acp_registry.is_registry(row) and body.env is not None:
        return _refused(agent_bundles.SnapshotRefused([acp_registry.env_sentence(row)]))
    env = row.env if body.env is None else body.env
    result = _edit(session, user, row, ref=body.ref, subpath=body.subpath, env=env)
    if result is not None:
        return result
    if body.status is not None:
        row.status = body.status
    if rebind:
        manifest = {k: v for k, v in (row.manifest or {}).items() if k != acp_registry.KEY_ACCOUNT}
        if body.key_account_id is not None:
            manifest[acp_registry.KEY_ACCOUNT] = str(body.key_account_id)
        row.manifest = manifest
    if body.env is not None:
        try:
            agent_bundles.refuse_secret_env(row.manifest, body.env)
        except agent_bundles.SnapshotRefused as exc:
            session.rollback()
            return _refused(exc)
        row.env = body.env
    stamp_actor(row, user)
    wanted = row.slug
    if not _committed(session, row):
        return _refused(agent_bundles.SnapshotRefused([agent_bundles.slug_taken_sentence(wanted)]))
    session.refresh(row)
    return _out(row)


def _key_account_refusal(session: Session, principal: Principal, row: Agent,
                         account_id: Optional[UUID]) -> Optional[JSONResponse]:
    """The refusal of binding `account_id` (None: unbinding) to `row`, or None when it may: a 403 unless the caller is
    the org admin (the account's key is billable, as the install is theirs), a 422 unless `row` is a registry agent
    and the account an enabled API-key account of the row's org whose pair the org's provider grid runs."""
    if not principal.is_org_admin:
        raise HTTPException(status_code=403, detail="Only an org admin can bind a key account to an agent")
    if not acp_registry.is_registry(row):
        return _refused(agent_bundles.SnapshotRefused([acp_registry.key_only_sentence(row)]))
    if account_id is None:
        return None
    account = session.scalars(
        select(Account).where(Account.id == account_id, Account.org_id == row.org_id).limit(1)
    ).first()
    if account is None:
        return _refused(agent_bundles.SnapshotRefused(["No such account in this organization"]))
    sentence = acp_registry.key_account_refusal(account, row) or agent_resolution.key_pair_refusal(
        session, account, row, row.org_id,
    )
    return _refused(agent_bundles.SnapshotRefused([sentence])) if sentence else None


def _edit(
    session: Session, user: AdminUser, row: Agent, *, ref: Optional[str], subpath: Optional[str],
    env: Optional[dict] = None,
) -> Optional[JSONResponse]:
    """Re-snapshot `row` at a new ref/sub-path, reading as `user` who becomes its repo reader. None when
    nothing moved or it succeeded; the refusal response otherwise, with the row untouched. `env` is the agent's
    variables as they will stand: a new manifest 2 is refused with a secret-shaped one before anything is stored."""
    new_ref = row.ref if ref is None else ref.strip()
    try:
        new_subpath = row.subpath if subpath is None else agent_bundles.normalised_subpath(subpath)
        if new_ref == row.ref and new_subpath == row.subpath:
            return None
        if row.is_catalog and not is_commit_sha(new_ref):
            raise agent_bundles.SnapshotRefused([_CATALOG_REF_SENTENCE])
        snapshot = agent_bundles.take_snapshot(
            session, user_id=user.id, repo_url=row.repo_url, subpath=new_subpath, ref=new_ref,
            retry=_REGISTER_RETRY,
        )
        if agent_bundles.slug_taken(session, row.org_id, snapshot.manifest.name, other_than=row.id):
            raise agent_bundles.SnapshotRefused([agent_bundles.slug_taken_sentence(snapshot.manifest.name)])
        agent_bundles.refuse_secret_env(snapshot.manifest.to_json(), env)
        key = agent_bundles.store_bundle(row.id, snapshot)
    except agent_bundles.SnapshotRefused as exc:
        return _refused(exc)
    except _HANDLED_ELSEWHERE:
        raise
    except Exception:  # as `_create`: a sentence with an error id, never a bare 500
        return _unexpected(session, "Fetching the new commit")
    row.ref, row.subpath, row.repo_access_user_id = new_ref, new_subpath, user.id
    agent_bundles.apply_snapshot(row, snapshot, key, now=_now())
    return None


@router.delete("/api/agents/{agent_id}", status_code=204, dependencies=_ON_PLAN)
@feature("agents", "edit")
def delete_agent(
    agent_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Delete an agent no task or project names; otherwise 409 naming the counts (disable it instead), and
    listing the first `_IN_USE_LISTED` of each the caller may see (`tasks: [{id, title}]`,
    `projects: [{id, name}]`) to link; the counts stay whole.

    The bundle objects stay in storage: the control plane holds no delete grant outside
    `task-attachments/`, and a run's recorded commit may still be read back."""
    row = _org_agent(session, principal, agent_id)
    _require_editor(principal, user, row)
    tasks = int(session.scalar(select(func.count()).select_from(Task).where(Task.agent_id == row.id)) or 0)
    projects = int(
        session.scalar(select(func.count()).select_from(Project).where(Project.default_agent_id == row.id)) or 0
    )
    if tasks or projects:
        return JSONResponse(status_code=409, content={
            "detail": _in_use_sentence(row.display_name, tasks, projects),
            "tasks": [
                {"id": task_id, "title": title}
                for task_id, title in session.execute(
                    scoped_task_select(principal).with_only_columns(Task.id, Task.title)
                    .where(Task.agent_id == row.id).order_by(Task.id.desc()).limit(_IN_USE_LISTED)
                )
            ],
            "projects": [
                {"id": project_id, "name": name}
                for project_id, name in session.execute(
                    scoped_project_select(principal, key=PROJECTS_VIEW_KEY).with_only_columns(Project.id, Project.name)
                    .where(Project.default_agent_id == row.id)
                    .order_by(Project.name).limit(_IN_USE_LISTED)
                )
            ],
        })
    session.delete(row)
    # A row of another org (or one added since the count) still names it: RESTRICT held.
    with conflict_as_409(session, f"{row.display_name} is still in use; disable it instead"):
        session.commit()
    return Response(status_code=204)


def _in_use_sentence(name: str, tasks: int, projects: int) -> str:
    parts = []
    if tasks:
        parts.append(f"{tasks} task{'s' if tasks != 1 else ''}")
    if projects:
        parts.append(f"{projects} project{'s' if projects != 1 else ''} as the default agent")
    return f"{name} cannot be deleted: {' and '.join(parts)} still use it. Disable it instead."


@router.post("/api/agents/{agent_id}/refresh", response_model=AgentOut, dependencies=_ON_PLAN)
@feature("agents", "edit")
def refresh_agent(
    agent_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """Re-resolve the ref, re-snapshot, re-validate. A failure is recorded on the row as `resolve_error`,
    the last good snapshot keeps serving (DD4), and the sentences come back as a 422."""
    row = _org_agent(session, principal, agent_id)
    _require_editor(principal, user, row)
    if acp_registry.is_registry(row):
        raise HTTPException(status_code=409, detail=acp_registry.pinned_sentence(row))
    _require_cell(session, principal, row)
    refused = agent_bundles.refresh(session, row, now=_now())
    wanted = row.slug
    if not _committed(session, row):
        return _refused(agent_bundles.SnapshotRefused([agent_bundles.slug_taken_sentence(wanted)]))
    if refused is not None:
        return _refused(refused)
    session.refresh(row)
    return _out(row)


def _committed(session: Session, row: Agent) -> bool:
    """Commit, False when a concurrent registration took the manifest's name first (`uq_agents_org_slug`)."""
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if constraint_name(exc) != "uq_agents_org_slug":
            raise
        return False
    return True


# ── The picker ──────────────────────────────────────────────────────────────────────────────────────


def _unrunnable_reason(row: Agent, persona: Optional[str]) -> Optional[str]:
    if row.status == STATUS_REMOVED:
        return f"{row.display_name} was removed from the catalog — tasks using it will fail to start"
    if row.resolved_commit is None and not acp_registry.is_registry(row):
        return f"{row.display_name} has never been fetched — open Agents and press Refresh"
    if persona is not None and persona not in (row.personas or []):
        return f"{row.display_name} does not serve the {persona} persona"
    return None


@router.get("/api/agents/choices", response_model=list[ChoiceOut])
@feature("agents", "view")
def agent_choices(
    persona: Optional[str] = Query(default=None),
    _: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[ChoiceOut]:
    """The one picker's entries. Built-ins are derived from `agent_providers.PROVIDERS` at read time (DD11),
    only those the org has an account for and its provider grid runs (`provider_auth_policy.provider_on_plan`); then the
    org's active agents and its enabled connectors whose own grid cell is on (`agent_resolution.cell_refusal`). An
    agent that cannot run `persona` (or at all) comes back disabled, with the reason."""
    held = set(session.scalars(select(distinct(Account.provider)).where(Account.org_id == principal.org_id)))
    memo: dict = {}
    out = [
        ChoiceOut(kind="builtin", provider=provider.value, display_name=provider.label)
        for provider in agent_providers.PROVIDERS
        if provider.value in held and provider_auth_policy.provider_on_plan(session, principal.org_id, provider.value, memo=memo)
    ]
    if persona is not None and persona not in SDK_PERSONAS:
        # Script and system personas never resolve to an agent; list none rather than all-disabled.
        return out
    if principal.org_id is None or not agent_resolution.agents_on(session, principal.org_id):
        # No agent row on the grid: the picker keeps its built-ins and offers no agent, rather than a 403.
        return out
    own = session.scalars(
        select(Agent)
        .where(Agent.org_id == principal.org_id, Agent.is_catalog.is_(False), Agent.status == STATUS_ACTIVE)
        .order_by(Agent.display_name)
    )
    catalog = [row for row, enabled in _visible_catalog(session, principal) if enabled]
    for row in [*own, *catalog]:
        if agent_resolution.cell_refusal(session, row, principal.org_id) is not None:
            continue  # its row is off the org's grid: not offered (an existing pick is named at dispatch)
        if unsupported(row.manifest) is not None:
            continue  # an SDK v1 agent cannot run: never offered (an existing pick is refused at dispatch)
        reason = _unrunnable_reason(row, persona)
        out.append(ChoiceOut(
            kind="agent", id=row.id, display_name=row.display_name, agent_type=row.agent_type,
            version=row.version, is_catalog=row.is_catalog, disabled=reason is not None,
            removed=row.status == STATUS_REMOVED, reason=reason,
        ))
    return out


# ── The catalog ─────────────────────────────────────────────────────────────────────────────────────

_CATALOG_REF_SENTENCE = "A catalog connector is pinned to the reviewed commit: `ref` must be a full 40-character commit sha"


@router.get("/api/agent-catalog", response_model=list[AgentOut], dependencies=_ON_PLAN)
@feature("agents", "view")
def list_catalog(
    _: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[AgentOut]:
    """Every active connector with this org's enabled flag; a removed one only where this org enabled it. An
    operator's org filter picks whose flags (`_enabled_ids`)."""
    return [_out(row, enabled=enabled) for row, enabled in _visible_catalog(session, principal, listed=True)]


@router.post("/api/agent-catalog/{agent_id}/enable", response_model=AgentOut, dependencies=_SDK_ON_PLAN)
@feature("agents", "edit")
def enable_catalog_agent(
    agent_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AgentOut:
    _require_org_editor(principal)
    row, enabled = _catalog_row(session, principal, agent_id)
    if row.status != STATUS_ACTIVE:
        raise HTTPException(status_code=409, detail=f"{row.display_name} was removed from the catalog and cannot be enabled")
    if not enabled:
        session.add(AgentEnablement(org_id=principal.org_id, agent_id=row.id, created_by_id=user.id))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()  # enabled concurrently: the same end state
    return _out(row, enabled=True)


@router.post("/api/agent-catalog/{agent_id}/disable", response_model=AgentOut, dependencies=_ON_PLAN)
@feature("agents", "edit")
def disable_catalog_agent(
    agent_id: str,
    _: AdminUser = Depends(require_user),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AgentOut:
    _require_org_editor(principal)
    row, _enabled = _catalog_row(session, principal, agent_id)
    session.execute(
        delete(AgentEnablement).where(
            AgentEnablement.org_id == principal.org_id, AgentEnablement.agent_id == row.id
        )
    )
    session.commit()
    return _out(row, enabled=False)


@router.post("/api/agent-catalog", response_model=AgentOut, status_code=201, dependencies=_SDK_ON_PLAN)
@superadmin_only(Tenancy.OPERATOR)
def publish_catalog_agent(
    body: AgentIn,
    user: AdminUser = Depends(require_operator),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """Publish a reviewed connector, pinned to an exact commit, read with the operator's own connection."""
    if not is_commit_sha(body.ref.strip()):
        return _refused(agent_bundles.SnapshotRefused([_CATALOG_REF_SENTENCE]))
    # The operator's own org: `require_operator` holds it to the operator plane, where the catalog lives.
    return _create(session, user, body, org_id=principal.org_id, is_catalog=True)


@router.patch("/api/agent-catalog/{agent_id}", response_model=AgentOut, dependencies=_ON_PLAN)
@superadmin_only(Tenancy.OPERATOR)
def update_catalog_agent(
    agent_id: str,
    body: CatalogPatch,
    user: AdminUser = Depends(require_operator),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """Withdraw (`removed`) or restore (`active`) a connector, and/or move it to another exact commit. A
    withdrawn connector keeps its enablements, so those orgs see it marked rather than vanished."""
    row = row_by_uuid_or_404(session, Agent, agent_id, detail="no such connector")
    if not row.is_catalog:
        raise HTTPException(status_code=404, detail="no such connector")
    result = _edit(session, user, row, ref=body.ref, subpath=None)
    if result is not None:
        return result
    if body.status is not None:
        row.status = body.status
    stamp_actor(row, user)
    wanted = row.slug
    if not _committed(session, row):
        return _refused(agent_bundles.SnapshotRefused([agent_bundles.slug_taken_sentence(wanted)]))
    session.refresh(row)
    return _out(row)

