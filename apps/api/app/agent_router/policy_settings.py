"""The org's routing policy — which providers and SDK agents the router may pick, and the model tier range.

Read from `Org.routing_allowed` (NULL = every provider and agent; else `{"providers": [...]|null,
"agent_ids": [...]|null}`, a null half = all of that half) and `Org.routing_tier_floor` / `routing_tier_ceiling`
(`app/model_catalog.py` tiers, NULL = no bound). `validated` is the one writer-side check (`routes/orgs.py`).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app import agent_providers, agent_resolution, model_catalog
from app.models.orgs import Org


@dataclass(frozen=True)
class OrgRoutingPolicy:
    allowed_providers: Optional[frozenset[str]]
    allowed_agent_ids: Optional[frozenset[UUID]]
    tier_floor: Optional[str]
    tier_ceiling: Optional[str]
    #: A digest of the four, so a decision record names the policy it was made under.
    version: str


def org_policy(session: Session, org_id: UUID) -> OrgRoutingPolicy:
    """The org's routing policy; an unknown org reads the unrestricted policy."""
    org = session.get(Org, org_id)
    allowed = (org.routing_allowed if org is not None else None) or {}
    providers = allowed.get("providers")
    agent_ids = allowed.get("agent_ids")
    floor = org.routing_tier_floor if org is not None else None
    ceiling = org.routing_tier_ceiling if org is not None else None
    canonical = json.dumps(
        {"providers": sorted(providers) if providers is not None else None,
         "agent_ids": sorted(agent_ids) if agent_ids is not None else None,
         "floor": floor, "ceiling": ceiling},
        sort_keys=True,
    )
    return OrgRoutingPolicy(
        allowed_providers=frozenset(providers) if providers is not None else None,
        allowed_agent_ids=frozenset(UUID(value) for value in agent_ids) if agent_ids is not None else None,
        tier_floor=floor,
        tier_ceiling=ceiling,
        version=hashlib.sha256(canonical.encode()).hexdigest()[:12],
    )


#: The most entries one half of the allowed runners may list.
ALLOWED_MAX = 200


def validated_allowed(
    session: Session, org_id: UUID, value: Optional[dict[str, Any]], stored: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """`value` as stored: None for "all"; else both halves, each a sorted list or None. ValueError naming the
    first problem. An agent id already in `stored` that the org can no longer use is dropped, not refused, so a
    disabled agent never wedges the policy."""
    if value is None:
        return None
    unknown_keys = set(value) - {"providers", "agent_ids"}
    if unknown_keys:
        raise ValueError(f"Allowed runners take providers and agent_ids only, not {', '.join(sorted(unknown_keys))}.")
    for half in ("providers", "agent_ids"):
        entries = value.get(half)
        if entries is not None and (
            not isinstance(entries, list) or len(entries) > ALLOWED_MAX or not all(isinstance(e, str) for e in entries)
        ):
            raise ValueError(f"{half} is a list of at most {ALLOWED_MAX} names, or null.")
    providers = value.get("providers")
    agent_ids = value.get("agent_ids")
    if providers is None and agent_ids is None:
        return None
    if providers is not None:
        bad = [p for p in providers if p not in agent_providers.PROVIDER_VALUES]
        if bad:
            raise ValueError(f"{', '.join(map(str, bad))} is not a built-in runner.")
        providers = sorted(set(providers))
    if agent_ids is not None:
        before = set((stored or {}).get("agent_ids") or ())
        ids = []
        for raw in agent_ids:
            try:
                agent_id = UUID(str(raw))
            except ValueError:
                raise ValueError(f"{raw} is not an agent id.") from None
            agent = agent_resolution.load_agent(session, agent_id)
            if agent is None or not agent_resolution.visible_to(session, agent, org_id):
                if str(agent_id) in before:
                    continue
                raise ValueError("One of the allowed agents is not an agent this organization can use.")
            ids.append(str(agent_id))
        agent_ids = sorted(set(ids))
    if providers == [] and agent_ids == []:
        raise ValueError("Allow at least one runner, or allow all.")
    return {"providers": providers, "agent_ids": agent_ids}


def validated_tiers(floor: Optional[str], ceiling: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The tier range as stored (each a tier id or None). ValueError when a value names no tier or the floor sits
    above the ceiling."""
    order = model_catalog.MODEL_IDENTIFIERS   # most capable first
    for value in (floor, ceiling):
        if value is not None and value not in order:
            raise ValueError(f"{value} is not a model tier. Use one of {', '.join(order)}.")
    if floor is not None and ceiling is not None and order.index(floor) < order.index(ceiling):
        raise ValueError("The lowest tier can't be above the highest tier.")
    return floor, ceiling
