"""Pin a Windows node's fleet tmux server anchor, and tell the control plane while it stays unverifiable.

The anchor (`host_platform.windows.fleet_server_anchor`) is the daemon's only proof that a process is a runner's:
without it every runner call is refused `not_a_runner`. Each pass pins it (`pin_fleet_server_anchor`, which also
replaces a server that outlived a `jaah-runner` restart). While it stays unverified for `GRACE_PASSES` passes
running — one pass of slack for a service restart in flight — each pass posts one warning line the control plane
reads as the `fleet_anchor_unverified` Health finding (`apps/api/app/node_log_findings.py`, which holds `MARK`'s
twin), active while the line keeps arriving and cleared once it stops.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

import control_plane_api as control_plane_api_mod
from windows_engine_health import post_warning

INTERVAL_SECONDS = 60.0
# The API's classifier matches this text (`node_log_findings._ANCHOR_MARK`).
MARK = "fleet server anchor unverified: "
GRACE_PASSES = 2
UNIT = "jaah-runner"


def _pin() -> bool:
    from host_platform import windows  # noqa: PLC0415 — Windows-only
    return windows.pin_fleet_server_anchor()


def _problem() -> Optional[str]:
    from host_platform import windows  # noqa: PLC0415 — Windows-only
    return windows.fleet_anchor_problem()


class AnchorHealthReporter:
    """One `PeriodicStep`: pin the anchor; post the warning line while it stays unverified."""

    def __init__(
        self,
        *,
        pin: Callable[[], bool] = _pin,
        problem: Callable[[], Optional[str]] = _problem,
        post: Callable[[dict], dict] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._pin = pin
        self._problem = problem
        self._post = post or control_plane_api_mod.agent_api_node_logs
        self._clock = clock
        self._failed = 0

    def pass_once(self) -> bool:
        """True when a warning was posted."""
        if self._pin():
            self._failed = 0
            return False
        self._failed += 1
        why = self._problem()
        # No problem left: the pin just replaced a stale server, and the next pin verifies its successor.
        if self._failed < GRACE_PASSES or why is None:
            return False
        return post_warning(self._post, UNIT, MARK + why + " - runners on this node are refused not_a_runner",
                            self._clock())

    async def step(self) -> None:
        await asyncio.to_thread(self.pass_once)
