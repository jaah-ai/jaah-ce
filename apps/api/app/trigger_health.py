"""A trigger's health — the derived badge, the counter rules, and the seam the health-signals child fills.

Health is derived on read from columns on the trigger row, never stored, the way cron health is
(`routes/crons.py::_health`). `apply_outcome` is the single home of the counter table (§3.9 rows 1-5);
`fold_filed_outcomes` its last two rows (a filed Task's terminal outcome), `fold_budget` the advisory budget
(§6, D7 — nothing here ever stops a run), `idle_due` the days rule (R13), and `on_failed` the owner's notice
and email (`app/trigger_notices.py`). The processor calls the folds at the start of each trigger's pass.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.triggers import (
    DEDUPED,
    DISPATCHED,
    FAILED,
    FILTERED,
    HEALTH_ACTIVE,
    HEALTH_DEGRADED,
    HEALTH_FAILED,
    HEALTH_PAUSED,
)

# Outcomes that prove the trigger works: they reset the failure streak and stamp `last_success_at`.
SUCCESS_OUTCOMES = frozenset({DISPATCHED, FILTERED, DEDUPED})
# Outcomes that count toward auto-pause (preflight, render and filing failures).
FAILURE_OUTCOMES = frozenset({FAILED})


def _when(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def trigger_health(trigger, *, paused_by_name: Optional[str] = None) -> tuple[str, Optional[str]]:
    """(state, reason) for `trigger`; the first rule that applies wins (§6). `reason` is None only for Active.

    Pure: `paused_by_name` is the caller's lookup of `paused_by_id` (a read the list does in one query)."""
    if trigger.paused_at is not None:
        who = paused_by_name or "an admin"
        why = (trigger.paused_reason or "").strip() or "no reason given"
        return HEALTH_PAUSED, f"Paused by {who} at {_when(trigger.paused_at)}: {why}"
    if trigger.failed_at is not None:
        return HEALTH_FAILED, trigger.failed_reason or "Stopped by the system"
    if trigger.consecutive_failures > 0:
        noun = "failure" if trigger.consecutive_failures == 1 else "consecutive failures"
        last = trigger.last_failure_reason or "unknown"
        return HEALTH_DEGRADED, f"{trigger.consecutive_failures} {noun} — last: {last}"
    if trigger.budget_breach_task_id is not None:
        return HEALTH_DEGRADED, trigger.budget_breach_reason or (
            f"Budget exceeded on Task #{trigger.budget_breach_task_id}"
        )
    return HEALTH_ACTIVE, None


def is_enabled(trigger) -> bool:
    """Neither paused by a human nor failed by the system."""
    return trigger.paused_at is None and trigger.failed_at is None


def stopped_reason(trigger) -> Optional[str]:
    """The `skipped` reason a delivery to a stopped trigger is logged with, or None when it is enabled."""
    if trigger.paused_at is not None:
        return "trigger paused"
    if trigger.failed_at is not None:
        return f"trigger failed: {trigger.failed_reason or 'stopped by the system'}"
    return None


def apply_outcome(trigger, outcome: str, *, reason: Optional[str] = None, now: Optional[datetime] = None) -> None:
    """The §3.9 counter table, rows 1-5: a success resets the streak and stamps `last_success_at`; a failure
    adds one and records its reason; every other outcome (rate-capped, skipped, unmatched, queued) leaves the
    counters alone — so an invalid signature can never pause a trigger (A6)."""
    if outcome in SUCCESS_OUTCOMES:
        trigger.consecutive_failures = 0
        trigger.last_success_at = now or datetime.now(timezone.utc)
    elif outcome in FAILURE_OUTCOMES:
        trigger.consecutive_failures = (trigger.consecutive_failures or 0) + 1
        trigger.last_failure_reason = reason


def should_auto_pause(trigger) -> bool:
    """Whether the failure streak has reached the trigger's auto-pause threshold (NULL = off, D9)."""
    return (
        is_enabled(trigger)
        and trigger.auto_pause_failures is not None
        and trigger.consecutive_failures >= trigger.auto_pause_failures
    )


