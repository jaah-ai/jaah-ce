"""The agent-facing project-settings channel: a runner reconciles its own project's governance row
and default env-var catalogue instead of parking to ask an operator.

The authority is the launcher's: every write resolves the task's launcher
(`app/task_ancestry.py::launcher_user_id`) and runs the operator's own writer under that `Principal`
(`routes/projects.py::apply_project_update`, `routes/project_env.py`), so nothing here re-decides the
governance door, secret-shape guard or grants. Only the caller's own project (no `project_id`
argument), no reach-widening field (`PROJECT_SETTINGS_EXCLUDED_FIELDS`), project-default env tier only.
Every write is held by the loop guard (`routes/agent/auth.py::require_human_started`): settings and env vars decide
what later work does, so a task a machine started may read them but never change them.
The release pipeline (`PUT .../project/pipeline`) is the console's own write too
(`routes/releases.py::replace_pipeline`), under the launcher's `projects:edit`.
Deploy tracking (`PUT .../project/deploys`) is the console's Connect deploys, GitHub only and under the same key.
`_task_or_404_for_node`'s declared gaps now also reach these writes (accepted). Auth is
`routes/agent/auth.py::_check_token_for_task` only (`@infra`; the token, bound to the declared caller task).
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jaah_repo_replace, jaah_repos, node_credentials, placement_groups, project_repos
from app.authz.catalog import Tenancy, permission_key
from app.authz.declarations import infra
from app.authz.object_access import require_object_access
from app.authz.principal import Principal, principal_for_user
from app.authz.visibility import ObjectAccess, has_project_key
from app.db_async import on_session
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.projects import Project, ProjectRepo
from app.models.tasks import Task
from app.persona_registry import (
    PROJECT_SETTINGS_EXCLUDED_FIELDS,
    PROJECT_SETTINGS_LICENCE,
)
from app.task_ancestry import launcher_user_id

# The one door onto `/api/agent/*` (enforced by
# `test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`), and the shared
# CP-side task/node binding backstop.
from app.routes.agent.auth import (
    _check_token_for_task,
    _task_or_404_for_node,
    project_of_task,
    require_human_started,
)
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
from app.releases import basics, pipeline, stages
from app.pipeline_gates import full_gate
from app.pipeline_gates.schema import GateIn, TargetIn
from app.routes.releases import (
    PipelineIn,
    PipelineStageIn,
    PipelineStepIn,
    StepWhoIn,
    pipeline_specs,
    replace_pipeline,
)
from app.routes.projects import (
    GOVERNANCE_FIELDS,
    ProjectRepoIn,
    ProjectRepoOut,
    ProjectSettingsFieldsIn,
    ProjectSettingsFieldsOut,
    project_repo_out,
    has_authority_to_author_governance,
    apply_project_update,
    require_assignment_writer,
)
from app.entity_shape import StrictModel, from_row

log = logging.getLogger("agent-project-settings")

# No `prefix=` on the router: the public/private ALB split test reads each `@infra` path off the
# decorator to prove the public ALB refuses it. So every decorator spells `/api/agent/...`
# in full; a relative path would register outside the agent plane.
router = APIRouter(tags=["agent-project-settings"])

#: `AgentProjectSettingsIn` fields that are not project columns, so not the licence's: each has its own gate.
#: `node_groups` is the console's project group PUT, under the launcher's `require_assignment_writer`.
NON_COLUMN_FIELDS: frozenset[str] = frozenset({"node_groups"})


# ── The two questions every route here asks, in this order ────────────────────────────────────────────
#
# Which task is calling (and may this node speak for it: `_task_or_404_for_node`), and whose authority the
# write runs under. Every persona holds the one licence (`persona_registry.PROJECT_SETTINGS_LICENCE`), so
# the second answer — the launcher's own project keys — is the bound.


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
    object access, so without this a keyless launcher could write ordinary columns (`port_vars`,
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


def _refuse_a_field_outside_the_licence(sent: dict[str, Any], task: Task) -> None:
    """403 the moment a request names a field the licence does not cover.

    The second door: `AgentProjectSettingsIn`'s `extra="forbid"` 422s first in practice, but the licence is
    the enforced answer, so it is checked too. Names the field and why, so a model caller does not retry a
    permanent refusal."""
    licence = PROJECT_SETTINGS_LICENCE
    outside = sorted(set(sent) - licence.fields)
    if not outside:
        return
    excluded = [name for name in outside if name in PROJECT_SETTINGS_EXCLUDED_FIELDS]
    log.warning(
        "project settings: refused task=%s fields=%s (outside the licence)", task.id, outside,
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


# The settings columns are the console writers' own block (`ProjectSettingsFieldsIn`), spelled once; the
# docstring is the wire description, so the pointer stays out of it.
class AgentProjectSettingsIn(StrictModel, ProjectSettingsFieldsIn):
    """A partial update of the caller's own project row.

    The fields are `PROJECT_SETTINGS_WRITABLE_FIELDS`; the API test suite asserts
    the writable/excluded partition of `ProjectUpdate` is total, so a new column is never agent-writable
    by accident. `extra="forbid"` is the first refusal. Same partial-update semantics and validator as
    the operator path (`apply_project_update`)."""

    name: Optional[str] = None
    # The connected repositories as a whole set, as on `PUT /api/projects` — narrowed by
    # `_refuse_a_repository_change`: a runner re-orders, re-branches and picks the primary, never re-points.
    repos: Optional[list[ProjectRepoIn]] = None
    # The answer to the 409 a primary switch off a Jaah-made repository earns (`app/jaah_repo_replace.py`).
    jaah_repo: Optional[Literal["move", "retire"]] = None
    # The project's node groups by name (`[]` = All), not a project column: `_set_node_groups` writes it.
    node_groups: Optional[list[str]] = Field(default=None, max_length=100)


class AgentNodeGroupsOut(BaseModel):
    """Where the project runs: `mode` "all" (every org node) or "groups"; `groups` their names; `active` false when
    groups were kept but the org's plan has Node groups off (the project then runs on All)."""

    mode: Literal["all", "groups"]
    groups: list[str]
    active: bool


