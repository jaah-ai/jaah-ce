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

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import adopt_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.db_async import DbDep, on_session, run_deferring
from app.models.identity import AdminUser
from app.routes.tasks.create import TaskUpdate
from app.routes.tasks.policy import MAX_TASK_TITLE_LEN
from app.routes.tasks.task_wire import TaskOut, _task_out
from app.routes.tasks.update import update_task
from app.task_lookup import get_task_or_404
from app.task_title import RegeneratedTitle, regenerated_title, stamp_language

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
@on_session(deferring=True)
def rename_task(
    session: Session,
    task_id: int,
    payload: TaskTitleIn,
    caller: AsyncCaller = Depends(current_caller),
) -> TaskOut:
    """Rename a Task: the PUT's title edit, on the caller's plane (module docstring)."""
    _user = adopt_user(session, caller.actor)
    _saved_task_or_409(task_id, session, caller.principal)
    return update_task.session_body(session, task_id, TaskUpdate(title=payload.title), _user, caller.principal)


@router.post("/{task_id}/title/regenerate", response_model=TaskOut)
@feature(
    "tasks", "edit", tenancy=Tenancy.CALLER_PLANE, object_type="task", object_params=("task_id",),
    author_draft=True,
)
async def regenerate_task_title(
    db: DbDep,
    task_id: int,
    caller: AsyncCaller = Depends(current_caller),
) -> TaskOut:
    """Re-derive the title from the current body with the auto-title model (`app/task_title.py`), overwriting
    whatever the row carries, and write it as a rename. Synchronous, as Re-tag: one short vendor call, no
    connection held across it (`regenerated_title` owns its own session; this one is released first).

    On a top-level task the language is re-detected with it and written over the subtree, unless a person
    picked one (`app/task_language.py`); a reply naming none leaves it as it is. A child keeps its epic's.

    A model that gives nothing new (no key, a failure, the same title) leaves the row as it is and answers it."""
    await db.run_sync(_saved_task_or_409_rolled_back, task_id, caller.principal)
    # The vendor call (and its own session) off the loop, with no connection held across it.
    regenerated = await asyncio.to_thread(regenerated_title, task_id)
    return await run_deferring(db, _regenerate_task_title, task_id, regenerated, caller.actor, caller.principal)


def _saved_task_or_409_rolled_back(session: Session, task_id: int, principal: Principal | None) -> None:
    """`_saved_task_or_409`, then the read's connection handed back before the vendor call."""
    _saved_task_or_409(task_id, session, principal)
    session.rollback()


def _regenerate_task_title(
    session: Session,
    task_id: int,
    regenerated: RegeneratedTitle | None,
    _user: AdminUser,
    principal: Principal | None,
) -> TaskOut:
    """`regenerate_task_title`'s writes, over the route's async session (`run_sync`), once the model answered."""
    _user = adopt_user(session, _user)
    out = None
    if regenerated is not None and regenerated.title is not None:
        out = update_task.session_body(session, task_id, TaskUpdate(title=regenerated.title), _user, principal)
    # After the rename, so a refused rename writes nothing.
    if regenerated is not None and regenerated.language is not None:
        task = get_task_or_404(task_id, session)
        if task.parent_task_id is None and stamp_language(session, task, regenerated.language):
            session.commit()
            out = None
    return out or _task_out(get_task_or_404(task_id, session), session=session, principal=principal)
