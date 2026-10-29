"""The one bounded task->parent walk, and the actor-column questions asked through it.

`launcher_user_id` (whose permissions this task runs with), `starter_user_id` (whose git credential it
pushes with), `creator_user_id` (who filed it), `executor_user_id` (who owns what its runner files) and
`assignee_user_id` (who is told about it) share the walk and nothing else; each states its own fallback.

This module must never learn what a grant is (`app/placement_grants.py` owns that), or the walk grows
a second, grant-aware copy.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.identity import AdminUser
from app.models.tasks import Task

log = logging.getLogger("app.task_ancestry")

# The Task, its parent, its grandparent: the operator's chosen bound. The walk resolves provenance,
# never access; that is safe for alerts only because the answer path re-checks access
# (`slack_inbound._may_answer`), so the bound and that re-check move together.

MAX_OWNER_WALK_HOPS = 3


def task_and_ancestors(session: Session, task_id: Optional[int]):
    """The Task and then its ancestors, nearest first; the one bounded parent walk in this tree.

    A generator so a caller that finds its answer early abandons it and pays for no further ancestors.
    Exhausting the bound costs callers different things: an alert falls to its pinned recipient, an epic
    probe reads "not in an epic", a launcher resolution reads unrestricted (fail-open, D5), a starter
    resolution is refused. Hence one caller-neutral warning.

    `visited` guards cycles: `parent_task_id` is a plain self-FK that does not forbid one."""
    if task_id is None:
        return
    visited: set[int] = set()
    current: Optional[int] = task_id
    for _ in range(MAX_OWNER_WALK_HOPS):
        if current in visited:
            # A `parent_task_id` cycle; the schema does not forbid one.
            log.warning(
                "task-ancestry: parent_task_id cycle reached at task_id=%s while walking ancestors "
                "— the walk stops here",
                current,
            )
            return
        visited.add(current)
        task = session.get(Task, current)
        if task is None:
            return
        yield task
        if task.parent_task_id is None:
            return
        current = task.parent_task_id
    log.warning(
        "task-ancestry: gave up walking parents from task_id=%s after %s hops — no assignee found (the "
        "alert falls to the creator chain, then the pinned recipient), no owner found (an alert falls "
        "to the pinned recipient), no epic found (a question is referred to nobody), no launcher found "
        "(dispatch treats the task as unrestricted), and no starter found (git access is refused, or "
        "falls to the system credential for a machine-filed row)",
        task_id, MAX_OWNER_WALK_HOPS,
    )


def launcher_user_id(session: Session, task_id: Optional[int]) -> Optional[UUID]:
    """Whose permissions this task runs with — its credential roster, project environment, project
    settings, peer-task reads and placement grants: the chain's starter (`starter_user_id`), else its
    creator (`creator_user_id`), else None.

    The starter wins because the person who last pressed Start/Run is the one acting through the task:
    a task one human filed and another (re)started runs as the starter. A system re-dispatch stamps no
    starter, so it keeps the last human's. A chain nobody started falls to its creator. D5: a chain
    resolving to nobody or to the system actor is unrestricted; the system actor is refused as a grant
    subject. Equal to `executor_user_id` by construction; the two names answer different questions.

    A starter outside the task's own org is passed over for the creator: grants resolve inside the
    principal's org, so a cross-tenant Start would otherwise empty the roster and env of a tenant task, or
    lend it another org's credentials."""
    starter = starter_user_id(session, task_id)
    if starter is not None:
        task = session.get(Task, task_id)
        user = session.get(AdminUser, starter)
        if task is None or user is None or user.org_id == task.org_id:
            return starter
    return creator_user_id(session, task_id)


