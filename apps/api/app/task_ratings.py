"""Rating arithmetic shared by every surface that shows a Task's stars.

The single home of the average and its rounding, so the board and the rating endpoint cannot
disagree about the same task. It also breaks an import cycle: `routes/tasks/` needs the aggregate
while `routes/task_ratings.py` imports `PERSONA_EPIC` from `routes/tasks/policy.py`.

Storage: `app/models/tasks.py::TaskRating`; routes and gating: `app/routes/task_ratings.py`.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tasks import TaskRating

# One decimal is a contract, not a display choice: two surfaces rounding differently disagree about
# the same task.
_AVERAGE_PRECISION = 1


def average_of(stars: list[int]) -> float | None:
    """The rounded average of one task's votes, or NULL when there are none.

    NULL, never 0.0, at zero votes (absence is not a state).
    Takes the caller's rows rather than a second `avg()` query, which could disagree with the `votes`
    list beside it if a vote landed between the two statements.
    """
    if not stars:
        return None
    return round(sum(stars) / len(stars), _AVERAGE_PRECISION)


def rating_aggregates(session: Session, task_ids: list[int]) -> dict[int, tuple[float, int]]:
    """`{task_id: (average, count)}` for a whole page of tasks, in one grouped query.

    One query, not one per row, since the board renders stars on every visible task. A task with no
    votes is absent from the result; callers supply their own unrated default.
    """
    if not task_ids:
        return {}
    rows = session.execute(
        select(TaskRating.task_id, func.avg(TaskRating.stars), func.count())
        .where(TaskRating.task_id.in_(task_ids))
        .group_by(TaskRating.task_id)
    ).all()
    # `avg` over smallint returns `numeric` (a `Decimal`); `float()` first makes the rounding identical
    # to `average_of` (Decimal and float round ties differently).
    return {
        task_id: (round(float(average), _AVERAGE_PRECISION), count)
        for task_id, average, count in rows
    }
