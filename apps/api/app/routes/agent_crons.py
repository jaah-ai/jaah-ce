"""The agent-facing cron channel: a runner schedules a Runner cron in its own project, and edits or
deletes only the crons its own task created (`SHAPE_CRON`).

The licence is the filed-draft doors' (`routes/agent/agent_filing.py::_require_filing_caller`). The authority is the
chain's executor (`app/task_ancestry.py::executor_user_id`): the write runs as that human, refused unless
they could create this cron in the console (an operator, or an org admin of the cron's org), and
every validation is the console's own writer (`routes/crons.py::write_cron_*`), so nothing here
re-decides a schedule, a persona or a project. Only `activity_type = "runner"`, only the caller's own
project, and edit/delete reach only `Cron.created_by_task_id == caller.id` (else 404, existence
undisclosed). Auth is `routes/agent/auth.py::_check_token`, the caller bound to its node and still live (`_cron_caller`).
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import node_credentials
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.authz.principal import principal_for_user
from app.cron_activity_runner import ACTIVITY_TYPE as RUNNER_ACTIVITY_TYPE
from app.crons import org_for
from app.db import get_session
from app.models.automations import Cron
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.tasks import Task
from app.persona_registry import SHAPE_CRON
from app.task_ancestry import executor_user_id
from app.tenancy.create_surface import named_org

# The one door onto `/api/agent/*` (`test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`)
# and the filing licence the filed-draft doors share.
from app.routes.agent.auth import _check_token, _task_or_404_for_node
from app.routes.agent.agent_filing import _require_filing_caller
from app.routes.crons import (
    CronCreate,
    CronDetail,
    CronUpdate,
    _creatable_descriptor,
    write_cron_create,
    write_cron_delete,
    write_cron_update,
)
from app.routes.tasks.policy import MAX_TASK_TITLE_LEN, PERSONA_CODER
from app.routes.tasks.agent_filing import _require_live_filer
from app.entity_shape import get_or_404, StrictModel

log = logging.getLogger("agent-crons")

# No `prefix=`: the public/private ALB split test reads each `@infra` path off the decorator
# (see `routes/agent_project_settings.py`).
router = APIRouter(tags=["agent-crons"])

#: The runner-params keys this surface writes; `project_ids` is pinned, never sent.
_PARAM_FIELDS: tuple[str, ...] = ("instructions", "persona", "model", "hidden", "autonomous")


# ── The wire shapes ───────────────────────────────────────────────────────────────────────────────────


class AgentCronCreateIn(StrictModel):
    """One Runner cron in the caller's own project. No `org_id`, `activity_type` or project: each is
    decided server-side. `persona` is checked by the activity's own params schema (`SCHEDULABLE_PERSONAS`)."""

    # The filing door's own bounds (`AgentTopLevelTaskIn`): both reach logs, the audit trail and the console.
    name: str = Field(min_length=1, max_length=MAX_TASK_TITLE_LEN)
    description: str = Field(default="", max_length=MAX_TASK_TITLE_LEN)
    schedule_kind: str
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    instructions: str
    persona: str = PERSONA_CODER
    model: Optional[str] = None
    hidden: bool = False
    autonomous: bool = True
    enabled: bool = True


class AgentCronUpdateIn(StrictModel):
    """A partial update of one cron the caller's task created. `cron_id` travels in the body because the
    daemon substitutes only `{task_id}` into a path. Project and activity are not editable."""

    cron_id: UUID
    name: Optional[str] = Field(default=None, min_length=1, max_length=MAX_TASK_TITLE_LEN)
    description: Optional[str] = Field(default=None, max_length=MAX_TASK_TITLE_LEN)
    schedule_kind: Optional[str] = None
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    instructions: Optional[str] = None
    persona: Optional[str] = None
    model: Optional[str] = None
    hidden: Optional[bool] = None
    autonomous: Optional[bool] = None
    enabled: Optional[bool] = None


class AgentCronDeleteOut(BaseModel):
    """A 200 with a body, not a 204: the caller is a model reading a tool result."""

    cron_id: str
    deleted: bool


