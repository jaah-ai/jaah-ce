"""The agent-facing trigger channel: a runner creates an event-driven trigger in its own project, and edits or
deletes only the triggers its own task created (`SHAPE_TRIGGER`).

The twin of `routes/agent_crons.py`, whose docstring argues the shared parts: any persona's runner, the loop guard on
create and enable (`routes/agent/auth.py::require_human_started`), the caller a live filer held by THIS node, and the write
runs as the chain's executor (`agent_crons._executor_authority`: an org admin of the trigger's org, or an
operator — the bar the console's `@org_admin` trigger routes ask). Every validation is the console's own writer
(`routes/triggers.py::write_trigger_*`), so nothing here re-decides a source, a persona or a template. Only the
caller's own project, and edit/delete reach only `TaskTrigger.created_by_task_id == caller.id` (else 404,
existence undisclosed).

The secret never reaches the agent: the create leaves the trigger's one reveal owed and answers the console URL
where a human spends it ("Reveal secret (one time)"). No response here carries the secret or the ingress URL.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import config
from app.automation_alerts import AUTOMATIONS_PATH
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db_async import on_session
from app.entity_shape import from_row
from app.models.automations import TaskTrigger
from app.models.tasks import Task

# The one door onto `/api/agent/*` (`test_authz_policy.py::test_every_agent_surface_route_depends_on_the_token_check`)
# and the loop guard on every door that makes work run unattended.
from app.routes.agent.auth import AgentDoor, require_human_started
from app.routes.agent_crons import (
    _cron_caller, _executor_authority, _own_or_404, _project_org, _refuse_a_repeat,
)
from app.routes.triggers import (
    _NULLABLE_FIELDS,
    TriggerCreate,
    TriggerCreateFields,
    TriggerUpdate,
    TriggerUpdateFields,
    write_trigger_create,
    write_trigger_delete,
    write_trigger_update,
)
from app.user_activity import record_write

log = logging.getLogger("agent-triggers")

# No `prefix=`: the public/private ALB split test reads each `@infra` path off the decorator
# (see `routes/agent_project_settings.py`).
router = APIRouter(tags=["agent-triggers"])

_NOUN = "trigger"
#: The sentence a model relays to the operator; the secret itself is the console's to show (PRD R19).
REVEAL_SENTENCE = "Reveal the signing secret once in the console: {url}"


def next_step(url: str, owed: bool) -> str:
    """The sentence a model relays about a saved row's console page: the reveal when one is still owed a person,
    else where to manage it. Shared with `routes/agent_automations.py`."""
    return REVEAL_SENTENCE.format(url=url) if owed else f"Manage it in the console: {url}"


# ── The wire shapes ───────────────────────────────────────────────────────────────────────────────────


class AgentTriggerCreateIn(TriggerCreateFields):
    """One trigger in the caller's own project. No project, org or preflight credential: the first two are
    decided server-side, and a credential is an operator's pick in the console. Limits (concurrency, rate,
    budget, auto-pause) take the console's defaults; an operator tunes them on the detail page."""


class AgentTriggerUpdateIn(TriggerUpdateFields):
    """A partial update of one trigger the caller's task created. `trigger_id` travels in the body because the
    daemon substitutes only `{task_id}` into a path. Project and source kind are not editable."""

    trigger_id: UUID


class AgentTriggerOut(BaseModel):
    """What a model reads back. Deliberately NOT `TriggerOut`: no secret, no ingress URL (it carries the ingress
    token), nothing a human must not first see in the console."""

    id: str
    name: str
    source_kind: str
    persona: str
    project_id: int
    console_url: str
    # The sentence to relay to the operator: where to reveal the secret (a signed kind), else where to manage it.
    next_step: str


class AgentTriggerDeleteOut(BaseModel):
    """A 200 with a body, not a 204: the caller is a model reading a tool result."""

    trigger_id: str
    deleted: bool


# ── Who acts, and on what ─────────────────────────────────────────────────────────────────────────────


def _own_trigger_or_404(session: Session, caller: Task, trigger_id: UUID) -> TaskTrigger:
    # A trigger a human moved to another project has left the runner's reach.
    return _own_or_404(
        session, caller, TaskTrigger, trigger_id, _NOUN, TaskTrigger.project_id == caller.project_id
    )


