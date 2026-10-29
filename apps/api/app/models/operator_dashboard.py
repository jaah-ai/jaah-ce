"""Operator dashboard history: hourly ops snapshots, AI outcome reviews and weekly insight digests."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.ops_snapshots import OPS_SNAPSHOT_KINDS, OPS_SNAPSHOTS_TABLE
from app.operator_dashboard.vocabulary import (
    DIGEST_MAX_BULLETS,
    DIGEST_STATUSES,
    INSIGHT_DIGESTS_TABLE,
    OUTCOME_REVIEWS_TABLE,
)
from app.models.base import Base, org_unique, OrgScoped, timestamp_column, UuidPkMixin


class OutcomeReview(UuidPkMixin, OrgScoped, Base):
    """One AI quality review of a delivered task: a 1–5 score and a one-line reason (operator dashboard R8).

    `app/operator_dashboard/review.py` owns the one writer (the weekly `outcome-review` cron) and the vocabulary;
    one row per task, ever (`UNIQUE (task_id)`), so a task is never reviewed twice. `score` is NULL only for a
    review the model answered invalidly twice: the row keeps the spend (`usage_by_model`, counted in the
    dashboard's system AI cost) and `reason` says why it was skipped. `run_id` is the cron firing's. Machine-written,
    so no entity shape."""

    __tablename__ = OUTCOME_REVIEWS_TABLE
    __table_args__ = (
        CheckConstraint("score BETWEEN 1 AND 5", name="ck_outcome_reviews_score"),
        CheckConstraint("char_length(reason) <= 200", name="ck_outcome_reviews_reason"),
        UniqueConstraint("task_id", name="uq_outcome_reviews_task_id"),
        # The dashboard's window read.
        Index("ix_outcome_reviews_created_at", "created_at"),
    )

    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    score: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    usage_by_model: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class InsightDigest(UuidPkMixin, OrgScoped, Base):
    """One week's AI-written digest of what changed on the operator dashboard and why (R7b).

    `app/operator_dashboard/digest.py` owns the one writer (the weekly `insight-digest` cron) and the reader
    (`/api/operator/dashboard/digests`). One row per covered week (`week_start`, a Monday; the week is
    `[week_start, week_start + 7d)`). `bullets` is `[{text, metric, slice}]`, at most DIGEST_MAX_BULLETS, each
    text at most DIGEST_BULLET_MAX_CHARS, validated before it is stored. `failed` carries `error` and no bullets.
    `usage_by_model` is every call the week's runs paid for, counted in the dashboard's system AI cost. Operator
    content (it names tenants), so it lives in the Operator org. Machine-written, so no entity shape."""

    __tablename__ = INSIGHT_DIGESTS_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in DIGEST_STATUSES) + ")", name="ck_insight_digests_status"
        ),
        CheckConstraint(
            f"jsonb_typeof(bullets) = 'array' AND jsonb_array_length(bullets) <= {DIGEST_MAX_BULLETS}",
            name="ck_insight_digests_bullets",
        ),
        UniqueConstraint("week_start", name="uq_insight_digests_week_start"),
        # The dashboard's cost read, by generation day.
        Index("ix_insight_digests_generated_at", "generated_at"),
    )

    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = timestamp_column()
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bullets: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    usage_by_model: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class OpsSnapshot(OrgScoped, Base):
    """One hourly snapshot of one point-in-time series (node utilisation, worktree occupancy, account quota, wallet
    floor) for one subject — the operator dashboard's year of history behind sources that keep none.

    `app/ops_snapshots.py` owns the vocabulary, the `values` shapes and the one writer (the `ops-snapshot` cron).
    `taken_at` is the hour. The subject columns carry no FK on purpose: a retired node's or a deleted account's
    past is still fleet history, and the retention sweep (365 days) is what bounds the table. `org_id` is the
    subject's own org. Machine-written, so no entity shape."""

    __tablename__ = OPS_SNAPSHOTS_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in OPS_SNAPSHOT_KINDS) + ")",
            name="ck_ops_snapshots_kind",
        ),
        # The idempotency (one row per kind, subject and hour) AND the dashboard's `(kind, taken_at)` range read,
        # which its leading columns serve. NULLS NOT DISTINCT: each kind leaves some subject columns NULL.
        Index(
            "uq_ops_snapshots_subject_hour", "kind", "taken_at", "node_id", "account_id", "project_id", "org_id",
            unique=True, postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    taken_at: Mapped[datetime] = timestamp_column(server_now=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    account_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    project_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
