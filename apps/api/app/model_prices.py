"""Per-model token rates and the token→dollar arithmetic — the single home for both.

Separate from `routes/model_prices.py` so `migrations/versions/202610251700_baseline.py` (which seeds the table
without the ORM) and `app/task_run_metrics.py` need not import a route. `SEED_PRICES` is plain data.

Five token classes because Anthropic prices input five ways across a 40× spread; conflating them is wrong
by an order of magnitude (`tests/test_model_prices_math.py`). `canonical_model` collapses every spelling
of a model onto its family, the key rates join on; `merge_usage` applies it.

Rates are $/MTok. Seed rows are kept current by operator edit in Settings. A capture is priced once, when
it is written (`lock_capture_costs`): a rate edit applies to captures after it, never to finished work.
Standard rates for other providers come through `app.provider_model_prices`; `prices_by_model` lays DB rows
over those defaults, so an operator's negotiated rate always wins.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from app.provider_model_prices import official_prices

# Every rate is per MTok; a Decimal divisor keeps cost math out of binary floating point.
MTOK = Decimal(1_000_000)

# Wire vocabulary: the node writes these keys, ingest stores them verbatim, this module prices them, so a
# rename breaks every captured `Task.metrics`. `input` is uncached; `cache_write_*` are the 5m/1h TTLs
# (1.25×/2×, split under `usage.cache_creation`); `cache_read` is a hit at 0.1×.
TOKEN_CLASSES: tuple[str, ...] = (
    "input",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
    "output",
)

# Explicit because columns carry the `_per_mtok` unit suffix the wire keys do not.
PRICE_COLUMNS: dict[str, str] = {
    "input": "input_per_mtok",
    "cache_write_5m": "cache_write_5m_per_mtok",
    "cache_write_1h": "cache_write_1h_per_mtok",
    "cache_read": "cache_read_per_mtok",
    "output": "output_per_mtok",
}

# The model families a price row is named in. Bare family, not a versioned id: the fleet dispatches an
# alias and the CLI records whatever snapshot is current, so a versioned row would expire on the next
# snapshot. New snapshots are costed at the family's rates. Shared with `app/model_catalog.py` by value,
# not import: pinnable and costable are different questions.
MODEL_FAMILIES: tuple[str, ...] = ("opus", "sonnet", "haiku", "fable")

# The family must sit adjacent to `claude`: cost accounting must never bill another vendor's model at
# our rates, and family tokens occur in foreign ids (`gpt-4o-haiku`, `llama-3-opus-70b`,
# `deepseek-r1-anthropic-distill-opus`, `my-claude-clone-haiku-finetune`). "Mentions claude/anthropic
# anywhere" leaks; adjacency (vendor word, optional version, family) is the shape every genuine id has.
# Both boundaries are zero-width so adjacent pairings (`claude-haiku/claude-sonnet`) are all found; the
# leading one stops `superclaude-opus`. The trailing one admits a digit (`claude-opus5` is a version).
_CLAUDE_FAMILY_RE = re.compile(
    r"(?<![a-z0-9])claude[.\-_/ ]?(?:\d[\d.\- ]*[.\-_ ]?)?("
    + "|".join(re.escape(family) for family in sorted(MODEL_FAMILIES, key=len, reverse=True))
    + r")(?![a-z])"
)

# The CLI's context-window suffix on a model id (`deepseek-v4-pro[1m]` is the same model), shared by
# value with `model_catalog._CONTEXT_SUFFIX_RE`: pinnable and costable are different questions and this
# module imports no catalogue.
_CONTEXT_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")


# Per-request pricing tiers a capture can suffix onto a model id (`<model>@<tier>`): the node picks one per
# line from what that request spent or when it ran, and `canonical_model` folds a Claude id's `[1m]` context
# suffix onto `@1m`. Only these are kept through `canonical_model`; any other `@` is part of the id (a
# Vertex snapshot, `claude-opus-4-5@20251101`, folds onto its family as before).
PRICING_TIERS: tuple[str, ...] = ("@long-context", "@peak", "@1m")

# The `[1m]` long-context tier: ~2x on every class (platform.claude.com pricing), derived from each
# family's effective base row in `prices_by_model`, exactly as `haiku@long-context` below.
LONG_CONTEXT_1M_MULTIPLIER = Decimal(2)

# Claude Haiku's long-prompt tier: a request whose prompt (input plus cache writes and reads) exceeds 100K
# tokens is billed at 5x on every class. The node suffixes such a line `@long-context`
# (`apps/jaahd/session_sweep.py`); `prices_by_model` derives the tier from the effective `haiku` row.
HAIKU_LONG_CONTEXT_MULTIPLIER = Decimal(5)


def canonical_model(name: str) -> str:
    """One model id → the family it belongs to, or the id unchanged when it belongs to none. The join key.

    An unrecognized name passes through verbatim, finds no rate row, and `cost_usd` reports it by name as
    unpriced; guessing a family would bill spend at a rate nobody chose. `routes/model_prices.py::
    _validated_name` folds through this, so such a name can still get its own row.

    A `PRICING_TIERS` suffix is kept, so `claude-haiku-5-5@long-context` is `haiku@long-context`.
    A context-window suffix on a non-Claude id is dropped for the join (`deepseek-v4-pro[1m]` is
    `deepseek-v4-pro`); on a Claude-family id it names the `[1m]` long-context tier and folds onto the
    derived `family@1m` row (~2x) — never the base family's rate, and never a zero-priced passthrough
    (the node is not a trust boundary, so a crafted suffix must not buy a $0 model).
    Arms, in order: a bare family is itself; otherwise the last `claude`-adjacent pairing
    (`_CLAUDE_FAMILY_RE`) wins, so `haiku-pool/claude-opus-5` is Opus and
    `claude-sonnet-4-5-v2:0-opus` is Sonnet. Case-insensitive match; passthrough keeps original case.
    """
    stripped = name.strip()
    tier = next((t for t in PRICING_TIERS if stripped.lower().endswith(t)), "")
    if tier:
        return canonical_model(stripped[: -len(tier)]) + tier
    unsuffixed = stripped
    while True:  # a hand-edited double suffix (`x[1m][1m]`) strips to the id
        shorter = _CONTEXT_SUFFIX_RE.sub("", unsuffixed)
        if shorter == unsuffixed:
            break
        unsuffixed = shorter
    suffixed = unsuffixed != stripped
    lowered = unsuffixed.lower()
    if lowered in MODEL_FAMILIES:
        return f"{lowered}@1m" if suffixed else lowered
    # Last pairing wins; `re` has no right-to-left search.
    found = None
    for match in _CLAUDE_FAMILY_RE.finditer(lowered):
        found = match
    return f"{found.group(1)}@1m" if found and suffixed else (found.group(1) if found else unsuffixed)


# Rows: (model id, description, input, cache_write_5m, cache_write_1h, cache_read, output). `name` is a
# `MODEL_FAMILIES` member; rates are exact-decimal strings. Other models report unpriced, never zero.
# Rates are the latest generation's and stay put across version bumps (accepted trade); per-generation
# accuracy would need an effective-from date. The `[1m]` context tier is the derived `family@1m` row
# (`LONG_CONTEXT_1M_MULTIPLIER`).
# Haiku is its <=100K-prompt rate; the >100K tier (5x) is `haiku@long-context`, derived in `prices_by_model`.
# Cache reads differ per family: 0.025x input on Fable, 0.05x on Opus, 0.1x on Sonnet and Haiku.
# Source checked 2026-10-14 (Fable 5.1, Opus 5.5, Sonnet 5.5, Haiku 5.5):
#   https://platform.claude.com/docs/en/about-claude/pricing
SEED_PRICES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    ("fable", "Claude Fable", "10.0000", "12.5000", "20.0000", "0.2500", "50.0000"),
    ("opus", "Claude Opus", "4.0000", "5.0000", "8.0000", "0.2000", "20.0000"),
    ("sonnet", "Claude Sonnet", "2.0000", "2.5000", "4.0000", "0.2000", "10.0000"),
    ("haiku", "Claude Haiku", "0.1000", "0.1250", "0.2000", "0.0100", "0.5000"),
)

# $/MTok to four decimals; shared by the ORM column, the revision and the validator.
PRICE_PRECISION = 12
PRICE_SCALE = 4


def empty_usage() -> dict[str, int]:
    """A zeroed usage bucket with every class present. Fresh per call: callers accumulate into it."""
    return {cls: 0 for cls in TOKEN_CLASSES}


def merge_usage(
    into: dict[str, dict[str, int]], other: Optional[Mapping[str, Mapping[str, Any]]]
) -> dict[str, dict[str, int]]:
    """Add one `usage_by_model` blob into an accumulator, per model and per class. Mutates and returns
    `into`.

    Model ids become canonical here, so both the ingest fold (`app/task_metrics_ingest.py::
    fold_session_metrics`) and the pricing fold (`app/task_run_metrics.py`) canonicalize old captures
    too, with no backfill; two spellings of one family sum into one bucket.

    Defensive: the blob is node-written JSONB, so a non-dict entry, unknown class or non-int count is
    dropped, never raised — a malformed capture degrades the figure, never 500s the page."""
    if not isinstance(other, Mapping):
        return into
    for model, classes in other.items():
        if not isinstance(model, str) or not isinstance(classes, Mapping):
            continue
        bucket = into.setdefault(canonical_model(model), empty_usage())
        for cls in TOKEN_CLASSES:
            raw = classes.get(cls)
            if isinstance(raw, bool) or not isinstance(raw, int):
                continue
            bucket[cls] += raw
    return into


def prices_by_model(rows: Iterable[Any]) -> dict[str, dict[str, Decimal]]:
    """Index `ModelPrice` ORM rows (or anything with the same attributes) by model id. Pure reshape of
    the caller's query.

    Does not canonicalize `row.name`: the write path (`routes/model_prices.py::_validated_name`) already
    does, and canonicalizing here could silently merge two rows by iteration order."""
    # Official defaults first, then DB rows: explicit operator rates always win and keep their existing
    # live-edit semantics. A release adds current vendor coverage without a data migration or overwriting
    # an operator's negotiated rate.
    out = official_prices()
    configured: set[str] = set()
    for row in rows:
        configured.add(row.name)
        out[row.name] = {
            cls: Decimal(getattr(row, column)) for cls, column in PRICE_COLUMNS.items()
        }
    # An operator base override says nothing about its tier multipliers (`@long-context`, `@peak`, any
    # future `@<tier>`). Keep an explicitly configured tier row, but remove an official derived tier when
    # only its base was replaced rather than inventing a negotiated tier rate.
    for name in configured:
        prefix = f"{name}@"
        for tier in [key for key in out if key.startswith(prefix) and key not in configured]:
            del out[tier]
    # Derived from whichever `haiku` row is effective, so an operator's Haiku rate carries its tier with it;
    # an explicit `haiku@long-context` row wins.
    haiku = out.get("haiku")
    if haiku is not None and "haiku@long-context" not in out:
        out["haiku@long-context"] = {cls: rate * HAIKU_LONG_CONTEXT_MULTIPLIER for cls, rate in haiku.items()}
    # The `[1m]` long-context tier (`canonical_model` folds a suffixed family id onto it) is derived the
    # same way, per family; an explicit `family@1m` row wins.
    for family in MODEL_FAMILIES:
        base = out.get(family)
        if base is not None and f"{family}@1m" not in out:
            out[f"{family}@1m"] = {cls: rate * LONG_CONTEXT_1M_MULTIPLIER for cls, rate in base.items()}
    return out


def cost_usd(
    usage_by_model: Mapping[str, Mapping[str, int]],
    prices: Mapping[str, Mapping[str, Decimal]],
) -> tuple[Decimal, list[str]]:
    """Price one `usage_by_model` blob. Returns `(total_usd, unpriced_models)`.

    A model with no price row adds nothing to the total and is returned by name, never costed at zero or
    at another rate. An all-zero model is not reported. Returns exact Decimal; rounding is the caller's."""
    total = Decimal(0)
    unpriced: list[str] = []
    for model, classes in usage_by_model.items():
        spent = sum(int(classes.get(cls) or 0) for cls in TOKEN_CLASSES)
        if spent <= 0:
            continue
        rates = prices.get(model)
        if rates is None:
            unpriced.append(model)
            continue
        for cls in TOKEN_CLASSES:
            count = int(classes.get(cls) or 0)
            if count:
                total += (Decimal(count) * rates[cls]) / MTOK
    return total, sorted(unpriced)


