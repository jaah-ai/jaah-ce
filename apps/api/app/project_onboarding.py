"""The onboarding task every console-created project starts with.

`POST /api/projects` files one `onboard` task on the new project and starts it, Guided (`autonomous=False`),
attributed to the creator — the same create and Start a person would make from the task form, so it carries
their authority and nothing more. Best effort: a refused create or Start never fails the project create; the
setup guide still leads to the project's task list, where one can be filed by hand. When the operator seeds a
tenant's project (`routes/projects.py::_create_project_in`) it is filed as that org's Owner seat (`app/act_as.py`),
and Start names the org's Owner to push as, since the operator is no member who could
(`routes/tasks/dispatch.py::_push_identity_or_422`, `owner_to_push_as`).

A project created with an intake answer whose repository Jaah made (`app/project_intake.py`) is titled "Set up <name>
in Jaah" and asks what the project is about, already knowing the answer (`setup_answer`). Any other repo project's
task is started from the "Prepare project for Jaah" starter template
(`task_template_starters.PREPARE_FOR_JAAH_ID`): its brief, as the operator last edited it, is the body, and the start
counts toward Most popular. An unpublished or deleted template, or one with a required input placeholder
(nobody is there to answer it), falls back to the starter's own brief. What the
onboarding engineer then does is its role's doctrine (`dispatch_compose/doctrine.py`, role `onboard`); the brief only
names the ask.

A new web app (`intake.kind == "web_app"`) gets no onboarding task: it gets an App Wizard epic
(`file_app_wizard_epic`) — App Wizard, then design and plan, then the builders of its first version. The epic runs
Full Auto, so design and plan never ask anything; the wizard always interviews (`Persona.always_interviews`), seeded
with the person's own description so it does not ask again what that already says.

A business project (`project_intake.BUSINESS_KINDS`, in a new repo or the person's own) gets no onboarding task
either: it gets one Business Wizard task (`file_business_wizard_task`), Guided, which interviews the owner, records
how the business works in the repository, and ships it as a pull request.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
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
BUSINESS_WIZARD_PERSONA = "business_wizard"


def owner_to_push_as(session: Session, org_id: UUID) -> Optional[UUID]:
    """Whom an operator's onboarding Start in `org_id` names to push as: the org's earliest direct, enabled Owner, else
    its earliest enabled superadmin; None when it has neither. The rule `tenancy/transfer.py::target_owner` keeps for
    a transfer, homed here as well because that module is not in the community edition."""
    from app.access import OWNER_ROLE
    from app.access_catalog import ROLES_BY_SLUG
    from app.models.access import RoleBinding

    owner_binding = (
        select(RoleBinding.id).where(
            RoleBinding.user_id == AdminUser.id, RoleBinding.org_id == org_id, RoleBinding.scope_org_id == org_id,
            RoleBinding.role_id == ROLES_BY_SLUG[OWNER_ROLE].id,
        ).exists()
    )
    return session.scalar(
        select(AdminUser.id)
        .where(AdminUser.org_id == org_id, AdminUser.disabled_at.is_(None), AdminUser.is_system.is_(False),
               owner_binding | AdminUser.is_superadmin.is_(True))
        .order_by(owner_binding.desc(), AdminUser.created_at, AdminUser.id)
        .limit(1)
    )


def onboarding_task_title(project: Project, setup_answer: Optional[str] = None) -> str:
    """The task's title, sent explicitly: a title derived from the body runs its lines together."""
    if project.has_repo and setup_answer is None:
        return f"Prepare {project.name} for Jaah"
    return f"Set up {project.name} in Jaah"


def onboarding_task_body(project: Project, setup_answer: Optional[str] = None) -> str:
    """The task's Description: what the ask covers. It does not repeat the title, which the task page already shows
    above it."""
    if setup_answer is not None:
        # The scope is settled here ("prepare it and plan the build") so the runner does not ask how far to go.
        return (f"Ask me what this project is about, then set it up (prepare it and plan the build). From setup you "
                f"already know: {setup_answer}. Don't ask that again.")
    if project.has_repo:
        return next(s.body for s in STARTERS if s.id == PREPARE_FOR_JAAH_ID)
    return "This project has no code repository. Ask me what I want to do with it, then configure the project for that."


def app_wizard_epic_title(project: Project) -> str:
    """The epic's title, sent explicitly like the onboarding task's."""
    return f"Build {project.name}"


def app_wizard_epic_body(project: Project, description: str, setup_answer: Optional[str] = None) -> str:
    """The epic's Description, which every child's work item is composed from: the app's name and the person's own
    description, quoted as they typed it (the dispatch fences the whole body as untrusted data)."""
    lines = [f"Build the first version of {project.name}.", "", "In the owner's words:", "", _quoted(description)]
    if setup_answer:
        lines += ["", f"From setup: {setup_answer}."]
    return "\n".join(lines)


