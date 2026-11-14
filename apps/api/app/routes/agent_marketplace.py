"""The agent marketplace — `GET /api/agent-marketplace`, read by Agents › Catalog (`apps/web/src/pages/agents/CatalogPage.tsx`).

One list of every runner an org can use, in four kinds: `builtin` (each `agent_providers.PROVIDERS` row), `org_agent`
(the org's own SDK agents), `catalog` (operator-published connectors, `routes/agents.py::_visible_catalog`) and
`registry` (the org's ACP registry installs). Each entry carries the personas it takes, a cost band
(`model_prices.cost_band`; built-ins only — an agent's model is its own), its availability on the org's live nodes,
whether the router may pick it (`agent_router.router_eligibility`) and its ledger profile.

No vendor self-reporting: every number here is Jaah's own. The ledger figures come only from the outcome ledger,
through `agent_router.ledger_stats` (`_ledger`): `no_ledger` while it holds no profile of the entry or while a
tenant's plan leaves Agent performance out (`plan_terms.AGENT_PERFORMANCE`), `insufficient`
below `ledger_stats.MIN_OUTCOMES` judged outcomes, else `ok` with its metrics; the entry model below whitelists its
fields and reads a manifest only for its SDK version (`agents.unsupported`: an SDK v1 entry is never enabled and its
router line says why), and a manifest carrying a performance, cost or quality key is refused at publish,
registration and refresh (`tools/manifest_check.py::SELF_REPORTED_KEY`).

Open to every org member holding `agents:view`, whatever the plan: an org without Agents on its grid still sees the
built-ins and the catalog, and each SDK-backed entry's `enable_refusal` says why it cannot be turned on
(`agent_resolution.cell_refusal`). Turning a catalog connector on or off stays `routes/agents.py`'s.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import (
    account_auth, acp_registry, agent_providers, agent_resolution, model_prices, persona_registry, plan_terms,
    pool_state,
)
from app.agent_router import candidates as router_candidates
from app.agent_router import features as router_features
from app.agent_router import ledger_stats, router_eligibility
from app.agents import STATUS_REMOVED, unsupported
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.db_async import DbDep
from app.models.agent_sessions import Agent
from app.models.metering import ModelPrice
from app.models.nodes import Node
from app.routes.agents import _catalog_rows_async, _visible_catalog

router = APIRouter(tags=["agents"])

Kind = Literal["builtin", "org_agent", "catalog", "registry"]
#: Whether an account of the provider may take a run now (`fleet.gate_accounts`, read-only): `ok`, `tiered` (only
#: an API-key spillover account), `blocked` (every account over its quota gate), `no_account`; agents carry no
#: account of their own, so theirs is `not_applicable`.
QuotaState = Literal["ok", "tiered", "blocked", "no_account", "not_applicable"]


class PersonaOut(BaseModel):
    name: str
    label: str


class CostBandOut(BaseModel):
    band: Literal["$", "$$", "$$$"]
    low_usd: float
    high_usd: float


class AvailabilityOut(BaseModel):
    enabled: bool
    capable_nodes_online: int
    quota_state: QuotaState


class RouterOut(BaseModel):
    eligible: bool
    reason: str


class LedgerMetricOut(BaseModel):
    """One ledger figure with its interval, as the outcome ledger computed it."""

    key: str
    value: float
    low: Optional[float] = None
    high: Optional[float] = None


class LedgerOut(BaseModel):
    state: Literal["no_ledger", "insufficient", "ok"]
    n: int
    metrics: Optional[list[LedgerMetricOut]] = None


class MarketplaceEntryOut(BaseModel):
    kind: Kind
    #: The agent's id (every kind but `builtin`), or the provider value (`builtin`).
    id: Optional[UUID] = None
    provider: Optional[str] = None
    display_name: str
    description: str
    personas: list[PersonaOut]
    cost_band: Optional[CostBandOut]
    availability: AvailabilityOut
    router: RouterOut
    ledger: LedgerOut
    #: Why this org cannot turn the entry on (its grid cell is off), or None. Catalog connectors only act on it.
    enable_refusal: Optional[str] = None
    #: A catalog connector the operator withdrew, still listed where this org enabled it.
    removed: bool = False
    #: Why the agent can no longer run (`agents.unsupported`: an SDK v1 agent), or None. Such an entry is never
    #: enabled and the router never picks it, whatever its row's status says.
    unsupported: Optional[str] = None


def _persona_out(names: list[str]) -> list[PersonaOut]:
    out = []
    for name in names:
        record = persona_registry.persona(name)
        out.append(PersonaOut(name=name, label=record.label if record is not None else name))
    return out


#: The personas a built-in provider takes: every pipeline persona that reaches a runner (the epic itself does not).
BUILTIN_PERSONAS: tuple[str, ...] = tuple(
    p.name for p in persona_registry.PERSONA_RECORDS if p.pipeline and p.dispatchable
)


def _ledger(session: Session, org_id: UUID, ref: str, shown: bool) -> LedgerOut:
    """The ledger's profile of `ref`, reduced to this shape; every figure is the ledger's own. Not `shown` (the plan
    leaves Agent performance out) reads as no ledger, unread."""
    if not shown:
        return LedgerOut(state="no_ledger", n=0)
    profile = ledger_stats.current().agent_profile(session, org_id, ref) or {}
    n = int(profile.get("n") or 0)
    if not profile:
        return LedgerOut(state="no_ledger", n=0)
    if n < ledger_stats.MIN_OUTCOMES:
        return LedgerOut(state="insufficient", n=n)
    metrics = [LedgerMetricOut(key=str(m["key"]), value=float(m["value"]),
                               low=m.get("low"), high=m.get("high"))
               for m in profile.get("metrics") or [] if isinstance(m, dict) and "key" in m and "value" in m]
    return LedgerOut(state="ok", n=n, metrics=metrics)


def _router(session: Session, org_id: UUID, ref: str) -> RouterOut:
    eligible, reason = router_eligibility(session, org_id, ref)
    return RouterOut(eligible=eligible, reason=reason)


def _live_org_nodes(session: Session, org_id: UUID) -> list[Any]:
    """The org's live nodes that take work (dispatch's own freshness rule; a Multi-Node Host takes none), with what
    each can run."""
    clause = pool_state.live_node_clause(pool_state.max_observation_age_secs())
    return list(session.execute(
        select(Node.id, Node.runner_clis).where(*clause, Node.org_id == org_id, pool_state.not_a_vnode_host())
    ).all())


def _quota_state(candidates: list[dict], provider: str, org_id: UUID) -> QuotaState:
    from app import jaah_scheduler
    from app.routes import fleet

    if not any(c.get("provider") == provider for c in candidates):
        return "no_account"
    # The quota question only: an account merely at its runner cap is busy, not over quota.
    status, enabled, tier = fleet.gate_accounts(candidates, allowed=None, provider=provider, org_id=org_id,
                                                apply_runner_cap=False)
    chosen = tier[0] if status == "tier" and tier else enabled[0] if status == "cold_start" and enabled else None
    if chosen is None:
        return "blocked"
    record = agent_providers.by_value(provider)
    kinds = record.auth_kinds if record is not None else (account_auth.AUTH_KIND_OAUTH,)
    return "tiered" if jaah_scheduler.is_tiered_pick(chosen, kinds) else "ok"


def _builtins(session: Session, org_id: UUID, nodes: list[Any], ledger_shown: bool) -> list[MarketplaceEntryOut]:
    on_grid = set(router_candidates.grid_providers(session, org_id))
    prices = model_prices.prices_by_model(session.scalars(select(ModelPrice)))
    accounts = router_features._accounts_by_org(session).get(org_id, [])
    personas = _persona_out(list(BUILTIN_PERSONAS))
    out = []
    for provider in agent_providers.PROVIDERS:
        # NULL `runner_clis` is "never attested", which asserts nothing (`fleet._node_cli_refusal`).
        capable = sum(1 for row in nodes if row.runner_clis is None or provider.binary in row.runner_clis)
        band = model_prices.cost_band(provider.value, prices)
        out.append(MarketplaceEntryOut(
            kind="builtin", provider=provider.value, display_name=provider.label,
            description=provider.description,
            personas=personas, cost_band=CostBandOut(**band) if band else None,
            availability=AvailabilityOut(enabled=provider.value in on_grid, capable_nodes_online=capable,
                                         quota_state=_quota_state(accounts, provider.value, org_id)),
            router=_router(session, org_id, provider.value),
            ledger=_ledger(session, org_id, provider.value, ledger_shown),
            enable_refusal=None if provider.value in on_grid else f"Your plan doesn't include {provider.label}.",
        ))
    return out


def _agent_entry(session: Session, org_id: UUID, row: Agent, kind: Kind, enabled: bool,
                 node_ids: list[str], capable_by_placement: dict[str, int], ledger_shown: bool) -> MarketplaceEntryOut:
    placement = agent_resolution.placement_class(row)
    if placement not in capable_by_placement:  # one node read per placement class, not per agent
        capable_by_placement[placement] = len(agent_resolution.sdk_capable_nodes(session, node_ids, placement=placement))
    capable = capable_by_placement[placement]
    ref = str(row.id)
    retired = unsupported(row.manifest)
    # An SDK v1 agent cannot run: never shown enabled, and its Router line names why rather than the plan's sentence.
    router_out = RouterOut(eligible=False, reason=retired) if retired is not None else _router(session, org_id, ref)
    return MarketplaceEntryOut(
        kind=kind, id=row.id, display_name=row.display_name, description=row.description or "",
        personas=_persona_out(list(row.personas or [])), cost_band=None,
        availability=AvailabilityOut(enabled=enabled and retired is None, capable_nodes_online=capable,
                                     quota_state="not_applicable"),
        router=router_out, ledger=_ledger(session, org_id, ref, ledger_shown),
        enable_refusal=agent_resolution.cell_refusal(session, row, org_id), removed=row.status == STATUS_REMOVED,
        unsupported=retired,
    )


@router.get("/api/agent-marketplace", response_model=list[MarketplaceEntryOut])
@feature("agents", "view")
async def list_agent_marketplace(
    db: DbDep,
    caller: AsyncCaller = Depends(current_caller),
) -> list[MarketplaceEntryOut]:
    """Built-ins, then the org's own agents, the catalog and its registry installs, each by name."""
    catalog = await _catalog_rows_async()
    return await db.run_sync(_list_agent_marketplace, principal=caller.principal, catalog=catalog)


