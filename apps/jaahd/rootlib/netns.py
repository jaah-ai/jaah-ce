"""Root-namespace network primitives the vnode helper's links need: IPv6 off on a host end, the docker
FORWARD pass-through, main-table routing by source, and `ip_forward`. Every function is idempotent.

The per-link namespaces and veths themselves are `vnodehelper/ops.py`'s; this module holds what is not
specific to one of them.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import hostnet, runner
from .runner import run

log = logging.getLogger("jaah.rootlib.netns")


def pin_to_main_table(cidr: str, priority: int, run=None) -> None:
    """Route every packet sourced from `cidr` by the main table, so it leaves by the uplink. Idempotent.

    The nft tables drop link traffic not leaving by the uplink but cannot choose the route. With `tailscale
    up --accept-routes`, a node in the fleet VPC gets the VPC CIDR in table 52 (ahead of `main`), so DNAT'd
    DNS to the VPC resolver would leave by `tailscale0` and be dropped. One rule by source covers the range
    (anti-spoof makes the source exact). `File exists` is success. `run` defaults to this module's, looked
    up at call time."""
    run = run or globals()["run"]
    shown = run("ip", "-4", "rule", "show", "priority", str(priority), check=False)
    if f"from {cidr} lookup main" in shown.stdout:
        return
    added = run("ip", "-4", "rule", "add", "from", cidr, "lookup", "main", "priority", str(priority),
                check=False)
    if added.returncode != 0 and "File exists" not in added.stderr:
        raise RuntimeError(f"ip rule add from {cidr}: {added.stderr.strip() or 'failed'}")


# `-w`: queue for the xtables lock — dockerd rewrites its chains on every container start.
_IPTABLES_WAIT = ("-w", "5")


def _iptables_available() -> bool:
    """Whether this node has the binary at all.

    `check=False` does not cover a missing program (`FileNotFoundError`), which would fail a vnode's
    create mid-build and its destroy before the link and namespace go. No iptables means no docker FORWARD
    policy to get past."""
    return Path(runner.PROGRAMS["iptables"]).exists()


def allow_through_docker_forward(host_if: str, uplink_if: str) -> None:
    """Let a link's already-filtered traffic past rootful docker's `FORWARD DROP` policy.

    Not a second filter and must never become one — the nft table decides; an nft `accept` only means
    "continue", so without these the forwarded egress hits docker's drop.

    `DOCKER-USER`, not `FORWARD`: docker flushes `FORWARD` on restart but documents `DOCKER-USER` as
    user-owned, jumps to it first, and never flushes it. The reply rule is conntrack ESTABLISHED/RELATED
    only, so a new inbound connection still meets docker's drop (no inbound reachability)."""
    # Created if missing so a node whose docker has not started converges; docker adopts it. With no docker at
    # all these sit in an unreferenced chain.
    if not _iptables_available():
        log.info("no iptables on this node — no docker FORWARD policy for %s to pass through", host_if)
        return
    run("iptables", *_IPTABLES_WAIT, "-N", "DOCKER-USER", check=False)
    # An egress rule pinned to a PREVIOUS uplink goes first: the uplink moves (`uplink_watch.py`) and a
    # rebuild after a NIC change lands here too, and a leftover `-o <old>` would pass traffic by a wire the
    # nft table no longer names the day that wire carries a default route again.
    listing = run("iptables", *_IPTABLES_WAIT, "-S", "DOCKER-USER", check=False)
    for stale in _stale_egress_rules(listing.stdout, host_if, uplink_if):
        while run("iptables", *_IPTABLES_WAIT, "-D", *stale, check=False).returncode == 0:
            pass
    for rule in _docker_user_rules(host_if, uplink_if):
        # Delete every copy first so retries converge on exactly one rule.
        while run("iptables", *_IPTABLES_WAIT, "-D", *rule, check=False).returncode == 0:
            pass
        run("iptables", *_IPTABLES_WAIT, "-I", *rule)


