"""The in-session review lanes a coder runs on its own diff before its PR — THE SINGLE HOME of the lane set per review
tier and of how a lane is spelled for the runner's harness.

The lanes per tier follow the code-review policy's "Autonomous review profile" (this module reads that
table, never redefines a tier); a task with no declared `review_tier` runs the Standard lanes. The router records the
lanes on every coder pick (`router_pick.review.lanes`, `[{lane, tier}]`, provider-neutral); the composer names them in
the coder's Start-here block on the AUTHOR's harness: a lane runs inside the runner's own session, so it can only use
a model that harness serves — each built-in on its own provider's tiers, and an SDK agent (whose harness is not ours)
gets no line. The runner reports what it actually ran in its run telemetry
(`review_lanes`, validated by `ReviewLaneIn` at `routes/agent/fleet_observe.py`).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import Field

from app import agent_providers, model_catalog
from app.entity_shape import StrictModel

#: The persona whose dispatch carries review lanes.
CODER = "coder"
#: The tier a task runs the lanes of until its runner declares one.
DEFAULT_TIER = "standard"
#: Each review tier's in-session lanes, in run order (code-review-policy §Autonomous review profile).
TIER_LANES: dict[str, tuple[str, ...]] = {
    "trivial": (),
    "minor": ("code-reviewer",),
    "standard": ("code-reviewer", "verifier"),
    "major": ("critic", "code-reviewer", "verifier"),
    "security": ("critic", "code-reviewer", "security-reviewer", "verifier"),
}
#: The model tier every lane runs at, resolved on the author's harness.
LANE_TIER = model_catalog.TIER_HIGH
#: The subagent a harness whose account layer is OMC (`agent_providers.account_layer`) spawns for a lane. Another
#: harness has no named subagent: its lane is a fresh session of its own on the named model.
_OMC_LAYER, _OMC_AGENT = "omc", "oh-my-claudecode:{lane}"
#: A lane in words, for the console.
LANE_LABELS: dict[str, str] = {
    "code-reviewer": "Code reviewer",
    "verifier": "Verifier",
    "critic": "Critic",
    "security-reviewer": "Security reviewer",
}


class ReviewLaneIn(StrictModel):
    """One review lane a run actually ran, as its telemetry reports it."""

    lane: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    #: The subagent or agent that ran the lane; null when the harness names none.
    agent_id: Optional[str] = Field(default=None, max_length=200)


def lanes_for(tier: Optional[str]) -> list[dict[str, Any]]:
    """The provider-neutral lanes of `tier` (Standard when undeclared or unknown), as the decision records them."""
    names = TIER_LANES.get(tier or DEFAULT_TIER, TIER_LANES[DEFAULT_TIER])
    return [{"lane": name, "tier": LANE_TIER} for name in names]


def with_lanes(cfg: dict[str, Any], tier: Optional[str]) -> dict[str, Any]:
    """A copy of Config `cfg` whose review carries `tier`'s lanes; the original is never mutated."""
    review = dict(cfg.get("review") or {})
    review["lanes"] = lanes_for(tier)
    review.setdefault("blind", [])
    return {**cfg, "review": review}


def for_author(lanes: list[dict[str, Any]], *, agent_run: bool, provider: str,
               resolve_model: Callable[[str], str]) -> list[dict[str, Any]]:
    """`lanes` filtered to the author's harness: `[{lane, provider, model, agent_id}]` on `provider`'s own models
    (`resolve_model(tier)` → its concrete model, or "" when it cannot say: `model` is then "", and the line names
    the tier instead), none for an SDK agent run or an unknown provider."""
    record = agent_providers.by_value(provider) if provider else None
    if agent_run or record is None:
        return []
    omc = agent_providers.account_layer(record.value) == _OMC_LAYER
    out = []
    for lane in lanes:
        name, tier = str(lane.get("lane") or ""), str(lane.get("tier") or LANE_TIER)
        if name not in LANE_LABELS:
            continue
        out.append({
            "lane": name, "provider": record.value, "model": resolve_model(tier) or "", "tier": tier,
            "agent_id": _OMC_AGENT.format(lane=name) if omc else None,
        })
    return out


def prompt_line(lanes: list[dict[str, Any]]) -> str:
    """The Start-here "Review lanes:" line for `lanes` (`for_author`'s output); "" when there are none."""
    if not lanes:
        return ""
    parts = []
    for lane in lanes:
        who = f"subagent `{lane['agent_id']}`" if lane.get("agent_id") else "a fresh session of your own harness"
        model = f"model `{lane['model']}`" if lane.get("model") else f"your harness's {lane.get('tier') or LANE_TIER} tier"
        parts.append(f"{lane['lane']} — {who} on {model}")
    return (
        "  Review lanes (from this dispatch's routing decision; blind — each gets the diff and the spec, never "
        "your rationale): " + "; ".join(parts) + ". Report the lanes you ran in `telemetry_publish`'s payload as "
        "`review_lanes: [{lane, provider, model, agent_id}]` (`agent_id` null when none)."
    )
