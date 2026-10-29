"""Sliding-window rate limiters for public endpoints: one in-memory, one shared through Postgres.

`RateLimiter` counts hits in this process. It is exact and free, and the unit-test subject, but the control
plane runs as N ECS tasks: N in-memory limiters hand a caller N times the budget, and which task a request
lands on is the load balancer's choice. `SharedRateLimiter` keeps the same window in the `rate_limit_hits`
table (`models.RateLimitHit`), so every task counts the same hits and the budget is one budget. It falls back
to an in-memory `RateLimiter` while the database is unreachable, so it is never the reason a public route
fails. Use `SharedRateLimiter` for a budget that must hold across the fleet; keep `RateLimiter` for a bound
that is fine per process (and for the fallback itself).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window counter per key, bounded in both dimensions.

    Each key's deque is pruned to the window on every call, and the key map itself is swept once per
    window: a key whose newest hit has aged out is dropped. Without the sweep the map only grows — the
    login routes key it on source IP. The sweep is amortized (not an O(keys) walk per request), so between
    sweeps the map holds at most the keys seen in the last two windows.

    Locked because `allow` runs on FastAPI's threadpool: unlocked, the read-modify-write on a key's deque
    loses updates (a caller exceeds its budget), and a sweep racing the zero-limit `del` raises `KeyError`.
    """

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._next_sweep = self.clock() + self.window
        self._lock = threading.Lock()

    @property
    def tracked_keys(self) -> int:
        """Number of keys currently held — what the eviction bound is asserted on."""
        with self._lock:
            return len(self._hits)

    def allow(self, key: str, *, limit: Optional[int] = None) -> bool:
        """Record a hit for `key` if its window has room. `limit` overrides the constructor's for this
        call, for a budget that differs per key (`app/org_quotas.py`, one org's own cap)."""
        return self._admit(key, record=True, limit=limit)

    def would_allow(self, key: str, *, limit: Optional[int] = None) -> bool:
        """`allow` without recording a hit: whether this window still has room for `key`."""
        return self._admit(key, record=False, limit=limit)

    def _admit(self, key: str, *, record: bool, limit: Optional[int] = None) -> bool:
        if limit is None:
            limit = self.limit
        now = self.clock()
        with self._lock:
            if now >= self._next_sweep:
                self._evict_idle(now)
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= limit:
                if not hits:
                    # A zero limit never records a hit, so never keep an empty deque for it either.
                    del self._hits[key]
                return False
            if record:
                hits.append(now)
            elif not hits:
                del self._hits[key]
            return True

    def _evict_idle(self, now: float) -> None:
        """Drop every key whose newest hit is older than the window (its deque would prune to empty).

        Caller holds `_lock`. Keyed on the newest hit, not the oldest: a key with one stale and one fresh
        hit is live, and evicting it would hand its caller a fresh budget mid-window.
        """
        for k, h in list(self._hits.items()):
            if not h or now - h[-1] > self.window:
                self._hits.pop(k, None)
        self._next_sweep = now + self.window


_SQL_LOCK = "SELECT pg_advisory_xact_lock(hashtext(:lockkey))"
_SQL_PRUNE = (
    "DELETE FROM rate_limit_hits WHERE bucket = :bucket AND key = :key "
    "AND hit_at < now() - make_interval(secs => :window)"
)
_SQL_COUNT = "SELECT count(*) FROM rate_limit_hits WHERE bucket = :bucket AND key = :key"
_SQL_HIT = "INSERT INTO rate_limit_hits (bucket, key, hit_at) VALUES (:bucket, :key, now())"


class SharedRateLimiter:
    """`RateLimiter`'s contract, counted in `rate_limit_hits` so every control-plane task shares one budget.

    `allow` is one short transaction on its own session: a transaction-scoped advisory lock on
    `ratelimit:<bucket>:<key>` (so two tasks admitting the same key at once cannot both read `limit - 1`),
    prune the key's hits older than the window, count, and insert the hit if under the limit. The window is
    measured by the database clock (`now()`), the one clock every task agrees on; `clock` is accepted for
    signature parity and drives only the fallback. A zero limit inserts nothing. `bucket` names the budget:
    two limiters over the same key space with different limits must not share rows.

    On ANY database failure — a blip, a failover, a missing table — `allow` answers from an internal
    in-memory `RateLimiter` with the same limit and window instead of raising. Failing closed would let a
    database hiccup lock every visitor out of a public route whose whole job is to answer; failing open
    would drop the budget exactly when the store is least able to absorb a flood. Degraded per task (N×
    budget) is the smallest loss: the bound still holds within this process. The fallback is logged once per
    process, not per hit, so a long outage cannot flood the log.

    The in-memory window is also kept AHEAD of the database, not only behind it: every request this process
    admits is recorded locally too, so the local count is a lower bound on the shared count, and a key the
    local window already refuses is refused without a transaction. A single source flooding a public route
    past its budget then costs the database nothing after its first `limit` hits; only a request that may be
    admitted pays the round trip. A many-source flood under every per-key limit still pays one short
    transaction per request, which is what a fleet-wide limiter costs.
    """

    def __init__(
        self, limit: int, window: float = 60.0, clock=time.monotonic, *, bucket: str
    ):
        self.limit = limit
        self.window = window
        self.clock = clock
        self.bucket = bucket
        self._fallback = RateLimiter(limit, window, clock)
        self._warned = False
        self._warn_lock = threading.Lock()

    def allow(self, key: str, *, limit: Optional[int] = None) -> bool:
        """`RateLimiter.allow`'s contract; `limit` overrides the constructor's for this call (a per-key
        budget), in the shared count and the fallback alike."""
        if limit is None:
            limit = self.limit
        # The local lower bound first: a key over budget here is over budget fleet-wide, no DB needed.
        if not self._fallback.would_allow(key, limit=limit):
            return False
        try:
            admitted = self._allow_shared(key, limit)
        except Exception as exc:  # noqa: BLE001 — every DB failure degrades to the in-memory bound
            self._warn_once(exc)
            return self._fallback.allow(key, limit=limit)
        if admitted:
            self._fallback.allow(key, limit=limit)
        return admitted

    def _allow_shared(self, key: str, limit: int) -> bool:
        from sqlalchemy import text

        from app.db import SessionLocal  # lazy: the module stays importable without an engine

        params = {"bucket": self.bucket, "key": key, "window": float(self.window)}
        with SessionLocal() as session, session.begin():
            session.execute(text(_SQL_LOCK), {"lockkey": f"ratelimit:{self.bucket}:{key}"})
            session.execute(text(_SQL_PRUNE), params)
            count = session.execute(text(_SQL_COUNT), params).scalar_one()
            if count >= limit:
                return False
            session.execute(text(_SQL_HIT), params)
            return True

    def _warn_once(self, exc: Exception) -> None:
        with self._warn_lock:
            if self._warned:
                return
            self._warned = True
        log.warning(
            "shared rate limiter %r: database unavailable (%s: %s); counting in-process while it stays "
            "so — the budget is per task, not fleet-wide, meanwhile (the next call tries the database "
            "again; this is logged once per process)",
            self.bucket, type(exc).__name__, exc,
        )
