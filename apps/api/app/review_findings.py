"""The review seams shared by intelligent routing and the outcome ledger: a task's declared review tier and the findings
its review lanes reported.

* `declare_tier` — THE ONE SETTER of `tasks.review_tier`. A coder runner declares it on `pull_request_enqueue` and on
  its settle (`task_complete`/`task_fail`); the latest declaration wins. Every declaration fires each hook registered
  with `on_tier_declared` once.
* `record` — THE ONE WRITER of `review_findings` (`app/models/review.py`).

Advisory only (decision D2): nothing here blocks, delays or gates a PR, the merge lane or a settle, and no reader of a
finding decides anything. Nothing here commits.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Literal, Optional
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_router import decisions
from app.entity_shape import StrictModel
from app.models.review import ReviewFinding
from app.models.routing import FINAL, RoutingDecision
from app.models.tasks import REVIEW_TIERS, Task

log = logging.getLogger(__name__)

ReviewTier = Literal["trivial", "minor", "standard", "major", "security"]
assert REVIEW_TIERS == ReviewTier.__args__, "ReviewTier must spell the column's closed set"  # type: ignore[attr-defined]

#: The most findings one call records; the rest are dropped.
MAX_FINDINGS = 200
#: The most findings one task holds across its attempts; past it, new ones are dropped.
MAX_FINDINGS_PER_TASK = 1000


class FindingIn(StrictModel):
    """One finding as a runner reports it."""

    severity: Literal["blocker", "major", "minor", "info"]
    category: Literal["security", "correctness", "test"]
    file: Optional[str] = Field(default=None, max_length=1000)
    line: Optional[int] = Field(default=None, ge=1, le=2**31 - 1)
    title: str = Field(min_length=1, max_length=500)
    detail: str = Field(default="", max_length=20_000)


# ── The review tier ─────────────────────────────────────────────────────────────────────────────────────

TierHook = Callable[[Session, Task, str], None]
_TIER_HOOKS: list[TierHook] = []


def on_tier_declared(fn: TierHook) -> TierHook:
    """Register `fn(session, task, tier)` to run on every declaration, after the column is set. Usable as a decorator.
    A hook that raises is logged and skipped: a declaration never fails its call."""
    _TIER_HOOKS.append(fn)
    return fn


def declare_tier(session: Session, task: Task, tier: Optional[str]) -> None:
    """Record `tier` as `task`'s review tier and fire every hook once. `None` (nothing declared) changes nothing and
    fires nothing. `tier` is already validated (`ReviewTier` on the wire)."""
    if tier is None:
        return
    if tier not in REVIEW_TIERS:
        raise ValueError(f"review tier {tier!r} is not one of {REVIEW_TIERS}")
    task.review_tier = tier
    session.flush()
    for hook in list(_TIER_HOOKS):
        try:
            with session.begin_nested():
                hook(session, task, tier)
        except Exception:  # noqa: BLE001 — a hook never fails the runner's call (advisory, D2)
            log.exception("review tier hook %s failed for task %s", getattr(hook, "__name__", hook), task.id)


# ── Findings ───────────────────────────────────────────────────────────────────────────────────────────

#: The lane an in-session finding came from, by its category: the authoring session's own review lanes.
IN_SESSION_LANE: dict[str, str] = {
    "security": "in_session_security_reviewer",
    "correctness": "in_session_reviewer",
    "test": "in_session_verifier",
}


def authoring_decision_id(session: Session, task: Task) -> Optional[UUID]:
    """The routing decision of `task`'s latest claimed dispatch, or None when none was recorded."""
    return session.scalars(
        select(RoutingDecision.id)
        .where(RoutingDecision.task_id == task.id, RoutingDecision.status == FINAL)
        .order_by(RoutingDecision.finalized_at.desc().nulls_last())
        .limit(1)
    ).first()


