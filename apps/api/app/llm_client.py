"""The one LLM client factory, plus the constants that configure it.

One factory so there is one "is a client available" rule; a second would let one surface issue live
billable calls while another stays hermetic. Callers pass a timeout in; they never re-read the env.

`make_client` is the only reader of `DEEPSEEK_API_KEY` and `ANTHROPIC_API_KEY`, so a test (or
`tests/conftest.py`'s `_no_live_llm_calls`) that blanks a key must patch it here; `tests/test_llm_client.py`
pins that. Both providers speak the Anthropic SDK (DeepSeek through its Anthropic-compatible endpoint), so a
caller's code is the same whichever `provider` it asks for.

The support agent picks its provider from config, through `support_llm`: the operator's
`support_llm_credential_id` setting (a stored API key) when set, else the env (`SUPPORT_LLM_PROVIDER`, DeepSeek by
default). A trigger's model preflight uses its own stored key, else the operator's
`trigger_preflight_llm_credential_id` (`trigger_preflight_llm`). Titles, tags and org slugs ride the support agent's
provider and key (`summary_llm`), so customer text reaches only the provider the operator chose. Every path
builds the client in `_client`, so the `sk-` rule and the SDK settings have one home.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")


# The title-and-tag model and budgets: Settings > Integrations > Titles and tags, read per call. The classifier
# has its own budget: it sends the whole tag catalogue to a thinking model whose latency scales with the input,
# and raising the title budget instead would slow every title render.
def summary_model() -> str:
    from app.operator_settings import reads as settings_reads

    return settings_reads.get_str("summary_model")


def summary_timeout() -> float:
    from app.operator_settings import reads as settings_reads

    return settings_reads.get_float("summary_timeout_secs")


def classify_timeout() -> float:
    from app.operator_settings import reads as settings_reads

    return settings_reads.get_float("classify_timeout_secs")


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DEEPSEEK = "deepseek"
ANTHROPIC = "anthropic"
PROVIDERS: tuple[str, ...] = (DEEPSEEK, ANTHROPIC)
#: DeepSeek's flash id as its pricing page serves it; `deepseek-v4-flash` is a legacy name
#: (api-docs.deepseek.com/quick_start/pricing). The CLI's tier aliases add a `[1m]` context suffix to the
#: same ids (`model_catalog._CONTEXT_SUFFIX_RE`).
DEEPSEEK_FLASH = "deepseek-flash"
#: Each provider's default model for the support agent.
SUPPORT_DEFAULT_MODELS = {DEEPSEEK: DEEPSEEK_FLASH, ANTHROPIC: "claude-haiku-4-5-20251001"}
#: Each provider's API model for a `model_catalog` tier, the value the `support_llm_model` select stores.
#: API ids, not the CLI aliases `agent_providers.tier_aliases` declares: this client calls the vendor API.
SUPPORT_TIER_MODELS = {
    ANTHROPIC: {
        "max": "claude-fable-5-1",
        "high": "claude-opus-5-5",
        "standard": "claude-sonnet-5-5",
        "low": "claude-haiku-4-5-20251001",
    },
    DEEPSEEK: {
        "max": "deepseek-v4-pro",
        "high": "deepseek-v4-pro",
        "standard": DEEPSEEK_FLASH,
        "low": DEEPSEEK_FLASH,
    },
}
#: The tier models that always think (adaptive; it cannot be disabled), where thinking tokens count against
#: `max_tokens`: sent at low effort, a chat turn's depth, with room for the reply after the thinking.
#: Only these ids: Haiku 4.5, DeepSeek and an older free-text id take no `effort` and would 400 on it.
SUPPORT_THINKING_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"})
SUPPORT_THINKING_OPTIONS = {"output_config": {"effort": "low"}, "max_tokens": 4000}
#: The options for a one-shot reply on a model that thinks before it answers (DeepSeek's v4s): the
#: off-switch their Anthropic-compatible endpoint honours (measured) keeps a small cap for the
#: reply instead of spending it on reasoning, which returns no text block. A model that always thinks
#: takes `SUPPORT_THINKING_OPTIONS` instead (its thinking cannot be disabled).
NO_THINKING_OPTIONS = {"thinking": {"type": "disabled"}, "max_tokens": 2048}
SUPPORT_LLM_PROVIDER = os.environ.get("SUPPORT_LLM_PROVIDER") or DEEPSEEK
SUPPORT_MODEL = os.environ.get("SUPPORT_MODEL") or SUPPORT_DEFAULT_MODELS.get(SUPPORT_LLM_PROVIDER, "")
#: The credential types `support_llm_credential_id` may name, and the provider each one is.
SUPPORT_CREDENTIAL_PROVIDERS = {"anthropic_api_key": ANTHROPIC, "deepseek_api_key": DEEPSEEK}
#: The providers whose endpoint honours a tool's `strict` flag (schema-valid arguments). DeepSeek's compatible
#: endpoint is not promised the field, so it is sent only where it is a promise.
STRICT_TOOL_PROVIDERS = frozenset({ANTHROPIC})

#: DeepSeek's documented standard reply ceiling; the beta ceiling (8192) is what `task_classifier`
#: escalates a retry to, so a one-shot call with no retry stays under the standard one.
DEEPSEEK_REPLY_CEILING = 4096


def reply_options(provider: str, default_budget: int) -> dict[str, Any]:
    """The reply options for a one-shot call: `default_budget` — on DeepSeek its standard reply ceiling
    with thinking turned off (its v4s think first and honour the switch, as measured), so the cap
    buys the reply, never the reasoning. The beta ceiling is a retry's escalation (`task_classifier`)."""
    if provider == DEEPSEEK:
        return {"max_tokens": DEEPSEEK_REPLY_CEILING, "thinking": {"type": "disabled"}}
    return {"max_tokens": default_budget}


