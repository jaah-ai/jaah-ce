"""The onboarding task every console-created project starts with.

`POST /api/projects` files one `onboard` task on the new project and starts it, Guided (`autonomous=False`),
attributed to the creator — the same create and Start a person would make from the task form, so it carries
their authority and nothing more. Best effort: a refused create or Start never fails the project create; the
setup guide still leads to the project's task list, where one can be filed by hand. Not filed when the
operator seeds a tenant's project (`routes/projects.py::_create_project_in`).

A repo project's task is started from the "Prepare project for Jaah" starter template
(`task_template_starters.PREPARE_FOR_JAAH_ID`): its brief, as the operator last edited it, is the body, and the start
counts toward Most popular. An unpublished or deleted template falls back to the starter's own brief. What the
onboarding engineer then does is its role's doctrine (`dispatch_compose/doctrine.py`, role `onboard`); the brief only
names the ask.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.authz.catalog import permission_key
from app.authz.principal import Principal
from app.authz.visibility import has_project_key
from app.models.identity import AdminUser
from app.models.projects import Project
from app.task_template_starters import PREPARE_FOR_JAAH_ID, STARTERS
from app import task_templates

log = logging.getLogger(__name__)

ONBOARD_PERSONA = "onboard"


def onboarding_task_title(project: Project) -> str:
    """The task's title, sent explicitly: a title derived from the body runs its lines together."""
    return f"Prepare {project.name} for Jaah" if project.has_repo else f"Set up {project.name} in Jaah"


def onboarding_task_body(project: Project) -> str:
    """The task's Description: what the ask covers. It does not repeat the title, which the task page already shows
    above it."""
    if project.has_repo:
        return next(s.body for s in STARTERS if s.id == PREPARE_FOR_JAAH_ID)
    return "This project has no code repository. Ask me what I want to do with it, then configure the project for that."


def file_onboarding_task(
    session: Session, project: Project, actor: AdminUser, principal: Optional[Principal]
) -> Optional[int]:
    """File and start the onboarding task on a just-committed `project`; its id, or None when nothing was
    filed. A system project is skipped (`create_task` refuses one); a refused
    Start leaves the task filed in `draft`, and its id is still returned."""
    # Imported here: `routes.tasks` is the console's own create and Start, and a module-level import would
    # load the whole task router into every importer of this module. The alias, as `routes/compliance.py`'s:
    # a bare `….create_task(` reads as an event-loop spawn to `test_tenancy_plane_sites.py`.
    from app.routes.tasks import create, dispatch
    from app.routes.tasks.create import create_task as create_console_task

    if project.system_kind is not None:
        return None
    try:
        template_id = UUID(PREPARE_FOR_JAAH_ID) if project.has_repo else None
        if template_id is not None and task_templates.startable(session, template_id, project.org_id) is None:
            template_id = None
        created = create_console_task(
            body=create.TaskCreate(
                project_id=project.id, persona=ONBOARD_PERSONA, autonomous=False,
                title=onboarding_task_title(project), template_id=template_id,
                # Blank takes the template's brief (`create.py::_apply_template`).
                body="" if template_id is not None else onboarding_task_body(project),
            ),
            _user=actor, principal=principal, session=session,
        )   # commits
    except HTTPException as exc:
        session.rollback()
        log.info("onboarding task not filed for project %s: %s", project.id, exc.detail)
        return None
    except Exception:
        # Never a 500 on a committed project: a retried create would make a second one.
        session.rollback()
        log.exception("onboarding task not filed for project %s", project.id)
        return None
    # Start is the task form's `tasks:edit` lever; the direct call skips its route guard, so ask it here.
    if not has_project_key(principal, permission_key("tasks", "edit"), project.id):
        log.info("onboarding task %s filed but not started: its creator may not start tasks", created.id)
        return created.id
    try:
        dispatch.start_task(task_id=created.id, body=None, _user=actor, principal=principal, session=session)
    except HTTPException as exc:
        session.rollback()
        log.info("onboarding task %s filed but not started: %s", created.id, exc.detail)
    except Exception:
        session.rollback()
        log.exception("onboarding task %s filed but not started", created.id)
    return created.id
