"""Which configurations the router may choose between for a task (architecture §4.3) — THE SINGLE HOME of the
candidate rule, the blind-review vendor rule (A9) and `router_eligibility`.

    providers = the org's provider grid ∩ its routing policy's providers; each at every tier in the policy's range
    agents    = the org's own SDK agents + its enabled catalog agents, for the task's persona, runnable, in the
                policy's agents — only with the plan feature (`plan_terms.intelligent_routing`); a registry agent
                never in a MicroVM project
    a Jaah-account org: only the model families its account holds a profile for, on the provider owning each

Each candidate is then marked placeable or not from `features.availability` (`excluded_reason='unavailable'`).
Candidates are sorted by `decisions.config_key`, the order the sampler draws in (architecture AD4).
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import agent_providers, agent_resolution, edition_features, model_catalog, plan_terms, provider_auth_policy
from app.agent_router import decisions, features, modes
from app.agent_router.personas import ROUTED_PERSONAS
from app.agent_router.policy_settings import OrgRoutingPolicy, org_policy
from app.models.agent_sessions import Agent, AgentEnablement
from app.models.orgs import Org
from app.models.projects import Project
from app.models.tasks import Task

UNAVAILABLE, UNPLACED, LATENCY_CAP = "unavailable", "unplaced", "latency_cap"
#: The tier a review persona's child runs at when nothing else says (architecture §4.3), clamped to the policy.
REVIEW_TIER = model_catalog.TIER_HIGH


def tiers_within(floor: Optional[str], ceiling: Optional[str]) -> list[str]:
    """The tiers between the policy's floor (lowest allowed) and ceiling (highest), most capable first."""
    order = model_catalog.MODEL_IDENTIFIERS
    low = order.index(floor) if floor in order else len(order) - 1
    high = order.index(ceiling) if ceiling in order else 0
    return list(order[high:low + 1])


def clamp(tier: str, floor: Optional[str], ceiling: Optional[str]) -> str:
    allowed = tiers_within(floor, ceiling)
    if tier in allowed:
        return tier
    order = model_catalog.MODEL_IDENTIFIERS
    return min(allowed, key=lambda t: abs(order.index(t) - order.index(tier)))


def grid_providers(session: Session, org_id: UUID, memo: Optional[dict] = None) -> list[str]:
    """The built-in providers the org's plan grid has any cell of, in catalogue order."""
    rows = {row for row, _auth in provider_auth_policy.allowed(session, org_id, memo=memo)}
    return [p.value for p in agent_providers.PROVIDERS if p.value in rows]


def configs(session: Session, org: Org, project: Project, persona: str, policy: OrgRoutingPolicy,
            memo: dict) -> list[dict[str, Any]]:
    """Every configuration the policy allows for `persona` in `project`, before availability, sorted by key."""
    providers = [p for p in grid_providers(session, org.id, memo.setdefault("grid", {}))
                 if policy.allowed_providers is None or p in policy.allowed_providers]
    tiers = tiers_within(policy.tier_floor, policy.tier_ceiling)
    route = None
    if edition_features.enabled("jaah_ai_credit"):
        from app.jaah_routing.routing import org_route
        route = features.remember(memo, ("bedrock", org.id), lambda: org_route(session, org.id))
    pairs = [(p, t) for p in providers for t in tiers]
    if route is not None:
        # The Jaah account runs only the model families it holds a profile for, each on the provider that owns it.
        pairs = [(p, t) for p, t in pairs if model_catalog.TIER_FAMILY.get(t) in route.arns
                 and agent_providers.provider_of_model(model_catalog.TIER_FAMILY[t]) == p]
    out = [decisions.config(kind=decisions.KIND_BUILTIN, provider=p, tier=t, model=t) for p, t in pairs]
    if route is None and features.remember(memo, ("feature", org.id),
                                        lambda: plan_terms.intelligent_routing(session, org)):
        microvm = features.microvm_project(session, project.id, memo)
        out += [decisions.config(kind=decisions.KIND_AGENT, agent_id=agent.id)
                for agent in routable_agents(session, org.id, persona, policy, microvm=microvm)]
    return sorted(out, key=decisions.config_key)


def routable_agents(session: Session, org_id: UUID, persona: str, policy: OrgRoutingPolicy, *,
                    microvm: bool = False) -> list[Agent]:
    """The org's own SDK agents and its enabled catalog agents that take `persona`, can run now and the policy
    allows; no registry agent in a MicroVM project."""
    from app import acp_registry

    enabled = select(AgentEnablement.agent_id).where(AgentEnablement.org_id == org_id)
    rows = session.scalars(select(Agent).where(
        Agent.status == "active",
        or_(and_(Agent.org_id == org_id, Agent.is_catalog.is_(False)),
            and_(Agent.is_catalog.is_(True), Agent.id.in_(enabled))),
    ).order_by(Agent.display_name, Agent.id)).all()
    return [
        agent for agent in rows
        if persona in (agent.personas or [])
        and (policy.allowed_agent_ids is None or agent.id in policy.allowed_agent_ids)
        and not (microvm and acp_registry.is_registry(agent))
        and agent_resolution.unrunnable_reason(session, agent, org_id) is None
    ]


