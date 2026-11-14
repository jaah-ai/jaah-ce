"""Tell the control plane while this node's daemon runs tmux command clients unjailed (`session_user.jail_gap`).

A runner-reachable fleet server can make the daemon's command client read or write a file, or exec a command, as
the daemon's account (`tmux_jail`). Where no jail runs — a Linux kernel without Landlock, a Windows node without
its runner logon file — each pass logs one warning line carrying `MARK`; the shipped log (`node_logs`) reaches the
control plane, whose classifier reads it as the `tmux_unjailed` Health finding (`apps/api/app/node_log_findings.py` holds `MARK`'s twin), active while the
line keeps arriving and cleared once it stops.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

import session_user

log = logging.getLogger("jaahd.tmux_jail_health")

# Well inside the control plane's 30-minute finding window, so the finding stays active between passes.
INTERVAL_SECONDS = 600.0
# The API's classifier matches this text (`node_log_findings._UNJAILED_MARK`).
MARK = "tmux command clients unjailed: "


class JailHealthReporter:
    """One `PeriodicStep`: log the warning line while `jail_gap` names a gap."""

    def __init__(self, *, gap: Callable[[], str] = session_user.jail_gap) -> None:
        self._gap = gap

    def pass_once(self) -> bool:
        """True when a warning was logged."""
        gap = self._gap()
        if not gap:
            return False
        log.warning("%s%s - a runner can make them read or write files, or run a command, as this daemon's account",
                    MARK, gap)
        return True

    async def step(self) -> None:
        await asyncio.to_thread(self.pass_once)
