"""The agent-facing project-settings channel: a runner reconciles its own project's governance row
and default env-var catalogue instead of parking to ask an operator.

The authority is the launcher's: every write resolves the task's launcher
(`app/task_ancestry.py::launcher_user_id`) and runs the operator's own writer under that `Principal`
(`routes/projects.py::apply_project_update`, `routes/project_env.py`), so nothing here re-decides the
governance door, secret-shape guard or grants. Only the caller's own project (no `project_id`
argument), no reach-widening field (`PROJECT_SETTINGS_EXCLUDED_FIELDS`), project-default env tier only.
`_task_or_404_for_node`'s declared gaps now also reach these writes (accepted). Auth is
`routes/agent/auth.py::_check_token_for_task` only (`@infra`; the token, bound to the declared caller task).
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import node_credentials, project_repos
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import infra
from app.authz.object_access import require_object_access
from app.authz.principal import Principal, principal_for_user
from app.authz.visibility import ObjectAccess, has_project_key
from app.db import get_session
from app.models.identity import AdminUser
from app.models.projects import Project, ProjectRepo
from app.models.tasks import Task
from app.persona_registry import (
    PROJECT_SETTINGS_EXCLUDED_FIELDS,
    PROJECT_SETTINGS_LICENCE_BY_PERSONA,
    ProjectSettingsLicence,
)
from app.task_ancestry import launcher_user_id

# The one door onto `/api/agent/*` (enforced by
# `test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`), and the shared
# CP-side task/node binding backstop.
from app.routes.agent.auth import _check_token_for_task, _task_or_404_for_node
from app.routes.agent.task_secrets import _granted_credentials
from app.routes.project_env import (
    EnvVarOut,
    EnvVarWriteIn,
    create_env_var,
    default_tier_catalogue_out,
    default_tier_row_or_404,
    delete_env_var,
    update_env_var,
)
from app.routes.projects import (
    GOVERNANCE_FIELDS,
    ProjectRepoIn,
    ProjectRepoOut,
    TechStackEntry,
    project_repo_out,
    apply_project_update,
    has_authority_to_author_governance,
)
from app.entity_shape import StrictModel

log = logging.getLogger("agent-project-settings")

# No `prefix=` on the router: the public/private ALB split test reads each `@infra` path off the
# decorator to prove the public ALB refuses it. So every decorator spells `/api/agent/...`
# in full; a relative path would register outside the agent plane.
router = APIRouter(tags=["agent-project-settings"])


# ── The three questions every route here asks, in this order ──────────────────────────────────────────
#
# Which task is calling (and may this node speak for it), does its persona hold a licence, and whose
# authority does the write run under. Each answer is a precondition of the next, so a refusal names
# the caller's actual problem.


def _licensed_task(
    session: Session, caller: node_credentials.AgentCaller, task_id: int
) -> tuple[Task, ProjectSettingsLicence]:
    """The caller's own task and its persona's licence, or a refusal.

    404 first, via `_task_or_404_for_node` (node binding and cross-org floor), so a node with no business
    naming the task learns nothing. Then 403 on the licence: "your task, but not your persona's
    authority" is actionable and discloses nothing the runner does not already know."""
    task = _task_or_404_for_node(session, caller, task_id)
    licence = PROJECT_SETTINGS_LICENCE_BY_PERSONA.get(task.persona)
    if licence is None:
        log.warning(
            "project settings: refused task=%s (persona=%s holds no project-settings licence)",
            task_id, task.persona,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"task {task_id} (persona={task.persona!r}) may not read or write its project's "
                "settings. Reconcile the repo TO the governance row you were dispatched against, and "
                "where the row itself is wrong, name it in your report and your settle summary instead."
            ),
        )
    return task, licence


