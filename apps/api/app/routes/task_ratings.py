"""Five-star rating on a console Task — the read and the one replaceable write.

`GET`/`PUT /api/tasks/{task_id}/rating`. The `(task_id, user_id)` primary key of
`app/models/tasks.py::TaskRating` is "one vote per user" (migration 0154); aggregates are single-homed in
`app/task_ratings.py`.

The write is `tasks:view`, not `tasks:edit` (operator decision): whoever may see the task may judge
it. Object denial stays 404 (`app/authz/`). The write has two distinct 409s (`_is_ratable`, `done`);
reads have neither, so votes survive a task leaving `done`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import actors
from app.auth import require_user
from app.authz.declarations import feature
from app.db import get_session
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.tasks import Task, TaskRating
# `app/routes/tasks/` is the persona vocabulary's home. No cycle because the shared batched
# aggregate lives in `app/task_ratings.py`; do not move the loader back into this file.
from app.routes.tasks.policy import PERSONA_EPIC
from app.task_lifecycle import DONE
from app.task_lookup import get_task_or_404
from app.task_ratings import average_of

router = APIRouter(prefix="/api/tasks", tags=["ratings"])


class TaskRatingVoteOut(BaseModel):
    """One voter's stars, the voter as a resolved display name (`actors.actor_names`), never a uuid,
    so names match every other provenance cell and a rename reaches past votes."""

    user: str
    stars: int


class TaskRatingOut(BaseModel):
    """The rating of one Task as the console reads it: the aggregate, the caller's own vote, the roster.

    `average` is null when `count == 0`, never 0.0, and `my_stars` is null when the caller has not
    voted (a 0 would show a phantom vote): absence is not a state. Rounded to one decimal here, not in
    clients, so surfaces cannot disagree."""

    task_id: int
    average: float | None
    count: int
    my_stars: int | None
    votes: list[TaskRatingVoteOut]


class TaskRatingIn(BaseModel):
    """The body of a vote. `ge=1, le=5` rejects as a 422 before the handler runs; the DB's own CHECK
    (migration 0154) is the floor for raw-SQL paths, not a duplicate."""

    stars: int = Field(ge=1, le=5)


def _rating_out(session: Session, task_id: int, user_id) -> TaskRatingOut:
    """Project one Task's whole rating for `user_id`: one SELECT of the votes, one batched name
    resolution. The aggregate is computed from the same rows so it cannot disagree with `votes` under
    a concurrent vote."""
    rows = session.execute(
        select(TaskRating.user_id, TaskRating.stars).where(TaskRating.task_id == task_id)
    ).all()
    names = actors.actor_names(session, [row_user_id for row_user_id, _ in rows])
    count = len(rows)
    # Rounding and "null at zero votes" are single-homed in `average_of`, shared with the list route.
    average = average_of([stars for _, stars in rows])
    my_stars = next((stars for row_user_id, stars in rows if row_user_id == user_id), None)
    # Deterministic order (stars desc, then name) so the hover list does not reshuffle on refetch. A
    # voter missing from the name map sorts as "" rather than 500ing.
    votes = sorted(
        (
            TaskRatingVoteOut(user=names.get(row_user_id, ""), stars=stars)
            for row_user_id, stars in rows
        ),
        key=lambda vote: (-vote.stars, vote.user),
    )
    return TaskRatingOut(
        task_id=task_id, average=average, count=count, my_stars=my_stars, votes=votes
    )


def _is_ratable(task: Task) -> bool:
    """Is this Task one of the rows the rating feature applies to at all?

    Epics and top-level tasks only (operator decision): a rating judges a deliverable, not internal
    pipeline stages, which would otherwise outvote their epic. All three disjuncts are kept since a
    nested epic is still an epic; the epic predicate mirrors `apps/web/src/components/tasks/shared.tsx`
    so the server never refuses a row the UI offers stars on. State is checked separately in
    `rate_task`."""
    return (
        task.parent_task_id is None
        or task.persona == PERSONA_EPIC
        or task.is_plannable
    )


@router.get("/{task_id}/rating", response_model=TaskRatingOut)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def get_task_rating(
    task_id: int,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TaskRatingOut:
    """This Task's rating: the aggregate, the calling user's own vote, and who voted what.

    No state guard (see the module docstring)."""
    get_task_or_404(task_id, session)
    return _rating_out(session, task_id, user.id)


@router.put("/{task_id}/rating", response_model=TaskRatingOut)
# `tasks:view`, not `tasks:edit`, deliberately (see the module docstring).
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def rate_task(
    task_id: int,
    payload: TaskRatingIn,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TaskRatingOut:
    """Record the calling user's vote on this Task, replacing their previous one if they had one.

    A Postgres upsert, not read-then-write, so concurrent PUTs (a double click) cannot both INSERT
    and 500; the conflict target is the composite primary key. `updated_at` is set explicitly because
    the ORM `onupdate` hook does not reach a Core statement.

    Two 409s with different details: not in the rateable set (`_is_ratable`), or not `done` yet. Both
    come after the 404 and FastAPI's 422.

    `record_transition=False` is mandatory: True would persist a same-state `task:rated`
    `ActivityEvent` that the activity view shows as a lifecycle transition. False still reaches live
    SSE subscribers."""
    task = get_task_or_404(task_id, session)
    # The two 409s are reported apart: "not finished" is a wait, "not rateable" is permanent.
    if not _is_ratable(task):
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} is not a rateable task (only epics and top-level tasks are rated); "
                f"it is a {task.persona!r} child of task {task.parent_task_id}"
            ),
        )
    # Second, so the more informative refusal wins for a row failing both.
    if task.state != DONE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} is {task.state!r}, not {DONE!r} — only finished work can be rated"
            ),
        )
    # `org_id` must be named: the write seam stamps only ORM instances in `before_flush`, and this
    # Core insert flushes nothing, so the row would be unreadable under the enforcing read seam. It
    # inherits the task's org; the task was already read under the caller's scope.
    stmt = pg_insert(TaskRating).values(
        task_id=task_id, user_id=user.id, stars=payload.stars, org_id=task.org_id
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TaskRating.task_id, TaskRating.user_id],
            # `org_id` is in the update set too, healing a legacy NULL-org row on re-vote. The
            # excluded value derives from the task, never from the caller.
            set_={
                "stars": stmt.excluded.stars,
                "updated_at": func.now(),
                "org_id": stmt.excluded.org_id,
            },
        )
    )
    emit_console_event(
        session, entity="task", id=task_id, action="rated", record_transition=False
    )
    # One transaction for guard, upsert and notify: a refused vote writes nothing and `pg_notify`
    # never announces a rolled-back vote. The projection is read after commit.
    session.commit()
    return _rating_out(session, task_id, user.id)
