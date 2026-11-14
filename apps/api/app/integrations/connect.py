"""One-click Connect: a catalog vendor (or an unlisted provider with an `on_grant` hook) connected with no row saved
first. The start leg (`routes/integrations.py`) seals what the person chose into the flow cookie; the shared callback
(`routes/credentials/connect.py`) spends the code and creates the row here, so an abandoned attempt leaves nothing.

What the cookie carries is a `PendingConnect`, sealed (`credential_crypto.seal`, AAD bound to the attempt's state) rather
than only signed: a person's own OAuth client secret (BYO) rides in it. Nothing here logs a code, token or secret.
"""
from __future__ import annotations

import importlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Optional
from uuid import UUID

from app import credential_crypto, data_access_events, oauth2_wire, oauth_providers
from app.integrations import catalog
from app.oauth_providers import PROVIDERS, OAuthProvider

if TYPE_CHECKING:
    from fastapi.responses import RedirectResponse
    from sqlalchemy.orm import Session

    from app.models.credentials import Credential
    from app.models.identity import AdminUser

log = logging.getLogger(__name__)

# The pending payload's domain separator: a sealed pending from another store, or another attempt, never opens.
_PENDING_AAD = b"integration-connect-pending:"


@dataclass(frozen=True)
class PendingConnect:
    """What the person chose at the start leg, carried to the callback; the hook contract's `pending` argument.

    `owner` is the new row's ownership (`org` or `personal`); `project_ids` the projects it is attached to.
    `client_id`/`client_secret` are a person's own OAuth app (BYO), blank on the deployment's. `scope` is what the
    consent asked for; `account` is filled at the callback from the vendor's account read. `return_to` and `app` are
    a hooked provider's own choices (`ConnectHooks.prepare` checks them): the console page to land on, and which of
    the deployment's apps the attempt runs on (GitHub's `git_access.APP_KNOBS`). `org_id` is the org a hooked
    provider's `prepare` started the attempt for, for its hook to hold the callback to (tenant Gmail); None otherwise.
    `origin` is an origin-scoped provider's host for this attempt (`oauth_providers.scoped_origin`, already checked),
    blank for every other provider: the endpoints are filled from it and the new row keeps it."""

    integration: str
    name: str
    owner: Literal["org", "personal"]
    user_id: UUID
    project_ids: tuple[int, ...] = ()
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    scope: str = ""
    account: str = ""
    return_to: str = ""
    app: str = ""
    org_id: Optional[UUID] = None
    origin: str = ""

    @property
    def byo(self) -> bool:
        return bool(self.client_id)


def seal_pending(pending: PendingConnect, *, state: str) -> str:
    """The cookie member: the pending choice sealed under this attempt's state."""
    envelope = credential_crypto.seal({
        "integration": pending.integration, "name": pending.name, "owner": pending.owner,
        "user_id": str(pending.user_id), "project_ids": list(pending.project_ids),
        "client_id": pending.client_id, "client_secret": pending.client_secret, "scope": pending.scope,
        "return_to": pending.return_to, "app": pending.app,
        "org_id": str(pending.org_id) if pending.org_id is not None else "", "origin": pending.origin,
    }, aad=_PENDING_AAD + state.encode())
    return json.dumps(envelope, separators=(",", ":"))


def open_pending(raw: str, *, state: str) -> Optional[PendingConnect]:
    """The sealed member opened, or None for anything that does not open as one (treated as an expired attempt)."""
    try:
        data = credential_crypto.unseal(json.loads(raw), aad=_PENDING_AAD + state.encode(), label="connect attempt")
        owner = data["owner"]
        if owner not in ("org", "personal"):
            return None
        return PendingConnect(
            integration=str(data["integration"]), name=str(data["name"]), owner=owner,
            user_id=UUID(str(data["user_id"])), project_ids=tuple(int(pid) for pid in data["project_ids"]),
            client_id=str(data.get("client_id") or ""), client_secret=str(data.get("client_secret") or ""),
            scope=str(data.get("scope") or ""), return_to=str(data.get("return_to") or ""),
            app=str(data.get("app") or ""),
            org_id=UUID(str(data["org_id"])) if data.get("org_id") else None,
            origin=str(data.get("origin") or ""),
        )
    except (ValueError, KeyError, TypeError):
        return None


def provider_for(slug: str) -> Optional[OAuthProvider]:
    """The provider a one-click Connect for `slug` runs on: a catalog vendor's OAuth method, or an unlisted provider
    that names an `on_grant` hook this edition ships (one with neither has no row to create). None otherwise."""
    spec = catalog.spec_for(slug)
    provider = PROVIDERS.get(slug)
    if provider is None:
        return None
    if spec is not None and spec.provider is not None:
        return provider
    return provider if provider.on_grant and _hooks_shipped(provider) else None


