"""The behaviour switch: the plan switch with its org override, AND the project's opt-in (package docstring)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app import plan_terms
from app.models.projects import Project

GateReason = Literal["production_qa_off", "project_not_opted_in"]

#: The 409 a project opt-in answers while its org's switch is off (`routes/projects.py`), and the console's twin
#: of its first sentence (`ProjectProductionQaTab.tsx` says "Off for this org").
OFF_FOR_ORG = "Production QA is off for this organization. An operator turns it on in the org's Overrides."


@dataclass(frozen=True)
class GateState:
    enabled: bool
    reason: Optional[GateReason]


def org_on(session: Session, org_id: Optional[UUID]) -> bool:
    """The org's `production_qa` plan switch (`plan_terms.switch_on`: the override, else the plan chain, else the
    default — off). A project with no org reads `(default)`'s value."""
    return plan_terms.switch_on(session, org_id, plan_terms.PRODUCTION_QA)


def orgs_on(session: Session, org_ids: Iterable[Optional[UUID]]) -> frozenset[UUID]:
    """Which of these orgs have the switch on: one `org_on` per distinct org (a project page's projection)."""
    return frozenset(org_id for org_id in {o for o in org_ids if o is not None} if org_on(session, org_id))


def state(session: Session, project: Project) -> GateState:
    """Whether Production QA behaviour runs for `project`: the org's switch first, then the project's opt-in."""
    if not org_on(session, project.org_id):
        return GateState(enabled=False, reason="production_qa_off")
    if not project.production_qa_enabled:
        return GateState(enabled=False, reason="project_not_opted_in")
    return GateState(enabled=True, reason=None)
