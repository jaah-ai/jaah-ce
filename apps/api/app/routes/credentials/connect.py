"""The control-plane OAuth2 Connect flow — start and callback that store a refresh token onto an operator's own row."""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.actors import stamp_actor
from app.oauth_flow_cookie import FlowCookie
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.authz.visibility import scoped_credential_select_for
from app.config import console_url
from app import db_async
from app.db_async import DbDep
from app.entity_shape import is_blank, refused_as
from app.credentials.registry import CREDENTIAL_TYPES, type_label
from app.credential_crypto import open_secret
from app import login_funnel, mcp_oauth, oauth2_wire, oauth_flow_cookie, oauth_providers
from app.credentials.oauth_wizards import action_label, connect_for_type, provider_label
from app.oauth_providers import OAuthProvider
from app.credentials.testers import request_origin
from app.integrations import catalog
from app.integrations import connect as integration_connect
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.routes.credentials.guards import (
    _EDIT_KEY,
    _refuse_a_caller_supplied_endpoint_without_the_edit_key,
    _scoped_row_or_404,
)
from app.routes.credentials.credential_wire import (
    _audit,
    _connect_redirect_uri,
    _scrubbed_write,
    _validated_type,
)


router = APIRouter(tags=["credentials"])


# Log an outcome plus an id, never a code, verifier, secret, token or config.
log = logging.getLogger(__name__)


# ── The control-plane OAuth2 flow: Connect ────────────────────────────────────
# The operator saves their own OAuth client into a row (or the provider runs on the deployment's app, named by an
# operator knob: `OAuthProvider.platform_app`), presses Connect and consents; the callback writes the refresh
# token onto that row (`credentials/oauth_wizards.py`, providers in `app/oauth_providers.py`). State is a signed cookie
# (CSRF nonce, PKCE verifier) via `app/oauth_flow_cookie.py`, shared with `routes/git_connect.py`.
# Nothing logs, echoes or stores a code, verifier, secret or token; logs carry an id and a slug.

# The narrowest path both doors share, so a cookie carrying a PKCE verifier does not ride along on every
# request to this router (hence the start leg under `connect/` too).
_CONNECT_COOKIE_PATH = "/api/credentials/connect"

_CONNECT_FLOW = FlowCookie(
    suffix="_credential_connect",
    path=_CONNECT_COOKIE_PATH,
    # Its own salt, so a git-connect attempt cannot be replayed as a credential attempt.
    salt="credential-connect-flow",
    # The callback writes to the row the attempt started from; taking the id from the query instead
    # would let a callback aim a grant at a different credential.
    # `scope`: what a scope-catalogue provider was asked for, written onto the row at the callback (the vendor's
    # grant does not echo it). Optional, so an attempt started before it existed still reads.
    # `integration`/`pending`: a one-click Connect with no row yet (`routes/integrations.py`), which carries the
    # person's choices sealed (`app/integrations/connect.py::PendingConnect`) instead of a `credential_id`. Exactly one
    # of `credential_id` and `pending` is set; the callback refuses a cookie with both or neither.
    members=("state", "verifier", "user_id", "credential_id", "scope", "integration", "pending"),
    optional=("scope", "credential_id", "integration", "pending"),
)

# A hooked provider's attempt (`integrations.connect.ConnectHooks`) returns to the callback path its vendor app
# registered, so its cookie lives at that path instead: same members and checks, its own name and salt per path, so
# an attempt cookie never rides to, or is replayed on, another path's callback.
_HOOKED_FLOWS: dict[str, FlowCookie] = {}