def _hooks_shipped(provider: OAuthProvider) -> bool:
    """Whether a provider's hook modules are in this tree: an edition that leaves its feature out (tenant Gmail in the
    community edition) has no such module, so its provider offers no Connect."""
    from app.credentials.testers import resolve_hook

    try:
        resolve_hook(provider.on_grant or "")
        if provider.connect_hooks:
            importlib.import_module(provider.connect_hooks.partition(":")[0])
    except ImportError:
        return False
    return True


class ClientRefusal(Exception):
    """Why no OAuth client can run this attempt: `slug` is the flow's closed error, `status`/`detail` the start's."""

    def __init__(self, slug: str, status: int, detail: str) -> None:
        super().__init__(detail)
        self.slug, self.status, self.detail = slug, status, detail


def client_for(provider: OAuthProvider, pending: PendingConnect, *, consumer: str) -> tuple[str, str]:
    """`(client_id, client_secret)` for this attempt: the person's own app when they brought one (and the provider
    takes a row client), else the deployment's (`platform_client`). Blocking (opens the operator credential)."""
    source = oauth_providers.client_source_of(provider)
    if pending.byo:
        if source == "platform":
            raise ClientRefusal("bad_request", 400, f"{provider.label} connects through Jaah's app only.")
        if not pending.client_secret:
            raise ClientRefusal("bad_request", 400, "Enter your OAuth app's client secret with its client ID.")
        return pending.client_id, pending.client_secret
    if source == "row":
        raise ClientRefusal(
            "bad_request", 400, f"{provider.label} connects with your own OAuth app - enter its client ID and secret."
        )
    platform = oauth_providers.platform_client(provider, consumer=consumer)
    if platform is None:
        raise ClientRefusal(
            "not_configured", 409,
            f"Jaah's {provider.label} app isn't set up yet - use your own OAuth app or an API key, or ask an operator.",
        )
    return platform


def app_configured(provider: OAuthProvider) -> bool:
    """Whether the deployment's app for `provider` can take a Connect: a hooked provider's own answer
    (`ConnectHooks.app_available`), else its `app_setting` designated. Blocking for a hooked provider."""
    hooks = hooks_for(provider)
    if hooks is not None and hooks.app_available is not None:
        return hooks.app_available(provider)
    return oauth_providers.client_source_of(provider) != "row" and oauth_providers.platform_client_configured(provider)


def byo_allowed(provider: OAuthProvider) -> bool:
    """Whether a person's own OAuth app is accepted: never for a hooked provider (each refuses one at the start)."""
    if hooks_for(provider) is not None:
        return False
    return oauth_providers.client_source_of(provider) != "platform"


def pending_config(provider: OAuthProvider, pending: PendingConnect) -> dict[str, str]:
    """The row config an attempt's endpoints are filled from (`oauth_providers.endpoint_for`): an origin-scoped
    provider's host under its `origin_field` (an `origin_label` provider's label), else nothing."""
    if not (provider.origin_field and pending.origin):
        return {}
    return {provider.origin_field: oauth_providers.origin_value(provider, pending.origin)}


