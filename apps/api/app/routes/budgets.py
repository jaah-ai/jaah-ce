"""Monthly budgets on a project or a group — the API behind Project settings, Group settings and Billing › Budgets.

Every rule (spend, states, the hold, the notices) is `app/budgets.py`'s. Every door is `@org_admin` (an Org
Owner/Admin in their own org, an operator fleet-wide) and refused with 403 naming the plan feature while the org's
plan leaves Budgets out (`plan_terms.require_switch`). `Tenancy.CALLER_PLANE`: the seam narrows every read, so
another org's project or group is a 404. The list is the caller's own org's.

Audit: `UserActivityMiddleware` records each write (the path names the project or group; the body the amount).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import budgets, plan_terms
from app.actors import actor_names, stamp_actor
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin
from app.db import get_session
from app.models.access import Budget, Group
from app.models.identity import AdminUser
from app.models.projects import Project
from app.entity_shape import get_or_404, optional_uuid

router = APIRouter(prefix="/api/budgets", tags=["budgets"])

#: The most one budget may be: $1,000,000 a month, so a typo of extra zeros is refused rather than stored.
MAX_MONTHLY_USD_MICROS = 1_000_000 * 1_000_000


class BudgetOut(BaseModel):
    id: UUID
    scope: Literal["project", "group"]
    project_id: Optional[int]
    group_id: Optional[UUID]
    #: The project's or group's name.
    name: str
    monthly_usd_micros: int
    spent_micros: int
    #: Whole percent spent, rounded down.
    percent: int
    #: Below 80% `ok`, 80–99% `warn`, 100% and over `held`.
    state: Literal["ok", "warn", "held"]
    #: The UTC month the spend is of, as its first day.
    month: date
    updated_at: datetime
    updated_by: Optional[str]


class BudgetsOut(BaseModel):
    items: list[BudgetOut]


class ScopeBudgetOut(BaseModel):
    budget: Optional[BudgetOut]
    #: This month's spend, budget or not.
    spent_micros: int
    month: date


class BudgetIn(BaseModel):
    monthly_usd_micros: int = Field(gt=0, le=MAX_MONTHLY_USD_MICROS)


def _out(status: budgets.Status, names: dict[UUID, str]) -> BudgetOut:
    budget = status.budget
    return BudgetOut(
        id=budget.id, scope=status.scope, project_id=budget.project_id, group_id=budget.group_id, name=status.name,
        monthly_usd_micros=budget.monthly_usd_micros, spent_micros=status.spent_micros, percent=status.percent,
        state=status.state, month=budgets.month_of(), updated_at=budget.updated_at,
        updated_by=names.get(budget.updated_by_id),
    )


_NO_PROJECT = "unknown project"


def _subject(session: Session, scope: str, scope_id: str) -> tuple[UUID, str, int | UUID]:
    """The project or group a path names — its org, its name, its typed id — or a 404."""
    if scope == "project":
        if not (scope_id.isascii() and scope_id.isdigit() and len(scope_id) <= 9):
            raise HTTPException(status_code=404, detail=_NO_PROJECT)
        project = get_or_404(session, Project, int(scope_id), detail=_NO_PROJECT)
        return project.org_id, project.name, project.id  # type: ignore[return-value]
    group_id = optional_uuid(scope_id)
    group = session.get(Group, group_id) if group_id is not None else None
    if group is None:
        raise HTTPException(status_code=404, detail="unknown group")
    return group.org_id, group.name, group.id  # type: ignore[return-value]


def _row(session: Session, scope: str, typed_id: int | UUID, *, lock: bool = False) -> Optional[Budget]:
    column = Budget.project_id if scope == "project" else Budget.group_id
    stmt = select(Budget).where(column == typed_id)
    if lock:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).one_or_none()


def _status(session: Session, budget: Budget, name: str) -> BudgetOut:
    scope = budgets.scope_of(budget)
    typed_id = budget.project_id if scope == "project" else budget.group_id
    status = budgets.Status(budget, name, budgets.spent(session, budget.org_id, scope, typed_id))  # type: ignore[arg-type]
    return _out(status, actor_names(session, [budget.updated_by_id]))


@router.get("", response_model=BudgetsOut)
@org_admin(Tenancy.CALLER_PLANE)
def list_budgets(actor: AdminUser = Depends(require_user), session: Session = Depends(get_session)) -> BudgetsOut:
    """Every budget of the caller's org with this month's spend: projects first, then groups, each by name."""
    plan_terms.require_switch(session, actor.org_id, budgets.SWITCH)  # type: ignore[arg-type]
    statuses = budgets.statuses(session, actor.org_id)  # type: ignore[arg-type]
    names = actor_names(session, [s.budget.updated_by_id for s in statuses])
    return BudgetsOut(items=[_out(s, names) for s in statuses])


@router.get("/{scope}/{scope_id}", response_model=ScopeBudgetOut)
@org_admin(Tenancy.CALLER_PLANE)
def get_budget(
    scope: Literal["project", "group"], scope_id: str, session: Session = Depends(get_session),
) -> ScopeBudgetOut:
    """One project's or group's budget (None = unlimited) and what it spent this month."""
    org_id, name, typed_id = _subject(session, scope, scope_id)
    plan_terms.require_switch(session, org_id, budgets.SWITCH)
    budget = _row(session, scope, typed_id)
    if budget is None:
        return ScopeBudgetOut(budget=None, spent_micros=budgets.spent(session, org_id, scope, typed_id),
                              month=budgets.month_of())
    out = _status(session, budget, name)
    return ScopeBudgetOut(budget=out, spent_micros=out.spent_micros, month=out.month)


@router.put("/{scope}/{scope_id}", response_model=BudgetOut)
@org_admin(Tenancy.CALLER_PLANE)
def put_budget(
    scope: Literal["project", "group"], scope_id: str, body: BudgetIn,
    actor: AdminUser = Depends(require_user), session: Session = Depends(get_session),
) -> BudgetOut:
    """Set the monthly budget. A raise past the spend lifts the hold on the next dispatch pass, and re-arms the
    notice of each threshold it is now below (`budgets.stamp_after_raise`)."""
    org_id, name, typed_id = _subject(session, scope, scope_id)
    plan_terms.require_switch(session, org_id, budgets.SWITCH)
    budget = _row(session, scope, typed_id, lock=True)
    if budget is None:
        budget = Budget(org_id=org_id, monthly_usd_micros=body.monthly_usd_micros,
                        **({"project_id": typed_id} if scope == "project" else {"group_id": typed_id}))
        stamp_actor(budget, actor, created=True)
        session.add(budget)
    elif budget.monthly_usd_micros != body.monthly_usd_micros:
        budget.monthly_usd_micros = body.monthly_usd_micros
        budgets.stamp_after_raise(budget, budgets.spent(session, org_id, scope, typed_id))
        stamp_actor(budget, actor)
    session.commit()
    session.refresh(budget)
    return _status(session, budget, name)


@router.delete("/{scope}/{scope_id}", status_code=204)
@org_admin(Tenancy.CALLER_PLANE)
def delete_budget(
    scope: Literal["project", "group"], scope_id: str, session: Session = Depends(get_session),
) -> Response:
    """Remove the budget: the project or group is unlimited again. Removing none is a no-op."""
    org_id, _name, typed_id = _subject(session, scope, scope_id)
    plan_terms.require_switch(session, org_id, budgets.SWITCH)
    budget = _row(session, scope, typed_id, lock=True)
    if budget is not None:
        session.delete(budget)
        session.commit()
    return Response(status_code=204)
