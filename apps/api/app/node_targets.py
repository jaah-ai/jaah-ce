"""Node targets — the server half of "run a task on these nodes".

A node-target automation files ONE task per resolved node, each PINNED to that node (`Task.pinned_node_id`) and
filed in its org's system "Nodes" project (`Project.system_kind == 'nodes'`): a folder project (no repository),
so the task runs in a node-level, non-repo workspace — the dynamic folder slot a dispatch makes for it
(`slot_allocation.add_dynamic`, via `lease_drain._take_dynamic`).

The rules live where they always did; this module only provisions:
* placement — `placement_grants._allowed` narrows a pinned task's nodes to its pin, and
  `placement_grants.unplaceable_reason` names a pin whose node is gone or disabled;
* visibility — a system project and its tasks are shown to org admins and operators only
  (`authz.scope_key_clause`, `authz.has_scope_key`);
* creation — a task in a system project is refused unless it carries a pin (`refuse_unpinned_system_tasks`, the
  flush-time floor, and each create route's own 422). The one sanctioned filer passes `pinned_node_id` to
  `routes/tasks/create.py::new_top_level_task` after calling `prepare`.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.orgs import Org
from app.models.projects import Project
from app.models.tasks import Task

#: The `projects.system_kind` value of the per-org Nodes project (revision 0416's CHECK lists every kind).
SYSTEM_KIND_NODES = "nodes"

#: The sentence every refusal of an unpinned task in a system project carries (routes and the flush floor).
UNPINNED_SYSTEM_TASK = (
    "tasks in a system project are filed by automations only, each pinned to a node; "
    "file this task in one of your own projects"
)


#: A Nodes-project runner's follow-up when its org has no project of its own to receive it.
NO_FOLLOW_UP_PROJECT = (
    "this task runs in the system Nodes project and its organization has no project of its own; "
    "create a project, then file this task again"
)

#: A Nodes-project runner's follow-up that names artifacts: they stay in the admin-only Nodes project.
NO_FOLLOW_UP_ATTACHMENTS = (
    "this task runs in the system Nodes project; its follow-up is filed in the organization's own project and "
    "cannot carry this task's artifacts — put what matters in the body"
)


class SystemProjectTaskRefused(ValueError):
    """A flush would insert a task into a system project without a node pin. The flush-time backstop of the
    create routes' 422 (`refuse_unpinned_system_tasks`)."""


def nodes_project_name(org_slug: str) -> str:
    """The Nodes project's display name. `projects.name` is unique fleet-wide, so the org's slug keeps one org's
    Nodes project from colliding with another's."""
    return f"Nodes ({org_slug})"


def follow_up_project_id(session: Session, org_id: UUID) -> Optional[int]:
    """Where a runner in the org's Nodes project files a follow-up: the org's oldest project of its own, the
    nearest server-side rule to its default project. None when the org has none."""
    return session.scalar(
        select(Project.id).where(Project.org_id == org_id, Project.system_kind.is_(None)).order_by(Project.id).limit(1)
    )


def ensure_nodes_project(session: Session, org_id: UUID) -> Project:
    """The org's Nodes project, created on first use. Idempotent and race-safe: an `INSERT … ON CONFLICT DO
    NOTHING` on the partial unique `(org_id, system_kind)`, then a read. Flushes, never commits."""
    slug = session.scalar(select(Org.slug).where(Org.id == org_id))
    if slug is None:
        raise ValueError(f"node_targets.ensure_nodes_project: no org {org_id}")
    session.execute(
        pg_insert(Project)
        .values(
            name=nodes_project_name(slug),
            org_id=org_id,
            system_kind=SYSTEM_KIND_NODES,
        )
        .on_conflict_do_nothing(
            index_elements=["org_id", "system_kind"],
            index_where=Project.system_kind.is_not(None),
        )
    )
    return session.scalars(
        select(Project).where(Project.org_id == org_id, Project.system_kind == SYSTEM_KIND_NODES)
    ).one()


def prepare(session: Session, org_id: UUID, node_id: UUID) -> int:
    """Answer the project id a node task of `node_id` is filed in: the org's Nodes project, made on first use. It
    runs on every node of the org (the All group, `app/placement_groups.py`), so nothing is written per node;
    each task gets a dynamic folder slot at dispatch. Flushes, never commits."""
    project = ensure_nodes_project(session, org_id)
    session.flush()
    return project.id


def _system_project_ids(session: Session, project_ids: Iterable[int]) -> set[int]:
    wanted = {pid for pid in project_ids if pid is not None}
    if not wanted:
        return set()
    return set(session.scalars(
        select(Project.id).where(Project.id.in_(wanted), Project.system_kind.is_not(None))
    ).all())


def refuse_unpinned_system_tasks(session: Session, _flush_context: Any, _instances: Any) -> None:
    """`before_flush` listener (`app/db.py`): refuse a flush that would add a task to a system project without
    a node pin, whichever writer built it — a cron, a trigger, an agent filing, an epic's children. The routes'
    422 is the readable half; this is the backstop. One query per flush that adds unpinned tasks, none
    otherwise."""
    unpinned = [obj for obj in session.new if isinstance(obj, Task) and obj.pinned_node_id is None]
    if not unpinned:
        return
    with session.no_autoflush:
        system = _system_project_ids(session, (task.project_id for task in unpinned))
    if system:
        raise SystemProjectTaskRefused(UNPINNED_SYSTEM_TASK)
