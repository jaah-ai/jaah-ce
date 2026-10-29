"""The egress allowlist the vnode helper's host table applies (`vnodehelper/nft.py`): globally routable
IPv4 only, COMPUTED as the complement of `NON_ROUTABLE_DESTINATIONS`, so a network the node grows later
(a `docker network create`, a VPC interface) is denied without being listed.
"""
from __future__ import annotations

import ipaddress

# Every IPv4 range that is NOT globally routable — a curated subset of the IANA special-purpose
# registry (RFC 6890), not the fleet's networks, so it cannot go stale. Notable: 169.254/16 holds the
# instance-metadata service (the node's role credentials); 100.64/10 is the tailnet;
# 198.51.100/24 is where the vnode /31 links come from, so denying it separates vnodes.
NON_ROUTABLE_DESTINATIONS = (
    "0.0.0.0/8",          # "this network"
    "10.0.0.0/8",         # RFC 1918
    "100.64.0.0/10",      # CGNAT — the tailnet
    "127.0.0.0/8",        # loopback
    "169.254.0.0/16",     # link-local, incl. the instance-metadata endpoint
    "172.16.0.0/12",      # RFC 1918 — docker's default pool, the static slots' compose networks
    "192.0.0.0/24",       # IETF protocol assignments
    "192.0.2.0/24",       # TEST-NET-1
    "192.88.99.0/24",     # 6to4 relay anycast (deprecated)
    "192.168.0.0/16",     # RFC 1918 — docker's other default pool
    "198.18.0.0/15",      # RFC 2544 benchmarking
    "198.51.100.0/24",    # TEST-NET-2
    "203.0.113.0/24",     # TEST-NET-3
    "224.0.0.0/4",        # multicast
    "240.0.0.0/4",        # reserved, up to and including 255.255.255.255
)

_V4_LAST = 0xFFFFFFFF


def _merged_spans(networks: tuple[str, ...]) -> list[list[int]]:
    """The networks as sorted, merged `[first, last]` integer spans.

    Merging matters: `224.0.0.0/4` and `240.0.0.0/4` are adjacent, and two touching spans left
    separate would put an empty gap between them in the complement below.
    """
    parsed = [ipaddress.ip_network(entry) for entry in networks]
    for net in parsed:
        # Raised, not asserted (`python3 -O` strips asserts): keeps the complement an IPv4 computation.
        if net.version != 4:
            raise ValueError(f"{net} is not IPv4; v6 is dropped outright, never filtered")
    spans = sorted([int(net.network_address), int(net.broadcast_address)] for net in parsed)
    merged: list[list[int]] = []
    for first, last in spans:
        if merged and first <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], last)
        else:
            merged.append([first, last])
    return merged


def _span_literal(first: int, last: int) -> str:
    low, high = ipaddress.IPv4Address(first), ipaddress.IPv4Address(last)
    return str(low) if first == last else f"{low}-{high}"


def globally_routable_destinations() -> tuple[str, ...]:
    """THE ALLOWLIST — everything `NON_ROUTABLE_DESTINATIONS` leaves over, as nft range literals.

    Computed, never typed, so the only thing to audit is the deny list, which is a published registry.
    """
    allowed: list[str] = []
    cursor = 0
    for first, last in _merged_spans(NON_ROUTABLE_DESTINATIONS):
        if first > cursor:
            allowed.append(_span_literal(cursor, first - 1))
        cursor = max(cursor, last + 1)
    if cursor <= _V4_LAST:
        allowed.append(_span_literal(cursor, _V4_LAST))
    return tuple(allowed)
