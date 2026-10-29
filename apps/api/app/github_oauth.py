"""The GitHub OAuth adapter — GitHub's departures from plain OAuth2 on top of the generic wire in
`oauth2_wire.py`. Connect GitHub and GitHub sign-in authorize against ONE of two designated apps
(`git_access.app_config`): the GitHub App by default, whose user token (`ghu_`) reaches the
accounts it is installed on, or a classic OAuth App, whose token (`gho_`) reaches what its user reaches.

* `scope` is `SCOPE` on every authorize: `repo` and `workflow` for git, `read:org` for the orgs a new
  repository can be created in (`forge_repos.list_owners`; a grant made before it was asked for keeps working
  without it until the person reconnects), `read:user` and `user:email` for sign-in's identity read. A GitHub App ignores it (its declared permissions govern, "Email addresses: read"
  included). An org's "OAuth app access restrictions" or SAML SSO can still withhold an OAuth App token from
  an org's repositories, and a missing installation a GitHub App's (`routes/git_connect.py::git_connect_reach`
  names both).
* A classic OAuth App token does not expire and carries no refresh token; a GitHub App's expires (8h) with a
  rotating single-use refresh token when the App opts in. So `TokenGrant` fields are Optional, and
  `git_access.py` refreshes under a row lock.
* No `response_type` on authorize and no `grant_type` on exchange (see `oauth2_wire.authorize_url`).

PKCE (`S256`) is sent on every authorize even though optional. The public names are re-exported here
so callers (`git_connect.py`, `git_access.py`, `durable/git_token.py`) need not know the wire module.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app import github_api, http_bounded, oauth2_wire
from app.oauth2_wire import (  # re-exported: see this module's header
    OAuth2Error,
    PkcePair,
    TokenGrant,
    new_pkce_pair,
    new_state,
)

__all__ = [
    "AUTHORIZE_URL",
    "SCOPE",
    "TOKEN_URL",
    "REFRESH_TOKEN_DEAD",
    "GitHubOAuthError",
    "PkcePair",
    "TokenGrant",
    "authorize_url",
    "clean_login",
    "exchange_code",
    "fetch_login_identity",
    "is_app_issued",
    "new_pkce_pair",
    "new_state",
    "refresh",
    "revoke_user_token",
]

log = logging.getLogger("app.github_oauth")

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"

# What every authorize asks for (module header). Space-separated, GitHub's spelling.
SCOPE = "repo workflow read:org read:user user:email"

# The prefix of a GitHub App's user-to-server token (`is_app_issued`).
_APP_TOKEN_PREFIX = "ghu_"

# Generous (logins are ≤39 chars): it bounds third-party text, not GitHub's rules.
_LOGIN_MAX_LEN = 100

# Named in the "not JSON" message so whoever is looking at the failure has somewhere to look next.
_STATUS_PAGE = "https://www.githubstatus.com"


class GitHubOAuthError(OAuth2Error):
    """A connect or refresh attempt GitHub refused, carrying an operator-readable reason.

    A subclass so the wire can raise it directly (`error_cls`). `code` is GitHub's `error` member
    (`bad_verification_code`, `bad_refresh_token`, ...) or None for a transport failure.
    """


# The one refresh failure a retry can never fix: the user must authorize again, so `git_access` turns
# it into a reconnect prompt. GitHub's spelling, distinct from RFC 6749's `oauth2_wire.INVALID_GRANT`.
REFRESH_TOKEN_DEAD = "bad_refresh_token"


def is_app_issued(access_token: str) -> bool:
    """Whether `access_token` was minted by a GitHub App rather than an OAuth App
    (`git_access.token_app_kind`). Only the prefix is examined; nothing is logged."""
    return access_token.startswith(_APP_TOKEN_PREFIX)


def clean_login(raw: object) -> Optional[str]:
    """A forge account name (a GitHub login, any forge's `account_field`) bounded before it becomes a stored
    credential name, or None.

    The stored name bypasses `routes/credentials/credential_wire.py::_validated_name`, so this caps length and refuses (not
    strips) control characters; the caller's fallback name beats a mangled one."""
    if not isinstance(raw, str):
        return None
    login = raw.strip()
    if not login or len(login) > _LOGIN_MAX_LEN:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in login):
        return None
    return login


def authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    login: Optional[str] = None,
    prompt: Optional[str] = None,
) -> str:
    """The URL to send the user's browser to.

    `SCOPE` always, and no `response_type` (module header). `login` only pre-fills the account picker;
    `prompt="select_account"` forces it, so an already-authorized App is not silently re-approved for
    whichever account happens to be signed in.
    """
    extra = {key: value for key, value in (("login", login), ("prompt", prompt)) if value}
    return oauth2_wire.authorize_url(
        endpoint=AUTHORIZE_URL,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        challenge=challenge,
        scope=SCOPE,
        response_type=None,
        extra=extra or None,
    )


def exchange_code(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str
) -> TokenGrant:
    """Trade an authorization code for a user access token.

    `redirect_uri` must be byte-identical to the one sent to `authorize_url`, or GitHub answers
    `redirect_uri_mismatch` — it is a binding, not a destination, on this call.
    """
    return oauth2_wire.exchange_code(
        endpoint=TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
        vendor="GitHub",
        # GitHub's vendor type: without a recognised `Accept` the endpoint answers form-encoded.
        accept=github_api.GITHUB_ACCEPT,
        status_hint=_STATUS_PAGE,
        error_cls=GitHubOAuthError,
        send_grant_type=False,
    )


