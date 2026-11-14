"""A freeze override (R7.5): a person who may press a step AND may edit the project's pipeline runs that one request
through a freeze (Freeze now included, D-11) by typing a reason. The ONE home of its rules, for both seams:

- a Merge press (`app/pipeline/actions.py`): stored on the press row (`pipeline_approvals.freeze_override`), read by
  `app/pipeline/gate.py::merge_freeze`;
- a release request or retry (`app/releases/service.py`): stored on the release (`releases.freeze_override`), read by
  `service.release_freeze`.

The same right and reason gate an UNTESTED release (`record_untested`): a promote that skips the full gate.

Each override writes one audit row (`freeze_override`, `action_type="write"`: the Audit page's default filter).
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import user_activity
from app.authz.principal import Principal
from app.authz.visibility import has_project_key
from app.clock import utcnow
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.releases import Release, ReleaseStage
from app.releases.access import PROJECTS_EDIT

#: The shortest reason an override takes.
REASON_MIN = 10
#: The longest reason stored (the request bodies cap it too).
REASON_MAX = 500
#: How the audit row and history name Freeze now, which has no name of its own.
FREEZE_NOW_NAME = "Freeze now"


class OverrideRefused(Exception):
    """An override the caller may not make, or a reason too short: the status and body the route renders."""

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(body.get("code"))
        self.status = status
        self.body = body


def reason_of(principal: Optional[Principal], project_id: int, reason: Optional[str], *, may_press: bool,
              field: str = "override_reason") -> Optional[str]:
    """The trimmed reason of an override the caller asks for, or None when none is asked (`reason` absent). A caller
    who may not press the step or not edit the project's pipeline is 403 `override_forbidden`; then a reason under
    `REASON_MIN` characters is 422 `reason_short`."""
    if reason is None:
        return None
    if principal is None or principal.user_id is None or not may_press \
            or not has_project_key(principal, PROJECTS_EDIT, project_id):
        raise OverrideRefused(403, {"code": "override_forbidden"})
    # Control and invisible format characters count for nothing: a reason must be readable.
    text = "".join(ch for ch in reason if unicodedata.category(ch) not in ("Cc", "Cf") or ch == "\n").strip()
    if len(text) < REASON_MIN:
        raise OverrideRefused(422, {"code": "reason_short", "field": field, "min": REASON_MIN})
    return text[:REASON_MAX]


def covers(stored: Optional[dict[str, Any]], hit: Any) -> bool:
    """Whether the stored override `stored` passes the freeze `hit` (`service.FreezeHit`): only the freeze it was
    given for (Freeze now as itself), and only the occurrence it was given in — a later window of a repeating
    freeze, a Freeze now switched on again, or another freeze needs an override of its own."""
    if not stored or hit is None:
        return False
    if hit.freeze_now:
        same = stored.get("freeze_name") == FREEZE_NOW_NAME and stored.get("freeze_id") is None
    else:
        same = stored.get("freeze_id") == (str(hit.freeze_id) if hit.freeze_id is not None else None)
    if not same:
        return False
    try:
        given = datetime.fromisoformat(str(stored.get("at")))
    except ValueError:
        return False
    return hit.starts is None or given >= hit.starts


def untested_of(release: Release) -> Optional[dict[str, Any]]:
    """The person's decision to ship `release` without the full gate (`inputs.untested`: `{by_id, at, reason}`), or
    None. Read by the release's wire (pin the head), the merge lane's release batch and CI's certificate question
    (skip the suite)."""
    untested = (release.inputs or {}).get("untested")
    return untested if isinstance(untested, dict) and untested.get("reason") else None


def record_untested(session: Session, project: Project, stage: ReleaseStage, principal: Principal, reason: str,
                    release_id: int) -> dict[str, Any]:
    """An untested release (`service.deploy`'s `untested_reason`): a promote that ships the source branch's head and
    skips the full gate the merge lane and CI would run on it. Checked as an override (`reason_of`); one audit row
    (`release_untested`); returns the stored value (`{by_id, at, reason}`, the release's `inputs.untested`)."""
    stored = {"by_id": str(principal.user_id), "at": utcnow().isoformat(), "reason": reason}
    user = session.scalar(select(AdminUser).where(AdminUser.id == UUID(str(principal.user_id))))
    user_activity.record(
        session, user, "write", "release_untested", f"Release #{release_id} to {stage.name} without tests",
        path=f"/api/projects/{project.id}/release/deploy", method="POST",
        data={"project_id": project.id, "stage_id": str(stage.id), "release_id": release_id, "reason": reason})
    return stored


def record(session: Session, project: Project, stage: ReleaseStage, principal: Principal, reason: str,
           hit: Any, *, release_id: Optional[int] = None, task_id: Optional[int] = None) -> dict[str, Any]:
    """The stored override of one request through `hit` (`service.FreezeHit`), and its audit row; returns the
    stored value (`{by_id, at, reason, freeze_id, freeze_name}`)."""
    name = FREEZE_NOW_NAME if hit.freeze_now else hit.name
    stored = {"by_id": str(principal.user_id), "at": utcnow().isoformat(), "reason": reason,
              "freeze_id": str(hit.freeze_id) if hit.freeze_id is not None else None, "freeze_name": name}
    subject = f"Release #{release_id} to {stage.name}" if release_id is not None \
        else f"Merge of task #{task_id} into {stage.name}"
    user = session.scalar(select(AdminUser).where(AdminUser.id == UUID(str(principal.user_id))))
    user_activity.record(
        session, user, "write", "freeze_override", f"Override freeze “{name}” · {subject}",
        path=f"/api/projects/{project.id}/pipeline/steps/{stage.id}", method="POST",
        data={"project_id": project.id, "stage_id": str(stage.id), "release_id": release_id, "task_id": task_id,
              "freeze_id": stored["freeze_id"], "freeze_name": name, "reason": reason})
    return stored
