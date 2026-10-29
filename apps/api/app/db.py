"""Engine + session plumbing (SQLAlchemy 2 sync, psycopg3)."""
from __future__ import annotations

import collections
import functools
import inspect
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app import after_commit, db_credentials
from app.config import db_pool_sizes

log = logging.getLogger(__name__)

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


_checkout_depth = threading.local()


class WatchedQueuePool(QueuePool):
    """A pool telling `app/pool_watch.py` when a checkout times out — the signal that a saturated pool is
    failing checkouts rather than turning over (`pool_watch.starved`), counted under the pool's `lane`.
    SQLAlchemy has no event for it. Counted at the outermost `_do_get` only: QueuePool re-enters it within one
    checkout. The lane is a class attribute, not an instance one, because `Pool.recreate()` (after a failover)
    builds a fresh instance of the same class."""

    lane = "request"

    def _do_get(self):  # type: ignore[no-untyped-def]
        depth = getattr(_checkout_depth, "n", 0)
        _checkout_depth.n = depth + 1
        try:
            return self._checkout(outermost=depth == 0)
        except sa_exc.TimeoutError:
            if depth == 0:
                from app import pool_watch

                pool_watch.count_checkout_timeout(self.lane)
            raise
        finally:
            _checkout_depth.n = depth

    def _checkout(self, *, outermost: bool):  # type: ignore[no-untyped-def]
        """The checkout itself; a lane with its own admission rule (`ReservedQueuePool`) overrides it."""
        return super()._do_get()


class IdentityQueuePool(WatchedQueuePool):
    lane = "identity"


def _pool_kwargs() -> dict:
    """The request pool: the CP runs exactly one uvicorn worker (enforced in `entrypoint.sh` and `create_app()`).

    Not the process's whole ceiling: the identity and background lanes below each hold their own small pool,
    DBOS builds another engine (`DBOS_POOL_SIZE`, `app/durable/runtime.py`) and a few raw dialers sit outside
    all of them. The control plane AUTOSCALES its task count (the deployment's ECS autoscaling),
    and a deploy's overlap doubles it, so the per-task ceiling is an ENVIRONMENT decision: `DB_POOL_SIZE` /
    `DB_MAX_OVERFLOW`, `DB_IDENTITY_POOL_SIZE`, `DB_BACKGROUND_POOL_SIZE` (read by
    `app/config.py::db_pool_sizes`), set per environment in its tfvars against its RDS class's
    `max_connections` (the deployment's tests assert that budget). Unset — the local
    stack — keeps 20 + 20. A ceiling, not a preallocation: a task idles far below it.

    The request threadpool is sized to this capacity, and one request draws at most ONE connection from it
    (its route session): the identity reads that used to take a second (the principal, a node token) have
    their own lane, so no request can hold one connection here while waiting for another (a hold-and-wait
    deadlock). A pool saturated for longer than a burst fails the health probe and dumps every
    thread's stack (`app/pool_watch.py`).

    `pool_timeout` is set against the ALB's 15s kill window (5s interval, 3 misses). It bounds one
    checkout, not a request, and a 10s wait can still span three probe instants; only a value under 10.0
    makes "at most two missed probes" true."""
    sizes = db_pool_sizes()
    return {
        "poolclass": WatchedQueuePool,
        "pool_size": sizes.pool_size,
        "max_overflow": sizes.max_overflow,
        "pool_timeout": 10.0,
    }


def _lane_pool_kwargs(size: int, timeout: float, poolclass: type) -> dict:
    """A side lane's pool: fixed at `size`, no overflow. A `WatchedQueuePool` of its own lane: `pool_watch` counts
    its timeouts and dumps it when it starves, while its health verdict stays about the request pool alone."""
    return {"poolclass": poolclass, "pool_size": size, "max_overflow": 0, "pool_timeout": timeout}


# Background connections only a NESTED session may take: one opened by a thread already holding a connection
# of any lane (a request's nested read, a dispatch worker's settings read).
BACKGROUND_NESTED_RESERVE = 3
_OUTER = "outer"


