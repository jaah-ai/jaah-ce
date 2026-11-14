"""Which mode the agent router runs in for a project — THE SINGLE HOME of the mode rule.

* Shadow — measurement: the router writes its decision and grades it against the actual dispatch, and acts on nothing.
  Always the mode when the plan feature (`plan_terms.INTELLIGENT_ROUTING`) is off or the project inherits.
* Recommend — the task form and the dispatch plan show the choice and its reason, one click to accept.
* Auto — dispatch follows the choice inside the org's policy (`policy_settings.py`).

A project's mode (`Project.routing_mode`: `inherit` = off, `recommend`, `auto`) is capped at its org's maximum
(`Org.routing_max_mode`: `off`, `recommend`, `auto`). Both vocabularies are the models' (`ROUTING_MODES`,
`ROUTING_MAX_MODES`), ranked here in their order: lowering the org's maximum rewrites every project above it
(`demote_projects`), and raising it restores nothing.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import plan_terms
from app.models.orgs import ROUTING_MAX_MODES, Org
from app.models.projects import ROUTING_MODES, Project

#: The project mode that follows nothing: off.
INHERIT = "inherit"
#: The org maximum that allows nothing.
OFF = "off"
#: The audit kind of one project demoted by a lowered org maximum (`routes/orgs.py::update_org`).
DEMOTED = "routing.mode_demoted"


class Mode(StrEnum):
    SHADOW = "shadow"
    RECOMMEND = "recommend"
    AUTO = "auto"


def rank(mode: str) -> int:
    """`off`/`inherit` 0, `recommend` 1, `auto` 2 — either vocabulary. ValueError on anything else."""
    if mode in ROUTING_MODES:
        return ROUTING_MODES.index(mode)
    return ROUTING_MAX_MODES.index(mode)


def project_mode_for_rank(value: int) -> str:
    """The project mode at `value` (`rank`'s inverse): 0 is `inherit`."""
    return ROUTING_MODES[value]


def cap(session: Session, org: Org) -> str:
    """The highest mode a project of `org` may choose now: the org's maximum while the plan feature is on, else
    `off`."""
    return org.routing_max_mode if plan_terms.intelligent_routing(session, org) else OFF


def effective_mode(session: Session, org: Org, project: Project) -> Mode:
    """The mode the router runs in for `project`: Shadow without the plan feature or with the project inheriting,
    else the lower of the project's mode and the org's maximum."""
    if project.routing_mode == INHERIT or not plan_terms.intelligent_routing(session, org):
        return Mode.SHADOW
    level = min(rank(project.routing_mode), rank(org.routing_max_mode))
    return Mode.SHADOW if level == 0 else Mode(project_mode_for_rank(level))


def mode_basis(session: Session, org: Org, project: Project) -> dict[str, Any]:
    """What `effective_mode` read, for a decision record or a reason line."""
    return {
        "plan_feature": plan_terms.intelligent_routing(session, org),
        "org_max": org.routing_max_mode,
        "project_mode": project.routing_mode,
    }


def refusal(session: Session, org: Org, mode: str) -> Optional[str]:
    """Why a project of `org` may not choose `mode` now, as a sentence; None when it may."""
    if rank(mode) == 0:
        return None
    if not plan_terms.intelligent_routing(session, org):
        return NOT_ON_PLAN
    if rank(mode) > rank(org.routing_max_mode):
        return ABOVE_MAX.format(max=MAX_WORDS[org.routing_max_mode])
    return None


#: The refusals `refusal` gives, spelled once for the routes, the tests and the console.
NOT_ON_PLAN = "Intelligent routing isn't included in your plan."
ABOVE_MAX = "Your organization allows routing up to {max}. An Admin can raise it in Settings › Organizations."
#: The org maximum in words, for `ABOVE_MAX`.
MAX_WORDS = {"off": "Off", "recommend": "Recommend", "auto": "Auto"}


def demote_projects(session: Session, org_id: UUID, new_max: str) -> list[tuple[Project, str]]:
    """Lower every project of `org_id` above `new_max` to it (`off` → `inherit`), locked; `[(project, its mode
    before)]`. The caller audits each and commits, in the transaction that wrote `new_max`."""
    ceiling = rank(new_max)
    above = [mode for mode in ROUTING_MODES if rank(mode) > ceiling]
    if not above:
        return []
    rows = session.scalars(
        select(Project).where(Project.org_id == org_id, Project.routing_mode.in_(above)).with_for_update()
    ).all()
    demoted = []
    for project in rows:
        before = project.routing_mode
        project.routing_mode = project_mode_for_rank(ceiling)
        demoted.append((project, before))
    return demoted
