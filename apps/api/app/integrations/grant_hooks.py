"""The `[auth.oauth] on_grant_config = "module:function"` hooks the vendor specs name (`OAuthProvider.on_grant_config`):
what a fresh grant reaches that the token answer does not say, read from the vendor with the new access token and
written onto the row beside it (`integrations/connect.py::learned_from_grant`).

Each is called as `(provider, grant, config) -> {field: value}`, blocking, after the code is spent; `config` is the
row the grant lands on (empty for a new row, a saved row's own on a row-bound Connect). One that finds nothing a
row could use raises `GrantRefused("no_account")`, and nothing is stored. Only an exception class or a status is
logged; never a token.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Mapping, Optional

import httpx

from app import http_bounded
from app.integrations.connect import GrantRefused
from app.oauth2_wire import TokenGrant
from app.oauth_providers import OAuthProvider

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15.0
_MAX_BYTES = 256 * 1024

#: The seam tests replace (`http_bounded.client`'s redirect-off, capped-read policy).
_client = http_bounded.client

#: Xero's list of the organisations a grant reaches (developer.xero.com/documentation/guides/oauth2/tenants).
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
#: Atlassian's list of the sites a grant reaches; each `id` is the cloud ID every API call is addressed by
#: (developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/).
ATLASSIAN_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
#: PostHog's cloud regions, tried in this order: the OAuth server is shared, the private API is per region.
POSTHOG_REGIONS = ("https://us.posthog.com", "https://eu.posthog.com")


def _get_json(provider: OAuthProvider, url: str, access_token: str) -> tuple[int, Any]:
    """`(status, parsed JSON or None)` of a GET with the grant's token; `(0, None)` when the vendor is unreachable."""
    try:
        with _client(_TIMEOUT_SECONDS, {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}) as client:
            with client.stream("GET", url) as response:
                raw, truncated = http_bounded.read_capped(response, _MAX_BYTES)
                status = response.status_code
    except httpx.HTTPError as exc:
        # The class only: `str(exc)` renders the request URL.
        log.warning("oauth connect: %s grant read failed (%s)", provider.key, type(exc).__name__)
        return 0, None
    return status, None if truncated else http_bounded.json_or_none(raw)


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _jwt_claim(token: str, claim: str) -> Optional[str]:
    """One claim of a JWT access token's payload, unverified: the token came from the vendor's token endpoint over
    TLS a moment ago, and the claim only picks among the vendor's own answers (never authorizes anything)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return None
    return _text(payload.get(claim)) if isinstance(payload, dict) else None


def xero_tenant(provider: OAuthProvider, grant: TokenGrant, config: Mapping[str, Any]) -> dict[str, str]:
    """The Xero organisation this consent chose: of `/connections`, the ones whose `authEventId` is the token's
    `authentication_event_id` (this sign-in; none matching is refused, never another organisation), else - a token
    naming no event - every one; newest first, organisations before practices."""
    status, connections = _get_json(provider, XERO_CONNECTIONS_URL, grant.access_token)
    if status != 200 or not isinstance(connections, list):
        log.warning("oauth connect: xero connections read answered HTTP %s", status)
        raise GrantRefused("no_account")
    tenants = [item for item in connections if isinstance(item, dict) and _text(item.get("tenantId"))]
    event = _jwt_claim(grant.access_token, "authentication_event_id")
    chosen = [item for item in tenants if item.get("authEventId") == event] if event else tenants
    # Newest first within each kind: a stable sort on the date descending, then on the kind.
    chosen.sort(key=lambda item: str(item.get("createdDateUtc") or ""), reverse=True)
    chosen.sort(key=lambda item: item.get("tenantType") != "ORGANISATION")
    if not chosen:
        raise GrantRefused("no_account")
    tenant = chosen[0]
    out = {"tenant_id": str(_text(tenant.get("tenantId")))}
    name = _text(tenant.get("tenantName"))
    if name:
        out["account"] = name
    return out


def atlassian_site(provider: OAuthProvider, grant: TokenGrant, config: Mapping[str, Any]) -> dict[str, str]:
    """The Atlassian site this grant reaches: its cloud ID (every API call goes to
    `https://api.atlassian.com/ex/jira/<cloud ID>`) and its own address. A row that already names its site keeps it:
    only that site's grant is taken, and none is refused. A new row takes the first site granting Jira work
    (accessible-resources lists every site the person ever granted the app, in the order Atlassian answers)."""
    status, sites = _get_json(provider, ATLASSIAN_RESOURCES_URL, grant.access_token)
    if status != 200 or not isinstance(sites, list):
        log.warning("oauth connect: atlassian accessible-resources read answered HTTP %s", status)
        raise GrantRefused("no_account")
    usable = [site for site in sites if isinstance(site, dict) and _text(site.get("id"))
              and str(site.get("url") or "").startswith("https://")]
    own = str(config.get("base_url") or "").strip().rstrip("/").lower()
    if own:
        usable = [site for site in usable if str(site["url"]).rstrip("/").lower() == own]
    jira = [site for site in usable if any("jira" in str(scope) for scope in site.get("scopes") or [])]
    chosen = (jira or usable)[:1]
    if not chosen:
        raise GrantRefused("no_account")
    site = chosen[0]
    return {"cloud_id": str(_text(site.get("id"))), "base_url": str(site["url"]).rstrip("/")}


def posthog_region(provider: OAuthProvider, grant: TokenGrant, config: Mapping[str, Any]) -> dict[str, str]:
    """The PostHog cloud region the signed-in account lives in: the first whose private API reads the user with
    this token. Its address is the row's PostHog host, which Test and agents call."""
    for base in POSTHOG_REGIONS:
        status, user = _get_json(provider, f"{base}/api/users/@me/", grant.access_token)
        if status == 200 and isinstance(user, dict):
            out = {"base_url": base}
            email = _text(user.get("email"))
            if email:
                out["account"] = email
            return out
    raise GrantRefused("no_account")