def _out(trigger: TaskTrigger) -> AgentTriggerOut:
    # The automation's page, or the list for a trigger older than automations (`automation_id` is nullable).
    tail = f"/{trigger.automation_id}" if trigger.automation_id else ""
    url = config.console_url(f"{AUTOMATIONS_PATH}{tail}")
    # Owed only on an agent-created signed trigger whose reveal no human has spent yet.
    owed = trigger.secret_sealed is not None and trigger.secret_revealed_at is None
    return from_row(
        AgentTriggerOut, trigger,
        console_url=url,
        next_step=next_step(url, owed),
    )


# ── The three routes ──────────────────────────────────────────────────────────────────────────────────


@router.post("/api/agent/tasks/triggers", response_model=AgentTriggerOut, status_code=201)
@infra(Tenancy.ORG_SCOPED)
@on_session
def create_agent_trigger(
    session: Session,
    body: AgentTriggerCreateIn,
    door: AgentDoor,
) -> AgentTriggerOut:
    """Create one trigger in the caller's own project, owned by the chain's executor. The secret stays sealed
    and unrevealed; the answer names where a human reveals it."""
    caller = _cron_caller(session, *door)
    require_human_started(session, caller, what="creating a trigger")
    org_id = _project_org(session, caller)
    executor = _executor_authority(session, caller, org_id, noun=_NOUN)
    _refuse_a_repeat(session, caller, body.name, model=TaskTrigger, noun=_NOUN, twice="creating it twice")
    # Pinned to the caller's own project: a runner files nowhere it was not dispatched.
    write = TriggerCreate(project_id=caller.project_id, **body.model_dump())
    trigger, _no_secret = write_trigger_create(session, write, executor, created_by_task_id=caller.id)
    # The capture middleware audits the console's create but skips `/api/agent/`, so the runner's is named here.
    record_write(
        session, executor, "trigger_create", f"Created trigger {trigger.name}",
        data={"trigger_id": str(trigger.id), "source_kind": trigger.source_kind, "via_task_id": caller.id},
    )
    session.commit()
    log.info("agent trigger: task=%s created trigger=%s (as executor=%s)", caller.id, trigger.id, executor.id)
    return _out(trigger)


@router.patch("/api/agent/tasks/triggers", response_model=AgentTriggerOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def update_agent_trigger(
    session: Session,
    body: AgentTriggerUpdateIn,
    door: AgentDoor,
) -> AgentTriggerOut:
    """Patch one trigger the caller's task created, as the executor."""
    caller = _cron_caller(session, *door)
    trigger = _own_trigger_or_404(session, caller, body.trigger_id)
    # A live trigger keeps filing unattended (enabled: `routes/triggers.py::TriggerOut`); this surface cannot
    # resume one, so only an edit of a paused or failed trigger is free.
    if trigger.paused_at is None and trigger.failed_at is None:
        require_human_started(session, caller, what="changing a live trigger")
    executor = _executor_authority(session, caller, trigger.org_id, noun=_NOUN)
    # A model's client often sends `null` for an untouched field; only a clearable field takes it as "clear".
    sent = {
        k: v for k, v in body.model_dump(exclude_unset=True, exclude={"trigger_id"}).items()
        if v is not None or k in _NULLABLE_FIELDS
    }
    trigger = write_trigger_update(session, trigger, TriggerUpdate(**sent), executor, via_task_id=caller.id)
    log.info(
        "agent trigger: task=%s updated trigger=%s fields=%s (as executor=%s)",
        caller.id, body.trigger_id, sorted(sent), executor.id,
    )
    return _out(trigger)


@router.delete("/api/agent/tasks/triggers", response_model=AgentTriggerDeleteOut)
@infra(Tenancy.ORG_SCOPED)
@on_session
def delete_agent_trigger(
    session: Session,
    door: AgentDoor,
    trigger_id: UUID = Query(..., description="the id trigger_create answered"),
) -> AgentTriggerDeleteOut:
    """Delete one trigger the caller's task created, as the executor. Tasks it already filed stay."""
    caller = _cron_caller(session, *door)
    trigger = _own_trigger_or_404(session, caller, trigger_id)
    executor = _executor_authority(session, caller, trigger.org_id, noun=_NOUN)
    write_trigger_delete(session, trigger, executor, via_task_id=caller.id)
    return AgentTriggerDeleteOut(trigger_id=str(trigger_id), deleted=True)