def _project_of(session: Session, task: Task) -> Project:
    """The caller's own project, or a 409 naming the reason there is none.

    Both arms guard states the schema currently forbids (`project_id` is NOT NULL with an FK). 409, not
    404 (the caller named a task, not a project) and not an assert (a data conflict, not a broken CP)."""
    if task.project_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"task {task.id} names no project, so it has no project settings to read or write",
        )
    project = session.get(Project, task.project_id)
    if project is None:
        # Unconstructible today (FK); a 409 for the reason above.
        raise HTTPException(
            status_code=409,
            detail=f"task {task.id} names project {task.project_id}, which no longer exists",
        )
    return project


def _launcher_authority(session: Session, task: Task) -> tuple[AdminUser, Principal]:
    """The person whose authority this write runs under — the task's launcher — or a 403.

    The ancestry walk, not `task.created_by_id`: pipeline-filed children have no direct creator
    (`routes/agent/task_secrets.py::_task_grant_principal`). Same resolution as the credential roster.

    None means deny, and a `None` principal must never be passed on: `app/authz/` reads it as the
    agent principal and allows everything. Placement reads the same silence the other way on purpose.
    Also a schema requirement: `created_by_id`/`updated_by_id` are NOT NULL, so a write needs a real user."""
    launcher_id = launcher_user_id(session, task.id)
    user = session.get(AdminUser, launcher_id) if launcher_id is not None else None
    if user is None:
        log.warning(
            "project settings: refused task=%s (no launcher resolves: id=%s)", task.id, launcher_id
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"task {task.id} resolves no launching user, so there is no authority for this write to "
                "run under and nobody to attribute it to. An operator has to make this change, or "
                "re-file the task under their own account."
            ),
        )
    return user, principal_for_user(session, user)


#: The access mode each project action implies (`view` = read, `edit` = write), matching the console
#: twins `GET /{project_id}/env-vars` and `GET /{project_id}/members`.
_PROJECT_ACTION_MODES: dict[str, ObjectAccess] = {
    "view": ObjectAccess.READ,
    "edit": ObjectAccess.WRITE,
}


def _require_the_launchers_project_key(
    session: Session,
    project: Project,
    launcher: AdminUser,
    principal: Principal,
    *,
    action: str,
) -> None:
    """The console route's own declaration, re-applied to the launcher — the half of the operator path
    that is a decorator and so does not travel with an extracted writer.

    `apply_project_update` re-applies the governance door and validators but not the route decorator's
    object access, so without this a keyless launcher could write ordinary columns (`port_block`,
    `notes`, ...) and read every command and default-tier literal in cleartext.

    Asks the guard's two questions through its own helpers, in order: `require_object_access` (404, or
    403 for a write on a readable row), then `has_project_key`. The env-var writers already
    call `has_project_key` inside `_authorize_tier`, so they only need the `view` half from here."""
    mode = _PROJECT_ACTION_MODES[action]
    key = permission_key("projects", action)
    require_object_access(session, principal, "project", project.id, mode=mode)
    if not has_project_key(principal, key, project.id):
        log.warning(
            "project settings: refused project=%s to launcher=%s (no %s on it)",
            project.id, launcher.id, key,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"this task runs with the authority of the user who launched it, and they hold no "
                f"{key} on this project — the same refusal they would get in the console. Report what "
                "the row should say and let an operator change it."
            ),
        )


def _refuse_a_field_outside_the_licence(
    sent: dict[str, Any], licence: ProjectSettingsLicence, task: Task
) -> None:
    """403 the moment a request names a field this persona's licence does not cover.

    The second door: `AgentProjectSettingsIn`'s `extra="forbid"` 422s first in practice, but the model is
    one persona's shape while licences are per persona, so the licence itself must be checked. Names the
    field and why, so a model caller does not retry a permanent refusal."""
    outside = sorted(set(sent) - licence.fields)
    if not outside:
        return
    excluded = [name for name in outside if name in PROJECT_SETTINGS_EXCLUDED_FIELDS]
    log.warning(
        "project settings: refused task=%s fields=%s (outside persona=%s licence)",
        task.id, outside, task.persona,
    )
    detail = (
        f"task {task.id} (persona={task.persona!r}) may not write {', '.join(outside)}. Its licence "
        f"covers {licence.constraint}"
    )
    if excluded:
        detail += (
            f" — and {', '.join(excluded)} is withheld from every runner by decision, not by this "
            "licence's narrowness, because it widens the runner's own reach rather than describing the "
            "repo. Name the change you want in your report and let an operator make it."
        )
    raise HTTPException(status_code=403, detail=detail)


