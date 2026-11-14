"""The integration catalog: the single home of every vendor fact, one TOML spec per vendor.

Each vendor is `vendors/<slug>.toml` beside this module, with its logo at `vendors/logos/<slug>.svg`; the slugs
loaded must equal `vendors/EXPECTED.txt` (the oracle list). This loader validates every spec at import, so a
malformed vendor fails the gate, and derives from it what the existing engine already runs on:

* a `CredentialTypeSpec` in `credentials/registry.py::CREDENTIAL_TYPES` (unless the spec adopts an existing
  type through `credential_type`, when only catalog metadata is added and the hand-written type is untouched);
* an `OAuthProvider` in `oauth_providers.PROVIDERS` for an `oauth` auth method;
* a Test: an `HttpTestSpec` (one declared HTTP call), a format check, or a `module:function` hook, performed by
  `credentials/testers.py::catalog_tester`.

An adopting spec with an `oauth` method the registry has no provider for extends its hand-written type with the
fields Connect fills (`extended_type`): the type's own fields keep their kinds, its required key fields become one
`require_one_of` group beside the grant, and rows saved before stay exactly as they were. Its `[test]`, if any, is
the Test of Connect rows only (`grant_test`); a pasted key keeps the type's own Test.

Adding a key vendor or an OAuth vendor is therefore one spec file plus a logo: no route, no component.

THE PINNED SCHEMA (every later vendor wave and the docs generator build against it)::

    slug = "acme"                     # ^[a-z][a-z0-9_]{1,39}$; the credential type slug unless overridden
    label = "Acme"
    category = "dev_tools"            # one of CATEGORIES
    credential_type = "acme"          # optional; an existing CREDENTIAL_TYPES slug adopts that type
    vendor_docs_url = "https://..."   # required: where the auth facts were confirmed
    summary = "One line for the gallery card."
    default_base_url = "https://..."  # optional; derived from the test URL when the host is fixed

    [[auth]]                          # ordered: the first is the primary method
    kind = "oauth"                    # oauth | key | basic | service_account | db | mcp
    [auth.oauth]                      # OAuthProvider fields by name, plus:
    jaah_app = true                   #   derives knob `oauth_app.<slug>` (a Jaah-owned app; BYO stays allowed)
    on_grant = "module:function"      #   optional post-exchange hook (`oauth_providers.OAuthProvider.on_grant`)
    origin_field = "base_url"         #   optional: endpoints on each customer's own host, written `{origin}/path`,
    origin_hosts = ["acme.com"]       #   filled from that declared url field; only a subdomain of these fills it
                                      #   (or written `https://{subdomain}.acme.com/path`: a required text field
                                      #   holding the customer's label, asked before consent - Zendesk)
    on_grant_config = "module:function"   # optional: `(provider, grant, config) -> {field: value}` after the exchange (the
    grant_fields = ["tenant_id"]      #   organisation or site a grant reaches); `grant_fields` names what it fills
    client_metadata = true            #   optional: the client is a metadata document Jaah serves (no knob, no secret)
    fields = [...]                    #   optional: fields only Connect fills (none required), e.g. a cloud id
    [auth.oauth.registration]         #   free-form, read by the docs generator
    [[auth]]
    kind = "key"
    fields = [{ key = "api_key", label = "API key", kind = "secret", required = true, help = "..." }]

    [test]                            # HTTP (default) | kind = "format" | tester = "module:function"
    method = "GET"                    # GET | POST
    path = "https://api.acme.com/v1/me"   # absolute https URL; `{field}` placeholders, see HttpTestSpec
    body = '{"q":1}'                  # POST only; may carry one `{field}`: a non-secret value of [A-Za-z0-9_.-]
    auth = { field = "api_key", header = "Authorization", prefix = "Bearer" }
                                      # or { basic = ["user", "key"] }, or { basic = ["key"] } (key as user name; optional constant `password`)
    identity = [["user", "name"]]
    field_headers = { "X-Acme-Account" = "account_id" }   # optional: a non-secret field's value as a header
    rejected_statuses = [401, 403, 404]   # optional: the statuses that mean "key refused" (default 401, 403)
    rejected_when = { path = ["errors", 0, "extensions", "code"], values = ["UNAUTHENTICATED"] }
                                      # optional: a body that says "refused" whatever the status (GraphQL, OAuth)
    oauth_scheme = "Bearer"           # optional: the scheme an OAuth row's access token is sent under
    rejected_text = "ERROR 120 :: [A-Z ]+"   # optional: a regex; a body matching it says "refused" whatever the status
    require_true = ["user", "active"]  # optional: a 2xx is green only when this JSON path is literally `true`
    require_equal = { path = ["client_id"], field = "client_id" }
                                      # optional: a 2xx is green only when this JSON path equals the row's field
    host_suffixes = ["acme.com"]      # optional: a url field's host must be one of these or under one (may then POST)
    [test.token]                      # optional: mint a client-credentials token first, sent as the Bearer
    url = "https://login.acme.com/{tenant_id}/token"  # placeholder: the test's url field origin, or one path segment
    client_id_field = "client_id"     #   form fields sent in the POST body (never a URL)
    client_secret_field = "client_secret"
    scope = "https://api.acme.com/.default"   # optional
    grant_type = "client_credentials" #   "" sends none (a vendor login that takes only the pair)
    audience = "acme-api"             #   optional: an `audience` form field (Auth0-style token endpoints)
    client_auth = "body"              #   body (default) | basic: the pair as HTTP Basic instead of form fields
    prefix = "Bearer"                 #   the Authorization scheme the minted token is sent with

    [mcp]                             # MCP vendors: url, url_editable, transport, auth (project link + Test)
    [mcp.registration]                #   optional, free-form: console_url, steps, issuer (the docs generator's setup task)
    [docs]                            # reader-page prose: agent_uses, prerequisites, limitations, ...

Category labels are single-homed in `categories.toml` (`CATEGORIES`). Nothing here imports an HTTP client or opens a secret.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Mapping, Optional
from urllib.parse import urlsplit

from app.credentials.registry import CREDENTIAL_TYPES, GENERIC_FALLBACK_TYPES, CredentialTypeSpec, FieldSpec
from app.oauth_providers import PROVIDERS, OAuthProvider, OAuthScope

#: Where the real vendor specs live.
VENDORS_DIR = Path(__file__).resolve().parent / "vendors"

#: The closed category enum and each category's label, in gallery order: read from `categories.toml` beside this
#: module, their single home (the integration docs generator reads the same file).
CATEGORIES: dict[str, str] = dict(
    tomllib.loads((Path(__file__).resolve().parent / "categories.toml").read_text(encoding="utf-8"))["categories"]
)

_AUTH_KINDS: tuple[str, ...] = ("oauth", "key", "basic", "service_account", "db", "mcp")
_FIELD_KINDS: tuple[str, ...] = ("text", "url", "email", "secret")

#: The knob prefix every Jaah-owned catalog OAuth app is designated by: `oauth_app.<slug>`.
OAUTH_APP_KNOB_PREFIX = "oauth_app."
#: The credential type every such knob names (`credentials/registry.py`).
OAUTH_APP_TYPE = "oauth_app"

# The parameters the OAuth wire already sends (`oauth2_wire`): a renamed client parameter may not shadow one.
_RESERVED_OAUTH_PARAMS = frozenset({
    "client_secret", "code", "state", "redirect_uri", "grant_type", "refresh_token", "code_verifier",
    "code_challenge", "code_challenge_method", "scope", "response_type",
})
_SLUG = re.compile(r"[a-z][a-z0-9_]{1,39}")
# A hook names a module of this app only (`app.`), so no spec can import anything else.
_HOOK = re.compile(r"app\.[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")
#: A `{field}` placeholder in a Test URL or auth header template (the tester fills a template with the same pattern).
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
#: The `[mcp].auth` values whose Connect signs in at the server (discovery, then RFC 7591 or the row's own client).
MCP_SIGN_IN = frozenset({"oauth_dcr", "oauth_registered"})
_MCP_AUTH = re.compile(r"oauth_dcr|oauth_registered|none|bearer_field:[a-z][a-z0-9_]{0,63}")
_HEADER = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


class CatalogError(RuntimeError):
    """A spec the loader refuses. `RuntimeError`, so an import-time refusal fails the gate (`python -O` keeps it)."""


@dataclass(frozen=True)
class TokenExchangeSpec:
    """A client-credentials token minted before the Test call and sent on it as `Authorization: Bearer`.

    `url` carries at most one `{field}` placeholder: the Test URL's own url field as the whole origin (a tenant
    instance's login route), or one path segment on a fixed host (`https://login.example/{tenant_id}/token`).
    The client id and secret travel in the form-encoded POST body, or as HTTP Basic when `client_auth` is
    `basic` (a vendor that refuses the pair in the body); never in a URL. `audience` adds that form field."""

    url: str
    client_id_field: str
    client_secret_field: str
    scope: Optional[str] = None
    grant_type: str = "client_credentials"
    prefix: str = "Bearer"
    audience: Optional[str] = None
    client_auth: Literal["body", "basic"] = "body"

    @property
    def placeholders(self) -> tuple[str, ...]:
        return tuple(PLACEHOLDER.findall(self.url))


@dataclass(frozen=True)
class HttpTestSpec:
    """A Test that is one declared HTTP call: a superset of `registry.KeyTestSpec`.

    `url` is an absolute https URL. A `{field}` placeholder is filled from the row's config, in one of two places
    only, so a tenant value can never move the request off the vendor: the whole origin (`{base_url}/me`, where
    the field is the type's `url` field and so the sealing destination) or one DNS label of the host
    (`https://{subdomain}.acme.com/me`, the value one `[A-Za-z0-9-]` label) or one path segment of a fixed host
    (`https://api.acme.com/ex/{cloud_id}/me`, the value the same shape). Every dial of a URL carrying a
    placeholder goes through `http_bounded.vetted_addresses` (testers.py).

    `auth`: `header` (the key in `header_name`, after `value_prefix`; or `value_template`, the whole value with
    `{field}` placeholders filled from the row, for a vendor whose header carries several fields), `basic`
    (`user_field` and `key_field` as HTTP Basic; with no `user_field`, the key is the user name and the password
    is `basic_password`, a constant, empty unless the vendor names one), `query` (the key as query
    parameter `query_param`: the vendor's own scheme, never logged),
    `sigv4` (`key_field` is the secret access key, `user_field` the access key id; `sigv4_service` and
    `sigv4_region`) or `none`; `user_suffix` is appended to the Basic user name (a vendor that wants
    `<email>/token`). An OAuth-connected row sends its access token as `Authorization: <oauth_scheme>`
    (`Bearer` unless the vendor names its own); a `token` Test (`TokenExchangeSpec`) sends the token it minted,
    after the exchange's `prefix`. `rejected_path`/`rejected_values`: a JSON body (any status) whose
    value at that path is one of the values is the vendor refusing the credential, for a vendor that answers a bad
    token with a 200 GraphQL error or a bad client with a 400 code. `rejected_text`: a regex searched in the body
    (any status, read as text) that is the vendor refusing the credential, for a vendor that answers a bad key
    with a 200 plain-text error; the match is quoted in the sentence, so the pattern bounds what is echoed.
    A POST `body` may carry one `{field}` placeholder of a non-secret, non-url field, filled only with a value of
    `[A-Za-z0-9_.-]` (an id the vendor needs in the query), so a tenant value cannot reshape the body.
    `host_suffixes` pins a url-field origin to the vendor's own domains (the host is one of them or a subdomain),
    checked before anything is sent; such an origin is the vendor's, not tenant-chosen, so it may take a POST.
    On a 2xx, `require_true` names a JSON path that must be literally `true`
    (a vendor that answers 200 for an unknown key); `require_equal_path` names a JSON path whose value must equal
    the row's `require_equal_field` (a token check that names the app the token was minted for); then `list_field` names a key that must hold a list, else `identity` lists the JSON paths tried for
    "Connected to {label} {identity_phrase} {identity}.".

    `field_headers` pairs a header with a non-secret field sent beside the key (a vendor that wants an account
    id next to the key), as `registry.KeyTestSpec.field_headers` does. `rejected_statuses` are the answers read
    as "<Vendor> rejected the <key>" (AC2.6): 401 and 403, plus 404 for a vendor that answers a refused key so.
    """

    url: str
    method: Literal["GET", "POST"] = "GET"
    body: Optional[str] = None
    content_type: Optional[str] = None
    auth: Literal["header", "basic", "query", "sigv4", "none"] = "header"
    key_field: Optional[str] = None
    user_field: Optional[str] = None
    user_suffix: str = ""
    header_name: str = "Authorization"
    value_prefix: str = ""
    value_template: Optional[str] = None
    basic_password: Optional[str] = None
    query_param: Optional[str] = None
    sigv4_service: Optional[str] = None
    sigv4_region: Optional[str] = None
    accept: Optional[str] = "application/json"
    extra_headers: tuple[tuple[str, str], ...] = ()
    identity: tuple[tuple[str | int, ...], ...] = ()
    identity_phrase: str = "as"
    list_field: Optional[str] = None
    field_headers: tuple[tuple[str, str], ...] = ()
    rejected_statuses: tuple[int, ...] = (401, 403)
    rejected_path: tuple[str | int, ...] = ()
    rejected_values: tuple[str, ...] = ()
    oauth_scheme: str = "Bearer"
    token: Optional[TokenExchangeSpec] = None
    require_true: tuple[str | int, ...] = ()
    require_equal_path: tuple[str | int, ...] = ()
    require_equal_field: Optional[str] = None
    rejected_text: Optional[str] = None
    host_suffixes: tuple[str, ...] = ()

    @property
    def placeholders(self) -> tuple[str, ...]:
        return tuple(PLACEHOLDER.findall(self.url))

    @property
    def body_placeholders(self) -> tuple[str, ...]:
        return tuple(PLACEHOLDER.findall(self.body or ""))


@dataclass(frozen=True)
class FormatTestSpec:
    """The format-check fallback for a vendor with no safe read-only call (AC3.2): required fields non-blank,
    each `patterns` field matching its regex, and `public_host_field`'s host resolving to a public address.
    The result says "Format checked - <Vendor> was not called"."""

    patterns: tuple[tuple[str, str], ...] = ()
    public_host_field: Optional[str] = None


@dataclass(frozen=True)
class AuthMethod:
    """One way to connect, in the spec's order (the first is primary)."""

    kind: str
    fields: tuple[FieldSpec, ...] = ()


@dataclass(frozen=True)
class McpSpec:
    """An MCP vendor's server: what the project link hands a runner (`app/mcp_links.py`) and what Test dials when
    the spec declares no `[test]` (`credentials/testers.py::_catalog_mcp`). `auth`: `oauth_dcr`, `oauth_registered`,
    `bearer_field:<key>` or `none`."""

    url: str
    url_editable: bool = False
    transport: Literal["streamable_http", "sse"] = "streamable_http"
    auth: str = "none"

    @property
    def sign_in(self) -> bool:
        """Whether Connect signs in at the server's own authorization server (`app/mcp_oauth.py`)."""
        return self.auth in MCP_SIGN_IN

    def server_url(self, config: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
        """`(url, None)`: the server a row dials - its own `url` where the vendor lets it be edited, else the spec's
        with each `{field}` filled from the row (one DNS label each) - or `(None, the sentence saying why not)`.
        The one resolution Test, the project link and Connect share."""
        url = self.url
        own = str(config.get("url") or "").strip()
        if self.url_editable and own:
            return own, None
        for name in PLACEHOLDER.findall(url):
            value = str(config.get(name) or "").strip()
            if not value:
                return None, f"{name} is required before testing."
            if not _DNS_LABEL.fullmatch(value):
                return None, f"{name} must be a single name such as 'acme' - no dots, slashes or spaces."
            url = url.replace(f"{{{name}}}", value.lower())
        return url, None


@dataclass(frozen=True)
class IntegrationSpec:
    """One loaded, validated vendor spec. `raw` keeps the parsed TOML for the docs generator."""

    slug: str
    label: str
    category: str
    credential_type: str
    vendor_docs_url: str
    summary: str
    auth: tuple[AuthMethod, ...]
    http_test: Optional[HttpTestSpec] = None
    format_test: Optional[FormatTestSpec] = None
    tester: Optional[str] = None
    provider: Optional[OAuthProvider] = None
    jaah_app: bool = False
    mcp: Optional[McpSpec] = None
    default_base_url: Optional[str] = None
    logo_svg: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def oauth(self) -> bool:
        return self.provider is not None

    @property
    def app_knob(self) -> Optional[str]:
        """The operator knob naming this vendor's Jaah-owned OAuth app, or None when there is none."""
        return f"{OAUTH_APP_KNOB_PREFIX}{self.slug}" if self.jaah_app else None

    @property
    def adopts_existing_type(self) -> bool:
        return self.credential_type in _HAND_WRITTEN_TYPES


# The types written by hand in the registry, captured before any spec is derived: an adopting spec layers over
# one of these, and the conflict guard compares against them.
_HAND_WRITTEN_TYPES: frozenset[str] = frozenset(CREDENTIAL_TYPES)
_HAND_WRITTEN_PROVIDERS: frozenset[str] = frozenset(PROVIDERS)
#: Each hand-written type as written, before `extended_type` layers a Connect over it.
_HAND_WRITTEN_TYPES_SPECS: dict[str, CredentialTypeSpec] = dict(CREDENTIAL_TYPES)
_HAND_WRITTEN_FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    slug: spec.fields for slug, spec in _HAND_WRITTEN_TYPES_SPECS.items()
}


# ── Parsing ───────────────────────────────────────────────────────────────────────────────────────────────


def _need(data: Mapping[str, Any], key: str, kind: type, where: str) -> Any:
    value = data.get(key)
    if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
        raise CatalogError(f"{where}: `{key}` is required and must be a non-empty {kind.__name__}")
    return value.strip() if isinstance(value, str) else value


def _opt(data: Mapping[str, Any], key: str, kind: type | tuple[type, ...], where: str, default: Any = None) -> Any:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, kind):
        raise CatalogError(f"{where}: `{key}` has the wrong type")
    return value


def _https_url(value: str, where: str, *, templated: bool = False) -> str:
    probe = PLACEHOLDER.sub("x", value) if templated else value
    parts = urlsplit(probe)
    if parts.scheme != "https" or not parts.hostname or any(char.isspace() for char in value):
        raise CatalogError(f"{where}: {value!r} must be an absolute https URL")
    return value


def _unknown_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise CatalogError(f"{where}: unknown key(s) {unknown}")


def _parse_fields(raw: Any, where: str) -> tuple[FieldSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CatalogError(f"{where}: `fields` must be a list of tables")
    out: list[FieldSpec] = []
    for index, item in enumerate(raw):
        at = f"{where}.fields[{index}]"
        if not isinstance(item, dict):
            raise CatalogError(f"{at}: must be a table")
        _unknown_keys(item, {"key", "label", "kind", "required", "help", "placeholder", "default", "outright"}, at)
        key = _need(item, "key", str, at)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise CatalogError(f"{at}: key {key!r} must be snake_case")
        kind = _opt(item, "kind", str, at, "text")
        if kind not in _FIELD_KINDS:
            raise CatalogError(f"{at}: kind {kind!r} is not one of {list(_FIELD_KINDS)}")
        required = _opt(item, "required", bool, at, True)
        help_text = _opt(item, "help", str, at)
        if kind == "secret" and _opt(item, "default", str, at) is not None:
            raise CatalogError(f"{at}: a secret field takes no default")
        if not help_text or not help_text.strip():
            # The docs page's "where to find it" steps are this text: a field without it is a thin page.
            raise CatalogError(f"{at}: `help` (where to find the value) is required")
        out.append(FieldSpec(
            key, _need(item, "label", str, at), kind, required,
            _opt(item, "placeholder", str, at), _opt(item, "default", str, at), help_text.strip(),
            _opt(item, "outright", bool, at, False),
        ))
    return tuple(out)


def _parse_provider(slug: str, label: str, raw: Mapping[str, Any], where: str) -> tuple[OAuthProvider, bool]:
    allowed = {
        "authorize_url", "token_url", "refresh_url", "scope", "scopes", "scope_separator", "pkce", "basic_auth",
        "send_grant_type", "refresh_grant_type", "response_type", "authorize_extra", "account_url",
        "account_field", "jaah_app", "on_grant", "registration", "token_ttl_seconds", "client_id_param", "expiring_grant_ok",
        "origin_field", "origin_hosts", "on_grant_config", "grant_fields", "client_metadata", "fields",
    }
    _unknown_keys(raw, allowed, where)
    raw, origin_label = _label_scoped(raw, where)
    origin_field, origin_hosts = _origin_scope(raw, where)
    authorize = _endpoint_url(_need(raw, "authorize_url", str, where), f"{where}.authorize_url", origin_field)
    token = _endpoint_url(_need(raw, "token_url", str, where), f"{where}.token_url", origin_field)
    refresh_url = _opt(raw, "refresh_url", str, where)
    if refresh_url is not None:
        _endpoint_url(refresh_url, f"{where}.refresh_url", origin_field)
    scopes_raw = _opt(raw, "scopes", list, where, [])
    scopes: list[OAuthScope] = []
    for index, item in enumerate(scopes_raw):
        at = f"{where}.scopes[{index}]"
        if not isinstance(item, dict):
            raise CatalogError(f"{at}: must be a table")
        _unknown_keys(item, {"name", "label", "optional", "why"}, at)
        scopes.append(OAuthScope(_need(item, "name", str, at), _need(item, "label", str, at),
                                 bool(_opt(item, "optional", bool, at, False))))
    scope = _opt(raw, "scope", str, where)
    if scopes and scope:
        raise CatalogError(f"{where}: `scope` and `scopes` are exclusive")
    account_url = _opt(raw, "account_url", str, where)
    if account_url is not None:
        _endpoint_url(account_url, f"{where}.account_url", origin_field)
    on_grant = _opt(raw, "on_grant", str, where)
    if on_grant is not None and not _HOOK.fullmatch(on_grant):
        raise CatalogError(f"{where}: on_grant {on_grant!r} must be `module:function`")
    extra = _opt(raw, "authorize_extra", dict, where)
    if extra is not None and not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()):
        raise CatalogError(f"{where}: authorize_extra must map strings to strings")
    jaah_app = bool(_opt(raw, "jaah_app", bool, where, False))
    client_metadata = bool(_opt(raw, "client_metadata", bool, where, False))
    if client_metadata and jaah_app:
        raise CatalogError(f"{where}: a client_metadata vendor's client is the document Jaah serves - no jaah_app knob")
    on_grant_config = _opt(raw, "on_grant_config", str, where)
    if on_grant_config is not None and not _HOOK.fullmatch(on_grant_config):
        raise CatalogError(f"{where}: on_grant_config {on_grant_config!r} must be `module:function`")
    grant_fields = _opt(raw, "grant_fields", list, where, [])
    if not all(isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) for key in grant_fields):
        raise CatalogError(f"{where}: grant_fields lists field keys")
    if bool(grant_fields) != (on_grant_config is not None):
        raise CatalogError(f"{where}: on_grant_config and grant_fields go together")
    if on_grant_config is not None and on_grant is not None:
        raise CatalogError(f"{where}: an on_grant hook writes the row itself - on_grant_config adds to the engine's")
    ttl = _opt(raw, "token_ttl_seconds", int, where)
    if ttl is not None and not 300 <= ttl <= 86400:
        raise CatalogError(f"{where}: token_ttl_seconds must be between 300 and 86400")
    client_id_param = _opt(raw, "client_id_param", str, where, "client_id")
    if (not re.fullmatch(r"[a-z][a-z_]{1,31}", client_id_param) or client_id_param in _RESERVED_OAUTH_PARAMS
            or client_id_param in (_opt(raw, "authorize_extra", dict, where) or {})):
        raise CatalogError(f"{where}: client_id_param {client_id_param!r} must be a snake_case parameter name "
                           "that no OAuth call already sends")
    provider = OAuthProvider(
        key=slug, label=label, authorize_url=authorize, token_url=token, refresh_url=refresh_url,
        scope=scope, scopes=tuple(scopes), scope_separator=_opt(raw, "scope_separator", str, where, " "),
        pkce=bool(_opt(raw, "pkce", bool, where, True)), basic_auth=bool(_opt(raw, "basic_auth", bool, where, False)),
        send_grant_type=bool(_opt(raw, "send_grant_type", bool, where, True)),
        refresh_grant_type=bool(_opt(raw, "refresh_grant_type", bool, where, True)),
        response_type=_opt(raw, "response_type", str, where, "code"),
        authorize_extra=dict(extra) if extra else None,
        account_url=account_url, account_field=_opt(raw, "account_field", str, where),
        app_setting=f"{OAUTH_APP_KNOB_PREFIX}{slug}" if jaah_app else None,
        app_type=OAUTH_APP_TYPE if jaah_app else None,
        # A metadata-document client is the deployment's alone: a person's own app has no document to serve.
        client_source="platform" if client_metadata else "platform_or_row" if jaah_app else "row",
        public_client=client_metadata,
        on_grant=on_grant, token_ttl_seconds=ttl,
        client_id_param=client_id_param,
        expiring_grant_ok=bool(_opt(raw, "expiring_grant_ok", bool, where, False)),
        origin_field=origin_field, origin_hosts=origin_hosts, origin_label=origin_label,
        client_metadata=client_metadata, on_grant_config=on_grant_config, grant_fields=tuple(grant_fields),
    )
    return provider, jaah_app


