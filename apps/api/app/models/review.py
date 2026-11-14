"""Review findings: one `review_findings` row per finding a review lane reported against a task's change.

`app/review_findings.py::record` is the only writer. Advisory only: no PR, lane or settle reads them. A CP-written
system record, so no entity shape.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _in_check, OrgScoped, timestamp_column, UuidPkMixin

#: Who found it: a blind lane (its own task), a lane inside the authoring session, or a CI scan.
LANES = (
    "blind_reviewer", "blind_security_reviewer", "blind_tester", "in_session_reviewer",
    "in_session_security_reviewer", "in_session_verifier", "ci_scan",
)
SEVERITIES = ("blocker", "major", "minor", "info")
CATEGORIES = ("security", "correctness", "test")


class ReviewFinding(UuidPkMixin, OrgScoped, Base):
    """One finding: severity, category, where (file:line) and what, who detected it on which configuration, and the
    configuration that authored the change. `task_id` is the AUTHORING task; `detecting_task_id` the task that
    reported it (the authoring task itself for an in-session lane)."""

    __tablename__ = "review_findings"
    __table_args__ = (
        _in_check("detecting_lane", LANES, "ck_review_findings_detecting_lane"),
        _in_check("severity", SEVERITIES, "ck_review_findings_severity"),
        _in_check("category", CATEGORIES, "ck_review_findings_category"),
        Index("ix_review_findings_task_id", "task_id"),
        Index("ix_review_findings_org_created", "org_id", "created_at"),
    )

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_review_findings_task_id"), nullable=False
    )
    detecting_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", name="fk_review_findings_detecting_task_id"), nullable=True
    )
    detecting_lane: Mapped[str] = mapped_column(Text, nullable=False)
    #: Configs (`agent_router/decisions.py::config`): what ran the detecting lane, and what authored the change.
    detecting_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    authoring_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The authoring dispatch's routing decision, when one was recorded.
    routing_decision_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("routing_decisions.id", ondelete="SET NULL", name="fk_review_findings_routing_decision_id"),
        nullable=True,
    )
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    file: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    head_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
