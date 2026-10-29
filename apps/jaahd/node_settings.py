"""The control plane's node tunables, as last received on `GET /api/agent/pool-tick` (`node_settings`).

The CP's single home is Settings > Infrastructure > Daemon (`apps/api/app/operator_settings/`, `NODE_SETTING_KEYS`);
`SPECS` mirrors each knob's default and range, pinned against that catalogue by `tests/test_node_settings.py`.
Every pool-tick body passes through `update_from_tick` (`control_plane_api.agent_api_pool_tick`), and
consumers call `get` at use time, so an edit applies from the next pass with no restart.

Until a tick delivers a value, and on a tick from an older CP that sends no `node_settings`, the code
default applies. A wrong-typed or out-of-range value is logged and the previous value kept: the node never
runs on a number the catalogue would refuse. Process-local: a one-shot subprocess that reads no tick runs
on the defaults.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Any, NamedTuple

log = logging.getLogger("jaahd.node_settings")


class Spec(NamedTuple):
    default: int
    minimum: int
    maximum: int


SPECS: dict[str, Spec] = {
    "node_account_probe_interval_secs": Spec(600, 120, 900),
    "node_agents_leased_probe_secs": Spec(5, 1, 300),
    "node_agents_failed_retry_secs": Spec(5, 1, 300),
    "node_agents_dirty_watch_secs": Spec(300, 240, 3600),
    "node_agents_full_scan_secs": Spec(3600, 60, 86400),
    "node_agents_probe_max_parallel": Spec(2, 1, 16),
    "node_clone_timeout_secs": Spec(900, 300, 7200),
    "node_max_concurrent_commands": Spec(64, 1, 256),
}

# Written by the pool-tick thread, read from the event loop and worker threads; a dict swap under a lock.
_lock = threading.Lock()
_values: dict[str, int] = {key: spec.default for key, spec in SPECS.items()}


def get(key: str) -> int:
    """`key`'s last received value, else its code default. A key not in `SPECS` is a caller bug: KeyError."""
    with _lock:
        return _values[key]


def _accepted(key: str, raw: Any) -> int | None:
    """`raw` as this knob's value, or None (logged) when the catalogue would refuse it."""
    spec = SPECS[key]
    # bool subclasses int; a float with a fraction is not an integer setting.
    integral = (not isinstance(raw, bool) and isinstance(raw, (int, float)) and math.isfinite(raw)
                and raw == int(raw))
    if not integral:
        log.warning("node_settings: ignoring %s=%r from the pool tick (not an integer); keeping %d",
                    key, raw, get(key))
        return None
    value = int(raw)
    if not spec.minimum <= value <= spec.maximum:
        log.warning("node_settings: ignoring %s=%d from the pool tick (outside %d..%d); keeping %d",
                    key, value, spec.minimum, spec.maximum, get(key))
        return None
    return value


def update_from_tick(body: Any) -> None:
    """Take every usable `node_settings` value off one pool-tick body. Never raises.

    An absent or non-dict field (an older CP, the empty tick, an unreachable CP's `{}`) changes nothing; a
    key this node does not know is ignored (a newer CP), and a bad value keeps the previous one."""
    if not isinstance(body, dict):
        return
    received = body.get("node_settings")
    if not isinstance(received, dict):
        return
    accepted: dict[str, int] = {}
    for key in SPECS.keys() & received.keys():
        value = _accepted(key, received[key])
        if value is not None:
            accepted[key] = value
    if not accepted:
        return
    with _lock:
        changed = {key: value for key, value in accepted.items() if _values[key] != value}
        _values.update(accepted)
    if changed:
        log.info("node_settings: applied from the pool tick: %s", changed)