#: A host-label endpoint: `https://{field}.<the vendor's domain>/path`, the field one label of the customer's own.
_LABEL_ENDPOINT = re.compile(r"https://\{([a-z][a-z0-9_]*)\}\.([a-z0-9-]+(?:\.[a-z0-9-]+)+)(/[A-Za-z0-9._~/-]*)")


def _label_scoped(raw: Mapping[str, Any], where: str) -> tuple[Mapping[str, Any], bool]:
    """`(raw, origin_label)`: a provider whose endpoints are written on a customer label of one fixed domain
    (`https://{subdomain}.zendesk.com/oauth/tokens`) rewritten as the origin-scoped one it is (`origin_field`, the one
    domain as `origin_hosts`, every endpoint `{origin}/path`), so one mechanism fills and pins every leg."""
    match = _LABEL_ENDPOINT.fullmatch(str(raw.get("authorize_url") or ""))
    if match is None:
        return raw, False
    if "origin_field" in raw or "origin_hosts" in raw:
        raise CatalogError(f"{where}: a host-label endpoint names its field itself - declare no origin_field")
    name, domain = match.group(1), match.group(2)
    out = dict(raw)
    for key in ("authorize_url", "token_url", "refresh_url", "account_url"):
        if key not in out:
            continue
        each = _LABEL_ENDPOINT.fullmatch(str(out[key]))
        if each is None or each.group(1) != name or each.group(2) != domain:
            raise CatalogError(f"{where}.{key}: every endpoint is on https://{{{name}}}.{domain}")
        out[key] = "{origin}" + each.group(3)
    out["origin_field"], out["origin_hosts"] = name, [domain]
    return out, True


