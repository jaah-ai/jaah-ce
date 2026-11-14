"""The `routing_decisions` row — THE ONE WRITER of the table, and the Config shape every pick is spelled in.

Lifecycle (architecture AD3, AD5): `pending` (computed by `agent_router.refresh_picks` for the task's next attempt,
recomputed while unlocked, unfrozen and its `inputs_hash` moved) → `locked` at the lease (`lock`, with the
dispatch id) → `final` at the claim (`finalize`), or `superseded`. A declined lease puts the row back to `pending`
(`unlock`). At most one live row per task (`uq_routing_decisions_live_task`).

Nothing here commits, and nothing here writes `tasks`: any `tasks` write moves `updated_at` and the list windows.
The callers in `agent_router/__init__.py` wrap each write in a SAVEPOINT that never raises (R3).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app import agent_providers, agent_resolution
from app.clock import utcnow
from app.models.routing import (
    FINAL, LIVE_STATUSES, LOCKED, PENDING, RECOMMEND_RESPONSES, SUPERSEDED, RoutingDecision,
)
from app.models.tasks import Task

KIND_BUILTIN, KIND_AGENT = "builtin", "agent"
ACCEPTED, OVERRIDDEN, NO_RESPONSE = RECOMMEND_RESPONSES
#: The window the exploration budget is measured over (A1), and the rows it counts: a pending or locked row is a
#: reservation, so one pass's fan-out cannot overspend it.
BUDGET_WINDOW = timedelta(days=30)
_BUDGET_STATUSES = (PENDING, LOCKED, FINAL)
#: `stats_version` of a decision that read no ledger statistics.
NO_STATS = "none"


def config(*, kind: str, provider: Optional[str] = None, agent_id: Optional[UUID] = None,
           tier: Optional[str] = None, model: Optional[str] = None) -> dict[str, Any]:
    """One Config (architecture §3.1): a runner, its tier and model, and its review lanes (a coder's pick fills them:
    `review_lanes.with_lanes`)."""
    return {
        "kind": kind, "provider": provider, "agent_id": str(agent_id) if agent_id is not None else None,
        "tier": tier, "model": model, "review": {"lanes": [], "blind": []},
    }


def config_key(cfg: dict[str, Any]) -> str:
    """A Config's identity among the router's candidates: `"<kind>:<provider|agent_id>:<tier>"` (`-` for no tier).
    Candidates are ordered by it (architecture AD4); never shown to a person."""
    who = cfg.get("agent_id") if cfg.get("kind") == KIND_AGENT else cfg.get("provider")
    return f"{cfg.get('kind')}:{who or '-'}:{cfg.get('tier') or '-'}"


def config_of_choice(choice: agent_resolution.Choice, task: Task) -> dict[str, Any]:
    """The Config of a resolved (non-refused) choice: an agent runs no model of ours; a built-in carries the
    task's own model pin, or none (the persona and project defaults decide at compose)."""
    if isinstance(choice, agent_resolution.Sdk):
        return config(kind=KIND_AGENT, agent_id=choice.agent.id)
    from app import model_catalog

    model = task.model or None
    return config(kind=KIND_BUILTIN, provider=getattr(choice, "provider", None),
                  tier=model_catalog.tier_of(model) or None if model else None, model=model)


def describe(session: Session, cfg: dict[str, Any]) -> str:
    """A Config in words a person reads: the agent's or vendor's display name, never a slug."""
    if cfg.get("kind") == KIND_AGENT:
        agent = agent_resolution.load_agent(session, UUID(cfg["agent_id"])) if cfg.get("agent_id") else None
        return agent.display_name if agent is not None else "an agent that no longer exists"
    provider = agent_providers.by_value(cfg["provider"]) if cfg.get("provider") else None
    return provider.label if provider is not None else "any available runner"