def mark_failed(
    session: Session, trigger, reason: str, *, now: Optional[datetime] = None, idle: bool = False
) -> None:
    """Stop `trigger` (system stop, A2) with `reason`, then the failure hook. Idempotent: an already-failed
    trigger keeps its first reason. `idle`: the days rule's stop, which the owner alone hears about."""
    if trigger.failed_at is not None:
        return
    trigger.failed_at = now or datetime.now(timezone.utc)
    trigger.failed_reason = reason
    on_failed(session, trigger, reason, idle=idle)


def auto_pause_if_due(session: Session, trigger, *, now: Optional[datetime] = None) -> bool:
    """Apply auto-pause after a counted failure; True when it fired."""
    if not should_auto_pause(trigger):
        return False
    reason = f"{trigger.consecutive_failures} consecutive failures (last: {trigger.last_failure_reason or 'unknown'})"
    mark_failed(session, trigger, reason, now=now)
    return True


def clear_stops(trigger, *, now: Optional[datetime] = None) -> None:
    """Resume (a human action): clears both stops and the streak, so a fresh delivery files (AC-12.2), and
    restarts the idle rule's clock (`resumed_at`)."""
    trigger.resumed_at = now or datetime.now(timezone.utc)
    trigger.paused_at = None
    trigger.paused_by_id = None
    trigger.paused_reason = None
    trigger.failed_at = None
    trigger.failed_reason = None
    trigger.consecutive_failures = 0
    trigger.last_failure_reason = None


# ── The health signals (§3.9's last two rows, §6) ──────────────────────────────────────────────────────

#: How many of a trigger's recent filed Tasks one pass prices against the budget (each costs a handful of reads).
MAX_BUDGET_TASKS = 20
#: A filed Task is priced while it runs, and this long after its last write: its cost lands after the runner exits.
BUDGET_LOOKBACK = timedelta(hours=24)


def usd(amount: Decimal) -> str:
    """`$0.42`; a sub-cent amount keeps four places so `$0.0042` does not read as `$0.00`."""
    if amount != 0 and abs(amount) < Decimal("0.01"):
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def budget_reason(task_id: int, cost: Decimal, budget: Decimal) -> str:
    """The Degraded reason a budget breach shows (§6)."""
    return f"budget exceeded on Task #{task_id} ({usd(cost)} of {usd(budget)})"


def idle_due(trigger, at: datetime) -> bool:
    """The days rule (R13, D6): the trigger is enabled, has `auto_pause_idle_days` set, and has had no successful
    run (or, never having had one, has existed) for more than that many days before `at` — counted from its last
    Resume too, so a resumed trigger gets its full window. Off when unset."""
    days = trigger.auto_pause_idle_days
    if days is None or not is_enabled(trigger):
        return False
    anchors = [a for a in (trigger.last_success_at or trigger.created_at, trigger.resumed_at) if a is not None]
    return bool(anchors) and max(anchors) < at - timedelta(days=days)


def idle_reason(trigger) -> str:
    days = trigger.auto_pause_idle_days
    return f"no successful run for {days} {'day' if days == 1 else 'days'}"


def on_failed(session: Session, trigger, reason: str, *, idle: bool = False) -> None:
    """The moment a trigger goes Failed: its owner gets a `trigger_failed` notice and an email (R13), and unless
    `idle`, the org's Owners and Admins an important one (`app/automation_alerts.py`)."""
    from app import trigger_notices

    trigger_notices.notify_failed(session, trigger, reason, idle=idle)


def _filed(trigger):
    from app.models.automations import TriggerDelivery
    from app.models.tasks import Task

    return (
        select(TriggerDelivery, Task)
        .join(Task, Task.id == TriggerDelivery.task_id)
        .where(TriggerDelivery.trigger_id == trigger.id, TriggerDelivery.state == DISPATCHED)
    )


def _cost(session: Session, task_id: int) -> Optional[Decimal]:
    """The filed Task's run cost in USD (its whole subtree, `task_run_metrics`), or None when it has none yet."""
    from app.task_run_metrics import compute_run_metrics

    metrics = compute_run_metrics(task_id, session)
    raw = metrics.get("cost_usd") if metrics else None
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _clear_breach(trigger) -> None:
    trigger.budget_breach_task_id = None
    trigger.budget_breach_reason = None


