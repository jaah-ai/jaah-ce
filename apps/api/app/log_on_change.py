"""Log a repeating condition once per state change, not once per tick.

A caller that re-detects the same condition every tick (a poll, a cron, a per-row read) asks
`changed(key, value)`: True the first time `key` carries `value`, False while it keeps carrying it.
`forget(key)` on recovery, so a later regression logs again. `WarnOnce` is the other shape: one WARNING per
key for the process's life, DEBUG after, never reset in production.

Process-local and lock-free: a race costs at most one duplicate line. Values stay in memory only.
"""
from __future__ import annotations

import logging
from typing import Hashable

# Keys are few (nodes, alarm keys); the cap only bounds a pathological key stream.
_MAX_KEYS = 4096


class ChangeLog:
    """The last logged value per key; one instance per log line."""

    def __init__(self, max_keys: int = _MAX_KEYS) -> None:
        self._last: dict[Hashable, Hashable] = {}
        self._max_keys = max_keys

    def changed(self, key: Hashable, value: Hashable) -> bool:
        """Record `value` for `key`; True iff it differs from the last one recorded (or none was)."""
        if key in self._last and self._last[key] == value:
            return False
        if key not in self._last and len(self._last) >= self._max_keys:
            self._last.clear()
        self._last[key] = value
        return True

    def forget(self, key: Hashable) -> None:
        """The condition cleared: the next `changed(key, ...)` is True again."""
        self._last.pop(key, None)


class WarnOnce:
    """One WARNING per key, DEBUG after. `cap` bounds the key set: a full set demotes every NEW key to debug
    rather than evicting (the keys may arrive before an auth gate). `key_max` truncates str keys (the full
    value stays on the log line); other key shapes are kept whole."""

    def __init__(self, log: logging.Logger, cap: int = 512, key_max: int = 64) -> None:
        self.log, self.cap, self.key_max = log, cap, key_max
        self.seen: set[Hashable] = set()

    def say(self, key: Hashable, msg: str, *args: object) -> None:
        if isinstance(key, str):
            key = key[: self.key_max]
        if key in self.seen or len(self.seen) >= self.cap:
            self.log.debug(msg, *args)
            return
        self.seen.add(key)
        self.log.warning(msg, *args)

    def reset(self) -> None:
        """Test seam only — the set is never cleared in production."""
        self.seen.clear()

    def __len__(self) -> int:
        return len(self.seen)
