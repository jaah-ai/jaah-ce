"""Monthly budgets on a project or a group (D-F8: a "team" is the existing `Group`): THE SINGLE HOME of their rules.

SPEND is the Jaah AI model spend of the calendar month in UTC: Σ `token_usage_calls.charge_micros` since the
month's first day — a project's by `project_id`, a group's by `user_id` among its members (a member's calls count
for every group they are in). Read per org in one grouped query per scope (`Ledger`), on the
`ix_token_usage_calls_org_occurred` index.

STATES, per budget: below 80% nothing; 80–99% `warn`; 100% and over `held`. No budget = unlimited.

THE HOLD applies while the org's plan has Budgets (`plan_terms` switch `budgets_enabled`): a `ready` task of a
held project, or one whose launcher (`task_ancestry.launcher_user_id`) is in a held group, is not dispatched
(`routes/fleet.py::fleet_allocation`) and waits naming the budget (`org_limits.wait_reasons`). The project's
budget is named first, then the launcher's groups by name. Running tasks are never touched. A raise, or the
month turning, lifts it on the next pass — nothing is stored to undo.

THE NOTICES go once a month each (`warned_month`, `held_month`): the `budget-alerts` cron (`alert`) emails the
org's Owners and Admins through the billing preferences (`email_notify.deliver_billing_message`, plus Telegram
where linked) and records `Emailed N admins` on the org. A raise below a threshold clears its stamp (`stamp_after_raise`),
so crossing it again notifies again.
"""
from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Final, Iterable, Literal, Optional, Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import edition_features, email_notify, merge_lane_state, plan_terms, task_ancestry
from app.clock import utcnow
from app.models.access import Budget, Group, UserGroupMembership
from app.models.metering import TokenUsageCall
from app.models.projects import Project
from app.money import usd_text
from app.user_activity import record_write

# The community edition has no Telegram: `_notify` reads this name behind the same check.
if edition_features.enabled("telegram"):
    from app import telegram_notify

log = logging.getLogger("budgets")

#: The plan switch that turns budgets on for an org (`app/plan_terms.py::SWITCHES`).
SWITCH: Final = "budgets_enabled"
#: The share of a budget at which Owners and Admins are warned.
WARN_PERCENT: Final = 80
#: The alert's row in the org's activity (`customer_history.CATEGORY_OF`'s `billing.`).
ALERT_EMAILED: Final = "billing.budget_alert_emailed"

Scope = Literal["project", "group"]
State = Literal["ok", "warn", "held"]
SCOPES: Final[tuple[Scope, ...]] = ("project", "group")


def month_of(now: Optional[datetime] = None) -> date:
    """The UTC calendar month `now` falls in, as its first day."""
    now = (now or utcnow()).astimezone(timezone.utc)
    return date(now.year, now.month, 1)


def _month_start(month: date) -> datetime:
    return datetime(month.year, month.month, month.day, tzinfo=timezone.utc)


def percent(spent_micros: int, limit_micros: int) -> int:
    """Whole percent of the budget spent, rounded down (99.9% is still 99%)."""
    return spent_micros * 100 // limit_micros


def state_of(spent_micros: int, limit_micros: int) -> State:
    if spent_micros >= limit_micros:
        return "held"
    if spent_micros * 100 >= limit_micros * WARN_PERCENT:
        return "warn"
    return "ok"


def scope_of(budget: Budget) -> Scope:
    return "project" if budget.project_id is not None else "group"


@dataclass(frozen=True)
class Status:
    """One budget this month: what it caps, its `name` (the project's or group's), what was spent."""

    budget: Budget
    name: str
    spent_micros: int

    @property
    def scope(self) -> Scope:
        return scope_of(self.budget)

    @property
    def state(self) -> State:
        return state_of(self.spent_micros, self.budget.monthly_usd_micros)

    @property
    def percent(self) -> int:
        return percent(self.spent_micros, self.budget.monthly_usd_micros)

    def amounts(self) -> str:
        """"$12.40 of $10.00"."""
        return f"{usd_text(self.spent_micros)} of {usd_text(self.budget.monthly_usd_micros)}"

    def hold_reason(self) -> str:
        """Why a task this budget holds waits, and what to do next."""
        if self.scope == "project":
            return f"Waiting: project budget reached ({self.amounts()}). Raise it in Project settings."
        return f"Waiting: {self.name} group budget reached ({self.amounts()}). Raise it in Group settings."


