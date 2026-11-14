"""The task page's Routing panel: `GET /api/tasks/{task_id}/routing`, its "Use this"
(`POST /api/tasks/{task_id}/routing/accept`), and the task form's recommendation (`POST /api/routing/preview`).

Read-only: the task's live (`pending`/`locked`) decision and its `final` history, newest attempt first, from
`routing_decisions` (`app/agent_router/decisions.py` is the only writer), and the in-session review lanes its runs
reported in `run_telemetry`. Same access as the task itself
(`tasks:view` on the object; a task the caller cannot see is 404). Everything a person reads is rendered HERE in
words — runner display names, tier labels, the mode and why — so no config key, provider slug or enum value reaches
the page; the closed sets the client still maps (`status`, `used`, `recommend_response`) are labels only.

The two writes are a person's answer to a Recommend pick (`agent_router.accept`, AD14): "Use this" writes the pick into
the task's runner and model fields as the person's pin, under `tasks:edit`. The preview writes nothing: the pick it
shows is indicative, seeded from the draft, and the form sends it back as `routing_accepted_config` when accepted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import agent_providers, agent_resolution, agent_router, blind_review, model_catalog, plan_terms
from app.agent_router import decisions, modes, review_lanes
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, current_caller
from app.authz.object_access import can_access
from app.authz.visibility import ObjectAccess, has_project_key
from app.db_async import on_session
from app.entity_shape import from_row
from app.models.agent_sessions import RunTelemetry
from app.models.identity import AdminUser
from app.models.routing import FINAL, LIVE_STATUSES, RoutingDecision
from app.models.tasks import Task
from app.project_identity import project_or_404
from app.task_lookup import get_task_or_404

# A task's routing reads and its accept are behind the Intelligent routing plan switch for a tenant (403 while off; the
# operator passes). The preview needs no gate: it answers nothing outside Recommend, which the switch caps.
router = APIRouter(
    prefix="/api/tasks", tags=["routing"],
    dependencies=[Depends(plan_terms.tenant_switch_required(plan_terms.INTELLIGENT_ROUTING))],
)
preview_router = APIRouter(prefix="/api/routing", tags=["routing"])

_TIER_LABELS = {choice.value: choice.label for choice in model_catalog.MODELS}
_MODE_LABELS = {mode.value: mode.value.capitalize() for mode in modes.Mode}
#: An excluded candidate's reason in words. The policy records no exclusion yet (`agent_router/policy.py` is
#: default-only), so its codes have no vocabulary to map: one sentence until the sampling policy names them.
_EXCLUDED = "The routing policy left it out for this task."


class RoutingConfigOut(BaseModel):
    """One runner configuration in words: who runs it, at which tier, on which model (null when unpinned)."""

    runner: str
    tier: Optional[str] = None
    model: Optional[str] = None
    #: The review the configuration runs, in words.
    review: str = "The project's review settings"


class RoutingCandidateOut(BaseModel):
    config: RoutingConfigOut
    eligible: bool
    #: Why it was left out, as a sentence; null when eligible.
    excluded_reason: Optional[str] = None
    #: Null while the outcome ledger has too little data to score it ("Insufficient data").
    score: Optional[float] = None
    sample: Optional[int] = None


class RoutingDecisionOut(BaseModel):
    id: str
    attempt: int
    #: `pending`, `locked`, `final`.
    status: str
    mode: str
    #: Why the project runs in that mode, as a sentence.
    mode_reason: str
    reason: str
    #: `router` or `default`: which pick the dispatch uses.
    used: str
    router_pick: RoutingConfigOut
    default_pick: RoutingConfigOut
    #: What actually ran; null until the claim.
    actual: Optional[RoutingConfigOut] = None
    candidates: list[RoutingCandidateOut]
    #: Recommend mode only: `accepted`, `overridden`, `no_response`; null otherwise or before the claim.
    recommend_response: Optional[str] = None
    human_pick: Optional[RoutingConfigOut] = None
    exploratory: bool
    #: Whether a person may still accept this recommendation ("Use this"): pending, Recommend, unanswered.
    can_accept: bool = False
    created_at: datetime
    locked_at: Optional[datetime] = None
    finalized_at: Optional[datetime] = None


class ReviewLaneUsedOut(BaseModel):
    """One in-session review lane a run reported in its telemetry, in words."""

    #: "Code reviewer", "Verifier", … (the lane's own name when it is not one we know).
    lane: str
    #: The vendor's or agent's display name.
    runner: str
    model: Optional[str] = None
    reported_at: datetime


class BlindChildOut(BaseModel):
    """One blind review subtask of a coder task: its role, state, and the runner reviewing it (null before routing)."""

    task_id: int
    role: str
    state: str
    runner: Optional[str] = None


class TaskRoutingOut(BaseModel):
    task_id: int
    #: The decision the task's next dispatch will use; null when there is none yet.
    live: Optional[RoutingDecisionOut] = None
    #: Every dispatch's final decision, newest first.
    history: list[RoutingDecisionOut]
    #: The review lanes the task's runs reported (`run_telemetry.payload.review_lanes`), newest run first.
    review_lanes_used: list[ReviewLaneUsedOut] = []
    #: A coder task's Blind review line (`blind_review.status`): which review subtasks were filed, or why none were.
    blind_review: Optional[str] = None
    #: Those subtasks, each with the runner that reviews it.
    blind_children: list[BlindChildOut] = []


def _review_words(cfg: dict[str, Any]) -> str:
    """A Config's review lanes in words; none named is the project's own review settings."""
    review = cfg.get("review") or {}
    lanes: list[str] = []
    for lane in review.get("lanes") or []:
        name = str(lane.get("lane") or "") if isinstance(lane, dict) else str(lane)
        lanes.append(review_lanes.LANE_LABELS.get(name, name or "Review"))
    if not lanes:
        return "The project's review settings"
    words = ", ".join(lanes)
    return f"{words}; blind review" if review.get("blind") else words


def _config_out(session: Session, cfg: Optional[dict[str, Any]]) -> Optional[RoutingConfigOut]:
    if not cfg:
        return None
    tier = cfg.get("tier")
    return RoutingConfigOut(
        runner=decisions.describe(session, cfg),
        tier=_TIER_LABELS.get(tier) if tier else None,
        model=model_catalog.without_context_suffix(cfg.get("model")) or None,
        review=_review_words(cfg),
    )


def mode_reason(mode: str, basis: dict[str, Any]) -> str:
    """Why the decision ran in `mode`, from the `mode_basis` it recorded (`modes.mode_basis`), in a sentence."""
    label = _MODE_LABELS.get(mode, "Shadow")
    if not basis:
        return f"{label} mode."
    if not basis.get("plan_feature"):
        return f"{label} mode, because intelligent routing isn't included in your plan."
    project_mode = str(basis.get("project_mode") or "")
    org_max = str(basis.get("org_max") or "")
    if org_max == modes.OFF:
        return f"{label} mode, because routing is off for your organization."
    if project_mode == modes.INHERIT:
        return f"{label} mode, because routing is off for this project."
    try:
        capped = modes.rank(org_max) < modes.rank(project_mode)
    except ValueError:
        capped = False
    if capped:
        return f"{label} mode, because your organization allows routing up to {modes.MAX_WORDS.get(org_max, org_max)}."
    return f"{label} mode, as this project's routing setting chooses."


def _candidate_out(session: Session, entry: dict[str, Any]) -> RoutingCandidateOut:
    eligible = bool(entry.get("eligible", True))
    return RoutingCandidateOut(
        config=_config_out(session, entry.get("config")) or RoutingConfigOut(runner="any available runner"),
        eligible=eligible,
        excluded_reason=None if eligible else _EXCLUDED,
        score=entry.get("score"),
        # How many finished tasks the score rests on (the policy's own `sample` is its Thompson draw, not a count).
        sample=(entry.get("stats") or {}).get("n"),
    )


def decision_out(session: Session, row: RoutingDecision) -> RoutingDecisionOut:
    return from_row(
        RoutingDecisionOut, row,
        mode_reason=mode_reason(row.mode, row.mode_basis or {}),
        router_pick=_config_out(session, row.router_pick) or RoutingConfigOut(runner="any available runner"),
        default_pick=_config_out(session, row.default_pick) or RoutingConfigOut(runner="any available runner"),
        actual=_config_out(session, row.actual) if row.status == FINAL else None,
        candidates=[_candidate_out(session, entry) for entry in (row.candidates or [])],
        human_pick=_config_out(session, row.human_pick),
        can_accept=agent_router.answerable(row),
    )


def _lane_runner(session: Session, lane: dict[str, Any]) -> str:
    """Who ran a reported lane, by display name: an agent of ours by its name, else the vendor's label."""
    agent_id = lane.get("agent_id")
    if agent_id:
        try:
            agent = agent_resolution.load_agent(session, UUID(str(agent_id)))
        except ValueError:
            agent = None
        if agent is not None:
            return agent.display_name
    provider = agent_providers.by_value(str(lane.get("provider") or ""))
    return provider.label if provider is not None else "Another runner"