class AgentProjectSettingsOut(ProjectSettingsFieldsOut):
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
    #: Names which fields the verdict above is about; their current values are the inherited
    #: `ProjectSettingsFieldsOut` block, which the PUT edits.
    governance_fields: list[str]
    #: `EnvVarOut` unchanged, the single serialized shape. Project-default tier only.
    env_vars: list[EnvVarOut]
    #: Every connected repository, with the ids a `repos` write names.
    repos: list[ProjectRepoOut] = []
    #: The release pipeline, as the console's `GET /api/projects/{id}/pipeline` serves it
    #: (`app/releases/pipeline.py::read`); the stage ids a `pipeline_set` names. None without a repository.
    pipeline: Optional[dict[str, Any]] = None
    #: The project's node groups (`app/placement_groups.py::project_groups`).
    node_groups: Optional[AgentNodeGroupsOut] = None


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
    source: Literal["literal", "credential", "port"]
    literal: Optional[str] = None
    credential_name: Optional[str] = None
    credential_field: Optional[str] = None
    # `source='port'` only (`EnvVarWriteIn.port_preferred`); None = auto-assigned per slot. Its range is the
    # operator model's, refused as a 422 by `_operator_write_body`.
    port_preferred: Optional[int] = None


class AgentStepWhoIn(StrictModel, StepWhoIn):
    """`StepWhoIn`, strict: a misspelled key 422s instead of resetting the step to its default."""


class AgentGateIn(StrictModel, GateIn):
    """`GateIn`, strict."""


class AgentTargetIn(StrictModel, TargetIn):
    """`TargetIn`, strict."""


class AgentPipelineStageIn(StrictModel, PipelineStageIn):
    """`PipelineStageIn`, strict."""

    target: Optional[AgentTargetIn] = None
    after: Optional[list[AgentGateIn]] = Field(default=None, max_length=100)


class AgentPipelineStepIn(StrictModel, PipelineStepIn):
    """`PipelineStepIn`, strict."""

    who: Optional[AgentStepWhoIn] = None
    approvers: Optional[AgentStepWhoIn] = None
    gates: Optional[list[AgentGateIn]] = Field(default=None, max_length=100)


class AgentPipelineIn(StrictModel, PipelineIn):
    """The console's `PipelineIn`, strict at every level: a runner's misspelled field would otherwise land as a
    default (an approval dropped, a step made manual) while the call reports success."""

    stages: list[AgentPipelineStageIn]
    steps: list[AgentPipelineStepIn]