def connect_flow(cookie_path: str = _CONNECT_COOKIE_PATH, slug: str = "") -> FlowCookie:
    """The one-click flow cookie scoped to `cookie_path`: `_CONNECT_FLOW` for the shared callback's own path, else
    one per provider `slug` at that path, so attempts on two providers sharing a path never overwrite each other."""
    if cookie_path == _CONNECT_COOKIE_PATH:
        return _CONNECT_FLOW
    key = f"{cookie_path}#{slug}"
    if key not in _HOOKED_FLOWS:
        tag = "_".join(part for part in f"{cookie_path}/{slug}".strip("/").replace("-", "_").split("/") if part)
        _HOOKED_FLOWS[key] = FlowCookie(
            suffix=f"_credential_connect_{tag}", path=cookie_path, salt=f"credential-connect-flow:{key}",
            members=_CONNECT_FLOW.members, optional=tuple(_CONNECT_FLOW.optional),
        )
    return _HOOKED_FLOWS[key]


# The closed vocabulary this flow may say to a browser, asserted by `_connect_redirect`: every value
# reaches a URL, so none may reflect a vendor's error text.
_CONNECT_ERRORS = oauth_flow_cookie.closed_errors({
    "expired": "the attempt had no valid cookie left - it was abandoned, or took over ten minutes",
    "state_mismatch": "the callback's state did not match the one this browser started with",
    "wrong_user": "the attempt was started by a different signed-in user",
    "denied": "authorization was not completed at the vendor",
    "no_code": "the vendor returned no authorization code",
    "exchange_failed": "the vendor refused to exchange the authorization code",
    "no_refresh_token": "the vendor returned no refresh token",
    "credential_gone": "the credential was deleted or is no longer editable",
    "bad_request": "this credential cannot be connected as it stands",
    "not_configured": "the deployment's OAuth app for this provider was unset or removed mid-attempt",
    "name_taken": "another credential in the same place took the chosen name during the attempt",
    "no_account": "the vendor named no organisation or site the sign-in reaches (`OAuthProvider.on_grant_config`)",
})


class OAuthConnectStartOut(BaseModel):
    """What the console needs to send the browser to the vendor, and to name what it is doing.

    Labels are echoed for `OAuthStartOut`'s reason; for the generic type this is the only place a real
    vendor name exists (resolved from the authorize URL's host).
    """

    authorize_url: str
    provider_label: str
    action_label: str


class OAuthConnectStartIn(BaseModel):
    """The start leg's optional body. `scopes`: the provider's OPTIONAL scopes the person keeps (each one's `name`
    from `oauth_connect.scopes`); absent keeps them all, and required ones are always asked for. Ignored by a
    provider with no scope catalogue."""

    scopes: Optional[list[str]] = Field(default=None, max_length=64)


def _connect_redirect(credential_id: UUID, *, error: Optional[str] = None) -> RedirectResponse:
    """A 303 back to the credential's own page, optionally carrying one slug from `_CONNECT_ERRORS`.

    303, not 302: the callback's effect is a write, so the follow must be a plain GET, and a reload must
    not resubmit the spent code. A redirect rather than a raise, because the reader is a human in a
    browser.
    """
    if error is not None and error not in _CONNECT_ERRORS:
        raise ValueError(f"{error!r} is not a member of the closed credential-connect error vocabulary")
    query = {"connect_error": error} if error else {"connected": "1"}
    target = (
        console_url(f"/credentials/{credential_id}?{urlencode(query)}")
    )
    return RedirectResponse(target, status_code=303)


def _connect_endpoints(
    provider: OAuthProvider, config: Mapping[str, Any]
) -> tuple[str, str] | None:
    """`(authorize endpoint, token endpoint)` for one attempt, or None when the row cannot supply them.

    Provider first, row second, as `oauth2_tokens.token_endpoint` and `testers._refresh_exchange` do. The
    generic provider pins nothing, so the row's fields (https-validated at write time) are the endpoints.
    """
    # An origin-scoped provider's pinned endpoints are filled from the row's vendor host (`endpoint_for`).
    authorize = oauth_providers.endpoint_for(provider, provider.authorize_url, config) if provider.authorize_url \
        else config.get("authorize_url")
    token = oauth_providers.endpoint_for(provider, provider.token_url, config) if provider.token_url \
        else config.get("token_url")
    if not isinstance(authorize, str) or is_blank(authorize):
        return None
    if not isinstance(token, str) or is_blank(token):
        return None
    authorize, token = authorize.strip(), token.strip()
    # Both must parse with the parser that will dial them: a value `urlsplit` accepts but httpx refuses
    # would raise an uncaught `httpx.InvalidURL`. Unreachable today (`require_https_base_url` on
    # `oauth2_client`), guarded for a future type. Not in `_validated_value`, which would change every
    # existing type's behaviour.
    if request_origin(authorize) is None or request_origin(token) is None:
        return None
    return authorize, token


