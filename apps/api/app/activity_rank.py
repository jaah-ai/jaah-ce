"""Recent activity, the order the create wizards pre-pick by: a project's tasks created in the last
`WINDOW`, and a node's distinct tasks run there (`agent_sessions`) in the same window. Read by
`GET /api/projects?order=activity` and `GET /api/nodes?order=activity`; nothing is stored."""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable
from uuid import UUID

from sqlalchemy import ColumnElement, distinct, func, select
from sqlalchemy.orm import Session

from app.clock import utcnow as _now
from app.models.agent_sessions import AgentSession
from app.models.projects import Project
from app.models.tasks import Task

WINDOW = timedelta(days=30)


def project_task_count() -> ColumnElement[int]:
    """Per `Project` row in the enclosing select: its tasks created within `WINDOW` (a correlated count)."""
    return (
        select(func.count(Task.id))
        .where(Task.project_id == Project.id, Task.created_at >= _now() - WINDOW)
        .correlate(Project)
        .scalar_subquery()
    )


def node_task_counts(session: Session, node_ids: Iterable[UUID]) -> dict[UUID, int]:
    """Distinct tasks with a session on each node within `WINDOW`; a node with none is absent."""
    ids = list(node_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(AgentSession.node_id, func.count(distinct(AgentSession.task_id)))
        .where(
            AgentSession.node_id.in_(ids),
            AgentSession.task_id.is_not(None),
            AgentSession.started_at >= _now() - WINDOW,
        )
        .group_by(AgentSession.node_id)
    ).all()
    return {node_id: count for node_id, count in rows}
