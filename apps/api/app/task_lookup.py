"""Resolve a `Task` by id or 404 — the ONE spelling, shared by every task-addressed route.

`app/routes/tasks/`, `attachments.py` and `task_ratings.py` each carried a private copy; three copies
of a four-line function is the context-architecture defect (the third same-language copy triggers
extraction), and two of them had already drifted on the message. Read through `entity_shape.get_or_404`
(a SELECT the tenancy seam filters), never `session.get`.

`page_memo`: `GET /api/tasks/{id}/page` runs its panels' handlers in-process on one session, each loading the
same row again; inside it a found row is served from a memo instead. Opt-in there only, never per transaction:
the composed read runs under one seam binding, while a memo spanning a transaction could hand a row loaded
under one scope to a read bound to another.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.entity_shape import get_or_404
from app.models.tasks import Task

#: `session.info` key of the page memo (`page_memo`): task id → the `Task` a lookup found.
_PAGE_TASKS = "_task_lookup_page_tasks"


@contextmanager
def page_memo(session: Session) -> Iterator[None]:
    """Within the block, `get_task_or_404` and `find_task` serve a row already found from a memo — a strong
    reference, so it is the very instance a re-SELECT would return from the identity map (a SELECT does not
    refresh a loaded row) — and `session.get` hits every row loaded since. A miss is never memoised: its 404
    stays each caller's own."""
    session.info[_PAGE_TASKS] = {}
    # Every row the parts load is held for the block too: the identity map holds rows WEAKLY, so a row dropped
    # by one part (the org `hipaa.org_is_on` reads) is collected and the next part's `session.get` re-SELECTs it
    # (`slot_allocation.task_has_repo`'s pin, same reason).
    held: list[object] = []

    def hold(_session: Session, instance: object) -> None:
        held.append(instance)

    event.listen(session, "loaded_as_persistent", hold)
    try:
        yield
    finally:
        event.remove(session, "loaded_as_persistent", hold)
        session.info.pop(_PAGE_TASKS, None)


def find_task(task_id: int, session: Session) -> Optional[Task]:
    """The Task, or None — `get_task_or_404` without the refusal, through the same memo."""
    memo = session.info.get(_PAGE_TASKS)
    if memo is not None and task_id in memo:
        return memo[task_id]
    task = session.scalars(select(Task).where(Task.id == task_id)).one_or_none()
    if memo is not None and task is not None:
        memo[task_id] = task
    return task


def get_task_or_404(task_id: int, session: Session) -> Task:
    """The Task, or a 404 naming the id — same status and message from every route, so a client cannot
    tell which route refused. No `with_for_update`: a caller that needs the row locked loads it itself."""
    memo = session.info.get(_PAGE_TASKS)
    if memo is not None and task_id in memo:
        return memo[task_id]
    task = get_or_404(session, Task, task_id, detail=f"task {task_id} not found")
    if memo is not None:
        memo[task_id] = task
    return task
