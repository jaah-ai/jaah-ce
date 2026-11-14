"""The one OAuth2 engine every standard Connect rides: a declarative provider catalogue and the calls on it.

Every authorization-code Connect in the console reads its provider from `PROVIDERS` and goes through
`authorize_url` / `exchange_code` / `refresh` / `fetch_account` here, which put each provider's wire
quirks onto `oauth2_wire.py` (the RFC wire, no vendor knowledge). The flows on top differ only in where the
result is stored:
  * credential Connect (`routes/credentials/connect.py`, refresh `oauth2_tokens.py`) — a credential row; the
    `credentials/oauth_wizards.py` subset names which credential types it fills;
  * forge Connect (the one-click engine, `routes/integrations.py`, with the forges' hooks: `forge_oauth.on_grant`,
    `routes/forge_connect.py::CONNECT_HOOKS`);
  * tenant Gmail (the one-click engine with Gmail's hooks: `tenant_gmail.on_grant`, `routes/org_email.py::CONNECT_HOOKS`);
  * Add to Slack (`routes/slack_connect.py`, whose code exchange goes through the plan-gated
    `slack_egress` seam because Slack answers with the install, not a bare grant).

A provider either pins its OAuth client to an operator-plane credential (`app_setting` names the operator
Settings knob, `app_type` the credential type it must point at — a Jaah-owned app, as Figma, the forges and
Gmail) or leaves it on the row being connected (an operator's own client: Google, Heroku, Zapier, generic).
Slack's app is Jaah-owned too, but its client rides the `slack` credential the socket dials as. Endpoints are public vendor facts and may be pinned here; a client never is.

A `scopes` catalogue makes the consent choosable: every scope not `optional` is always asked for, and the
person connecting may untick each `optional` one (`requested_scope`). Without a catalogue, `scope` is the
fixed scope string, or the row states it.

Nothing here logs a token, code, verifier or secret.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Iterable, Literal, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from app import github_api, http_bounded, oauth2_wire
from app.clock import utcnow
from app.oauth2_wire import OAuth2Error, TokenGrant

log = logging.getLogger("app.oauth_providers")


@dataclass(frozen=True)
class OAuthScope:
    """One scope a provider's consent may ask for. `optional` ones are the person's to untick."""

    name: str
    label: str
    optional: bool = False


@dataclass(frozen=True)
class OAuthProvider:
    """One provider's authorization-code flow, as data.

    `authorize_url`/`token_url` None means the credential row supplies them (`generic`); a `{tenant}`
    placeholder is filled by the caller (Entra). `refresh_url` None refreshes at `token_url`.
    `basic_auth` sends the client pair as HTTP Basic instead of in the body (Bitbucket, Figma).
    `send_grant_type`/`refresh_grant_type` False omit `grant_type` on the exchange / the refresh (GitHub's
    exchange never sent it; Figma's refresh endpoint takes `refresh_token` alone). `response_type` None
    omits it on authorize (GitHub, Slack). `authorize_extra` carries non-RFC constants (Google's
    `access_type=offline`). `account_url`/`account_field` read who connected, with the access token as `Bearer`.
    `app_setting`/`app_type`: see the module header. `client_source` says whose client a grant runs on:
    `platform` (the knob's app), `row` (the connected row's own pair) or `platform_or_row` (the row's pair when it
    carries one - a person's own app, BYO - else the knob's); None derives `platform` from `app_setting`, else
    `row` (`client_source_of`). `on_grant`: a `"module:function"` that replaces the default "create a credential
    row from the grant" after a one-click Connect (`routes/integrations.py`), called as
    `(session, provider, grant: TokenGrant, pending: PendingConnect) -> Credential`. `connect_hooks`: a
    `"module:attribute"` naming the `integrations.connect.ConnectHooks` of a provider whose Connect keeps its own
    callback path, client resolution and landing page around the shared engine (the forges, Slack).
    `public_client`: the client may carry no secret (RFC 6749 §2.1 - what Dynamic Client Registration hands out),
    so a blank `client_secret` is sent as none rather than refused. `discovery`: the endpoints and client are never
    the row's to state - `"mcp"` derives them, on every authorize, exchange and refresh, from the row's server URL
    (RFC 9728 -> RFC 8414 -> RFC 7591, `app/mcp_oauth.py::grant_for`), the one field its secrets are sealed to.
    `token_ttl_seconds`: the lifetime assumed for an access token the vendor sends with no `expires_in`
    (Salesforce), so the cache expires and the refresh runs instead of serving a dead token forever; None trusts a
    grant without expiry as non-expiring.
    `client_id_param` names the client parameter on the wire for a vendor that does not call it `client_id`
    (TikTok's `client_key`).
    `expiring_grant_ok` stores a grant whose access token expires with no refresh token (LinkedIn's 60-day
    tokens for self-serve apps): the row works until then, and its Test asks for a reconnect after.
    `origin_field`/`origin_hosts`: a vendor whose OAuth endpoints live on each customer's own host (Backlog's
    `https://<space>.backlog.com`). Every endpoint then starts with `{origin}`, filled per row from the url field
    `origin_field` (`endpoint_for`), and only a subdomain of one of `origin_hosts` fills it (`scoped_origin`), so a
    tenant value can never aim the deployment's client secret off the vendor. `origin_label`: the origin field holds
    only the customer's own label under the one `origin_hosts` domain (Zendesk's `acme` for
    `https://acme.zendesk.com`), written `https://{subdomain}.zendesk.com/...` in the spec.
    `client_metadata`: the deployment's client is a Client ID Metadata Document Jaah serves itself
    (`client_metadata_url`; PostHog): the client id is that document's URL, a public client with no secret and no
    operator knob, so it is always configured.
    `on_grant_config`: a `"module:function"` called after the exchange as `(provider, grant, config) -> {field:
    value}` (`config` the row the grant lands on, empty for a new one),
    blocking: what the grant reaches that the token answer does not say (a Xero organisation, an Atlassian site),
    written onto the row beside the tokens; it raises `integrations.connect.GrantRefused` when there is none.
    `grant_fields` names the fields it fills, which a Connect row therefore has without a pasted key.
    """

    key: str
    label: str
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    refresh_url: Optional[str] = None
    scope: Optional[str] = None
    scopes: tuple[OAuthScope, ...] = ()
    scope_separator: str = " "
    pkce: bool = True
    basic_auth: bool = False
    send_grant_type: bool = True
    refresh_grant_type: bool = True
    response_type: Optional[str] = "code"
    authorize_extra: Optional[Mapping[str, str]] = None
    account_url: Optional[str] = None
    account_field: Optional[str] = None
    app_setting: Optional[str] = None
    app_type: Optional[str] = None
    client_source: Optional[Literal["platform", "row", "platform_or_row"]] = None
    on_grant: Optional[str] = None
    connect_hooks: Optional[str] = None
    public_client: bool = False
    discovery: Optional[Literal["mcp"]] = None
    token_ttl_seconds: Optional[int] = None
    client_id_param: str = "client_id"
    expiring_grant_ok: bool = False
    origin_field: Optional[str] = None
    origin_hosts: tuple[str, ...] = ()
    origin_label: bool = False
    client_metadata: bool = False
    on_grant_config: Optional[str] = None
    grant_fields: tuple[str, ...] = ()

    @property
    def platform_app(self) -> bool:
        """Whether the OAuth client is the deployment's own (an operator knob, or the metadata document Jaah serves),
        not the connected row's."""
        return self.app_setting is not None or self.client_metadata