def _list_agent_marketplace(
    session: Session,
    principal: Principal,
    catalog: list[Agent],
) -> list[MarketplaceEntryOut]:
    org_id = principal.org_id
    if org_id is None:
        raise HTTPException(status_code=403, detail="The marketplace lists one organization's agents")
    nodes = _live_org_nodes(session, org_id)
    node_ids = [str(row.id) for row in nodes]
    ledger_shown = principal.is_operator or plan_terms.switch_on(session, org_id, plan_terms.AGENT_PERFORMANCE)
    out = _builtins(session, org_id, nodes, ledger_shown)
    own = list(session.scalars(
        select(Agent).where(Agent.org_id == org_id, Agent.is_catalog.is_(False), Agent.status != STATUS_REMOVED)
        .order_by(Agent.display_name, Agent.slug)
    ))
    capable: dict[str, int] = {}
    out += [_agent_entry(session, org_id, row, "org_agent", row.status == "active", node_ids, capable, ledger_shown)
            for row in own if not acp_registry.is_registry(row)]
    out += [_agent_entry(session, org_id, row, "catalog", enabled, node_ids, capable, ledger_shown)
            for row, enabled in _visible_catalog(session, principal, catalog)]
    out += [_agent_entry(session, org_id, row, "registry", row.status == "active", node_ids, capable, ledger_shown)
            for row in own if acp_registry.is_registry(row)]
    return out