def _origin_scope(raw: Mapping[str, Any], where: str) -> tuple[Optional[str], tuple[str, ...]]:
    """`(origin_field, origin_hosts)`: both or neither; each host a lowercase domain of two labels or more."""
    field_name = _opt(raw, "origin_field", str, where)
    hosts = _opt(raw, "origin_hosts", list, where)
    if (field_name is None) != (hosts is None):
        raise CatalogError(f"{where}: origin_field and origin_hosts go together")
    if field_name is None or hosts is None:
        return None, ()
    if not hosts or not all(isinstance(host, str) and re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
                            for host in hosts):
        raise CatalogError(f"{where}: origin_hosts must list lowercase domains such as acme.com")
    return field_name, tuple(hosts)


def _endpoint_url(value: str, where: str, origin_field: Optional[str]) -> str:
    """An OAuth endpoint: a fixed https URL, or - for an origin-scoped provider, and then always - `{origin}/path`."""
    if origin_field is None:
        return _https_url(value, where)
    if not re.fullmatch(r"\{origin\}/[A-Za-z0-9._~/-]*", value):
        raise CatalogError(f"{where}: an origin-scoped endpoint is written {{origin}}/path")
    return value


def _parse_test(raw: Mapping[str, Any], where: str) -> tuple[Optional[HttpTestSpec], Optional[FormatTestSpec], Optional[str]]:
    tester = _opt(raw, "tester", str, where)
    kind = _opt(raw, "kind", str, where, "hook" if tester else "http")
    if kind == "hook":
        _unknown_keys(raw, {"kind", "tester"}, where)
        if tester is None or not _HOOK.fullmatch(tester):
            raise CatalogError(f"{where}: tester must be `module:function`")
        return None, None, tester
    if tester is not None:
        raise CatalogError(f"{where}: `tester` is its own kind of test")
    if kind == "format":
        _unknown_keys(raw, {"kind", "patterns", "public_host_field"}, where)
        patterns = _opt(raw, "patterns", dict, where, {})
        for key, pattern in patterns.items():
            try:
                re.compile(pattern)
            except (re.error, TypeError) as exc:
                raise CatalogError(f"{where}.patterns.{key}: not a regex ({exc})") from None
        return None, FormatTestSpec(
            patterns=tuple(sorted(patterns.items())), public_host_field=_opt(raw, "public_host_field", str, where)
        ), None
    if kind != "http":
        raise CatalogError(f"{where}: kind {kind!r} is not http, format or hook")
    _unknown_keys(raw, {
        "kind", "method", "path", "body", "content_type", "auth", "accept", "headers", "identity", "identity_phrase",
        "list_field", "field_headers", "rejected_statuses", "rejected_when", "oauth_scheme", "require_true", "token",
        "host_suffixes", "require_equal", "rejected_text",
    }, where)
    url = _need(raw, "path", str, where)
    if url.startswith("{"):
        # A url field as the whole origin; the field itself is https-validated at write and test.
        if not re.fullmatch(r"\{[a-z][a-z0-9_]*\}(/\S*)?", url):
            raise CatalogError(f"{where}.path: {url!r} must be {{url_field}}/path")
    else:
        _https_url(url, f"{where}.path", templated=True)
    method = _opt(raw, "method", str, where, "GET").upper()
    if method not in ("GET", "POST"):
        raise CatalogError(f"{where}: method must be GET or POST")
    body = _opt(raw, "body", str, where)
    if body is not None and method != "POST":
        raise CatalogError(f"{where}: a body is sent with POST only")
    auth_raw = _opt(raw, "auth", (dict, str), where, "none")
    auth_fields: dict[str, Any] = {"auth": "none"}
    if isinstance(auth_raw, str):
        if auth_raw != "none":
            raise CatalogError(f"{where}: auth must be a table or \"none\"")
    elif "basic" in auth_raw:
        _unknown_keys(auth_raw, {"basic", "password", "user_suffix"}, f"{where}.auth")
        pair = auth_raw["basic"]
        if not (isinstance(pair, list) and len(pair) in (1, 2) and all(isinstance(x, str) for x in pair)):
            raise CatalogError(f"{where}.auth: basic is [user_field, password_field] or [key_field]")
        if len(pair) == 2 and "password" in auth_raw:
            raise CatalogError(f"{where}.auth: a constant password goes with [key_field] only")
        suffix = _opt(auth_raw, "user_suffix", str, f"{where}.auth", "")
        if ":" in suffix or any(char.isspace() for char in suffix):
            raise CatalogError(f"{where}.auth: user_suffix may hold no colon or space")
        if suffix and len(pair) != 2:
            raise CatalogError(f"{where}.auth: user_suffix needs a user field")
        # One field: the key is the Basic user name and the password a constant, empty by default (`curl -u key:`).
        auth_fields = ({"auth": "basic", "user_field": pair[0], "key_field": pair[1], "user_suffix": suffix}
                       if len(pair) == 2 else {"auth": "basic", "key_field": pair[0],
                                               "basic_password": _opt(auth_raw, "password", str, where, "")})
    elif "query" in auth_raw:
        _unknown_keys(auth_raw, {"query", "field"}, f"{where}.auth")
        auth_fields = {"auth": "query", "query_param": _need(auth_raw, "query", str, where),
                       "key_field": _need(auth_raw, "field", str, where)}
    elif "sigv4" in auth_raw:
        _unknown_keys(auth_raw, {"sigv4"}, f"{where}.auth")
        sig = auth_raw["sigv4"]
        if not isinstance(sig, dict):
            raise CatalogError(f"{where}.auth.sigv4: must be a table")
        _unknown_keys(sig, {"service", "region", "access_key_field", "secret_key_field"}, f"{where}.auth.sigv4")
        auth_fields = {
            "auth": "sigv4", "sigv4_service": _need(sig, "service", str, where),
            "sigv4_region": _need(sig, "region", str, where),
            "user_field": _need(sig, "access_key_field", str, where), "key_field": _need(sig, "secret_key_field", str, where),
        }
    else:
        _unknown_keys(auth_raw, {"field", "header", "prefix", "template"}, f"{where}.auth")
        auth_fields = {"auth": "header", "key_field": _need(auth_raw, "field", str, where),
                       "header_name": _opt(auth_raw, "header", str, where, "Authorization"),
                       "value_prefix": _opt(auth_raw, "prefix", str, where, ""),
                       "value_template": _opt(auth_raw, "template", str, where)}
        template = auth_fields["value_template"]
        if template is not None and (auth_fields["value_prefix"]
                                     or "{" + auth_fields["key_field"] + "}" not in template):
            raise CatalogError(f"{where}.auth: a template takes no prefix and must carry {{{auth_fields['key_field']}}}")
    headers = _opt(raw, "headers", dict, where, {})
    field_headers = _opt(raw, "field_headers", dict, where, {})
    for name, value in (*headers.items(), *field_headers.items(), (auth_fields.get("header_name", "Authorization"), "")):
        if not _HEADER.fullmatch(str(name)) or not isinstance(value, str):
            raise CatalogError(f"{where}: header {name!r} is not a valid header")
    names = [str(name).lower() for name in (*headers, *field_headers, auth_fields.get("header_name", "Authorization"))]
    if len(set(names)) != len(names):
        raise CatalogError(f"{where}: header names repeat (case-insensitive)")
    rejected = _opt(raw, "rejected_statuses", list, where, [401, 403])
    if not rejected or not all(isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 500
                                for code in rejected):
        raise CatalogError(f"{where}: rejected_statuses is a non-empty list of 4xx statuses")
    identity = _opt(raw, "identity", list, where, [])
    if not all(isinstance(path, list) and path and all(isinstance(s, (str, int)) for s in path) for path in identity):
        raise CatalogError(f"{where}: identity is a list of JSON paths")
    list_field = _opt(raw, "list_field", str, where)
    if list_field and identity:
        raise CatalogError(f"{where}: list_field and identity are exclusive")
    accept = _opt(raw, "accept", str, where, "application/json")
    rejected_when = _opt(raw, "rejected_when", dict, where, {})
    _unknown_keys(rejected_when, {"path", "values"}, f"{where}.rejected_when")
    rejected_path, rejected_values = rejected_when.get("path", []), rejected_when.get("values", [])
    if rejected_when and not (
        isinstance(rejected_path, list) and rejected_path
        and all(isinstance(s, (str, int)) and not isinstance(s, bool) for s in rejected_path)
        and isinstance(rejected_values, list) and rejected_values and all(isinstance(v, str) for v in rejected_values)
    ):
        raise CatalogError(f"{where}.rejected_when: `path` is a JSON path and `values` a list of strings")
    oauth_scheme = _opt(raw, "oauth_scheme", str, where, "Bearer")
    if not _HEADER.fullmatch(oauth_scheme):
        raise CatalogError(f"{where}: oauth_scheme {oauth_scheme!r} is not one token")
    require_true = _opt(raw, "require_true", list, where, [])
    if not all(isinstance(step, (str, int)) and not isinstance(step, bool) for step in require_true) or (require_true and not accept):
        raise CatalogError(f"{where}: require_true is one JSON path, read from a JSON answer")
    require_equal = _opt(raw, "require_equal", dict, where, {})
    _unknown_keys(require_equal, {"path", "field"}, f"{where}.require_equal")
    equal_path, equal_field = require_equal.get("path", []), require_equal.get("field")
    if require_equal and not (
        isinstance(equal_path, list) and equal_path
        and all(isinstance(s, (str, int)) and not isinstance(s, bool) for s in equal_path)
        and isinstance(equal_field, str) and equal_field and accept
    ):
        raise CatalogError(f"{where}.require_equal: `path` is a JSON path read from a JSON answer and `field` a field key")
    rejected_text = _opt(raw, "rejected_text", str, where)
    if rejected_text is not None:
        try:
            re.compile(rejected_text)
        except re.error as exc:
            raise CatalogError(f"{where}.rejected_text: not a regex ({exc})") from None
        if not rejected_text:
            raise CatalogError(f"{where}.rejected_text: must not be empty")
    host_suffixes = _opt(raw, "host_suffixes", list, where, [])
    if not all(isinstance(item, str) and _DOMAIN.fullmatch(item) for item in host_suffixes):
        raise CatalogError(f"{where}: host_suffixes are lowercase domains such as 'acme.com'")
    token_raw = _opt(raw, "token", dict, where)
    token = _parse_token(token_raw, f"{where}.token") if token_raw is not None else None
    if token is not None and auth_fields["auth"] != "none":
        raise CatalogError(f"{where}: a token Test sends the minted token - declare no `auth`")
    return HttpTestSpec(
        url=url, method=method, body=body,
        content_type=_opt(raw, "content_type", str, where, "application/json" if body is not None else None),
        accept=accept or None, extra_headers=tuple(sorted(headers.items())),
        identity=tuple(tuple(path) for path in identity),
        identity_phrase=_opt(raw, "identity_phrase", str, where, "as"), list_field=list_field, token=token,
        field_headers=tuple(sorted(field_headers.items())), rejected_statuses=tuple(sorted(set(rejected))),
        rejected_path=tuple(rejected_path), rejected_values=tuple(rejected_values), oauth_scheme=oauth_scheme,
        require_true=tuple(require_true), host_suffixes=tuple(host_suffixes),
        require_equal_path=tuple(equal_path), require_equal_field=equal_field, rejected_text=rejected_text,
        **auth_fields,
    ), None, None