class AgentConnectDeploysIn(StrictModel):
    """The workflow files that deploy to Production (`production_qa/connect.py::connect`'s `workflows`)."""

    deploy_workflows: list[str] = Field(max_length=50)


# ── The six routes ────────────────────────────────────────────────────────────────────────────────────


def _settings_out(
    session: Session, project: Project, task: Task, launcher: AdminUser, principal: Principal
) -> AgentProjectSettingsOut:
    """Serialize the settings payload — a plain function, called by the read route and by the tail of
    the write.

    Not a call of the guarded read handler: that re-ran the `projects:view` check, so an edit-only
    launcher landed a write and then got a 403, which a model caller retries. A plain function also
    avoids unresolved `Depends` parameters."""
    return from_row(
        AgentProjectSettingsOut, project,
        project_id=project.id,
        project_name=project.name,
        # The merge step's command gates (the one control), never the mirror column.
        gate_full_cmd=full_gate.full_gate_command(session, project),
        may_author_governance=has_authority_to_author_governance(
            session, launcher, principal, project
        ),
        governance_fields=list(GOVERNANCE_FIELDS),
        env_vars=default_tier_catalogue_out(session, project),
        repos=[project_repo_out(row) for row in project_repos.repos_of(session, project.id)],
        pipeline=pipeline.read(session, project) if stages.has_repo(project) else None,
        node_groups=_node_groups_out(session, project),
    )


def _node_groups_out(session: Session, project: Project) -> AgentNodeGroupsOut:
    view = placement_groups.project_groups(session, project.id)
    return AgentNodeGroupsOut(mode=view.mode, groups=[g.name for g in view.groups], active=view.active)


def _set_node_groups(
    session: Session, project: Project, names: list[str], launcher: AdminUser, principal: Principal,
) -> None:
    """Put the project on the groups named `names` (`[]` = All), as the console's project PUT does
    (`routes/placement_groups.py::put_project_placement_groups`) under the launcher's authority: the same
    `require_assignment_writer` gate, the same plan switch (403 for groups while it is off), always confirmed
    (a runner cannot answer the move dialog; running tasks finish where they are, PRD D16). Flushes, never commits."""
    require_assignment_writer(launcher, principal)
    jaah_repos.refuse_member_edit(session, project.id, launcher, principal)
    if names:
        # The plan's answer before the names': with the switch off, "not on your plan" is the one to act on.
        placement_groups.require_groups_on(session, principal)
    group_ids = placement_groups.group_ids_by_name(session, project.org_id, names) if names else []
    placement_groups.set_project_groups(session, project, group_ids, principal, confirm=True)