def refresh(*, client_id: str, client_secret: str, refresh_token: str) -> TokenGrant:
    """Trade a refresh token for a new access token AND A NEW REFRESH TOKEN.

    The returned `refresh_token` must be persisted: GitHub retires the one just spent, and dropping it
    surfaces hours later as `bad_refresh_token`.
    """
    return oauth2_wire.refresh(
        endpoint=TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        vendor="GitHub",
        accept=github_api.GITHUB_ACCEPT,
        status_hint=_STATUS_PAGE,
        error_cls=GitHubOAuthError,
    )


# ── Console sign-in (`app/login_oauth.py`): read who the token is, then keep or give the token back ─────

API_URL = "https://api.github.com"

# One budget per call; the user is waiting on a redirect.
_LOGIN_TIMEOUT_SECONDS = 15.0

# `/user` and `/user/emails` are a few KB; the cap bounds a hostile or broken answer.
_LOGIN_MAX_BYTES = 256 * 1024

# The seam tests replace (`http_bounded.client`'s redirect-off, capped-read policy).
_client = http_bounded.client


def _get_json(client: httpx.Client, path: str) -> tuple[int, object]:
    with client.stream("GET", f"{API_URL}{path}") as response:
        raw, truncated = http_bounded.read_capped(response, _LOGIN_MAX_BYTES)
        status = response.status_code
    return status, (None if truncated else http_bounded.json_or_none(raw))


def fetch_login_identity(access_token: str) -> tuple[int, list[tuple[str, bool]], Optional[str]]:
    """The account's stable numeric id, its VERIFIED emails as `(email_lower, primary)`, and its login
    (`clean_login`; None when unusable — it only names the connection a sign-in may keep).

    `/user` for `id` and `login`, `/user/emails` for the addresses (private ones included), keeping only
    `verified is True`. Never falls back to `/user`'s public `email` or an unverified address: a 403/404 on
    `/user/emails` means the grant lacks the `user:email` scope, and sign-in fails. Raises
    `GitHubOAuthError` on every failure; only a status or an exception class is ever logged.
    """
    try:
        with _client(_LOGIN_TIMEOUT_SECONDS, github_api.headers(access_token)) as client:
            status, user = _get_json(client, "/user")
            if status != 200 or not isinstance(user, dict):
                log.warning("github-login: /user answered HTTP %s", status)
                raise GitHubOAuthError("GitHub did not say who this account is.")
            user_id = user.get("id")
            if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
                log.warning("github-login: /user carried no numeric id")
                raise GitHubOAuthError("GitHub did not say who this account is.")
            status, emails = _get_json(client, "/user/emails")
    except httpx.HTTPError as exc:
        # The class only: `str(exc)` renders the request URL.
        log.warning("github-login: identity read failed (%s)", type(exc).__name__)
        raise GitHubOAuthError("Could not reach GitHub to read the account.") from exc
    if status in (403, 404):
        log.warning("github-login: /user/emails unavailable — the grant lacks the user:email scope")
        raise GitHubOAuthError("GitHub did not share this account's email addresses.")
    if status != 200 or not isinstance(emails, list):
        log.warning("github-login: /user/emails answered HTTP %s", status)
        raise GitHubOAuthError("GitHub did not share this account's email addresses.")
    verified: list[tuple[str, bool]] = []
    for entry in emails:
        if not isinstance(entry, dict) or entry.get("verified") is not True:
            continue
        email = entry.get("email")
        if isinstance(email, str) and email.strip():
            verified.append((email.strip().lower(), entry.get("primary") is True))
    return user_id, verified, clean_login(user.get("login"))


def revoke_user_token(*, client_id: str, client_secret: str, access_token: str) -> bool:
    """Revoke ONE user token (`DELETE /applications/{client_id}/token`), never the grant.

    A sign-in that does not keep its token as the user's connection (`routes/login_oauth.py`) gives it
    straight back, as does a Connect that refuses a token of the wrong kind. Retried once on a transport error
    or a 5xx. Returns whether GitHub confirmed (204); never raises, never logs the token. `/grant` is never
    called: it would also revoke every other token of the same App the user holds, their connection's
    included.
    """
    url = f"{API_URL}/applications/{client_id}/token"
    for attempt in (1, 2):
        try:
            with _client(
                _LOGIN_TIMEOUT_SECONDS, github_api.headers(None)
            ) as client:
                # Streamed and never read: only the status matters, and nothing unbounded is buffered.
                with client.stream(
                    "DELETE", url, json={"access_token": access_token}, auth=(client_id, client_secret)
                ) as response:
                    status = response.status_code
        except httpx.HTTPError as exc:
            log.warning("github-login: revoke attempt %s failed (%s)", attempt, type(exc).__name__)
            continue
        if status == 204:
            return True
        if status == 404:
            # Usually the token is already gone (the end state wanted), but a client mismatch answers 404
            # too, so it is logged apart from a confirmed revoke.
            log.warning("github-login: revoke answered HTTP 404 — treated as already revoked")
            return True
        log.warning("github-login: revoke attempt %s answered HTTP %s", attempt, status)
        if status < 500:
            return False
    return False