# A capture's dollars are fixed when the capture is written, at the rates in force then: a later rate edit
# prices later captures only. Each session entry of `Task.metrics` carries them as `cost_by_model` (canonical
# model -> exact decimal string, priced models only); a legacy blob with no `sessions` map carries it at its
# top level. Readers price capture by capture (`captures`), never a sum across them: one model can be priced
# in one capture and unpriced in another.
COST_BY_MODEL = "cost_by_model"


def lock_costs(
    usage_by_model: Mapping[str, Mapping[str, int]],
    prices: Mapping[str, Mapping[str, Decimal]],
) -> dict[str, str]:
    """Price each spending model of a canonical `usage_by_model` now → `{model: usd}`. A model with no rate is
    left out, so it reads as unpriced for good (`capture_cost`); exact, unrounded decimal strings."""
    out: dict[str, str] = {}
    for model, classes in usage_by_model.items():
        total, unpriced = cost_usd({model: classes}, prices)
        if not unpriced and sum(int(classes.get(cls) or 0) for cls in TOKEN_CLASSES) > 0:
            out[model] = str(total)
    return out


def read_locked(raw: Any) -> Optional[dict[str, Decimal]]:
    """A stored `cost_by_model` as Decimals keyed by today's `canonical_model` (so a later change to the fold
    still finds its model), or `None` when there is none or it does not parse — the caller then prices live
    rather than trusting half a figure."""
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Decimal] = {}
    for model, usd in raw.items():
        if not isinstance(model, str) or not isinstance(usd, str):
            return None
        try:
            figure = Decimal(usd)
        except ArithmeticError:
            return None
        key = canonical_model(model)
        out[key] = out.get(key, Decimal(0)) + figure
    return out


