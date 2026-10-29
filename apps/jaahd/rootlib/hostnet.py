"""Which interface a vnode's egress may leave the node by — sensed on the node, never configured.

`vnodehelper/nft.py`'s allowlist decides destinations; the routing table decides the wire, so both are pinned.
A positive pin, not a deny list: tailscale1, `--tun=<name>`, wg0/tun0, or a docker bridge on a public
subnet would all otherwise carry egress off the node (the tailnet one masqueraded AS the node).
A tailscale exit node makes the uplink `tailscale0`: that is a refusal (`NoUplink`), never a pin (D16).
Accepted limit: the node's resolver must be reachable over the uplink, else DNS fails closed.
`/proc/net/route`, not `ip route`: no subprocess, no `PROGRAMS` entry, testable from a sample.
Stdlib only, no import side effects — the rebuild oneshot imports this first.
"""
from __future__ import annotations

PROC_NET_ROUTE = "/proc/net/route"

# The tailnet's device, which `node_uplink()` refuses to pin.
TAILSCALE_IF = "tailscale0"

# /proc/net/route's columns, by index. A default route is all-zeros hex in Destination and Mask, so a
# string compare is exact regardless of endianness.
_IFACE, _DESTINATION, _METRIC, _MASK = 0, 1, 6, 7
_DEFAULT = "00000000"


class NoUplink(Exception):
    """This node has no interface a vnode's egress may legitimately leave by.

    Raised, never defaulted: pinning nothing leaves the wire unconstrained, pinning the tailnet device
    hands a vnode the control plane.
    """

    REMEDY = ("give this node a default route over an ordinary uplink, or clear the tailscale exit "
              "node — a vnode may not egress over the tailnet (D16)")


def default_routes(proc_net_route_text: str) -> tuple[tuple[str, int], ...]:
    """Every interface carrying a default route, as `(interface, metric)`, lowest metric first.

    Ties keep file order (the kernel's own pick). A malformed line is skipped, never raised on.
    """
    found: list[tuple[str, int]] = []
    for line in proc_net_route_text.splitlines()[1:]:   # the first line is the column header
        fields = line.split()
        if len(fields) <= _MASK:
            continue
        if fields[_DESTINATION] != _DEFAULT or fields[_MASK] != _DEFAULT:
            continue
        try:
            metric = int(fields[_METRIC])
        except ValueError:
            continue
        found.append((fields[_IFACE], metric))
    return tuple(sorted(found, key=lambda entry: entry[1]))


def node_uplink(proc_net_route_text: str | None = None, *, tailscale_if: str = TAILSCALE_IF) -> str:
    """The one interface a vnode's egress may leave by, or `NoUplink`.

    The lowest-metric default route wins: the kernel's choice for every public address the allowlist admits.
    """
    if proc_net_route_text is None:
        try:
            with open(PROC_NET_ROUTE, encoding="utf-8", errors="replace") as handle:
                proc_net_route_text = handle.read()
        except OSError as exc:
            raise NoUplink(f"cannot read {PROC_NET_ROUTE} ({exc.__class__.__name__})") from exc

    routes = default_routes(proc_net_route_text)
    if not routes:
        raise NoUplink(f"no default route on this node — {NoUplink.REMEDY}")

    interface, _ = routes[0]
    if interface == tailscale_if:
        # Not "take the runner-up": the node really sends public traffic over the tailnet, so pinning
        # another wire would break egress in a way that reads like a filter bug.
        raise NoUplink(
            f"the default route on this node is {interface}, the tailnet device — a vnode's "
            f"egress may not leave over the tailnet (D16). {NoUplink.REMEDY}")
    return interface
