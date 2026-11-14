"""The executor's work that is not a release (`release_jobs`): a `survey` (which changes each stage's branch holds). At
most one open per (project, mode); the
executor task carries it out (`executor.ensure_executor`). Nothing here commits.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.projects import Project
from app.models.releases import ReleaseJob
from app.releases import executor

SURVEY = "survey"
#: A job claimed this long ago by a run that never closed it is handed out again.
CLAIM_TTL = timedelta(minutes=15)


def claimable(job: ReleaseJob, now: datetime, task_id: Optional[int] = None) -> bool:
    """Whether an executor run (`task_id`, or any new one) may take `job` now: unclaimed, or its claim is older than
    `CLAIM_TTL` and not that run's own. A run that ended without closing its job leaves a fresh claim: until it expires,
    no run may take the job, so none is filed for it (a new one would only end "nothing to do")."""
    claimed_by = (job.payload or {}).get("claimed_by")
    if claimed_by is None:
        return True
    if task_id is not None and claimed_by == task_id:
        return False
    return job.claimed_at is None or job.claimed_at < now - CLAIM_TTL


def open_job(session: Session, project_id: int, mode: str) -> Optional[ReleaseJob]:
    return session.scalar(select(ReleaseJob).where(
        ReleaseJob.project_id == project_id, ReleaseJob.mode == mode, ReleaseJob.done_at.is_(None)))


def file(session: Session, project: Project, mode: str, *, payload: Optional[dict[str, Any]] = None,
         requester_id: Optional[UUID] = None) -> tuple[ReleaseJob, bool]:
    """The project's open `mode` job, filed when there is none (an open one takes `payload` and `requester_id` when
    given), and the executor that will run it. `(job, created)`."""
    job = open_job(session, project.id, mode)
    created = False
    if job is None:
        try:
            with session.begin_nested():
                job = ReleaseJob(org_id=project.org_id, project_id=project.id, mode=mode, payload=payload,
                                 requested_by_id=requester_id)
                session.add(job)
                session.flush()
            created = True
        except IntegrityError:
            job = open_job(session, project.id, mode)  # A concurrent writer filed it.
            assert job is not None
    elif payload is not None:
        job.payload = payload
        job.requested_by_id = requester_id or job.requested_by_id
        session.flush()
    executor.ensure_executor(session, project, requester_id)
    return job, created