def _refuse_a_repository_change(
    session: Session, project: Project, rows: Optional[list[dict[str, Any]]], task: Task
) -> None:
    """403 for a `repos` write that adds, removes or re-points a repository: which hosts a runner's token is sent
    to is placement, withheld from every runner as `repo_url` is (`PROJECT_SETTINGS_EXCLUDED_FIELDS`). Its
    branches, its primary and its order describe the repo, so those pass to `apply_project_update`."""
    if rows is None:
        return
    # Locked until the write commits, so an operator's re-point cannot land between this check and the write.
    session.scalars(select(ProjectRepo.id).where(ProjectRepo.project_id == project.id).with_for_update()).all()
    # The exact spelling (spaces aside): the write stores what is sent, so a case variant would re-point it.
    stored = {row.id: row.repo_url.strip() for row in project_repos.repos_of(session, project.id)}
    named = {row.get("id"): (row.get("repo_url") or "").strip() for row in rows}
    unchanged = len(named) == len(rows) and named == stored
    if unchanged and all(row.get("forge") is None and row.get("forge_base_url") is None for row in rows):
        return
    log.warning("project settings: refused task=%s repository set change on project=%s", task.id, project.id)
    raise HTTPException(
        status_code=403,
        detail=(
            f"task {task.id} may not add, remove or re-point a repository: which hosts a runner's token is sent "
            "to is withheld from every runner. Send every connected repository with its id and unchanged "
            "repo_url (project_get lists them) to change branches, the primary or the order; report any "
            "other change for an operator to make."
        ),
    )


# ── The wire shapes ───────────────────────────────────────────────────────────────────────────────────


class AgentProjectSettingsIn(StrictModel):
    """A partial update of the caller's own project row.

    The fields are `PROJECT_SETTINGS_WRITABLE_FIELDS`; the API test suite asserts
    the writable/excluded partition of `ProjectUpdate` is total, so a new column is never agent-writable
    by accident. `extra="forbid"` is the first refusal. Same partial-update semantics and validator as
    the operator path (`apply_project_update`)."""

    name: Optional[str] = None
    integration_branch: Optional[str] = None
    branch_prefix: Optional[str] = None
    gate_quick_cmd: Optional[str] = None
    gate_full_cmd: Optional[str] = None
    stack_up_cmd: Optional[str] = None
    stack_down_cmd: Optional[str] = None
    push_enabled: Optional[bool] = None
    default_model: Optional[str] = None
    port_vars: Optional[list[str]] = None
    port_block: Optional[int] = None
    tech_stack: Optional[list[TechStackEntry]] = None
    notes: Optional[str] = None
    # Retired: accepted and ignored so a runner on a not-yet-upgraded node, whose tool still
    # offers it, loses none of the call's other fields. Goes with the `projects.fleet_enabled` column.
    fleet_enabled: Optional[bool] = None
    # The connected repositories as a whole set, as on `PUT /api/projects` — narrowed by
    # `_refuse_a_repository_change`: a runner re-orders, re-branches and picks the primary, never re-points.
    repos: Optional[list[ProjectRepoIn]] = None


