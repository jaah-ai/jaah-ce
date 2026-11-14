"""OAuth for remote MCP servers: from an `mcp_server` row's server URL, the authorization server, the client and
the `resource` one authorize, exchange or refresh runs on (`grant_for`).

The MCP authorization spec chains three RFCs, all run here against the row's server URL:

1. RFC 9728 - the protected-resource metadata (the `resource_metadata` a 401's `WWW-Authenticate` names, else
   `/.well-known/oauth-protected-resource[/<path>]` on the server's origin) gives the `resource` identifier and
   the authorization server;
2. RFC 8414 - that server's metadata (`/.well-known/oauth-authorization-server[/<path>]`, then OpenID
   discovery), whose `issuer` must be the one asked about, gives the authorize, token and registration endpoints;
3. RFC 7591 - Dynamic Client Registration mints a client for Jaah's one Connect callback. The client is stored
   as an operator-plane `oauth_app` row per (authorization server, environment), so every later Connect to the
   same server reuses it; each environment is its own database, so the environment is implied.

WHY EVERY CALL DISCOVERS AGAIN: the row's secrets (refresh token, a pasted client secret) are sealed to its one
host field, `url` (`credentials/registry.py::sealing_destination`). An endpoint stored on the row would be a second,
editable destination the seal does not bind, and a refresh would POST the grant wherever it pointed. So nothing
on the row names an endpoint, and the registered client never leaves the operator plane: a runner fetching the
row cannot read it.

A server that publishes no protected-resource metadata (the 2025-03-26 revision) is its own authorization server;
one with no authorization-server metadata at all is refused (use a bearer token) rather than guessed at. The
Connect itself is the ordinary credential Connect (`routes/credentials/connect.py`) on the `mcp` provider
(`oauth_providers.PROVIDERS["mcp"]`): PKCE always, and RFC 8707's `resource` on every authorize, exchange and
refresh. Every URL here is a host a tenant chose (or one such a host named), so each is dialled only at the
addresses `http_bounded.vetted_addresses` passed. Nothing here logs a token, a client secret or a body.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from time import monotonic
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from app import http_bounded

log = logging.getLogger("mcp-oauth")

_TIMEOUT_SECONDS = 10.0
_MAX_BYTES = 64 * 1024
#: The name Jaah registers as (RFC 7591 `client_name`), shown on the vendor's consent screen.
CLIENT_NAME = "Jaah"
#: The operator-plane `oauth_app` row a registration is stored in is named for its authorization server.
_ROW_NAME_PREFIX = "MCP client · "
_ROW_NAME_MAX = 200
#: The token endpoint auth methods a registration may come back with (RFC 7591 §2) that Jaah can speak.
_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")


class McpOAuthError(Exception):
    """Discovery or registration failed; the message is a sentence safe to show (no token, no body)."""


@dataclass(frozen=True)
class AuthorizationServer:
    """What discovery found: where consent and tokens happen, and what the grant is for."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str]
    resource: str


@dataclass(frozen=True)
class RegisteredClient:
    client_id: str
    client_secret: str = ""
    # How the client authenticates at the token endpoint (`_AUTH_METHODS`).
    auth_method: str = "none"


@dataclass(frozen=True)
class Grant:
    """What one authorize, exchange or refresh on an `mcp_server` row runs on. `registered`: Jaah's own client
    (its secret, if any, is `client_secret`); False is the row's own client, whose secret the caller opens off
    the row. Never stored on the row."""

    authorization_endpoint: str
    token_endpoint: str
    resource: str
    client_id: str
    client_secret: str
    basic_auth: bool
    registered: bool

    @property
    def extra(self) -> dict[str, str]:
        """The parameters every leg adds: RFC 8707's `resource`."""
        return {"resource": self.resource}


# ── The pinned wire ───────────────────────────────────────────────────────────────────────────────────────────────

def _client(host: str, addresses: tuple[str, ...]) -> httpx.Client:
    """The seam tests replace: a redirect-off client dialling only the vetted addresses."""
    return http_bounded.pinned_client(host, addresses, _TIMEOUT_SECONDS)