def reads_images(provider: str, model: str) -> bool:
    """Whether `model` on `provider` reads image blocks: Anthropic's models do; of DeepSeek's two ids
    only `DEEPSEEK_FLASH` reads them, v4-pro guesses at input it is billed for (measured).
    The title paths send screenshots only when this is True; the support agent keeps the stricter
    `SupportLLM.accepts_images` gate (Anthropic only). The context suffix is stripped before the match:
    a free-text pin in the CLI's spelling (`deepseek-flash[1m]`) is the same model."""
    if provider != DEEPSEEK:
        return provider == ANTHROPIC
    from app import model_catalog  # Deferred: the suffix stripper's home; the vendor seam imports no catalogue.

    return model_catalog.without_context_suffix(model).lower() == DEEPSEEK_FLASH


def with_strict(tool: dict[str, Any], provider: str) -> dict[str, Any]:
    """`tool` with `strict` set where `provider` honours it (`STRICT_TOOL_PROVIDERS`), IN PLACE, returned."""
    if provider in STRICT_TOOL_PROVIDERS:
        tool["strict"] = True
    return tool


def make_client(*, timeout: Optional[float] = None, provider: str = DEEPSEEK):
    """Construct `provider`'s client (the Anthropic SDK either way) from the env key, or None if unavailable.

    `timeout` None is the title budget (`summary_timeout`), resolved at the call. None (never a raise) when the key or SDK is absent, or `provider` is not one of `PROVIDERS`; callers
    fall back to a deterministic result or refuse honestly (`task_classifier`). Keyword-only so a bare float
    cannot read as a token count."""
    key = {DEEPSEEK: DEEPSEEK_API_KEY, ANTHROPIC: ANTHROPIC_API_KEY}.get(provider, "")
    return _client(provider, key, summary_timeout() if timeout is None else timeout)


def _client(provider: str, key: str, timeout: float):
    """`provider`'s client for `key`, or None. The SDK is imported lazily so the API runs without it."""
    if provider not in PROVIDERS:
        return None
    # The deployed secret can hold a bring-up placeholder; requiring the vendor prefix sends that case
    # down the same fallback as an unset key instead of issuing doomed calls.
    if not key.startswith("sk-"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    base_url = {"base_url": DEEPSEEK_BASE_URL} if provider == DEEPSEEK else {}
    return anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=0, **base_url)


#: The image formats a vision model reads, by the type their bytes sniff as (`sniffed_image_type`).
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
#: Anthropic's 5 MB per-image limit is on the BASE64 payload (the API refuses "image exceeds 5 MB maximum:
#: N bytes > 5242880 bytes", counting the encoded string — docs.claude.com, Vision › image size limits), so
#: the raw bytes may be at most 3/4 of it.
MAX_IMAGE_BYTES = 5 * 1024 * 1024 * 3 // 4


def sniffed_image_type(body: bytes) -> Optional[str]:
    """The `IMAGE_TYPES` format `body` is by its magic bytes, or None. The declared content type is the
    uploader's claim; the API refuses an image block whose bytes do not match its `media_type`."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_block(media_type: str, body: bytes) -> dict[str, Any]:
    """One `image` content block for `body`, whose bytes sniffed as `media_type`."""
    return {"type": "image", "source": {
        "type": "base64", "media_type": media_type, "data": base64.b64encode(body).decode("ascii"),
    }}


def text_blocks(message: Any) -> list[str]:
    """The `text` blocks of a reply, in order (a thinking model prepends a `thinking` block, a tool call carries
    none). The one reader of the SDK's content shape: callers join or take the first."""
    return [
        getattr(block, "text", "") or ""
        for block in getattr(message, "content", None) or []
        if getattr(block, "type", None) == "text"
    ]


