"""Session plumbing: the sync face of the one async pool (`app/db_async.py`), the offline engine, and the listeners
every app session carries."""
from __future__ import annotations

import collections
import contextvars
import functools
import logging
import time
from contextlib import AbstractContextManager

from sqlalchemy import Table, alias, create_engine, event, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app import after_commit, db_credentials

log = logging.getLogger(__name__)


@functools.cache
def excluded_of(table: Table):
    """`pg_insert(table).excluded`, built once per table: the same `alias(table, name="excluded").columns` the
    dialect builds per statement, which costs hundreds of cyclic objects per call on the hot upserts. Immutable,
    so shared across threads. Pinned by `tests/test_db_excluded_of.py`."""
    return alias(table, name="excluded").columns

# The credential chokepoint, installed before any engine exists: one `do_connect` listener on the
# `Engine` class covers this engine, DBOS's and Alembic's; raw psycopg dialers use
# `db_credentials.psycopg_connect*`. Rationale in `app/db_credentials.py`. At import, not a startup hook,
# because `create_engine` below runs at import and `migrate.py` never starts the app.
db_credentials.install()


def database_url() -> str:
    """This app's engine URL, a thin re-export of `db_credentials.database_url` (the decision lives there).

    Kept because `migrations/env.py` and `app/durable/runtime.py::engine_config` import this name.
    """
    return db_credentials.database_url()


#: How deep the caller is in `_do_get`. A contextvar, not a thread-local: on the async pool every task shares the
#: loop thread, and a checkout waiting on its queue lets another task's checkout start (`app/db_async.py`).
_checkout_depth: contextvars.ContextVar[int] = contextvars.ContextVar("db_checkout_depth", default=0)


class WatchedPoolMixin:
    """A pool telling `app/pool_watch.py` when a checkout times out — the signal that a saturated pool is
    failing checkouts rather than turning over (`pool_watch.starved`), counted under the pool's `lane`.
    SQLAlchemy has no event for it. Counted at the outermost `_do_get` only: QueuePool re-enters it within one
    checkout. The lane is a class attribute, not an instance one, because `Pool.recreate()` (after a failover)
    builds a fresh instance of the same class. Mixed into the async pool's `AsyncAdaptedQueuePool`
    (`app/db_async.py`)."""

    lane = "async"  # `app/metrics_emf.py::POOL_LANE`, the one pool's EMF dimension value

    def _do_get(self):  # type: ignore[no-untyped-def]
        depth = _checkout_depth.get()
        token = _checkout_depth.set(depth + 1)
        try:
            return super()._do_get()  # type: ignore[misc]
        except sa_exc.TimeoutError:
            if depth == 0:
                from app import pool_watch

                pool_watch.count_checkout_timeout(self.lane)
            raise
        finally:
            _checkout_depth.reset(token)


# ── One pool per task, and sync ORM code on it ───────────────────────────────────────────────────────
# The control plane's ONE pool is the async engine's (`app/db_async.py`). Sync ORM code — the shared services a
# route runs through `run_sync`, cron bodies, lease-drain's workers, DBOS steps — opens `BridgedSession()`, which
# binds that pool's sync face (`db_async.sync_bind`): natively in a greenlet on the serving loop, through the
# bridge on any other thread while the loop runs. There is no sync pool of the control plane's own.
#
# Every lock wait on a bridged connection is bounded. A cron firing holds its run lock while its body lives
# (`cron_runtime._bounded_call`), so one body parked behind a row lock that is never released would stop that
# cron for good, and every other cron queued behind the same row with it (lease-drain, task-pipeline and trigger
# intake, 2026-10-19). Bounded, the wait raises `LockNotAvailable`, the sweep that waited is contained and retried
# next pass. Well under lease-drain's own 60s bound; a healthy wait is milliseconds.
BACKGROUND_LOCK_TIMEOUT_MS = 15_000