class ReservedQueuePool(WatchedQueuePool):
    """The background pool: an OUTER checkout (the thread holds no connection of any lane) first takes one of
    `size - BACKGROUND_NESTED_RESERVE` permits, so a full fan-out of outer holders — lease-drain's workers —
    always leaves a nested checkout a connection: neither a worker's own settings read waits on its siblings,
    nor a request's nested read on a launch burst. A pool smaller than the reserve plus one keeps a single
    outer permit."""

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._outer_permits = threading.BoundedSemaphore(max(1, self.size() - BACKGROUND_NESTED_RESERVE))

    lane = "background"

    def _checkout(self, *, outermost: bool):  # type: ignore[no-untyped-def]
        outer = outermost and not any(holds_connection(lane) for lane in ENGINES)
        if outer and not self._outer_permits.acquire(timeout=self._timeout):
            raise sa_exc.TimeoutError(
                f"background pool: no outer connection free within {self._timeout:.0f}s "
                f"({BACKGROUND_NESTED_RESERVE} of {self.size()} are reserved for nested sessions)"
            )
        try:
            record = super()._checkout(outermost=outermost)
        except BaseException:
            if outer:
                self._outer_permits.release()
            raise
        if outer:
            record.info[_OUTER] = True
        return record

    def _return_outer_permit(self, connection_record) -> None:  # type: ignore[no-untyped-def]
        if connection_record is not None and connection_record.info.pop(_OUTER, False):
            self._outer_permits.release()



engine = create_engine(database_url(), hide_parameters=True, **_pool_kwargs())

# ── Lanes: who may wait on whom ──────────────────────────────────────────────────────────────────────
# Three pools, so no request-pool connection is ever held by a thread waiting for another:
# * REQUEST (`engine`): ONE connection per request, its route session, bounded by the request threadpool.
# * IDENTITY (`identity_engine`): who is asking — the principal, a node token, a cookie's row — taken by
#   `IdentitySessionLocal` explicitly and by nothing else, so slow work can never fail authentication.
# * BACKGROUND (`background_engine`): work outside the request bound — lease-drain's dispatch workers, cron
#   firings, `asyncio.to_thread` callers. A thread joins it (`background_lane`, `background_executor`) and
#   every `SessionLocal()` it opens binds here, so a launch burst exhausts its own pool, never the requests'.
#   Also every `SessionLocal()` a thread opens while it already holds a request connection (a handler's
#   nested settings or trail read), moved here by `_LanedSession`: never the request pool's second checkout.
_sizes = db_pool_sizes()
# The request pool's checkout bound: an identity read is on a request's path.
identity_engine = create_engine(
    database_url(), hide_parameters=True,
    **_lane_pool_kwargs(_sizes.identity_pool_size, 10.0, poolclass=IdentityQueuePool),
)
# Every lock wait on the background lane is bounded. A cron firing holds its run lock while its body
# lives (`cron_runtime._bounded_call`), so one body parked behind a row lock that is never released would stop
# that cron for good, and every other cron queued behind the same row with it (lease-drain, task-pipeline and
# trigger intake, 2026-10-19). Bounded, the wait raises `LockNotAvailable`, the sweep that waited is contained and
# retried next pass. Well under lease-drain's own 60s bound; a healthy wait on this lane is milliseconds.
BACKGROUND_LOCK_TIMEOUT_MS = 15_000

# Longer: no probe waits on it, and a dispatch worker queueing out a launch burst beats one failing it.
background_engine = create_engine(
    database_url(), hide_parameters=True,
    connect_args={"options": f"-c lock_timeout={BACKGROUND_LOCK_TIMEOUT_MS}"},
    **_lane_pool_kwargs(_sizes.background_pool_size, 30.0, poolclass=ReservedQueuePool),
)
ENGINES = (engine, identity_engine, background_engine)

_lane = threading.local()
_HOLDER = "holder"
_held_lock = threading.Lock()
# (engine, thread ident) -> connections that thread holds from that engine.
_held: dict[tuple[int, int], int] = {}


