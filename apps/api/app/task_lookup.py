"""Resolve a `Task` by id or 404 — the ONE spelling, shared by every task-addressed route.

`app/routes/tasks/`, `attachments.py` and `task_ratings.py` each carried a private copy; three copies
of a four-line function is the context-architecture defect (the third same-language copy triggers
extraction), and two of them had already drifted on the message. Read through `entity_shape.get_or_404`
(a SELECT the tenancy seam filters), never `session.get`.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.entity_shape import get_or_404
from app.models.tasks import Task


def get_task_or_404(task_id: int, session: Session) -> Task:
    """The Task, or a 404 naming the id — same status and message from every route, so a client cannot
    tell which route refused. No `with_for_update`: a caller that needs the row locked loads it itself."""
    return get_or_404(session, Task, task_id, detail=f"task {task_id} not found")