def _token_prefix(raw: Mapping[str, Any], where: str) -> str:
    prefix = _opt(raw, "prefix", str, where, "Bearer")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", prefix):
        raise CatalogError(f"{where}: prefix {prefix!r} must be one word, such as Bearer")
    return prefix


def _parse_token(raw: Mapping[str, Any], where: str) -> TokenExchangeSpec:
    _unknown_keys(raw, {"url", "client_id_field", "client_secret_field", "scope", "grant_type", "prefix", "audience",
                        "client_auth"}, where)
    url = _need(raw, "url", str, where)
    if not url.startswith("{"):
        _https_url(url, f"{where}.url", templated=True)
    elif not re.fullmatch(r"\{[a-z][a-z0-9_]*\}(/\S*)?", url):
        raise CatalogError(f"{where}.url: {url!r} must be {{url_field}}/path")
    return TokenExchangeSpec(
        url=url, client_id_field=_need(raw, "client_id_field", str, where),
        client_secret_field=_need(raw, "client_secret_field", str, where), scope=_opt(raw, "scope", str, where),
        grant_type=_opt(raw, "grant_type", str, where, "client_credentials"),
        prefix=_token_prefix(raw, where), audience=_opt(raw, "audience", str, where),
        client_auth=_token_client_auth(raw, where),
    )


