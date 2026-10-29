"""Task templates — who sees, starts and edits which template, and the start counter.

The rules every reader of a template shares, so the gallery (`app/routes/task_templates.py`) and the task create seam
(`app/routes/tasks/create.py`) cannot disagree: a template is VISIBLE to an org when it is global, or the org's own
while the org's plan has "Templates (org)" on (`plan_terms.ORG_TEMPLATES`); it STARTS when visible and published. The
tables and their reasons: `app/models/task_templates.py`.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Union
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import plan_terms
from app.clock import utcnow
from app.models.task_templates import OrgTaskTemplate, OrgTaskTemplateStartDay, TaskTemplate, TaskTemplateStartDay

ORIGIN_GLOBAL, ORIGIN_ORG = "global", "org"
#: "Most popular" counts the last 90 days, today included (PRD D8).
POPULAR_DAYS = 90
#: The task create seam's refusal.
UNAVAILABLE_DETAIL = "template_id: Template unavailable"

AnyTemplate = Union[TaskTemplate, OrgTaskTemplate]


def origin_of(row: AnyTemplate) -> str:
    return ORIGIN_ORG if isinstance(row, OrgTaskTemplate) else ORIGIN_GLOBAL


def org_templates_on(session: Session, org_id: Optional[UUID]) -> bool:
    """Whether `org_id`'s plan has "Templates (org)" on; no org reads off."""
    return org_id is not None and plan_terms.switch_on(session, org_id, plan_terms.ORG_TEMPLATES)


def find(session: Session, template_id: UUID) -> Optional[AnyTemplate]:
    """The global template, or else the org template readable in `session`'s scope, with this id."""
    return session.get(TaskTemplate, template_id) or session.scalars(
        select(OrgTaskTemplate).where(OrgTaskTemplate.id == template_id)
    ).one_or_none()


def visible(row: AnyTemplate, org_id: Optional[UUID], *, switch_on: bool) -> bool:
    """Global, or `org_id`'s own while its switch is on."""
    if isinstance(row, TaskTemplate):
        return True
    return row.org_id == org_id and switch_on


def startable(session: Session, template_id: UUID, org_id: Optional[UUID]) -> Optional[AnyTemplate]:
    """The template a task in `org_id` may be created from — visible and published — or None."""
    row = find(session, template_id)
    if row is None or not row.published:
        return None
    if isinstance(row, OrgTaskTemplate) and not visible(row, org_id, switch_on=org_templates_on(session, org_id)):
        return None
    return row


def window_start(today: Optional[date] = None) -> date:
    """The first day the popularity window counts."""
    return (today or utcnow().date()) - timedelta(days=POPULAR_DAYS - 1)


def starts_90d(session: Session, rows: list[AnyTemplate]) -> dict[UUID, int]:
    """Each template's starts in the window, read off the two counter tables (never `tasks`)."""
    since = window_start()
    counts: dict[UUID, int] = {}
    global_ids = [row.id for row in rows if isinstance(row, TaskTemplate)]
    org_ids = [row.id for row in rows if isinstance(row, OrgTaskTemplate)]
    if global_ids:
        counts.update(session.execute(
            select(TaskTemplateStartDay.template_id, func.sum(TaskTemplateStartDay.starts))
            .where(TaskTemplateStartDay.template_id.in_(global_ids), TaskTemplateStartDay.day >= since)
            .group_by(TaskTemplateStartDay.template_id)
        ).all())
    if org_ids:
        counts.update(session.execute(
            select(OrgTaskTemplateStartDay.org_template_id, func.sum(OrgTaskTemplateStartDay.starts))
            .where(OrgTaskTemplateStartDay.org_template_id.in_(org_ids), OrgTaskTemplateStartDay.day >= since)
            .group_by(OrgTaskTemplateStartDay.org_template_id)
        ).all())
    return {key: int(value) for key, value in counts.items()}


def record_start(session: Session, row: AnyTemplate) -> None:
    """Add one start to today's counter. Called only for a task a PERSON created (I-2), in the create's own
    transaction."""
    today = utcnow().date()
    if isinstance(row, TaskTemplate):
        statement = insert(TaskTemplateStartDay).values(template_id=row.id, day=today, starts=1)
        statement = statement.on_conflict_do_update(
            index_elements=["template_id", "day"], set_={"starts": TaskTemplateStartDay.starts + 1}
        )
    else:
        statement = insert(OrgTaskTemplateStartDay).values(
            org_id=row.org_id, org_template_id=row.id, day=today, starts=1
        )
        statement = statement.on_conflict_do_update(
            index_elements=["org_template_id", "day"], set_={"starts": OrgTaskTemplateStartDay.starts + 1}
        )
    session.execute(statement)
