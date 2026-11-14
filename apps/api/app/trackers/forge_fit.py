"""A Forge Invocation Token (FIT): how Jaah knows a call to the Jira ingress came from Jaah's own Jira Forge app.

Forge sends `Authorization: Bearer <FIT>` on every call the app makes to its remote (`invokeRemote`). The token is a
JWT Atlassian signs with a key from its JWKS; Jaah accepts it only when the signature checks against that set, `iss`
is `forge/invocation-token`, `aud` is this environment's app id (`Settings.jira_forge_app_id`, empty = refuse all), it
has not expired, and — when `Settings.jira_forge_environment_id` names one — it comes from that Forge environment.
Each token is accepted ONCE: its `jti` (when it has none, its signed header and payload — never the signature
segment, whose encoding can vary while it still verifies) is spent in the shared `rate_limit_hits` store for
`REPLAY_WINDOW_SECONDS`, and a second use is refused as a replay. A token living longer than that window less
`CLOCK_SKEW_SECONDS` is refused outright, so no record is pruned before its token expires; a store that cannot be
reached refuses the call (fails closed), since Forge mints a fresh token for its next one. Its
`context.cloudId` is the Jira site the call is about; `app.apiBaseUrl` is where the app's
system token (`x-forge-oauth-system`, a JWT of its own whose `exp` is read but never trusted for anything else) reaches
that site. Source: developer.atlassian.com/platform/forge/remote/essentials/.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from app.config import get_settings
from app.jwks import SigningKeys
from app.ratelimit import SharedRateLimiter
from app.trackers.protocol import header

log = logging.getLogger("tracker-forge-fit")

JWKS_URL = "https://forge.cdn.prod.atlassian-dev.net/.well-known/jwks.json"
ISSUER = "forge/invocation-token"
SYSTEM_TOKEN_HEADER = "x-forge-oauth-system"
# Where an app's system token may be sent: Atlassian's API gateway, never a host a token names.
API_GATEWAY_HOST = "api.atlassian.com"
_KEYS = SigningKeys(JWKS_URL, "Atlassian", "forge fit", ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512"), 256, log)

# How long a spent token is remembered: an hour, inside the day `rate_limit_hits` keeps a row. A FIT lives minutes.
REPLAY_WINDOW_SECONDS = 3600.0
# Margin for the app's and the database's clocks disagreeing about when that hour ends.
CLOCK_SKEW_SECONDS = 300.0
# One use per token, shared across replicas; never degraded to one process's memory.
_SPENT = SharedRateLimiter(limit=1, window=REPLAY_WINDOW_SECONDS, bucket="forge-fit-spent", fail_closed=True)


class FitRefused(Exception):
    """No token, or one that is unsigned by Atlassian, expired, for another app or naming no site."""


def bearer(headers: Mapping[str, str]) -> Optional[str]:
    scheme, _, token = (header(headers, "authorization") or "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def verified_claims(headers: Mapping[str, str]) -> dict[str, Any]:
    """The FIT's claims, checked as the module docstring says, or `FitRefused` — and the token SPENT: a second call
    with the same token, in this request or any other, is refused as a replay."""
    import jwt

    app_id = get_settings().jira_forge_app_id
    if not app_id:
        raise FitRefused("no Jira Forge app is configured here")
    token = bearer(headers)
    if token is None:
        raise FitRefused("no bearer token")
    try:
        key = _KEYS.key_for(token)
        claims = jwt.decode(token, key.key, algorithms=[key.algorithm_name], audience=app_id, issuer=ISSUER,
                            options={"require": ["exp", "iss", "aud"]})
    except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
        raise FitRefused(f"token rejected: {type(exc).__name__}") from exc
    if cloud_id(claims) is None:
        raise FitRefused("token names no site")
    environment = get_settings().jira_forge_environment_id
    if environment and not _same_environment(_app(claims).get("environment", {}).get("id"), environment):
        # One app id spans its Forge environments: a development install's token is not this control plane's.
        raise FitRefused("token is from another Forge environment")
    _spend(token, claims)
    return claims


def _spend(token: str, claims: Mapping[str, Any]) -> None:
    """Record this verified token's one use, or `FitRefused` when it was used before, lives too long to guard, or
    the store cannot record it."""
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp - time.time() > REPLAY_WINDOW_SECONDS - CLOCK_SKEW_SECONDS:
        raise FitRefused("token lives longer than its use is remembered")
    jti = claims.get("jti")
    seed = f"jti:{jti}" if isinstance(jti, str) and jti else f"signed:{token.rsplit('.', 1)[0]}"
    try:
        fresh = _SPENT.allow(hashlib.sha256(seed.encode()).hexdigest())
    except Exception as exc:  # noqa: BLE001 — unrecorded is unguarded: refuse, Forge's next call brings a new token
        log.warning("forge fit: the replay store is unavailable (%s); refused", type(exc).__name__)
        raise FitRefused("replay store unavailable") from exc
    if not fresh:
        log.warning("forge fit: a replayed token was refused (jti %s)", "present" if seed.startswith("jti:") else "absent")
        raise FitRefused("token replayed")


def unverified_claims(headers: Mapping[str, str]) -> dict[str, Any]:
    """The FIT's claims WITHOUT a signature check: only for naming the connection a request claims (the intake
    then verifies), or re-reading a token `verified_claims` already passed. {} when there is no readable token."""
    import jwt

    token = bearer(headers)
    if token is None:
        return {}
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return {}
    return claims if isinstance(claims, dict) else {}


def _same_environment(claimed: Any, configured: str) -> bool:
    """The FIT names its environment as an ARI (`ari:cloud:ecosystem::environment/<app uuid>/<environment id>`); the
    setting may hold that ARI or the bare id `forge environments list` shows."""
    return isinstance(claimed, str) and (claimed == configured or claimed.endswith(f"/{configured}"))


def _app(claims: Mapping[str, Any]) -> Mapping[str, Any]:
    value = claims.get("app")
    if not isinstance(value, Mapping):
        return {}
    environment = value.get("environment")
    return {**value, "environment": environment if isinstance(environment, Mapping) else {}}


def _context(claims: Mapping[str, Any]) -> Mapping[str, Any]:
    value = claims.get("context")
    return value if isinstance(value, Mapping) else {}


def cloud_id(claims: Mapping[str, Any]) -> Optional[str]:
    value = _context(claims).get("cloudId")
    return value if isinstance(value, str) and 0 < len(value) <= 255 else None


def site_host(claims: Mapping[str, Any]) -> Optional[str]:
    """The Jira site's host (`acme.atlassian.net`) from `context.siteUrl` — absent from a trigger's or scheduled
    trigger's token, which names only the cloud id."""
    value = _context(claims).get("siteUrl")
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    return parts.hostname if parts.scheme == "https" else None


def api_base(claims: Mapping[str, Any]) -> Optional[str]:
    """`app.apiBaseUrl` when it is on Atlassian's gateway (`https://api.atlassian.com/...`), else None."""
    app = claims.get("app")
    value = app.get("apiBaseUrl") if isinstance(app, Mapping) else None
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname != API_GATEWAY_HOST or parts.query or parts.fragment:
        return None
    return value.rstrip("/")


def system_token(headers: Mapping[str, str]) -> Optional[tuple[str, Optional[datetime]]]:
    """The app's system token Forge passed with this call and when it expires (its own `exp`), or None."""
    import jwt

    token = (header(headers, SYSTEM_TOKEN_HEADER) or "").strip()
    if not token or len(token) > 16_384:
        return None
    try:
        exp = jwt.decode(token, options={"verify_signature": False}).get("exp")
    except jwt.PyJWTError:
        exp = None
    expires = datetime.fromtimestamp(exp, tz=timezone.utc) if isinstance(exp, (int, float)) else None
    return token, expires
