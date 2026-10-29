"""Official standard token rates for non-default agent providers.

This is the provider-specific seam consumed by the provider-neutral arithmetic in
``app.model_prices``.  Database rows remain the operator-controlled override; these defaults make new
published model prices available to existing deployments without a migration.
"""
from __future__ import annotations

from decimal import Decimal


# Rates are (fresh input, cached input, cache write, output) in dollars per million tokens. These fleet
# Codex runs use ChatGPT/Codex accounts, whose rate card says cache writes are not charged; Gemini does
# not report billable cache writes. Both therefore use zero here even though OpenAI's API product has a
# separate cache-write price.
# Sources checked 2026-10-09:
#   https://developers.openai.com/api/docs/pricing
#   https://ai.google.dev/gemini-api/docs/pricing
# Zero cache writes for Codex accounts: https://help.openai.com/en/articles/20001415 (checked 2026-09-26)
# Neither vendor exposes a stable machine-readable price catalog. Release-pinned, exact-id defaults are
# therefore the automatic safe path available here; the existing DB rows remain the live override.
_STANDARD_RATES: dict[str, tuple[str, str, str, str]] = {
    # Exact public ids only. A new catalog model stays visibly unpriced until its own official rate is
    # known; silently inheriting a family rate can produce a plausible but false figure.
    "gpt-6-astra": ("10.0000", "1.0000", "0", "50.0000"),
    "gpt-6.1-sol": ("2.0000", "0.1000", "0", "10.0000"),
    "gpt-6-sol": ("2.0000", "0.2000", "0", "10.0000"),
    "gpt-6-luna": ("0.1000", "0.0100", "0", "0.5000"),
    # gpt-5.6-sol only: promotional per OpenAI "at least through November 21, 2026"; re-check after.
    "gpt-5.6-sol": ("4.0000", "0.4000", "0", "20.0000"),
    "gpt-5.6-terra": ("2.0000", "0.2000", "0", "12.0000"),
    "gpt-5.6-luna": ("0.2000", "0.0200", "0", "1.2000"),
    "gpt-5.5": ("5.0000", "0.5000", "0", "30.0000"),
    "gemini-3.5-flash": ("1.5000", "0.1500", "0", "9.0000"),
    "gemini-3.5-flash-lite": ("0.3000", "0.0300", "0", "2.5000"),
    "gemini-3.1-flash-lite": ("0.2500", "0.0250", "0", "1.5000"),
    # Gemini model aliases can fall back to these concrete ids by account/feature eligibility.
    "gemini-3-flash-preview": ("0.5000", "0.0500", "0", "3.0000"),
    "gemini-2.5-flash": ("0.3000", "0.0300", "0", "2.5000"),
    "gemini-2.5-flash-lite": ("0.1000", "0.0100", "0", "0.4000"),
    # No longer on Google's pricing page (2026-10-09); last published rate kept so past captures stay priced.
    "gemini-3-pro-preview": ("2.0000", "0.2000", "0", "12.0000"),
    "gemini-2.5-pro": ("1.2500", "0.1250", "0", "10.0000"),
    "gemini-3.1-pro-preview": ("2.0000", "0.2000", "0", "12.0000"),
    "gemini-3.1-pro-preview-customtools": ("2.0000", "0.2000", "0", "12.0000"),
    # DeepSeek, run through Claude Code against DeepSeek's Anthropic-compatible endpoint. Off-peak is the
    # base; `@peak` (2x, `apply_peak_rates`) is the key a peak-window usage line is captured under. DeepSeek reports no
    # cache writes, so any that do arrive are priced at the miss rate rather than understated at zero.
    # Source checked 2026-10-09: https://api-docs.deepseek.com/quick_start/pricing
    "deepseek-flash": ("0.1500", "0.0030", "0.1500", "0.6000"),
    "deepseek-v4-pro": ("0.6600", "0.0220", "0.6600", "1.9800"),
}

# Long context: OpenAI >272K input tokens, Gemini Pro >200K — both 2x input/cached, 1.5x output (pages above).
_LONG_CONTEXT_MODELS = {
    "gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
    "gemini-3-pro-preview", "gemini-2.5-pro",
    "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools",
}

# Peak window 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri, at twice the off-peak base on every class.
_PEAK_MODELS = {"deepseek-flash", "deepseek-v4-pro"}


def apply_long_context_rates(
    rates: dict[str, dict[str, Decimal]],
) -> dict[str, dict[str, Decimal]]:
    """Derive per-request long-context buckets from whichever standard rows are effective."""
    for model in _LONG_CONTEXT_MODELS:
        base = rates.get(model)
        if base is None:
            continue
        tier = f"{model}@long-context"
        if tier in rates:
            continue
        rates[tier] = {
            "input": base["input"] * 2,
            "cache_write_5m": base["cache_write_5m"],
            "cache_write_1h": base["cache_write_1h"],
            "cache_read": base["cache_read"] * 2,
            "output": base["output"] * Decimal("1.5"),
        }
    return rates


def apply_peak_rates(
    rates: dict[str, dict[str, Decimal]],
) -> dict[str, dict[str, Decimal]]:
    """Derive the `@peak` bucket (2x every class) from whichever off-peak base rows are effective."""
    for model in _PEAK_MODELS:
        base = rates.get(model)
        if base is None:
            continue
        tier = f"{model}@peak"
        if tier in rates:
            continue
        rates[tier] = {cls: rate * 2 for cls, rate in base.items()}
    return rates


def bedrock_family_prices() -> dict[str, dict[str, Decimal]]:
    """The Jaah account's default per-family Bedrock rates, `bedrock:<family>` (AS9).

    Bedrock's on-demand list price for Anthropic's models is Anthropic's own list price per token class, so
    the defaults ARE the Claude seed rates (`app/model_prices.py::SEED_PRICES`), read, not retyped. A
    regional-endpoint premium or a negotiated rate is the operator's `bedrock:<family>` row in Settings >
    Pricing, which wins (`prices_by_model`). A family with no seed rate stays unpriced and is refused."""
    # Deferred: `app.model_prices` imports this module.
    from app.model_prices import PRICE_COLUMNS, SEED_PRICES, TOKEN_CLASSES

    columns = list(PRICE_COLUMNS)
    out: dict[str, dict[str, Decimal]] = {}
    for name, _description, *rates in SEED_PRICES:
        by_class = dict(zip(columns, rates))
        out[f"bedrock:{name}"] = {cls: Decimal(by_class[cls]) for cls in TOKEN_CLASSES}
    return out


def official_prices() -> dict[str, dict[str, Decimal]]:
    """Return standard rates for the exact observed models.

    Unknown models stay absent instead of inheriting a guessed family price. Per-request long-context
    and peak buckets are retained under an internal suffix selected at capture time. Tool fees, storage,
    subscription inclusion, and fast/regional modes remain outside the standard-rate estimate.
    """
    rates = {
        model: {
            "input": Decimal(input_rate),
            # This shared class is the only provider-neutral cache-write bucket. Its historical `5m`
            # name describes Anthropic's first TTL, not a claim that every provider exposes that TTL.
            "cache_write_5m": Decimal(cache_write_rate),
            # The one cache-write rate covers either TTL a CLI reports; zero for every provider but
            # DeepSeek, which prices a stray write at its miss rate.
            "cache_write_1h": Decimal(cache_write_rate),
            "cache_read": Decimal(cached_rate),
            "output": Decimal(output_rate),
        }
        for model, (input_rate, cached_rate, cache_write_rate, output_rate) in _STANDARD_RATES.items()
    }
    rates.update(bedrock_family_prices())
    return apply_peak_rates(apply_long_context_rates(rates))
