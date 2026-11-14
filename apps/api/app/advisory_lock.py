"""A session-level `pg_try_advisory_lock` on its own dedicated connection — what `cron_runtime.RunLock` and
`routes/tasks/answers.py::_AnswerLock` are, spelled once.

Session-scoped so a dying process frees the lock (no lease expiry to tune). Its own connection because a
pooled connection carrying a session lock hands it to the next checkout, and because a request session that
commits mid-block would end a transaction-level lock early. Not the schema lock (`migrate.serialized`):
that one WAITS, this one refuses. Each holder keeps its own `classid`, so the lock spaces stay apart.

REUSED LOCK CONNECTIONS. Dialing one (TLS and authentication) per acquire was the control plane's largest
single CPU cost: every cron tick takes a run lock. A connection that holds NO lock goes back to a small idle
list (`_IDLE_MAX`) and the next acquire reuses it: one that lost its try (it never held the lock) or whose
unlock Postgres confirmed. One whose unlock failed or did not confirm is closed, so a reused connection never
carries a lock into the next holder (a session advisory lock is re-entrant, and a carried one would make the
next try succeed for the wrong holder). Kept at most `_IDLE_SECONDS`, so a rotated credential or a server-side
idle close costs one retry at most: a reused connection that fails its try is closed and the try is dialed
fresh once.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import psycopg

from app import db_credentials

log = logging.getLogger(__name__)

# Keyed by an int the caller holds, or by a name Postgres hashes (`hashtext`) so no caller reproduces the hash;
# both return the int key, which the unlock needs.
_TRY_LOCK_INT_SQL = "SELECT pg_try_advisory_lock(%(classid)s, %(key)s) AS locked, %(key)s AS k"
_TRY_LOCK_NAME_SQL = (
    "SELECT pg_try_advisory_lock(%(classid)s, k) AS locked, k "
    "FROM (SELECT hashtext(%(key)s) AS k) AS keyed"
)


#: Idle lock connections kept for reuse, per process. A handful covers the crons a tick fires together.
_IDLE_MAX = 8
#: How long an idle lock connection is kept (from its dial), so a credential rotation or a server-side idle
#: timeout retires it rather than failing a try.
_IDLE_SECONDS = 600.0
#: libpq TCP bounds on every lock connection: a reused one whose peer vanished (a failover's old primary) fails
#: its try within seconds and is redialed, rather than hanging on the kernel's retransmit timeout.
_TCP_BOUNDS = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3,
               "tcp_user_timeout": 10_000}
_idle: list[tuple[psycopg.Connection, float]] = []
_idle_guard = threading.Lock()


def _reusable(conn: psycopg.Connection, dialed_at: float, now: float) -> bool:
    return (not conn.closed and now - dialed_at < _IDLE_SECONDS
            and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE)


def _take_idle() -> Optional[tuple[psycopg.Connection, float]]:
    """A reusable idle lock connection, or None; expired ones met on the way are closed."""
    now = time.monotonic()
    found, stale = None, []
    with _idle_guard:
        while _idle:
            conn, dialed_at = _idle.pop()
            if _reusable(conn, dialed_at, now):
                found = (conn, dialed_at)
                break
            stale.append(conn)
    for conn in stale:
        _close_quietly(conn)
    return found


def _give_back(conn: psycopg.Connection, dialed_at: float) -> None:
    """Keep a connection that holds no lock for the next acquire, or close it when it is spent or the list full."""
    if _reusable(conn, dialed_at, time.monotonic()):
        with _idle_guard:
            if len(_idle) < _IDLE_MAX:
                _idle.append((conn, dialed_at))
                return
    _close_quietly(conn)


def drain_idle() -> None:
    """Close every idle lock connection: for a test that swaps the dialer, so its next acquire dials."""
    with _idle_guard:
        conns = [conn for conn, _ in _idle]
        _idle.clear()
    for conn in conns:
        _close_quietly(conn)


def _close_quietly(conn: psycopg.Connection) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 — closing frees whatever it held
        pass


class SessionLock:
    """One try-lock on `(classid, key)`. `label` names the holder in the unlock-failure log line;
    `connect_timeout` bounds the dial when the holder has no other bound yet."""

    def __init__(
        self, classid: int, key: int | str, *, label: str, connect_timeout: Optional[float] = None
    ) -> None:
        self.classid = classid
        self.key = key
        self.label = label
        self._connect_timeout = connect_timeout
        self._conn: Optional[psycopg.Connection] = None
        self._key: Optional[int] = None
        self._dialed_at = 0.0
        self._guard = threading.Lock()

    def acquire(self) -> bool:
        """True iff this process now holds the lock. False means another holder of the same key is live."""
        reused = _take_idle()
        if reused is not None:
            conn, dialed_at = reused
            try:
                return self._try(conn, dialed_at)
            except Exception as exc:  # noqa: BLE001 — a dead idle connection: dial fresh once
                log.debug("%s: a reused lock connection failed (%s); dialing a fresh one", self.label, exc)
        # Through `db_credentials`, not `psycopg` directly: a raw dialer outside the `do_connect`
        # chokepoint, so the rotating RDS credential is injected here.
        options: dict[str, object] = dict(_TCP_BOUNDS)
        if self._connect_timeout is not None:
            options["connect_timeout"] = self._connect_timeout
        conn = db_credentials.psycopg_connect(autocommit=True, **options)
        return self._try(conn, time.monotonic())

    def _try(self, conn: psycopg.Connection, dialed_at: float) -> bool:
        """One try-lock on `conn`. Held: kept until `release`. Refused: back to the idle list (it holds nothing).
        A failure closes `conn` and raises."""
        try:
            with conn.cursor() as cur:
                sql = _TRY_LOCK_NAME_SQL if isinstance(self.key, str) else _TRY_LOCK_INT_SQL
                cur.execute(sql, {"classid": self.classid, "key": self.key})
                locked, key = cur.fetchone()
        except Exception:
            _close_quietly(conn)
            raise
        if not locked:
            _give_back(conn, dialed_at)
            return False
        self._conn, self._key, self._dialed_at = conn, key, dialed_at
        return True

    def release(self) -> None:
        """Unlock, then keep the connection for reuse only when Postgres confirmed the unlock (else close it,
        which frees the lock regardless). Idempotent and never raises: it runs from a `finally` and a future's
        done-callback, and a raise would lose the close as well as the unlock."""
        with self._guard:
            conn, key, dialed_at = self._conn, self._key, self._dialed_at
            self._conn, self._key = None, None
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", (self.classid, key))
                row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 — closing the connection frees the lock regardless
            log.warning("%s: advisory unlock failed (%s); closing the connection", self.label, exc)
            _close_quietly(conn)
            return
        if row is not None and row[0] is True:
            _give_back(conn, dialed_at)
        else:
            _close_quietly(conn)