# ── Spend ────────────────────────────────────────────────────────────────────────────────────────────


def _next_month(month: date) -> date:
    return date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)


def _this_month(org_id: UUID, month: date):
    return (
        TokenUsageCall.org_id == org_id,
        TokenUsageCall.occurred_at >= _month_start(month),
        TokenUsageCall.occurred_at < _month_start(_next_month(month)),
    )


def project_spend(session: Session, org_id: UUID, project_ids: Iterable[int], month: date) -> dict[int, int]:
    """`{project id: micros spent in `month`}` for `project_ids` (absent = nothing spent)."""
    ids = list(project_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(TokenUsageCall.project_id, func.sum(TokenUsageCall.charge_micros))
        .where(*_this_month(org_id, month), TokenUsageCall.project_id.in_(ids))
        .group_by(TokenUsageCall.project_id)
    ).all()
    return {project_id: int(total or 0) for project_id, total in rows}


def group_spend(session: Session, org_id: UUID, group_ids: Iterable[UUID], month: date) -> dict[UUID, int]:
    """`{group id: micros its members spent in `month`}` for `group_ids`. Each member counted once per group,
    however many membership rows they hold (legacy scoped rows can repeat a pair)."""
    ids = list(group_ids)
    if not ids:
        return {}
    members = (
        select(UserGroupMembership.group_id, UserGroupMembership.user_id)
        .where(UserGroupMembership.group_id.in_(ids)).distinct().subquery()
    )
    rows = session.execute(
        select(members.c.group_id, func.sum(TokenUsageCall.charge_micros))
        .join(TokenUsageCall, TokenUsageCall.user_id == members.c.user_id)
        .where(*_this_month(org_id, month))
        .group_by(members.c.group_id)
    ).all()
    return {group_id: int(total or 0) for group_id, total in rows}


def spent(session: Session, org_id: UUID, scope: Scope, scope_id: int | UUID, now: Optional[datetime] = None) -> int:
    """What one project or group spent this month, budget or not."""
    month = month_of(now)
    if scope == "project":
        return project_spend(session, org_id, [scope_id], month).get(scope_id, 0)  # type: ignore[arg-type]
    return group_spend(session, org_id, [scope_id], month).get(scope_id, 0)  # type: ignore[arg-type]


def statuses_of(session: Session, budgets: Sequence[Budget], now: Optional[datetime] = None) -> list[Status]:
    """Each of one org's `budgets` with its name and this month's spend: two grouped queries, whatever the count.
    Projects first, then groups, each by name."""
    if not budgets:
        return []
    org_id = budgets[0].org_id
    month = month_of(now)
    project_ids = [b.project_id for b in budgets if b.project_id is not None]
    group_ids = [b.group_id for b in budgets if b.group_id is not None]
    by_project = project_spend(session, org_id, project_ids, month)
    by_group = group_spend(session, org_id, group_ids, month)
    names: dict[object, str] = {}
    if project_ids:
        names.update(session.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids))).tuples().all())
    if group_ids:
        names.update(session.execute(select(Group.id, Group.name).where(Group.id.in_(group_ids))).tuples().all())
    out = [
        Status(b, names.get(b.project_id if b.project_id is not None else b.group_id, ""),
               by_project.get(b.project_id, 0) if b.project_id is not None else by_group.get(b.group_id, 0))  # type: ignore[arg-type]
        for b in budgets
    ]
    return sorted(out, key=lambda s: (SCOPES.index(s.scope), s.name.lower(), str(s.budget.id)))


def org_budgets(session: Session, org_id: UUID, *, lock: bool = False) -> list[Budget]:
    stmt = select(Budget).where(Budget.org_id == org_id)
    if lock:
        stmt = stmt.with_for_update(skip_locked=True)
    return list(session.scalars(stmt).all())


def statuses(session: Session, org_id: UUID, now: Optional[datetime] = None) -> list[Status]:
    """Every budget of `org_id` this month (`statuses_of`)."""
    return statuses_of(session, org_budgets(session, org_id), now)