def _dial(method: str, url: str, *, body: Optional[dict] = None,
          accept: str = "application/json") -> tuple[int, Any, Mapping[str, str]]:
    """`(status, parsed JSON or None, headers)` for one request to `url`, refused when its host is not public. The
    read is bounded in bytes and in wall-clock time (a server may answer with an event stream that never ends)."""
    refusal, addresses = http_bounded.vetted_addresses(url)
    if refusal is not None or not addresses:
        raise McpOAuthError(f"The MCP server's authorization was refused - {refusal or 'it did not resolve'}.")
    headers = {"Accept": accept}
    content = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        content = json.dumps(body).encode("utf-8")
    deadline = monotonic() + _TIMEOUT_SECONDS
    try:
        with _client(httpx.URL(url).host, addresses) as client:
            with client.stream(method, url, headers=headers, content=content) as response:
                raw, truncated = http_bounded.read_capped(response, _MAX_BYTES, lambda: monotonic() >= deadline)
                return response.status_code, (None if truncated else http_bounded.json_or_none(raw)), response.headers
    except httpx.HTTPError as exc:
        log.info("mcp oauth: %s failed (%s)", method, type(exc).__name__)
        raise McpOAuthError("The MCP server's authorization server could not be reached.") from None


def _https(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip().startswith("https://") or http_bounded.origin(value.strip()) is None:
        raise McpOAuthError(f"The authorization server's {what} is not an https:// URL.")
    return value.strip()


def _well_known(url: str, suffix: str, *, with_path: bool) -> str:
    """RFC 8414 §3 / RFC 9728 §3.1: the well-known segment goes between the origin and the URL's path."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") if with_path else ""
    return urlunsplit((parts.scheme, parts.netloc, f"/.well-known/{suffix}{path}", "", ""))


def canonical_resource(url: str) -> str:
    """The MCP server's canonical URI (RFC 8707 §2, the MCP spec's form): lowercase scheme and host, no fragment,
    no query, no trailing slash."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _covers(resource: str, server_url: str) -> bool:
    """Whether a declared `resource` is this server's: the same origin, and the server's path at or under its path
    (RFC 9728 §3.3 as MCP clients apply it - a resource may name the server's root)."""
    if http_bounded.origin(resource) != http_bounded.origin(server_url):
        return False
    declared = urlsplit(canonical_resource(resource)).path
    path = urlsplit(canonical_resource(server_url)).path
    return path == declared or path.startswith(declared.rstrip("/") + "/")


_RESOURCE_METADATA = re.compile(r'resource_metadata="([^"]+)"')


# ── Discovery ────────────────────────────────────────────────────────────────────────────────────────────────────

def _protected_resource(server_url: str) -> Optional[dict[str, Any]]:
    """RFC 9728 metadata for the server, or None when it publishes none."""
    candidates = [_well_known(server_url, "oauth-protected-resource", with_path=True),
                  _well_known(server_url, "oauth-protected-resource", with_path=False)]
    # A 401 to an unauthenticated MCP request may name the document itself (RFC 9728 §5.1); tried first.
    status, _body, headers = _dial("POST", server_url, accept="application/json, text/event-stream", body={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": CLIENT_NAME, "version": "1"}},
    })
    if status == 401:
        named = _RESOURCE_METADATA.search(headers.get("www-authenticate", ""))
        # Only an https document is read: a plain-http one (Confidence names one) is skipped for the well-known
        # candidates on the server's own https origin, which serve the same document.
        if named and named.group(1).strip().startswith("https://") and http_bounded.origin(named.group(1).strip()):
            candidates.insert(0, named.group(1).strip())
    for candidate in dict.fromkeys(candidates):
        status, body, _ = _dial("GET", candidate)
        if status == 200 and isinstance(body, dict):
            return body
    return None