def grant_config(
    type_fields: frozenset[str], grant: oauth2_wire.TokenGrant, pending: PendingConnect,
    provider: Optional[OAuthProvider] = None, learned: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """The new row's config from a grant: the tokens, what the attempt learned, the person's own client when
    they brought one (so refresh runs on it, `oauth_providers.uses_platform_client`), an origin-scoped
    provider's host (so refresh and Test reach the same one), and what its `on_grant_config` hook `learned` (the
    organisation or site the grant reaches; it names the account over the account read). Only declared keys."""
    config: dict[str, Any] = dict(pending_config(provider, pending)) if provider is not None else {}
    config["access_token"] = grant.access_token
    if grant.refresh_token:
        config["refresh_token"] = grant.refresh_token
    if grant.expires_at:
        config["expiry_date"] = str(int(grant.expires_at.timestamp() * 1000))
    if pending.scope:
        config["scope"] = pending.scope
    if pending.account:
        config["account"] = pending.account
    if pending.byo:
        config["client_id"] = pending.client_id
        config["client_secret"] = pending.client_secret
    config.update(learned or {})
    return {key: value for key, value in config.items() if key in type_fields}


def learned_from_grant(
    provider: OAuthProvider, grant: oauth2_wire.TokenGrant, config: Optional[Mapping[str, Any]] = None,
) -> dict[str, str]:
    """What the provider's `on_grant_config` hook learns from a fresh grant (`{}` for a provider with none): only
    its declared `grant_fields`, plus the account it names, as non-blank strings. `config` is the row the grant
    lands on (a saved row's config on a row-bound Connect, so the hook keeps to the site it already names). Blocking
    (the hook reads the vendor). Raises `GrantRefused` when the grant reaches nothing a row could use."""
    if provider.on_grant_config is None:
        return {}
    from app.credentials.testers import resolve_hook

    found = resolve_hook(provider.on_grant_config)(provider, grant, dict(config or {}))
    allowed = {*provider.grant_fields, "account"}
    learned = {key: value.strip() for key, value in dict(found).items()
               if key in allowed and isinstance(value, str) and value.strip()}
    if not set(provider.grant_fields) <= set(learned):
        raise GrantRefused("no_account")
    return learned


def grant_is_storable(grant: oauth2_wire.TokenGrant, provider: Optional[OAuthProvider] = None) -> bool:
    """A grant worth a row: one with a refresh token, or an access token the vendor says never expires (Linear,
    Notion and others issue such tokens). An expiring token with no way to renew it is not a credential, unless
    the provider declares that is all it ever issues (`OAuthProvider.expiring_grant_ok`)."""
    if grant.refresh_token or (grant.access_token and grant.expires_at is None):
        return True
    return bool(grant.access_token) and provider is not None and provider.expiring_grant_ok


class GrantRefused(Exception):
    """An `on_grant` hook's refusal of a grant it was handed: `slug` is one of the provider's closed error slugs
    (`ConnectHooks.finish`). The hook has already given the grant back when it must; nothing is stored."""

    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


@dataclass(frozen=True)
class GrantOutcome:
    """What an `on_grant` hook may return instead of the bare row: the row, plus work to run only once the row is
    committed (giving back a token the reconnect replaced). Best effort: `after_commit` must never raise."""

    credential: "Credential"
    after_commit: Optional[Callable[[], None]] = None


@dataclass(frozen=True)
class GrantContext:
    """Who the callback authenticated, for a hook that writes as them: the route's `require_user` row (an acting
    operator's seat copy included) and its principal — never re-derived from the pending payload, whose `user_id`
    the engine has already held equal to `user.id`. `client` is the OAuth client that spent the code (a hook that
    refuses a grant gives it back with that pair), None outside the hooked flow."""

    user: "AdminUser"
    principal: Any
    client: Optional["ConnectClient"] = None


_GRANT_CONTEXT: ContextVar[Optional[GrantContext]] = ContextVar("integration_connect_grant_context", default=None)


def grant_context() -> GrantContext:
    """The `GrantContext` of the `on_grant` call in progress; outside one, a programming error, raised."""
    context = _GRANT_CONTEXT.get()
    if context is None:
        raise RuntimeError("grant_context() is read only inside an on_grant hook the engine called")
    return context


def call_on_grant(target: str, session: Any, provider: OAuthProvider, grant: oauth2_wire.TokenGrant,
                  pending: PendingConnect, *, context: GrantContext) -> GrantOutcome:
    """Run a provider's post-exchange hook (`OAuthProvider.on_grant`) with `context` readable to it
    (`grant_context`): the row it wrote, as a `GrantOutcome`. It may raise `GrantRefused`."""
    from app.credentials.testers import resolve_hook

    token = _GRANT_CONTEXT.set(context)
    try:
        result = resolve_hook(target)(session, provider, grant, pending)
    finally:
        _GRANT_CONTEXT.reset(token)
    return result if isinstance(result, GrantOutcome) else GrantOutcome(credential=result)


@dataclass(frozen=True)
class ConnectClient:
    """The OAuth client one attempt runs on, as a hooked provider resolves it: the pair, and the endpoints and
    authorize parameters this attempt needs beyond the provider's pinned ones (an Entra tenant, an account picker).
    `accept` is the exchange's `Accept` header (GitHub answers its own vendor type)."""

    client_id: str
    client_secret: str = field(repr=False)
    authorize_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    authorize_extra: Optional[Mapping[str, str]] = None
    accept: str = "application/json"


@dataclass(frozen=True)
class ConnectHooks:
    """A provider whose Connect predates the shared engine and keeps its own surroundings: the forges
    (`routes/forge_connect.py::CONNECT_HOOKS`). Named by `OAuthProvider.connect_hooks`; the engine still owns
    authorize, PKCE, state, the sealed pending, the exchange and the row's write (`on_grant`).

    `redirect_path(provider)` is the callback path the vendor's app already registered (served by a route that hands
    the callback to `routes/integrations.py::finish_hooked_callback`), so moving onto the engine changes nothing at
    the vendor; `cookie_path` is the flow cookie's path, which must cover it (one cookie per provider there).
    `prepare`, `client` and `after_grant` may block (operator settings, the app's secret, the vendor), so the engine
    runs them off the loop; `refusal` and `finish` must not block:

    * `prepare(pending, user)` checks and completes the start's choices, raising `HTTPException`;
    * `client(provider, pending)` is the attempt's `ConnectClient`, raising `ClientRefusal`;
    * `finish(slug, pending, error)` is where the browser lands, an `error` from the provider's closed vocabulary;
    * `refusal(provider, slug, text)`, when set, maps a vendor `denied`/`exchange_failed` (with the vendor's own
      text, never echoed) to the provider's own slug; the default keeps the engine's slug;
    * `after_grant(session, user, provider, grant, pending, credential_id)`, when set, runs once the row is
      committed, and may return a redirect that replaces `finish`'s success page; the default returns none;
    * `exchange(provider, pending, client, code, redirect_uri)`, when set, trades the code instead of the standard
      exchange, for a vendor whose answer is more than a grant (Slack's `oauth.v2.access` through its plan-gated
      egress seam, the team in `TokenGrant.extra`). Blocking; raises `oauth2_wire.OAuth2Error` on a refusal;
    * `app_available(provider)`, when set, says whether the deployment's app can take a Connect now (the catalog's
      `app_configured`, since a hooked provider's app is not the `app_setting` knob). Blocking; opens no secret it
      does not need. A hooked provider never takes a person's own app."""

    redirect_path: Callable[[OAuthProvider], str]
    cookie_path: str
    prepare: Callable[[PendingConnect, "AdminUser"], PendingConnect]
    client: Callable[[OAuthProvider, PendingConnect], ConnectClient]
    finish: Callable[[str, Optional[PendingConnect], Optional[str]], "RedirectResponse"]
    refusal: Callable[[OAuthProvider, str, str], str] = lambda provider, slug, text: slug
    after_grant: Callable[
        ["Session", "AdminUser", OAuthProvider, oauth2_wire.TokenGrant, PendingConnect, UUID],
        Optional["RedirectResponse"],
    ] = lambda *_args: None
    exchange: Optional[
        Callable[[OAuthProvider, PendingConnect, ConnectClient, str, str], oauth2_wire.TokenGrant]
    ] = None
    app_available: Optional[Callable[[OAuthProvider], bool]] = None


def hooks_for(provider: OAuthProvider) -> Optional[ConnectHooks]:
    """The provider's `ConnectHooks`, or None for one the engine runs alone."""
    if not provider.connect_hooks:
        return None
    module_name, _, attribute = provider.connect_hooks.partition(":")
    hooks = getattr(importlib.import_module(module_name), attribute, None)
    if not isinstance(hooks, ConnectHooks):
        raise RuntimeError(f"{provider.connect_hooks}: names no ConnectHooks")
    return hooks


def connected_row_test_result(credential_id: UUID) -> Any:
    """The new row's Test, on its own session (the tester dials out, so no route transaction is held): its
    `TestResult`, or None for a type with no tester."""
    from sqlalchemy import select

    from app.credentials.testers import TESTERS, new_deadline
    from app.db import BridgedSession
    from app.models.credentials import Credential

    with BridgedSession() as session:
        # A select, not `session.get`: the tenancy seam filters it (this thread carries the request's bound org).
        row = session.scalar(select(Credential).where(Credential.id == credential_id))
        # Only a catalog-owned Test runs unattended here: it dials the vendor's fixed host or a vetted tenant address.
        # A hand-written tester (a hook's row) is not vetted for tenant egress, so it waits for the person's Test.
        if row is None or row.nodevault or row.type not in TESTERS or catalog.spec_for_type(row.type) is None:
            return None
        tester = TESTERS[row.type]
        # Through the recording seam: the whole config is opened, so the read is on the record.
        config = data_access_events.opened_config_for(session, row, consumer="connect_test")
        session.commit()
    return tester(config, new_deadline())


def redirect_query(error: Optional[str], slug: str) -> Mapping[str, str]:
    """Where a pending attempt that failed sends the browser back: the vendor's Connect screen with the error."""
    return {"integration": slug, "connect_error": error or "expired"}