def _provider_label(provider: OAuthProvider, authorize_endpoint: str) -> str:
    """What to call the vendor on the button and in the sentence above it: the provider's label, else
    the authorize URL's host.
    """
    label = provider_label(provider)
    if label:
        return label
    # Total over any string: a vendor name is never worth a 500.
    try:
        host = httpx.URL(authorize_endpoint).host
    except (httpx.InvalidURL, ValueError, UnicodeError):
        return "the vendor"
    return host or "the vendor"


@router.post("/api/credentials/connect/start/{credential_id}", response_model=OAuthConnectStartOut)
# `PATCH`'s own declaration: connecting edits this row. `own_credential=True` lets a keyless caller
# connect a credential they brought; narrowing is `_scoped_row_or_404` under `_EDIT_KEY`, as for PATCH.
@feature("credentials", "edit", own_credential=True)
async def start_credential_connect(
    credential_id: UUID,
    request: Request,
    response: Response,
    db: DbDep,
    body: Optional[OAuthConnectStartIn] = Body(default=None),
    caller: AsyncCaller = Depends(current_caller),
) -> OAuthConnectStartOut:
    """Begin a Connect attempt: mint the PKCE pair and state, and hand back the vendor's consent URL.

    The row must already exist: the client id and secret live in it (`refresh_token` is optional on every
    connect type for this reason), unless the provider runs on the deployment's own app (409 while an operator
    has not designated it). `_EDIT_KEY` through the scoped select, so only an editable row can be connected.
    `_connect_out` says why every org is served this flow.
    """
    provider, config, authorize_endpoint, token_endpoint = await db.run_sync(
        _start_credential_connect, credential_id, caller.principal
    )
    # The destination rule resolves the row's host (DNS) and the platform app opens its own session and secret:
    # both off the loop, with no transaction held.
    await db_async.release(db)
    extra: Optional[dict[str, str]] = None
    if provider.discovery is not None:
        # An MCP server's endpoints and client are derived from its URL now (RFC 9728 -> 8414, and RFC 7591 when
        # Jaah has no client there yet), never read off the row (`mcp_oauth` says why).
        grant = await run_in_threadpool(_start_discovered_grant, provider, config, caller.principal)
        authorize_endpoint, client_id, extra = grant.authorization_endpoint, grant.client_id, grant.extra
    else:
        client_id = await run_in_threadpool(_start_client_id, provider, config, token_endpoint, caller.principal)

    # A catalogue provider asks for its own scopes (required ones, plus the optional ones kept); any other asks
    # for the row's, else the provider's fixed one, else none: an invented scope would grant the wrong thing.
    stated = config.get("scope")
    with refused_as(400, oauth_providers.ScopeChoiceError):
        scope = oauth_providers.requested_scope(
            provider,
            chosen=body.scopes if body is not None else None,
            stated=stated if isinstance(stated, str) else None,
        )

    pkce = oauth2_wire.new_pkce_pair()
    state = oauth2_wire.new_state()
    url = oauth_providers.authorize_url(
        provider,
        endpoint=authorize_endpoint,
        client_id=client_id,
        redirect_uri=_connect_redirect_uri(),
        state=state,
        challenge=pkce.challenge if provider.pkce else "",
        scope=scope,
        extra=extra,
    )
    _CONNECT_FLOW.set(
        response,
        {
            "state": state,
            "verifier": pkce.verifier,
            "user_id": caller.actor.id,
            "credential_id": credential_id,
            "scope": (scope or "") if provider.scopes else "",
        },
    )
    # Re-mint the session cookie: an older `SameSite=Strict`
    # cookie would be withheld on the cross-site callback and 401.
    login_funnel.reissue_session(request, response, caller.actor)
    log.info("credential connect: user=%s started an attempt on credential=%s", caller.actor.id, credential_id)
    return OAuthConnectStartOut(
        authorize_url=url,
        provider_label=_provider_label(provider, authorize_endpoint),
        action_label=action_label(provider),
    )