def _server_metadata(issuer: str) -> dict[str, Any]:
    """RFC 8414 metadata for `issuer` (then OpenID discovery), refused when none answers or it is not `issuer`'s."""
    candidates = [_well_known(issuer, "oauth-authorization-server", with_path=True),
                  _well_known(issuer, "openid-configuration", with_path=True),
                  f"{issuer.rstrip('/')}/.well-known/openid-configuration"]
    for candidate in dict.fromkeys(candidates):
        status, body, _ = _dial("GET", candidate)
        if status == 200 and isinstance(body, dict):
            named = body.get("issuer")
            # RFC 8414 §3.3: `issuer` is required and must be the one asked about (a mix-up defence).
            if not isinstance(named, str) or named.rstrip("/") != issuer.rstrip("/"):
                raise McpOAuthError("The authorization server's metadata does not name the issuer it was fetched for.")
            return body
    raise McpOAuthError("The MCP server publishes no OAuth authorization server metadata - use a bearer token.")


def discover(server_url: str) -> AuthorizationServer:
    """The authorization server for the MCP server at `server_url`, or `McpOAuthError`."""
    server_url = server_url.strip()
    resource = canonical_resource(server_url)
    document = _protected_resource(server_url)
    if document is not None:
        named = document.get("resource")
        if isinstance(named, str) and named.strip():
            # RFC 9728 §3.3: the metadata must be for this server, or it is someone else's.
            if not _covers(named.strip(), server_url):
                raise McpOAuthError("The MCP server's resource metadata names another server.")
            resource = named.strip()  # verbatim: an audience check may compare the exact string
        servers = document.get("authorization_servers")
        if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
            raise McpOAuthError("The MCP server's resource metadata names no authorization server.")
        issuer = _https(servers[0], "issuer")
    else:
        parts = urlsplit(server_url)
        issuer = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    metadata = _server_metadata(issuer)
    methods = metadata.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise McpOAuthError("The authorization server does not support PKCE (S256), which MCP requires.")
    registration = metadata.get("registration_endpoint")
    return AuthorizationServer(
        issuer=issuer,
        authorization_endpoint=_https(metadata.get("authorization_endpoint"), "authorize endpoint"),
        token_endpoint=_https(metadata.get("token_endpoint"), "token endpoint"),
        registration_endpoint=_https(registration, "registration endpoint") if registration else None,
        resource=resource,
    )