def review_lanes_used(session: Session, task: Any) -> list[ReviewLaneUsedOut]:
    """Every review lane the task's runs reported, newest run first, in the order each run reported them."""
    runs = session.scalars(
        select(RunTelemetry)
        .where(RunTelemetry.task_id == str(task.id), RunTelemetry.org_id == task.org_id,
               RunTelemetry.payload.has_key("review_lanes"))
        .order_by(RunTelemetry.created_at.desc(), RunTelemetry.id.desc())
    ).all()
    out = []
    for run in runs:
        lanes = run.payload.get("review_lanes")
        for lane in lanes if isinstance(lanes, list) else []:
            if not isinstance(lane, dict):
                continue
            name = str(lane.get("lane") or "")
            out.append(ReviewLaneUsedOut(
                lane=review_lanes.LANE_LABELS.get(name, name or "Review"), runner=_lane_runner(session, lane),
                model=model_catalog.without_context_suffix(lane.get("model")) or None, reported_at=run.created_at,
            ))
    return out


@router.get("/{task_id}/routing", response_model=TaskRoutingOut)
@feature("tasks", "view", object_type="task", object_params=("task_id",))
@on_session
def get_task_routing(session: Session, task_id: int, _user: AdminUser = Depends(require_user)) -> TaskRoutingOut:
    """The task's live routing decision and its final history. Writes nothing."""
    return _task_routing(session, get_task_or_404(task_id, session))


