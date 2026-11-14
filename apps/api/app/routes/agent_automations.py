"""The agent-facing automation channel: a runner manages its org's automations (When -> Target -> Action) as the
human its task acts for (operator decision 3: full manage of any automation that human may see).

The authority is the launcher's (`routes/agent/auth.py::launcher_principal`) held to the console's own bar, which is
`@org_admin` on every `/api/automations` route: an operator, or an Org Owner/Admin of the automation's org
(`routes/agent_crons.py::_executor_bar`, reused). Reads go through the console's own visibility
(`automation_service.visible_clause` / `require`) and writes through its own service, so the agent never
out-reaches its human and nothing here re-decides a When, a Target or an Action. The bodies the console answers
(`automation_service.out`) are the bodies answered here, less the ingress URL (`_agent_out`).

The loop guard (`routes/agent/auth.py::require_human_started`) holds every door that changes what later work does —
create, any edit, resume, Run now and delete — for a task a person started. A machine-started task may read (list,
catalogue, one) and reduce (pause), nothing else. No answer carries an event automation's signing secret: its one
reveal stays owed to a person in the console, as on the trigger door (`routes/agent_triggers.py::REVEAL_SENTENCE`).
Auth is `_check_token`, the caller
declared in `X-Agent-Task-Id`, held by this node and live (`live_agent_caller`). Ids travel in the body or a query
parameter: the daemon substitutes only `{task_id}` into a path.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import automation_service, config, node_credentials
from app.automation_alerts import AUTOMATIONS_PATH
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.authz.principal import Principal
from app.clock import utcnow
from app.db_async import DbDep, on_session, on_thread
from app.models.automations import Automation
from app.models.identity import AdminUser
from app.models.tasks import Task

# The one door onto `/api/agent/*` (`test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`).
from app.routes.agent.auth import AgentDoor, caller_and_launcher, require_human_started
from app.routes.agent_crons import _executor_bar, _project_org
from app.routes.agent_triggers import next_step
from app.routes.automations import AutomationPatch, PauseIn, RunIn, _org_or_reject, action_types, run_now

log = logging.getLogger("agent-automations")

# No `prefix=`: the public/private ALB split test reads each `@infra` path off the decorator (`routes/agent_crons.py`).
router = APIRouter(tags=["agent-automations"])

LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200


# ── The wire shapes ───────────────────────────────────────────────────────────────────────────────────


class AgentAutomationCreateIn(AutomationPatch):
    """The console's create body (`routes/automations.py::AutomationCreate`) without `org_id`: the automation lands in
    the caller's own project's org. `name` and `when` are required, as there."""

    name: str = Field(min_length=1, max_length=200)
    when: dict[str, Any]


class AgentAutomationPatchIn(AutomationPatch):
    automation_id: UUID


class AgentAutomationPauseIn(PauseIn):
    automation_id: UUID


class AgentAutomationIdIn(BaseModel):
    automation_id: UUID


class AgentAutomationRunIn(RunIn):
    automation_id: UUID


class AgentAutomationDeleteOut(BaseModel):
    """A 200 with a body, not a 204: the caller is a model reading a tool result."""

    automation_id: str
    deleted: bool


# ── Who acts, and on what ─────────────────────────────────────────────────────────────────────────────


def _acting(
    session: Session, agent: node_credentials.AgentCaller, x_agent_task_id: str, *, guard: Optional[str] = None,
) -> tuple[Task, AdminUser, Principal, UUID]:
    """The live caller, its launcher and the org the bar was asked over (the caller's own project's org), plus the
    loop guard when `guard` names the act. Asked before any automation is read, so a refused caller learns nothing
    about which automations exist."""
    caller, user, principal = caller_and_launcher(session, agent, x_agent_task_id)
    org_id = _project_org(session, caller)
    # `launcher_principal` resolved the executor already (and refused more: an `is_system` or other-org launcher).
    _executor_bar(caller, user, principal, org_id, noun="automation")
    if guard is not None:
        require_human_started(session, caller, what=guard)
    return caller, user, principal, org_id


def _automation(
    session: Session, caller: Task, user: AdminUser, principal: Principal, checked_org: UUID, automation_id: UUID,
    *, for_update: bool = False,
) -> Automation:
    """One automation the launcher may see (the console's `require`), and the bar again only when it sits in another
    org than the one `_acting` already asked it over (an operator's reach)."""
    automation = automation_service.require(session, principal, automation_id, for_update=for_update)
    if automation.org_id != checked_org:
        _executor_bar(caller, user, principal, automation.org_id, noun="automation")
    return automation


def _agent_out(session: Session, automation: Automation, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """The console's body (`automation_service.out`) less `when.ingress_url`: it carries the ingress token, which
    the agent trigger door leaves out too (`routes/agent_triggers.py::AgentTriggerOut`)."""
    payload = automation_service.out(session, automation, now=now)
    payload["when"].pop("ingress_url", None)
    return payload


def _saved_out(session: Session, saved: automation_service.Saved) -> dict[str, Any]:
    """The console's `{automation, secret}` body with `secret` always None, plus the sentence a model relays. Every
    write here passes the caller's task as `created_by_task_id`, so the writer leaves an event secret's one reveal
    owed to a person (`routes/triggers.py::write_trigger_create`)."""
    automation = saved.automation
    payload = _agent_out(session, automation)
    url = config.console_url(f"{AUTOMATIONS_PATH}/{automation.id}")
    owed = payload["when"].get("secret_state") == "owed"
    return {
        "automation": payload,
        "secret": None,
        "next_step": next_step(url, owed),
    }


# ── Reads ─────────────────────────────────────────────────────────────────────────────────────────────


@router.get("/api/agent/tasks/automations")
@infra(Tenancy.ORG_SCOPED)
@on_session
def list_agent_automations(
    session: Session,
    door: AgentDoor,
    q: Optional[str] = Query(default=None, max_length=200, description="name contains, case-insensitively"),
    paused: Optional[bool] = Query(default=None),
    limit: int = Query(default=LIST_DEFAULT_LIMIT, ge=1, le=LIST_MAX_LIMIT),
) -> dict[str, Any]:
    """Every automation the launcher may see, by name; `total` counts the filtered set before `limit`."""
    _caller, _user, principal, _org = _acting(session, *door)
    statement = select(Automation).where(automation_service.visible_clause(principal))
    if q and q.strip():
        statement = statement.where(Automation.name.icontains(q.strip(), autoescape=True))
    now = utcnow()
    rows = session.scalars(statement.order_by(Automation.name, Automation.id)).all()
    if paused is not None:
        rows = [a for a in rows if automation_service.is_paused(automation_service.child_of(session, a)) == paused]
    return {"items": [_agent_out(session, a, now=now) for a in rows[:limit]], "total": len(rows)}


@router.get("/api/agent/tasks/automations/catalogue")
@infra(Tenancy.ORG_SCOPED)
@on_session
def agent_automation_catalogue(
    session: Session,
    door: AgentDoor,
) -> dict[str, Any]:
    """The action types the launcher may create, each with its When kinds, Target kinds and params schema
    (`routes/automations.py::action_types`)."""
    _caller, _user, principal, _org = _acting(session, *door)
    return action_types(principal)


@router.get("/api/agent/tasks/automations/one")
@infra(Tenancy.ORG_SCOPED)
@on_session
def get_agent_automation(
    session: Session,
    door: AgentDoor,
    automation_id: UUID = Query(...),
) -> dict[str, Any]:
    caller, user, principal, org_id = _acting(session, *door)
    return _agent_out(session, _automation(session, caller, user, principal, org_id, automation_id))


# ── Writes ────────────────────────────────────────────────────────────────────────────────────────────


@router.post("/api/agent/tasks/automations", status_code=201)
@infra(Tenancy.ORG_SCOPED)
@on_thread
def create_agent_automation(
    session: Session,
    body: AgentAutomationCreateIn,
    background: BackgroundTasks,
    door: AgentDoor,
) -> dict[str, Any]:
    """Create one automation in the caller's project's org, owned by the launcher, its provenance the caller's task.
    On a worker thread for the console route's reason (`routes/automations.py::create_automation`). An event
    automation's signed-source secret is not answered to an agent (`_saved_out`)."""
    caller, user, principal, org_id = _acting(session, *door, guard="creating an automation")
    org_id = _org_or_reject(session, org_id)
    saved = automation_service.create(
        session, org_id=org_id, body=body.model_dump(exclude_none=True), actor=user, principal=principal,
        background=background, created_by_task_id=caller.id,
    )
    log.info("agent automation: task=%s created automation=%s (as %s)", caller.id, saved.automation.id, user.id)
    return _saved_out(session, saved)


@router.patch("/api/agent/tasks/automations")
@infra(Tenancy.ORG_SCOPED)
@on_thread
def update_agent_automation(
    session: Session,
    body: AgentAutomationPatchIn,
    background: BackgroundTasks,
    door: AgentDoor,
) -> dict[str, Any]:
    """Patch one automation, sent keys only, through the console's service. Guarded whatever the field: any edit
    changes what later work does (a paused one's edit runs on its resume)."""
    caller, user, principal, org_id = _acting(session, *door, guard="editing an automation")
    automation = _automation(session, caller, user, principal, org_id, body.automation_id)
    sent = body.model_dump(exclude_unset=True, exclude={"automation_id"})
    saved = automation_service.update(
        session, automation, body=sent, actor=user, principal=principal, background=background,
        created_by_task_id=caller.id,
    )
    log.info("agent automation: task=%s updated automation=%s fields=%s", caller.id, automation.id, sorted(sent))
    return _saved_out(session, saved)


@router.post("/api/agent/tasks/automations/pause")
@infra(Tenancy.ORG_SCOPED)
@on_session
def pause_agent_automation(
    session: Session,
    body: AgentAutomationPauseIn,
    background: BackgroundTasks,
    door: AgentDoor,
) -> dict[str, Any]:
    """Pause one automation. Open to a machine-started task: stopping work never feeds a loop."""
    caller, user, principal, org_id = _acting(session, *door)
    automation = _automation(session, caller, user, principal, org_id, body.automation_id)
    automation_service.set_paused(session, automation, True, actor=user, background=background, reason=body.reason)
    return _agent_out(session, automation)


@router.post("/api/agent/tasks/automations/resume")
@infra(Tenancy.ORG_SCOPED)
@on_session
def resume_agent_automation(
    session: Session,
    body: AgentAutomationIdIn,
    background: BackgroundTasks,
    door: AgentDoor,
) -> dict[str, Any]:
    caller, user, principal, org_id = _acting(session, *door, guard="resuming an automation")
    automation = _automation(session, caller, user, principal, org_id, body.automation_id)
    automation_service.set_paused(session, automation, False, actor=user, background=background)
    return _agent_out(session, automation)


@router.post("/api/agent/tasks/automations/run")
@infra(Tenancy.ORG_SCOPED)
async def run_agent_automation(
    body: AgentAutomationRunIn,
    db: DbDep,
    door: AgentDoor,
) -> dict[str, Any]:
    """Run once now, off the request, through the console's own Run now (`routes/automations.py::run_now`). `async`
    because that path awaits the engine and the background firing; the refusals run first on the route session."""
    user, principal = await db.run_sync(_run_caller, *door, body.automation_id)
    return await run_now(db, body.automation_id, body, principal=principal, actor=user)


def _run_caller(
    session: Session, agent: node_credentials.AgentCaller, x_agent_task_id: str, automation_id: UUID,
) -> tuple[AdminUser, Principal]:
    caller, user, principal, org_id = _acting(session, agent, x_agent_task_id, guard="running an automation")
    _automation(session, caller, user, principal, org_id, automation_id)
    return user, principal


@router.delete("/api/agent/tasks/automations", response_model=AgentAutomationDeleteOut)
@infra(Tenancy.ORG_SCOPED)
@on_thread
def delete_agent_automation(
    session: Session,
    background: BackgroundTasks,
    door: AgentDoor,
    automation_id: UUID = Query(...),
) -> AgentAutomationDeleteOut:
    """Delete one automation with its When child; tasks it filed stay. On a worker thread for the console route's
    reason (`routes/automations.py::delete_automation`). Guarded: a delete stops scheduled work, but it is not the
    pause a machine-started task may make, so it stays a person's to decide."""
    caller, user, principal, org_id = _acting(session, *door, guard="deleting an automation")
    automation = _automation(session, caller, user, principal, org_id, automation_id)
    automation_service.delete(session, automation, actor=user, background=background)
    log.info("agent automation: task=%s deleted automation=%s", caller.id, automation_id)
    return AgentAutomationDeleteOut(automation_id=str(automation_id), deleted=True)