def _token_client_auth(raw: Mapping[str, Any], where: str) -> Literal["body", "basic"]:
    value = _opt(raw, "client_auth", str, where, "body")
    if value == "basic":
        return "basic"
    if value != "body":
        raise CatalogError(f"{where}: client_auth must be body or basic")
    return "body"


def _parse_mcp(raw: Mapping[str, Any], where: str) -> McpSpec:
    # `registration` is free-form, read by the docs generator only (a vendor that admits only clients it approved).
    _unknown_keys(raw, {"url", "url_editable", "transport", "auth", "registration"}, where)
    transport = _opt(raw, "transport", str, where, "streamable_http")
    if transport not in ("streamable_http", "sse"):
        raise CatalogError(f"{where}: transport must be streamable_http or sse")
    auth = _opt(raw, "auth", str, where, "none")
    if not _MCP_AUTH.fullmatch(auth):
        raise CatalogError(f"{where}: auth {auth!r} is not oauth_dcr, oauth_registered, bearer_field:<key> or none")
    return McpSpec(
        url=_https_url(_need(raw, "url", str, where), f"{where}.url", templated=True),
        url_editable=bool(_opt(raw, "url_editable", bool, where, False)), transport=transport, auth=auth,
    )


def check_logo(svg: str, where: str) -> str:
    """The logo as served, or the refusal. Refused rather than rewritten: no script, no event handler, no
    `foreignObject` and no reference outside the document (only `#fragment` hrefs), so the immutable-cached
    `image/svg+xml` answer can never run or fetch anything."""
    lowered = svg.lower()
    if "<svg" not in lowered:
        raise CatalogError(f"{where}: not an SVG")
    if re.search(r"<\s*(script|foreignobject|iframe|embed|object)\b", lowered):
        raise CatalogError(f"{where}: an SVG logo may carry no script or embedded document")
    if re.search(r"\son[a-z]+\s*=", lowered):
        raise CatalogError(f"{where}: an SVG logo may carry no event handler")
    for match in re.finditer(r"(?:xlink:)?href\s*=\s*([\"'])(.*?)\1", svg, re.IGNORECASE | re.DOTALL):
        if not match.group(2).strip().startswith("#"):
            raise CatalogError(f"{where}: an SVG logo may reference only its own #fragments")
    if re.search(r"url\(\s*['\"]?(?!#)", lowered) or "@import" in lowered or "javascript:" in lowered:
        raise CatalogError(f"{where}: an SVG logo may load nothing external")
    return svg


def parse_spec(data: Mapping[str, Any], *, source: str = "<spec>", logo_svg: Optional[str] = None) -> IntegrationSpec:
    """One spec from its parsed TOML, validated; `CatalogError` names the first problem."""
    _unknown_keys(data, {
        "slug", "label", "category", "credential_type", "vendor_docs_url", "summary", "default_base_url", "auth",
        "test", "mcp", "docs",
    }, source)
    slug = _need(data, "slug", str, source)
    if not _SLUG.fullmatch(slug):
        raise CatalogError(f"{source}: slug {slug!r} must match {_SLUG.pattern}")
    where = f"{source} ({slug})"
    label = _need(data, "label", str, where)
    category = _need(data, "category", str, where)
    if category not in CATEGORIES:
        raise CatalogError(f"{where}: category {category!r} is not one of {list(CATEGORIES)}")
    vendor_docs_url = _https_url(_need(data, "vendor_docs_url", str, where), f"{where}.vendor_docs_url")
    summary = _need(data, "summary", str, where)
    credential_type = _opt(data, "credential_type", str, where, slug)
    default_base_url = _opt(data, "default_base_url", str, where)
    if default_base_url is not None:
        _https_url(default_base_url, f"{where}.default_base_url")

    auth_raw = data.get("auth")
    if not isinstance(auth_raw, list) or not auth_raw:
        raise CatalogError(f"{where}: at least one [[auth]] method is required")
    methods: list[AuthMethod] = []
    provider: Optional[OAuthProvider] = None
    jaah_app = False
    for index, item in enumerate(auth_raw):
        at = f"{where}.auth[{index}]"
        if not isinstance(item, dict):
            raise CatalogError(f"{at}: must be a table")
        _unknown_keys(item, {"kind", "oauth", "fields"}, at)
        kind = _need(item, "kind", str, at)
        if kind not in _AUTH_KINDS:
            raise CatalogError(f"{at}: kind {kind!r} is not one of {list(_AUTH_KINDS)}")
        if kind == "oauth":
            if provider is not None:
                raise CatalogError(f"{at}: one oauth method per vendor")
            oauth_raw = item.get("oauth")
            if not isinstance(oauth_raw, dict):
                raise CatalogError(f"{at}: an oauth method needs its [auth.oauth] table")
            provider, jaah_app = _parse_provider(slug, label, oauth_raw, f"{at}.oauth")
            if item.get("fields"):
                raise CatalogError(f"{at}: an oauth method's fields go in [auth.oauth] (only Connect fills them)")
            grant_only = _parse_fields(oauth_raw.get("fields"), f"{at}.oauth")
            if any(field.required or field.kind == "secret" for field in grant_only):
                raise CatalogError(f"{at}.oauth: a field only Connect fills is optional and not a secret")
            methods.append(AuthMethod(kind, grant_only))
        else:
            if "oauth" in item:
                raise CatalogError(f"{at}: only an oauth method takes [auth.oauth]")
            methods.append(AuthMethod(kind, _parse_fields(item.get("fields"), at)))

    if provider is not None and provider.origin_field is not None:
        declared = {item.key: item for method in methods for item in method.fields}
        origin = declared.get(provider.origin_field)
        kind = "text" if provider.origin_label else "url"
        if origin is None or origin.kind != kind or not origin.required:
            raise CatalogError(f"{where}: origin_field {provider.origin_field!r} is not a declared required {kind} field")

    http_test = format_test = tester = None
    test_raw = data.get("test")
    if test_raw is not None:
        if not isinstance(test_raw, dict):
            raise CatalogError(f"{where}: [test] must be a table")
        http_test, format_test, tester = _parse_test(test_raw, f"{where}.test")
    mcp_raw = data.get("mcp")
    mcp = _parse_mcp(mcp_raw, f"{where}.mcp") if isinstance(mcp_raw, dict) else None
    if mcp_raw is not None and mcp is None:
        raise CatalogError(f"{where}: [mcp] must be a table")
    docs = data.get("docs")
    if docs is not None and not isinstance(docs, dict):
        raise CatalogError(f"{where}: [docs] must be a table")
    if logo_svg is not None:
        check_logo(logo_svg, f"{where} logo")
    spec = IntegrationSpec(
        slug=slug, label=label, category=category, credential_type=credential_type,
        vendor_docs_url=vendor_docs_url, summary=summary, auth=tuple(methods), http_test=http_test,
        format_test=format_test, tester=tester, provider=provider, jaah_app=jaah_app, mcp=mcp,
        default_base_url=default_base_url, logo_svg=logo_svg, raw=dict(data),
    )
    if provider is not None and http_test is not None and http_test.token is not None:
        # An OAuth row sends its own access token, so a token exchange would silently never run.
        raise CatalogError(f"{where}: an oauth vendor's Test sends the grant's token - declare no [test.token]")
    _check_test_fields(spec, where)
    return spec


