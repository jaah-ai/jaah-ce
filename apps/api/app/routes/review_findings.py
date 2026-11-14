"""A task's review findings (`app/review_findings.py`): what every review lane found in its change, for the Task page's
Details › Review subtab. Read only; advisory (nothing reads a finding to decide anything)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app import review_findings
from app.auth import require_user
from app.authz.declarations import feature
from app.db_async import on_session
from app.models.identity import AdminUser
from app.models.review import SEVERITIES, ReviewFinding
from app.task_lookup import get_task_or_404

router = APIRouter(tags=["review-findings"])


class ReviewFindingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    severity: str
    category: str
    file: Optional[str]
    line: Optional[int]
    title: str
    detail: str
    detecting_lane: str
    detecting_task_id: Optional[int]
    #: Display names (`review_findings.config_label`), never the stored Config.
    detecting_config_label: str
    authoring_config_label: str
    head_sha: Optional[str]
    created_at: datetime


class TaskReviewFindingsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[ReviewFindingOut]


@router.get("/api/tasks/{task_id}/review-findings", response_model=TaskReviewFindingsOut)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
@on_session
def get_task_review_findings(
    session: Session,
    task_id: int,
    _viewer: AdminUser = Depends(require_user),
) -> dict[str, Any]:
    """The findings against this task's change, most severe first, then oldest first."""
    task = get_task_or_404(task_id, session)
    rank = case({severity: index for index, severity in enumerate(SEVERITIES)}, value=ReviewFinding.severity)
    rows = list(session.scalars(
        select(ReviewFinding).where(ReviewFinding.task_id == task.id, ReviewFinding.org_id == task.org_id)
        .order_by(rank, ReviewFinding.created_at, ReviewFinding.id)
    ))
    labels: dict[str, str] = {}

    def label(cfg: dict[str, Any]) -> str:
        key = repr(sorted((k, str(v)) for k, v in (cfg or {}).items()))
        if key not in labels:
            labels[key] = review_findings.config_label(session, cfg)
        return labels[key]

    return {"rows": [
        {
            "id": row.id, "severity": row.severity, "category": row.category, "file": row.file, "line": row.line,
            "title": row.title, "detail": row.detail, "detecting_lane": row.detecting_lane,
            "detecting_task_id": row.detecting_task_id, "detecting_config_label": label(row.detecting_config),
            "authoring_config_label": label(row.authoring_config), "head_sha": row.head_sha,
            "created_at": row.created_at,
        }
        for row in rows
    ]}
