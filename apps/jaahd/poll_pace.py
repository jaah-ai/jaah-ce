"""How often an idle node asks the control plane anything: fast while something is happening, slow when not.

Every node polls the one CP process, so an idle fleet's polling competes with every console read. A step
that can tell "nothing to do" returns `IDLE` (`True`), and `main.run` then waits its `idle_interval`
instead of its `interval` until it reports work again. Any node activity — a CP-pushed intent, a live
runner, an open console — snaps every paced step back to its fast interval for `ACTIVE_HOLD_SECS`, so a
node that just did something answers the follow-up at full speed. Pushed work never waits on this pace: a
`send_intent` frame spawns its own pass (`main.NodeStatePublisher.push_slot_intent`).
"""
from __future__ import annotations

import time

# What a paced step returns when it found nothing to do this pass. Anything else (None included) is "busy".
IDLE = True

# The observation pass's idle interval: pool-tick + fleet/observe. 30s is a quarter of the CP's default
# observation-freshness bound (`fleet_max_observation_age_secs`, 120s), so an idle node's occupancy stays
# trusted with three passes to spare.
PROJECTION_IDLE_INTERVAL_SECS = 30.0

# Back-office polls (transcript work list, GHA runner sync) when they return nothing to do.
BACKGROUND_IDLE_INTERVAL_SECS = 300.0

# How long after the last activity every paced step stays at its fast interval.
ACTIVE_HOLD_SECS = 60.0

# Monotonic stamp of the last activity on this node; starts "just active", so a fresh daemon runs fast first.
_active_at: float = time.monotonic()


def note_activity(now: float | None = None) -> None:
    """Something happened on this node: every paced step runs at its fast interval for `ACTIVE_HOLD_SECS`."""
    global _active_at
    _active_at = time.monotonic() if now is None else now


def recently_active(now: float | None = None) -> bool:
    """Whether this node saw activity within `ACTIVE_HOLD_SECS`."""
    return ((time.monotonic() if now is None else now) - _active_at) < ACTIVE_HOLD_SECS


def interval(fast: float, slow: float | None, *, idle: bool, now: float | None = None) -> float:
    """The interval a paced step waits next: `slow` only when its last pass was idle, it has one, and the node
    has been quiet for `ACTIVE_HOLD_SECS`; `fast` otherwise."""
    if slow is None or not idle or recently_active(now):
        return fast
    return max(fast, slow)