def _task_routing(session: Session, task: Task) -> TaskRoutingOut:
    rows = session.scalars(
        select(RoutingDecision)
        .where(RoutingDecision.task_id == task.id, RoutingDecision.org_id == task.org_id,
               RoutingDecision.status.in_((*LIVE_STATUSES, FINAL)))
        .order_by(RoutingDecision.created_at.desc())
    ).all()
    live = next((row for row in rows if row.status in LIVE_STATUSES), None)
    history = sorted((row for row in rows if row.status == FINAL),
                     key=lambda row: row.finalized_at or row.created_at, reverse=True)
    return TaskRoutingOut(
        task_id=task.id,
        live=decision_out(session, live) if live is not None else None,
        history=[decision_out(session, row) for row in history],
        review_lanes_used=review_lanes_used(session, task),
        blind_review=blind_review.status(session, task),
        blind_children=[BlindChildOut(**c) for c in blind_review.children_out(session, task)],
    )


@router.post("/{task_id}/routing/accept", response_model=TaskRoutingOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
@on_session
def accept_task_routing(session: Session, task_id: int, _user: AdminUser = Depends(require_user)) -> TaskRoutingOut:
    """"Use this": the live recommendation's pick becomes the task's runner and model, as a person's pin, and the
    decision is recorded `accepted`. 409 when there is no recommendation to accept (not Recommend, already
    answered, dispatching, or the router fell back)."""
    task = get_task_or_404(task_id, session)
    if agent_router.accept(session, task) is None:
        raise HTTPException(status_code=409, detail="There is no recommendation to accept for this task.")
    session.commit()
    return _task_routing(session, task)


class RoutingPreviewIn(BaseModel):
    """The task form's draft: the project and persona it is filed under and the text the router reads."""

    project_id: int
    persona: str = "coder"
    title: str = Field(default="", max_length=1000)
    body: str = Field(default="", max_length=200_000)
    #: A saved task: its own live recommendation, when it has one, is the answer.
    task_id: Optional[int] = None


class RoutingRecommendationOut(BaseModel):
    config: RoutingConfigOut
    #: The Config itself, sent back as `routing_accepted_config` when the person accepts it.
    pick: dict[str, Any]
    #: The full reason, a sentence or two.
    reason: str
    exploratory: bool
    #: `accepted`/`overridden` once a person answered this task's recommendation; null while unanswered.
    response: Optional[str] = None


class RoutingPreviewOut(BaseModel):
    #: The project's effective routing mode for this draft; null when the router does not route its persona.
    mode: Optional[str] = None
    #: What the router recommends; null outside Recommend mode or when it falls back to the default.
    recommendation: Optional[RoutingRecommendationOut] = None


@preview_router.post("/preview", response_model=RoutingPreviewOut)
@feature("tasks", "create")
@on_session
def preview_routing(
    session: Session,
    body: RoutingPreviewIn,
    caller: AsyncCaller = Depends(current_caller),
) -> RoutingPreviewOut:
    """The router's recommendation for a task draft, indicative only: seeded from the draft, so the same text shows
    the same pick. Writes nothing. `project_id` arrives in the body, so it is checked here as the create door does:
    `tasks:create` on that project, a missing and a foreign project one 404."""
    project = project_or_404(session, body.project_id, detail="Project not found")
    if not has_project_key(caller.principal, permission_key("tasks", "create"), project.id):
        raise HTTPException(status_code=404, detail="Project not found")
    task: Optional[Task] = None
    if body.task_id is not None:
        task = get_task_or_404(body.task_id, session)
        # The task's own read gate, as `GET …/routing` declares it: a hidden task or another's draft is 404.
        if task.project_id != project.id or not can_access(session, caller.principal, "task", task.id,
                                                            mode=ObjectAccess.READ):
            raise HTTPException(status_code=404, detail="Task not found")
        live = decisions.live_row(session, task.id)
        if live is not None and live.mode == modes.Mode.RECOMMEND.value and live.fallback_reason is None:
            return RoutingPreviewOut(mode=live.mode, recommendation=_recommendation_out(
                session, live.router_pick, live.reason, live.exploratory, live.recommend_response))
    else:
        task = Task(org_id=project.org_id, project_id=project.id, persona=body.persona, title=body.title,
                    body=body.body, dispatch_attempts=0)
    mode = agent_router.recommend_mode(session, task, project)
    if mode is None:
        return RoutingPreviewOut()
    draft = agent_router.preview(session, task, project, inputs_hash=agent_router.draft_hash(task.persona, task.body),
                                 launcher_id=caller.actor.id)
    if task.id is None and task in session:
        session.expunge(task)
    if draft is None:
        return RoutingPreviewOut(mode=mode.value)
    return RoutingPreviewOut(mode=mode.value, recommendation=_recommendation_out(
        session, draft.router_pick, draft.reason, draft.exploratory, None))


def _recommendation_out(session: Session, pick: dict[str, Any], text: str, exploratory: bool,
                        response: Optional[str]) -> RoutingRecommendationOut:
    return RoutingRecommendationOut(
        config=_config_out(session, pick) or RoutingConfigOut(runner="any available runner"),
        pick=pick, reason=text, exploratory=exploratory,
        response=response if response in (decisions.ACCEPTED, decisions.OVERRIDDEN) else None,
    )
