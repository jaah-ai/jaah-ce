"""The OAuth2 authorization-code wire — PKCE, the authorize URL, the code exchange, the refresh.

The wire only: RFC 6749/7636 against an endpoint it is handed, with no database and no vendor
knowledge, so it is testable against recorded responses. Callers: `github_oauth.py` (GitHub App
user-to-server; no `scope`, rotating single-use refresh tokens, opt-in expiry) and
`routes/credentials/` (the console's Connect button, against the operator's own client).

Nothing here holds a client id or secret; both arrive per call (`credentials/oauth_wizards.py` owns
the no-OAuth-client-of-our-own rule). `client_secret` goes in the POST body, never a URL, and nothing
here logs a token, code, verifier or secret — only outcomes and provider error codes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

import httpx

from app import http_bounded

log = logging.getLogger("oauth2-wire")

# Per-operation httpx timeout; a user is waiting on a redirect, so fail fast.
TIMEOUT_SECONDS = 15.0

# The wall-clock bound `TIMEOUT_SECONDS` is not: httpx's read timeout resets per read, so a dripping
# host is never timed out. Checked in the read loop; required because the endpoint may be an
# operator-chosen `token_url` reached by a runner (`app/oauth2_tokens.py`).
_DEADLINE_SECONDS = 20.0

# The body bound (`app/http_bounded.py`); a token response is a handful of JSON members.
_MAX_RESPONSE_BYTES = 64 * 1024

# A body that filled the cap; fails closed rather than parsing a prefix.
_TRUNCATED = object()

# `S256` only; `plain` would only weaken the flow.
_PKCE_METHOD = "S256"


class OAuth2Error(RuntimeError):
    """An attempt the provider refused, carrying an operator-readable reason.

    `code` is the provider's `error` member (`invalid_grant`, ...) or None for a transport failure.
    Callers branch on `code`, never on the message text.
    """

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


# RFC 6749 §5.2's "connection is dead, authorize again" — the one refresh failure a retry never fixes,
# so callers prompt a reconnect. GitHub's own spelling is `github_oauth.REFRESH_TOKEN_DEAD`.
INVALID_GRANT = "invalid_grant"


@dataclass(frozen=True)
class TokenGrant:
    """What the provider handed back — the same shape from an initial exchange and from a refresh.

    `refresh_token` is often absent (most refreshes omit it; exchanges without offline access too);
    keep the one you hold when none comes back. `expires_at` is absolute and tz-aware, since a
    relative lifetime is useless once stored.
    """

    access_token: str
    refresh_token: Optional[str]
    expires_at: Optional[datetime]
    # The OIDC `id_token`, when the provider sends one (Microsoft sign-in reads it); None for GitHub.
    id_token: Optional[str] = None


@dataclass(frozen=True)
class PkcePair:
    """A PKCE verifier and the challenge derived from it.

    The verifier is secret until the code exchange; only the challenge appears in the authorize URL.
    """

    verifier: str
    challenge: str


def new_pkce_pair() -> PkcePair:
    """A fresh PKCE pair: 32 `secrets` bytes, base64url unpadded (inside RFC 7636's 43..128 chars)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return PkcePair(verifier=verifier, challenge=challenge)


def new_state() -> str:
    """A fresh, unpredictable CSRF state value, compared for equality on the callback."""
    return secrets.token_urlsafe(32)


def authorize_url(
    *,
    endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    scope: Optional[str] = None,
    extra: Optional[Mapping[str, str]] = None,
    response_type: Optional[str] = "code",
) -> str:
    """The URL to send the user's browser to.

    `scope` is optional (Bitbucket Cloud's consumer carries its own); blank means absent. `extra` carries per-provider
    constants outside RFC 6749 (Google's `access_type=offline`, `prompt=consent`, needed for a refresh
    token); never operator input, and it cannot override the fixed parameters, which are applied last.
    `response_type=None` suppresses the parameter for the GitHub flow, which has always omitted it;
    keep the escape hatch so its wire bytes do not change. Every PKCE-capable caller passes a
    `challenge`; blank omits both PKCE parameters.
    """
    params: dict[str, str] = dict(extra or {})
    params.update({"client_id": client_id, "redirect_uri": redirect_uri, "state": state})
    # A blank `challenge` sends no PKCE pair, for a provider without it (Bitbucket Cloud).
    if challenge:
        params.update({"code_challenge": challenge, "code_challenge_method": _PKCE_METHOD})
    if response_type:
        params["response_type"] = response_type
    if scope:
        params["scope"] = scope
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(params)}"


def exchange_code(
    *,
    endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    verifier: str,
    vendor: str,
    accept: str = "application/json",
    status_hint: Optional[str] = None,
    error_cls: type[OAuth2Error] = OAuth2Error,
    send_grant_type: bool = True,
    basic_auth: bool = False,
) -> TokenGrant:
    """Trade an authorization code for an access token.

    `redirect_uri` must be byte-identical to the one sent to `authorize_url`, or the provider answers
    `redirect_uri_mismatch` — it is a binding on this call, not a destination.

    `send_grant_type=False` keeps the GitHub exchange's bytes unchanged (it never sent it). A blank
    `verifier` sends no `code_verifier` (a provider without PKCE). `basic_auth` moves the client pair
    from the body to an HTTP Basic header, for a provider that documents only that (Bitbucket Cloud).
    """
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if verifier:
        form["code_verifier"] = verifier
    if send_grant_type:
        form["grant_type"] = "authorization_code"
    return _token_request(
        endpoint,
        form,
        what="exchange",
        vendor=vendor,
        accept=accept,
        status_hint=status_hint,
        error_cls=error_cls,
        basic_auth=basic_auth,
    )