@router.get("/api/credentials/connect/callback")
# The same declaration as the start leg, or a self-service connect would 403 after the code was spent.
# Narrowed by `_connect_row_or_none` (same scoped select, `_EDIT_KEY`), plus the cookie's `user_id` must
# match the signed-in user and its `credential_id` names the row.
@feature("credentials", "edit", own_credential=True)
async def credential_connect_callback(
    request: Request,
    db: DbDep,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    caller: AsyncCaller = Depends(current_caller),
) -> RedirectResponse:
    """Finish a Connect attempt: verify the round trip, spend the code, store the refresh token.

    Before the code is spent: the cookie is present, signed and unexpired (`FlowCookie.read`); its state
    equals the query's in constant time (login CSRF); its user id equals the signed-in user. Then the row
    is re-resolved through the scoped select.

    A missing refresh token is a failure (`no_refresh_token`): an unrenewable access token is not a
    stored credential. The one exception is a catalog type's grant whose access token never expires
    (`integrations/connect.py::grant_is_storable`, WordPress.com), the same rule its one-click Connect applies.
    """
    # The checks are `oauth_flow_cookie.verified_attempt`'s (shared with `routes/git_connect.py`, which
    # documents their order); every slug it returns is in `_CONNECT_ERRORS`.
    flow, refusal = oauth_flow_cookie.verified_attempt(
        _CONNECT_FLOW, request,
        callback_state=state, user_id=caller.actor.id,
        log=log, flow_name="credential connect",
    )
    # The row is readable only once the cookie verified, so a refusal goes to the nil id.
    if refusal is not None:
        integration = _unverified_integration(request)
        if integration:
            # A one-click attempt has no row to return to: back to its vendor's Connect screen.
            from app.routes.integrations import new_page

            return _finish_connect(new_page(integration, refusal))
        target = _no_credential_id()
        if flow is not None:
            target = _connect_credential_id(flow) or target
        return _finish_connect(_connect_redirect(target, error=refusal))
    assert flow is not None  # narrowing: `verified_attempt` returns exactly one of the two
    if bool(flow.get("pending")) == bool(flow.get("credential_id")):
        # Both or neither: no build mints such a cookie, so it is treated as expired.
        log.warning("credential connect: user=%s cookie named both or neither of a row and a pending attempt", caller.actor.id)
        return _finish_connect(_connect_redirect(_no_credential_id(), error="expired"))
    if flow.get("pending"):
        # A one-click Connect: the row is created now, by the integration engine.
        from app.routes.integrations import finish_pending_connect

        return _finish_connect(await finish_pending_connect(
            db, flow, code=code, error=error, user=caller.actor, principal=caller.principal,
        ))
    credential_id = _connect_credential_id(flow)
    if credential_id is None:
        # Signed, so only a build that changed the member's shape reaches this; treated as expired.
        log.warning("credential connect: user=%s cookie carried an unreadable credential id", caller.actor.id)
        return _finish_connect(_connect_redirect(_no_credential_id(), error="expired"))

    if error:
        # Checked here, not in the shared helper: a vendor `error` means the human declined, not that the
        # round trip is untrustworthy. Read, never echoed.
        log.info("credential connect: user=%s did not complete authorization", caller.actor.id)
        return _finish_connect(_connect_redirect(credential_id, error="denied"))
    if not code:
        # After the decline branch: a decline legitimately carries no code.
        return _finish_connect(_connect_redirect(credential_id, error="no_code"))

    resolved = await db.run_sync(_credential_connect_callback, caller.principal, credential_id)
    if resolved is None:
        log.warning(
            "credential connect: user=%s finished an attempt on credential=%s, which is gone or "
            "no longer editable", caller.actor.id, credential_id,
        )
        return _finish_connect(_connect_redirect(credential_id, error="credential_gone"))
    if isinstance(resolved, str):
        return _finish_connect(_connect_redirect(credential_id, error=resolved))
    row, provider, config, authorize_endpoint, token_endpoint = resolved
    # Everything up to the grant is outbound I/O (DNS, the platform app's own session, opening the secret, the
    # vendor's token and account endpoints): off the loop, with no transaction held. The row stays loaded
    # (`expire_on_commit=False`) for the write after.
    await db_async.release(db)
    refusal, grant, learned = await run_in_threadpool(
        _exchange_connect_grant, row, provider, config, authorize_endpoint, token_endpoint, caller.principal, code, flow,
        credential_id,
    )
    if refusal is not None:
        return _finish_connect(_connect_redirect(credential_id, error=refusal))
    assert grant is not None  # narrowing: `_exchange_connect_grant` returns a refusal or a grant
    await db.run_sync(_store_connect_grant, caller.actor, row, grant, learned)
    log.info("credential connect: credential=%s connected by user=%s", credential_id, caller.actor.id)
    return _finish_connect(_connect_redirect(credential_id))