# The forges' Connect runs on the shared engine (`routes/integrations.py`) with these two hooks: the row and the
# per-user git pointer (`forge_oauth.on_grant`), and the app, callback path and landing page they keep.
_FORGE_ON_GRANT = "app.forge_oauth:on_grant"
_FORGE_HOOKS = "app.routes.forge_connect:CONNECT_HOOKS"

# GitHub's endpoints and scope (`github_oauth.py` re-exports them under their historic names).
_GITHUB_SCOPE = "repo workflow read:org read:user user:email"

# Figma's REST scopes (developers.figma.com/docs/rest-api/scopes), kept to what a runner builds from a design
# with: Figma's public-app review wants every scope tied to a feature, so nothing speculative is asked for.
# `current_user:read` names the connected account (`account_url`); the rest read a design, its feedback and its
# design system, and `folders:read` lists a team's folders and their files so a runner can find a design.
# The one write, replying on a design, is optional.
_FIGMA_SCOPES: tuple[OAuthScope, ...] = (
    OAuthScope("current_user:read", "Your name, email and avatar"),
    OAuthScope("folders:read", "List folders and their files"),
    OAuthScope("file_content:read", "Read file contents"),
    OAuthScope("file_comments:read", "Read comments"),
    OAuthScope("library_content:read", "Read published components and styles"),
    OAuthScope("file_comments:write", "Post and delete comments and reactions", optional=True),
)

_GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"

PROVIDERS: dict[str, OAuthProvider] = {
    # ── Forges (`forge_oauth.FORGE_OAUTH` adds what a forge needs beyond the wire) ──
    "github": OAuthProvider(
        key="github", label="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scope=_GITHUB_SCOPE, send_grant_type=False, response_type=None,
        account_url=f"{github_api.GITHUB_API_BASE}/user", account_field="login",
        # GitHub resolves its app through `git_access.APP_KNOBS` (GitHub App or OAuth App); this names the
        # OAuth App only.
        app_setting="git_app_credential_id", app_type="github_app",
        on_grant=_FORGE_ON_GRANT, connect_hooks=_FORGE_HOOKS,
    ),
    # PKCE; 2 h tokens whose refresh token rotates.
    "gitlab": OAuthProvider(
        key="gitlab", label="GitLab",
        authorize_url="https://gitlab.com/oauth/authorize", token_url="https://gitlab.com/oauth/token",
        scope="api read_user write_repository",
        account_url="https://gitlab.com/api/v4/user", account_field="username",
        app_setting="gitlab_app_credential_id", app_type="gitlab_app",
        on_grant=_FORGE_ON_GRANT, connect_hooks=_FORGE_HOOKS,
    ),
    # No PKCE (state alone binds the callback); no `scope` (it lives on the consumer); Basic only; `/2.0/user`
    # has no `username`, so `nickname`.
    "bitbucket": OAuthProvider(
        key="bitbucket", label="Bitbucket",
        authorize_url="https://bitbucket.org/site/oauth2/authorize",
        token_url="https://bitbucket.org/site/oauth2/access_token",
        pkce=False, basic_auth=True,
        account_url="https://api.bitbucket.org/2.0/user", account_field="nickname",
        app_setting="bitbucket_app_credential_id", app_type="bitbucket_app",
        on_grant=_FORGE_ON_GRANT, connect_hooks=_FORGE_HOOKS,
    ),
    # A Microsoft Entra ID v2 app: the Azure DevOps resource's `.default` plus `offline_access` (no refresh token
    # without it); `prompt=select_account` shows the account picker.
    "azure_devops": OAuthProvider(
        key="azure_devops", label="Azure DevOps",
        authorize_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        scope="499b84ac-1321-427f-aa17-267ca6975798/.default offline_access",
        authorize_extra={"prompt": "select_account"},
        account_url="https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1",
        account_field="emailAddress",
        app_setting="azure_devops_app_credential_id", app_type="azure_devops_app",
        on_grant=_FORGE_ON_GRANT, connect_hooks=_FORGE_HOOKS,
    ),
    # ── Credential Connect on the operator's own client (`credentials/oauth_wizards.py`) ──
    # No default scope: Google scopes are per-API, and a default would silently mis-grant. `access_type=offline`
    # and `prompt=consent` are what make a refresh token come back.
    "google": OAuthProvider(
        key="google", label="Google",
        authorize_url=_GOOGLE_AUTHORIZE, token_url=_GOOGLE_TOKEN,
        authorize_extra={"access_type": "offline", "prompt": "consent"},
    ),
    # `global` is the scope a Platform API token carries.
    "heroku": OAuthProvider(
        key="heroku", label="Heroku",
        authorize_url="https://id.heroku.com/oauth/authorize", token_url="https://id.heroku.com/oauth/token",
        scope="global",
    ),
    # Zapier's partner endpoints (authorize on the API host, token on zapier.com).
    "zapier": OAuthProvider(
        key="zapier", label="Zapier",
        authorize_url="https://api.zapier.com/v2/authorize", token_url="https://zapier.com/oauth/token/",
        scope="profile",
    ),
    # Pins nothing, for `oauth2_client`: endpoints, scope and label come off the row. No `authorize_extra`:
    # offline access is spelled differently per vendor.
    "generic": OAuthProvider(key="generic", label=""),
    # A remote MCP server's own authorization server, found from the server URL each time (`app/mcp_oauth.py`), with
    # Jaah's Dynamically Registered client or the row's own. PKCE always (the MCP authorization spec requires it);
    # the client is usually public. Nothing on the row names an endpoint.
    "mcp": OAuthProvider(key="mcp", label="", client_source="row", public_client=True, discovery="mcp"),
    # ── Credential Connect on a Jaah-owned app ──
    # developers.figma.com/docs/rest-api/oauth-apps: client pair as HTTP Basic on both calls; refresh is its own
    # endpoint taking `refresh_token` alone, and does not rotate it.
    "figma": OAuthProvider(
        key="figma", label="Figma",
        authorize_url="https://www.figma.com/oauth",
        token_url="https://api.figma.com/v1/oauth/token",
        refresh_url="https://api.figma.com/v1/oauth/refresh",
        scopes=_FIGMA_SCOPES, basic_auth=True, refresh_grant_type=False,
        account_url="https://api.figma.com/v1/me", account_field="handle",
        app_setting="figma_app_credential_id", app_type="figma_app",
    ),
    # ── Org connectors ──
    # A member's own Gmail, send only, on the operator's Google client; consent shown every time so a reconnect
    # is handed a fresh refresh token. Its hook writes the org's mailbox and its system `gmail_oauth` row; its hooks
    # keep the callback path registered on that client, the Email card's checks and landing.
    "gmail": OAuthProvider(
        key="gmail", label="Google",
        authorize_url=_GOOGLE_AUTHORIZE, token_url=_GOOGLE_TOKEN,
        scope="https://www.googleapis.com/auth/gmail.send openid email",
        authorize_extra={"access_type": "offline", "prompt": "consent", "include_granted_scopes": "false"},
        app_setting="tenant_gmail_oauth_credential_id", app_type="google", client_source="platform",
        on_grant="app.tenant_gmail:on_grant", connect_hooks="app.routes.org_email:CONNECT_HOOKS",
    ),
    # The bot scopes are exactly the Slack app manifest's. Slack's
    # authorize takes neither PKCE nor `response_type`, and comma-separated scopes.
    "slack": OAuthProvider(
        key="slack", label="Slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        scope="chat:write,im:write,im:history,app_mentions:read,links:read,links:write,users:read,channels:read,groups:read,chat:write.public",
        scope_separator=",", pkce=False, response_type=None,
        # No knob: the client is on the `slack` credential the socket dials as (`slack_socket._load_credential`), and
        # the install answer (team, bot user) is more than a grant: the row is `slack_workspaces.on_grant`'s, the
        # rest (Slack-registered callback, plan-gated exchange, landing) `routes/slack_connect.py::CONNECT_HOOKS`.
        on_grant="app.slack_workspaces:on_grant",
        connect_hooks="app.routes.slack_connect:CONNECT_HOOKS",
    ),
}