# ── Who acts, and on what ─────────────────────────────────────────────────────────────────────────────


def _cron_caller(
    session: Session, agent: node_credentials.AgentCaller, x_agent_task_id: str, *, shape: str = SHAPE_CRON
) -> Task:
    """The calling task: licensed for `shape`, still a live filer, and held by THIS node.

    Liveness as the filed-draft doors (`_require_live_filer`): a settled or archived chat must not keep
    scheduling. Node binding as the project-settings doors (`_task_or_404_for_node`): the header is
    self-declared, and a cron outlives the call, so another node in the org must not name this task.
    Shared with `routes/agent_triggers.py` (`shape=SHAPE_TRIGGER`)."""
    caller = _require_filing_caller(x_agent_task_id, session, shape=shape, agent=agent)
    _require_live_filer(caller)
    return _task_or_404_for_node(session, agent, caller.id)


def _executor_authority(session: Session, caller: Task, org_id: UUID, *, noun: str = "cron") -> AdminUser:
    """The chain's executor, or a 403 when there is none or they could not create a `noun` by hand.

    `principal.is_org_admin` is the bar the console's `@org_admin` route asks (an Org Owner/Admin in a
    tenant org, an operator on the operator plane), so a runner never out-reaches its human. Shared with
    `routes/agent_triggers.py` (`noun="trigger"`): the trigger routes ask the same bar."""
    executor_id = executor_user_id(session, caller.id)
    user = session.get(AdminUser, executor_id) if executor_id is not None else None
    if user is None:
        log.warning("agent %s: refused task=%s (no executor resolves: id=%s)", noun, caller.id, executor_id)
        raise HTTPException(
            status_code=403,
            detail=(
                f"task {caller.id} resolves no human who started or created it, so nobody would own this "
                f"{noun}. Name it in your report and let an operator create it."
            ),
        )
    principal = principal_for_user(session, user)
    # An org admin of THIS cron's org, or an operator: `is_org_admin` alone holds for an admin of any org.
    if not (principal.is_operator or (principal.is_org_admin and user.org_id == org_id)):
        log.warning("agent %s: refused task=%s (executor=%s is not an org admin)", noun, caller.id, user.id)
        raise HTTPException(
            status_code=403,
            detail=(
                "this task runs with the authority of the user who started it (else created it), and they "
                f"may not create {noun}s — the same refusal they would get in the console. Name it in your "
                "report and let an org admin create it."
            ),
        )
    return user


def _own_or_404(session: Session, caller: Task, model: Any, row_id: UUID, noun: str, *also: Any) -> Any:
    """The `model` row, locked, only when the caller's own task created it (and `also` holds); anything
    else is one 404. Shared with `routes/agent_triggers.py`."""
    return get_or_404(
        session, model, row_id, detail=f"no {noun} {row_id} created by task {caller.id}",
        where=(model.created_by_task_id == caller.id, *also), lock=True,
    )


def _refuse_a_repeat(
    session: Session, caller: Task, name: str, *,
    model: Any = Cron, noun: str = "cron", twice: str = "scheduling the same work twice",
) -> None:
    """409 when this task already created a `noun` of this name: a retried call (a lost reply) must not
    create it twice. Names the existing id so the caller edits it instead."""
    existing = session.scalar(
        select(model.id).where(model.created_by_task_id == caller.id, model.name == name.strip())
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {caller.id} already created {noun} {existing} named {name.strip()!r}; edit that one "
                f"(task_{noun}_update) rather than {twice}"
            ),
        )


def _project_org(session: Session, caller: Task) -> UUID:
    """The org of the caller's own project — where its cron lands."""
    project = session.get(Project, caller.project_id) if caller.project_id is not None else None
    if project is None:
        raise HTTPException(
            status_code=409, detail=f"task {caller.id} names no project, so it has nowhere to schedule"
        )
    return project.org_id


# ── The three routes ──────────────────────────────────────────────────────────────────────────────────


