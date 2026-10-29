"""The control-plane OAuth2 Connect flow — start and callback that store a refresh token onto an operator's own row."""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.actors import stamp_actor
from app.auth import require_user
from app.oauth_flow_cookie import FlowCookie
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import scoped_credential_select_for
from app.config import get_settings
from app.db import get_session
from app.entity_shape import is_blank
from app.credentials.registry import type_label
from app.credential_crypto import open_secret
from app import login_funnel, oauth2_wire, oauth_flow_cookie
from app.credentials.oauth_wizards import (
    ConnectProviderSpec,
    connect_for_type,
)
from app.credentials.testers import request_origin
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
# The operator saves their own OAuth client into a row, presses Connect and consents; the callback
# writes the refresh token onto that row (`credentials/oauth_wizards.py`). State is a signed cookie
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
    members=("state", "verifier", "user_id", "credential_id"),
)

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
})


class OAuthConnectStartOut(BaseModel):
    """What the console needs to send the browser to the vendor, and to name what it is doing.

    Labels are echoed for `OAuthStartOut`'s reason; for the generic type this is the only place a real
    vendor name exists (resolved from the authorize URL's host).
    """

    authorize_url: str
    provider_label: str
    action_label: str


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
        f"{get_settings().frontend_url.rstrip('/')}/credentials/{credential_id}?{urlencode(query)}"
    )
    return RedirectResponse(target, status_code=303)


def _connect_endpoints(
    provider: ConnectProviderSpec, config: Mapping[str, Any]
) -> tuple[str, str] | None:
    """`(authorize endpoint, token endpoint)` for one attempt, or None when the row cannot supply them.

    Provider first, row second, as `oauth2_tokens.token_endpoint` and `testers._refresh_exchange` do. The
    generic provider pins nothing, so the row's fields (https-validated at write time) are the endpoints.
    """
    authorize = provider.authorize_url or config.get("authorize_url")
    token = provider.token_url or config.get("token_url")
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


def _provider_label(provider: ConnectProviderSpec, authorize_endpoint: str) -> str:
    """What to call the vendor on the button and in the sentence above it: the provider's label, else
    the authorize URL's host.
    """
    if provider.provider_label:
        return provider.provider_label
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
def start_credential_connect(
    credential_id: UUID,
    request: Request,
    response: Response,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> OAuthConnectStartOut:
    """Begin a Connect attempt: mint the PKCE pair and state, and hand back the vendor's consent URL.

    The row must already exist: the client id and secret live in it (`refresh_token` is optional on both
    OAuth2 types for this reason). `_EDIT_KEY` through the scoped select, so only an editable row can be
    connected. `_connect_out` says why every org is served this flow.
    """
    row = _scoped_row_or_404(session, principal, credential_id, _EDIT_KEY)
    _validated_type(row.type)

    provider = connect_for_type(row.type)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail=f"credential type '{row.type[:64]}' has no OAuth2 connect flow - enter its fields instead",
        )
    config = dict(row.config or {})
    endpoints = _connect_endpoints(provider, config)
    if endpoints is None:
        # The field names are the instruction.
        raise HTTPException(
            status_code=400,
            detail="Save this credential's Authorize URL and Token URL first.",
        )
    authorize_endpoint, _token_endpoint = endpoints
    # Before the consent URL is published, so no one is refused at the end of a consent screen.
    _refuse_a_caller_supplied_endpoint_without_the_edit_key(provider, _token_endpoint, principal)
    # Client id and secret are both required to start, so no one consents only to fail at the exchange.
    if is_blank(config.get("client_id")) or is_blank(config.get("client_secret")):
        raise HTTPException(
            status_code=400,
            detail="Save this credential's client ID and secret before connecting.",
        )

    pkce = oauth2_wire.new_pkce_pair()
    state = oauth2_wire.new_state()
    scope = config.get("scope")
    url = oauth2_wire.authorize_url(
        endpoint=authorize_endpoint,
        client_id=str(config["client_id"]).strip(),
        redirect_uri=_connect_redirect_uri(),
        state=state,
        challenge=pkce.challenge,
        # The row's scope, else the provider default, else blank: an invented scope would grant the wrong
        # thing.
        scope=(str(scope).strip() if isinstance(scope, str) and scope.strip() else provider.default_scope),
        extra=provider.authorize_extra,
    )
    _CONNECT_FLOW.set(
        response,
        {
            "state": state,
            "verifier": pkce.verifier,
            "user_id": user.id,
            "credential_id": credential_id,
        },
    )
    # Re-mint the session cookie (`git_connect.git_connect_start` says why): an older `SameSite=Strict`
    # cookie would be withheld on the cross-site callback and 401.
    login_funnel.reissue_session(request, response, user)
    log.info("credential connect: user=%s started an attempt on credential=%s", user.id, credential_id)
    return OAuthConnectStartOut(
        authorize_url=url,
        provider_label=_provider_label(provider, authorize_endpoint),
        action_label=provider.action_label,
    )