def _track_holders(tracked) -> None:  # type: ignore[no-untyped-def]
    def note_checkout(dbapi_connection, connection_record, connection_proxy) -> None:  # type: ignore[no-untyped-def]
        key = (id(tracked), threading.get_ident())
        connection_record.info[_HOLDER] = key
        with _held_lock:
            _held[key] = _held.get(key, 0) + 1

    def note_return(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
        """Checkin or detach. Keyed on the record's holder, not the calling thread: a route session is closed
        by FastAPI's teardown thread, not the thread that checked it out."""
        if isinstance(tracked.pool, ReservedQueuePool):
            tracked.pool._return_outer_permit(connection_record)
        drop_holder(connection_record)

    def note_invalidate(dbapi_connection, connection_record, exception) -> None:  # type: ignore[no-untyped-def]
        """An invalidated record forgets its holder: a checkout listener's `InvalidatePoolError` (the idle
        ping) makes SQLAlchemy reconnect the SAME record — clearing its `info` — and fire `checkout` again, so
        without this the thread counted two holds and its one checkin returned one. A record
        invalidated mid-use is checked in later with no holder, so nothing is returned twice."""
        drop_holder(connection_record)

    def drop_holder(connection_record) -> None:  # type: ignore[no-untyped-def]
        key = connection_record.info.pop(_HOLDER, None) if connection_record is not None else None
        if key is None:
            return
        with _held_lock:
            left = _held.get(key, 0) - 1
            if left > 0:
                _held[key] = left
            else:
                _held.pop(key, None)

    event.listen(tracked, "checkout", note_checkout)
    event.listen(tracked, "checkin", note_return)
    event.listen(tracked, "detach", note_return)
    event.listen(tracked, "invalidate", note_invalidate)


for _engine in ENGINES:
    _track_holders(_engine)


def holds_connection(of) -> bool:  # type: ignore[no-untyped-def]
    """Whether the calling thread holds a connection of engine `of` right now."""
    with _held_lock:
        return (id(of), threading.get_ident()) in _held


def in_background_lane() -> bool:
    return getattr(_lane, "background", False)


def enter_background_lane() -> None:
    """Put the calling thread in the background lane for its life — a `ThreadPoolExecutor` initializer."""
    _lane.background = True


@contextmanager
def background_lane():
    """The calling thread is in the background lane for this block (a thread, not a context: work handed to
    an executor or `asyncio.to_thread` runs on that pool's own thread, whose lane its executor set)."""
    prior = in_background_lane()
    _lane.background = True
    try:
        yield
    finally:
        _lane.background = prior


def background_executor(max_workers: int, thread_name_prefix: str) -> ThreadPoolExecutor:
    """A thread pool whose every worker is in the background lane."""
    return ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix=thread_name_prefix, initializer=enter_background_lane
    )


class _LanedSession(Session):
    """Picks its lane once, at construction, so one session never spans two pools: the background engine on
    a background-lane thread or one already holding a request or identity connection, else the request
    engine. A route session (`get_session`) is never moved: its thread may still be marked as holding a
    connection whose request has gone off-thread. Only a session about to bind the request engine is moved:
    an explicit other bind (a test harness, a side lane) is the caller's choice."""

    def __init__(self, bind=None, *, route: bool = False, **kwargs):  # type: ignore[no-untyped-def]
        if bind is engine and not route and (
            in_background_lane() or holds_connection(engine) or holds_connection(identity_engine)
        ):
            bind = background_engine
        super().__init__(bind=bind, **kwargs)