# A platform-sourced client needs its app-setting knob, and a platform-app provider pins its endpoints: one left
# to the row would let a tenant aim the deployment's client secret at a host of its choosing. `RuntimeError`, not
# `assert`, so `python -O` keeps it.
for _provider in PROVIDERS.values():
    if _provider.client_source in ("platform", "platform_or_row") and not _provider.platform_app:
        raise RuntimeError(f"{_provider.key}: client_source {_provider.client_source!r} needs an app_setting knob")
    if _provider.platform_app and not (_provider.authorize_url and _provider.token_url):
        raise RuntimeError(f"{_provider.key}: a platform-app provider must pin its authorize and token endpoints")


#: The placeholder an origin-scoped provider's endpoints start with (`OAuthProvider.origin_field`).
ORIGIN_PLACEHOLDER = "{origin}"
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def scoped_origin(provider: OAuthProvider, value: Any) -> Optional[str]:
    """`https://<host>` for `value` (`acme.backlog.com`, or `https://acme.backlog.com` with at most a `/` after it)
    when the host is a subdomain of one of `provider.origin_hosts`; None for anything else - another host, a port,
    userinfo, a path, a query, or a provider with no `origin_hosts`. An `origin_label` provider also takes the bare
    label (`acme`)."""
    if not provider.origin_hosts or not isinstance(value, str):
        return None
    raw = value.strip()
    if provider.origin_label and _HOST_LABEL.fullmatch(raw.lower()):
        # The bare label the spec's field asks for (`acme`), under the vendor's one domain.
        raw = f"{raw}.{provider.origin_hosts[0]}"
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".")
    if (parts.scheme.lower() != "https" or port is not None or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc):
        return None
    labels = host.split(".")
    if not all(_HOST_LABEL.fullmatch(label) for label in labels):
        return None
    # Exactly one label under the vendor's domain (the customer's own space), never a deeper or sibling name.
    if not any(host.endswith(f".{suffix}") and "." not in host[: -len(suffix) - 1] for suffix in provider.origin_hosts):
        return None
    return f"https://{host}"