@router.get("/api/agent/tasks/{task_id}/project", response_model=AgentProjectSettingsOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def get_task_project_settings(
    session: Session,
    task_id: int,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> AgentProjectSettingsOut:
    """This task's project settings, the governance verdict, and the default env-var catalogue.

    Read takes the same launcher check as the write (`projects:view`)."""
    task = _task_or_404_for_node(session, caller, task_id)
    project = project_of_task(session, task)
    launcher, principal = _launcher_authority(session, task)
    _require_the_launchers_project_key(session, project, launcher, principal, action="view")
    return _settings_out(session, project, task, launcher, principal)


@router.put("/api/agent/tasks/{task_id}/project", response_model=AgentProjectSettingsOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def update_task_project_settings(
    session: Session,
    task_id: int,
    body: AgentProjectSettingsIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> AgentProjectSettingsOut:
    """Apply a partial update to this task's project row, with the launcher's authority.

    Every refusal below is the operator path's own (`apply_project_update` is the single writer), and
    the audit names the launcher. This adds only who the actor is and which fields may be named. The
    403 to expect is the governance one: a runner then reconciles the rest and reports the seven
    columns as a blocker for a human."""
    task = _task_or_404_for_node(session, caller, task_id)
    require_human_started(session, task, what="changing project settings")
    project = project_of_task(session, task)
    launcher, principal = _launcher_authority(session, task)

    # `exclude_unset`, like the operator route; the licence is checked against the fields actually sent.
    _require_the_launchers_project_key(session, project, launcher, principal, action="edit")

    sent = body.model_dump(exclude_unset=True)
    # Not a project column, so not the licence's: the placement write has its own gate (`_set_node_groups`).
    node_groups = sent.pop("node_groups", None)  # the one `NON_COLUMN_FIELDS` member
    _refuse_a_field_outside_the_licence(sent, task)
    _refuse_a_repository_change(session, project, sent.get("repos"), task)
    if not sent and node_groups is None:
        # An empty update is a 422, so a model caller does not read a no-op as "landed".
        raise HTTPException(status_code=422, detail="no settings were sent, so there is nothing to update")
    if node_groups is not None:
        # First, in the same transaction as the settings write below, which commits both.
        _set_node_groups(session, project, node_groups, launcher, principal)
        if not sent:
            # Wakes the org's open consoles, as the console's own write does; commits.
            emit_console_event(session, entity="worktree", id=0, action="upserted", org_id=project.org_id, commit=True)
            log.info("project settings: task=%s set node groups on project=%s to %s (as launcher=%s)",
                     task.id, project.id, node_groups or "All", launcher.id)
            return _settings_out(session, project, task, launcher, principal)

    if sent.get("jaah_repo") == jaah_repo_replace.RETIRE or (
        sent.get("jaah_repo") is not None and jaah_repo_replace.live_app(session, project.id)
    ):
        # Retiring archives the project's code, and with a hosted app running either answer deletes it: irreversible,
        # so a person's call in the console, never a runner's. A runner may only move, and only with no app running.
        raise HTTPException(status_code=403, detail=jaah_repo_replace.RUNNER_MAY_NOT_RETIRE)
    try:
        apply_project_update(session, project, sent, launcher, principal, runner=True)
    except HTTPException as exc:
        # The console's dialog reads the structured 409; a runner's tool error relays a string `detail` only.
        if exc.status_code == 409 and isinstance(exc.detail, dict) and (
            exc.detail.get("code") == jaah_repo_replace.CONFLICT_CODE
        ):
            raise HTTPException(status_code=409, detail=jaah_repo_replace.as_sentence(exc.detail)) from None
        raise
    log.info(
        "project settings: task=%s persona=%s updated project=%s fields=%s (as launcher=%s)",
        task.id, task.persona, project.id, sorted(sent), launcher.id,
    )
    # Serialized from the row in hand, never via the guarded read — see `_settings_out`.
    return _settings_out(session, project, task, launcher, principal)


@router.put("/api/agent/tasks/{task_id}/project/env-vars", response_model=EnvVarOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def write_task_project_env_var(
    session: Session,
    task_id: int,
    body: AgentEnvVarWriteIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> EnvVarOut:
    """Create a project-default environment variable, or replace one, with the launcher's authority.

    One route discriminated by `env_var_id` (see `AgentEnvVarWriteIn`); both branches call the
    operator-path writers unchanged, keeping the secret-shape refusal and grant check unbypassable.
    The body is translated, not revalidated: `credential_name` becomes `credential_id` and `user_id` is
    pinned to `None`."""
    task = _task_or_404_for_node(session, caller, task_id)
    require_human_started(session, task, what="writing a project environment variable")
    project = project_of_task(session, task)
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
@on_session
def delete_task_project_env_var(
    session: Session,
    task_id: int,
    env_var_id: UUID = Query(..., description="the variable's id, as the project read reports it"),
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> Response:
    """Remove a project-default environment variable, with the launcher's authority.

    The id is a query parameter (transport); another project's row or a per-user override is the same
    404, so a runner cannot learn who holds overrides."""
    task = _task_or_404_for_node(session, caller, task_id)
    require_human_started(session, task, what="deleting a project environment variable")
    project = project_of_task(session, task)
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


@router.put("/api/agent/tasks/{task_id}/project/pipeline")
@infra(Tenancy.ORG_SCOPED)
@on_session
def replace_task_project_pipeline(
    session: Session,
    task_id: int,
    body: AgentPipelineIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> dict[str, Any]:
    """Replace this task's project release pipeline, with the launcher's authority.

    The console's own write (`routes/releases.py::replace_pipeline`) after the console's own check
    (`projects:edit`), so every refusal is the operator path's; only its structured details are flattened into
    a sentence, since a runner's tool error relays a string `detail` only. The project columns the save would
    write (`pipeline.project_fields`: the work stage's prefix, the integration branch) answer to the licence
    first, so `branch_prefix` stays out of reach here as it is on `PUT .../project`."""
    task = _task_or_404_for_node(session, caller, task_id)
    require_human_started(session, task, what="changing the release pipeline")
    project = project_of_task(session, task)
    launcher, principal = _launcher_authority(session, task)
    _require_the_launchers_project_key(session, project, launcher, principal, action="edit")
    if not stages.has_repo(project):
        raise HTTPException(
            status_code=404, detail="this project has no repository, so it has no release pipeline to set"
        )
    basics.require_editor(session, project)
    try:
        _refuse_a_field_outside_the_licence(pipeline.project_fields(project, *pipeline_specs(body)), task)
        out = replace_pipeline(session, project, body, launcher, principal)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and "message" in exc.detail:
            raise HTTPException(status_code=exc.status_code, detail=_pipeline_refusal(exc.detail)) from None
        raise
    log.info(
        "project pipeline: task=%s replaced the pipeline of project=%s with %d stages (as launcher=%s)",
        task.id, project.id, len(body.stages), launcher.id,
    )
    return out


def _pipeline_refusal(detail: dict[str, Any]) -> str:
    """A pipeline refusal's `{code, message, stage_index?}` (`app/releases/pipeline.py::refuse`) as one sentence."""
    where = f" (stage {detail['stage_index']}, counting from 0)" if "stage_index" in detail else ""
    return f"{detail.get('code', 'refused')}: {detail['message']}{where}"


#: Every forge but GitHub answers each new deploy secret once, for a person to paste — never to a runner.
_CONNECT_DEPLOYS_IN_CONSOLE = (
    "this project's repository is not on GitHub, so a person connects deploy tracking in the console "
    "(Customize › CI pipeline, or Pipeline › the Production stage)"
)


@router.put("/api/agent/tasks/{task_id}/project/deploys")
@infra(Tenancy.ORG_SCOPED)
@on_session
def connect_task_project_deploys(
    session: Session,
    task_id: int,
    body: AgentConnectDeploysIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
) -> dict[str, Any]:
    """Connect this task's project deploy tracking, with the launcher's authority: the console's Connect deploys
    (`routes/production_qa.py::connect_deploys`) after its own checks (`projects:edit`, a member never on a
    Jaah-managed project). GitHub only, where the connect installs the webhooks itself and keeps every secret
    sealed; the answer carries no secret."""
    from app.production_qa import connect as deploy_connect
    from app.triggers import SOURCE_GITHUB

    task = _task_or_404_for_node(session, caller, task_id)
    require_human_started(session, task, what="connecting deploy tracking")
    project = project_of_task(session, task)
    launcher, principal = _launcher_authority(session, task)
    _require_the_launchers_project_key(session, project, launcher, principal, action="edit")
    jaah_repos.refuse_member_edit(session, project.id, launcher, principal)
    forge, repository = deploy_connect.deploy_forge(session, project)
    if forge is None or repository is None:
        raise HTTPException(status_code=422, detail=deploy_connect.NO_REPOSITORY)
    if forge != SOURCE_GITHUB:
        raise HTTPException(status_code=409, detail=_CONNECT_DEPLOYS_IN_CONSOLE)
    connected = deploy_connect.connect(session, project, launcher, workflows=body.deploy_workflows, reveal=False)
    session.commit()
    installed = connected.installed
    out: dict[str, Any] = {
        "state": "connected" if installed is not None and installed.state == "installed" else "needs_setup",
        "reason": None, "install_url": None,
        "deploy_workflows": connected.deploy_workflows,
        "triggers": [{"name": t.name, "events": deploy_connect.events_of(t)} for t, _secret in connected.triggers],
    }
    if out["state"] == "needs_setup":
        from app.git_reach import app_install_url

        out["reason"] = installed.reason if installed is not None else None
        out["install_url"] = app_install_url()
    log.info(
        "project deploys: task=%s connected project=%s (%s, as launcher=%s)",
        task.id, project.id, out["state"], launcher.id,
    )
    return out


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
            port_preferred=body.port_preferred,
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