def first_text(message: Any) -> str:
    """The first non-blank `text` block of a reply, stripped, else ''."""
    return next((text.strip() for text in text_blocks(message) if text.strip()), "")


@dataclass(frozen=True)
class SupportLLM:
    """What the support agent talks to: a ready client, and the provider and model it was built for."""

    client: Any  # anthropic.Anthropic
    provider: str  # DEEPSEEK or ANTHROPIC
    model: str
    # Only Anthropic is sent images here: DeepSeek's endpoint accepts image blocks, but only
    # `DEEPSEEK_FLASH` reads them (v4-pro guesses; measured). `reads_images` is the per-model
    # gate the title paths use.
    accepts_images: bool
    #: Request parameters this model needs over the caller's own (`SUPPORT_THINKING_OPTIONS` or none).
    request_options: Mapping[str, Any] = field(default_factory=dict)


def _credential_source(knob: str, *, env_fallback: bool) -> Optional[tuple[str, Any]]:
    """`(provider, credential row)` for the caller whose credential knob is `knob`; the row is None on the env
    path (knob blank and `env_fallback`). None when the knob is blank with no env fallback, or set but naming no
    usable row: a bad id, a missing row, or another type. That is "unavailable", never the env path, since the
    operator asked for this key. Read on the operator plane in a session of its own (as
    `cloud_node_launch._fleet_tailscale_credential`): a caller may run bound to a user's org, where the
    operator's credential does not resolve — and filtered to the operator plane (org zero) too, so a tenant's
    credential id pasted into the knob never resolves either. The returned row is detached."""
    from sqlalchemy import select

    from app.operator_settings import reads as settings_reads
    from app.db import SessionLocal
    from app.entity_shape import optional_uuid
    from app.models.credentials import Credential
    from app.tenancy import seam
    from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

    raw = settings_reads.get_str(knob).strip()
    if not raw:
        return (SUPPORT_LLM_PROVIDER, None) if env_fallback else None
    credential_id = optional_uuid(raw)
    if credential_id is None:
        log.warning("%s unavailable: %r is not a credential id", knob, raw)
        return None
    with seam.detached(), seam.operator_scope(), SessionLocal() as fleet:
        row = fleet.scalars(
            select(Credential).where(
                Credential.id == credential_id,
                Credential.type.in_(tuple(SUPPORT_CREDENTIAL_PROVIDERS)),
                Credential.org_id.in_(OPERATOR_PLANE_ORG_IDS),
            )
        ).one_or_none()
    if row is None:
        log.warning(
            "%s unavailable: %s names no Anthropic or DeepSeek API key credential (deleted, or another type)",
            knob, raw,
        )
        return None
    return SUPPORT_CREDENTIAL_PROVIDERS[row.type], row


def knob_llm(
    knob: str, *, timeout: float, model_knob: Optional[str] = None, env_fallback: bool = False,
    tier: Optional[str] = None,
) -> Optional[SupportLLM]:
    """The client, provider and model the credential knob `knob` names, or None when it is unavailable.

    `env_fallback` makes a blank knob mean the env (`SUPPORT_LLM_PROVIDER`, `SUPPORT_MODEL`, the env keys via
    `make_client`); without it a blank knob is unavailable. The model is `tier` on that provider when given, else
    `model_knob`'s tier when set, else that path's default."""
    from app.operator_settings import reads as settings_reads
    from app.credential_crypto import open_secret

    source = _credential_source(knob, env_fallback=env_fallback)
    if source is None:
        return None
    provider, row = source
    if row is None:
        client = make_client(timeout=timeout, provider=provider)
        default_model = SUPPORT_MODEL
    else:
        try:
            key = open_secret(row, "api_key").strip()
        except (KeyError, ValueError):
            key = ""
        client = _client(provider, key, timeout)
        if client is None:
            # The id only: the key never reaches a log.
            log.warning(
                "%s unavailable: credential %s holds no usable API key (or the SDK is absent)", knob, row.id,
            )
        default_model = SUPPORT_DEFAULT_MODELS[provider]
    if client is None:
        return None
    stored = tier or (settings_reads.get_str(model_knob) if model_knob else "")
    return _llm(client, provider, _support_model(provider, stored, default_model))


def _llm(client: Any, provider: str, model: str) -> SupportLLM:
    return SupportLLM(
        client=client, provider=provider, model=model, accepts_images=provider == ANTHROPIC,
        request_options=SUPPORT_THINKING_OPTIONS if model in SUPPORT_THINKING_MODELS else {},
    )


def support_llm(*, timeout: float) -> Optional[SupportLLM]:
    """The support agent's client, provider and model, or None when it is unavailable.

    Settings > Integrations > Support agent when set, else the env (`SUPPORT_LLM_PROVIDER`, `SUPPORT_MODEL`,
    the env keys via `make_client`). The model is `support_llm_model`'s tier on that provider when set, else
    that path's default."""
    return knob_llm(
        "support_llm_credential_id", timeout=timeout, model_knob="support_llm_model", env_fallback=True,
    )