def entry(cfg: dict[str, Any], *, eligible: bool = True, excluded_reason: Optional[str] = None) -> dict[str, Any]:
    """One `candidates` record; the policy fills `stats`, `score`, `sample` and the cost."""
    return {**decisions.candidate(cfg), "eligible": eligible, "excluded_reason": excluded_reason}


def candidate_set(session: Session, task: Task, project: Project, org: Org, policy: OrgRoutingPolicy,
                  memo: dict) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """`(candidates, availability)`: each allowed configuration, marked unplaceable where availability says so."""
    allowed = features.remember(memo, ("configs", project.id, task.persona),
                             lambda: configs(session, org, project, task.persona, policy, memo))
    facts = features.availability(session, task, project, allowed, memo)
    out = []
    for cfg in allowed:
        ok = facts[decisions.config_key(cfg)]["placement_ok"]
        out.append(entry(cfg, eligible=ok, excluded_reason=None if ok else UNAVAILABLE))
    return out, facts


def is_other_vendor(cfg: dict[str, Any], author: Optional[dict[str, Any]]) -> bool:
    """Whether `cfg` runs on a vendor other than the author's: another built-in provider, or an agent that is not
    the author's own."""
    author = author or {}
    if cfg["kind"] == decisions.KIND_BUILTIN:
        return not (author.get("kind") == decisions.KIND_BUILTIN and cfg["provider"] == author.get("provider"))
    return cfg["agent_id"] != author.get("agent_id")


def other_vendor(session: Session, entries: list[dict[str, Any]], author: Optional[dict[str, Any]],
                 policy: OrgRoutingPolicy) -> Optional[dict[str, Any]]:
    """The A9 rule: among the eligible candidates on a vendor other than the author's, the best sampled draw when
    any of them has ledger outcomes (`entries` scored by the policy, each with its `stats` and `sample`); else the
    first — built-ins in catalogue order at the review tier (clamped to the policy, else the nearest tier offered),
    then agents by display name — or None when there is none."""
    others = [e for e in entries if e["eligible"] and is_other_vendor(e["config"], author)]
    if any((e.get("stats") or {}).get("n") for e in others):
        return max(others, key=lambda e: e.get("sample") or 0)["config"]
    eligible = [e["config"] for e in others]
    target = clamp(REVIEW_TIER, policy.tier_floor, policy.tier_ceiling)
    order = model_catalog.MODEL_IDENTIFIERS
    for provider in agent_providers.PROVIDERS:
        offered = [c for c in eligible if c["kind"] == decisions.KIND_BUILTIN and c["provider"] == provider.value]
        if offered:
            return min(offered, key=lambda c: abs(order.index(c["tier"]) - order.index(target)))
    agents = [c for c in eligible if c["kind"] == decisions.KIND_AGENT]
    names = {c["agent_id"]: decisions.describe(session, c) for c in agents}
    return min(agents, key=lambda c: (names[c["agent_id"]], c["agent_id"])) if agents else None


# ── For the marketplace (C8) ────────────────────────────────────────────────────────────────────────


def router_eligibility(session: Session, org_id: UUID, agent_ref: str) -> tuple[bool, str]:
    """Whether the router may choose `agent_ref` (a built-in provider value or an agent id) for `org_id`, with the
    reason in a sentence a person reads."""
    policy = org_policy(session, org_id)
    org = session.scalars(select(Org).where(Org.id == org_id)).first()
    if org is None or not plan_terms.intelligent_routing(session, org):
        return False, modes.NOT_ON_PLAN
    provider = agent_providers.by_value(agent_ref)
    if provider is not None:
        if provider.value not in grid_providers(session, org_id):
            return False, f"Your plan doesn't include {provider.label}."
        if policy.allowed_providers is not None and provider.value not in policy.allowed_providers:
            return False, f"Your organization's routing policy doesn't allow {provider.label}."
        return True, f"The router may choose {provider.label}."
    try:
        agent = agent_resolution.load_agent(session, UUID(str(agent_ref)))
    except ValueError:
        agent = None
    if agent is None or agent.status != "active" or not agent_resolution.visible_to(session, agent, org_id):
        return False, "This agent isn't enabled for your organization."
    if not set(agent.personas or []) & set(ROUTED_PERSONAS):
        return False, f"The router chooses runners for build and review tasks only, and {agent.display_name} " \
                      "takes neither."
    refusal = agent_resolution.unrunnable_reason(session, agent, org_id)
    if refusal is not None:
        return False, refusal
    if policy.allowed_agent_ids is not None and agent.id not in policy.allowed_agent_ids:
        return False, f"Your organization's routing policy doesn't allow {agent.display_name}."
    return True, f"The router may choose {agent.display_name}."
