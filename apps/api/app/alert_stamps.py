"""Durable one-shot-alert stamps for the Task pipeline.

Two alerts fire exactly once per spell, re-armed when the condition clears: the stuck-epic alert and
the needs-attention entry-notify. Stamps live in `system_state` (an app-level KV row), not on disk —
the control plane's container filesystem dies with each deploy — and not on `tasks`, since a stamp is a
fact about what the sweep told the operator, not about the task.

The needs-attention stamp has two writers (this sweep and `app/task_notify.py`), hence `claim`; see
`StampStore`. The store is injected so the DB-free `apps/api/test_*.py` suites use `MemoryStampStore`;
the DB path is covered by the API test suite.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Protocol

log = logging.getLogger("cockpit-scheduler")

# How far `DbStampStore.retain` backdates a caller's `stamped_before` — see the comment there.
_CLOCK_SKEW_MARGIN = timedelta(seconds=5)

# Every key this module writes starts here, so a re-arm scan can find its own family and nothing else's.
KEY_PREFIX = "pipeline:stamp:"


def key_for(family: str, ident: str | int) -> str:
    """The `system_state.key` for one stamp. `family` separates the two alert kinds; `ident` is the thing
    alerted about (an epic id, a task id) — never a composite that has to be parsed back apart."""
    return f"{KEY_PREFIX}{family}:{ident}"


class StampStore(Protocol):
    """Five operations. `seen`/`mark`/`retain` are "exactly once per spell" for a single writer;
    `claim`/`release` give the same guarantee when two producers can announce the same condition.

    A `seen`-then-`mark` pair cannot arbitrate two producers (both read "not yet alerted"), so `claim`
    does the read-and-write as one statement. A claimant that fails to deliver calls `release`, so the
    other producer or the next tick still announces the condition.
    """

    def seen(self, key: str) -> bool: ...

    def seen_many(self, keys: Iterable[str]) -> set[str]:
        """The subset of `keys` already stamped: `seen` for a whole sweep in one read."""

    def mark(self, key: str) -> None: ...

    def claim(self, key: str) -> bool:
        """Atomically stamp `key` and report whether THIS caller is the one that stamped it.

        True ⇒ you own the announcement, go alert. False ⇒ someone already did; stay silent."""

    def release(self, key: str) -> None:
        """Give back a claim whose alert was never delivered, so the condition is announced again."""

    def retain(
        self, family: str, keys: Iterable[str], *, stamped_before: datetime | None = None
    ) -> None:
        """Drop every stamp in `family` that is NOT in `keys` — the re-arm.

        `stamped_before` spares a stamp written at or after that instant: `keys` is a snapshot, and
        pruning a stamp claimed after it re-arms the backstop behind an alert already sent. Pass the
        snapshot's own time."""