#: Every tier model of each provider, by tier: what `provider_model` maps across. DeepSeek's two models serve two
#: tiers each, so the later (lower) tier wins, matching `SUPPORT_DEFAULT_MODELS`.
_MODEL_TIERS = {model: tier for tiers in SUPPORT_TIER_MODELS.values() for tier, model in tiers.items()}


#: The legacy DeepSeek flash id, folded like the current one while a stored knob or env still names it.
LEGACY_DEEPSEEK_FLASH = "deepseek-v4-flash"


def provider_model(provider: str, configured: str) -> str:
    """`configured` on `provider`: another provider's tier model becomes `provider`'s model of the same tier;
    `provider`'s own model or any other id stays as written. The legacy flash id folds too, so a deployed
    env that still names it calls the current id."""
    if configured == LEGACY_DEEPSEEK_FLASH:
        return DEEPSEEK_FLASH if provider == DEEPSEEK else SUPPORT_TIER_MODELS[provider]["low"]
    if configured in SUPPORT_TIER_MODELS[provider].values():
        return configured
    tier = _MODEL_TIERS.get(configured)
    return SUPPORT_TIER_MODELS[provider][tier] if tier else configured


def summary_llm(*, timeout: Optional[float] = None) -> Optional[SupportLLM]:
    """The client and model behind task titles, tags and org slugs, or None when it is unavailable: the support
    agent's provider and key (`support_llm`), with `summary_model` on that provider (`provider_model`).
    `timeout` None is the title budget (`summary_timeout`)."""
    llm = knob_llm(
        "support_llm_credential_id", timeout=summary_timeout() if timeout is None else timeout, env_fallback=True,
    )
    if llm is None:
        return None
    return _llm(llm.client, llm.provider, provider_model(llm.provider, summary_model()))


class LLMUnavailable(Exception):
    """No client could be built; the message is the readable reason (never a key)."""


#: The operator knob a trigger's model preflight falls back to when the trigger names no key (AD-I4).
TRIGGER_PREFLIGHT_KNOB = "trigger_preflight_llm_credential_id"


def trigger_preflight_llm(session: Any, trigger: Any, principal: Any, *, timeout: float) -> SupportLLM:
    """The client and model a trigger's model preflight calls, or `LLMUnavailable` saying why not.

    The trigger's own `preflight_credential_id` first: an Anthropic API key `principal` (the trigger's owner)
    may use on the trigger's project (`authz.usable_credential_clause`), read through `session`, which is bound
    to the trigger's org. Else the operator's `trigger_preflight_llm_credential_id`. Either way the model is the
    catalog's lowest tier on the provider: a preflight is a cheap yes/no or a short list."""
    from sqlalchemy import select

    from app import model_catalog
    from app.operator_settings import reads as settings_reads
    from app.authz.visibility import usable_credential_clause
    from app.credential_crypto import open_secret
    from app.models.credentials import Credential

    if trigger.preflight_credential_id is None:
        if not settings_reads.get_str(TRIGGER_PREFLIGHT_KNOB).strip():
            raise LLMUnavailable("no model key: the trigger names none and the platform key is not set")
        llm = knob_llm(TRIGGER_PREFLIGHT_KNOB, timeout=timeout, tier=model_catalog.TIER_LOW)
        if llm is None:
            raise LLMUnavailable("the platform's preflight key is unusable (deleted, another type, or no API key)")
        return llm
    row = session.scalars(
        select(Credential).where(
            Credential.id == trigger.preflight_credential_id,
            Credential.type == "anthropic_api_key",
            usable_credential_clause(principal, trigger.project_id),
        )
    ).one_or_none()
    if row is None:
        raise LLMUnavailable("the trigger's Anthropic API key is gone, or its owner may no longer use it")
    try:
        key = open_secret(row, "api_key").strip()
    except (KeyError, ValueError):
        key = ""
    client = _client(ANTHROPIC, key, timeout)
    if client is None:
        raise LLMUnavailable("the trigger's Anthropic API key credential holds no usable API key")
    return _llm(client, ANTHROPIC, SUPPORT_TIER_MODELS[ANTHROPIC][model_catalog.TIER_LOW])


def _support_model(provider: str, stored: str, default_model: str) -> str:
    """The model for the `support_llm_model` value: a tier's model on `provider`, a free-text id stored
    before the knob was a select as written, else `default_model`."""
    from app import model_catalog

    raw = stored.strip()
    tier = model_catalog.tier_of(raw)
    if tier:
        return SUPPORT_TIER_MODELS[provider][tier]
    return raw or default_model