# ── The hold ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Ledger:
    """One dispatch pass's (or one read's) view of the held budgets, each org read once: its plan switch, then —
    only for an org that has budgets — its budgets' spend. `hold_reason` answers per task."""

    session: Session
    now: Optional[datetime] = None
    _held: dict[UUID, list[Status]] = field(default_factory=dict)
    _groups_of: dict[UUID, set[UUID]] = field(default_factory=dict)

    def held(self, org_id: UUID) -> list[Status]:
        """`org_id`'s budgets at or over 100%, when its plan has Budgets; else none."""
        if org_id not in self._held:
            found = org_budgets(self.session, org_id)
            on = bool(found) and plan_terms.switch_on(self.session, org_id, SWITCH)
            self._held[org_id] = [s for s in statuses_of(self.session, found, self.now) if s.state == "held"] if on else []
        return self._held[org_id]

    def _launcher_groups(self, task) -> set[UUID]:
        user_id = task_ancestry.launcher_user_id(self.session, task.id)
        if user_id is None:
            return set()
        if user_id not in self._groups_of:
            self._groups_of[user_id] = set(self.session.scalars(
                select(UserGroupMembership.group_id).where(UserGroupMembership.user_id == user_id)
            ).all())
        return self._groups_of[user_id]

    def hold_reason(self, task) -> Optional[str]:
        """Why a budget holds `task` back (its project's first, then its launcher's groups by name), or None."""
        # A merge-lane run runs no model, so no budget holds it (`routes/fleet.py` places it apart).
        if task.org_id is None or merge_lane_state.is_lane_persona(task.persona):
            return None
        held = self.held(task.org_id)
        if not held:
            return None
        for status in held:
            if status.scope == "project" and status.budget.project_id == task.project_id:
                return status.hold_reason()
        groups = [s for s in held if s.scope == "group"]
        if groups:
            mine = self._launcher_groups(task)
            for status in groups:
                if status.budget.group_id in mine:
                    return status.hold_reason()
        return None


def hold_reasons(session: Session, tasks: Iterable, now: Optional[datetime] = None) -> dict[int, str]:
    """`{task id: reason}` for those of `tasks` a budget holds back (`Ledger.hold_reason`)."""
    ledger = Ledger(session, now)
    out: dict[int, str] = {}
    for task in tasks:
        reason = ledger.hold_reason(task)
        if reason is not None:
            out[task.id] = reason
    return out


# ── Saving ───────────────────────────────────────────────────────────────────────────────────────────


def stamp_after_raise(budget: Budget, spent_micros: int, now: Optional[datetime] = None) -> None:
    """After the amount changed: a notice whose threshold the new amount is no longer at goes again when it is
    crossed again this month (a raise lifts the hold; a cut that crosses a line is the cron's to notice)."""
    state = state_of(spent_micros, budget.monthly_usd_micros)
    if state != "held":
        budget.held_month = None
    if state == "ok":
        budget.warned_month = None


# ── The notices (the `budget-alerts` cron) ───────────────────────────────────────────────────────────


@dataclass
class Report:
    warned: int = 0
    reached: int = 0
    errors: int = 0

    @property
    def acted(self) -> bool:
        return bool(self.warned or self.reached or self.errors)

    def summary(self) -> str:
        if not self.acted:
            return "no budget crossed 80% or 100%"
        return f"{self.reached} reached, {self.warned} at {WARN_PERCENT}%, {self.errors} failed"


def _console_path(status: Status) -> str:
    if status.scope == "project":
        return f"/projects/{status.budget.project_id}"
    return f"/groups/{status.budget.group_id}"


def _wording(status: Status, reached: bool) -> tuple[str, str]:
    """The notice's headline and text."""
    what = f"Project {status.name}" if status.scope == "project" else f"Group {status.name}"
    who = f"in {status.name}" if status.scope == "project" else f"launched by members of {status.name}"
    where = "Project settings" if status.scope == "project" else "Group settings"
    spent_line = f"Spent {status.amounts()} this month (UTC)."
    if reached:
        return (
            f"{what} reached its budget",
            f"{spent_line} New tasks {who} wait until next month. Running tasks finish. Raise it in {where}.",
        )
    return (
        f"{what} has used {status.percent}% of its budget",
        f"{spent_line} At 100%, new tasks {who} wait. Raise it in {where}.",
    )


