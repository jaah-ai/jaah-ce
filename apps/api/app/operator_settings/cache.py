"""The process-wide cache of both stored override tiers: its lock, TTL, generation and invalidation."""
from __future__ import annotations

import threading
from typing import Any, Optional
from uuid import UUID

_CACHE_TTL_SECONDS = 3.0

# ── Process-wide cache ──
# `_cached_at`/`_cached_overrides` are one piece of state, guarded together (threaded ASGI worker), so a
# pre-write read cannot re-stamp a stale snapshot over `invalidate_cache()`. The DB read runs outside
# the lock; `_generation` detects an invalidation during it, and such a value is served uncached.

_cache_lock = threading.Lock()
_cached_at: Optional[float] = None
_cached_overrides: dict[str, Any] = {}
_generation: int = 0

#: The second cache, sharing `_cached_overrides`' lock, TTL and generation, so one
#: `invalidate_cache()` clears both tiers.
_cached_org_at: Optional[float] = None
_cached_org: dict[UUID, dict[str, Any]] = {}
_cached_overridden_at: Optional[float] = None
_cached_overridden: frozenset[str] = frozenset()


def invalidate_cache() -> None:
    """Force the next `overrides()` call, in this process, to re-read the row. Call after a successful
    `apply_overrides` commit so the writer sees its own edit immediately."""
    global _cached_at, _cached_org_at, _cached_org, _cached_overridden_at, _generation
    with _cache_lock:
        _cached_at = None
        # Both tiers and the key census together, so a resolution beginning after this call never composes
        # a value neither tier held. One already in flight can still tear (two cached reads), bounded by the TTL.
        _cached_org_at = None
        _cached_org = {}
        _cached_overridden_at = None
        _generation += 1