class MemoryStampStore:
    """A dict. For tests, and for a caller that deliberately wants no durability."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.stamped_at: dict[str, datetime] = {}

    def seen(self, key: str) -> bool:
        return key in self.keys

    def seen_many(self, keys: Iterable[str]) -> set[str]:
        return set(keys) & self.keys

    def mark(self, key: str) -> None:
        self.stamped_at.setdefault(key, datetime.now(timezone.utc))
        self.keys.add(key)

    def claim(self, key: str) -> bool:
        if key in self.keys:
            return False
        self.mark(key)
        return True

    def release(self, key: str) -> None:
        self.keys.discard(key)
        self.stamped_at.pop(key, None)

    def retain(
        self, family: str, keys: Iterable[str], *, stamped_before: datetime | None = None
    ) -> None:
        prefix = f"{KEY_PREFIX}{family}:"
        keep = set(keys)

        def dropped(key: str) -> bool:
            if not key.startswith(prefix) or key in keep:
                return False
            at = self.stamped_at.get(key)
            return stamped_before is None or at is None or at < stamped_before

        for key in [k for k in self.keys if dropped(k)]:
            self.release(key)


class DbStampStore:
    """`system_state`-backed, best effort, failing toward a duplicate alert rather than a silent one.

    A failed read returns False ("not yet alerted"); a failed write is logged and swallowed, so a
    delivered alert is never rolled back and the next tick may repeat it.
    """

    def __init__(self, session_factory=None) -> None:
        # Resolved on first use: `app/task_pipeline.py`'s hermetic suites import this module on a host
        # with no sqlalchemy, so the DB stack is imported only inside methods.
        self._session_factory = session_factory

    def _sessions(self):
        if self._session_factory is None:
            from app.db import SessionLocal

            self._session_factory = SessionLocal
        return self._session_factory

    def seen(self, key: str) -> bool:
        try:
            from sqlalchemy import text

            with self._sessions()() as session:
                row = session.execute(
                    text("SELECT 1 FROM system_state WHERE key = :key"), {"key": key}
                ).first()
                return row is not None
        except Exception as exc:  # noqa: BLE001 — an unreadable stamp must not suppress an alert
            log.warning("alert-stamps: read failed for %s (%s) — treating as not yet alerted", key, exc)
            return False

    def seen_many(self, keys: Iterable[str]) -> set[str]:
        """One statement for a sweep's worth of keys, where `seen` per key cost a session and a round trip
        each (4.6% of the api's CPU in `sweep_needs_attention_notify`). Fails open like `seen`:
        an unreadable batch answers "none seen", so `claim` still arbitrates and nothing goes silent."""
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return set()
        try:
            from sqlalchemy import text

            with self._sessions()() as session:
                rows = session.execute(
                    text("SELECT key FROM system_state WHERE key = ANY(:keys)"), {"keys": wanted}
                ).scalars()
                return set(rows)
        except Exception as exc:  # noqa: BLE001 — an unreadable stamp must not suppress an alert
            log.warning("alert-stamps: batch read of %d stamps failed (%s) — treating as not yet alerted",
                        len(wanted), exc)
            return set()

    def _execute(self, sql: str, params: dict) -> None:
        """One statement, committed, in a fresh session. Raises; each writer decides how loud."""
        from sqlalchemy import text

        with self._sessions()() as session:
            session.execute(text(sql), params)
            session.commit()

    def mark(self, key: str) -> None:
        try:
            self._execute(
                "INSERT INTO system_state (key, value) VALUES (:key, '{}'::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = system_state.value",
                {"key": key},
            )
        except Exception as exc:  # noqa: BLE001 — never roll back an alert that was already delivered
            log.warning("alert-stamps: could not persist %s (%s) — it may re-alert next tick", key, exc)

    def claim(self, key: str) -> bool:
        """One INSERT, so the read and the write cannot be interleaved by the other producer.

        `ON CONFLICT DO NOTHING ... RETURNING` makes the row the answer: returned ⇒ this caller owns
        the announcement. Fails open like `seen`: a DB error answers True, risking a duplicate rather
        than silence."""
        try:
            from sqlalchemy import text

            with self._sessions()() as session:
                row = session.execute(
                    text(
                        "INSERT INTO system_state (key, value) VALUES (:key, '{}'::jsonb) "
                        "ON CONFLICT (key) DO NOTHING RETURNING key"
                    ),
                    {"key": key},
                ).first()
                session.commit()
                return row is not None
        except Exception as exc:  # noqa: BLE001 — an unusable stamp store must not suppress an alert
            log.warning("alert-stamps: claim failed for %s (%s) — alerting anyway", key, exc)
            return True

    def release(self, key: str) -> None:
        """Hand back a claim whose alert was not delivered. Loud on failure: a leftover stamp keeps the
        condition unannounced until it clears."""
        try:
            self._execute("DELETE FROM system_state WHERE key = :key", {"key": key})
        except Exception as exc:  # noqa: BLE001 — see the docstring: this is the silent direction
            log.warning("alert-stamps: could not release the claim on %s (%s) — the undelivered alert "
                        "for it may stay silent until the condition clears", key, exc)

    def retain(
        self, family: str, keys: Iterable[str], *, stamped_before: datetime | None = None
    ) -> None:
        keep = list(set(keys))
        prefix = f"{KEY_PREFIX}{family}:"
        try:
            # `updated_at` is the INSERT time on the database's clock; `stamped_before` is the caller's,
            # so it is backdated by a skew margin. Cost: a cleared stamp inside it re-arms a pass later.
            sql = "DELETE FROM system_state WHERE key LIKE :prefix || '%'"
            params: dict = {"prefix": prefix}
            if keep:
                sql += " AND key <> ALL(:keep)"
                params["keep"] = keep
            if stamped_before is not None:
                sql += " AND updated_at < :before"
                params["before"] = stamped_before - _CLOCK_SKEW_MARGIN
            self._execute(sql, params)
        except Exception as exc:  # noqa: BLE001 — a failed re-arm leaves a stamp, which suppresses one
            # future alert for a condition that recurred. Loud, because that is the silent direction.
            log.warning("alert-stamps: re-arm failed for family %s (%s) — a recurrence may stay silent",
                        family, exc)


_default_store: StampStore | None = None


def default_store() -> StampStore:
    """The process-wide DB store, built on first use so importing this module needs no database."""
    global _default_store
    if _default_store is None:
        _default_store = DbStampStore()
    return _default_store