def _notify(session: Session, status: Status, reached: bool) -> None:
    """Tell the org's Owners and Admins, then record how many were emailed. Never raises."""
    org_id = status.budget.org_id
    headline, text = _wording(status, reached)
    href = _console_path(status)
    try:
        sent, failed = email_notify.deliver_billing_message(
            session, org_id, headline=headline, text=text, button_label="Open budget", href=href,
            event=email_notify.BUDGET_EVENT,
        )
        # And over Telegram, to whoever chose it; the record counts emails only (as `plan_rollover` does).
        if edition_features.enabled("telegram"):
            telegram_notify.deliver_billing_message(
                session, org_id, headline=headline, text=text, href=href, event=email_notify.BUDGET_EVENT,
            )
        # Why nobody was emailed; `plan_rollover` is billing's, so an edition without it records the count alone.
        unsent: Optional[str] = None
        why = ""
        if edition_features.enabled("billing"):
            from app.plan_rollover import UNSENT, unsent_reason

            try:
                unsent = unsent_reason(session, org_id, failed, email_notify.BUDGET_EVENT) if sent == 0 else None
            except Exception:  # noqa: BLE001 - the reason is a courtesy; the row is still owed
                log.exception("budgets: org %s emailed nobody and the reason could not be read", org_id)
                unsent = "send_failed"
            why = f" ({UNSENT[unsent]})" if unsent else ""
        record_write(
            session, None, ALERT_EMAILED,
            f"Emailed {sent} admin{'' if sent == 1 else 's'}{why}: {headline} "
            f"({status.amounts()})",
            data={
                "org_id": str(org_id), "count": sent, "scope": status.scope, "name": status.name,
                "budget_id": str(status.budget.id), "level": 100 if reached else WARN_PERCENT,
                "spent_micros": status.spent_micros, "monthly_usd_micros": status.budget.monthly_usd_micros,
                **({"unsent": unsent} if unsent else {}),
            },
            org_id=org_id,
        )
        session.commit()
    except Exception:  # noqa: BLE001 - the stamp is committed; a lost email must not resend or block the rest
        session.rollback()
        log.exception("budgets: org %s budget %s notice failed", org_id, status.budget.id)


def _alert_org(session: Session, org_id: UUID, now: Optional[datetime], report: Report) -> None:
    """Stamp each of `org_id`'s budgets newly at 80% or 100% this month, commit, then notify."""
    if not plan_terms.switch_on(session, org_id, SWITCH):
        session.rollback()
        return
    month = month_of(now)
    due: list[tuple[Status, bool]] = []
    for status in statuses_of(session, org_budgets(session, org_id, lock=True), now):
        budget = status.budget
        if status.state == "held" and budget.held_month != month:
            budget.held_month = month
            # Past 100% is past 80%: one notice, not two.
            budget.warned_month = month
            due.append((status, True))
        elif status.state == "warn" and budget.warned_month != month:
            budget.warned_month = month
            due.append((status, False))
    session.commit()
    for status, reached in due:
        if reached:
            report.reached += 1
        else:
            report.warned += 1
        _notify(session, status, reached)


def alert(now: Optional[datetime] = None) -> Report:
    """One pass over every org that has budgets (module docstring), on the operator plane."""
    fleet: AbstractContextManager[Session]
    if edition_features.enabled("multi_node_hosts"):
        from app import vnodes

        fleet = vnodes.fleet_session()
    else:
        # Run only by its system cron, which already holds the operator plane (`cron_runtime`).
        from app.db import SessionLocal

        fleet = SessionLocal()
    report = Report()
    with fleet as session:
        org_ids = list(session.scalars(select(Budget.org_id).distinct()).all())
        session.rollback()
        for org_id in org_ids:
            try:
                _alert_org(session, org_id, now, report)
            except Exception:  # noqa: BLE001 - one org must not stall the rest; retried next pass
                session.rollback()
                report.errors += 1
                log.exception("budgets: org %s alert pass failed", org_id)
    return report
