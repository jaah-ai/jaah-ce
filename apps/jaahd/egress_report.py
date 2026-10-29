"""Report this node's public egress IPv4 to the control plane — the source of the control plane's
emergency tailnet allowlist (the API's headscale allowlist, D13).

Headscale's v1 `Node` carries no public IP, so the node says it: `tailscale netcheck --format=json`'s
`GlobalV4` (what the DERP/STUN servers saw) goes to `POST /api/agent/nodes/egress` as `{"ipv4": ...}`, or
`null` when netcheck found none. Only on a tailnet-joined node (`tailscale status` BackendState
`Running`): an unjoined host has no business in the allowlist, and netcheck there says nothing about the
path a joined client would take.

Sent on change, and at least every `RESEND_SECONDS` so the CP's 7-day freshness window never drops a node
whose IP simply did not move. A failed POST is not recorded as sent, so the next pass retries. A failed
netcheck sends nothing; leaving the tailnet, or starting off it, sends one null.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import subprocess
import time
from typing import Callable, Optional

import control_plane_api as control_plane_api_mod
import environment

log = logging.getLogger("jaahd.egress_report")

INTERVAL_SECONDS = 900.0
RESEND_SECONDS = 6 * 3600.0
_TAILSCALE_TIMEOUT_SECONDS = 30.0

# "Not sent yet": distinct from a sent `None`.
_UNSENT = object()
# netcheck itself failed (timeout, bad exit, bad JSON) — distinct from "netcheck found no GlobalV4", so one
# flaky pass never reports null and drops the node from an armed allowlist.
SENSE_FAILED = object()


def _tailscale_json(*args: str) -> Optional[dict]:
    if not environment.tailscale_askable():
        return None
    try:
        proc = subprocess.run(
            environment.tailscale_command(*args),
            capture_output=True, text=True, timeout=_TAILSCALE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("egress-report: tailscale %s unavailable (%s)", args[0], exc)
        return None
    if proc.returncode != 0:
        log.debug("egress-report: tailscale %s failed (rc=%s)", args[0], proc.returncode)
        return None
    try:
        parsed = json.loads(proc.stdout or "{}")
    except ValueError:
        log.warning("egress-report: tailscale %s returned unparseable JSON", args[0])
        return None
    return parsed if isinstance(parsed, dict) else None


def tailnet_joined() -> bool:
    status = _tailscale_json("status", "--json")
    return bool(status) and status.get("BackendState") == "Running"


def sense_egress_ipv4() -> object:
    """`GlobalV4` from netcheck (an `ip:port` string) as a bare public IPv4; None when netcheck found none;
    `SENSE_FAILED` when netcheck could not be run or read."""
    report = _tailscale_json("netcheck", "--format=json")
    if report is None:
        return SENSE_FAILED
    value = str(report.get("GlobalV4") or "").strip()
    host = value.rsplit(":", 1)[0] if value.count(":") == 1 else value
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        return None
    return str(address) if address.is_global and not address.is_multicast else None


class EgressReporter:
    """One `PeriodicStep`: sense, and POST on change or after `RESEND_SECONDS`."""

    def __init__(
        self,
        *,
        joined: Callable[[], bool] = tailnet_joined,
        sense: Callable[[], object] = sense_egress_ipv4,
        post: Callable[[Optional[str]], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._joined = joined
        self._sense = sense
        self._post = post or control_plane_api_mod.agent_api_node_egress
        self._clock = clock
        self._sent: object = _UNSENT
        self._sent_at = 0.0

    def pass_once(self) -> bool:
        """One pass, synchronously. Returns whether a report was sent."""
        now = self._clock()
        if not self._joined():
            # Left the tailnet: clear the IP once, so the node leaves the allow set now, not in 7 days. Also on a
            # first pass: the CP may still hold what the daemon before an upgrade or restart stamped.
            if self._sent is None:
                return False
            return self._send(None, now)
        ipv4 = self._sense()
        if ipv4 is SENSE_FAILED:
            return False
        if ipv4 == self._sent and now - self._sent_at < RESEND_SECONDS:
            return False
        return self._send(ipv4, now)

    def _send(self, ipv4: Optional[str], now: float) -> bool:
        if not self._post(ipv4):
            return False
        if ipv4 != self._sent:
            log.info("egress-report: egress IPv4 is now %s", ipv4 or "unknown")
        self._sent, self._sent_at = ipv4, now
        return True

    async def step(self) -> None:
        await asyncio.to_thread(self.pass_once)
