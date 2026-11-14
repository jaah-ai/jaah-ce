"""Custom connectors: the REST APIs and MCP servers an org defines for itself (`models/integrations.py::CustomConnector`).

A connector is a definition, never a secret. A CONNECTION to it is an ordinary `credentials` row of one of the
existing generic types, whose config carries `custom_connector_id`, so no new credential type, tester or runner
contract exists for it (architecture §4):

=================  ==================  =====================================================================
kind / scheme      credential type     config the connection is born with
=================  ==================  =====================================================================
rest / bearer      `generic_api_key`   base_url, api_key, header_name `Authorization`, value_prefix (`Bearer`)
rest / header_key  `generic_api_key`   base_url, api_key, header_name, value_prefix (blank sends the key alone)
rest / basic       `generic_login`     base_url, login, password
rest / oauth2      `oauth2_client`     authorize_url, token_url, client_id, client_secret, scope (then Connect)
mcp / any          `mcp_server`        url, transport, bearer_token or client_id/client_secret/scope
=================  ==================  =====================================================================

A REST connector whose connections are generic rows (`FREE_FORM_SCHEMES`) takes any Base URL, as those types do
(`registry.CredentialTypeSpec.free_form_url`): the console warns on an odd shape, and only an http(s) one is tested.

The Test (`connection_result`) dials `base_url + test_path` at addresses `http_bounded.vetted_addresses` passed
(the SSRF rule: the metadata service and RFC 1918 hosts are never dialled), so it is safe for a tenant to run; an
MCP connector's Test is the MCP handshake (`credentials/testers.py::test_mcp_server`, vetted by `mcp_client`).

The routes are `routes/integrations_custom.py`; the gallery merge is `routes/integrations.py::list_integrations`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit
from uuid import UUID

from app import http_bounded
from app.credentials import testers
from app.credentials.registry import CUSTOM_CONNECTOR_ID_KEY
from app.models.integrations import (
    CUSTOM_CONNECTOR_AUTH_SCHEMES,
    CUSTOM_CONNECTOR_KINDS,
    CUSTOM_CONNECTOR_MCP_TRANSPORTS,
    CustomConnector,
)

#: The config key a connection names its connector by: undeclared on the generic types, classified non-secret.
CONNECTOR_ID_KEY = CUSTOM_CONNECTOR_ID_KEY

#: The schemes each kind takes. A REST connector always carries a secret (a row with nothing to hand a runner is
#: not a connection); an MCP server may be open.
SCHEMES_BY_KIND: Mapping[str, tuple[str, ...]] = {
    "rest": ("bearer", "header_key", "basic", "oauth2_client"),
    "mcp": ("none", "bearer", "oauth2_client"),
}

#: The REST schemes whose connections are `free_form_url` generic rows, so their Base URL is never refused.
FREE_FORM_SCHEMES: frozenset[str] = frozenset({"bearer", "header_key", "basic"})

#: The gallery's category for every custom connector (`categories.toml`).
CATEGORY = "custom"

_SLUG = re.compile(r"[a-z][a-z0-9_]{1,39}")
_HEADER = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
_PREFIX = re.compile(r"[A-Za-z0-9._~+/-]{0,32}")
_SCOPE = re.compile(r"[\x21-\x7e]+(?: [\x21-\x7e]+)*")
_PATH = re.compile(r"/[A-Za-z0-9\-._~!$&'()*+,;=:@%/?]*")
_URL_MAX = 2048


class ConnectorError(ValueError):
    """A definition (or a connection's choices) the API refuses: its text is the 400's detail."""


@dataclass(frozen=True)
class ConnectorFields:
    """A validated definition, ready to write onto a `CustomConnector` row."""

    kind: str
    base_url: str
    auth_scheme: str
    header_name: Optional[str]
    value_prefix: Optional[str]
    oauth_authorize_url: Optional[str]
    oauth_token_url: Optional[str]
    oauth_scope: Optional[str]
    test_path: Optional[str]
    mcp_transport: Optional[str]


def slug_from_name(name: str) -> str:
    """A slug for a name (`Acme Billing API` -> `acme_billing_api`), padded and cut to the slug rule."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"c_{slug}".rstrip("_")
    if len(slug) < 2:
        slug = f"{slug}_api"
    return slug[:40].rstrip("_")


def checked_slug(slug: str, taken: frozenset[str]) -> str:
    """`slug` if it fits the catalog's slug rule and names no built-in vendor; `ConnectorError` otherwise."""
    if not _SLUG.fullmatch(slug):
        raise ConnectorError("slug must be 2-40 lowercase letters, digits or underscores, starting with a letter")
    if slug in taken:
        raise ConnectorError(f"slug {slug!r} is a built-in integration's - choose another")
    return slug


def public_https_url(value: str, label: str) -> str:
    """`value` stripped, if it is an https URL whose host resolves only to public addresses; else the refusal.

    THE SSRF RULE at define time (the Test re-vets and pins at dial time): a connector whose host is the metadata
    service, loopback or an RFC 1918 address is refused before anyone can connect to it."""
    url = value.strip()
    if not url or len(url) > _URL_MAX:
        raise ConnectorError(f"{label} is required (at most {_URL_MAX} characters)")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ConnectorError(f"{label} must be an https:// URL")
    if parts.username or parts.password:
        raise ConnectorError(f"{label} must not carry a user name or password - the connection holds those")
    if parts.fragment:
        raise ConnectorError(f"{label} must not carry a #fragment")
    refusal = http_bounded.public_destination_refusal(url)
    if refusal is not None:
        raise ConnectorError(f"{label} is refused: {refusal}")
    return url


def free_form_url(value: str, label: str) -> str:
    """`value` stripped, refused only when blank or too long: no shape rule (`FREE_FORM_SCHEMES`). Never vetted
    here; the Test vets and pins at dial time, so a private host saves but is never dialled."""
    url = value.strip()
    if not url or len(url) > _URL_MAX:
        raise ConnectorError(f"{label} is required (at most {_URL_MAX} characters)")
    return url


def is_http_url(value: str) -> bool:
    """Whether a Test can dial `value`: an http(s) URL with a host."""
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def _optional(value: Optional[str]) -> Optional[str]:
    stripped = (value or "").strip()
    return stripped or None


def checked_fields(
    *, kind: str, base_url: str, auth_scheme: str, header_name: Optional[str] = None,
    value_prefix: Optional[str] = None, oauth_authorize_url: Optional[str] = None,
    oauth_token_url: Optional[str] = None, oauth_scope: Optional[str] = None, test_path: Optional[str] = None,
    mcp_transport: Optional[str] = None,
) -> ConnectorFields:
    """A whole definition validated together (a scheme decides which other columns apply, and a column that does
    not apply is refused rather than silently stored)."""
    if kind not in CUSTOM_CONNECTOR_KINDS:
        raise ConnectorError(f"kind must be one of {', '.join(CUSTOM_CONNECTOR_KINDS)}")
    if auth_scheme not in CUSTOM_CONNECTOR_AUTH_SCHEMES or auth_scheme not in SCHEMES_BY_KIND[kind]:
        raise ConnectorError(f"auth_scheme for a {kind} connector must be one of {', '.join(SCHEMES_BY_KIND[kind])}")
    header_name, value_prefix = _optional(header_name), _optional(value_prefix)
    authorize, token, scope = _optional(oauth_authorize_url), _optional(oauth_token_url), _optional(oauth_scope)
    test_path, transport = _optional(test_path), _optional(mcp_transport)

    if kind == "rest" and auth_scheme in FREE_FORM_SCHEMES:
        url = free_form_url(base_url, "Base URL")
    else:
        url = public_https_url(base_url, "Base URL" if kind == "rest" else "Server URL")
    if kind == "rest":
        url = url.rstrip("/")
        if transport is not None:
            raise ConnectorError("mcp_transport applies to an MCP server only")
    else:
        transport = transport or "streamable_http"
        if transport not in CUSTOM_CONNECTOR_MCP_TRANSPORTS:
            raise ConnectorError(f"mcp_transport must be one of {', '.join(CUSTOM_CONNECTOR_MCP_TRANSPORTS)}")
        if test_path is not None:
            raise ConnectorError("test_path applies to a REST connector only - an MCP server is tested by its handshake")

    if auth_scheme == "header_key":
        if header_name is None:
            raise ConnectorError("header_name is required for a header key")
    elif header_name is not None:
        raise ConnectorError("header_name applies to a header key only")
    if header_name is not None:
        if not _HEADER.fullmatch(header_name):
            raise ConnectorError("header_name must be one HTTP header name (no spaces or separators)")
        clause = testers._header_name_clause(header_name)
        if clause is not None:
            raise ConnectorError(f"header_name {clause}")
    if value_prefix is not None:
        if auth_scheme not in ("bearer", "header_key") or kind != "rest":
            raise ConnectorError("value_prefix applies to a REST bearer or header key only")
        if not _PREFIX.fullmatch(value_prefix):
            raise ConnectorError("value_prefix must be one word (letters, digits, . _ ~ + / -)")

    if auth_scheme == "oauth2_client":
        if kind == "rest":
            if authorize is None or token is None:
                raise ConnectorError("oauth_authorize_url and oauth_token_url are required for OAuth2")
            authorize = public_https_url(authorize, "Authorize URL")
            token = public_https_url(token, "Token URL")
        elif authorize is not None or token is not None:
            raise ConnectorError("an MCP server's OAuth endpoints are discovered from its URL - leave them blank")
        if scope is not None and (len(scope) > 1024 or not _SCOPE.fullmatch(scope)):
            raise ConnectorError("oauth_scope must be space-separated scope names")
    elif authorize is not None or token is not None or scope is not None:
        raise ConnectorError("the oauth_* fields apply to OAuth2 only")

    if test_path is not None and (len(test_path) > 512 or not _PATH.fullmatch(test_path) or "//" in test_path):
        raise ConnectorError("test_path must be a path starting with / (for example /v1/me)")

    return ConnectorFields(
        kind=kind, base_url=url, auth_scheme=auth_scheme, header_name=header_name, value_prefix=value_prefix,
        oauth_authorize_url=authorize, oauth_token_url=token, oauth_scope=scope, test_path=test_path,
        mcp_transport=transport,
    )


# ── Connections ───────────────────────────────────────────────────────────────────────────────────────


def credential_type_for(connector: CustomConnector) -> str:
    """The existing generic type a connection to `connector` is saved as (the module's table)."""
    if connector.kind == "mcp":
        return "mcp_server"
    return {"basic": "generic_login", "oauth2_client": "oauth2_client"}.get(connector.auth_scheme, "generic_api_key")


#: The secret inputs each scheme takes from the person connecting, `{input: required}`.
SECRET_INPUTS: Mapping[tuple[str, str], Mapping[str, bool]] = {
    ("rest", "bearer"): {"api_key": True},
    ("rest", "header_key"): {"api_key": True},
    ("rest", "basic"): {"login": True, "password": True},
    ("rest", "oauth2_client"): {"client_id": True, "client_secret": True},
    ("mcp", "none"): {},
    ("mcp", "bearer"): {"bearer_token": True},
    ("mcp", "oauth2_client"): {"client_id": False, "client_secret": False},
}


#: Every input any scheme takes.
ALL_INPUTS: frozenset[str] = frozenset(key for inputs in SECRET_INPUTS.values() for key in inputs)


def _given(value: Any) -> bool:
    """A supplied input: a non-blank string, or a browser-sealed envelope (a dict, checked by the write path)."""
    return isinstance(value, dict) or (isinstance(value, str) and bool(value.strip()))


def skeleton_config(connector: CustomConnector) -> dict[str, str]:
    """A new connection's config before the person's inputs: the connector's own fields, as the generic type
    spells them. Its host field is what the secrets are sealed to (the registry's `sealing_destination`)."""
    if connector.kind == "mcp":
        config = {"url": connector.base_url, "transport": connector.mcp_transport or "streamable_http"}
        if connector.oauth_scope:
            config["scope"] = connector.oauth_scope
        return config
    if connector.auth_scheme == "basic":
        return {"base_url": connector.base_url}
    if connector.auth_scheme == "oauth2_client":
        config = {"authorize_url": connector.oauth_authorize_url or "", "token_url": connector.oauth_token_url or ""}
        if connector.oauth_scope:
            config["scope"] = connector.oauth_scope
        return config
    header = connector.header_name if connector.auth_scheme == "header_key" else "Authorization"
    prefix = connector.value_prefix if connector.value_prefix is not None else (
        "Bearer" if connector.auth_scheme == "bearer" else ""
    )
    config = {"base_url": connector.base_url, "header_name": header or "Authorization"}
    if prefix:
        config["value_prefix"] = prefix
    return config


def connection_config(connector: CustomConnector, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The posted config of a new connection: `skeleton_config` plus the person's inputs (strings, or sealed
    envelopes). Validated by the credential write path afterwards (`_merged_config`); this refuses an input the
    scheme does not take, or a required one left blank. Never names a value."""
    wanted = SECRET_INPUTS[(connector.kind, connector.auth_scheme)]
    given = {key: value for key, value in inputs.items() if _given(value)}
    for key in given:
        if key not in wanted:
            raise ConnectorError(f"{key} does not apply to this connector")
    for key, required in wanted.items():
        if required and key not in given:
            raise ConnectorError(f"{key} is required")
    return {**skeleton_config(connector), **given}


def test_url(connector: CustomConnector) -> str:
    """What a REST connection's Test dials: the base URL plus the test path (none: the base URL itself)."""
    return f"{connector.base_url}{connector.test_path or ''}"


def connection_result(connector: CustomConnector, config: Mapping[str, Any], deadline: float) -> testers.TestResult:
    """The Test of one connection (its opened config) against its connector. Blocking: it dials out."""
    # A secret goes only to the address it was given (and sealed) for: a row whose own address differs from the
    # connector's is refused rather than sent to the connector's.
    own = str(config.get("url" if connector.kind == "mcp" else "base_url") or "").rstrip("/")
    if connector.auth_scheme != "oauth2_client" or connector.kind == "mcp":
        if own != connector.base_url.rstrip("/"):
            return testers._failure(
                "This connection was made for another address than the connector's - connect again."
            )
    if connector.kind == "mcp":
        return testers.test_mcp_server({**config, "url": connector.base_url,
                                        "transport": connector.mcp_transport or "streamable_http"}, deadline)
    if not is_http_url(connector.base_url):
        # The console withholds Test here (`FREE_FORM_SCHEMES`); this answers a direct call.
        return testers._failure("Only an http(s) Base URL can be tested.")
    url = test_url(connector)
    scheme = connector.auth_scheme
    if scheme == "basic":
        login, password = str(config.get("login") or ""), str(config.get("password") or "")
        if not login or not password:
            return testers._failure("The login and password are required before testing.")
        return testers.custom_rest_result(connector.name, url, deadline, basic=(login, password))
    if scheme == "oauth2_client":
        token, verdict = testers.grant_token(dict(config))
        if verdict is not None:
            return verdict
        if token is None:
            return testers._failure("Press Connect first: this connection holds no access token yet.")
        return testers.custom_rest_result(connector.name, url, deadline, header=("Authorization", f"Bearer {token}"))
    key = str(config.get("api_key") or "")
    if not key:
        return testers._failure("The API key is required before testing.")
    # The connector's own header and prefix (what `connection_config` wrote), never the row's edited copy.
    shaped = skeleton_config(connector)
    header_name = shaped.get("header_name") or "Authorization"
    prefix = shaped.get("value_prefix", "")
    return testers.custom_rest_result(
        connector.name, url, deadline, header=(header_name, f"{prefix} {key}" if prefix else key),
    )


def connector_id_of(row: Any) -> Optional[UUID]:
    """The connector a credentials row is a connection to, or None. Reads tier 1 only (no secret is opened)."""
    value = (row.config or {}).get(CONNECTOR_ID_KEY)
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def connector_vouches_for(config: Mapping[str, Any]) -> bool:
    """Whether a connection's OAuth endpoints are its connector's own, which an Org Owner/Admin defined and the
    define step vetted: then a member's Connect dials a host the org chose, not one the member typed, and needs no
    `credentials:edit` (`routes/credentials/guards.py::_refuse_a_caller_supplied_endpoint_without_the_edit_key`).

    A REST row's authorize and token URLs must equal the connector's; an MCP row's server URL must (its endpoints are
    discovered from that URL). A member cannot set the link (`_merged_config` refuses the undeclared key), and editing
    an endpoint away from the connector's ends the vouching. Read through the tenancy seam, so only the caller's
    org's connector answers. Blocking (one DB read)."""
    from sqlalchemy import select

    from app.db import BridgedSession

    link = config.get(CONNECTOR_ID_KEY)
    if not isinstance(link, str):
        return False
    try:
        connector_id = UUID(link)
    except ValueError:
        return False
    with BridgedSession() as session:
        connector = session.scalar(select(CustomConnector).where(CustomConnector.id == connector_id))
        if connector is None or connector.auth_scheme != "oauth2_client":
            return False
        same = lambda a, b: bool(a) and str(a).strip().rstrip("/") == str(b or "").strip().rstrip("/")  # noqa: E731
        if connector.kind == "mcp":
            return same(config.get("url"), connector.base_url)
        return same(config.get("token_url"), connector.oauth_token_url) and same(
            config.get("authorize_url"), connector.oauth_authorize_url
        )
