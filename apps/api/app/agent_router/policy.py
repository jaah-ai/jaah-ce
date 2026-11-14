"""The router's policy: which Config a task's next dispatch should run (architecture §4.4).

Thompson sampling over the placeable candidates (`candidates.py`): each draws a survival probability from
Beta(1 + survived, 1 + n − survived) over its ledger statistics (`ledger_stats.py`) and is scored per expected dollar;
the highest draw is the router's pick. `greedy` is the best expected survival per dollar among MATURE candidates
(n ≥ `MIN_OUTCOMES`, inside the project's latency cap); a pick that is not the greedy one is `exploratory`. Exploration
is capped by the project's `experiment_budget_share` (A1): when the pick's expected dollars would take the share of the
project's router-chosen spend that was exploratory over the last 30 days (`decisions.routed_spend`, reservations
included) above the knob, the greedy pick is taken instead (`exploration_capped`); a knob of 0 never explores. The draw is seeded from the task, attempt and inputs, so the same
pending row always yields the same pick (architecture AD4).

`router_pick` is always the router's own choice; `used = 'router'` only when it acts. It falls back to the precedence
chain (`used = 'default'`), in this order: persona not routed, playbook stage, a person's or an automation's pin, a
vendor binding, a pick that could not be placed in time (AD16), then — for a review child whose project runs Blind review
preferring another vendor — the A9 rule acts in every mode; otherwise Shadow mode, a single placeable candidate, no candidate
with enough data. In Recommend the pick waits for a person to accept it.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import agent_providers, agent_resolution, model_catalog, model_prices
from app.agent_router import candidates, decisions, features, ledger_stats, modes, reason, review_lanes
from app.agent_router.decisions import PickDraft
from app.agent_router.personas import REVIEW_PERSONAS, ROUTED_PERSONAS
from app.agent_router.policy_settings import OrgRoutingPolicy, org_policy
from app.models.orgs import Org
from app.models.projects import Project
from app.models.tasks import PIN_HUMAN, PIN_SYSTEM, Task

#: How long a pick the router acts on may wait unleased before it is given up (architecture AD16).
ROUTER_UNPLACED_FALLBACK_SECS = 600

#: A rough token profile of one attempt per complexity bucket, for a configuration the ledger cannot price yet:
#: millions of tokens per class (`model_prices.TOKEN_CLASSES`) of an XS task, scaled per bucket.
_ESTIMATE_MTOK = {"input": Decimal("0.3"), "cache_write_5m": Decimal("0.3"), "cache_read": Decimal("3"),
                  "output": Decimal("0.08")}
_BUCKET_SCALE = {"XS": 1, "S": 2, "M": 4, "L": 8}


@dataclass(frozen=True)
class Prepared:
    """What a pick reads before it decides: the features (availability included) and the candidate records."""

    features: dict[str, Any]
    entries: list[dict[str, Any]]
    policy: OrgRoutingPolicy

    def available(self) -> list[str]:
        return [decisions.config_key(e["config"]) for e in self.entries if e["eligible"]]


@dataclass(frozen=True)
class PickContext:
    """What the caller already read for this task, so `pick` reads nothing twice."""

    mode: modes.Mode
    default_pick: dict
    held: bool
    inputs_hash: str = ""
    prepared: Optional[Prepared] = None
    #: Config keys whose router pick was not placed in time for this attempt (AD16).
    unplaced: frozenset[str] = frozenset()
    memo: dict = field(default_factory=dict)
    #: Pick as if no person or automation had pinned the runner: what the router would recommend (the task form's
    #: preview, and the record of a person's answer to it).
    ignore_pin: bool = False


def prepare(session: Session, task: Task, project: Project, org: Org, memo: dict) -> Prepared:
    """What every pass reads for every task, since the inputs hash needs it: the candidates and their availability
    (a routed persona only). The rest of the features are read by `pick` alone, which runs only when a row is
    written (`complete`)."""
    policy = memo.get(("policy", org.id)) or memo.setdefault(("policy", org.id), org_policy(session, org.id))
    feats: dict[str, Any] = {"persona": task.persona, "availability": {}}
    entries: list[dict[str, Any]] = []
    if task.persona in ROUTED_PERSONAS:
        entries, feats["availability"] = candidates.candidate_set(session, task, project, org, policy, memo)
    return Prepared(features=feats, entries=entries, policy=policy)


def complete(session: Session, task: Task, project: Project, prepared: Prepared, memo: dict) -> Prepared:
    """`prepared` with the complexity, policy and stratum features read in, unless it carries them already."""
    if "stratum" in prepared.features:
        return prepared
    complexity = features.complexity(session, task, memo)
    feats = {
        "persona": task.persona, "complexity": complexity,
        "policy": features.policy_features(session, project.org_id, project, prepared.policy, memo),
        "availability": prepared.features.get("availability", {}),
        "stratum": features.stratum(task.persona, complexity),
    }
    return Prepared(features=feats, entries=prepared.entries, policy=prepared.policy)


def fallback_reason(task: Task, *, held: bool, ignore_pin: bool = False) -> Optional[str]:
    """Why the router may not act on `task` whatever its candidates (architecture §4.4), or None."""
    if task.persona not in ROUTED_PERSONAS:
        return "persona_not_routed"
    if task.playbook_stage is not None:
        return "playbook"
    if task.pin_source == PIN_HUMAN and not ignore_pin:
        return "human_pinned"
    if task.pin_source == PIN_SYSTEM and not ignore_pin:
        return "system_pinned"
    if held or (task.bound_provider and not (ignore_pin and task.pin_source is not None)):
        # Ignoring a pin ignores the vendor binding it wrote; a vendor the claim stamped still binds.
        return "vendor_bound"
    return None


def seed(task_id: Optional[int], attempt: int, inputs: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{task_id}:{attempt}:{inputs}".encode()).digest()[:8], "big")


def pick(session: Session, task: Task, project: Project, *, attempt: int,
         context: Optional[PickContext] = None) -> PickDraft:
    """The decision for `task`'s attempt `attempt`. `context` carries what the caller already read; without it,
    it is read here. A coder's picks carry its in-session review lanes (`review_lanes.with_lanes`), at its declared
    review tier, Standard until one is declared."""
    draft = _pick(session, task, project, attempt=attempt, context=context)
    if task.persona != review_lanes.CODER:
        return draft
    return replace(draft, router_pick=review_lanes.with_lanes(draft.router_pick, task.review_tier),
                   default_pick=review_lanes.with_lanes(draft.default_pick, task.review_tier))


def _pick(session: Session, task: Task, project: Project, *, attempt: int,
          context: Optional[PickContext] = None) -> PickDraft:
    if context is None:
        context = _context(session, task, project)
    default = context.default_pick
    org = session.scalars(select(Org).where(Org.id == project.org_id)).one()
    prepared = complete(session, task, project,
                        context.prepared or prepare(session, task, project, org, context.memo), context.memo)
    feats = prepared.features
    early = fallback_reason(task, held=context.held, ignore_pin=context.ignore_pin)
    if early == "persona_not_routed":
        return _draft(default, default, used="default", why=early, entries=[decisions.candidate(default)],
                      feats=feats, text=reason.fallback(session, early, default, persona=task.persona))

    stats_source = ledger_stats.current()
    scored = _score(session, task, project, prepared, context, stats_source, attempt)
    router_pick, greedy, sampled = scored["pick"] or default, scored["greedy"], scored["pick"]
    exploratory = sampled is not None and (greedy is None or decisions.config_key(sampled) != decisions.config_key(greedy))
    entries, eligible = scored["entries"], [e for e in scored["entries"] if e["eligible"]]
    # "Prefer a different vendor" is a sub-switch of Blind review (architecture §3.3): off with it.
    blind = task.persona in REVIEW_PERSONAS and bool(project.blind_review and project.blind_review_other_vendor)
    other = None
    author = _author_config(session, task) if blind else None
    if blind:
        other = candidates.other_vendor(session, entries, author, prepared.policy)
        router_pick, exploratory = other or author or default, False
    cost = _cost_of(entries, router_pick)
    common = dict(entries=entries, feats=feats, exploratory=exploratory, cost=cost,
                  stats_version=stats_source.version)

    why = early or ("candidate_unavailable" if context.unplaced else None)
    if why is not None:
        return _draft(router_pick, default, used="default", why=why,
                      text=reason.fallback(session, why, default, pick=router_pick), **common)
    if blind:
        return _draft(router_pick, default, used="router", why=None,
                      text=reason.review(session, router_pick, other=other is not None, author_known=author is not None),
                      **common)
    if context.mode == modes.Mode.SHADOW:
        why = "mode_off"
    elif not eligible:
        return _draft(router_pick, default, used="default", why="candidate_unavailable",
                      text=reason.fallback(session, "none_placeable", default), **common)
    elif len(eligible) == 1:
        why = "allowed_set_one"
    elif greedy is None:
        why = "data_below_minimum"
    if why is not None:
        return _draft(router_pick, default, used="default", why=why,
                      text=reason.fallback(session, why, default, pick=router_pick), **common)
    capped = exploratory and over_budget(session, project, task, cost)
    if capped:
        router_pick, exploratory = greedy, False
        common.update(exploratory=False, cost=_cost_of(entries, router_pick), capped=True)
    text = _chosen_reason(session, scored, router_pick, feats, exploratory)
    if capped:
        text = f"{text} {reason.EXPLORATION_CAPPED}"
    if context.mode == modes.Mode.RECOMMEND:
        return _draft(router_pick, default, used="default", why=None,
                      text=f"{text} {reason.recommended(session, router_pick, default)}", **common)
    return _draft(router_pick, default, used="router", why=None, text=text, **common)


def _draft(router_pick: dict[str, Any], default: dict[str, Any], *, used: str, why: Optional[str],
           entries: list[dict[str, Any]], feats: dict[str, Any], text: str, exploratory: bool = False,
           cost: Optional[Decimal] = None, stats_version: str = decisions.NO_STATS, capped: bool = False) -> PickDraft:
    return PickDraft(
        router_pick=router_pick, default_pick=default, used=used, fallback_reason=why, candidates=entries,
        # A Recommend pick (`default` with no fallback) keeps its flag: accepted, it explores (finalize clears it
        # when the default ran).
        features=feats, reason=text, exploratory=exploratory and (used == "router" or why is None),
        exploration_capped=capped,
        expected_cost_usd=cost.quantize(Decimal("0.0001")) if cost is not None else None,
        stats_version=stats_version,
    )


def over_budget(session: Session, project: Project, task: Task, cost: Optional[Decimal]) -> bool:
    """Whether an exploratory pick costing `cost` would take the project's exploratory share of its router-chosen
    spend above `experiment_budget_share` (A1). An unpriced pick adds no dollars."""
    share = Decimal(project.experiment_budget_share if project.experiment_budget_share is not None else "0.1")
    if share <= 0:
        return True
    spent, total = decisions.routed_spend(session, project.id, task.persona, exclude_task=task.id)
    cost = cost or Decimal(0)
    if total + cost <= 0:
        return False
    return (spent + cost) / (total + cost) > share


# ── Scoring ─────────────────────────────────────────────────────────────────────────────────────────


def _score(session: Session, task: Task, project: Project, prepared: Prepared, context: PickContext,
           stats_source: ledger_stats.LedgerStats, attempt: int) -> dict[str, Any]:
    """Each candidate's statistics, cost, expected and sampled survival per dollar; the greedy and sampled picks."""
    entries = [dict(e) for e in prepared.entries]
    keys = [decisions.config_key(e["config"]) for e in entries]
    stats = stats_source.config_stats(session, task.org_id, prepared.features["stratum"], keys) if keys else {}
    bucket = prepared.features["complexity"]["bucket"]
    prices = features.remember(context.memo, "prices", lambda: _prices(session))
    fallback_cost = _estimate(context.default_pick, task.persona, bucket, prices)
    cap = project.routing_latency_cap_hours
    for key, item in zip(keys, entries, strict=True):
        s = stats.get(key, ledger_stats.NO_DATA)
        item["stats"] = s.as_record()
        if key in context.unplaced:
            item.update(eligible=False, excluded_reason=candidates.UNPLACED)
        elif cap is not None and s.p50_secs is not None and s.p50_secs > float(cap) * 3600:
            item.update(eligible=False, excluded_reason=candidates.LATENCY_CAP)
        cost, estimated = (s.cost_usd, False) if s.n >= ledger_stats.MIN_OUTCOMES and s.cost_usd else (
            _estimate(item["config"], task.persona, bucket, prices), True)
        if cost is None:
            # An unpriced runner is costed at the precedence chain's runner's estimate (architecture §4.4).
            cost = fallback_cost
        item["cost_usd"], item["cost_estimated"] = (str(cost) if cost is not None else None), estimated
    eligible = [(key, item) for key, item in zip(keys, entries, strict=True) if item["eligible"]]
    # One unit for every candidate: dollars when each has a cost, else survival alone — never dollars for some and a
    # nominal 1 for others, which would rank on units rather than data.
    priced = all(item["cost_usd"] is not None and Decimal(item["cost_usd"]) > 0 for _, item in eligible)
    # A recommendation (`ignore_pin`) is seeded on its draft alone, so the form's preview of a task not yet saved and
    # the saved task draw the same pick.
    rng = random.Random(seed(None if context.ignore_pin else task.id, attempt, context.inputs_hash))
    greedy = sampled = None
    best_mean = best_draw = None
    for key, item in eligible:  # config_key order (AD4)
        s = stats.get(key, ledger_stats.NO_DATA)
        per = float(item["cost_usd"]) if priced else 1.0
        mean = (1 + s.survived) / (2 + s.n)
        draw = rng.betavariate(1 + s.survived, 1 + s.n - s.survived)
        item["sample"] = round(draw / per, 6)
        if s.n >= ledger_stats.MIN_OUTCOMES:
            item["score"] = round(mean / per, 6)
            if best_mean is None or mean / per > best_mean:
                best_mean, greedy = mean / per, item["config"]
        if best_draw is None or draw / per > best_draw:
            best_draw, sampled = draw / per, item["config"]
    return {"entries": entries, "greedy": greedy, "pick": sampled, "stats": stats}