def refresh(
    *,
    endpoint: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    scope: Optional[str] = None,
    vendor: str,
    accept: str = "application/json",
    status_hint: Optional[str] = None,
    error_cls: type[OAuth2Error] = OAuth2Error,
    basic_auth: bool = False,
) -> TokenGrant:
    """Trade a refresh token for a new access token.

    If the response carries a refresh token, store it (GitHub rotates and retires the spent one);
    otherwise keep the one you sent. `basic_auth` as for `exchange_code`.
    """
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if scope:
        form["scope"] = scope
    return _token_request(
        endpoint,
        form,
        what="refresh",
        vendor=vendor,
        accept=accept,
        status_hint=status_hint,
        error_cls=error_cls,
        basic_auth=basic_auth,
    )


def _token_request(
    endpoint: str,
    form: dict[str, str],
    *,
    what: str,
    vendor: str,
    accept: str,
    status_hint: Optional[str],
    error_cls: type[OAuth2Error],
    basic_auth: bool = False,
) -> TokenGrant:
    """POST to the token endpoint and parse the one response shape both callers share.

    A refusal can arrive as HTTP 200 (GitHub: 200 with `error` and no `access_token`), so the
    discriminator is `error`/`access_token`, not the status.
    """
    auth: Optional[tuple[str, str]] = None
    if basic_auth:
        auth = (form.pop("client_id"), form.pop("client_secret"))
    try:
        # `auth` only when set, so the no-Basic call keeps its three-argument shape.
        status_code, body = _post_bounded(endpoint, form, accept, **({"auth": auth} if auth else {}))
    except httpx.HTTPError as exc:
        # Class name only: httpx's message renders the full request URL.
        log.warning("oauth2 %s: transport failure (%s)", what, type(exc).__name__)
        raise error_cls(
            f"Could not reach {vendor} to {what} a token ({type(exc).__name__}). This is usually "
            "transient - try again."
        ) from exc

    payload = _json_object(
        body, status_code, what=what, vendor=vendor, status_hint=status_hint, error_cls=error_cls
    )

    error = payload.get("error")
    if isinstance(error, str) and error:
        description = payload.get("error_description")
        detail = description if isinstance(description, str) and description else error
        log.warning("oauth2 %s: refused code=%s", what, error)
        raise error_cls(f"{vendor} refused the {what}: {detail}", code=error)

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        # Neither `access_token` nor `error`: fail now rather than store an empty credential.
        log.warning("oauth2 %s: no access_token in a %s response", what, status_code)
        raise error_cls(
            f"{vendor}'s {what} response carried no access token (HTTP {status_code})."
        )

    return TokenGrant(
        access_token=access_token,
        refresh_token=optional_str(payload.get("refresh_token")),
        expires_at=expires_at(payload.get("expires_in")),
        id_token=optional_str(payload.get("id_token")),
    )


def _post_bounded(
    endpoint: str, form: dict[str, str], accept: str, *, auth: Optional[tuple[str, str]] = None
) -> tuple[int, Any]:
    """POST `form` and read at most `_MAX_RESPONSE_BYTES`, inside `_DEADLINE_SECONDS` of wall clock.

    Returns `(status_code, body)`: `bytes`, or `_TRUNCATED` when the cap or deadline stopped the read.
    The client is built here (the tests' seam) with redirects off: following a 302 would carry the
    client secret to an unvalidated host.
    """
    deadline = monotonic() + _DEADLINE_SECONDS
    with httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, headers={"Accept-Encoding": "identity"}
    ) as client:
        with client.stream(
            "POST", endpoint, data=form, headers={"Accept": accept}, **({"auth": auth} if auth else {})
        ) as response:
            # One byte past the cap, so a body of exactly `_MAX_RESPONSE_BYTES` is still read whole.
            body, truncated = http_bounded.read_capped(
                response, _MAX_RESPONSE_BYTES + 1, lambda: monotonic() >= deadline
            )
            return response.status_code, _TRUNCATED if truncated else body


def _json_object(
    body: Any,
    status_code: int,
    *,
    what: str,
    vendor: str,
    status_hint: Optional[str],
    error_cls: type[OAuth2Error],
) -> dict[str, Any]:
    """`body` as a JSON object, or an `OAuth2Error` naming the status.

    A non-object body (e.g. a proxy's HTML 200) is a refusal, not a crash. So is a truncated body: a
    prefix that happens to parse would be believed.
    """
    hint = f" If this persists, check {status_hint}." if status_hint else ""
    if body is _TRUNCATED:
        raise error_cls(
            f"{vendor}'s {what} response was too large or too slow to read (HTTP {status_code}).{hint}"
        )
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise error_cls(
            f"{vendor}'s {what} response was not JSON (HTTP {status_code}).{hint}"
        ) from exc
    if not isinstance(payload, dict):
        raise error_cls(
            f"{vendor}'s {what} response was not a JSON object (HTTP {status_code}).{hint}"
        )
    return payload


def optional_str(value: Any) -> Optional[str]:
    """A non-empty string, or None; empty and absent mean the same thing."""
    return value if isinstance(value, str) and value else None


def expires_at(expires_in: Any) -> Optional[datetime]:
    """`expires_in` seconds turned into an absolute UTC instant, or None when none was sent.

    A non-numeric or non-positive value reads as "no expiry", failing toward a handled 401 rather than
    a refresh loop.
    """
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        return None
    if expires_in <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