def fold_filed_outcomes(session: Session, trigger, *, now: Optional[datetime] = None) -> None:
    """§3.9's last two rows, once per filed Task (`outcome_counted`): `system_failed` adds one failure (and may
    auto-pause); `done` clears a budget breach when its cost is within budget and it is newer than the breach."""
    from app.task_lifecycle import DONE, SYSTEM_FAILED
    from app.models.automations import TriggerDelivery
    from app.models.tasks import Task

    rows = session.execute(
        _filed(trigger)
        .where(TriggerDelivery.outcome_counted.is_(False), Task.state.in_((DONE, SYSTEM_FAILED)))
        .order_by(TriggerDelivery.id)
    ).all()
    for delivery, task in rows:
        delivery.outcome_counted = True
        if task.state == SYSTEM_FAILED:
            trigger.consecutive_failures = (trigger.consecutive_failures or 0) + 1
            trigger.last_failure_reason = f"Task #{task.id} failed"
            auto_pause_if_due(session, trigger, now=now)
        elif (
            trigger.budget_breach_task_id is not None
            and trigger.budget_usd is not None
            and task.id > trigger.budget_breach_task_id
        ):
            cost = _cost(session, task.id)
            if cost is not None and cost <= Decimal(trigger.budget_usd):
                _clear_breach(trigger)


def fold_budget(session: Session, trigger, *, now: Optional[datetime] = None) -> None:
    """Price the trigger's recent filed Tasks (still running, or written to within the last 24 h) against
    `budget_usd` (§6). An over-budget Task flags its delivery (`budget_exceeded_usd`, which the Task detail
    shows) and, unless a NEWER Task already finished within budget, the trigger (Degraded). ADVISORY ONLY (D7):
    nothing here stops, pauses or fails a run. No budget set clears any breach."""
    from app.task_lifecycle import ACTIVE, DONE
    from app.models.tasks import Task

    if trigger.budget_usd is None:
        _clear_breach(trigger)
        return
    budget = Decimal(trigger.budget_usd)
    if trigger.budget_breach_task_id is not None:
        # A budget raised past the breaching Task's cost ends the breach, as it drops that Task's flag.
        breach_cost = _cost(session, trigger.budget_breach_task_id)
        if breach_cost is not None and breach_cost <= budget:
            _clear_breach(trigger)
    cutoff = (now or datetime.now(timezone.utc)) - BUDGET_LOOKBACK
    rows = session.execute(
        _filed(trigger)
        .where(or_(Task.state == ACTIVE, Task.updated_at >= cutoff))
        .order_by(Task.id.desc())
        .limit(MAX_BUDGET_TASKS)
    ).all()
    newest_over: Optional[tuple[int, Decimal]] = None
    newest_within_done = 0
    for delivery, task in rows:
        cost = _cost(session, task.id)
        if cost is None:
            continue
        if cost > budget:
            # Only new information re-flags the trigger: an old over-budget Task whose row was merely touched
            # (a comment, a rating) must not undo a later Task's recovery.
            fresh = delivery.budget_exceeded_usd is None or cost > delivery.budget_exceeded_usd
            delivery.budget_exceeded_usd = cost
            if (fresh or task.id == trigger.budget_breach_task_id) and (
                newest_over is None or task.id > newest_over[0]
            ):
                newest_over = (task.id, cost)
        elif task.state == DONE:
            newest_within_done = max(newest_within_done, task.id)
    if newest_over is not None and newest_over[0] > newest_within_done:
        task_id, cost = newest_over
        if trigger.budget_breach_task_id is None or task_id >= trigger.budget_breach_task_id:
            trigger.budget_breach_task_id = task_id
            trigger.budget_breach_reason = budget_reason(task_id, cost, budget)
    elif trigger.budget_breach_task_id is not None and newest_within_done > trigger.budget_breach_task_id:
        _clear_breach(trigger)
