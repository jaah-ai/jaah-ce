"""The outcome scan's interval, resolved in one place: project override → org override → operator default.

The operator default is the `outcome_scan_interval_minutes` knob (`app/operator_settings/catalog_tasks.py`, 60 min,
floor 15); the org's Owner/Admin sets `orgs.outcome_scan_interval_minutes`, a project its own
`projects.outcome_scan_interval_minutes` (both NULL = inherit, 15..1440 by their check constraints).
"""
from __future__ import annotations

from typing import Literal, NamedTuple, Optional

from sqlalchemy.orm import Session

from app.models.orgs import Org
from app.models.projects import Project

KNOB = "outcome_scan_interval_minutes"
#: The bounds every override is checked against (the columns' check constraints say the same).
MINIMUM = 15
MAXIMUM = 1440

Source = Literal["project", "org", "global"]


class Interval(NamedTuple):
    minutes: int
    source: Source


def global_interval() -> int:
    from app.operator_settings import reads as settings_reads

    return settings_reads.get_int(KNOB)


def resolve(project_minutes: Optional[int], org_minutes: Optional[int]) -> Interval:
    """The pure resolution: the first override set wins, else the operator default."""
    if project_minutes is not None:
        return Interval(int(project_minutes), "project")
    if org_minutes is not None:
        return Interval(int(org_minutes), "org")
    return Interval(global_interval(), "global")


def scan_interval(session: Session, project: Project) -> Interval:
    """`project`'s scan interval in minutes and where it came from."""
    org = session.get(Org, project.org_id)
    return resolve(project.outcome_scan_interval_minutes, org.outcome_scan_interval_minutes if org else None)


def check(minutes: Optional[int]) -> Optional[str]:
    """Why `minutes` is not a valid override, or None (NULL — inherit — is always valid)."""
    if minutes is None:
        return None
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not MINIMUM <= minutes <= MAXIMUM:
        return f"The scan interval must be between {MINIMUM} and {MAXIMUM} minutes."
    return None
