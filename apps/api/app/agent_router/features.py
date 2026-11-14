"""What the router knows about a task when it picks (architecture §4.2) — THE SINGLE HOME of the complexity bucket
the router strata use, and of the availability read.

    {persona,
     complexity: {body_chars, files_named, repo_language, clarity_score, similar_prior, bucket: XS|S|M|L},
     policy: {allowed, tier_floor, tier_ceiling, budget: {state, percent, headroom_usd} | null},
     availability: {<config_key>: {accounts_gate: ok|tiered|blocked, five_h_pct, seven_d_pct,
                                   capable_nodes_online, sdk_capable, placement_ok}},
     stratum: "<persona>|<bucket>|<repo_language>"}

Everything here READS. Availability asks `_best_account`'s own gate (`routes/fleet.py::gate_accounts`, over
`jaah_scheduler.gate_and_tier`) for the task's granted accounts, and keeps only its verdict: placement itself stays `_best_account`'s (architecture D3, AD10).
`memo` is one dict per lease pass (`refresh_picks`), so the account set, each org's thresholds and budgets, and each
project's nodes are read once per pass, not once per task.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Callable, Optional, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import account_auth, agent_providers, agent_resolution
from app.agent_router import decisions
from app.agent_router.policy_settings import OrgRoutingPolicy
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.outcome_ledger import dimensions

T = TypeVar("T")

#: The ledger's complexity buckets (`outcome_ledger.dimensions`), so a stratum names the ledger rows it reads.
BUCKETS: tuple[str, ...] = dimensions.BUCKETS
#: Body characters a task of each bucket says, beside the ledger's own files-changed edge for it.
_MAX_BODY_CHARS: tuple[int, ...] = (1500, 4000, 10000)
#: `(bucket, max body characters, max files named)` — the first that holds both wins; anything larger is `L`.
_EDGES: tuple[tuple[str, int, int], ...] = tuple(
    (name, chars, max_files) for (name, _size, max_files), chars in zip(dimensions._EDGES, _MAX_BODY_CHARS, strict=True)
)
#: How many of the project's latest done tasks `similar_prior` reads.
PRIOR_WINDOW = 500
#: The stratum's spelling of an unknown language.
ANY_LANGUAGE = "-"

GATE_OK, GATE_TIERED, GATE_BLOCKED = "ok", "tiered", "blocked"

_TOKEN_SPLIT = re.compile(r"[\s`'\"()\[\]<>{},;|]+")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_PATHLIKE = re.compile(r"^[\w.@+-]+(?:/[\w.@+-]+)*$")


def bucket(body_chars: int, files_named: int, clarity: Optional[float] = None) -> str:
    """`XS`/`S`/`M`/`L` — the ledger's buckets — from what the task says before it runs: its body's length and the
    files it names, bumped one class when its epic's clarity score is below the ledger's bump threshold (the work is
    harder than it reads)."""
    name = next((b for b, max_chars, max_files in _EDGES if body_chars <= max_chars and files_named <= max_files),
                BUCKETS[-1])
    if clarity is not None and clarity < dimensions.CLARITY_BUMP_BELOW:
        name = BUCKETS[min(BUCKETS.index(name) + 1, len(BUCKETS) - 1)]
    return name


def files_named(body: str) -> list[str]:
    """The distinct repo-path-shaped tokens in `body`, in first-seen order: a relative path or a bare file name with
    an extension the ledger knows a language for; a `:line` suffix is dropped; URLs are not paths."""
    seen: dict[str, None] = {}
    for raw in _TOKEN_SPLIT.split(body or ""):
        token = _LINE_SUFFIX.sub("", raw.rstrip(".:!?"))
        if not token or "://" in raw or token.startswith("/") or not _PATHLIKE.match(token):
            continue
        extension = ("." + token.rsplit(".", 1)[1].lower()) if "." in token.rsplit("/", 1)[-1] else ""
        if extension in dimensions.LANGUAGE_BY_EXTENSION:
            seen.setdefault(token, None)
    return list(seen)


def remember(memo: dict, key: Any, read: Callable[[], T]) -> T:
    """`memo[key]`, read once per pass."""
    if key not in memo:
        memo[key] = read()
    return memo[key]


def complexity(session: Session, task: Task, memo: dict) -> dict[str, Any]:
    body = task.body or ""
    paths = files_named(body)
    clarity = remember(memo, ("clarity", task.parent_task_id), lambda: _epic_clarity(session, task.parent_task_id))
    name = bucket(len(body), len(paths), clarity)
    priors = remember(memo, ("priors", task.project_id, task.persona),
                   lambda: _prior_buckets(session, task.project_id, task.persona))
    return {
        "body_chars": len(body), "files_named": len(paths), "repo_language": dimensions.language_of(paths),
        "clarity_score": clarity, "similar_prior": priors.get(bucket(len(body), len(paths)), 0), "bucket": name,
    }


def _epic_clarity(session: Session, parent_id: Optional[int]) -> Optional[float]:
    """The clarity score the parent epic's business-analysis stage (or a new web app's App Wizard) settled with, on the
    gate's 0–1 scale."""
    if parent_id is None:
        return None
    score = session.scalars(
        select(Task.clarity_score).where(Task.parent_task_id == parent_id, Task.persona.in_(("business_analysis", "app_wizard")),
                                         Task.archived_at.is_(None), Task.clarity_score.is_not(None))
        .order_by(Task.id.desc()).limit(1)
    ).first()
    return dimensions.clarity(score)


