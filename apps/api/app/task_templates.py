"""Task templates — who sees, starts and edits which template, and the start counter.

The rules every reader of a template shares, so the gallery (`app/routes/task_templates.py`) and the task create seam
(`app/routes/tasks/create.py`) cannot disagree: a template is VISIBLE to an org when it is global, or the org's own
while the org's plan has "Templates (org)" on (`plan_terms.ORG_TEMPLATES`); it STARTS when visible and published. The
tables and their reasons: `app/models/task_templates.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Optional, Union
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

#: An input placeholder in a template's body: `{{input: Label}}` (required) or `{{input?: Label}}` (optional). Start
#: asks for each (`StartTemplateDialog.tsx`), an automation stores a value per label (`automation_actions.py`), and the
#: value replaces every token of that label. Distinct from a trigger's `{{ns.path}}` (`trigger_template.py`): `input`
#: is no trigger namespace, and a template's body is never rendered as a trigger template.
INPUT_TOKEN = re.compile(r"\{\{\s*input(\?)?\s*:\s*([^{}\n]{1,80}?)\s*\}\}")
#: What an optional input left blank reads as in the task's body.
INPUT_NOT_GIVEN = "(not given)"
MAX_INPUT_CHARS = 20_000


@dataclass(frozen=True)
class TemplateInput:
    label: str
    required: bool


def inputs_of(body: str) -> list[TemplateInput]:
    """The body's inputs in order of first use, one per label; a label that is required anywhere is required."""
    found: dict[str, bool] = {}
    for match in INPUT_TOKEN.finditer(body or ""):
        label = " ".join(match.group(2).split())
        found[label] = found.get(label, False) or match.group(1) is None
    return [TemplateInput(label, required) for label, required in found.items()]


class MissingInputs(ValueError):
    def __init__(self, labels: list[str]) -> None:
        super().__init__(f"needs a value for {', '.join(repr(label) for label in labels)}")
        self.labels = labels


def fill_inputs(body: str, values: Mapping[str, str]) -> str:
    """`body` with every input token replaced by its value (stripped). Raises `MissingInputs` naming each required
    input left blank; an optional one left blank reads `INPUT_NOT_GIVEN`."""
    given = {" ".join(label.split()): (value or "").strip() for label, value in values.items()}
    missing = [i.label for i in inputs_of(body) if i.required and not given.get(i.label)]
    if missing:
        raise MissingInputs(missing)
    return INPUT_TOKEN.sub(lambda m: given.get(" ".join(m.group(2).split())) or INPUT_NOT_GIVEN, body)


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