@router.get("/api/credentials/connect/callback")
# The same declaration as the start leg, or a self-service connect would 403 after the code was spent.
# Narrowed by `_connect_row_or_none` (same scoped select, `_EDIT_KEY`), plus the cookie's `user_id` must
# match the signed-in user and its `credential_id` names the row.
@feature("credentials", "edit", own_credential=True)
def credential_connect_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Finish a Connect attempt: verify the round trip, spend the code, store the refresh token.

    Before the code is spent: the cookie is present, signed and unexpired (`FlowCookie.read`); its state
    equals the query's in constant time (login CSRF); its user id equals the signed-in user. Then the row
    is re-resolved through the scoped select.

    A missing refresh token is a failure (`no_refresh_token`): an unrenewable access token is not a
    stored credential.
    """
    # The checks are `oauth_flow_cookie.verified_attempt`'s (shared with `routes/git_connect.py`, which
    # documents their order); every slug it returns is in `_CONNECT_ERRORS`.
    flow, refusal = oauth_flow_cookie.verified_attempt(
        _CONNECT_FLOW, request,
        callback_state=state, user_id=user.id,
        log=log, flow_name="credential connect",
    )
    # The row is readable only once the cookie verified, so a refusal goes to the nil id.
    if refusal is not None:
        target = _no_credential_id()
        if flow is not None:
            target = _connect_credential_id(flow) or target
        return _finish_connect(_connect_redirect(target, error=refusal))
    assert flow is not None  # narrowing: `verified_attempt` returns exactly one of the two
    credential_id = _connect_credential_id(flow)
    if credential_id is None:
        # Signed, so only a build that changed the member's shape reaches this; treated as expired.
        log.warning("credential connect: user=%s cookie carried an unreadable credential id", user.id)
        return _finish_connect(_connect_redirect(_no_credential_id(), error="expired"))

    if error:
        # Checked here, not in the shared helper: a vendor `error` means the human declined, not that the
        # round trip is untrustworthy. Read, never echoed.
        log.info("credential connect: user=%s did not complete authorization", user.id)
        return _finish_connect(_connect_redirect(credential_id, error="denied"))
    if not code:
        # After the decline branch: a decline legitimately carries no code.
        return _finish_connect(_connect_redirect(credential_id, error="no_code"))

    row = _connect_row_or_none(session, principal, credential_id)
    if row is None:
        log.warning(
            "credential connect: user=%s finished an attempt on credential=%s, which is gone or "
            "no longer editable", user.id, credential_id,
        )
        return _finish_connect(_connect_redirect(credential_id, error="credential_gone"))

    provider = connect_for_type(row.type)
    config = dict(row.config or {})
    endpoints = _connect_endpoints(provider, config) if provider is not None else None
    if endpoints is None or is_blank(config.get("client_id")) or is_blank(config.get("client_secret")):
        # The row changed under the attempt (type, endpoints or client edited away).
        return _finish_connect(_connect_redirect(credential_id, error="bad_request"))
    authorize_endpoint, token_endpoint = endpoints
        # Checked again: the row's type may have changed since start, so the provider is re-derived now.
        # A flow slug rather than a 422/403, since the browser is mid-redirect.
    try:
        _refuse_a_caller_supplied_endpoint_without_the_edit_key(provider, token_endpoint, principal)
    except HTTPException:
        # The only trace on this leg; names neither arm (destination or key).
        log.warning(
            "credential connect: credential=%s resolved to a caller-supplied token endpoint the caller "
            "may not connect (a refused destination, or no edit key) - refused", credential_id,
        )
        return _finish_connect(_connect_redirect(credential_id, error="bad_request"))

    try:
        client_secret = open_secret(row, "client_secret")
    except (ValueError, KeyError):
        # The stored secret cannot be opened (re-pointed endpoint, another deployment's key, a legacy
        # shape): the row changed under the attempt, the same answer as a blank one. Never a 500
        # mid-redirect; the class is not logged either (it can name the field's state).
        log.warning(
            "credential connect: credential=%s holds a client secret this deployment cannot open",
            credential_id,
        )
        return _finish_connect(_connect_redirect(credential_id, error="bad_request"))

    try:
        grant = oauth2_wire.exchange_code(
            endpoint=token_endpoint,
            client_id=str(config["client_id"]).strip(),
            client_secret=client_secret,
            code=code,
            redirect_uri=_connect_redirect_uri(),
            verifier=flow["verifier"],
            vendor=_provider_label(provider, authorize_endpoint),
        )
    except oauth2_wire.OAuth2Error as exc:
        # `exc.code`, never the vendor's message; Test on the row gives the actionable version.
        log.warning(
            "credential connect: exchange refused for credential=%s (code=%s)", credential_id, exc.code
        )
        return _finish_connect(_connect_redirect(credential_id, error="exchange_failed"))

    if not grant.refresh_token:
        # See the docstring: an access token with no way to renew it is not a stored credential.
        log.warning(
            "credential connect: credential=%s exchanged but the vendor returned no refresh token",
            credential_id,
        )
        return _finish_connect(_connect_redirect(credential_id, error="no_refresh_token"))

    _store_connect_grant(session, user, row, grant)
    log.info("credential connect: credential=%s connected by user=%s", credential_id, user.id)
    return _finish_connect(_connect_redirect(credential_id))


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
    session: Session, user: AdminUser, row: Credential, grant: oauth2_wire.TokenGrant
) -> None:
    """Write a completed grant onto the row: the refresh token, the access token, its expiry.

    The refresh token is the point; the other two are a warm cache. The expiry is epoch milliseconds
    (`oauth2_tokens.py`). The dict is reassigned, not mutated: SQLAlchemy tracks JSONB by identity.
    """
    config = dict(row.config or {})
    config["refresh_token"] = grant.refresh_token
    config["access_token"] = grant.access_token
    if grant.expires_at:
        config["expiry_date"] = str(int(grant.expires_at.timestamp() * 1000))
    else:
        config.pop("expiry_date", None)
    row.config = config
    stamp_actor(row, user)
    # Through `_audit` before the commit, so an unwritable audit rolls the connect back; the payload
    # carries identity, never the tokens.
    _audit(session, user, "credential_connected", f"Connected {type_label(row.type)} credential {row.name}", row)
    with _scrubbed_write(session, "connect"):
        session.commit()
