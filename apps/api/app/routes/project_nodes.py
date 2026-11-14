"""The nodes a task pin may name for a project: `GET /api/projects/{project_id}/runnable-nodes`.

The set `routes/tasks/create.py::_pinned_node_or_refuse` checks (`placement_grants.project_node_ids`), narrowed to the
nodes the caller can see. Where a project runs is node groups (`app/placement_groups.py`); this only reads it.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import placement_grants
from app.auth import adopt_user
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, current_caller
from app.authz.visibility import visible_node_clause
from app.db_async import on_session
from app.models.nodes import Node
from app.project_identity import project_or_404

router = APIRouter(tags=["projects"])


class RunnableNodeOut(BaseModel):
    id: UUID
    name: str


@router.get("/api/projects/{project_id}/runnable-nodes", response_model=list[RunnableNodeOut])
@feature("projects", "view", object_type="project", object_params=("project_id",))
@on_session
def get_project_runnable_nodes(
    session: Session,
    project_id: int,
    caller: AsyncCaller = Depends(current_caller),
) -> list[RunnableNodeOut]:
    """The nodes this project runs on that the caller can see, by name."""
    adopt_user(session, caller.actor)
    project = project_or_404(session, project_id)
    ids = placement_grants.project_node_ids(session, project.id, project.org_id)
    if not ids:
        return []
    return [RunnableNodeOut(id=nid, name=name) for nid, name in session.execute(
        select(Node.id, Node.name).where(Node.id.in_(sorted(ids, key=str)), visible_node_clause(caller.principal))
        .order_by(Node.name)
    ).all()]