def _prices(session: Session) -> dict[str, dict[str, Decimal]]:
    from app.models.metering import ModelPrice

    return model_prices.prices_by_model(session.scalars(select(ModelPrice)))


def _estimate(cfg: dict[str, Any], persona: str, bucket: str, prices: dict[str, dict[str, Decimal]]
              ) -> Optional[Decimal]:
    """One attempt's dollars from the bucket's token profile at the tier's family rates; None when the runner is
    not one Jaah prices (an agent, a vendor without a rate row)."""
    from app.persona_registry import PERSONA_CHARTER

    if cfg.get("kind") != decisions.KIND_BUILTIN:
        return None
    tier = cfg.get("tier") or PERSONA_CHARTER.get(persona, ("", candidates.REVIEW_TIER))[1]
    family = model_catalog.TIER_FAMILY.get(tier, "")
    # Rates are per model family; a family is priced only for the provider whose models it names.
    if cfg.get("provider") and agent_providers.provider_of_model(family) != cfg["provider"]:
        return None
    if family not in prices:
        return None
    scale = _BUCKET_SCALE.get(bucket, 1)
    usage = {family: {cls: int(mtok * scale * 1_000_000) for cls, mtok in _ESTIMATE_MTOK.items()}}
    return model_prices.cost_usd(usage, prices)[0]


