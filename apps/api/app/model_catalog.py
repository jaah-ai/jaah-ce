"""The vendor-neutral model TIERS a Task, project or persona may choose, the one statement of model
precedence, and the one rule turning a tier into the concrete model a given CLI runs.

Single-homed because dispatch, the Tasks form select, the project card, the cron form and a REST
validator all read it; separate lists would ship a select that saves an unroutable value. A tier names a
CLASS of model, never a vendor's product, because which CLI runs a task is decided later, by the claimed
account (`accounts.provider` -> `tasks.bound_provider`); the user cannot know the vendor when choosing.

Tier -> concrete model is decided at dispatch, once the provider is known (`resolve_tier_model`), and it
must always land on the vendor's CURRENT model for that class with no admin action when a vendor ships a
new one. Two mechanisms, declared per provider on `app/agent_providers.py::AgentProvider.tier_aliases`:

  * a vendor-maintained MOVING alias (Claude's `opus`), which the vendor re-points itself; or
  * no alias (`tier_aliases == ()`): the dispatching node's own CLI reports its live model catalog
    (`nodes.model_catalogs`, sensed by `apps/jaahd/provider_models.py`) and `pick_from_catalog` applies
    the declared rule below. A new model in the catalog is picked on the next dispatch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class ModelChoice:
    """One selectable tier. `label` is what the console's select shows and `description` is its help
    text, so every form is rendered from this catalogue rather than from a second copy in the web app."""

    value: str
    label: str
    description: str


TIER_MAX = "max"
TIER_HIGH = "high"
TIER_STANDARD = "standard"
TIER_LOW = "low"

# Most capable first — the order the select renders, decided here so two surfaces cannot disagree.
MODELS: tuple[ModelChoice, ...] = (
    ModelChoice(
        value=TIER_MAX,
        label="Max",
        description="The vendor's most capable model, and the most expensive. Hardest work only.",
    ),
    ModelChoice(
        value=TIER_HIGH,
        label="High",
        description="The default for every analysis stage and for build work.",
    ),
    ModelChoice(
        value=TIER_STANDARD,
        label="Standard",
        description="Faster and cheaper. The default for the system personas' mechanical passes.",
    ),
    ModelChoice(
        value=TIER_LOW,
        label="Low",
        description="Cheapest. For genuinely mechanical, highly repetitive work.",
    ),
)

MODEL_IDENTIFIERS: tuple[str, ...] = tuple(choice.value for choice in MODELS)

# The Claude aliases the tiers replaced, 1:1. Read on INPUT only — a client or a stored row from before the
# tiers (additive while two releases overlap) still means exactly what it meant — never offered or stored.
# Revision 0274 (collapsed into the baseline) rewrote stored values with the same map.
LEGACY_ALIASES: Mapping[str, str] = {
    "fable": TIER_MAX,
    "opus": TIER_HIGH,
    "sonnet": TIER_STANDARD,
    "haiku": TIER_LOW,
}

# The sentinel, not a member of the set: empty means "do not pin" — defer to the stage default, then the
# project card. Absence is not a state.
INHERIT = ""

# Precedence over stored values, most specific first; `resolve_model` is the one implementation. A
# per-request `--model` override sits above this chain and is single-homed in
# `dispatch_compose.compose._resolve_model`.
MODEL_PRECEDENCE: tuple[str, ...] = (
    "tasks.model",              # this Task's own pin — an operator's explicit choice for this row
    "persona stage default",    # the persona record's own default (app/persona_registry.py)
    "projects.default_model",   # the project card's per-repo default
)

# A context-window suffix a CLI passes through on a model id (`deepseek-flash[1m]`): the same model.
# The pricing join shares it by value (`model_prices._CONTEXT_SUFFIX_RE`).
_CONTEXT_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")


def without_context_suffix(model: str | None) -> str:
    """`model` stripped of its context-window suffix and surrounding space (`deepseek-flash[1m]` →
    `deepseek-flash`): the suffix names the CLI's window, not the model. The one stripper — `tier_of`,
    `fold_foreign_id` and `llm_client.reads_images` all read through it."""
    return _CONTEXT_SUFFIX_RE.sub("", (model or "").strip())


def keep_context_suffix(concrete: str, requested: str | None) -> str:
    """`concrete` carrying the context suffix `requested` pinned (`opus[1m]` on a bare-alias provider
    stays `opus[1m]` — the suffix names the window, and dropping it would silently downgrade the
    request); unchanged when `concrete` already carries one (DeepSeek's aliases do)."""
    stripped_request = without_context_suffix(requested)
    suffix = (requested or "").strip()[len(stripped_request):]
    return concrete if not suffix or _CONTEXT_SUFFIX_RE.search(concrete) else concrete + suffix


def tier_of(value: str | None) -> str:
    """The tier `value` names — a tier id, or a legacy Claude alias folded 1:1, case-insensitively, with
    or without its context suffix (`opus[1m]` is Opus) — or `INHERIT` when it names none. The one
    reading of a stored or submitted model value."""
    candidate = without_context_suffix(value).lower()
    if candidate in MODEL_IDENTIFIERS:
        return candidate
    return LEGACY_ALIASES.get(candidate, INHERIT)


def validate_model(value: str | None) -> str:
    """Normalize a submitted model value: `INHERIT` for empty/None, else a tier id (a legacy alias is
    folded onto its tier).

    Raises `ValueError` naming the accepted set, so an unroutable value is refused at the REST edge."""
    candidate = (value or "").strip()
    if not candidate:
        return INHERIT
    tier = tier_of(candidate)
    if not tier:
        raise ValueError(
            f"unknown model tier {candidate!r}; expected one of {MODEL_IDENTIFIERS} or empty to inherit"
        )
    return tier


def resolve_model(
    task_model: str | None,
    stage_default: str | None,
    project_default: Callable[[], str],
) -> str:
    """The effective stored value for one dispatch, in `MODEL_PRECEDENCE` order. Empty means "emit no
    `--model`" (the runner CLI's default).

    `project_default` is a callable because it is a database read, skipped when an earlier rung wins.
    A whitespace-only candidate falls through rather than winning, so `--model "  "` is never emitted.
    """
    return (task_model or "").strip() or (stage_default or "").strip() or project_default() or ""


# ── Tier -> concrete model ────────────────────────────────────────────────────────────────────────────

# A model id we will put on a CLI's argv. A catalog is node-reported data: the route and the node's sensor
# check this same shape, and it is checked again here where it is chosen.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
# A model's generation is the first dotted number in its id (`gpt-5.6-sol` -> (5, 6)).
_GENERATION_RE = re.compile(r"\d+(?:\.\d+)*")
# The catalog visibility a vendor uses for models it offers in its own picker; anything else (`hide`) is
# internal, legacy or special-purpose and is never chosen.
_OFFERED = "list"


# A node's catalog older than this is not trusted: its CLI may have been downgraded or its account removed,
# and a slug the vendor withdrew fails every turn. Four of the node's 30-minute report intervals
# (`apps/jaahd/provider_models.py::INTERVAL_SECONDS`), so one or two missed reports are tolerated.
CATALOG_FRESH_SECONDS = 4 * 1800


def _generation(slug: str) -> Optional[tuple[int, ...]]:
    """The generation a slug names, or None. A 4+-digit leading number is a date stamp
    (`...-2025-08`), not a generation, and would otherwise outrank every real model."""
    match = _GENERATION_RE.search(slug)
    if not match:
        return None
    generation = tuple(int(part) for part in match.group(0).split("."))
    return None if generation[0] >= 1000 else generation


def pick_from_catalog(tier: str, models: Sequence[Mapping[str, Any]] | None) -> str:
    """The concrete model for `tier` from one CLI's live catalog, or "" when the catalog offers none.

    THE DECLARED RULE: keep the models the vendor OFFERS (`visibility == "list"`) whose id carries a
    generation number; keep only the NEWEST generation; rank that cohort by the vendor's own `priority`
    (lower first). Max and High take the first, Standard the second (the first when the cohort has one),
    Low the last. So a vendor shipping a newer generation, or re-ranking, moves every tier on the next
    dispatch with no configuration anywhere."""
    cohort: list[tuple[tuple[int, ...], int, str]] = []
    for model in models or ():
        if not isinstance(model, Mapping) or model.get("visibility") != _OFFERED:
            continue
        slug, priority = model.get("slug"), model.get("priority")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            continue
        if not isinstance(priority, int) or isinstance(priority, bool):
            continue
        generation = _generation(slug)
        if generation is not None:
            cohort.append((generation, priority, slug))
    if not cohort:
        return ""
    newest = max(generation for generation, _p, _s in cohort)
    ranked = [
        slug for _g, _p, slug in sorted((c for c in cohort if c[0] == newest), key=lambda c: (c[1], c[2]))
    ]
    if tier == TIER_STANDARD:
        return ranked[1] if len(ranked) > 1 else ranked[0]
    if tier == TIER_LOW:
        return ranked[-1]
    return ranked[0]


def resolve_tier_model(
    tier: str,
    tier_aliases: Sequence[tuple[str, str]],
    catalog: Callable[[], Sequence[Mapping[str, Any]] | None],
) -> str:
    """The concrete `--model` value for `tier` on one provider: its declared moving alias, or — for a
    provider with none — `pick_from_catalog` over the dispatching node's reported catalog. "" (the CLI's
    own default) only when a catalog provider's node has reported nothing usable yet.

    `catalog` is a callable because it is a database read, made only for a provider that needs it."""
    if tier_aliases:
        return dict(tier_aliases).get(tier, "")
    return pick_from_catalog(tier, catalog())


# ── A foreign model id on a folding provider ─────────────────────────────────────────────────────────


class ForeignModelRefused(ValueError):
    """An exact model id a folding provider can neither serve nor translate (`fold_foreign_id`)."""


def fold_foreign_id(provider: Any, model: str) -> tuple[str, str]:
    """`(tier, concrete --model value)` for an EXACT model id on a provider declaring
    `foreign_model_ids == "fold"` (`app/agent_providers.py`) — the single home of that rule.

    Only an exact id reaches here — a per-request override from a direct `compose` caller, or a pre-tier
    stored value; a pin saved through `validate_model` is already a tier.
      * an id the provider serves (one of its `tier_aliases` values, with or without its context suffix)
        passes through as written, with no tier;
      * a Claude model id folds onto its tier by family (`model_prices.canonical_model`, then
        `LEGACY_ALIASES`: fable→max, opus→high, sonnet→standard, haiku→low) and onto the provider's alias
        for that tier — never passed through, where the vendor would silently remap it;
      * anything else raises `ForeignModelRefused` with a readable reason: the vendor would refuse it at the first
        request, so the dispatch is refused instead.

    `provider` is an `AgentProvider`, duck-typed (`value`, `tier_aliases`) so this module imports no
    provider catalogue."""
    from app.model_prices import canonical_model  # Deferred: pinnable and costable stay separate modules.

    raw = (model or "").strip()
    candidate = without_context_suffix(raw)
    aliases = dict(provider.tier_aliases)
    served = {alias for alias in aliases.values()} | {
        _CONTEXT_SUFFIX_RE.sub("", alias) for alias in aliases.values()
    }
    if raw in served:
        return "", raw
    if candidate in served:
        # A served alias carries a suffix the provider does not declare: it would ride the CLI's --model
        # to a vendor that silently remaps unknown ids — refused rather than launched.
        raise ForeignModelRefused(
            f"model {raw!r} cannot run on provider {provider.value!r}: a served id must be pinned exactly, "
            f"and its suffix is not one the provider declares ({', '.join(sorted(set(aliases.values()))) or 'none declared'})"
        )
    tier = LEGACY_ALIASES.get(canonical_model(candidate), INHERIT)
    concrete = aliases.get(tier, "") if tier else ""
    if not concrete:
        raise ForeignModelRefused(
            f"model {raw!r} cannot run on provider {provider.value!r}: it is neither a model that "
            f"provider serves ({', '.join(sorted(set(aliases.values()))) or 'none declared'}) nor a Claude "
            f"model it can translate to a tier. Pin a tier (max, high, standard, low) or one of its models"
        )
    return tier, concrete