def record(
    session: Session,
    task: Task,
    findings: list[FindingIn],
    *,
    lane: str,
    detecting_task: Optional[Task] = None,
    detecting_config: dict[str, Any],
    routing_decision_id: Optional[UUID] = None,
    head_sha: Optional[str] = None,
) -> list[UUID]:
    """Write one `review_findings` row per finding against `task`, the AUTHORING task. The authoring configuration is
    what ran `task` (`decisions.actual_config`). Returns the new ids, in order. Flushes."""
    authoring_config = decisions.actual_config(task)
    rows = [
        ReviewFinding(
            org_id=task.org_id, task_id=task.id, detecting_task_id=detecting_task.id if detecting_task else None,
            detecting_lane=lane, detecting_config=detecting_config, authoring_config=authoring_config,
            routing_decision_id=routing_decision_id, severity=f.severity, category=f.category, file=f.file,
            line=f.line, title=f.title, detail=f.detail, head_sha=head_sha,
        )
        for f in findings
    ]
    session.add_all(rows)
    session.flush()
    return [row.id for row in rows]


def parse_findings(raw: Any) -> list[FindingIn]:
    """The well-formed findings of a runner's raw `findings`, at most `MAX_FINDINGS`. Lenient by rule (advisory, D2):
    a malformed entry is dropped and logged, never a refusal of the call carrying it."""
    if not isinstance(raw, list):
        return []
    parsed: list[FindingIn] = []
    for item in raw[:MAX_FINDINGS]:
        try:
            parsed.append(item if isinstance(item, FindingIn) else FindingIn.model_validate(item))
        except ValidationError as exc:
            log.info("review: dropped a malformed finding (%d error(s))", exc.error_count())
    return parsed


def malformed_count(raw: Any, parsed: int) -> int:
    """How many of a runner's raw `findings` entries `parse_findings` dropped as malformed (`parsed` kept). A value
    that is not a list at all counts as one; entries past `MAX_FINDINGS` are a cap, not malformed."""
    if raw is None:
        return 0
    if not isinstance(raw, list):
        return 1
    return max(0, min(len(raw), MAX_FINDINGS) - parsed)


def security_by_lane(session: Session, ids: list[UUID]) -> dict[str, int]:
    """Of the findings `ids`, how many are `security` findings, by detecting lane."""
    if not ids:
        return {}
    return {
        lane: int(n) for lane, n in session.execute(
            select(ReviewFinding.detecting_lane, func.count())
            .where(ReviewFinding.id.in_(ids), ReviewFinding.category == "security")
            .group_by(ReviewFinding.detecting_lane)
        )
    }


def record_in_session(session: Session, task: Task, findings: list[FindingIn]) -> list[UUID]:
    """A runner's own settle: the findings its in-session lanes reported against its own change, each under the lane
    its category names (`IN_SESSION_LANE`). The detecting and authoring configuration are the same run's. Past
    `MAX_FINDINGS_PER_TASK` the rest are dropped."""
    held = session.scalar(select(func.count()).select_from(ReviewFinding).where(ReviewFinding.task_id == task.id)) or 0
    findings = findings[: max(0, MAX_FINDINGS_PER_TASK - held)]
    if not findings:
        return []
    config = decisions.actual_config(task)
    decision_id = authoring_decision_id(session, task)
    ids: list[UUID] = []
    for category, lane in IN_SESSION_LANE.items():
        group = [f for f in findings if f.category == category]
        if group:
            ids += record(session, task, group, lane=lane, detecting_task=task, detecting_config=config,
                          routing_decision_id=decision_id)
    return ids


def config_label(session: Session, cfg: Optional[dict[str, Any]]) -> str:
    """A Config in words: the runner's display name and the model family when a model is known, else the tier's label
    (a tier alone names no vendor's model)."""
    if not cfg:
        return "Unknown"
    if cfg.get("kind") == "ci_scan":  # A CI security scan (`outcome_ledger.security`): no agent ran it.
        return "CI scan"
    from app import edition_features, model_catalog
    from app.outcome_ledger import dimensions

    runner = decisions.describe(session, cfg)
    runner = runner[:1].upper() + runner[1:]
    family = dimensions.model_family(cfg.get("model"))
    if family:
        if edition_features.enabled("exec_dashboard"):
            from app.outcome_ledger import metrics
            return f"{runner} · {metrics.family_label(family)}"
    tier = next((choice.label for choice in model_catalog.MODELS if choice.value == cfg.get("tier")), None)
    return f"{runner} · {tier}" if tier else runner
