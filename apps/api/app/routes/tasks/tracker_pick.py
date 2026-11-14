"""`tracker_issue` on task create and update: the issue a person picked on the task form, linked to the task in the
task's own commit (`trackers.project_links.link_task`).

A task route's body runs on the database session with no tracker call in it, so the issue is read BEFORE the body
(`with_tracker_pick`): authorized first (`tasks:create` on the create's project, `tasks:edit` on the edited task's
resulting project; without it the body's own refusal answers), then read from its tracker and proven to sit in one
of the project's linked tracker projects. A 404 (`NOT_LINKED_ISSUE`) or a 502 with the tracker's sentence answers
before anything is written. Resending the task's current issue reads nothing.

`tracker_issue` absent leaves the link as it is; `null` clears a hand link (with its write-backs); a task the
tracker started keeps its issue (409).
"""
from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.authz.catalog import permission_key
from app.authz.visibility import has_project_key
from app.trackers import project_links, protocol


class TrackerIssuePick(BaseModel):
    """The task form's picked issue: which linked connection, and the issue's key there."""
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    key: str = Field(min_length=1, max_length=100)


ProjectOf = Callable[[Session, dict[str, Any]], Optional[int]]


def with_tracker_pick(project_of: ProjectOf) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Any]]:
    """Wrap an `on_session` endpoint whose `body` has `tracker_issue`: read the picked issue first and hand it to
    the body as `body._picked`. `project_of(session, kwargs)` names the project the issue must be linked to, or
    None when the caller may not act there (the body then refuses on its own) or nothing need be read."""
    def wrap(endpoint: Callable[..., Awaitable[Any]]) -> Callable[..., Any]:
        @functools.wraps(endpoint)
        async def wrapped(**kwargs: Any) -> Any:
            body = kwargs["body"]
            pick = body.tracker_issue if "tracker_issue" in body.model_fields_set else None
            if pick is not None:
                db = kwargs["db"]
                project_id = await db.run_sync(lambda session: project_of(session, kwargs))
                if project_id is not None:
                    body._picked = await read_pick(db, project_id, pick)
            return await endpoint(**kwargs)

        return wrapped

    return wrap


async def read_pick(db: Any, project_id: int, pick: TrackerIssuePick) -> project_links.PickedIssue:
    try:
        return await project_links.issue(db, project_id, pick.connection_id, pick.key)
    except project_links.IssueNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except protocol.TrackerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


def may_pick(principal: Any, project_id: Optional[int], action: str) -> Optional[int]:
    """`project_id` when the caller holds `tasks:<action>` there, else None."""
    if project_id is None or not has_project_key(principal, permission_key("tasks", action), project_id):
        return None
    return project_id


def unchanged(session: Session, task_id: int, pick: TrackerIssuePick) -> bool:
    """True when the task already links the picked issue: nothing to read or write."""
    return project_links.same_issue(project_links.task_link(session, task_id), pick.connection_id, pick.key)


def apply(session: Session, task: Any, body: Any) -> None:
    """Write the body's `tracker_issue` on `task` (flushed; the caller commits with the task)."""
    if "tracker_issue" not in body.model_fields_set:
        return
    pick: Optional[TrackerIssuePick] = body.tracker_issue
    try:
        if pick is None:
            project_links.unlink_task(session, task)
            return
        if unchanged(session, task.id, pick):
            return
        picked: Optional[project_links.PickedIssue] = body._picked
        if picked is None or picked.connection_id != pick.connection_id:
            # Not read by `with_tracker_pick` (a caller composing the body in-process): never linked unread.
            raise HTTPException(status_code=404, detail=project_links.NOT_LINKED_ISSUE)
        project_links.link_task(session, task, picked)
    except project_links.LinkRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