def _credential_connect_callback(
    session: Session, principal: Principal | None, credential_id: UUID
) -> None | str | tuple[Credential, OAuthProvider, dict[str, Any], str, str]:
    """The callback's row: None when gone or no longer editable, a refusal slug when it changed under the
    attempt, else `(row, provider, config, authorize endpoint, token endpoint)`."""
    row = _connect_row_or_none(session, principal, credential_id)
    if row is None:
        return None

    provider = connect_for_type(row.type)
    config = dict(row.config or {})
    if provider is not None and provider.discovery is not None:
        # Derived again at the exchange (`_exchange_connect_grant`), never read off the row.
        return row, provider, catalog.mcp_config(row.type, config), "", ""
    endpoints = _connect_endpoints(provider, config) if provider is not None else None
    if endpoints is None or provider is None or (
        not oauth_providers.uses_platform_client(provider, config)
        and oauth_providers.row_client_missing(provider, config)
    ):
        # The row changed under the attempt (type, endpoints or client edited away).
        return "bad_request"
    authorize_endpoint, token_endpoint = endpoints
    return row, provider, config, authorize_endpoint, token_endpoint


def _exchange_connect_grant(
    row: Credential, provider: OAuthProvider, config: Mapping[str, Any], authorize_endpoint: str,
    token_endpoint: str, principal: Principal | None, code: str, flow: Mapping[str, str], credential_id: UUID,
) -> tuple[Optional[str], Optional[oauth2_wire.TokenGrant], dict[str, str]]:
    """Spend the code: `(refusal slug, None, {})`, or `(None, grant, learned)` with what the attempt learned.
    Blocking (DNS, KMS, the vendor), so on a worker thread; touches no route session."""
    extra: Optional[dict[str, str]] = None
    basic_auth: Optional[bool] = None
    discovered: Optional[mcp_oauth.Grant] = None
    if provider.discovery is not None:
        # The endpoints and client derived from the row's URL now, as at start (`mcp_oauth.grant_for`).
        try:
            discovered = mcp_oauth.grant_for(config)
        except mcp_oauth.McpOAuthError:
            log.warning("credential connect: credential=%s could not rediscover its MCP authorization server", credential_id)
            return "bad_request", None, {}
        authorize_endpoint, token_endpoint = discovered.authorization_endpoint, discovered.token_endpoint
        extra, basic_auth = discovered.extra, discovered.basic_auth
    # Checked again: the row's type may have changed since start, so the provider is re-derived now.
    # A flow slug rather than a 422/403, since the browser is mid-redirect.
    try:
        _refuse_a_caller_supplied_endpoint_without_the_edit_key(provider, token_endpoint, principal, config)
    except HTTPException:
        # The only trace on this leg; names neither arm (destination or key).
        log.warning(
            "credential connect: credential=%s resolved to a caller-supplied token endpoint the caller "
            "may not connect (a refused destination, or no edit key) - refused", credential_id,
        )
        return "bad_request", None, {}

    if discovered is not None and discovered.registered:
        # Jaah's registration, held on the operator plane alone.
        client_id, client_secret = discovered.client_id, discovered.client_secret
    elif oauth_providers.uses_platform_client(provider, config):
        platform = oauth_providers.platform_client(provider, consumer="credential_connect")
        if platform is None:
            # The operator unset or re-pointed the app between start and callback.
            log.warning("credential connect: credential=%s finished after its provider's app was unset", credential_id)
            return "not_configured", None, {}
        client_id, client_secret = platform
    else:
        client_id = str(config["client_id"]).strip()
        try:
            # A public client (`OAuthProvider.public_client`) may hold no secret at all: none is sent.
            client_secret = (open_secret(row, "client_secret")
                             if not (provider.public_client and is_blank(config.get("client_secret"))) else "")
        except (ValueError, KeyError):
            # The stored secret cannot be opened (re-pointed endpoint, another deployment's key, a legacy
            # shape): the row changed under the attempt, the same answer as a blank one. Never a 500
            # mid-redirect; the class is not logged either (it can name the field's state).
            log.warning(
                "credential connect: credential=%s holds a client secret this deployment cannot open",
                credential_id,
            )
            return "bad_request", None, {}

    try:
        grant = oauth_providers.exchange_code(
            provider,
            endpoint=token_endpoint,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=_connect_redirect_uri(),
            # A provider without PKCE was sent no challenge, so it is sent no verifier either.
            verifier=flow["verifier"] if provider.pkce else "",
            vendor=_provider_label(provider, authorize_endpoint),
            extra=extra,
            basic_auth=basic_auth,
        )
    except oauth2_wire.OAuth2Error as exc:
        # `exc.code`, never the vendor's message; Test on the row gives the actionable version.
        log.warning(
            "credential connect: exchange refused for credential=%s (code=%s)", credential_id, exc.code
        )
        return "exchange_failed", None, {}

    if not grant.refresh_token and not (
        catalog.spec_for_type(row.type) is not None and integration_connect.grant_is_storable(grant, provider)
    ):
        # See the docstring: an access token with no way to renew it is not a stored credential, unless the
        # provider never issues one (`OAuthProvider.expiring_grant_ok`: reconnecting is the renewal).
        log.warning(
            "credential connect: credential=%s exchanged but the vendor returned no refresh token",
            credential_id,
        )
        return "no_refresh_token", None, {}

    learned: dict[str, str] = {}
    declared = {field.key for field in CREDENTIAL_TYPES[row.type].fields}
    try:
        # What the grant reaches (a Xero organisation, an Atlassian site). A url field the row already names keeps
        # its value: the row's secrets are sealed to that destination, so a Connect never moves it.
        url_fields = {field.key for field in CREDENTIAL_TYPES[row.type].fields if field.kind == "url"}
        learned.update({key: value for key, value in integration_connect.learned_from_grant(provider, grant, config).items()
                        if key in declared and not (key in url_fields and not is_blank(config.get(key)))})
    except integration_connect.GrantRefused as refusal:
        log.warning("credential connect: credential=%s grant reaches nothing a row could use", credential_id)
        return refusal.slug, None, {}
    if provider.scopes and flow.get("scope") and "scope" in declared:
        learned["scope"] = flow["scope"]
    if "account" in declared:
        # Best effort (`fetch_account`): a failed read costs only the name.
        account = oauth_providers.account_of(provider, grant.access_token, config)
        if account and "account" not in learned:
            learned["account"] = account
    return None, grant, learned