class AgentProjectSettingsOut(BaseModel):
    """What a runner is told about its own project: the current value of every field it may write, the
    verdict on the governance door, and the default-tier env-var catalogue.

    Served together because they are read together, so no runner discovers its authority by a 403.
    `may_author_governance` comes from `routes/projects.py::has_authority_to_author_governance`, resolved
    for the launcher."""

    project_id: int
    project_name: str
    # The read is wider than the write by `repo_url`: excluded from writes (placement), but useful and
    # not secret.
    repo_url: Optional[str] = None
    may_author_governance: bool
    #: Names which fields the verdict above is about.
    governance_fields: list[str]
    #: The delivery pair the runner ships under, which the PUT edits.
    integration_branch: str
    branch_prefix: str
    gate_quick_cmd: str
    gate_full_cmd: str
    stack_up_cmd: str
    stack_down_cmd: str
    push_enabled: bool
    default_model: Optional[str] = None
    port_vars: Optional[list[str]] = None
    port_block: Optional[int] = None
    tech_stack: Optional[list[dict[str, str]]] = None
    notes: Optional[str] = None
    #: `EnvVarOut` unchanged, the single serialized shape. Project-default tier only.
    env_vars: list[EnvVarOut]
    #: Every connected repository, with the ids a `repos` write names.
    repos: list[ProjectRepoOut] = []


class AgentEnvVarWriteIn(StrictModel):
    """Create or replace one project-default environment variable.

    `env_var_id` selects create vs update in the body because the daemon substitutes only `{task_id}`
    into the path. Present, it must name a project-default row of this project or 404
    (`default_tier_row_or_404`).

    `credential_name`, not an id: the runner's roster (`TaskCredentialSummary`) exposes no ids, only
    scope-relative paths (`org/aws`, `projects/<project>/aws`, `me/<name>`). It is resolved under the
    launcher's grants. No `user_id` field: every row here is a project default."""

    env_var_id: Optional[UUID] = None
    name: str
    description: str = ""
    # Same `Literal` as `EnvVarWriteIn.source`, refused at the request boundary. Redundant with
    # `_operator_write_body`'s catch; kept because a closed vocabulary belongs at the door.
    source: Literal["literal", "credential"]
    literal: Optional[str] = None
    credential_name: Optional[str] = None
    credential_field: Optional[str] = None


# ── The four routes ───────────────────────────────────────────────────────────────────────────────────


def _settings_out(
    session: Session, project: Project, task: Task, launcher: AdminUser, principal: Principal
) -> AgentProjectSettingsOut:
    """Serialize the settings payload — a plain function, called by the read route and by the tail of
    the write.

    Not a call of the guarded read handler: that re-ran the `projects:view` check, so an edit-only
    launcher landed a write and then got a 403, which a model caller retries. A plain function also
    avoids unresolved `Depends` parameters."""
    return AgentProjectSettingsOut(
        project_id=project.id,
        project_name=project.name,
        repo_url=project.repo_url,
        may_author_governance=has_authority_to_author_governance(
            session, launcher, principal, project
        ),
        governance_fields=list(GOVERNANCE_FIELDS),
        integration_branch=project.integration_branch,
        branch_prefix=project.branch_prefix,
        gate_quick_cmd=project.gate_quick_cmd,
        gate_full_cmd=project.gate_full_cmd,
        stack_up_cmd=project.stack_up_cmd,
        stack_down_cmd=project.stack_down_cmd,
        push_enabled=project.push_enabled,
        default_model=project.default_model,
        port_vars=project.port_vars,
        port_block=project.port_block,
        tech_stack=project.tech_stack,
        notes=project.notes,
        env_vars=default_tier_catalogue_out(session, project),
        repos=[project_repo_out(row) for row in project_repos.repos_of(session, project.id)],
    )