def business_wizard_task_title(project: Project) -> str:
    """The Business Wizard task's title, sent explicitly like the onboarding task's."""
    return f"Set up {project.name}"


def business_wizard_task_body(project: Project, description: str, setup_answer: Optional[str] = None) -> str:
    """The Business Wizard task's Description: the project's name and the person's own description of the
    initiative, quoted as they typed it, like `app_wizard_epic_body`."""
    lines = [
        f"Learn how {project.name} works as a business activity and set its workspace up for Jaah's agents.", "",
        "In the owner's words:", "", _quoted(description),
    ]
    if setup_answer:
        lines += ["", f"From setup: {setup_answer}."]
    return "\n".join(lines)


def _quoted(description: str) -> str:
    """`description` as a Markdown quote, line by line."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in description.strip().splitlines())


def file_app_wizard_epic(
    session: Session, project: Project, actor: AdminUser, principal: Optional[Principal], *,
    description: str, setup_answer: Optional[str] = None,
) -> Optional[int]:
    """File and start a new web app's App Wizard epic on a just-committed `project`; its id, or None when nothing was
    filed. Full Auto, so its design and plan stages never ask; its App Wizard asks anyway. Best effort, like
    `file_onboarding_task`."""
    from app.routes.tasks import create

    return _file_and_start(session, project, actor, principal, lambda: create.ServerTaskCreate(
        project_id=project.id, persona=create.PERSONA_EPIC, autonomous=True, title=app_wizard_epic_title(project),
        body=app_wizard_epic_body(project, description, setup_answer), epic_children=create.APP_WIZARD_CHILDREN,
    ))


def file_business_wizard_task(
    session: Session, project: Project, actor: AdminUser, principal: Optional[Principal], *,
    description: str, setup_answer: Optional[str] = None,
) -> Optional[int]:
    """File and start a business project's Business Wizard task on a just-committed `project`; its id, or None when
    nothing was filed. Guided: the wizard always interviews the owner. Best effort, like `file_onboarding_task`."""
    from app.routes.tasks import create

    return _file_and_start(session, project, actor, principal, lambda: create.TaskCreate(
        project_id=project.id, persona=BUSINESS_WIZARD_PERSONA, autonomous=False,
        title=business_wizard_task_title(project), body=business_wizard_task_body(project, description, setup_answer),
    ))


def file_onboarding_task(
    session: Session, project: Project, actor: AdminUser, principal: Optional[Principal], *,
    setup_answer: Optional[str] = None,
) -> Optional[int]:
    """File and start the onboarding task on a just-committed `project`; its id, or None when nothing was
    filed. `setup_answer`: what the project is, in words, when Jaah made its repository. A system project is skipped (`create_task` refuses one); a refused
    Start leaves the task filed in `draft`, and its id is still returned."""
    from app.routes.tasks import create

    def body() -> Any:
        template_id = UUID(PREPARE_FOR_JAAH_ID) if project.has_repo and setup_answer is None else None
        template = task_templates.startable(session, template_id, project.org_id) if template_id is not None else None
        # Nobody is there to answer a required input: such a template falls back to the starter's brief.
        if template is None or any(i.required for i in task_templates.inputs_of(template.body)):
            template_id = None
        return create.TaskCreate(
            project_id=project.id, persona=ONBOARD_PERSONA, autonomous=False,
            title=onboarding_task_title(project, setup_answer), template_id=template_id,
            # Blank takes the template's brief (`create.py::_apply_template`).
            body="" if template_id is not None else onboarding_task_body(project, setup_answer),
        )

    return _file_and_start(session, project, actor, principal, body)


def _file_and_start(
    session: Session, project: Project, actor: AdminUser, principal: Optional[Principal],
    build: Callable[[], Any],
) -> Optional[int]:
    """Create the task `build` describes as `actor`, then Start it; its id, or None when nothing was filed. A system
    project is skipped (`create_task` refuses one); a refused Start leaves the task filed in `draft`, and its id is
    still returned."""
    # Imported here: `routes.tasks` is the console's own create and Start, and a module-level import would
    # load the whole task router into every importer of this module.
    from app.routes.tasks import create, dispatch

    if project.system_kind is not None:
        return None
    try:
        created = create.create_task.session_body(
            body=build(), _user=actor, principal=principal, session=session,
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
    # An acting operator is no member to push as: the org's Owner stands in, as the Start dialog would offer. None
    # found leaves Start to refuse, and the task stays filed.
    start = None
    if principal is not None and principal.acting and project.org_id is not None:
        owner = owner_to_push_as(session, project.org_id)
        start = dispatch.TaskStartIn(push_as_id=owner) if owner is not None else None
    try:
        dispatch.start_task.session_body(task_id=created.id, body=start, _user=actor, principal=principal, session=session)
    except HTTPException as exc:
        session.rollback()
        log.info("onboarding task %s filed but not started: %s", created.id, exc.detail)
    except Exception:
        session.rollback()
        log.exception("onboarding task %s filed but not started", created.id)
    return created.id