def origin_value(provider: OAuthProvider, origin: str) -> str:
    """What a row keeps in `origin_field` for an attempt's checked `origin` (`scoped_origin`'s answer): the origin
    itself, or for an `origin_label` provider its first label (the subdomain the field asks for)."""
    if provider.origin_label:
        return (urlsplit(origin).hostname or "").split(".")[0]
    return origin


def endpoint_for(provider: OAuthProvider, url: Optional[str], config: Mapping[str, Any]) -> Optional[str]:
    """`url` (one of the provider's endpoints) as a row with `config` dials it: unchanged for a provider with fixed
    endpoints, its `{origin}` filled from the row's `origin_field` for an origin-scoped one (None when the row
    names no host `scoped_origin` accepts). None for no `url`."""
    if not url:
        return None
    if provider.origin_field is None:
        return url
    origin = scoped_origin(provider, config.get(provider.origin_field))
    return url.replace(ORIGIN_PLACEHOLDER, origin, 1) if origin and url.startswith(ORIGIN_PLACEHOLDER) else None


def _dialable(provider: OAuthProvider, target: Optional[str], what: str) -> str:
    """`target`, or the `ValueError` for none - or for an origin-scoped endpoint nobody filled (`endpoint_for`)."""
    if not target:
        raise ValueError(f"{provider.key}: no {what} endpoint")
    if ORIGIN_PLACEHOLDER in target:
        raise ValueError(f"{provider.key}: the {what} endpoint needs the row's {provider.origin_field}")
    return target


def row_client_missing(provider: OAuthProvider, config: Mapping[str, Any]) -> bool:
    """Whether a row-client grant cannot run: no client id, or no secret where the provider needs one. A sealed
    secret reads as present (`entity_shape.is_blank`)."""
    from app.entity_shape import is_blank

    if is_blank(config.get("client_id")):
        return True
    return not provider.public_client and is_blank(config.get("client_secret"))


def client_source_of(provider: OAuthProvider) -> str:
    """`platform`, `row` or `platform_or_row`: the provider's declared source, else the one `app_setting` implies."""
    if provider.client_source is not None:
        return provider.client_source
    return "platform" if provider.platform_app else "row"


def uses_platform_client(provider: OAuthProvider, config: Mapping[str, Any]) -> bool:
    """Whether a grant on a row with `config` runs on the deployment's app rather than the row's own client.

    `platform_or_row` follows the row: its own client id makes it the row's (BYO), a blank one the platform's."""
    source = client_source_of(provider)
    if source == "platform_or_row":
        client_id = config.get("client_id")
        return not (isinstance(client_id, str) and client_id.strip())
    return source == "platform"


class ScopeChoiceError(ValueError):
    """A scope choice naming a scope the provider's catalogue does not offer as optional."""


def requested_scope(
    provider: OAuthProvider, *, chosen: Optional[Iterable[str]] = None, stated: Optional[str] = None
) -> Optional[str]:
    """The scope string one consent asks for.

    With a catalogue: every required scope, plus each optional one in `chosen` (None keeps them all).
    Without one: `stated` (the row's own Scopes field) when non-blank, else the provider's fixed `scope`,
    else None — an invented scope would grant the wrong thing.
    """
    if provider.scopes:
        optional = {scope.name for scope in provider.scopes if scope.optional}
        picked = optional if chosen is None else set(chosen)
        unknown = picked - optional
        if unknown:
            raise ScopeChoiceError(f"{provider.label} offers no optional scope {sorted(unknown)[0][:64]!r}")
        names = [scope.name for scope in provider.scopes if not scope.optional or scope.name in picked]
        return provider.scope_separator.join(names)
    if stated and stated.strip():
        return stated.strip()
    return provider.scope