def _offline_pool_kwargs() -> dict:
    """The OFFLINE engine's pool: a throwaway process (`dev_bypass.is_fixture_env`: the local stack, the test suite,
    CI) keeps a small pool, because it opens thousands of short sessions; anywhere else nothing pools on it
    (`NullPool`), so it holds no connection at rest and adds nothing to the deployment's connection budget."""
    from app.dev_bypass import is_fixture_env

    if is_fixture_env():
        from app.config import DB_POOL_RECYCLE_SECONDS

        return {"pool_size": 5, "max_overflow": 15, "pool_timeout": 10.0, "pool_recycle": DB_POOL_RECYCLE_SECONDS}
    return {"poolclass": NullPool}


#: The engine of a process with NO serving loop — a script, a migration, a test without the app's lifespan, the
#: CP's own boot before its loop serves. Never the control plane's request or background path: while the serving
#: loop runs, every `BridgedSession()` binds the async pool (`_BridgeBoundSession`).
offline_engine = create_engine(
    database_url(), hide_parameters=True,
    # The bridged connections' bound lock wait (`BACKGROUND_LOCK_TIMEOUT_MS`), here too: a statement on it may run
    # on the loop's thread, where a lock wait that never ends would wedge the process.
    connect_args={"options": f"-c lock_timeout={BACKGROUND_LOCK_TIMEOUT_MS}"},
    **_offline_pool_kwargs(),
)

#: Sessions a serving process had to open on `offline_engine` (sync code on the loop thread outside a greenlet,
#: or a thread the loop itself blocks on: `db_async.run_unbridged`). Never reset; logged as they happen.
offline_while_serving = 0


def current_bind():  # type: ignore[no-untyped-def]
    """What sync ORM code binds right now: the async pool's sync face (`app/db_async.py::sync_bind`) whenever it
    can be reached, else `offline_engine`. A connection of its own (`current_bind().connect()`) is for a
    session-level advisory lock that must outlive the caller's commits."""
    global offline_while_serving
    from app import db_async

    bind = db_async.sync_bind()
    if bind is not None:
        return bind
    if db_async.serving_loop_running():
        offline_while_serving += 1
        log.warning("db: sync DB access off the async pool while the loop serves", stack_info=True)
    return offline_engine


