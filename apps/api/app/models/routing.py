"""The agent router's decision record: one `routing_decisions` row per dispatch (`app/agent_router/`).

`app/agent_router/decisions.py` is the only writer; the row's lifecycle is `pending` (computed for the next attempt,
recomputed while its inputs change) → `locked` (at the lease, with its `dispatch_id`) → `final` (at the claim), or
`superseded`. At most one live (`pending`/`locked`) row per task. A CP-written system record, so no entity shape.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _in_check, OrgScoped, timestamp_column, UuidPkMixin

PENDING, LOCKED, FINAL, SUPERSEDED = "pending", "locked", "final", "superseded"
STATUSES = (PENDING, LOCKED, FINAL, SUPERSEDED)
#: The statuses of the one row a task's next dispatch reads.
LIVE_STATUSES = (PENDING, LOCKED)
MODES = ("shadow", "recommend", "auto")
USED = ("router", "default")
#: Why `used` is `default` — the closed set `agent_router/policy.py` and `decisions.finalize` write.
FALLBACK_REASONS = (
    "allowed_set_one", "data_below_minimum", "mode_off", "human_pinned", "system_pinned", "playbook",
    "vendor_bound", "persona_not_routed", "candidate_unavailable", "no_pick",
)
RECOMMEND_RESPONSES = ("accepted", "overridden", "no_response")


class RoutingDecision(UuidPkMixin, OrgScoped, Base):
    """One dispatch's routing decision: the candidates, the router's own pick, the precedence chain's pick, which
    one ran and why, in a sentence. `id` is the stable reference the outcome ledger keeps
    (`task_outcomes.routing_decision_id`).

    `attempt` is the task's `dispatch_attempts` the claim bumped to. It is a label, not a key: the counter is
    refunded and reset, so the same number recurs; the live row is found by status."""

    __tablename__ = "routing_decisions"
    __table_args__ = (
        _in_check("status", STATUSES, "ck_routing_decisions_status"),
        _in_check("mode", MODES, "ck_routing_decisions_mode"),
        _in_check("used", USED, "ck_routing_decisions_used"),
        _in_check("fallback_reason", FALLBACK_REASONS, "ck_routing_decisions_fallback_reason"),
        _in_check("recommend_response", RECOMMEND_RESPONSES, "ck_routing_decisions_recommend_response"),
        Index("uq_routing_decisions_live_task", "task_id", unique=True,
              postgresql_where=text("status IN ('pending', 'locked')")),
        Index("ix_routing_decisions_org_finalized", "org_id", "finalized_at"),
        Index("ix_routing_decisions_project_finalized", "project_id", "finalized_at"),
        Index("ix_routing_decisions_task_id", "task_id"),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE", name="fk_routing_decisions_project_id"), nullable=False
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_routing_decisions_task_id"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=PENDING)
    #: What the pick was computed from (`decisions.inputs_hash`); a pending row is recomputed when it moves.
    inputs_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: A pick a person accepted: never recomputed.
    frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: Set at lock: the worktree's `worktrees.dispatch_id`, or the MicroVM's id.
    dispatch_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    mode_basis: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    #: A Config (`agent_router/decisions.py::config`): the router's own choice, always, even when not used.
    router_pick: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    default_pick: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: What ran, set at final; `model` filled when the dispatched model is stamped.
    actual: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    used: Mapped[str] = mapped_column(Text, nullable=False)
    fallback_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    exploratory: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    exploration_capped: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    expected_cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    recommend_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    human_pick: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    #: One or two full sentences a person reads; display names only.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: The ledger statistics the pick read; `none` until they exist.
    stats_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    locked_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    finalized_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