def _start_credential_connect(
    session: Session, credential_id: UUID, principal: Principal | None
) -> tuple[OAuthProvider, dict[str, Any], str, str]:
    """The start leg's row: `(provider, config, authorize endpoint, token endpoint)`, or its 404/400 refusal."""
    row = _scoped_row_or_404(session, principal, credential_id, _EDIT_KEY)
    _validated_type(row.type)
    if row.nodevault:
        # The control plane stores and refreshes a grant, which a NodeVault row never lets it open (D7). Reachable
        # only on a type that is NodeVault-eligible for its key and also signs in (Granola's MCP server).
        raise HTTPException(status_code=409, detail="A NodeVault secret can't be connected - add a new secret to sign in.")

    provider = connect_for_type(row.type)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail=f"credential type '{row.type[:64]}' has no OAuth2 connect flow - enter its fields instead",
        )
    config = dict(row.config or {})
    if provider.discovery is not None:
        # Derived at start from the server URL (`mcp_oauth.grant_for`); only the URL must be saved, which its type
        # requires - a catalog vendor's is its spec's (`catalog.mcp_config`).
        return provider, catalog.mcp_config(row.type, config), "", ""
    endpoints = _connect_endpoints(provider, config)
    if endpoints is None:
        # The field names are the instruction: an origin-scoped vendor's one field, else the generic pair.
        if provider.origin_field is not None:
            hosts = ", ".join(provider.origin_hosts)
            raise HTTPException(status_code=400, detail=f"Save this credential's {_field_label(row.type, provider.origin_field)} "
                                                        f"first: an address ending in {hosts}.")
        raise HTTPException(status_code=400, detail="Save this credential's Authorize URL and Token URL first.")
    authorize_endpoint, token_endpoint = endpoints
    return provider, config, authorize_endpoint, token_endpoint