def capture_cost(
    usage_by_model: Mapping[str, Mapping[str, int]],
    locked: Any,
    prices: Mapping[str, Mapping[str, Decimal]],
) -> tuple[Decimal, list[str]]:
    """`cost_usd`'s contract for ONE canonical capture: its locked figures when it carries readable ones,
    else the live rates. Only models in `usage_by_model` count, so a caller may pass one model's slice; a
    spending model the lock left out is unpriced."""
    figures = read_locked(locked)
    if figures is None:
        return cost_usd(usage_by_model, prices)
    total = Decimal(0)
    unpriced: list[str] = []
    for model, classes in usage_by_model.items():
        if sum(int(classes.get(cls) or 0) for cls in TOKEN_CLASSES) <= 0:
            continue
        if model in figures:
            total += figures[model]
        else:
            unpriced.append(model)
    return total, sorted(unpriced)


# A session entry's agent-reported running total (`session_events.UsageTotals.cumulative_cost_usd`, written by
# `task_metrics_ingest.fold_session_metrics`). Once present it is the session's whole cost: the entry's tokens
# still count toward token totals, but no reader prices them again (`prices_tokens`).
REPORTED_CUMULATIVE_COST = "reported_cumulative_cost_usd"


def prices_tokens(entry: Any) -> bool:
    """Whether a `Task.metrics["sessions"]` entry's `usage_by_model` is priced: false once the agent reported a
    running total (`REPORTED_CUMULATIVE_COST`), which already covers those tokens."""
    return not (isinstance(entry, Mapping) and entry.get(REPORTED_CUMULATIVE_COST) is not None)


