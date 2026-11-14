"""A Task's "done" notice: whether it is owed (`tasks.notify_when_done`), who it is owed to, and who has seen it.

The single home of three rules:

* **The default at filing** (`default_for`): ON for a human-filed task (`console` source, not a system persona) and an
  incident-class one (`INCIDENT_SOURCES`); OFF for everything else — schedules, other triggers, agent filings, system
  personas, and a chat (`advisor`), which never finishes on its own. Applied as the column's context-sensitive default (`column_default`), so every filing seam gets it and an
  explicit value wins.
* **The recipients** (`recipient_ids`): the assignee always; the creator too only when the task is human-filed. An
  automation's owner is its creator, and its tasks are not theirs to be told about unless assigned.
* **Seen** (`task_done_views`, `unseen_ids`, `mark_seen`): a recipient's "done but not seen" mark clears once their
  `seen_at` is at or after the task's `done_at`, so a reopened and re-shipped task is unseen again.

Delivery itself is `app/task_notify.py::_deliver_done_transition`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Optional
from uuid import UUID

from app import task_source
from app.persona_registry import SYSTEM_PERSONAS
from app.task_lifecycle import DONE

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.identity import AdminUser
    from app.models.tasks import Task

# Production QA's incident sources: someone is waiting on these, whoever filed them.
INCIDENT_SOURCES: frozenset[str] = frozenset(
    task_source.TRIGGER_PREFIX + kind for kind in ("incident", "deploy_failed", "health_breach")
)


def human_filed(source: Optional[str], persona: Optional[str]) -> bool:
    """Filed by a person — in the console, or a runner's filing they asked for (`task_source.requested_source`): a
    `console` source on a persona that is not a system one."""
    return (source or task_source.CONSOLE) == task_source.CONSOLE and persona not in SYSTEM_PERSONAS


def default_for(source: Optional[str], persona: Optional[str]) -> bool:
    """`notify_when_done` for a row filed without one."""
    from app.routes.tasks.policy import PERSONA_ADVISOR  # noqa: PLC0415 — policy imports the Task model, which reads this

    if persona == PERSONA_ADVISOR:
        return False
    return human_filed(source, persona) or source in INCIDENT_SOURCES


def column_default(context: Any) -> bool:
    """`Task.notify_when_done`'s Python-side default, read off the INSERT's own parameters. A missing source is the
    column's own default, `console`."""
    params = context.get_current_parameters()
    return default_for(params.get("source"), params.get("persona"))


def recipient_ids(task: "Task") -> set[UUID]:
    """Whose notice this is: the assignee, plus the creator of a human-filed task. Ids, not people: the caller decides
    who is human and who may read the task."""
    ids = {task.assigned_to_id} if task.assigned_to_id is not None else set()
    if task.created_by_id is not None and human_filed(task.source, task.persona):
        ids.add(task.created_by_id)
    return ids


def _owes(task: "Task", user_id: UUID) -> bool:
    # Top-level only: an epic's stages ship into the epic, which carries the one notice.
    return (
        task.notify_when_done and task.state == DONE and task.parent_task_id is None
        and user_id in recipient_ids(task)
    )


def unseen_ids(session: "Session", tasks: Iterable["Task"], user_id: Optional[UUID]) -> set[int]:
    """Which of `tasks` are done and not yet seen by `user_id` — `TaskOut.done_unseen` for a whole page in at most ONE
    query. Empty for no user."""
    if user_id is None:
        return set()
    owed = [task.id for task in tasks if _owes(task, user_id)]
    if not owed:
        return set()
    from sqlalchemy import exists, or_, select

    from app.models.identity import AdminUser
    from app.models.tasks import Task, TaskDoneView

    # A NULL `done_at` (a row shipped before the column) counts any view as having seen it.
    seen = exists().where(
        TaskDoneView.task_id == Task.id,
        TaskDoneView.user_id == user_id,
        or_(Task.done_at.is_(None), TaskDoneView.seen_at >= Task.done_at),
    )
    # Only rows of the viewer's own org: `mark_seen` records nothing across orgs, so a ring owed there could never
    # clear (an operator who filed into a tenant org).
    home_org = select(AdminUser.org_id).where(AdminUser.id == user_id).scalar_subquery()
    return set(session.scalars(select(Task.id).where(Task.id.in_(owed), Task.org_id == home_org, ~seen)))


def mark_seen(session: "Session", task: "Task", user: "AdminUser") -> None:
    """Stamp `user`'s `seen_at` on `task` now; the caller commits. Only a notice owed to them is recorded, so
    opening any task does not grow the table. A person of another org (an operator) records nothing: the row's org
    keys hold both ends to the task's org."""
    if user.org_id != task.org_id or not _owes(task, user.id):
        return
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.tasks import TaskDoneView

    # `org_id` named: a Core insert flushes nothing, so the write seam cannot stamp it (`routes/task_ratings.py`).
    stmt = pg_insert(TaskDoneView).values(task_id=task.id, user_id=user.id, org_id=task.org_id, seen_at=func.now())
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TaskDoneView.task_id, TaskDoneView.user_id],
            set_={"seen_at": func.now()},
        )
    )