def inputs_hash(task: Task, *, attempt: int, mode: str, policy_version: str, default_pick: dict[str, Any],
                project_knobs: dict[str, Any], held: bool, stats_version: str = NO_STATS,
                available: Optional[list[str]] = None) -> str:
    """What a pick depends on (architecture §4.1), digested: a pending row is recomputed when it moves.
    `available` is the sorted keys of the candidates placeable now, so a vendor coming or going re-picks."""
    body = hashlib.sha256((task.body or "").encode()).hexdigest()
    canonical = json.dumps({
        "persona": task.persona, "title": task.title, "body": body,
        "agent_id": str(task.agent_id) if task.agent_id else None, "bound_provider": task.bound_provider,
        "model": task.model, "pin_source": task.pin_source, "playbook_stage": task.playbook_stage,
        "attempt": attempt, "mode": mode, "policy": policy_version, "project": project_knobs,
        "stats": stats_version, "default": default_pick, "held": held, "available": available or [],
    }, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def live_row(session: Session, task_id: int, *, for_update: bool = False,
             nowait: bool = False) -> Optional[RoutingDecision]:
    """The task's one `pending`/`locked` row, or None. `nowait` raises at once on a row another transaction locks."""
    query = select(RoutingDecision).where(
        RoutingDecision.task_id == task_id, RoutingDecision.status.in_(LIVE_STATUSES)
    ).execution_options(populate_existing=True)
    return session.scalars(query.with_for_update(nowait=nowait) if for_update else query).first()


def live_rows(session: Session, task_ids: list[int]) -> dict[int, RoutingDecision]:
    """`{task id: live row}` for those of `task_ids` that have one, in one read."""
    if not task_ids:
        return {}
    rows = session.scalars(select(RoutingDecision).where(
        RoutingDecision.task_id.in_(task_ids), RoutingDecision.status.in_(LIVE_STATUSES)
    )).all()
    return {row.task_id: row for row in rows}


def routed_spend(session: Session, project_id: int, persona: str, *, exclude_task: Optional[int] = None
                 ) -> tuple[Decimal, Decimal]:
    """`(exploratory, total)` dollars of the project's `persona` dispatches the router chose (`used = 'router'`) in
    the last `BUDGET_WINDOW`, reservations included: what the outcome ledger priced the decision's dispatch at where
    it has (`task_outcomes.cost_usd`), else the pick's expected dollars. `exclude_task`'s live row is left out: the
    pick being made replaces it."""
    from app.models.outcomes import TaskOutcome

    actual = (select(func.sum(TaskOutcome.cost_usd)).where(TaskOutcome.routing_decision_id == RoutingDecision.id)
              .correlate(RoutingDecision).scalar_subquery())
    cost = func.coalesce(actual, RoutingDecision.expected_cost_usd, 0)
    query = select(
        func.coalesce(func.sum(case((RoutingDecision.exploratory, cost), else_=0)), 0),
        func.coalesce(func.sum(cost), 0),
    ).where(
        RoutingDecision.project_id == project_id, RoutingDecision.persona == persona,
        RoutingDecision.used == "router", RoutingDecision.status.in_(_BUDGET_STATUSES),
        RoutingDecision.created_at >= utcnow() - BUDGET_WINDOW,
    )
    if exclude_task is not None:
        query = query.where((RoutingDecision.task_id != exclude_task) | (RoutingDecision.status == FINAL))
    spent, total = session.execute(query).one()
    return Decimal(spent), Decimal(total)


@dataclass(frozen=True)
class PickDraft:
    """What `policy.pick` decides; `write_pending` stores it."""

    router_pick: dict[str, Any]
    default_pick: dict[str, Any]
    used: str
    fallback_reason: Optional[str]
    candidates: list[dict[str, Any]]
    features: dict[str, Any]
    reason: str
    exploratory: bool = False
    exploration_capped: bool = False
    expected_cost_usd: Optional[Any] = None
    stats_version: str = NO_STATS


def write_pending(session: Session, task: Task, *, attempt: int, mode: str, mode_basis: dict[str, Any],
                  inputs: str, draft: PickDraft) -> RoutingDecision:
    """Supersede the task's live row, if any, and add the new `pending` one. Flushes."""
    _supersede_live(session, task.id)
    row = RoutingDecision(
        org_id=task.org_id, project_id=task.project_id, task_id=task.id, attempt=attempt, status=PENDING,
        inputs_hash=inputs, persona=task.persona, mode=mode, mode_basis=mode_basis, features=draft.features,
        candidates=draft.candidates, router_pick=draft.router_pick, default_pick=draft.default_pick,
        used=draft.used, fallback_reason=draft.fallback_reason, exploratory=draft.exploratory,
        exploration_capped=draft.exploration_capped, expected_cost_usd=draft.expected_cost_usd,
        reason=draft.reason, stats_version=draft.stats_version,
    )
    session.add(row)
    session.flush()
    return row


def respond(session: Session, row: RoutingDecision, response: Optional[str], *,
            human_pick: Optional[dict[str, Any]] = None) -> None:
    """Record a person's answer to the live row's recommendation (AD14): `accepted`, or `overridden` with what they
    chose instead; the row is frozen, never recomputed. `None` takes an answer back (the person cleared their pin):
    the row thaws, and the next pass recomputes it. Flushes."""
    row.recommend_response = response
    row.human_pick = human_pick if response == OVERRIDDEN else None
    row.frozen = response is not None
    session.flush()


def _supersede_live(session: Session, task_id: int) -> None:
    session.execute(
        update(RoutingDecision)
        .where(RoutingDecision.task_id == task_id, RoutingDecision.status.in_(LIVE_STATUSES))
        .values(status=SUPERSEDED)
        .execution_options(synchronize_session=False)
    )


def lock(session: Session, task_id: int, dispatch_id: Optional[str]) -> None:
    """`pending → locked` at the lease, naming the dispatch. A lock an earlier lease left (it never claimed, or its
    finalize failed) is this lease's now."""
    session.execute(
        update(RoutingDecision)
        .where(RoutingDecision.task_id == task_id, RoutingDecision.status.in_(LIVE_STATUSES))
        .values(status=LOCKED, dispatch_id=dispatch_id, locked_at=utcnow())
        .execution_options(synchronize_session=False)
    )


def unlock(session: Session, task_id: int) -> None:
    """`locked → pending`: the lease was given back, so the pick is again the next attempt's. `locked_at` keeps the
    last lease's time: the unplaced timeout (AD16) runs from it."""
    session.execute(
        update(RoutingDecision)
        .where(RoutingDecision.task_id == task_id, RoutingDecision.status == LOCKED)
        .values(status=PENDING, dispatch_id=None)
        .execution_options(synchronize_session=False)
    )


def set_actual_model(session: Session, task_id: int, model: Optional[str]) -> None:
    """Record the concrete model the dispatch launches on its live row; `finalize` keeps it in `actual`."""
    row = live_row(session, task_id, for_update=True)
    if row is None or not model:
        return
    row.actual = {**(row.actual or {}), "model": model}
    session.flush()


#: The reason a claim with no live row records.
NO_PICK_REASON = "No routing decision was ready when this task was claimed, so it ran on {runner}."


def agrees(pick: Optional[dict[str, Any]], actual: Optional[dict[str, Any]]) -> bool:
    """Whether the router's pick is what ran: the same kind of runner and the same agent or vendor; a pick that
    names no vendor (any available runner) or no model, or a dispatch whose model is not stamped yet, leaves that part
    open."""
    if not pick or not actual or pick.get("kind") != actual.get("kind"):
        return False
    if pick.get("kind") == KIND_AGENT:
        return bool(pick.get("agent_id")) and pick.get("agent_id") == actual.get("agent_id")
    if pick.get("provider") and pick.get("provider") != actual.get("provider"):
        return False
    return not pick.get("model") or not actual.get("model") or pick.get("model") == actual.get("model")


def finalize(session: Session, task_id: int, dispatch_id: Optional[str]) -> Optional[RoutingDecision]:
    """`→ final` at the claim (architecture AD5), with `actual` read off the claimed row. A live row the router
    had chosen for but that was never locked does not describe what ran, so it is superseded; with no usable
    row, a default-only row records the dispatch (`no_pick`). Flushes."""
    task = session.scalars(select(Task).where(Task.id == task_id)).first()
    if task is None:
        return None
    actual = actual_config(task)
    now = utcnow()
    row = live_row(session, task_id, for_update=True)
    if row is not None and row.status == PENDING and row.used == "router":
        _supersede_live(session, task_id)
        row = None
    if row is None:
        from app.agent_router import modes, review_lanes

        cfg = {key: value for key, value in actual.items() if key != "model"} | {"model": task.model or None}
        if task.persona == review_lanes.CODER:
            cfg = review_lanes.with_lanes(cfg, task.review_tier)
        project, org = _project_and_org(session, task)
        mode = modes.effective_mode(session, org, project).value if project and org else modes.Mode.SHADOW.value
        basis = modes.mode_basis(session, org, project) if project and org else {}
        row = RoutingDecision(
            org_id=task.org_id, project_id=task.project_id, task_id=task.id, attempt=task.dispatch_attempts,
            status=FINAL, inputs_hash="", persona=task.persona, mode=mode, mode_basis=basis,
            features={"persona": task.persona}, candidates=[candidate(cfg)], router_pick=cfg, default_pick=cfg,
            actual=actual, used="default", fallback_reason="no_pick", dispatch_id=dispatch_id,
            reason=NO_PICK_REASON.format(runner=describe(session, cfg)), stats_version=NO_STATS,
            finalized_at=now,
        )
        session.add(row)
        session.flush()
        return row
    row.status = FINAL
    row.attempt = task.dispatch_attempts
    row.finalized_at = now
    row.dispatch_id = dispatch_id or row.dispatch_id
    row.actual = {**actual, "model": (row.actual or {}).get("model") or actual.get("model")}
    if row.mode == "recommend" and row.recommend_response is None:
        row.recommend_response = NO_RESPONSE
    if row.frozen and row.recommend_response == ACCEPTED:
        # An accepted pick that is what ran, its tier included: the router's choice was used (AD14), ahead of the
        # pin it wrote.
        pick_tier = (row.router_pick or {}).get("tier")
        if agrees(row.router_pick, row.actual) and (not pick_tier or pick_tier == (row.actual or {}).get("tier")):
            row.used = "router"
    if row.used != "router":
        # A Recommend pick nobody took explored nothing: the default ran.
        row.exploratory = False
    session.flush()
    return row


def candidate(cfg: dict[str, Any]) -> dict[str, Any]:
    """One `candidates` entry: the config, eligible, with no statistics yet."""
    return {"config": cfg, "eligible": True, "excluded_reason": None, "stats": None, "score": None, "sample": None}


def actual_config(task: Task) -> dict[str, Any]:
    """What the claimed dispatch runs, off the task's own stamps: the agent run's, else the bound vendor and the
    tier and model the lease stamped."""
    if task.run_agent_id is not None:
        return config(kind=KIND_AGENT, agent_id=task.run_agent_id)
    return config(kind=KIND_BUILTIN, provider=task.bound_provider, tier=task.dispatched_tier,
                  model=task.dispatched_model)


def _project_and_org(session: Session, task: Task):
    from app.models.orgs import Org
    from app.models.projects import Project

    project = session.scalars(select(Project).where(Project.id == task.project_id)).first()
    org = session.scalars(select(Org).where(Org.id == task.org_id)).first()
    return project, org