def _declared_fields(spec: IntegrationSpec, *, pasted_only: bool = False) -> dict[str, FieldSpec]:
    """The fields the spec's methods declare (`pasted_only`: its non-OAuth ones), deduplicated by key in spec
    order."""
    out: dict[str, FieldSpec] = {}
    for method in spec.auth:
        if pasted_only and method.kind == "oauth":
            continue
        for item in method.fields:
            existing = out.get(item.key)
            if existing is not None and (existing.kind != item.kind):
                raise CatalogError(f"{spec.slug}: field {item.key!r} is declared twice with different kinds")
            out.setdefault(item.key, item)
    return out


def _check_token_fields(test: HttpTestSpec, fields: Mapping[str, FieldSpec], where: str) -> None:
    """A token exchange names a declared client id, a declared secret, and at most one placeholder: the Test
    URL's own url-field origin, or one path segment of a fixed host (never a host label, never a secret)."""
    token = test.token
    assert token is not None  # narrowing: the caller checked
    if token.client_id_field not in fields:
        raise CatalogError(f"{where}.test.token: field {token.client_id_field!r} is not declared")
    secret = fields.get(token.client_secret_field)
    if secret is None or secret.kind != "secret":
        raise CatalogError(f"{where}.test.token: {token.client_secret_field!r} is not a declared secret field")
    names = token.placeholders
    if len(names) > 1 or (not names and "{" in token.url):
        raise CatalogError(f"{where}.test.token: at most one well-formed placeholder")
    for name in names:
        item = fields.get(name)
        if item is None or item.kind == "secret":
            raise CatalogError(f"{where}.test.token: placeholder {{{name}}} is not a declared non-secret field")
        if item.kind == "url":
            if not (token.url.startswith(f"{{{name}}}") and test.url.startswith(f"{{{name}}}")):
                raise CatalogError(f"{where}.test.token: a url placeholder must be the Test URL's own origin")
        elif not re.fullmatch(r"https://[a-z0-9.-]+(?:/[A-Za-z0-9._~-]+)*/\{" + name + r"\}(?:/[A-Za-z0-9._~-]+)*",
                              token.url):
            raise CatalogError(f"{where}.test.token: placeholder {{{name}}} must be one path segment of a fixed host")


def _check_test_fields(spec: IntegrationSpec, where: str) -> None:
    """Every field a Test names exists with the right kind (against the spec's fields, or the adopted type's)."""
    fields = dict(_declared_fields(spec))
    if spec.credential_type in _HAND_WRITTEN_TYPES:
        fields.update({item.key: item for item in _HAND_WRITTEN_FIELDS[spec.credential_type]})
    if spec.provider is not None:
        for grant_field in spec.provider.grant_fields:
            item = fields.get(grant_field)
            if item is None or item.kind == "secret":
                raise CatalogError(f"{where}: grant_fields names {grant_field!r}, not a declared non-secret field")
    test = spec.http_test
    if test is not None and test.oauth_scheme != "Bearer" and spec.provider is None:
        raise CatalogError(f"{where}.test: oauth_scheme applies to an OAuth row, and this vendor has no oauth method")
    if test is not None:
        key = test.key_field
        if key is not None and (key not in fields or fields[key].kind != "secret"):
            raise CatalogError(f"{where}.test: key field {key!r} is not a declared secret field")
        if test.user_field is not None and test.user_field not in fields:
            raise CatalogError(f"{where}.test: field {test.user_field!r} is not declared")
        for header, name in test.field_headers:
            if name not in fields or fields[name].kind == "secret":
                raise CatalogError(f"{where}.test: field_headers {header!r} names {name!r}, not a declared "
                                   "non-secret field")
        if test.auth != "none" and test.key_field is None:
            raise CatalogError(f"{where}.test: auth {test.auth!r} names no key field")
        equal_field = test.require_equal_field
        if equal_field is not None and (equal_field not in fields or fields[equal_field].kind == "secret"):
            # Compared, never echoed: still a non-secret field, so a mismatch sentence may name it.
            raise CatalogError(f"{where}.test: require_equal names {equal_field!r}, not a declared non-secret field")
        for name in test.placeholders:
            if name not in fields or fields[name].kind == "secret":
                raise CatalogError(f"{where}.test: placeholder {{{name}}} is not a declared non-secret field")
            if fields[name].kind == "url" and not test.url.startswith(f"{{{name}}}"):
                raise CatalogError(f"{where}.test: a url field placeholder must be the whole origin")
            if fields[name].kind != "url" and not (
                re.match(r"https://(?:[a-z0-9-]+\.)*\{" + name + r"\}\.", test.url)
                # One path segment of a fixed host (an Atlassian cloud ID): the host stays the vendor's own.
                or re.fullmatch(r"https://[a-z0-9.-]+(?:/[A-Za-z0-9._~@-]+)*/\{" + name + r"\}(?:/[A-Za-z0-9._~@-]*)*",
                                test.url)
            ):
                raise CatalogError(f"{where}.test: placeholder {{{name}}} must be one DNS label of the host or one "
                                   "path segment of a fixed host")
        if len(test.placeholders) > 1:
            raise CatalogError(f"{where}.test: at most one placeholder")
        if not test.placeholders and "{" in test.url:
            raise CatalogError(f"{where}.test: malformed placeholder")
        url_placeholder = any(fields[name].kind == "url" for name in test.placeholders if name in fields)
        if test.host_suffixes and not url_placeholder:
            raise CatalogError(f"{where}.test: host_suffixes pin a url field placeholder - the test has none")
        if test.method == "POST" and url_placeholder and not test.host_suffixes:
            # A POST body is readable by whoever runs the host: a tenant-chosen origin gets GETs only. A host label
            # under the vendor's own fixed domain (`{account}.snowflakecomputing.com`) is the vendor's host.
            # (The `[test.token]` exchange shares this origin and is pinned by name in the testers' I2 sweep.)
            raise CatalogError(f"{where}.test: a POST never goes to a tenant-chosen origin")
        for name in PLACEHOLDER.findall(test.value_template or ""):
            if name not in fields:
                raise CatalogError(f"{where}.test: template placeholder {{{name}}} is not a declared field")
        for header, value in test.extra_headers:
            if PLACEHOLDER.search(value):
                raise CatalogError(f"{where}.test: header {header} is a constant - send a field with field_headers")
        if test.token is not None:
            _check_token_fields(test, fields, where)
        if len(test.body_placeholders) > 1:
            raise CatalogError(f"{where}.test: at most one body placeholder")
        for name in test.body_placeholders:
            if name not in fields or fields[name].kind in ("secret", "url"):
                raise CatalogError(f"{where}.test: body placeholder {{{name}}} is not a declared text field")
    if spec.mcp is not None:
        bearer = spec.mcp.auth.partition(":")[2] if spec.mcp.auth.startswith("bearer_field:") else None
        if bearer is not None and (bearer not in fields or fields[bearer].kind != "secret"):
            raise CatalogError(f"{where}.mcp: bearer_field {bearer!r} is not a declared secret field")
        for name in PLACEHOLDER.findall(spec.mcp.url):
            if name not in fields or fields[name].kind not in ("text",):
                raise CatalogError(f"{where}.mcp: placeholder {{{name}}} is not a declared text field")
        if spec.mcp.url_editable and ("url" not in fields or fields["url"].kind != "url"):
            raise CatalogError(f"{where}.mcp: an editable server URL needs a declared `url` field of kind url")
    if spec.format_test is not None:
        for key, _pattern in spec.format_test.patterns:
            if key not in fields:
                raise CatalogError(f"{where}.test: pattern for undeclared field {key!r}")
        host = spec.format_test.public_host_field
        if host is not None and host not in fields:
            raise CatalogError(f"{where}.test: public_host_field {host!r} is not declared")