def creator_user_id(session: Session, task_id: Optional[int]) -> Optional[UUID]:
    """Who filed this task: the nearest ancestor's non-null, non-`is_system` `created_by_id`, or None
    when the chain names nobody.

    D1: a runner-filed row carries its chain's executor as `created_by_id` (`executor_user_id`), so its
    children resolve to that human (`routes/tasks/` stamps children one hop below the epic; nesting
    deeper would make the walk exhaust to None).

    No last-resort human here: that pin would attribute every creator-less task to one human."""
    for task in task_and_ancestors(session, task_id):
        if task.created_by_id is None:
            continue
        user = session.get(AdminUser, task.created_by_id)
        if user is not None and not user.is_system:
            return user.id
    return None


def starter_user_id(session: Session, task_id: Optional[int]) -> Optional[UUID]:
    """Whose git credential this task pushes with: the nearest ancestor's non-null, non-`is_system`
    `started_by_id`, or None when the chain names nobody.

    A sibling of `creator_user_id`, not a parameter, because it fails the other way: None fails closed
    at every caller, since lending an unattributed task a person's push credential is the mis-attribution
    the column prevents. A `coder` child inherits from the epic one hop up; nesting deeper would refuse.

    `is_system` is excluded: the seeded actor holds no credential, so a chain naming only it reads as
    machine-filed, which `git_access.py` refuses (except a cron-filed row, via its cron owner)."""
    return first_human_starter(session, started_by_chain(session, task_id))


def may_stamp_starter(user: Optional[AdminUser], org_id: Optional[UUID]) -> bool:
    """Whether `user` may be written as the `started_by_id` of a task in `org_id`: only a seat in that org.

    The git-token route reads the starter bound to the node's org (`routes/agent/task_secrets.py::get_task_git_token`), where
    another org's seat does not exist, so a cross-org stamp reads as no starter and every push is refused.
    Every writer of a starter from a person asks this; `launcher_user_id` passes over such a
    starter for the same reason."""
    return user is not None and user.org_id == org_id


def started_by_chain(session: Session, task_id: Optional[int]) -> list[UUID]:
    """`starter_user_id`'s first half: every `started_by_id` up the chain, nearest first."""
    return [task.started_by_id for task in task_and_ancestors(session, task_id) if task.started_by_id is not None]


def first_human_starter(session: Session, user_ids: list[UUID]) -> Optional[UUID]:
    """`starter_user_id`'s second half: the first of `user_ids` that exists and is not `is_system`. Separate so
    the people can be read in another org than the chain: a carried-over task's starter stayed in the org its
    project moved out of (`routes/agent/task_secrets.py::_carried_over_git_token`)."""
    for user_id in user_ids:
        user = session.get(AdminUser, user_id)
        if user is not None and not user.is_system:
            return user.id
    return None


def executor_user_id(session: Session, task_id: Optional[int]) -> Optional[UUID]:
    """Who owns what this task's runner files (tasks, crons, artifacts): the chain's starter
    (`starter_user_id`), else its creator (`creator_user_id`), else None.

    The starter wins because the person who ran the task is the one acting through it: an advisor one
    human created and another started files as the starter. A chain nobody started (a cron-fired task)
    falls to its creator, the cron owner. None leaves the filed row unattributed, as a chain naming no
    human always did."""
    return launcher_user_id(session, task_id)


def assignee_user_id(session: Session, task_id: Optional[int]) -> Optional[UUID]:
    """Who this task's notifications are addressed to: the nearest ancestor's non-null, non-`is_system`
    `assigned_to_id`, else the creator chain (`creator_user_id`).

    A chain naming no assignee falls back to the creator chain rather than failing, so a row with no
    assignee keeps reaching whoever it reached before; never None while an ancestor has a human creator.

    The assignee walk runs to its bound before the creator walk starts, so an epic addressed to one
    person addresses its pipeline-filed children to that person. `is_system` is excluded as above.

    No last-resort human here: the alert path (`notify_targets._task_owner`) decides who hears about a
    None, and it names nobody."""
    for task in task_and_ancestors(session, task_id):
        if task.assigned_to_id is None:
            continue
        user = session.get(AdminUser, task.assigned_to_id)
        if user is not None and not user.is_system:
            return user.id
    return creator_user_id(session, task_id)