def _field_label(type_slug: str, key: str) -> str:
    """`key`'s label on `type_slug` (its key when the type names none)."""
    return next((item.label for item in CREDENTIAL_TYPES[type_slug].fields if item.key == key), key)


def _start_client_id(
    provider: OAuthProvider, config: Mapping[str, Any], token_endpoint: str, principal: Principal | None
) -> str:
    """The client id the consent URL names, or the start leg's refusal. Blocking (DNS, the platform app's own
    session), so on a worker thread; touches no route session."""
    # Before the consent URL is published, so no one is refused at the end of a consent screen.
    _refuse_a_caller_supplied_endpoint_without_the_edit_key(provider, token_endpoint, principal, config)
    if oauth_providers.uses_platform_client(provider, config):
        platform = oauth_providers.platform_client(provider, consumer="credential_connect")
        if platform is None:
            raise HTTPException(status_code=409, detail=f"{provider.label} connect isn't set up. Ask an operator.")
        return platform[0]
    if oauth_providers.row_client_missing(provider, config):
        # Client id and secret are both required to start, so no one consents only to fail at the exchange.
        raise HTTPException(status_code=400, detail="Save this credential's client ID and secret before connecting.")
    return str(config["client_id"]).strip()


def _start_discovered_grant(
    provider: OAuthProvider, config: Mapping[str, Any], principal: Principal | None
) -> mcp_oauth.Grant:
    """The start leg's MCP grant (`mcp_oauth.grant_for`, registering Jaah when it has no client there), or its
    refusal. Discovery makes this deployment dial the row's host, so the caller-chosen-endpoint rule runs on the
    server URL first and on the discovered token endpoint after. Blocking, so on a worker thread."""
    _refuse_a_caller_supplied_endpoint_without_the_edit_key(provider, str(config.get("url") or ""), principal, config)
    with refused_as(422, mcp_oauth.McpOAuthError, prefix="Connect could not start: "):
        grant = mcp_oauth.grant_for(config, redirect_uri=_connect_redirect_uri(), register_missing=True)
    _refuse_a_caller_supplied_endpoint_without_the_edit_key(provider, grant.token_endpoint, principal, config)
    return grant