# ── Derivation ────────────────────────────────────────────────────────────────────────────────────────────


#: The fields Connect writes onto a catalog OAuth row, beside the operator's own client (BYO). Same shape as
#: `registry.py::_CONNECT_TOKEN_FIELDS`, which the generic Connect types share.
def _oauth_fields(spec: IntegrationSpec) -> tuple[FieldSpec, ...]:
    via = "leave blank to use Jaah's app" if spec.jaah_app else "required to connect"
    # A metadata-document client is Jaah's alone (`OAuthProvider.client_metadata`): no own app to name.
    own_app = () if spec.provider is not None and spec.provider.client_metadata else (
        FieldSpec("client_id", "Client ID", "text", False, help=f"Your own OAuth app's client ID - {via}"),
        FieldSpec("client_secret", "Client secret", "secret", False, help=f"Your own OAuth app's client secret - {via}"),
    )
    return own_app + (
        FieldSpec("refresh_token", "Refresh token", "secret", False, help="Filled by Connect"),
        FieldSpec("access_token", "Access token", "secret", False, help="Cached from the last refresh - no need to fill this"),
        FieldSpec("expiry_date", "Access token expiry", "text", False,
                  help="Milliseconds since the epoch, cached from the last refresh"),
        FieldSpec("scope", "Scopes", "text", False, help="Filled by Connect: the scopes the consent asked for"),
        FieldSpec("account", "Account", "text", False, help="Filled by Connect: the account that consented"),
    )


#: The fields an MCP sign-in (`McpSpec.sign_in`) Connect reads or fills: the custom `mcp_server` type's client and
#: grant fields (`credentials/registry.py`), so both run on the one `mcp` provider (`app/mcp_oauth.py`).
def _mcp_sign_in_fields() -> tuple[FieldSpec, ...]:
    return tuple(item for item in CREDENTIAL_TYPES["mcp_server"].fields
                 if item.key not in ("url", "transport", "bearer_token"))