def captures(metrics: Mapping[str, Any]) -> list[tuple[dict[str, dict[str, int]], Any]]:
    """A `Task.metrics` blob's priced captures as `(canonical usage_by_model, stored cost_by_model or None)`: one
    per session entry whose tokens are priced (`prices_tokens`), or the top-level pair for a legacy blob whose
    sessions carry no per-model usage. Captures with no usage are left out."""
    sessions = metrics.get("sessions")
    out: list[tuple[dict[str, dict[str, int]], Any]] = []
    per_session = False
    for entry in sessions.values() if isinstance(sessions, Mapping) else ():
        if isinstance(entry, Mapping):
            usage = merge_usage({}, entry.get("usage_by_model"))
            per_session = per_session or bool(usage)
            if usage and prices_tokens(entry):
                out.append((usage, entry.get(COST_BY_MODEL)))
    if not per_session:
        usage = merge_usage({}, metrics.get("usage_by_model"))
        if usage:
            out.append((usage, metrics.get(COST_BY_MODEL)))
    return out


def lock_capture_costs(metrics: dict[str, Any], prices: Mapping[str, Mapping[str, Decimal]]) -> dict[str, Any]:
    """Fix the dollars of each capture in a `Task.metrics` blob not yet priced: priced now and stored beside
    its usage. A capture already priced, or holding no usage, is left exactly as it is. Mutates and returns
    `metrics` (a priced session entry is copied, not shared).

    The single home of the lock, called by the capture fold (`app/task_metrics_ingest.py`) and the revision
    that locked the captures written before it (revision 202610141400, since collapsed into the baseline)."""
    sessions = metrics.get("sessions")
    per_session = False
    if isinstance(sessions, dict):
        locked_sessions = dict(sessions)
        for sid, entry in sessions.items():
            if not isinstance(entry, dict):
                continue
            usage = merge_usage({}, entry.get("usage_by_model"))
            if not usage:
                continue
            per_session = True
            if read_locked(entry.get(COST_BY_MODEL)) is None:
                locked_sessions[sid] = {**entry, COST_BY_MODEL: lock_costs(usage, prices)}
        if locked_sessions != sessions:
            metrics["sessions"] = locked_sessions
    if not per_session and read_locked(metrics.get(COST_BY_MODEL)) is None:
        usage = merge_usage({}, metrics.get("usage_by_model"))
        if usage:
            metrics[COST_BY_MODEL] = lock_costs(usage, prices)
    return metrics