def authorize_url(
    provider: OAuthProvider,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    scope: Optional[str] = None,
    endpoint: Optional[str] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> str:
    """Where the browser goes. `challenge` must be blank exactly when the provider has no PKCE. `scope` None
    sends `requested_scope(provider)`; `endpoint` overrides the pinned one (a row's URL, an Entra tenant);
    `extra` adds per-attempt parameters over the provider's own."""
    if bool(challenge) != provider.pkce:
        raise ValueError(f"{provider.key}: a PKCE challenge is {'required' if provider.pkce else 'not sent'}")
    target = _dialable(provider, endpoint or provider.authorize_url, "authorize")
    merged = {**(provider.authorize_extra or {}), **(extra or {})}
    return oauth2_wire.authorize_url(
        endpoint=target,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        challenge=challenge,
        scope=requested_scope(provider) if scope is None else scope,
        extra=merged or None,
        response_type=provider.response_type,
        client_id_param=provider.client_id_param,
    )


def exchange_code(
    provider: OAuthProvider,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    verifier: str,
    endpoint: Optional[str] = None,
    vendor: Optional[str] = None,
    accept: str = "application/json",
    status_hint: Optional[str] = None,
    error_cls: type[OAuth2Error] = OAuth2Error,
    extra: Optional[Mapping[str, str]] = None,
    basic_auth: Optional[bool] = None,
) -> TokenGrant:
    """Trade the callback's code for a grant (`redirect_uri` byte-identical to the authorize one). `extra`: the
    non-secret parameters the attempt adds (RFC 8707's `resource`); `basic_auth` overrides the provider's for a
    client that registered its own method (`mcp_oauth.Grant.basic_auth`)."""
    target = _dialable(provider, endpoint or provider.token_url, "token")
    return _with_assumed_expiry(provider, oauth2_wire.exchange_code(
        endpoint=target,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
        vendor=vendor or provider.label or "the vendor",
        accept=accept,
        status_hint=status_hint,
        error_cls=error_cls,
        send_grant_type=provider.send_grant_type,
        basic_auth=provider.basic_auth if basic_auth is None else basic_auth,
        extra=extra,
        client_id_param=provider.client_id_param,
    ))


def refresh(
    provider: OAuthProvider,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    endpoint: Optional[str] = None,
    scope: Optional[str] = None,
    vendor: Optional[str] = None,
    accept: str = "application/json",
    status_hint: Optional[str] = None,
    error_cls: type[OAuth2Error] = OAuth2Error,
    extra: Optional[Mapping[str, str]] = None,
    basic_auth: Optional[bool] = None,
) -> TokenGrant:
    """Spend `refresh_token` for a new grant, at `refresh_url` when the provider has one. The grant is the
    provider's answer as is: a caller holding a rotating token persists the returned one, and keeps its own
    when none comes back. `extra` and `basic_auth` as for `exchange_code`."""
    target = _dialable(provider, endpoint or provider.refresh_url or provider.token_url, "refresh")
    return _with_assumed_expiry(provider, oauth2_wire.refresh(
        endpoint=target,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        scope=scope,
        vendor=vendor or provider.label or "the vendor",
        accept=accept,
        status_hint=status_hint,
        error_cls=error_cls,
        basic_auth=provider.basic_auth if basic_auth is None else basic_auth,
        send_grant_type=provider.refresh_grant_type,
        extra=extra,
        client_id_param=provider.client_id_param,
    ))


def _with_assumed_expiry(provider: OAuthProvider, grant: TokenGrant) -> TokenGrant:
    """The grant, with `provider.token_ttl_seconds` as its expiry when the vendor named none."""
    if grant.expires_at is not None or provider.token_ttl_seconds is None:
        return grant
    return replace(grant, expires_at=utcnow() + timedelta(seconds=provider.token_ttl_seconds))


# The account read is best effort: the user is waiting on a redirect, and a failure only costs a name.
_ACCOUNT_TIMEOUT_SECONDS = 15.0
_ACCOUNT_MAX_BYTES = 256 * 1024
# The longest account name kept (a forge login, an email); longer is treated as unreadable.
_ACCOUNT_MAX_LEN = 100

# The seam tests replace (`http_bounded.client`'s redirect-off, capped-read policy).
_client = http_bounded.client


def account_of(provider: OAuthProvider, access_token: str, config: Mapping[str, Any]) -> Optional[str]:
    """`fetch_account` as a row with `config` reads it: an origin-scoped provider's account URL filled from the row."""
    if provider.origin_field is None:
        return fetch_account(provider, access_token)
    return fetch_account(provider, access_token, endpoint=endpoint_for(provider, provider.account_url, config))


def fetch_account(provider: OAuthProvider, access_token: str, *, endpoint: Optional[str] = None) -> Optional[str]:
    """Who connected (`account_field` of `account_url`), or None — never a failure of the connect, since the
    code is already spent. `endpoint` is the account URL as the row dials it (`endpoint_for`), for an
    origin-scoped provider. Only a status or an exception class is logged."""
    target = endpoint or provider.account_url
    if not target or not provider.account_field or ORIGIN_PLACEHOLDER in target:
        return None
    try:
        with _client(
            _ACCOUNT_TIMEOUT_SECONDS,
            {"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        ) as client:
            with client.stream("GET", target) as response:
                raw, truncated = http_bounded.read_capped(response, _ACCOUNT_MAX_BYTES)
                status = response.status_code
    except httpx.HTTPError as exc:
        # The class only: `str(exc)` renders the request URL.
        log.warning("oauth connect: %s account read failed (%s)", provider.key, type(exc).__name__)
        return None
    payload = None if truncated else http_bounded.json_or_none(raw)
    if status != 200 or not isinstance(payload, dict):
        log.warning("oauth connect: %s account read answered HTTP %s", provider.key, status)
        return None
    value = payload.get(provider.account_field)
    if not isinstance(value, str):
        return None
    login = value.strip()
    if not login or len(login) > _ACCOUNT_MAX_LEN or any(ord(char) < 0x20 or ord(char) == 0x7F for char in login):
        return None
    return login


def platform_client_configured(provider: OAuthProvider) -> bool:
    """Whether the operator designated this provider's app (the knob is non-blank). Opens nothing."""
    from app.operator_settings import reads as settings_reads

    if provider.client_metadata:
        return True
    if provider.app_setting is None:
        return False
    try:
        return bool(settings_reads.get_text(provider.app_setting).strip())
    except KeyError:
        # A knob this build does not declare (a catalog vendor whose knob is not derived here) is unset.
        return False


def platform_client(provider: OAuthProvider, *, consumer: str) -> Optional[tuple[str, str]]:
    """The deployment's own OAuth client `(client_id, client_secret)` for `provider`, or None while its knob is
    blank or names no usable `app_type` credential on the operator plane (`operator_credentials.opened`)."""
    from app import operator_credentials
    from app.operator_settings import reads as settings_reads

    if provider.client_metadata:
        # A public client: the document's URL is the client id, and there is no secret to open.
        return client_metadata_url(provider), ""
    if provider.app_setting is None or provider.app_type is None:
        return None
    try:
        raw = settings_reads.get_text(provider.app_setting).strip()
    except KeyError:
        return None
    if not raw:
        return None
    opened = operator_credentials.opened(raw, provider.app_type, consumer=consumer) or {}
    client_id = str(opened.get("client_id") or "").strip()
    client_secret = str(opened.get("client_secret") or "")
    return (client_id, client_secret) if client_id and client_secret else None


#: Where Jaah serves a `client_metadata` provider's Client ID Metadata Document (`routes/integrations.py`).
CLIENT_METADATA_PATH = "/api/public/integrations/{slug}/client-metadata.json"


def client_metadata_url(provider: OAuthProvider) -> str:
    """The deployment's client id for a `client_metadata` provider: the absolute URL of its metadata document, built
    from `frontend_url` as the redirect URI is, so it is the same on authorize, exchange and refresh."""
    from app.config import console_url

    return console_url(CLIENT_METADATA_PATH.format(slug=provider.key))