def register(server: AuthorizationServer, redirect_uri: str) -> RegisteredClient:
    """RFC 7591: a client for Jaah's Connect callback. Asks for a public client (PKCE is its proof); a server that
    insists on a confidential one hands back a secret and the method to send it by, both kept."""
    if server.registration_endpoint is None:
        raise McpOAuthError(
            "The authorization server offers no Dynamic Client Registration - enter your own client ID instead."
        )
    status, body, _ = _dial("POST", server.registration_endpoint, body={
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    if status not in (200, 201) or not isinstance(body, dict):
        raise McpOAuthError(f"The authorization server refused to register Jaah (HTTP {status}).")
    client_id = body.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise McpOAuthError("The authorization server registered Jaah but returned no client ID.")
    raw_secret = body.get("client_secret")
    secret = raw_secret if isinstance(raw_secret, str) else ""
    # RFC 7591 §2: an omitted method is `client_secret_basic` - for a client that holds a secret.
    method = body.get("token_endpoint_auth_method") or ("client_secret_basic" if secret else "none")
    if method not in _AUTH_METHODS:
        raise McpOAuthError(f"The authorization server registered Jaah for {str(method)[:40]!r}, which Jaah cannot use.")
    return RegisteredClient(client_id=client_id.strip(), client_secret=secret, auth_method=method)


# ── The operator-plane client store ─────────────────────────────────────────────────────────────────────────────

def _row_name(issuer: str) -> str:
    """The `oauth_app` row's name for `issuer`; a long issuer is cut and keeps a digest of the whole, so two issuers
    sharing a prefix never share a name (and the row's `provider` is compared exactly on read)."""
    name = f"{_ROW_NAME_PREFIX}{issuer}"
    if len(name) <= _ROW_NAME_MAX:
        return name
    digest = hashlib.sha256(issuer.encode("utf-8")).hexdigest()[:16]
    return f"{name[:_ROW_NAME_MAX - 18]}… {digest}"


def _stored_client(issuer: str) -> Optional[RegisteredClient]:
    """The client registered with `issuer` earlier, from its operator-plane `oauth_app` row, or None."""
    from sqlalchemy import select

    from app import data_access_events
    from app.db import BridgedSession
    from app.models.credentials import Credential
    from app.tenancy import seam
    from app.tenancy.ids import ORG_ZERO_ID

    with seam.detached(), seam.operator_scope(), BridgedSession() as session:
        row = session.scalars(select(Credential).where(
            Credential.org_id == ORG_ZERO_ID, Credential.type == "oauth_app", Credential.system.is_(True),
            Credential.name == _row_name(issuer),
        ).order_by(Credential.created_at.asc())).first()
        if row is None:
            return None
        try:
            config = data_access_events.opened_config_for(session, row, consumer="mcp_client_registration")
        except ValueError:
            log.warning("mcp oauth: the stored client for an authorization server cannot be opened (id=%s)", row.id)
            return None
        session.commit()
    client_id = str(config.get("client_id") or "").strip()
    if not client_id or config.get("provider") != issuer:
        return None
    # RFC 7591's default for a client holding a secret (rows written here always state it).
    method = str(config.get("token_auth") or "").strip() or ("client_secret_basic" if config.get("client_secret") else "none")
    return RegisteredClient(client_id, str(config.get("client_secret") or ""), method)


def _store_client(issuer: str, client: RegisteredClient) -> RegisteredClient:
    """Keep `client` as the operator-plane `oauth_app` row for `issuer`; a row another Connect stored first wins."""
    from sqlalchemy import func, select

    from app.db import BridgedSession
    from app.entity_shape import SYSTEM_ACTOR_ID
    from app.models.credentials import Credential
    from app.tenancy import seam
    from app.tenancy.ids import ORG_ZERO_ID

    name = _row_name(issuer)
    with seam.detached(), seam.operator_scope(), BridgedSession() as session:
        session.execute(select(func.pg_advisory_xact_lock(func.hashtext(name))))
        exists = session.scalar(select(Credential.id).where(
            Credential.org_id == ORG_ZERO_ID, Credential.type == "oauth_app", Credential.system.is_(True),
            Credential.name == name,
        ))
        if exists is None:
            config = {"client_id": client.client_id, "provider": issuer, "token_auth": client.auth_method,
                      "notes": "Registered by Dynamic Client Registration (RFC 7591) for MCP Connect"}
            if client.client_secret:
                config["client_secret"] = client.client_secret
            session.add(Credential(
                org_id=ORG_ZERO_ID, name=name, type="oauth_app", system=True, owner_id=None, config=config,
                description="The OAuth client Jaah registered with this MCP authorization server",
                created_by_id=SYSTEM_ACTOR_ID, updated_by_id=SYSTEM_ACTOR_ID,
            ))
            session.commit()
            log.info("mcp oauth: registered a client with an authorization server")
            return client
    # Lost a race: the first registration is the one every Connect uses.
    return _stored_client(issuer) or client


def grant_for(config: Mapping[str, Any], *, redirect_uri: str = "", register_missing: bool = False) -> Grant:
    """What one authorize, exchange or refresh on an `mcp_server` row runs on, derived from its `url` now.

    The row's own `client_id` (BYO) wins; blank means Jaah's registration with the server's authorization server,
    registered on the spot when `register_missing` (Connect start) and refused otherwise. Blocking (DNS, the
    vendor, the operator plane): call it on a worker thread."""
    url = str(config.get("url") or "").strip()
    if not url:
        raise McpOAuthError("Save the server URL before connecting.")
    server = discover(url)
    own = str(config.get("client_id") or "").strip()
    if own:
        return Grant(server.authorization_endpoint, server.token_endpoint, server.resource, own, "", False, False)
    client = _stored_client(server.issuer)
    if client is None:
        if not register_missing:
            raise McpOAuthError("Jaah has no client registered with this server's authorization server - press Connect.")
        client = _store_client(server.issuer, register(server, redirect_uri))
    return Grant(server.authorization_endpoint, server.token_endpoint, server.resource, client.client_id,
                 client.client_secret, client.auth_method == "client_secret_basic", True)
