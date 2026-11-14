"""A step's rules (the settings of the step INTO a stage, on that stage's row): the single reader of the
`release_stages` rule columns. `StepRules.of(stage)` works on a stored stage and on a release's snapshot
(`app/releases/snapshot.py::rule`), so a release is judged by the rules it was requested under.

Every default is today's behaviour: approvers the same as who may press, the requester may not approve, no timeout,
no instructions, no soak, any hours, newest wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from app.models.releases import ReleaseStage
from app.releases import hours as hours_mod

CANCEL, REMIND = "cancel", "remind"
NEWEST = "newest"


@dataclass(frozen=True)
class StepRules:
    approvals_required: int
    #: None = same as who may press (`deployers`, or the action's default).
    approvers: Optional[dict[str, list[str]]]
    requester_cant_approve: bool
    approval_timeout_minutes: Optional[int]
    on_timeout: str
    instructions: str
    soak_minutes: int
    hours: Optional[hours_mod.Hours]
    concurrency: str
    resumed_at: Optional[datetime]

    @classmethod
    def of(cls, stage: ReleaseStage) -> "StepRules":
        raw_hours = stage.allowed_hours
        return cls(
            approvals_required=int(stage.approvals_required or 0),
            approvers=_who(stage.approvers),
            requester_cant_approve=stage.requester_cant_approve is not False,
            approval_timeout_minutes=stage.approval_timeout_minutes,
            on_timeout=stage.on_timeout or CANCEL,
            instructions=stage.approver_instructions or "",
            soak_minutes=int(stage.soak_minutes or 0),
            hours=hours_mod.parse(raw_hours) if raw_hours else None,
            concurrency=stage.concurrency or NEWEST,
            resumed_at=stage.resumed_at,
        )


def _who(value: Any) -> Optional[dict[str, list[str]]]:
    if value is None:
        return None
    return {"roles": [str(r) for r in value.get("roles") or []], "users": [str(u) for u in value.get("users") or []]}

