"""The release executor task: one hidden `ready` task per project that carries its queued releases out on a node.

Mirrors `merge_lane.ensure_lane`: persona `release` (a script persona — the node runs the packaged release tool;
that tool and its verbs belong to the executor, not here), priority High, dedup `release:{project_id}:1`, Hidden on
Live Cast. Started by the requester when they are a usable member holding a forge login on the project's forge,
else by the project's "Run jobs as" person (`git_access.project_forge_person`). Called on deploy, hotfix, retry and
an approval that queues a release; the release tick calls it for every queued release.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import edition_features, forges, git_access
from app.events import emit_console_event
from app.models.nodes import Node
from app.models.projects import Project
from app.models.releases import Release
from app.models.tasks import Task
from app.releases import lifecycle
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, DONE, PAUSED, READY, SYSTEM_FAILED

log = logging.getLogger(__name__)

PERSONA = "release"
#: High (`task_priority.PRIORITY_LEVELS`).
PRIORITY = 10
_ENDED = (DONE, SYSTEM_FAILED)


def dedup_key(project_id: int) -> str:
    return f"release:{project_id}:1"


def open_task(session: Session, project_id: int) -> Optional[Task]:
    """The project's executor task that has not ended."""
    return session.scalar(
        select(Task).where(
            Task.project_id == project_id, Task.persona == PERSONA, Task.state.not_in(_ENDED),
        ).order_by(Task.id.desc()).limit(1)
    )


def ensure_executor(session: Session, project: Project, requester_id: Optional[UUID] = None, *,
                    for_release: bool = False) -> Optional[int]:
    """File the project's executor task unless one is open; its id (the open one's when there is one), or None when
    nobody can start it — for a queued release (`for_release`), also when nobody it could start as holds a forge
    login (`no_login_reason`): the release waits, saying why, and the tick files it once someone connects. Does not
    commit."""
    existing = open_task(session, project.id)
    if existing is not None:
        return existing.id
    person = git_access.requested_job_person(session, project, requester_id)
    if person is None:
        log.info("release executor: no person can start one for project %s", project.id)
        return None
    if for_release and not git_access.has_forge_login(session, person, project) \
            and no_login_reason(session, project) is not None:
        log.info("release executor: nobody holds a forge login for project %s", project.id)
        return None
    task = Task(
        project_id=project.id,
        org_id=project.org_id,
        title=f"Release: {project.name}",
        # A person reads it on the run's page (`authz._release_run_clause`): plain words, no code.
        body="Carries out this project's releases, one after another, as they are queued.",
        dedup_key=dedup_key(project.id),
        persona=PERSONA,
        priority=PRIORITY,
        state=READY,
        hidden=True,
        parent_task_id=None,
        created_by_id=person,
        started_by_id=person,
    )
    try:
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        # A concurrent call holds the key: its task serves the queue just as well.
        held = open_task(session, project.id)
        return held.id if held is not None else None
    if edition_features.enabled("live_cast"):  # Hidden on Live Cast: control-plane plumbing, never public content
        from app.live_cast import state as live_cast_state  # deferred: a top-level import closes an import cycle

        live_cast_state.hide_system_task(session, task)
    emit_console_event(session, entity="task", id=task.id, action="ready")
    log.info("release executor: filed task %s for project %s", task.id, project.id)
    return task.id


def no_login_reason(session: Session, project: Project) -> Optional[str]:
    """Why no executor can run `project`'s queued releases: neither their requesters nor the project's "Run jobs as"
    person (`git_access.project_forge_person`) holds a login on the project's forge; None when someone does, or the
    repository's login is not a person's (a Jaah-made repository, `jaah_repos.runner_app_repo`; no usable URL)."""
    from app import jaah_repos

    try:
        repo = forges.repo_ref(project)
    except forges.ForgeUrlError:
        return None
    if jaah_repos.runner_app_repo(session, project, repo) is not None:
        return None
    requesters = session.scalars(select(Release.requested_by_id).where(
        Release.project_id == project.id, Release.state == lifecycle.QUEUED).distinct())
    people = [*(r for r in requesters if r is not None and git_access.usable_job_person(session, r, project.org_id)),
              git_access.project_forge_person(session, project)]
    if any(p is not None and git_access.has_forge_login(session, p, project) for p in people):
        return None
    label = forges.FORGE_LABELS[repo.forge]
    return f"Nobody can run this release: connect a {label} account in {git_access.profile_path(repo.forge)}."


def waiting_for_login(session: Session, project: Project) -> Optional[str]:
    """`no_login_reason` while a release is queued and no executor is open (none could be filed), else None."""
    if open_task(session, project.id) is not None or session.scalar(select(Release.id).where(
            Release.project_id == project.id, Release.state == lifecycle.QUEUED).limit(1)) is None:
        return None
    return no_login_reason(session, project)


def executor_state(session: Session, project: Project) -> dict[str, object]:
    """`{state: idle|queued|running, node_online, task_id, wait_reason}`: the executor task's position, whether any of
    the project's org's nodes is connected now, the open executor task's id, and why a `queued` one is not running
    (`org_limits.wait_reasons`, the sentence its own task page shows; None when nothing holds it back)."""
    from app import org_limits
    from app.node_presence import connected_node_ids

    task = open_task(session, project.id)
    wait_reason = org_limits.wait_reasons(session, [task]).get(task.id) if task is not None and task.state == READY \
        else None
    if task is None:
        # No executor while a release is queued: nobody can start one (`ensure_executor`), so the queue says why.
        wait_reason = waiting_for_login(session, project)
    elif task.state == READY and wait_reason is None and task.started_by_id is not None \
            and not git_access.has_forge_login(session, task.started_by_id, project):
        # One a survey or arrival filed as someone with no login: its release still waits on a connect.
        wait_reason = no_login_reason(session, project)
    if task is None:
        state = "idle"
    elif task.state == READY:
        state = "queued"
    elif task.state in (ACTIVE, AWAITING_RESPONSE, PAUSED):
        state = "running"
    else:
        state = "idle"
    connected = set(connected_node_ids(session))
    node_online = bool(connected) and any(
        str(node_id) in connected
        for node_id in session.scalars(select(Node.id).where(Node.org_id == project.org_id))
    )
    return {"state": state, "node_online": node_online, "task_id": task.id if task is not None else None,
            "wait_reason": wait_reason}