def _prior_buckets(session: Session, project_id: int, persona: str) -> dict[str, int]:
    """`{bucket: count}` over the project's latest done tasks of `persona`, bucketed on body alone."""
    counts: dict[str, int] = {}
    for body in session.scalars(
        select(Task.body).where(Task.project_id == project_id, Task.persona == persona, Task.state == "done")
        .order_by(Task.id.desc()).limit(PRIOR_WINDOW)
    ):
        name = bucket(len(body or ""), len(files_named(body or "")))
        counts[name] = counts.get(name, 0) + 1
    return counts


def stratum(persona: str, complexity_features: dict[str, Any]) -> str:
    return f"{persona}|{complexity_features['bucket']}|{complexity_features['repo_language'] or ANY_LANGUAGE}"


def policy_features(session: Session, org_id: Any, project: Project, policy: OrgRoutingPolicy,
                    memo: dict) -> dict[str, Any]:
    allowed = None
    if policy.allowed_providers is not None or policy.allowed_agent_ids is not None:
        allowed = {
            "providers": sorted(policy.allowed_providers) if policy.allowed_providers is not None else None,
            "agent_ids": sorted(map(str, policy.allowed_agent_ids)) if policy.allowed_agent_ids is not None else None,
        }
    return {"allowed": allowed, "tier_floor": policy.tier_floor, "tier_ceiling": policy.tier_ceiling,
            "budget": remember(memo, ("budgets", org_id), lambda: _budgets(session, org_id)).get(project.id)}


def _budgets(session: Session, org_id: Any) -> dict[int, dict[str, Any]]:
    from app import budgets

    out = {}
    for status in budgets.statuses(session, org_id):
        if status.budget.project_id is None:
            continue
        headroom = Decimal(status.budget.monthly_usd_micros - status.spent_micros) / Decimal(1_000_000)
        out[status.budget.project_id] = {"state": status.state, "percent": status.percent,
                                         "headroom_usd": str(headroom.quantize(Decimal("0.01")))}
    return out


# ── Availability ────────────────────────────────────────────────────────────────────────────────────


def availability(session: Session, task: Task, project: Project, configs: list[dict[str, Any]],
                 memo: dict) -> dict[str, dict[str, Any]]:
    """`{config_key: facts}` for each of `configs`: may an account of its vendor run it now (the quota gates, read
    for the task's granted accounts), on how many of the project's live nodes, and so may it be placed."""
    nodes = remember(memo, ("nodes", project.id), lambda: _project_nodes(session, project.id))
    out: dict[str, dict[str, Any]] = {}
    per_provider: dict[str, dict[str, Any]] = {}
    for cfg in configs:
        key = decisions.config_key(cfg)
        if cfg["kind"] == decisions.KIND_AGENT:
            out[key] = _agent_availability(session, cfg, nodes, memo)
            continue
        provider = cfg["provider"]
        if provider not in per_provider:
            per_provider[provider] = _provider_availability(session, task, provider, nodes, memo)
        out[key] = per_provider[provider]
    return out


