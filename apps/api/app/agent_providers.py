"""The closed set of agent CLIs the fleet can run a runner on — the control plane's door onto the node's catalogue.

The catalogue itself (`AgentProvider`, `Degradation`, every provider row, `PROVIDERS`, `by_value`, the
renamed spellings) is single-homed in `apps/jaahd/agent_providers.py`: `apps/jaahd` may not import
`apps/api`, so the node's file is the one home and this module LOADS it by path and re-exports
it — the `app/secret_redact.py` pattern. By path, never via `sys.path`, which must not reach the daemon's
other modules. What follows the re-export is CP-only: the authorize-URL anchors, the API-key probe, model-id
attribution and the Jaah-account provider, read off each row's `capabilities` (or the default), none of which the
node reads.

Read by `dispatch_compose.compose.compose`, `app/routes/fleet.py::_account_candidates`, `routes/commands.py`,
`durable/accounts.py` (`login_completion`) and `GET /api/accounts/providers`. Callers read a field off the
record; nothing branches on a `value`.

Where the node's file is found: `app/shared_module.py` (the image's `/app/shared`, else the checkout).
"""
from __future__ import annotations

from urllib.parse import urlsplit

from app import shared_module

_NODE = shared_module.load("_node_agent_providers", shared_module.candidates("agent_providers.py", "apps/jaahd"))

Degradation = _NODE.Degradation
AgentProvider = _NODE.AgentProvider
CLAUDE = _NODE.CLAUDE
CODEX = _NODE.CODEX
DEEPSEEK = _NODE.DEEPSEEK
ANTIGRAVITY = _NODE.ANTIGRAVITY
PI = _NODE.PI
PROVIDERS: tuple = _NODE.PROVIDERS
PROVIDER_VALUES: tuple[str, ...] = _NODE.PROVIDER_VALUES
DEFAULT_PROVIDER: str = _NODE.DEFAULT_PROVIDER
PASTE_CODE: str = _NODE.PASTE_CODE
AWAIT_CREDENTIALS: str = _NODE.AWAIT_CREDENTIALS
by_value = _NODE.by_value
capabilities_of = _NODE.capabilities_of
account_layer = _NODE.account_layer
implied_on_wire = _NODE.implied_on_wire
Capabilities = _NODE.Capabilities
MODES: tuple[str, ...] = _NODE.MODES
VOCABULARY_SPELLINGS: tuple[tuple[str, str], ...] = _NODE.VOCABULARY_SPELLINGS
VOCABULARY_SNAPSHOT_KEY: str = _NODE.VOCABULARY_SNAPSHOT_KEY
CURRENT_VOCABULARY: str = _NODE.CURRENT_VOCABULARY


#: The provider a Jaah-account (Bedrock) run launches: the default one, the one CLI Bedrock is wired for
#: (`app/lease_drain.py` routes only it; the node's `fleet_dispatch.launch_runner` refuses any other).
JAAH_ACCOUNT_PROVIDER = by_value(DEFAULT_PROVIDER)

#: HIPAA surface -> the vendor entity a provider's CONSUMER subscription reaches (`app/hipaa.py::SURFACES`), where
#: that differs from the row's `capabilities.vendor` an API key reaches. The console spells the key too
#: (`apps/web/src/lib/providerCatalogue.ts::DEFAULT_SUBSCRIPTION_SURFACE`).
SUBSCRIPTION_SURFACES: dict[str, str] = {"claude_subscription": "anthropic_consumer"}


def _authorize_url_anchor(value: str) -> tuple[str, bool] | None:
    """`value`'s `(prefix, exact)` anchor (`Capabilities.authorize_url_anchor`), None for no opinion or an unknown
    value. The control plane's own second anchor on an authorize URL a node scraped (the console's `safeHref` admits
    any http(s) URL; cf. `routes/commands.py::_oauth_url_from_reply`)."""
    caps = capabilities_of(value)
    return caps.authorize_url_anchor if caps is not None else None


def authorize_url_prefix(value: str) -> str | None:
    """What `value`'s authorize URL must start with, or None when this provider declares no constant one.

    None is "no opinion" (a provider whose host varies, or a value from a newer build): the caller publishes the
    URL it holds. The decision itself is `authorize_url_admits`; this returns the expected string for diagnostics."""
    anchor = _authorize_url_anchor(value)
    return anchor[0] if anchor is not None else None


def authorize_url_exact(value: str) -> bool:
    """Whether `value`'s authorize URL must EQUAL its prefix (a constant verification page), False
    for a prefix match or no opinion. Served to the browser beside the prefix (`AgentProviderOut`), since
    it now anchors the link; the decision here stays `authorize_url_admits`."""
    anchor = _authorize_url_anchor(value)
    return anchor[1] if anchor is not None else False


def authorize_url_admits(value: str, url: str) -> bool | None:
    """Whether `url` is one this roster will let `value` publish -- None when it has no opinion.

    None: publish the URL already held (a varying host's login may not move). False: fail closed as "no URL",
    never a raise or substituted URL. An exact provider compares with `==`, refusing prefilled
    `user_code`, suffix-glued and `/../` tails."""
    anchor = _authorize_url_anchor(value)
    if anchor is None:
        return None
    prefix, exact = anchor
    return url == prefix if exact else url.startswith(prefix)


# How an API-key account's key is checked before the account is written: ONE `GET` of the row's
# `capabilities.api_key_probe_url` carrying the key in a header (`routes/accounts.py::_probe_api_key`). The
# header is the ENDPOINT's convention, keyed by its host: Google reads its key from `x-goog-api-key` (never a
# query string); every other endpoint takes `Authorization: Bearer`. CP-only: the node never probes.
_KEY_HEADER_BY_HOST: dict[str, tuple[str, str]] = {
    "generativelanguage.googleapis.com": ("x-goog-api-key", ""),
}
_BEARER: tuple[str, str] = ("Authorization", "Bearer")


def api_key_probe(value: str) -> tuple[str, str, str] | None:
    """`(url, header, scheme)` that `value`'s API key is verified with at registration and rotation, or None
    when nothing is probed (Anthropic: the Credentials page's own tester checks its key)."""
    caps = capabilities_of(value)
    url = caps.api_key_probe_url if caps is not None else None
    if not url:
        return None
    header, scheme = _KEY_HEADER_BY_HOST.get(urlsplit(url).hostname or "", _BEARER)
    return url, header, scheme


def provider_of_model(model: str) -> str | None:
    """The `PROVIDER_VALUES` member whose models `model` names (a usage line, a session's model — the
    operator dashboard's `provider` slice), by the lowercased prefixes each row's
    `capabilities.model_id_prefixes` lists, in catalogue order: first match wins. None for an id no prefix
    claims — unknown stays unknown, never guessed into a provider."""
    lowered = (model or "").strip().lower()
    return next((p.value for p in PROVIDERS for prefix in p.capabilities.model_id_prefixes
                 if lowered.startswith(prefix)), None)


# ── The default runtime's launch-contract names, read by lease_drain ──
#: The env var the node exports a runner's account home in (`ResolvedAccount.config_dir`): empty or absent, the
#: legacy-runtime CLI opens its default, unauthenticated home.
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
#: The env var that moves a SHARED OAuth account's token store (`ResolvedAccount.securestorage_dir`).
SECURESTORAGE_DIR_ENV = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
