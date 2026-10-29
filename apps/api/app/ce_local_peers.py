"""The community edition's control plane answers only callers on its own machine.

The edition has one node, on the same machine as its control plane (`app/ce_bootstrap.py`), and every port its
compose publishes binds 127.0.0.1. This middleware holds the same fact in the api itself, so republishing a port on
0.0.0.0 does not let a node (or a browser) on another machine reach it: an HTTP request or WebSocket is refused
unless its peer, and the client nginx names in `X-Real-IP` (`apps/web/nginx.conf.template` sets it on every proxied
location, overwriting any the client sent), are each loopback or an address on a subnet attached to one of this
container's own interfaces — the compose networks the web and the node reach it over.

A speed bump, not a boundary. What still passes: a tunnel that delivers from inside those networks (ssh, socat); a
Docker whose port publishing rewrites the source to the bridge gateway (Docker Desktop, rootless Docker, `iptables:
false`); the api run outside a container, where the attached subnets are the host's own LAN; an edited source tree.
The licence is the control. IPv4 and loopback only (the api binds 0.0.0.0); an unreadable route table admits
loopback alone. Only the first `X-Real-IP` is read: nginx overwrites it, and a direct caller is judged on its peer.
"""
from __future__ import annotations

import ipaddress
import logging
import time
from typing import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

ROUTE_TABLE = "/proc/net/route"
REFUSAL = b'{"detail":"Jaah Community Edition answers only this machine."}'
#: At most one route-table re-read and one refusal log line per this many seconds, so a scanner costs neither.
RECHECK_SECONDS = 5.0

IPNetwork = ipaddress.IPv4Network


def attached_subnets(route_table: str = ROUTE_TABLE) -> frozenset[IPNetwork]:
    """The IPv4 subnets this host's interfaces sit on directly: `route_table` rows with no gateway, the default
    route left out. `/proc/net/route` prints each address as little-endian hex."""
    try:
        with open(route_table, encoding="ascii") as table:
            rows = table.read().splitlines()[1:]
    except OSError as exc:
        log.warning("ce_local_peers: could not read %s (%s): only loopback is admitted", route_table, exc)
        return frozenset()
    subnets: set[IPNetwork] = set()
    for row in rows:
        fields = row.split()
        try:
            if len(fields) < 8 or int(fields[2], 16) != 0:
                continue
            destination, mask = (ipaddress.IPv4Address(int(value, 16).to_bytes(4, "little")) for value in
                                 (fields[1], fields[7]))
        except ValueError:
            continue
        if int(mask):
            subnets.add(IPNetwork(f"{destination}/{mask}", strict=False))
    return frozenset(subnets)


def is_local(host: str | None, subnets: Iterable[IPNetwork]) -> bool:
    """Is `host` loopback or on one of `subnets`? Anything unparseable (absent, a hostname) is not."""
    try:
        address = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return True
    return isinstance(address, ipaddress.IPv4Address) and any(address in subnet for subnet in subnets)


def _forwarded_for(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == b"x-real-ip":
            return value.decode("latin-1")
    return None


class LocalPeersOnlyMiddleware:
    """Refuse a non-local HTTP request (403) or WebSocket (closed before accept) — the module docstring's rule.
    The attached subnets are read once, and again on a miss (`RECHECK_SECONDS` apart), so a network the container
    joins later is seen."""

    def __init__(self, app: ASGIApp, route_table: str = ROUTE_TABLE) -> None:
        self.app = app
        self.route_table = route_table
        self.subnets = attached_subnets(route_table)
        self.read_at = time.monotonic()
        self.logged_at = float("-inf")

    def _admits(self, host: str | None) -> bool:
        if is_local(host, self.subnets):
            return True
        if time.monotonic() - self.read_at < RECHECK_SECONDS:
            return False
        self.subnets, self.read_at = attached_subnets(self.route_table), time.monotonic()
        return is_local(host, self.subnets)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        peer = (scope.get("client") or (None,))[0]
        forwarded = _forwarded_for(scope)
        if self._admits(peer) and (forwarded is None or self._admits(forwarded)):
            await self.app(scope, receive, send)
            return
        if time.monotonic() - self.logged_at >= RECHECK_SECONDS:
            self.logged_at = time.monotonic()
            log.warning("ce_local_peers: refused %s %s from %s (X-Real-IP %s); further refusals for %ss unlogged",
                        scope["type"], scope.get("path"), peer, forwarded, RECHECK_SECONDS)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": REFUSAL})