def deny_through_docker_forward(host_if: str, uplink_if: str) -> None:
    """Remove a link's pass-through — the current uplink's AND any older wire's, so a link whose
    re-pin once failed, or a destroy with no default route at all, leaves no trace. Tolerates the rules,
    the chain and the binary being gone."""
    if not _iptables_available():
        return
    listing = run("iptables", *_IPTABLES_WAIT, "-S", "DOCKER-USER", check=False)
    for rule in _docker_user_rules(host_if, uplink_if) + _stale_egress_rules(listing.stdout, host_if, uplink_if):
        while run("iptables", *_IPTABLES_WAIT, "-D", *rule, check=False).returncode == 0:
            pass


def _stale_egress_rules(listing: str, host_if: str, uplink_if: str) -> tuple[tuple[str, ...], ...]:
    """From `iptables -S DOCKER-USER`, this link's egress pass-throughs pinned to any wire but `uplink_if`,
    each as the `-D` argv that removes it. PURE. Only the exact shape `_docker_user_rules` installs is
    matched, so nothing an operator or docker wrote in the chain is touched."""
    stale = []
    for line in listing.splitlines():
        words = line.split()
        if (len(words) == 8 and words[:4] == ["-A", "DOCKER-USER", "-i", host_if]
                and words[4] == "-o" and words[6:] == ["-j", "ACCEPT"] and words[5] != uplink_if):
            stale.append(("DOCKER-USER", "-i", host_if, "-o", words[5], "-j", "ACCEPT"))
    return tuple(stale)


def _docker_user_rules(host_if: str, uplink_if: str) -> tuple[tuple[str, ...], ...]:
    """The two rules, spelled once so install and remove cannot drift (`-I`/`-D` share the shape)."""
    return (
        # `-o {uplink_if}` is the fail-closed half: without it a deleted nft table would leave the link fully
        # open; pinned to the uplink, a missing table leaves it fenced in.
        ("DOCKER-USER", "-i", host_if, "-o", uplink_if, "-j", "ACCEPT"),
        ("DOCKER-USER", "-o", host_if, "-m", "conntrack",
         "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"),
    )


# Parameterised so a test can point at a file instead of /proc.
IP_FORWARD_KNOB = "/proc/sys/net/ipv4/ip_forward"


def enable_ip_forwarding(knob_path: str = IP_FORWARD_KNOB) -> None:
    """`net.ipv4.ip_forward=1`, without which the filter permits packets the node never routes.

    Runtime knob only, no sysctl.d drop-in: a vnode's create and the boot rebuild both come through here.
    Idempotent; docker normally has it on already."""
    knob = Path(knob_path)
    try:
        if knob.read_text().strip() == "1":
            return
        # A 0→1 flip is worth a journal line: moby sets `FORWARD DROP` only when dockerd makes this
        # transition, so a later dockerd skips its default-drop.
        log.warning("net.ipv4.ip_forward was 0 and this helper is enabling it — a dockerd starting "
                    "after this will NOT set its own FORWARD default-drop")
        knob.write_text("1\n")
    except OSError as exc:
        # No knob or read-only /proc: either way this node cannot route egress.
        raise RuntimeError(
            f"{knob} is not writable ({exc.__class__.__name__}) — "
            "this node cannot route a vnode's egress") from exc


def _disable_ipv6(interface: str) -> None:
    """Turn the v6 stack off on one interface. Absent knob = a kernel built without v6; nothing to do."""
    knob = Path(f"/proc/sys/net/ipv6/conf/{interface}/disable_ipv6")
    try:
        knob.write_text("1\n")
    except FileNotFoundError:
        # A missing knob on a node that has v6 would leave the link v6-live behind only the filter — refuse;
        # no v6 stack at all is fine.
        if Path("/proc/sys/net/ipv6").exists():
            raise RuntimeError(
                f"this node has IPv6 but {knob} is missing — refusing to bring up {interface}")
        log.info("no v6 stack on this node — nothing to disable on %s", interface)


def _uplink_or_none() -> str:
    """The uplink, or `""` — a destroy must converge after the default route has gone."""
    try:
        return hostnet.node_uplink()
    except hostnet.NoUplink:
        return ""
