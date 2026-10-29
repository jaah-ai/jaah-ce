"""Which resolver a vnode's DNS is redirected TO — sensed on the node, never configured.

A vnode's `/etc/resolv.conf` names the host end of its /31 (no forwarder, see `vnodehelper/nft.py`); the
`prerouting` DNAT rewrites that address's port 53 to what this module returns. The loopback stub
(systemd-resolved's 127.0.0.53) is unreachable from another netns without node-global `route_localnet`,
so the uplink resolver behind it is used directly. The result is normally a private address in a denied
range; the host table's `ct original` rules admit only DNAT'd flows. Sensed at install time (the boot
rebuild re-reads it). One address, so no failover: a dead resolver shows as a hang.

Stdlib only, no import-time side effects — the rebuild oneshot imports this before anything else exists.
"""
from __future__ import annotations

import ipaddress

# systemd-resolved's uplink list never names the stub; `/etc/resolv.conf` is the fallback.
RESOLV_SOURCES = (
    "/run/systemd/resolve/resolv.conf",
    # NetworkManager+dnsmasq (self-hosted nodes): /etc/resolv.conf names dnsmasq's 127.0.1.1 stub and
    # the real uplink is only here.
    "/run/NetworkManager/resolv.conf",
    "/etc/resolv.conf",
    # Tailscale in "direct" mode (no systemd-resolved stub; Amazon Linux 2023) rewrites /etc/resolv.conf to its
    # own 100.100.100.100 and keeps the uplink here.
    "/etc/resolv.pre-tailscale-backup.conf",
)

# Tailscale's in-process resolver (MagicDNS): it answers only the host's own sockets, so a vnode's query
# DNAT'd to it never returns. Skipped like loopback, in every file.
TAILSCALE_RESOLVER = ipaddress.IPv4Address("100.100.100.100")


class NoResolver(Exception):
    """The node has no reachable upstream resolver, so a vnode would have no DNS.

    Raised rather than defaulted: a public-resolver fallback would leak queries to a third party and
    break every private name the node resolves.
    """

    REMEDY = ("configure a non-loopback nameserver on this node, or add the file that carries this "
              "node's uplink resolver to rootlib.hostdns.RESOLV_SOURCES")


def parse_nameservers(text: str) -> tuple[str, ...]:
    """The IPv4 `nameserver` addresses of a resolv.conf, in file order.

    IPv4 only (the table is an IPv4 filter and the link has v6 disabled). A malformed or v6 address is
    skipped rather than raising — one bad line must not take the node's vnodes down.
    """
    found: list[str] = []
    for line in text.splitlines():
        # `#` or `;` starts a comment; resolv.conf has no continuation syntax.
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        parts = stripped.split()
        if len(parts) < 2 or parts[0] != "nameserver":
            continue
        try:
            address = ipaddress.IPv4Address(parts[1])
        except ipaddress.AddressValueError:
            continue
        found.append(str(address))
    return tuple(found)


def node_resolver(sources: tuple[str, ...] = RESOLV_SOURCES) -> str:
    """The first IPv4 resolver this node knows that a vnode can reach, or `NoResolver`.

    First-wins across the sources; loopback and Tailscale's 100.100.100.100 are skipped in every file, since
    neither is reachable from a vnode whichever file named it.
    """
    read: list[str] = []
    for path in sources:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            continue
        read.append(path)
        for address in parse_nameservers(text):
            parsed = ipaddress.IPv4Address(address)
            if not parsed.is_loopback and parsed != TAILSCALE_RESOLVER:
                return address
    raise NoResolver(
        "no IPv4 nameserver a vnode can reach on this node (loopback and 100.100.100.100 do not count); read "
        f"{', '.join(read) if read else 'none of ' + ', '.join(sources)}"
        f" — {NoResolver.REMEDY}"
    )
