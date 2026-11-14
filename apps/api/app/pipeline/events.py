"""The `pipeline.changed` live event: `{entity: "pipeline", action: "changed", id: <project id>}` on the console
stream (`app/events.py`), so an open Pipeline page refetches its `view` (architecture §4).

One seam, transaction-scoped: every write that moves an item or a step marks its project (`touch`) and the event is
queued once per project just before the transaction commits, so a rolled-back write tells nobody and a burst of
writes is one event. Marks come from three places:

- ORM writes, found by a flush listener: releases, release approvals and changes, merge-lane rows, stages, passes and
  per-item approvals (whatever writes them: the service verbs, the executor, the tick, the merge lane);
- task transitions, through `app/events.py::emit_console_event` (`touch_task`): every lifecycle writer emits there;
- bulk statements no flush sees (`update(...)`, `insert(...)` on these tables), which call `touch` themselves.

The relay (`routes/stream.py`) shows the event only to a subscriber who may read the project.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy import event, select
from sqlalchemy.orm import Session

_PROJECTS = "pipeline_changed_projects"
_TASKS = "pipeline_changed_tasks"
#: The models whose rows move an item or a step, each carrying `project_id` (or reached through its release).
_WATCHED: Optional[tuple[type, ...]] = None


def _watched() -> tuple[type, ...]:
    global _WATCHED
    if _WATCHED is None:
        from app.models.merge_lane import MergeLaneRow
        from app.models.releases import (
            PipelineApproval,
            PipelinePass,
            Release,
            ReleaseApproval,
            ReleaseChange,
            ReleaseStage,
        )

        _WATCHED = (Release, ReleaseApproval, ReleaseChange, MergeLaneRow, ReleaseStage, PipelinePass,
                    PipelineApproval)
    return _WATCHED


def touch(session: Session, project_id: Optional[int]) -> None:
    """Mark `project_id`'s pipeline changed in this transaction (one event at commit)."""
    if project_id is not None:
        session.info.setdefault(_PROJECTS, set()).add(int(project_id))


def touch_task(session: Session, task_id: Any) -> None:
    """Mark the pipeline of the project task `task_id` belongs to (resolved at commit, one read for all). Only on
    Postgres: the event is a NOTIFY, and a unit test's bare SQLite session has no `tasks` to resolve it through."""
    if not isinstance(session, Session):
        return  # a recording stand-in in a unit test
    bind = session.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", "postgresql") != "postgresql":
        return
    if isinstance(task_id, int) and task_id > 0:
        session.info.setdefault(_TASKS, set()).add(task_id)


def _on_flush(session: Session, _context: Any) -> None:
    watched = _watched()
    rows: Iterable[Any] = (*session.new, *session.dirty, *session.deleted)
    for row in rows:
        if isinstance(row, watched):
            if row in session.dirty and not session.is_modified(row, include_collections=False):
                continue
            # The release id of a fresh approval or change is on the row; its project needs no read for a release.
            project_id = getattr(row, "project_id", None)
            if project_id is not None:
                touch(session, project_id)
            elif (release_id := getattr(row, "release_id", None)) is not None:
                session.info.setdefault(_PROJECTS + "_releases", set()).add(release_id)


def _before_commit(session: Session) -> None:
    from app.events import emit_console_event
    from app.models.projects import Project
    from app.models.releases import Release
    from app.models.tasks import Task

    # The commit's own flush runs after this hook: flush now, so a write still pending is marked too.
    session.flush()
    projects: set[int] = set(session.info.pop(_PROJECTS, set()))
    tasks: set[int] = set(session.info.pop(_TASKS, set()))
    releases: set[int] = set(session.info.pop(_PROJECTS + "_releases", set()))
    if tasks:
        projects |= {p for p in session.scalars(select(Task.project_id).where(Task.id.in_(sorted(tasks))))
                     if p is not None}
    if releases:
        projects |= set(session.scalars(select(Release.project_id).where(Release.id.in_(sorted(releases)))))
    if not projects:
        return
    orgs = dict(session.execute(select(Project.id, Project.org_id).where(Project.id.in_(sorted(projects)))).all())
    for project_id in sorted(projects):
        if project_id in orgs:
            emit_console_event(session, entity="pipeline", id=project_id, action="changed",
                               record_transition=False, org_id=orgs[project_id])


def _forget(session: Session, previous_transaction: Any) -> None:
    """A rolled-back transaction tells nobody; a savepoint's rollback leaves the outer transaction's marks."""
    if getattr(previous_transaction, "nested", False):
        return
    for key in (_PROJECTS, _TASKS, _PROJECTS + "_releases"):
        session.info.pop(key, None)


def install() -> None:
    """Register the listeners on every `Session` (idempotent); `app/pipeline/__init__.py` calls it at import."""
    if not event.contains(Session, "after_flush", _on_flush):
        event.listen(Session, "after_flush", _on_flush)
        event.listen(Session, "before_commit", _before_commit)
        event.listen(Session, "after_soft_rollback", _forget)
