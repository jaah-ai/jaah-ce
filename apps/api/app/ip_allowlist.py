"""Per-org IP allowlist: an org's ranges (`org_ip_ranges`, IPv4 and IPv6 CIDRs) are the only addresses
its people may use the console and API from. An empty list allows every address.

WHERE IT HOLDS — human traffic only (decision D-F5):
* every cookie-session API request, in `auth.SessionAuthMiddleware.dispatch` right after the cookie resolves, and
  every cookie WebSocket (`authz._resolve_principal`) — `session_refusal`;
* every sign-in, in `login_funnel` (`ip_policy` and its admission twin `admits`), and every org switch and completed
  second factor (`entry_refusal`), so a refused address never gets a session cookie at all;
* every open console (`console_relay/tickets.py`: the ticket is bound to the address that minted it, and the sweep
  ends a console whose address left the list) and every open `/api/stream` (re-checked at its keepalive).
Exempt by construction: node and runner traffic (`X-Agent-Token`, no cookie), trigger webhooks, SCIM, Telegram and
Slack webhooks — none carries a session cookie. Exempt by rule: an operator-plane superadmin (`is_operator_plane`),
the break-glass who clears a customer's list from its customer page. ANY FUTURE USER TOKEN TYPE (a personal API
token) must call `session_refusal` (or `refusal`) for its user, as the cookie does.

The check applies only while the org's plan has the switch `ip_allowlist_enabled` (`plan_terms`): with it off the
ranges are kept and nothing is refused.

The client address is `request.client.host`, which uvicorn sets from the ALB's own (rightmost) `X-Forwarded-For`
entry because it trusts nginx's loopback hop (`entrypoint.sh`); a client-forged header prefix is never read.

Per-process caches, 30 s TTL: the session's org (`admin_id` → org, or exempt) and the org's ranges. A save clears
its org's entry here (`invalidate`); other API processes catch up within the TTL.
"""
from __future__ import annotations

import ipaddress
import time
from contextlib import nullcontext
from typing import Final, Iterable, Optional, Union
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import login_funnel, plan_terms
from app.models.identity import AdminUser
from app.models.orgs import OrgIpRange
from app.tenancy import seam
from app.tenancy.ids import is_operator_plane

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

SWITCH: Final = "ip_allowlist_enabled"
#: The sign-in refusal's code (`login_funnel.LoginOutcome.reason`, the login page's slug).
REFUSAL_CODE: Final = "ip_not_allowed"
#: Most ranges one org may hold; a list past it is a paste gone wrong.
MAX_RANGES: Final = 100
MAX_LABEL: Final = 100
CACHE_TTL_SECONDS: Final = 30.0

_EXAMPLE = "Use an address like 203.0.113.7 or a range like 203.0.113.0/24 or 2001:db8::/32."


def refusal_sentence(ip: Optional[str]) -> str:
    """What a refused request or sign-in is told: why, and what to do next."""
    return (
        "Your organization only allows access from approved networks. "
        f"Ask an admin to add this address: {ip or 'unknown'}."
    )


def refused_response(ip: Optional[str]) -> JSONResponse:
    """The 403 a refused request answers — the middleware's, and a route's that raises `IpNotAllowed`. The web
    client reads `code` and lands on its blocked-network panel (`apps/web/src/api.ts::signalIpNotAllowed`)."""
    return JSONResponse({"detail": refusal_sentence(ip), "code": REFUSAL_CODE, "ip": ip}, status_code=403)


class IpNotAllowed(Exception):
    """A route's refusal of `ip` (a second factor completing from outside the list); `app/main.py` answers it with
    `refused_response`."""

    def __init__(self, ip: Optional[str]) -> None:
        super().__init__(REFUSAL_CODE)
        self.ip = ip


def parse_range(text: str) -> Network:
    """One range as typed: a CIDR (host bits are dropped: 10.0.0.5/24 is 10.0.0.0/24) or a bare address (/32 or
    /128). An IPv4-mapped IPv6 range (`::ffff:203.0.113.0/120`) is stored as its IPv4 range, since a mapped client
    is matched as IPv4 (`parse_address`); one typed in mapped form but wider than /96 (which masks the `ffff`
    away and would never match) is refused. ValueError carries the sentence a person is shown."""
    value = text.strip()
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise ValueError(f"“{value}” isn't a valid IP range. {_EXAMPLE}") from None
    if isinstance(network, ipaddress.IPv6Network) and value.lower().startswith("::ffff:") and (
        network.prefixlen < _MAPPED_PREFIX
    ):
        raise ValueError(f"“{value}” is wider than the IPv4-mapped block. Enter it as an IPv4 range. {_EXAMPLE}")
    if isinstance(network, ipaddress.IPv6Network) and network.network_address.ipv4_mapped is not None:
        return ipaddress.ip_network(
            f"{network.network_address.ipv4_mapped}/{network.prefixlen - _MAPPED_PREFIX}", strict=False
        )
    return network


#: `::ffff:0:0/96`, the IPv4-mapped block: a mapped /120 is an IPv4 /24.
_MAPPED_PREFIX: Final = 96


def parse_address(host: Optional[str]) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """The client address, or None when there is none or it is not an IP (a test client's name). An IPv4-mapped IPv6
    address is matched as its IPv4 address."""
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def allowed(ranges: Iterable[Network], host: Optional[str]) -> bool:
    """Whether `host` is in any of `ranges`. An unknown or non-IP host is in none (fail closed)."""
    address = parse_address(host)
    if address is None:
        return False
    return any(address.version == network.version and address in network for network in ranges)


# ── The caches ───────────────────────────────────────────────────────────────────────────────────────────────────────