def _finish_connect(response: RedirectResponse) -> RedirectResponse:
    """Retire the attempt cookie on the way out, on every path — success and every refusal alike, so no
    branch leaves a single-use secret in the browser.
    """
    _CONNECT_FLOW.clear(response)
    return response


def _connect_credential_id(flow: Mapping[str, str]) -> Optional[UUID]:
    """The credential the attempt named, or None when the cookie's member is not a UUID.

    One function for both readers, whose only answer to "not a UUID" is "treat as expired".
    """
    try:
        return UUID(flow["credential_id"])
    except (KeyError, ValueError):
        return None


def _unverified_integration(request: Request) -> str:
    """The catalog slug a refused attempt's cookie names, for the redirect only: read from the still-signed cookie
    (`FlowCookie.read`), never trusted for anything but choosing which Connect screen to show. Blank when none."""
    attempt = _CONNECT_FLOW.read(request)
    slug = (attempt or {}).get("integration") or ""
    from app.integrations import catalog

    return slug if catalog.spec_for(slug) is not None else ""


def _no_credential_id() -> UUID:
    """The row to send a cookie-less callback back to, of which there is none.

    The nil UUID 404s into the page's own not-found state; a plausible id could land on someone else's
    credential.
    """
    return UUID(int=0)


def _connect_row_or_none(
    session: Session, principal: Principal | None, credential_id: UUID
) -> Optional[Credential]:
    """The row this attempt may write, or None — the callback's `_scoped_row_or_404`.

    Returns rather than raises (the caller redirects). Same select and edit key, so deletion and a
    revoked grant are one answer.
    """
    return session.scalars(
        scoped_credential_select_for(principal, _EDIT_KEY).where(Credential.id == credential_id)
    ).first()


def _store_connect_grant(
    session: Session, user: AdminUser, row: Credential, grant: oauth2_wire.TokenGrant,
    learned: Mapping[str, str],
) -> None:
    """Write a completed grant onto the row: the refresh token, the access token, its expiry, and what the
    attempt `learned` (the scope asked for, the account that consented; plain fields the type declares).

    The refresh token is the point; the access token and expiry are a warm cache. The expiry is epoch
    milliseconds (`oauth2_tokens.py`). The dict is reassigned, not mutated: SQLAlchemy tracks JSONB by identity.
    """
    config = dict(row.config or {})
    config.update(learned)
    if grant.refresh_token:
        config["refresh_token"] = grant.refresh_token
    else:
        # A never-expiring or `expiring_grant_ok` catalog grant: no stale refresh token stays.
        config.pop("refresh_token", None)
    config["access_token"] = grant.access_token
    if grant.expires_at:
        config["expiry_date"] = str(int(grant.expires_at.timestamp() * 1000))
    else:
        config.pop("expiry_date", None)
    row.config = config
    # A fresh grant revives a row a refused refresh or a revocation marked (`oauth2_tokens._mark_needs_reconnect`).
    row.needs_reconnect_at = None
    row.needs_reconnect_reason = None
    stamp_actor(row, user)
    # Through `_audit` before the commit, so an unwritable audit rolls the connect back; the payload
    # carries identity, never the tokens.
    _audit(session, user, "credential_connected", f"Connected {type_label(row.type)} credential {row.name}", row)
    with _scrubbed_write(session, "connect"):
        session.commit()