class _BridgeBoundSession(Session):
    """Picks its bind once, at construction (`current_bind`). An explicit other bind (a test harness's own
    engine) is the caller's choice and is kept."""

    def __init__(self, bind=None, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(bind=bind if bind is not None else current_bind(), **kwargs)


#: The sync session factory: `with BridgedSession() as session:`.
BridgedSession = sessionmaker(class_=_BridgeBoundSession, expire_on_commit=False)


class AsyncBackedSession(Session):
    """The sync session inside every `AsyncSession` (`app/db_async.py`'s `sync_session_class`): the target the app
    session listeners below register on, so the async pool never skips a seam."""


#: Every target `_listen_sessions` registers on: the sync sessionmaker and the async sessions' sync class.
SESSION_TARGETS = (BridgedSession, AsyncBackedSession)


def every_org_session() -> AbstractContextManager[Session]:
    """A session for a sweep over EVERY org, run by a system cron: the operator plane's fleet session where
    Multi-Node Hosts exist (`vnodes.fleet_session`), else this edition's one plane, which the cron already holds
    (`cron_runtime`). The caller enters it (`with every_org_session() as session:`). Refused on a request:
    a request stays in the org it bound, so this never becomes a route's way around it."""
    from app import edition_features
    from app.tenancy import seam

    if seam.on_request_plane():
        raise RuntimeError("every_org_session is for system crons; a request must not detach from its org")
    if edition_features.enabled("multi_node_hosts"):
        from app import vnodes

        return vnodes.fleet_session()
    return BridgedSession()


# A pooled connection idle longer than this is pinged before reuse. SQLAlchemy's `pool_pre_ping` pings on
# EVERY checkout, and a console request checks out about three (its session, the principal's, the read
# trail's) — ~3% of the api's CPU on round trips. A connection a request just returned is not
# the one an RDS failover or idle reaper killed; one that sat idle is, and that one is still pinged.
PING_IF_IDLE_SECONDS = 30.0
_CHECKED_IN_AT = "checked_in_at"


def _stamp_checkin(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
    connection_record.info[_CHECKED_IN_AT] = time.monotonic()


def _ping_if_idle(dbapi_connection, connection_record, connection_proxy) -> None:  # type: ignore[no-untyped-def]
    """Ping a connection that sat idle. A dead one raises `InvalidatePoolError`, exactly as `pool_pre_ping`
    does: the pool discards it, recycles every connection older than the failure (a failover kills them all
    at once), and hands out a fresh one. A connection never checked in yet (just opened) is not pinged.

    The async engine (`app/db_async.py`) shares this listener: its `dbapi_connection` is SQLAlchemy's psycopg async
    adapter, whose cursor is not a context manager, so the cursor is closed explicitly (the listener runs in the
    checkout's greenlet, where the adapter's blocking-style calls work)."""
    checked_in_at = connection_record.info.get(_CHECKED_IN_AT)
    if checked_in_at is None or time.monotonic() - checked_in_at < PING_IF_IDLE_SECONDS:
        return
    try:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT 1")
        finally:
            cursor.close()
        # A read leaves the connection in a transaction under psycopg's non-autocommit default.
        dbapi_connection.rollback()
    except Exception as exc:  # noqa: BLE001 — any failure means this connection is not usable
        raise sa_exc.InvalidatePoolError(f"pooled connection failed its idle ping: {type(exc).__name__}") from exc


event.listen(offline_engine, "checkin", _stamp_checkin)
event.listen(offline_engine, "checkout", _ping_if_idle)


# An xid is assigned on a transaction's first write and never to a read-only one, so NULL means
# nothing written (see `release_connection`). Postgres 13+.
_XID_IF_WRITTEN = text("SELECT pg_current_xact_id_if_assigned()")


def release_connection(session: Session) -> bool:
    """Hand this session's pooled connection back before a long non-database wait. True if released.

    An autobegun read transaction pins its connection through any await, so a fan-out of such waits
    exhausts a pool sized for concurrent statements, taking down the whole control plane.

    Never touches a transaction that has written (returns False; the wait then pins as before). That
    takes two checks: `session.new`/`dirty`/`deleted` miss flushed and Core writes, so Postgres is asked
    (`pg_current_xact_id_if_assigned()`, NULL means read-only; Postgres 13+). The client-side check runs
    first because the probe query would autoflush pending ORM work. Savepoints are refused outright.

    Commits rather than rolls back: `expire_on_commit=False` keeps loaded instances usable. The xid cannot
    see `pg_advisory_xact_lock` or `SET LOCAL`, which the commit would drop; a caller holding either, or
    needing its pre- and post-wait work in one transaction, must not call this. The tenancy scope
    (`session.info`, `app/tenancy/seam.py`) survives the commit."""
    if not session.in_transaction():
        return False
    if session.in_nested_transaction():
        log.debug("release_connection declined: the caller is inside a savepoint")
        return False
    if session.new or session.dirty or session.deleted:
        log.debug("release_connection declined: the caller has unflushed ORM work")
        return False
    if session.scalar(_XID_IF_WRITTEN) is not None:
        log.debug("release_connection declined: this transaction has already written")
        return False
    session.commit()
    return True


def _listen_sessions(identifier: str, fn) -> None:  # type: ignore[no-untyped-def]
    """Register one session listener on every app session target, so a lane never skips a seam."""
    for target in SESSION_TARGETS:
        event.listen(target, identifier, fn)


#: Seconds each released route connection spent on `release_connection`'s round trips (the xid probe and the
#: COMMIT), newest last; `app/pool_watch.py` publishes their median once a minute and starts over. Bounded so a
#: process whose watch is off never grows it.
release_costs: "collections.deque[float]" = collections.deque(maxlen=10_000)


def release_route_connection(session: Session) -> None:
    """`release_connection` for a route session at a hop boundary: declines while after-commit work is owed,
    and a failure keeps the connection (until teardown) rather than failing the request. A release that went
    through records what its round trips cost (`release_costs`)."""
    # Work owed to a commit the endpoint never made (`app/after_commit.py`) is the close's rollback to drop.
    if after_commit.owed(session):
        return
    try:
        started = time.perf_counter()
        if release_connection(session):
            release_costs.append(time.perf_counter() - started)
    except sa_exc.SQLAlchemyError as exc:
        # A handler that caught a DB error without rolling back: the connection stays until teardown, as
        # before, and the response the handler chose still goes out.
        log.debug("release_route_connection: kept the route connection (%s)", type(exc).__name__)


# ── Provenance: `tasks.updated_by_id` is stamped on every flush this app makes ─────────────────────
# Registered on the sessionmakers, not in `app/actors.py`, so it binds every app session regardless of
# import order, and on `SESSION_TARGETS` rather than `Session` so migration and test sessions are left alone.

# Imported at the bottom, after `BridgedSession` exists; `app.actors`'s import chain does not import
# this module.

from app.actors import stamp_dirty_task_actors  # noqa: E402

_listen_sessions("before_flush", stamp_dirty_task_actors)

# ── The org write seam ─────────────────────────────────────────────────────────────────────────────
# Beside the actor stamp for the same two reasons; the listeners are independent (`updated_by_id`
# vs `org_id`). It enforces for ORM instances: under an org bind it stamps and refuses a different org.
# It does not refuse an unbound write (counted instead), never sees Core writes, and checks only a row's
# own `org_id` and its one census parent, not rows it points at. Homes: `app/tenancy/write_seam.py`,
# its bypass-site census test, and the unkeyed-reference register in `test_tenancy_composite_keys.py`.
from app.tenancy.write_seam import stamp_org  # noqa: E402

_listen_sessions("before_flush", stamp_org)

# ── The NodeVault invariant (`Credential.nodevault`) ───────────────────────────────────────────────
# Same registration convention. The flush-time backstop for a writer that skipped the route's 422
# (`credential_crypto.check_nodevault_invariant`); reads each envelope's `k`, never a key. ORM writes only,
# like every listener here: a Core or raw-SQL write to `credentials.config` is already banned
# (`tests/test_credential_crypto.py`), the key-rotation command aside, which never touches an `nv:` envelope.
from app.credential_crypto import refuse_broken_nodevault_invariant  # noqa: E402

_listen_sessions("before_flush", refuse_broken_nodevault_invariant)

# ── The system-project floor (`app/node_targets.py`) ───────────────────────────────────────────────
# Same registration convention. The flush-time backstop for the create routes' 422: no task lands in a system
# project without a node pin, whichever writer built it. ORM writes only, like every listener here.
from app.node_targets import refuse_unpinned_system_tasks  # noqa: E402

_listen_sessions("before_flush", refuse_unpinned_system_tasks)

# ── The account-roster nudge (`app/node_refresh.py`) ───────────────────────────────────────────────
# Same registration convention. A write to a field the node's account roster serves owes that org's nodes a
# `refresh` frame after commit. ORM writes only: a Core write (`fleet_observe`'s quota carry-forward) changes
# nothing a node reads, and a node's 300s roster poll covers any other.
from app.node_refresh import note_account_roster_changes  # noqa: E402

_listen_sessions("before_flush", note_account_roster_changes)

# ── The org read seam ──────────────────────────────────────────────────────────────────────────────
# Same registration convention and reason as the stamp above. `stamp_scope` records the scope on
# `after_begin`; `read_seam` applies the predicate on every ORM select. The shipped mode is enforcing
# (`seam.py`'s `_MODE`, pinned by a test): an out-of-scope read comes back
# short. Imported at the bottom; `app.tenancy.seam` does not import this module.
from app.tenancy.seam import read_seam, stamp_scope  # noqa: E402

_listen_sessions("after_begin", stamp_scope)
_listen_sessions("do_orm_execute", read_seam)