SessionLocal = sessionmaker(bind=engine, class_=_LanedSession, expire_on_commit=False)
IdentitySessionLocal = sessionmaker(bind=identity_engine, expire_on_commit=False)
SESSION_MAKERS = (SessionLocal, IdentitySessionLocal)

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
    at once), and hands out a fresh one. A connection never checked in yet (just opened) is not pinged."""
    checked_in_at = connection_record.info.get(_CHECKED_IN_AT)
    if checked_in_at is None or time.monotonic() - checked_in_at < PING_IF_IDLE_SECONDS:
        return
    try:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        # A read leaves the connection in a transaction under psycopg's non-autocommit default.
        dbapi_connection.rollback()
    except Exception as exc:  # noqa: BLE001 — any failure means this connection is not usable
        raise sa_exc.InvalidatePoolError(f"pooled connection failed its idle ping: {type(exc).__name__}") from exc


for _engine in ENGINES:
    event.listen(_engine, "checkin", _stamp_checkin)
    event.listen(_engine, "checkout", _ping_if_idle)


def get_session():
    """FastAPI dependency: one session per request, always on the request engine (`_LanedSession`)."""
    session: Session = SessionLocal(route=True)
    try:
        yield session
    finally:
        session.close()


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
    """Register one session listener on every app sessionmaker, so a lane never skips a seam."""
    for maker in SESSION_MAKERS:
        event.listen(maker, identifier, fn)


def _route_sessions(values) -> list[Session]:  # type: ignore[no-untyped-def]
    """The request-engine sessions among an endpoint's arguments: a `Session`, or one a `Caller` carries. An
    instance attribute only: `Request.session` is a property that asserts a middleware."""
    found = []
    for value in values:
        session = value if isinstance(value, Session) else getattr(value, "__dict__", {}).get("session")
        if isinstance(session, Session) and session.bind is engine and session not in found:
            found.append(session)
    return found


def release_after_endpoint(dependants) -> int:  # type: ignore[no-untyped-def]
    """Wrap every SYNC endpoint so its route session hands back a read-only connection the moment it returns,
    in its own thread (`release_connection`'s rules: a transaction that wrote keeps its connection). Takes the
    dependants requests are dispatched against (`app/route_inventory.py::dispatched_dependants`); returns how
    many were wrapped.

    Why: FastAPI validates a sync endpoint's response in ANOTHER threadpool call, so a route session would
    hold its connection while the request queues for that thread — and a burst fills the request pool with
    connections whose requests wait for threads that are all waiting for connections. What the
    response reads later re-checks out one connection in that thread, holding nothing else; loaded instances
    stay usable (`expire_on_commit=False`). An async endpoint validates inline and is left alone."""
    wrapped = 0
    for dependant in dependants:
        call = getattr(dependant, "call", None)
        if call is None or getattr(call, "_releases_route_connection", False):
            continue
        if inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call):
            continue
        dependant.call = _releasing(call)
        wrapped += 1
    return wrapped


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
        log.debug("release_after_endpoint: kept the route connection (%s)", type(exc).__name__)


def _releasing(call):  # type: ignore[no-untyped-def]
    @functools.wraps(call)
    def endpoint(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return call(*args, **kwargs)
        finally:
            for session in _route_sessions(kwargs.values()):
                release_route_connection(session)

    endpoint._releases_route_connection = True  # type: ignore[attr-defined]
    return endpoint


# ── Provenance: `tasks.updated_by_id` is stamped on every flush this app makes ─────────────────────
# Registered on the sessionmakers, not in `app/actors.py`, so it binds every app session regardless of
# import order, and on `SESSION_MAKERS` rather than `Session` so migration and test sessions are left alone.

# Imported at the bottom, after `SessionLocal` exists; `app.actors`'s import chain does not import
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

# ── The org read seam ──────────────────────────────────────────────────────────────────────────────
# Same registration convention and reason as the stamp above. `stamp_scope` records the scope on
# `after_begin`; `read_seam` applies the predicate on every ORM select. The shipped mode is enforcing
# (`seam.py`'s `_MODE`, pinned by a test): an out-of-scope read comes back
# short. Imported at the bottom; `app.tenancy.seam` does not import this module.
from app.tenancy.seam import read_seam, stamp_scope  # noqa: E402

_listen_sessions("after_begin", stamp_scope)
_listen_sessions("do_orm_execute", read_seam)