#: Past this many entries a write drops the expired ones, so the cache holds about one TTL's worth of sessions.
_SESSION_ORGS_PRUNE_AT: Final = 10_000
#: admin_id → (expires, org_id), org_id None for an exempt (operator-plane) row or one that is gone.
_SESSION_ORGS: dict[UUID, tuple[float, Optional[UUID]]] = {}
#: org_id → (expires, the ranges that hold — empty while the switch is off or the list is empty).
_ORG_RANGES: dict[UUID, tuple[float, tuple[Network, ...]]] = {}


def invalidate(org_id: UUID) -> None:
    """Forget the org's ranges in this process: the next request reads the saved list."""
    _ORG_RANGES.pop(org_id, None)


def clear_caches() -> None:
    """Forget everything (tests)."""
    _SESSION_ORGS.clear()
    _ORG_RANGES.clear()


def stored_ranges(session: Session, org_id: UUID) -> list[OrgIpRange]:
    """The org's saved rows, in the order they were entered. The operator plane already reads every org; anything
    else (the middleware's unbound read, an org admin's own org) reads under the org."""
    with nullcontext() if seam.current_scope() is seam.OPERATOR else seam.bound_to(org_id):
        return list(session.scalars(
            select(OrgIpRange).where(OrgIpRange.org_id == org_id).order_by(OrgIpRange.created_at, OrgIpRange.id)
        ))


def enforced_ranges(session: Session, org_id: UUID) -> tuple[Network, ...]:
    """The ranges that hold for the org now (cached): empty when the plan switch is off or nothing is saved."""
    now = time.monotonic()
    cached = _ORG_RANGES.get(org_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    ranges: tuple[Network, ...] = ()
    if plan_terms.switch_on(session, org_id, SWITCH):
        ranges = tuple(ipaddress.ip_network(str(row.cidr), strict=False) for row in stored_ranges(session, org_id))
    _ORG_RANGES[org_id] = (now + CACHE_TTL_SECONDS, ranges)
    return ranges


def _session_org(session: Session, admin_id: UUID) -> Optional[UUID]:
    """The org a session row's requests are held to, or None when the row is exempt or gone (cached)."""
    now = time.monotonic()
    cached = _SESSION_ORGS.get(admin_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    # This read discovers the org, so no org filter can apply (`seam.identity_scope`).
    with seam.identity_scope():
        user = session.get(AdminUser, admin_id)
        org_id = None if user is None or is_operator_plane(user.org_id, user.is_superadmin) else user.org_id
    if len(_SESSION_ORGS) > _SESSION_ORGS_PRUNE_AT:
        for key in [key for key, (expires, _) in _SESSION_ORGS.items() if expires <= now]:
            _SESSION_ORGS.pop(key, None)
    _SESSION_ORGS[admin_id] = (now + CACHE_TTL_SECONDS, org_id)
    return org_id


def refusal(session: Session, org_id: Optional[UUID], host: Optional[str]) -> Optional[str]:
    """The refusal sentence when the org's allowlist holds and `host` is outside it, else None."""
    if org_id is None:
        return None
    ranges = enforced_ranges(session, org_id)
    if not ranges or allowed(ranges, host):
        return None
    return refusal_sentence(host)


def session_refusal(admin_id: UUID, host: Optional[str]) -> Optional[str]:
    """`refusal` for a verified session cookie's row, on the identity lane (`app/db.py`): the middleware and the
    WebSocket principal call this before any route session exists."""
    from app.db import IdentitySessionLocal

    with IdentitySessionLocal() as session:
        return refusal(session, _session_org(session, admin_id), host)


def exempt(user: AdminUser) -> bool:
    """An operator-plane superadmin: never held to a tenant's list (the break-glass)."""
    return is_operator_plane(user.org_id, user.is_superadmin)


# ── Sign-in (`app/login_funnel.py`) ──────────────────────────────────────────────────────────────────────────────────


def admits(
    session: Session, user: AdminUser, method: str, idp_claims: Optional[dict], client_ip: Optional[str] = None
) -> bool:
    """The `ADMISSION_CHECKS` twin of `ip_policy`: whether this membership's org admits a sign-in from `client_ip`,
    read without recording anything. A door with no request (None) is not judged here; `ip_policy` still is."""
    if client_ip is None or exempt(user):
        return True
    return refusal(session, user.org_id, client_ip) is None


def ip_policy(
    session: Session,
    request: Request,
    user: Optional[AdminUser],
    identifier: str,
    method: str,
    idp_claims: Optional[dict],
) -> Optional[login_funnel.LoginOutcome]:
    """The `POLICY_HOOKS` entry: refuse a sign-in from outside the org's allowlist, so no cookie is minted."""
    if user is None or exempt(user):
        return None
    host = login_funnel.source_ip(request)
    if refusal(session, user.org_id, host) is None:
        return None
    login_funnel.record_login_failure(session, request, user, method=method, reason=REFUSAL_CODE)
    return login_funnel.LoginOutcome("refused", REFUSAL_CODE)


def entry_refusal(session: Session, user: AdminUser, client_ip: Optional[str]) -> Optional[str]:
    """The `ENTRY_CHECKS` entry: `REFUSAL_CODE` when an org switch into, or a second factor completing a sign-in
    of, `user` comes from outside its org's list — refused up front rather than minting a cookie the middleware
    would then refuse."""
    if exempt(user) or refusal(session, user.org_id, client_ip) is None:
        return None
    return REFUSAL_CODE


login_funnel.POLICY_HOOKS.append(ip_policy)
login_funnel.ADMISSION_CHECKS.append(admits)
login_funnel.ENTRY_CHECKS.append(entry_refusal)