def mcp_config(type_slug: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """`config` as an MCP Connect reads it (`app/mcp_oauth.py::grant_for`): for a catalog vendor's row, `url` is the
    server its spec resolves (`McpSpec.server_url`), removed when it resolves none; any other row is unchanged."""
    out = dict(config)
    spec = spec_for_type(type_slug)
    if spec is None or spec.mcp is None:
        return out
    url, _problem = spec.mcp.server_url(out)
    out.pop("url", None)
    if url is not None:
        out["url"] = url
    return out


def _derived_default_base_url(spec: IntegrationSpec, fields: Mapping[str, FieldSpec]) -> Optional[str]:
    """The type's fixed host: the spec's own, else the Test URL's origin with a host-label placeholder dropped;
    None when a url field is the host (the registry's rule)."""
    if any(item.kind == "url" for item in fields.values()):
        # An MCP vendor's optional server URL defaults to its spec's host; a required one (a tenant URL) has no default.
        required = any(item.kind == "url" and item.required for item in fields.values())
        return spec.default_base_url or (_fixed_origin(spec.mcp.url) if spec.mcp and not required else None)
    if spec.default_base_url:
        return spec.default_base_url
    for url in (
        spec.http_test.url if spec.http_test else None,
        spec.provider.token_url if spec.provider else None,
        # An MCP vendor's secrets go to its server alone (the sealing destination, `registry.sealing_destination`).
        spec.mcp.url if spec.mcp else None,
        spec.vendor_docs_url,
    ):
        origin = _fixed_origin(url) if url else None
        if origin:
            return origin
    return None


def _fixed_origin(url: str) -> Optional[str]:
    """`url`'s origin with a host-label placeholder dropped (`{account}.example.com` -> `example.com`), or None."""
    host = re.sub(r"^(?:[a-z0-9-]+\.)*\{[a-z0-9_]+\}\.", "", urlsplit(url).netloc)
    return f"https://{host}" if host and "{" not in host else None


def derived_type(spec: IntegrationSpec) -> CredentialTypeSpec:
    """The credential type a spec that does not adopt an existing one defines."""
    fields = _declared_fields(spec)
    oauth = spec.provider is not None
    # The MCP sign-in fills the grant fields; a vendor's own `[auth.oauth]` already does, and is its Connect.
    mcp_sign_in = spec.mcp is not None and spec.mcp.sign_in and not oauth
    require_one_of: tuple[tuple[str, ...], ...] = ()
    key_fields = tuple(fields.values())
    if (oauth or mcp_sign_in) and key_fields:
        # Both methods: a row is either a pasted key or a Connect grant, so the key's required fields become
        # one `require_one_of` group and the grant the other. An MCP sign-in's Connect runs on a saved row (as a
        # custom server's does), so that row saves with neither and its Test says what is missing.
        # An origin-scoped provider's host field is both methods' (every endpoint is on it): it stays required.
        shared = _grant_filled(spec)
        required = tuple(item.key for item in key_fields if item.required and item.key not in shared)
        key_fields = tuple(
            item if item.key in shared else replace(item, required=False)
            for item in key_fields
        )
        if required and oauth:
            require_one_of = (required, ("refresh_token",))
    all_fields = key_fields + (_oauth_fields(spec) if oauth else ()) + (_mcp_sign_in_fields() if mcp_sign_in else ())
    seen: set[str] = set()
    for item in all_fields:
        if item.key in seen:
            raise CatalogError(f"{spec.slug}: field {item.key!r} collides with a field Connect writes")
        seen.add(item.key)
    url_fields = [item.key for item in all_fields if item.kind == "url"]
    return CredentialTypeSpec(
        slug=spec.credential_type,
        label=spec.label,
        fields=all_fields,
        testable=True,
        default_base_url=_derived_default_base_url(spec, {item.key: item for item in all_fields}),
        require_https_base_url=bool(url_fields),
        # An MCP sign-in runs on the custom server's discovery provider (`oauth_providers.PROVIDERS["mcp"]`).
        oauth_connect=spec.slug if oauth else "mcp" if mcp_sign_in else None,
        base_url_field=url_fields[0] if len(url_fields) > 1 else None,
        # D7: the control plane receives and refreshes an OAuth row's tokens, so it can never be NodeVault. A key
        # beside an MCP sign-in keeps the type eligible (its rows predate the sign-in); a row holding a grant is
        # refused NodeVault on its own (`credential_consumers.oauth2_reason`).
        # A key method beside a vendor's own Connect keeps its rows NodeVault-eligible too, as a hand-written type a
        # Connect extends does: a row holding a grant is the one refused (`credential_consumers.oauth2_reason`).
        nodevault_eligible=(not oauth or any(m.fields for m in spec.auth if m.kind not in ("oauth", "mcp")))
        and not (mcp_sign_in and not any(m.fields for m in spec.auth if m.kind != "mcp")),
        require_one_of=require_one_of,
        # Connect caches the access token's expiry here (`_oauth_fields`), which the health facts read.
        expiry_field="expiry_date" if (oauth or mcp_sign_in) else None,
    )


def _grant_filled(spec: IntegrationSpec) -> frozenset[str]:
    """The non-token fields a Connect row has as well as a pasted one: the origin field and the hook's
    `grant_fields`. They stay required when both methods share a type."""
    provider = spec.provider
    if provider is None:
        return frozenset()
    return frozenset(provider.grant_fields) | ({provider.origin_field} if provider.origin_field else set())


def _extends_type(spec: IntegrationSpec) -> bool:
    """Whether an adopting spec adds a Connect its hand-written type lacks (an OAuth method with no registered
    provider of its own), so `extended_type` layers the grant fields over the type."""
    return (spec.credential_type in _HAND_WRITTEN_TYPES and spec.provider is not None
            and spec.slug not in _HAND_WRITTEN_PROVIDERS)


def extended_type(spec: IntegrationSpec) -> CredentialTypeSpec:
    """An adopted hand-written type with the Connect `spec` adds: its own fields, kinds and Test unchanged, plus
    the grant's fields it lacks (`_oauth_fields`, then the spec's Connect-only ones). Its required key fields not
    shared with the grant become optional and one `require_one_of` group beside `refresh_token`, so a stored row
    reads exactly as before and a Connect row saves without a pasted key."""
    base = _HAND_WRITTEN_TYPES_SPECS[spec.credential_type]
    shared = _grant_filled(spec) | {"access_token"}
    own = {item.key for item in base.fields}
    connect_only = tuple(item for method in spec.auth if method.kind == "oauth" for item in method.fields)
    added = tuple(item for item in (*_oauth_fields(spec), *connect_only) if item.key not in own)
    required = tuple(item.key for item in base.fields if item.required and item.key not in shared)
    fields = tuple(item if item.key in shared or not item.required else replace(item, required=False)
                   for item in base.fields) + added
    keys = [item.key for item in fields]
    if len(set(keys)) != len(keys):
        raise CatalogError(f"{spec.slug}: a Connect-only field repeats a field of {spec.credential_type!r}")
    require_one_of = base.require_one_of + ((required, ("refresh_token",)) if required else ())
    return replace(
        base, fields=fields, require_one_of=require_one_of, oauth_connect=spec.slug,
        expiry_field=base.expiry_field or "expiry_date",
    )


def _check_layering(spec: IntegrationSpec) -> None:
    """The conflict guard. A spec may layer over a hand-written type or provider only when what it declares
    agrees with it: an adopting spec's fields must exist on the type with the same kind, and an OAuth block over
    an existing provider must name the same endpoints. Anything else is two homes for one fact."""
    if spec.credential_type in _HAND_WRITTEN_TYPES:
        existing = {item.key: item for item in _HAND_WRITTEN_FIELDS[spec.credential_type]}
        for key, item in _declared_fields(spec, pasted_only=True).items():
            if key not in existing or existing[key].kind != item.kind:
                raise CatalogError(
                    f"{spec.slug}: field {key!r} disagrees with the hand-written type {spec.credential_type!r}"
                )
        if spec.format_test or spec.tester or (spec.http_test and not _extends_type(spec)):
            raise CatalogError(
                f"{spec.slug}: adopts {spec.credential_type!r}, whose Test is its own - declare no [test] (only a "
                "Connect it adds may declare one, for its own rows)"
            )
    elif spec.http_test is None and spec.format_test is None and spec.tester is None and spec.mcp is None:
        # An `[mcp]` block is a Test of its own: the MCP handshake (`credentials/testers.py::_catalog_mcp`).
        raise CatalogError(f"{spec.slug}: a new credential type needs a [test] (http, format, a tester hook or [mcp])")
    if spec.provider is not None and spec.slug in _HAND_WRITTEN_PROVIDERS:
        existing_provider = PROVIDERS[spec.slug]
        for name in ("authorize_url", "token_url", "refresh_url", "pkce", "basic_auth"):
            if getattr(existing_provider, name) != getattr(spec.provider, name):
                raise CatalogError(f"{spec.slug}: [auth.oauth] {name} disagrees with the registered provider")
        if existing_provider.scope and not existing_provider.scopes and spec.provider.scopes:
            # The gallery's Connect swaps this catalogue in (`routes/integrations.py`): it must ask exactly what the
            # registered provider's fixed scope does, or the gallery and the provider's own Connect would differ.
            asked = spec.provider.scope_separator.join(scope.name for scope in spec.provider.scopes)
            if asked != existing_provider.scope:
                raise CatalogError(f"{spec.slug}: [auth.oauth] scopes {asked!r} disagree with the registered "
                                   f"provider's {existing_provider.scope!r}")


def register(spec: IntegrationSpec, *, types: dict[str, CredentialTypeSpec] = CREDENTIAL_TYPES,
             providers: dict[str, OAuthProvider] = PROVIDERS) -> IntegrationSpec:
    """Merge one spec's derived type and provider into the engine's registries (conflict guard first); the spec as
    registered (its provider's client source settled against an adopted type, so both homes read one fact)."""
    _check_layering(spec)
    if _extends_type(spec):
        types[spec.credential_type] = extended_type(spec)
        own_client = next((item for item in _HAND_WRITTEN_FIELDS[spec.credential_type] if item.key == "client_id"), None)
        if spec.provider is not None and own_client is not None and own_client.kind != "text":
            # The type's own `client_id` is another client's sealed secret (Xero's Custom Connection), never a
            # person's own OAuth app: Connect runs on Jaah's app alone, so no row's pair is ever read as one.
            spec = replace(spec, provider=replace(spec.provider, client_source="platform"))
    if spec.credential_type not in _HAND_WRITTEN_TYPES:
        if spec.credential_type in types and types[spec.credential_type] != derived_type(spec):
            raise CatalogError(f"{spec.slug}: credential type {spec.credential_type!r} is defined twice")
        types[spec.credential_type] = derived_type(spec)
    if spec.provider is not None and spec.slug not in _HAND_WRITTEN_PROVIDERS:
        providers[spec.slug] = spec.provider
    return spec


def load_dir(directory: Path) -> dict[str, IntegrationSpec]:
    """Every `*.toml` spec in `directory` with its `logos/<slug>.svg`, keyed by slug; refuses a duplicate slug,
    a file named for another slug and a missing logo."""
    specs: dict[str, IntegrationSpec] = {}
    for path in sorted(directory.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise CatalogError(f"{path.name}: not valid TOML ({exc})") from None
        logo = directory / "logos" / f"{path.stem}.svg"
        if not logo.is_file():
            raise CatalogError(f"{path.name}: missing its logo logos/{path.stem}.svg")
        spec = parse_spec(data, source=path.name, logo_svg=logo.read_text(encoding="utf-8"))
        if spec.slug != path.stem:
            raise CatalogError(f"{path.name}: declares slug {spec.slug!r}; the file must be named for it")
        if spec.slug in specs:
            raise CatalogError(f"{path.name}: duplicate slug {spec.slug!r}")
        specs[spec.slug] = spec
    return specs


#: Every loaded vendor, by slug: the real specs, registered at import. Read through `spec_for`/`listed`.
INTEGRATIONS: dict[str, IntegrationSpec] = load_dir(VENDORS_DIR)
for _slug, _spec in list(INTEGRATIONS.items()):
    INTEGRATIONS[_slug] = register(_spec)
# Insertion order is dropdown order (`registry.py`), and the generic types are the fallback a member meets
# last: a derived type joins before them, never after.
for _fallback in GENERIC_FALLBACK_TYPES:
    if _fallback in CREDENTIAL_TYPES:
        CREDENTIAL_TYPES[_fallback] = CREDENTIAL_TYPES.pop(_fallback)


def listed() -> list[IntegrationSpec]:
    """The gallery's vendors, by label."""
    return sorted(INTEGRATIONS.values(), key=lambda spec: spec.label.lower())


def spec_for(slug: str) -> Optional[IntegrationSpec]:
    return INTEGRATIONS.get(slug)


def spec_for_type(type_slug: str) -> Optional[IntegrationSpec]:
    """The catalog spec whose credential type is `type_slug` and whose Test the catalog owns, or None."""
    for spec in INTEGRATIONS.values():
        if spec.credential_type == type_slug and not spec.adopts_existing_type:
            return spec
    return None


def catalog_provider_keys() -> frozenset[str]:
    """The OAuth providers the catalog added (those `credentials/oauth_wizards.py` serves for credential Connect):
    every catalog type's, and each Connect an adopting spec layers over its hand-written type (`extended_type`)."""
    return frozenset(
        spec.slug for spec in INTEGRATIONS.values()
        if spec.provider is not None and (not spec.adopts_existing_type or _extends_type(spec))
    )


def extended_types() -> frozenset[str]:
    """The hand-written types a catalog Connect extends (`extended_type`): their key rows are as before (NodeVault
    included); a row holding a grant is refused NodeVault on its own (`credential_consumers.oauth2_reason`)."""
    return frozenset(spec.credential_type for spec in INTEGRATIONS.values() if _extends_type(spec))


def hand_written_type(type_slug: str) -> Optional[CredentialTypeSpec]:
    """A hand-written type as written, before any Connect layered over it (`extended_type`); None for another."""
    return _HAND_WRITTEN_TYPES_SPECS.get(type_slug)


def grant_test(type_slug: str) -> Optional[IntegrationSpec]:
    """The adopting spec whose `[test]` is the Test of `type_slug`'s Connect rows (`extended_type`), or None."""
    for spec in INTEGRATIONS.values():
        if spec.credential_type == type_slug and _extends_type(spec) and spec.http_test is not None:
            return spec
    return None


def pasted_fields(type_slug: str) -> frozenset[str]:
    """The fields a person pastes into a `type_slug` row (its catalog spec's non-OAuth methods); empty for a type
    no spec describes. A grant field of the same name (HubSpot's `access_token`) is then theirs to paste."""
    for spec in INTEGRATIONS.values():
        if spec.credential_type == type_slug:
            return frozenset(_declared_fields(spec, pasted_only=True))
    return frozenset()