@router.get("/api/agent/tasks/{task_id}/project", response_model=AgentProjectSettingsOut)
@infra(Tenancy.ORG_SCOPED)
def get_task_project_settings(
    task_id: int,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> AgentProjectSettingsOut:
    """This task's project settings, the governance verdict, and the default env-var catalogue.

    Read takes the same licence as the write: what a persona is told about its project is itself what
    the licence records."""
    task, _licence = _licensed_task(session, caller, task_id)
    project = _project_of(session, task)
    launcher, principal = _launcher_authority(session, task)
    _require_the_launchers_project_key(session, project, launcher, principal, action="view")
    return _settings_out(session, project, task, launcher, principal)


@router.put("/api/agent/tasks/{task_id}/project", response_model=AgentProjectSettingsOut)
@infra(Tenancy.ORG_SCOPED)
def update_task_project_settings(
    task_id: int,
    body: AgentProjectSettingsIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> AgentProjectSettingsOut:
    """Apply a partial update to this task's project row, with the launcher's authority.

    Every refusal below is the operator path's own (`apply_project_update` is the single writer), and
    the audit names the launcher. This adds only who the actor is and which fields may be named. The
    403 to expect is the governance one: a runner then reconciles the rest and reports the seven
    columns as a blocker for a human."""
    task, licence = _licensed_task(session, caller, task_id)
    project = _project_of(session, task)
    launcher, principal = _launcher_authority(session, task)

    # `exclude_unset`, like the operator route; the licence is checked against the fields actually sent.
    _require_the_launchers_project_key(session, project, launcher, principal, action="edit")

    sent = body.model_dump(exclude_unset=True)
    sent.pop("fleet_enabled", None)
    _refuse_a_field_outside_the_licence(sent, licence, task)
    _refuse_a_repository_change(session, project, sent.get("repos"), task)
    if not sent:
        # An empty update is a 422, so a model caller does not read a no-op as "landed".
        raise HTTPException(
            status_code=422,
            detail="no settings were sent, so there is nothing to update",
        )

    apply_project_update(session, project, sent, launcher, principal)
    log.info(
        "project settings: task=%s persona=%s updated project=%s fields=%s (as launcher=%s)",
        task.id, task.persona, project.id, sorted(sent), launcher.id,
    )
    # Serialized from the row in hand, never via the guarded read — see `_settings_out`.
    return _settings_out(session, project, task, launcher, principal)


@router.put("/api/agent/tasks/{task_id}/project/env-vars", response_model=EnvVarOut)
@infra(Tenancy.ORG_SCOPED)
def write_task_project_env_var(
    task_id: int,
    body: AgentEnvVarWriteIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> EnvVarOut:
    """Create a project-default environment variable, or replace one, with the launcher's authority.

    One route discriminated by `env_var_id` (see `AgentEnvVarWriteIn`); both branches call the
    operator-path writers unchanged, keeping the secret-shape refusal and grant check unbypassable.
    The body is translated, not revalidated: `credential_name` becomes `credential_id` and `user_id` is
    pinned to `None`."""
    task, licence = _licensed_task(session, caller, task_id)
    _refuse_a_licence_covering_no_env_vars(task, licence)
    project = _project_of(session, task)
    launcher, principal = _launcher_authority(session, task)
    # The `projects:view` half: the console twins check `view` via decorator and `edit` inside the
    # writer, so calling the writer directly ran only the latter.
    _require_the_launchers_project_key(session, project, launcher, principal, action="view")

    write = _operator_write_body(session, task, body)

    if body.env_var_id is None:
        out = create_env_var(session, project, write, launcher, principal, viewer_id=None)
        log.info(
            "project env var: task=%s created %s on project=%s (as launcher=%s)",
            task.id, body.name, project.id, launcher.id,
        )
        return out

    row = default_tier_row_or_404(session, project.id, body.env_var_id)
    out = update_env_var(session, project, row, write, launcher, principal, viewer_id=None)
    log.info(
        "project env var: task=%s updated %s on project=%s (as launcher=%s)",
        task.id, body.name, project.id, launcher.id,
    )
    return out


@router.delete("/api/agent/tasks/{task_id}/project/env-vars", status_code=204)
@infra(Tenancy.ORG_SCOPED)
def delete_task_project_env_var(
    task_id: int,
    env_var_id: UUID = Query(..., description="the variable's id, as the project read reports it"),
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> Response:
    """Remove a project-default environment variable, with the launcher's authority.

    The id is a query parameter (transport); another project's row or a per-user override is the same
    404, so a runner cannot learn who holds overrides."""
    task, licence = _licensed_task(session, caller, task_id)
    _refuse_a_licence_covering_no_env_vars(task, licence)
    project = _project_of(session, task)
    launcher, principal = _launcher_authority(session, task)
    # The `projects:view` half: the console twins check `view` via decorator and `edit` inside the
    # writer, so calling the writer directly ran only the latter.
    _require_the_launchers_project_key(session, project, launcher, principal, action="view")
    row = default_tier_row_or_404(session, project.id, env_var_id)
    name = row.name
    delete_env_var(session, project, row, launcher, principal)
    log.info(
        "project env var: task=%s deleted %s from project=%s (as launcher=%s)",
        task.id, name, project.id, launcher.id,
    )
    return Response(status_code=204)


def _refuse_a_licence_covering_no_env_vars(task: Task, licence: ProjectSettingsLicence) -> None:
    """403 a persona whose licence covers the project row but not its env-var catalogue.

    One home for both env-var routes. Names neither write nor delete: `licence.env_vars` is one boolean
    for the whole catalogue."""
    if licence.env_vars:
        return
    log.warning(
        "project env var: refused task=%s (persona=%s licence covers no env vars)",
        task.id, task.persona,
    )
    raise HTTPException(
        status_code=403,
        detail=(
            f"task {task.id} (persona={task.persona!r}) may not change this project's environment "
            f"variables. Its licence covers {licence.constraint}."
        ),
    )


def _operator_write_body(
    session: Session, task: Task, body: AgentEnvVarWriteIn
) -> EnvVarWriteIn:
    """Translate this surface's body into the operator model the writers take, answering 422 for a rule
    that model enforces rather than letting it raise a 500.

    `EnvVarWriteIn` is constructed here, not parsed from a request, so its `pydantic.ValidationError`
    (e.g. `name` grammar) would propagate as a 500 and a model caller would retry. The message is built
    from `loc` and `msg` only, never the input, so a future rule on `literal` cannot disclose it."""
    try:
        return EnvVarWriteIn(
            name=body.name,
            # Pinned, never from the request: the project-default tier is the only one reached, and
            # `_authorize_tier` reads this to decide the authority required.
            user_id=None,
            source=body.source,
            literal=body.literal,
            credential_id=_credential_id_for(session, task, body),
            credential_field=body.credential_field,
            description=body.description,
        )
    except ValidationError as exc:
        rules = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'body'}: {error['msg']}"
            for error in exc.errors()
        )
        log.warning("project env var: refused task=%s on the operator model's rules", task.id)
        raise HTTPException(status_code=422, detail=rules) from exc


def _credential_id_for(
    session: Session, task: Task, body: AgentEnvVarWriteIn
) -> Optional[UUID]:
    """Resolve `credential_name` — a scope-relative path (`routes/agent/task_secrets.py::runner_path`) — to the
    id the operator's writer expects, bounded by the roster.

    `_granted_credentials` is the roster's own function (launcher's grants, no superadmin bypass,
    runner-account credentials excluded), so only paths `credential_list` showed resolve.
    Refusals mirror `routes/agent/task_secrets.py::get_task_credential`: 404 for missing or ungranted (not
    enumerable), 409 for a path several granted rows share. Returns `None` when no name was sent;
    `source` is the writer's to check."""
    if body.credential_name is None:
        return None
    matches = _granted_credentials(session, task, path=body.credential_name)
    if not matches:
        log.warning(
            "project env var: refused credential=%r for task=%s (no such credential, or no grant)",
            body.credential_name, task.id,
        )
        raise HTTPException(
            status_code=404,
            detail=(
                f"no credential at path {body.credential_name!r} is available to this task — "
                "credential_list is the roster of the paths that are"
            ),
        )
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(matches)} credentials share the path {body.credential_name!r} here — rename "
                "them to disambiguate"
            ),
        )
    return matches[0][1].id
