"""Task title: `PUT /api/tasks/{task_id}/title` (rename) and `POST /api/tasks/{task_id}/title/regenerate`.

Two routes apart from `PUT /api/tasks/{task_id}` because their plane differs: an operator may
rename ANOTHER org's task, and nothing else on it. They are `Tenancy.CALLER_PLANE`, so an operator binds the
fleet and `app/authz/enforce.py::_refuse_other_org_mutation` (ORG_SCOPED only) lets them through, while every
other caller is bound to their own org exactly as on the PUT. The guard cannot read a request body, so the
carve-out is these two routes by declaration, never "a PUT whose payload is title-only".

Both write through `update_task` itself with a title-only `TaskUpdate`: the lane-run freeze, the chat gate, the
epic children's re-stamp, the slot relabel, the actor stamp and the `task:updated` row (filed in the TASK's org,
naming the operator) are the PUT's own. An unsaved draft is refused: a title-only write would finalize it.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.models.identity import AdminUser
from app.routes.tasks.create import TaskUpdate
from app.routes.tasks.policy import MAX_TASK_TITLE_LEN
from app.routes.tasks.task_wire import TaskOut, _task_out
from app.routes.tasks.update import update_task
from app.task_lookup import get_task_or_404
from app.task_title import regenerated_title

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
log = logging.getLogger(__name__)

_UNSAVED_DETAIL = "Save the task first."


class TaskTitleIn(BaseModel):
    """The title and nothing else: any other field is a 422, never silently dropped — this route is the
    operator's other-org carve-out, so it must not look as if it took an edit it refused."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=MAX_TASK_TITLE_LEN)


def _saved_task_or_409(task_id: int, session: Session, principal: Principal | None) -> None:
    task = get_task_or_404(task_id, session)
    if task.unsaved:
        raise HTTPException(status_code=409, detail=_UNSAVED_DETAIL)
    # The carve-out's one server-side trace, beside `enforce.py`'s line for a refused other-org write.
    if principal is not None and principal.is_operator and task.org_id != principal.org_id:
        log.info("authz: operator %s retitles task %s in org %s", principal.user_id, task_id, task.org_id)


@router.put("/{task_id}/title", response_model=TaskOut)
@feature(
    "tasks", "edit", tenancy=Tenancy.CALLER_PLANE, object_type="task", object_params=("task_id",),
    author_draft=True,
)
def rename_task(
    task_id: int,
    payload: TaskTitleIn,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Rename a Task: the PUT's title edit, on the caller's plane (module docstring)."""
    _saved_task_or_409(task_id, session, principal)
    return update_task(task_id, TaskUpdate(title=payload.title), _user, principal, session)


@router.post("/{task_id}/title/regenerate", response_model=TaskOut)
@feature(
    "tasks", "edit", tenancy=Tenancy.CALLER_PLANE, object_type="task", object_params=("task_id",),
    author_draft=True,
)
def regenerate_task_title(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> TaskOut:
    """Re-derive the title from the current body with the auto-title model (`app/task_title.py`), overwriting
    whatever the row carries, and write it as a rename. Synchronous, as Re-tag: one short vendor call, no
    connection held across it (`regenerated_title` owns its own session; this one is released first).

    A model that gives nothing new (no key, a failure, the same title) leaves the row as it is and answers it."""
    _saved_task_or_409(task_id, session, principal)
    session.rollback()
    title = regenerated_title(task_id)
    if title is None:
        return _task_out(get_task_or_404(task_id, session), session=session, principal=principal)
    return update_task(task_id, TaskUpdate(title=title), _user, principal, session)