def _cost_of(entries: list[dict[str, Any]], cfg: dict[str, Any]) -> Optional[Decimal]:
    key = decisions.config_key(cfg)
    for item in entries:
        if decisions.config_key(item["config"]) == key and item.get("cost_usd") is not None:
            return Decimal(item["cost_usd"])
    return None


def _chosen_reason(session: Session, scored: dict[str, Any], pick_cfg: dict[str, Any], feats: dict[str, Any],
                   exploratory: bool) -> str:
    key = decisions.config_key(pick_cfg)
    by_key = {decisions.config_key(e["config"]): e for e in scored["entries"]}
    mine = by_key.get(key, {})
    rivals = [e for k, e in by_key.items() if k != key and e.get("score") is not None]
    rival = max(rivals, key=lambda e: e["score"]) if rivals else None
    prior = any(s.source == "prior" for s in scored["stats"].values())
    return reason.chosen(
        session, pick_cfg, mine.get("score"), language=feats["complexity"]["repo_language"], prior=prior,
        rival=rival["config"] if rival else None, rival_score=rival["score"] if rival else None,
        exploratory=exploratory,
    )


def _author_config(session: Session, task: Task) -> Optional[dict[str, Any]]:
    """What the coder task a review child reviews ran on: its latest final decision's `actual`, else its stamps."""
    from app.models.routing import FINAL, RoutingDecision

    if task.parent_task_id is None:
        return None
    actual = session.scalars(
        select(RoutingDecision.actual).where(RoutingDecision.task_id == task.parent_task_id,
                                             RoutingDecision.status == FINAL)
        .order_by(RoutingDecision.finalized_at.desc()).limit(1)
    ).first()
    if actual:
        return {**decisions.config(kind=actual.get("kind"), provider=actual.get("provider"),
                                   tier=actual.get("tier")), "agent_id": actual.get("agent_id")}
    parent = session.scalars(select(Task).where(Task.id == task.parent_task_id)).first()
    if parent is None:
        return None
    if parent.run_agent_id is not None:
        return decisions.config(kind=decisions.KIND_AGENT, agent_id=parent.run_agent_id)
    if parent.bound_provider:
        return decisions.config(kind=decisions.KIND_BUILTIN, provider=parent.bound_provider,
                                tier=parent.dispatched_tier)
    return None


def _context(session: Session, task: Task, project: Project, **extra: Any) -> PickContext:
    from app import slot_state

    org = session.scalars(select(Org).where(Org.id == project.org_id)).one()
    choice = agent_resolution.resolve_agent(session, task, project)
    return PickContext(
        mode=modes.effective_mode(session, org, project),
        default_pick=decisions.config_of_choice(choice, task),
        held=task.id is not None and slot_state.task_hold(session, task.id) is not None,
        **extra,
    )


def recommendation(session: Session, task: Task, project: Project, *, inputs_hash: str = "",
                   memo: Optional[dict] = None) -> PickDraft:
    """What the router would recommend for `task` were nobody's pin on it: `pick` with `ignore_pin`, read-only.
    Seeded from `inputs_hash`, so the same draft always shows the same pick."""
    attempt = (task.dispatch_attempts or 0) + 1
    context = _context(session, task, project, ignore_pin=True, inputs_hash=inputs_hash, memo=memo or {})
    return pick(session, task, project, attempt=attempt, context=context)
