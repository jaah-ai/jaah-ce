"""Task read: `GET /api/tasks/{task_id}`."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import editable_task_vm_clause, visible_task_vm_clause
from app.db import get_session
from app.models.agent_sessions import AgentSession
from app.models.identity import AdminUser
from app.models.nodes import MICROVM_ENDED, Microvm, Node
from app.models.tasks import Task
from app import edition_features, hipaa
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, READY
from app.task_lookup import get_task_or_404
from app.agent_sessions import STATUS_RUNNING as SESSION_RUNNING
from app.task_run_metrics import run_aggregates
from app.task_ratings import rating_aggregates
from app.task_tags import tag_refs_for
from app.routes.tasks.qa_render import _load_task_questions_out, _task_qa_entries
from app.routes.tasks.task_wire import (
    TaskLiveSessionOut,
    TaskMicrovmOut,
    TaskOut,
    _epic_children,
    _latest_settled_output_note,
    _ran_on,
    _task_out,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _running_microvm(session: Session, task_id: int, principal: Principal | None) -> Optional[TaskMicrovmOut]:
    """The task's RUNNING VM (`microvm_placement.RUNNING`), with this caller's console verdict; None otherwise.
    The caller already passed the task's view gate; the clause re-reads it beside the node's reach, as the
    console route does (`routes/console.py::_task_vm_console`). None without `worktrees:view`, the console
    route's own key: a button that only 403s is no terminal."""
    if principal is not None and not principal.has(permission_key("worktrees", "view")):
        return None
    if not edition_features.enabled("microvm"):
        return None  # The community edition has no MicroVMs.
    from app import microvm_placement

    row = session.execute(
        select(Microvm.id, Node.name, editable_task_vm_clause(principal).label("may_edit"))
        .select_from(Microvm)
        .join(Task, Task.id == Microvm.task_id)
        .join(Node, Node.id == Microvm.node_id)
        .where(Microvm.task_id == task_id, Microvm.state == microvm_placement.RUNNING)
        .where(visible_task_vm_clause(principal))
    ).first()
    return None if row is None else TaskMicrovmOut(vm_id=row.id, node=row.name, may_edit=bool(row.may_edit))


def _vm_hold_node(session: Session, task: Task) -> Optional[str]:
    """The name of the node holding the task's newest VM when "Start fresh elsewhere" would be accepted
    (`microvm_lifecycle.fresh_start_refusal`) and no runner is on the task; None otherwise or once the node is gone.
    A `ready` task whose VM already ended is placed afresh anyway (it was just started elsewhere, or its VM is
    gone), so it is not offered again."""
    if task.state in (ACTIVE, AWAITING_RESPONSE):
        return None
    if not edition_features.enabled("microvm"):
        return None  # The community edition has no MicroVMs.
    from app import microvm_lifecycle

    vm, refusal = microvm_lifecycle.fresh_start_refusal(session, task)
    if vm is None or refusal is not None:
        return None
    if task.state == READY and vm.state in MICROVM_ENDED:
        return None
    return session.scalar(select(Node.name).where(Node.id == vm.node_id))


def _live_session(session: Session, task: Task) -> Optional[TaskLiveSessionOut]:
    """The task's newest `running` `sessions` row (the row `GET /api/sessions?task=` lists), or None. The caller
    already passed the task's view gate, and a session row is evidence opened from its task. Pinned to the task's
    own org: `sessions.task_id` carries no composite org key, so a row naming this id from another org is not it."""
    row = session.execute(
        select(AgentSession.agent_session_id, AgentSession.runtime, AgentSession.started_at)
        .where(
            AgentSession.task_id == task.id,
            AgentSession.org_id == task.org_id,
            AgentSession.status == SESSION_RUNNING,
        )
        .order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
        .limit(1)
    ).first()
    return None if row is None else TaskLiveSessionOut(
        id=row.agent_session_id, runtime=row.runtime, started_at=row.started_at,
    )


@router.get("/{task_id}", response_model=TaskOut)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def get_task(
    task_id: int,
    request: Request,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Task detail; includes `children` (possibly an empty list) when the Task has child rows. Also carries
    the Task's Q&A resolutions and its own full stage-output artifact, read from the durable note store —
    detail-only, so the list route stays lightweight. A HIPAA org's read is recorded (`hipaa.record_phi_read`)."""
    task = get_task_or_404(task_id, session)
    children = _epic_children(task_id, session)
    # The root and its children in one call: expanding an epic draws its Cost column from these child rows
    # (`apps/web/src/pages/Tasks.tsx`), unlike tags and rating. Two queries for the whole set.
    costs, tokens = run_aggregates(session, [task_id, *(c.id for c in children or [])])
    live_session = _live_session(session, task)
    out = _task_out(
        task,
        children=list(children) if children else None,
        session=session,
        # Scopes the derived gate fields: `gated_child_id` is a navigation target. Without a principal
        # `_task_out` computes neither.
        principal=principal,
        qa_entries=_task_qa_entries(session, task),
        stage_output=_latest_settled_output_note(session, task),
        ran_on=_ran_on(session, task_id),
        microvm=_running_microvm(session, task_id, principal),
        vm_hold_node=_vm_hold_node(session, task),
        live_session=live_session,
        # The board card's run age, from the same row: only an `active` task is mid-run.
        run_started_at=live_session.started_at if live_session is not None and task.state == ACTIVE else None,
        # This row's tags only; nested children serialise `[]` (tags are a top-level concern, operator decision).
        tags=tag_refs_for(session, [task_id]).get(task_id),
        # This row's rating for the detail page's initial render (no unrated flicker), from the same batch
        # loader as the list; nested children get none, like tags.
        rating=rating_aggregates(session, [task_id]).get(task_id),
        # This row's rolled-up cost, the same number as the panel's tile (`app/task_run_metrics.py`).
        cost=costs.get(task_id),
        child_costs=costs,
        tokens=tokens.get(task_id),
        child_tokens=tokens,
        # Multi-round Q&A history — the rounds `GateForm` renders and the status Revise reads. Unconditional:
        # Revise can reopen a round in any state, and the console's `gated` is `awaiting_response OR some round
        # is open`, read off this field; a runner can also post a round while `active`. Costs one indexed
        # `task_id` SELECT, usually empty.
        questions=_load_task_questions_out(session, task_id),
    )
    hipaa.record_phi_read(
        session, request, record_type="task", record_id=task_id, task_id=task_id, org_id=task.org_id, user=_user
    )
    return out