def _project_nodes(session: Session, project_id: int) -> dict[str, Any]:
    from app import pool_state

    live = pool_state.live_nodes_for_project(session, project_id)
    rows = session.execute(select(Node.id, Node.runner_clis, Node.microvm).where(Node.id.in_(live))).all() \
        if live else []
    return {"ids": live, "rows": rows, "microvm": bool(rows) and all(row.microvm for row in rows)}


def microvm_project(session: Session, project_id: int, memo: dict) -> bool:
    """Whether every live node hosting the project is a MicroVM node — the project's placement class."""
    return remember(memo, ("nodes", project_id), lambda: _project_nodes(session, project_id))["microvm"]


def _provider_availability(session: Session, task: Task, provider: str, nodes: dict[str, Any],
                           memo: dict) -> dict[str, Any]:
    from app import jaah_scheduler, placement_grants
    from app.routes import fleet

    accounts = remember(memo, "accounts", lambda: _accounts_by_org(session))
    allowed = placement_grants.allowed_accounts(session, task.id, memo=memo.setdefault("grants", {}))
    # `_best_account`'s own gate, asked read-only: the verdict is kept, the account is not.
    status, enabled, tier = fleet.gate_accounts(accounts.get(task.org_id, []), allowed=allowed, provider=provider,
                                                org_id=task.org_id)
    chosen = tier[0] if status == "tier" else enabled[0] if status == "cold_start" else None
    record = agent_providers.by_value(provider)
    kinds = record.auth_kinds if record is not None else (account_auth.AUTH_KIND_OAUTH,)
    gate = (GATE_BLOCKED if chosen is None
            else GATE_TIERED if jaah_scheduler.is_tiered_pick(chosen, kinds) else GATE_OK)
    binary = record.binary if record is not None else None
    # NULL `runner_clis` is "never attested", which asserts nothing (`fleet._node_cli_refusal`).
    capable = sum(1 for row in nodes["rows"] if row.runner_clis is None or binary in row.runner_clis)
    return {
        "accounts_gate": gate,
        "five_h_pct": _lowest(c.get("five_hour_percent") for c in enabled),
        "seven_d_pct": _lowest(c.get("weekly_percent") for c in enabled),
        "capable_nodes_online": capable, "sdk_capable": False,
        "placement_ok": gate != GATE_BLOCKED and capable > 0,
    }


def _accounts_by_org(session: Session) -> dict[Any, list[dict]]:
    """The fleet's account candidates, read nodeless (quota from every node's reading), grouped by org."""
    from app.routes import fleet

    org_of = {row.id: row.org_id for row in fleet.account_rows(session)}
    out: dict[Any, list[dict]] = {}
    for candidate in fleet._account_candidates(session):
        out.setdefault(org_of.get(candidate["account_id"]), []).append(candidate)
    return out


def _lowest(values) -> Optional[float]:
    present = [float(v) for v in values if v is not None]
    return min(present) if present else None


def _agent_availability(session: Session, cfg: dict[str, Any], nodes: dict[str, Any], memo: dict) -> dict[str, Any]:
    from uuid import UUID

    agent = agent_resolution.load_agent(session, UUID(cfg["agent_id"]))
    placement = agent_resolution.placement_class(agent) if agent is not None else agent_resolution.PLACE_UNSUPPORTED
    capable = len(remember(memo, ("sdk", tuple(nodes["ids"]), placement),
                        lambda: agent_resolution.sdk_capable_nodes(session, list(nodes["ids"]), placement=placement)))
    registry_on_vm = placement == agent_resolution.PLACE_REGISTRY and nodes["microvm"]
    return {"accounts_gate": GATE_OK, "five_h_pct": None, "seven_d_pct": None, "capable_nodes_online": capable,
            "sdk_capable": capable > 0, "placement_ok": capable > 0 and not registry_on_vm}
