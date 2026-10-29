"""The merge lane: its rows, gate certificates and recorded flakes."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, _in_check, org_unique, OrgScoped, timestamp_column, UuidPkMixin


# ── The merge lane (revision 0409; `app/merge_lane.py` is the single home for every rule over these) ──────
#: `merge_lane_rows.state` — the closed vocabulary; the legal moves are `merge_lane.TRANSITIONS`.
MERGE_LANE_ROW_STATES = ("queued", "testing", "merged", "failed", "superseded", "closed")


class MergeLaneRow(UuidPkMixin, OrgScoped, Base):
    """One runner PR in a project's merge-lane queue, pinned to one head SHA. A new head supersedes the row
    and opens a fresh one; at most one open (`queued`/`testing`) row per (project, PR)."""

    __tablename__ = "merge_lane_rows"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("state", MERGE_LANE_ROW_STATES, "ck_merge_lane_rows_state"),
        Index(
            "uq_merge_lane_open", "project_id", "pr_number", unique=True,
            postgresql_where=text("state IN ('queued', 'testing')"),
        ),
        Index("ix_merge_lane_rows_project_state", "project_id", "state"),
        Index("ix_merge_lane_rows_lane_task_id", "lane_task_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_merge_lane_rows_project_id"), nullable=False
    )
    # The runner's task that reported the PR.
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE", name="fk_merge_lane_rows_task_id"), nullable=False
    )
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_url: Mapped[str] = mapped_column(Text, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    base_branch: Mapped[str] = mapped_column(Text, nullable=False)
    # `git_access.git_person_for_task` at report time: whose forge login merges this PR (L5). NULL is never
    # batched; the row is born `failed` with `failure.kind = "no_git_person"`.
    git_person_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    lane_task_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL", name="fk_merge_lane_rows_lane_task_id"), nullable=True
    )
    batch_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    recheck_after: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The red integration-branch commit this row was last requeued waiting on (`requeue` with `develop_sha`):
    # the lane runs no gate for a batch of rows all waiting on the commit the branch still points at.
    waiting_on: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    merge_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # When the `merged` event landed: the integration branch moved then (`merge_lane._base_check_superseded`).
    # `updated_at` cannot say so — any later write to the row moves it. NULL before the column existed.
    merged_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # `{kind, tests: [str], excerpt: str}`, `kind` one of `merge_lane.FAILURE_KINDS`.
    failure: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # Why the row last moved, one reader-facing line (`merge_lane.py`'s `_note_*`): a re-check, a requeue, a
    # superseding head, a close. NULL once it merges or fails (`failure` explains that) or before any move.
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The `merge_queue.event` idempotency keys already applied to this row: `"<event>"` strings.
    applied_events: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class MergeLaneCertificate(UuidPkMixin, OrgScoped, Base):
    """A tree the full gate passed on, for one gate command: (project, tree hash, gate_cmd_hash) -> green."""

    __tablename__ = "merge_lane_certificates"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint(
            "project_id", "tree_hash", "gate_cmd_hash", name="uq_merge_lane_certificates_project_tree_gate"
        ),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_merge_lane_certificates_project_id"), nullable=False
    )
    tree_hash: Mapped[str] = mapped_column(String(40), nullable=False)
    gate_cmd_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    develop_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    lane_task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    pr_numbers: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, server_default=text("'{}'::int[]"))
    created_at: Mapped[datetime] = timestamp_column()


class MergeLaneFlake(UuidPkMixin, OrgScoped, Base):
    """One flaky test observed by the lane (red once, green on the rerun of the same tree)."""

    __tablename__ = "merge_lane_flakes"
    __table_args__ = (
        org_unique(__tablename__),
        Index("ix_merge_lane_flakes_project_test", "project_id", "test_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_merge_lane_flakes_project_id"), nullable=False
    )
    test_id: Mapped[str] = mapped_column(Text, nullable=False)
    tree_hash: Mapped[str] = mapped_column(String(40), nullable=False)
    lane_task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    fix_task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