@router.post("/api/agent/tasks/crons", response_model=CronDetail, status_code=201)
@infra(Tenancy.ORG_SCOPED)
def create_agent_cron(
    body: AgentCronCreateIn,
    background_tasks: BackgroundTasks,
    x_agent_task_id: str = Header(default=""),
    agent: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> CronDetail:
    """Create one Runner cron in the caller's own project, owned by the chain's executor."""
    caller = _cron_caller(session, agent, x_agent_task_id)
    org_id = _project_org(session, caller)
    executor = _executor_authority(session, caller, org_id)
    _refuse_a_repeat(session, caller, body.name)
    params: dict[str, Any] = {
        "instructions": body.instructions,
        # Pinned to the caller's own project: a runner schedules nowhere it was not dispatched.
        "project_ids": [caller.project_id],
        "persona": body.persona,
        "hidden": body.hidden,
        "autonomous": body.autonomous,
    }
    if body.model is not None:
        params["model"] = body.model
    write = CronCreate(
        org_id=org_id,
        name=body.name,
        description=body.description,
        schedule_kind=body.schedule_kind,
        cron_expression=body.cron_expression,
        interval_seconds=body.interval_seconds,
        activity_type=RUNNER_ACTIVITY_TYPE,
        activity_params=params,
        enabled=body.enabled,
    )
    # The console handler's own decisions, in its order: a creatable activity, then a real, enabled org
    # matching this request's bind (`named_org`), placed by `org_for`.
    descriptor = _creatable_descriptor(write.activity_type)
    org_id = org_for(is_system=descriptor.is_system, author_org=named_org(session, write.org_id))
    # The executor owns it (`stamp_actor` inside the writer); the caller's task is what may edit it.
    out = write_cron_create(
        session, write, executor, background_tasks,
        descriptor=descriptor, org_id=org_id, created_by_task_id=caller.id,
    )
    log.info("agent cron: task=%s created cron=%s (as executor=%s)", caller.id, out.id, executor.id)
    return out


@router.patch("/api/agent/tasks/crons", response_model=CronDetail)
@infra(Tenancy.ORG_SCOPED)
def update_agent_cron(
    body: AgentCronUpdateIn,
    background_tasks: BackgroundTasks,
    x_agent_task_id: str = Header(default=""),
    agent: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> CronDetail:
    """Patch one cron the caller's task created, as the executor. Runner params merge onto the stored
    bag, so `project_ids` never moves."""
    caller = _cron_caller(session, agent, x_agent_task_id)
    cron = _own_or_404(session, caller, Cron, body.cron_id, "cron")
    executor = _executor_authority(session, caller, cron.org_id)
    sent = body.model_dump(exclude_unset=True, exclude={"cron_id"})
    patch = {k: v for k, v in sent.items() if k not in _PARAM_FIELDS}
    param_changes = {k: v for k, v in sent.items() if k in _PARAM_FIELDS}
    if param_changes:
        params = dict(cron.activity_params or {})
        params.update(param_changes)
        # `null` clears a key back to the schema's default (or is refused for a required one).
        patch["activity_params"] = {k: v for k, v in params.items() if v is not None}
    out = write_cron_update(
        session, cron, CronUpdate(**patch), executor, background_tasks, via_task_id=caller.id
    )
    log.info(
        "agent cron: task=%s updated cron=%s fields=%s (as executor=%s)",
        caller.id, body.cron_id, sorted(sent), executor.id,
    )
    return out


@router.delete("/api/agent/tasks/crons", response_model=AgentCronDeleteOut)
@infra(Tenancy.ORG_SCOPED)
def delete_agent_cron(
    background_tasks: BackgroundTasks,
    cron_id: UUID = Query(..., description="the id schedule_create answered"),
    x_agent_task_id: str = Header(default=""),
    agent: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> AgentCronDeleteOut:
    """Delete one cron the caller's task created, as the executor. Tasks it already fired stay."""
    caller = _cron_caller(session, agent, x_agent_task_id)
    cron = _own_or_404(session, caller, Cron, cron_id, "cron")
    executor = _executor_authority(session, caller, cron.org_id)
    write_cron_delete(session, cron, executor, background_tasks, via_task_id=caller.id)
    return AgentCronDeleteOut(cron_id=str(cron_id), deleted=True)
