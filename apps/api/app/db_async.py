"""Async DB access for the control plane: ONE pool per task, a connection held per statement, never across a
non-DB await. The home of that decision (ADR register: "Control-plane DB access is async").

THE POOL. One `AsyncEngine` (psycopg3 async) per process, created on the SERVING loop by the lifespan (`start`)
and sized by `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (`app/config.py::db_pool_sizes`, set per environment against RDS
`max_connections`; the deployment's connection-budget test counts it). SQLAlchemy forbids sharing a pooled async
engine across event loops, so a foreign loop never gets it: `async_engine()` raises off a loop thread, and raises
on another loop while a serving loop is registered (outside a test process). With no serving loop registered — a
script, a test without the app's lifespan — every loop shares one UNPOOLED engine (`NullPool`, the SQLAlchemy recipe
for several loops), so nothing pools across loops.

THE API (seam S1). `get_db`/`DbDep` give a route its `AsyncSession`; `async_session()` gives one to everything
else; `identity=True` is a LABEL (the same pool, no reservation), tagging the session for metrics. `release`
returns a read-only transaction's connection before a non-DB await. A request's own checkouts (`get_db`'s task)
are admitted first (`RequestAdmission`): requests never take the last `REQUEST_ADMISSION_RESERVE` connections, so a burst
queues there and background work still checks out. `run_in_session` runs existing sync ORM code
(`fn(session, ...)`) over the async connection (`AsyncSession.run_sync`: a greenlet, no thread). `run_lending` runs a body whose
shared services read on a session of their own (`side_session`: the operator plane's `vnodes.fleet_session`) on this
pool, handing the body's connection back for each. `run_on_loop`
is the bridge for code on a FOREIGN thread (a sync dependency in the threadpool, a DBOS step): it runs a coroutine
on the serving loop, in a copy of the caller's context (so a bound tenancy scope travels), and blocks that thread.

THE RULES.
 - A connection is checked out at a session's first statement and returned at commit, rollback or close. Never
   hold an open transaction across a non-DB await (node bus, HTTP, sleep): `await release(session)` or commit
   first. `tests/async_db_helpers.py::no_connection_across_awaits` fails a test that does.
 - A coroutine never holds two connections at once (the hold-and-wait deadlock).
   Holders are tracked per asyncio TASK through a contextvar (`_HOLD`; the checkout listener runs in SQLAlchemy's
   greenlet, which shares the task's context), and opening a session while the current task holds a connection is
   refused: it raises under test and on a local stack, and is logged and counted (`nested_refusals`) in production.
 - The tenancy seam applies unchanged: every `AsyncSession`'s sync session is an `app.db.AsyncBackedSession`, on
   which `app/db.py` registers the same listeners as on its sessionmakers (`read_seam`, `stamp_scope`, `stamp_org`,
   ...), and contextvars reach them (`tests/test_db_async.py` proves both).
 - `expire_on_commit=False`, as the sync sessions.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import inspect
import logging
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable, Iterator, Optional, TypeVar

from fastapi import Depends
from starlette.requests import HTTPConnection
from sqlalchemy import event
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from app import db, loop_watch
from app.config import DB_POOL_RECYCLE_SECONDS, REQUEST_ADMISSION_RESERVE, db_pool_sizes

log = logging.getLogger(__name__)

T = TypeVar("T")


def _disable_greenlet_referrer_scan() -> None:
    """Turn off greenlet's optional dead-thread cleanup on a free-threaded build.

    When a thread that ran greenlets exits (an anyio portal or worker thread, a bridge hop), greenlet frees its state
    later from a pending call on whichever thread runs next, and its optional cleanup calls `gc.get_referrers` on the
    dead thread's main greenlet, trusting exact refcounts that a free-threaded build splits per thread and merges
    lazily. That call SIGSEGVed an xdist worker (`~ThreadState` -> `gc.get_referrers` -> `_Py_MergeZeroLocalRefcount`
    -> greenlet dealloc). Skipping the scan only leaks, at most, one main greenlet per exited thread that had switched
    back to it by hand, which SQLAlchemy's `greenlet_spawn` never does. The build, not the live GIL, decides: refcounts
    stay split even with the GIL re-enabled.
    """
    import sysconfig

    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        return
    import greenlet

    greenlet.enable_optional_cleanup(False)


_disable_greenlet_referrer_scan()

#: `session.info` key naming the session's label (`async` or `identity`), for metrics and logs.
LANE_KEY = "lane"
ASYNC_LANE, IDENTITY_LABEL = "async", "identity"
#: A checkout's bound: under the ALB's 15s kill window (5s interval, 3 misses), and under 10s so a wait spans at most
#: two missed probes.
POOL_TIMEOUT_SECONDS = 10.0


#: The asyncio task whose checkouts are request-path ones: set by `get_db` for its request's task. A task of its own
#: (a bridge hop, a cron tick, a child task) is never the one recorded here, so its checkouts bypass admission.
_REQUEST_TASK: contextvars.ContextVar[Optional[asyncio.Task]] = contextvars.ContextVar("db_async_request_task",
                                                                                        default=None)


def request_admission_limit(pool_size: int, max_overflow: int) -> int:
    """How many connections request sessions may hold at once: the pool's ceiling less `REQUEST_ADMISSION_RESERVE`,
    never below one."""
    return max(1, pool_size + max_overflow - REQUEST_ADMISSION_RESERVE)


class RequestAdmission:
    """Keeps background headroom in the ONE pool: a request-path checkout (`get_db`'s task) first takes a slot of an
    asyncio semaphore sized `request_admission_limit`, and gives it back when its connection returns. Background
    checkouts (`run_on_loop`, the bridge, a cron's own task) bypass it, so a burst of requests queues here and a
    cron tick still gets one of the reserved connections. Mixed into the async pool; touched only on its loop."""

    _admission: Optional[asyncio.Semaphore] = None
    _admitted: Optional[set] = None
    _admission_loop: Optional[asyncio.AbstractEventLoop] = None

    def _admission_gate(self) -> asyncio.Semaphore:
        if self._admission is None:
            self._admission = asyncio.Semaphore(request_admission_limit(self.size(), self._max_overflow))  # type: ignore[attr-defined]
            self._admitted = set()
            self._admission_loop = asyncio.get_running_loop()
        return self._admission

    def _do_get(self):  # type: ignore[no-untyped-def]
        from sqlalchemy.util.concurrency import await_only, in_greenlet

        try:
            requested = _REQUEST_TASK.get() is asyncio.current_task() is not None
        except RuntimeError:  # no running loop: not a request's checkout
            requested = False
        # QueuePool re-enters `_do_get` within one checkout (`db.WatchedPoolMixin`): admitted once, at the outermost.
        if not requested or not in_greenlet() or db._checkout_depth.get() > 0:
            return super()._do_get()  # type: ignore[misc]
        gate = self._admission_gate()
        asked = time.perf_counter()
        try:
            await_only(asyncio.wait_for(gate.acquire(), self._timeout))  # type: ignore[attr-defined]
        except asyncio.TimeoutError:
            from app import pool_watch

            pool_watch.count_checkout_timeout(ASYNC_LANE)
            raise sa_exc.TimeoutError(
                f"request admission: no connection free for requests within {self._timeout:.0f}s "  # type: ignore[attr-defined]
                f"({REQUEST_ADMISSION_RESERVE} kept for background work, app/db_async.py)"
            ) from None
        finally:
            # Admitted, timed out or cancelled (a client gone, a relayed read cancelled): every wait counts.
            loop_watch.record_admission(time.perf_counter() - asked)
        try:
            record = super()._do_get()  # type: ignore[misc]
        except BaseException:
            gate.release()
            raise
        self._admitted.add(record)  # type: ignore[union-attr]
        return record

    def _do_return_conn(self, record):  # type: ignore[no-untyped-def]
        """Checkin and detach both come here: the admitted record's slot goes back with it."""
        try:
            super()._do_return_conn(record)  # type: ignore[misc]
        finally:
            if self._admitted is not None and record in self._admitted:
                self._admitted.discard(record)
                self._release_admission()

    def _release_admission(self) -> None:
        """The semaphore belongs to the loop it was made on: a return from any other thread (a garbage-collected
        connection's finalizer) hands the release to that loop."""
        gate, loop = self._admission, self._admission_loop
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is loop or loop is None:
            gate.release()  # type: ignore[union-attr]
        elif not loop.is_closed():
            loop.call_soon_threadsafe(gate.release)  # type: ignore[union-attr]


class WatchedAsyncQueuePool(RequestAdmission, db.WatchedPoolMixin, AsyncAdaptedQueuePool):
    """The async pool, admitting request checkouts through `RequestAdmission` and counting its timed-out checkouts
    under lane `async` (`app/pool_watch.py`)."""

    lane = ASYNC_LANE


class NestedSessionError(RuntimeError):
    """A task opened a session while it already held a connection (the module docstring's second rule)."""


class DisabledPoolError(RuntimeError):
    """The async pool is sized 0 for this process: the lane is disabled, never defaulted (`app/config.py`)."""


# ── The engines ──────────────────────────────────────────────────────────────────────────────────────────

_serving_loop: Optional[asyncio.AbstractEventLoop] = None
_pooled: Optional[AsyncEngine] = None
_unpooled: Optional[AsyncEngine] = None


def _strict() -> bool:
    """Whether a rule breach raises (a test process, a local stack) rather than being logged (production)."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or os.environ.get("APP_ENV", "local") != "production"


def _wire(engine: AsyncEngine) -> AsyncEngine:
    """The listeners every app engine carries (`app/db.py`): idle ping, checkin stamp, and the holder tracker."""
    sync_engine = engine.sync_engine
    event.listen(sync_engine, "checkin", db._stamp_checkin)
    event.listen(sync_engine, "checkout", db._ping_if_idle)
    event.listen(sync_engine, "checkout", _note_checkout)
    event.listen(sync_engine, "checkin", _note_return)
    event.listen(sync_engine, "detach", _note_return)
    event.listen(sync_engine, "invalidate", _note_invalidate)
    return engine


def _build_pooled() -> AsyncEngine:
    sizes = db_pool_sizes()
    if sizes.pool_size == 0:
        raise DisabledPoolError("DB_POOL_SIZE is 0: the async pool is disabled in this process")
    engine = _wire(create_async_engine(
        db.database_url(), hide_parameters=True, poolclass=WatchedAsyncQueuePool,
        pool_size=sizes.pool_size, max_overflow=sizes.max_overflow, pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_recycle=DB_POOL_RECYCLE_SECONDS,
    ))
    # Sync ORM code on a foreign thread reaches this pool (`sync_bind`): its connections await from anywhere.
    event.listen(engine.sync_engine, "connect", _adapt_for_threads)
    # The pool watch's checkin counter, wired before the engine is published: added later, on a pool already
    # serving, the listen mutates the listener deque while another thread's checkin iterates it ("deque mutated
    # during iteration" on free-threaded CPython).
    from app import pool_watch
    event.listen(engine.sync_engine, "checkin", pool_watch._count_checkin)
    return engine


def _running_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError(
            "async_engine() called off the event loop thread: database work runs on the serving loop "
            "(a CPU-offload thread, `app/offload.py::run_cpu`, must not touch the database; a foreign thread uses "
            "run_on_loop)"
        ) from None


def async_engine() -> AsyncEngine:
    """The engine for the calling loop: the process's one pooled engine on the serving loop. Raises off a loop
    thread, and on a foreign loop while a serving loop is registered (outside a test process); with none
    registered, every loop shares the unpooled engine (the module docstring)."""
    global _pooled, _unpooled
    loop = _running_loop()
    if _serving_loop is not None and loop is _serving_loop:
        if _pooled is None:
            _pooled = _build_pooled()
        return _pooled
    if _serving_loop is not None and _serving_loop.is_running() and not os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(
            "async_engine() called on a loop other than the serving loop: the pool belongs to one loop; a foreign "
            "thread reaches it through run_on_loop"
        )
    if db_pool_sizes().pool_size == 0:
        raise DisabledPoolError("DB_POOL_SIZE is 0: the async pool is disabled in this process")
    if _unpooled is None:
        _unpooled = _wire(create_async_engine(db.database_url(), hide_parameters=True, poolclass=NullPool))
    return _unpooled


def pooled_engine() -> Optional[AsyncEngine]:
    """The serving loop's pooled engine once built, else None — for `app/pool_watch.py`, which never builds it."""
    return _pooled


async def start() -> None:
    """Lifespan startup: register the running loop as the serving loop and build its pool here, so a sizing error
    fails the boot rather than the first request. A disabled pool (sized 0) registers the loop and builds nothing."""
    global _serving_loop, _pooled
    _serving_loop = asyncio.get_running_loop()
    if _pooled is None and db_pool_sizes().pool_size > 0:
        _pooled = _build_pooled()


async def stop() -> None:
    """Lifespan shutdown: dispose the pool and forget the loop — only this loop's (a test's second app, stopping,
    must not unregister the first's)."""
    global _serving_loop, _pooled
    if _serving_loop is not asyncio.get_running_loop():
        return
    # Under the hop lock: once it is released no foreign thread starts a hop to this loop, so none can race the
    # loop's close (a `call_soon_threadsafe` into a closing loop crashes the free-threaded interpreter).
    with _hop_lock:
        engine, _pooled, _serving_loop = _pooled, None, None
    if engine is not None:
        await engine.dispose()


# ── Holder tracking: per asyncio task, through a contextvar ─────────────────────────────────────────────────


@dataclass
class _Hold:
    task: Any
    count: int = 0


_HOLD: contextvars.ContextVar[Optional[_Hold]] = contextvars.ContextVar("db_async_hold", default=None)
_HOLDER = "async_holder"
#: Nested sessions refused-by-logging in production (the module docstring); never reset.
nested_refusals = 0


def _cell() -> _Hold:
    """The calling task's holder cell. A child task inherits its parent's context, so the cell is validated
    against the current task and replaced when it is not its own."""
    task = asyncio.current_task()
    cell = _HOLD.get()
    if cell is None or cell.task is not task:
        cell = _Hold(task)
        _HOLD.set(cell)
    return cell


def _note_checkout(dbapi_connection, connection_record, connection_proxy) -> None:  # type: ignore[no-untyped-def]
    """Engine `checkout` listener. Runs in SQLAlchemy's greenlet, which shares the checking-out task's context."""
    try:
        cell = _cell()
    except RuntimeError:  # no running loop: not an async checkout
        return
    cell.count += 1
    connection_record.info[_HOLDER] = cell


def _drop(connection_record) -> None:  # type: ignore[no-untyped-def]
    cell = connection_record.info.pop(_HOLDER, None) if connection_record is not None else None
    if cell is not None:
        cell.count = max(0, cell.count - 1)


def _note_return(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
    """Checkin or detach: keyed on the record's holder, not the caller (a close may run in another task)."""
    _drop(connection_record)


def _note_invalidate(dbapi_connection, connection_record, exception) -> None:  # type: ignore[no-untyped-def]
    """An invalidated record forgets its holder: a checkout listener's `InvalidatePoolError` (the idle ping) makes
    SQLAlchemy reconnect the SAME record and fire `checkout` again, so without this the task counted two holds."""
    _drop(connection_record)


def holds_connection() -> bool:
    """Whether the calling task holds a connection of an async engine right now."""
    cell = _HOLD.get()
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return False
    return cell is not None and cell.task is task and cell.count > 0


def _refuse_if_holding() -> None:
    global nested_refusals
    _cell()  # created in the task's own context before any checkout, so the listener finds it
    if not holds_connection():
        return
    message = (
        "a session was opened while this task already holds a connection: a coroutine never holds two "
        "(app/db_async.py) — use the session you have, or release it first"
    )
    if _strict():
        raise NestedSessionError(message)
    nested_refusals += 1
    log.warning("db_async: %s", message, stack_info=True)


# ── Sessions ───────────────────────────────────────────────────────────────────────────────────────────────

_maker = async_sessionmaker(class_=AsyncSession, sync_session_class=db.AsyncBackedSession, expire_on_commit=False)


@asynccontextmanager
async def async_session(*, identity: bool = False) -> AsyncIterator[AsyncSession]:
    """A session for non-request code and for identity reads (`identity=True`: a label, not a lane). Refused while
    the calling task already holds a connection."""
    _refuse_if_holding()
    session = _maker(bind=async_engine(), info={LANE_KEY: IDENTITY_LABEL if identity else ASYNC_LANE})
    try:
        yield session
    finally:
        await session.close()


async def get_db(conn: HTTPConnection) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: the route's `AsyncSession`. The auth and identity reads ahead of it in the dependency
    chain have completed and released by now, so this is the request's only connection. The session keeps the
    request's `state`, so its writes carry the actor the auth dependency resolved (`app/actors.py::_recorded_actor`)."""
    from app.actors import REQUEST_STATE_KEY

    # The request's task: its checkouts are admitted through `RequestAdmission`, keeping background headroom.
    _REQUEST_TASK.set(asyncio.current_task())
    async with async_session() as session:
        session.sync_session.info[REQUEST_STATE_KEY] = conn.state
        yield session


#: FastAPI tears dependencies down in reverse order of entry, and a sync dependency's teardown (`app/auth.py::
#: require_user`'s session) hops to a thread. Every async endpoint's connection is handed back (or, on a raise, rolled
#: back) before that teardown (`release_after_async_endpoint`), so its position among the dependencies is free;
#: declaring it last (`..., *, db: DbDep`) keeps even a connection that could not be handed back out of the hop.
DbDep = Annotated[AsyncSession, Depends(get_db)]


def on_session(
    fn: Callable[..., T] | None = None, *, side_reads: bool = False, deferring: bool = False,
) -> Callable[..., Any]:
    """`@on_session` on a sync `def handler(session: Session, ...)` makes it the route's endpoint: an `async def`
    taking `db: DbDep` in `session`'s place (every other parameter kept, so FastAPI resolves them as declared) that
    runs the body over the route's connection (`AsyncSession.run_sync`, a greenlet, no thread). The one shape a
    route body takes when its whole work is ORM reads and writes; a handler with a non-DB await stays `async def`.
    `side_reads=True` runs it through `run_lending`, for a body whose shared services read on a session of their
    own (`side_session`: the operator plane's `vnodes.fleet_session`). `deferring=True` runs it through
    `run_deferring`, for a body that queues post-commit legs with `defer_off_loop`; not with `side_reads`.
    The body is the endpoint's `session_body` (and `__wrapped__`); a caller composing handlers in-process
    (`routes/task_page.py`) runs that on its own session. `__annotations__` are the body's (`wraps` copies
    them) and still name `session`; FastAPI reads `__signature__`, which is the endpoint's."""
    if fn is None:
        return functools.partial(on_session, side_reads=side_reads, deferring=deferring)
    if side_reads and deferring:
        raise TypeError(f"{fn.__qualname__}: on_session takes side_reads or deferring, not both")
    signature = inspect.signature(fn, eval_str=True)
    params = list(signature.parameters.values())
    if not params or params[0].name != "session":
        raise TypeError(f"{fn.__qualname__}: an on_session body takes `session` first")

    @functools.wraps(fn)
    async def endpoint(**kwargs: Any) -> T:
        session = kwargs.pop("db")
        if side_reads:
            return await run_lending(session, fn, **kwargs)
        if deferring:
            return await run_deferring(session, fn, **kwargs)
        return await session.run_sync(fn, **kwargs)

    endpoint.session_body = fn  # type: ignore[attr-defined]
    endpoint.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[*params[1:], inspect.Parameter("db", inspect.Parameter.KEYWORD_ONLY, annotation=DbDep)],
    )
    return endpoint


def on_thread(
    fn: Callable[..., T] | None = None, *, actor: str | Callable[..., Any] | None = None, release: bool = True,
) -> Callable[..., Any]:
    """`@on_thread` on a sync `def handler(session: Session, ...)` makes it the route's endpoint, as `@on_session`
    does (the same `__signature__`: `db: DbDep` in `session`'s place, every other parameter kept; the body is the
    endpoint's `session_body`), but runs the body on a worker thread with a sync request-lane session
    (`run_blocking_in_session`) — the LAST resort of a route whose shared service does blocking outbound I/O (a
    payment gateway, a cloud API, a tailnet key mint, a DBOS workflow it waits on) INSIDE its transaction, or holds
    a row lock across such a call, where reading first and calling out after cannot be split without changing what
    the service locks. `on_session` (a greenlet, no thread) is the shape for everything else.

    The endpoint hands the route session's connection back first (`release`; `release=False` when nothing ahead of
    the body could have checked one out), then passes that session as `route`, so its attribution carries over and
    an open transaction is refused. `actor` names the parameter whose value owns the thread session's writes
    (`actor="user"`) or is a callable over the endpoint's resolved parameters, by keyword, returning it
    (`actor=lambda user, **_: act_as.person(user)`); `None` leaves the carried-over attribution. Bare
    (`@on_thread`) or with arguments (`@on_thread(actor="user")`)."""
    if fn is None:
        return functools.partial(on_thread, actor=actor, release=release)
    signature = inspect.signature(fn, eval_str=True)
    params = list(signature.parameters.values())
    if not params or params[0].name != "session":
        raise TypeError(f"{fn.__qualname__}: an on_thread body takes `session` first")
    names = {p.name for p in params[1:]}
    # Checked once here, not per request: a misspelt owner would otherwise be a 500 on the first call.
    if isinstance(actor, str) and actor not in names:
        raise TypeError(f"{fn.__qualname__}: actor={actor!r} names no parameter of the body")
    if callable(actor):
        wanted = {
            p.name for p in inspect.signature(actor).parameters.values()
            if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
        }
        if not wanted <= names:
            raise TypeError(f"{fn.__qualname__}: actor callable names {sorted(wanted - names)}, not parameters of the body")

    defaults = {p.name: p.default for p in params[1:] if p.default is not inspect.Parameter.empty}

    @functools.wraps(fn)
    async def endpoint(**kwargs: Any) -> T:
        route = kwargs.pop("db")
        if release:
            await route.run_sync(db.release_connection)
        for name, default in defaults.items():  # a direct caller may leave a defaulted parameter out; FastAPI never does
            kwargs.setdefault(name, default)
        if isinstance(actor, str):
            owner = kwargs[actor]
        else:
            owner = actor(**kwargs) if actor is not None else None
        # The body's parameters are bound here, not passed through: one of them may itself be named `actor`.
        return await run_blocking_in_session(functools.partial(fn, **kwargs), actor=owner, route=route)

    endpoint.session_body = fn  # type: ignore[attr-defined]
    endpoint.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=[*params[1:], inspect.Parameter("db", inspect.Parameter.KEYWORD_ONLY, annotation=DbDep)],
    )
    return endpoint


async def release(session: AsyncSession) -> bool:
    """`app/db.py::release_connection` for an `AsyncSession`: hand a read-only transaction's connection back
    before a non-DB await. True if released; a transaction that has written keeps its connection."""
    return await session.run_sync(db.release_connection)


def release_after_async_endpoint(dependants: Any) -> int:
    """Wrap every route endpoint so its route session
    (`DbDep`) hands back a read-only transaction's connection the moment the endpoint returns, under the same rules
    (`app/db.py::release_route_connection`); an endpoint that raised is rolled back at once instead, as its close
    would. Returns how many were wrapped.

    Why: the request's dependencies are torn down after the endpoint, in reverse order, so `get_db` — resolved
    first — closes last, and its session would hold the connection a handler's final read checked out while the
    response is serialised and every other dependency exits, a sync one (`require_user`'s own session) through a
    threadpool hop. A burst would then queue pool connections behind threads."""
    wrapped = 0
    for dependant in dependants:
        call = getattr(dependant, "call", None)
        if call is None or getattr(call, "_releases_route_connection", False):
            continue
        if not inspect.iscoroutinefunction(call):
            continue
        dependant.call = _releasing(call)
        wrapped += 1
    return wrapped


def _releasing(call: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    @functools.wraps(call)
    async def endpoint(*args: Any, **kwargs: Any) -> T:
        sessions = [v for v in kwargs.values() if isinstance(v, AsyncSession) and LANE_KEY in v.info]
        try:
            result = await call(*args, **kwargs)
        except BaseException:
            # A failed request's uncommitted work is the close's rollback to drop anyway: dropped now, so a
            # transaction that wrote before the refusal holds nothing across the dependencies' teardown either.
            for session in sessions:
                await session.rollback()
            raise
        for session in sessions:
            await session.run_sync(db.release_route_connection)
        return result

    endpoint._releases_route_connection = True  # type: ignore[attr-defined]
    return endpoint


#: `session.info` key: the post-commit legs a route body queued for `run_deferring` to run off the loop.
_DEFERRED = "deferred_off_loop"


def defer_off_loop(session: Any, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
    """`fn(*args, **kwargs)`, a post-commit leg that blocks on I/O of its own (a DBOS enqueue, a Slack or bus round
    trip, a session of its own): inline, or — inside a body `run_deferring` runs on the loop — queued for it to run
    on a worker thread once the body returns. An in-process caller on a worker thread keeps the inline call. A leg
    must not touch `session`: it runs after the session's transaction has ended, on another thread."""
    deferred = session.info.get(_DEFERRED)
    if deferred is None:
        fn(*args, **kwargs)
    else:
        deferred.append((fn, args, kwargs))


async def run_deferring(session: AsyncSession, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """`await session.run_sync(fn, *args)`, then every `defer_off_loop` leg it queued, in order, each on a worker
    thread — also when `fn` raised after queueing one, as the inline call had already run. The route's transaction
    ends first (rolled back: whatever it still holds, the session's close would discard), so no connection is held
    across a leg. A leg that raises is logged, never the request's answer: its write has committed."""
    deferred: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

    def body(sync_session: Any, *body_args: Any, **body_kwargs: Any) -> T:
        sync_session.info[_DEFERRED] = deferred
        try:
            return fn(sync_session, *body_args, **body_kwargs)
        finally:
            sync_session.info.pop(_DEFERRED, None)

    try:
        return await session.run_sync(body, *args, **kwargs)
    finally:
        if deferred:
            await session.rollback()
        for leg, leg_args, leg_kwargs in deferred:
            try:
                await asyncio.to_thread(leg, *leg_args, **leg_kwargs)
            except Exception:  # noqa: BLE001 — the body's outcome stands; the leg is after its commit
                log.exception("db_async: a deferred post-commit leg %r failed", leg)


def wait_off_loop(session: Any, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """`fn(*args, **kwargs)` — blocking work that is not the database (an identity provider, an Athena scan) — on a
    worker thread, waited for from INSIDE a `run_sync` body without blocking the loop (SQLAlchemy's `await_only`,
    the greenlet the body runs in). `session` (the body's) hands a read-only transaction's connection back first
    (`app/db.py::release_connection`; the tenancy scope survives); one that has written keeps it, which is logged:
    commit before handing work off. Off the event loop (a caller on a worker thread, `routes/tasks/answers.py::
    _thread_steps`) it is a plain call: that thread may block."""
    from sqlalchemy.util import await_only

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)
    if not db.release_connection(session) and session.in_transaction():
        log.warning("db_async: a written transaction holds its connection across off-loop work (%r)", fn)
    return await_only(asyncio.to_thread(fn, *args, **kwargs))


async def run_in_session(fn: Callable[..., T], /, *args: Any, identity: bool = False, **kwargs: Any) -> T:
    """`fn(session, *args, **kwargs)` — existing sync ORM code — on a fresh async session, over the async
    connection (`AsyncSession.run_sync`). The session is closed after: `fn` commits what it means to keep."""
    async with async_session(identity=identity) as session:
        return await session.run_sync(fn, *args, **kwargs)


#: The `run_lending` body running now: its asyncio task and its session's sync face, whose connection a
#: `side_session` opened inside the body hands back first. Set in the task's context, which the body's greenlet shares.
_LENDER: contextvars.ContextVar[Optional[tuple[Optional[asyncio.Task], Any]]] = contextvars.ContextVar(
    "db_async_lender", default=None
)


async def run_lending(session: AsyncSession, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """`await session.run_sync(fn, *args, **kwargs)` for a body whose shared services open a `side_session`: each
    one hands `session`'s connection back first and reads on the async pool, so the body needs no worker thread and
    its task never holds two connections. `session` checks a connection out again at its next statement."""
    token = _LENDER.set((asyncio.current_task(), session.sync_session))
    try:
        return await session.run_sync(fn, *args, **kwargs)
    finally:
        _LENDER.reset(token)


def _lending_body(lender: Optional[tuple[Optional[asyncio.Task], Any]]) -> bool:
    """Is this the greenlet of `lender`'s own `run_lending` body (not a thread or task its context was copied to)?"""
    from sqlalchemy.util.concurrency import in_greenlet

    if lender is None or not in_greenlet():
        return False
    try:
        return asyncio.current_task() is lender[0]
    except RuntimeError:  # no running loop: a worker thread
        return False


def hand_back() -> None:
    """Inside a `run_lending` body: hand the body's read-only transaction back (`app/db.py::release_route_connection`),
    ahead of a `side_session`. Called before the side session's scope is entered (`vnodes.fleet_session`), so the
    route session commits under its own scope; a no-op anywhere else, and once handed back."""
    lender = _LENDER.get()
    if lender is not None and _lending_body(lender):
        db.release_route_connection(lender[1])


@contextmanager
def side_session() -> Iterator[Any]:
    """A sync session of its own, for a shared service that reads beside its caller's session (the operator plane's
    `vnodes.fleet_session`). Inside a `run_lending` body: the body's connection is handed back first (`hand_back`),
    then this is an async-pool session's sync face, closed on exit — no sync session on the loop, one connection at a
    time. A body that has written keeps its connection, so a side session there would be a second: refused
    (`NestedSessionError`), always — a `side_reads` body reads. Anywhere else (a worker thread, a cron): a
    `BridgedSession()`, as always."""
    if not _lending_body(_LENDER.get()):
        with db.BridgedSession() as session:
            yield session
        return
    from sqlalchemy.util.concurrency import await_only

    hand_back()
    if holds_connection():
        raise NestedSessionError(
            "side_session: the run_lending body still holds its connection (it wrote): a side_reads body only reads"
        )
    side = _maker(bind=async_engine(), info={LANE_KEY: ASYNC_LANE})
    try:
        yield side.sync_session
    finally:
        await_only(side.close())


async def run_blocking_in_session(
    fn: Callable[..., T], /, *args: Any, actor: Any = None, route: Optional[AsyncSession] = None, **kwargs: Any,
) -> T:
    """`fn(session, *args, **kwargs)` on a worker thread with a sync request-lane session — the LAST resort of an
    `async def` route whose shared service does blocking outbound I/O (a payment gateway, a cloud API, a tailnet key
    mint, a DBOS workflow it waits on) INSIDE its transaction, or holds a row lock across such a call, where reading
    first and calling out after cannot be split without changing what the service locks. Everything else uses
    `run_sync` on the route's `DbDep`. `fn` commits what it keeps.

    Who owns the thread session's writes: `actor` (the route's `require_user` row, or an act-as seat copy), as
    `require_user` made it own the sync route's — the row itself stays on the route session, so `fn` merges it if it
    writes it; or `route`, the route's own `AsyncSession`, whose attribution (`app/actors.py`) is carried over. A
    `route` is also checked to hold no open transaction: a coroutine never holds two connections, and an open one
    would be held for the whole thread wait (raised under test and on a local stack, logged in production). The
    tenancy scope travels with the thread's copied context; the thread's session is a `BridgedSession`, so it draws on
    this same pool through the bridge."""
    from starlette.concurrency import run_in_threadpool

    from app.actors import ACTOR_SESSION_KEY, MACHINE_ACT_SESSION_KEY, set_request_actor

    carried: dict[str, Any] = {}
    if route is not None:
        if route.in_transaction():
            message = "run_blocking_in_session: the route session is still in a transaction — commit or release it first"
            if _strict():
                raise RuntimeError(message)
            log.warning(message)
        info = route.sync_session.info
        carried = {key: info[key] for key in (ACTOR_SESSION_KEY, MACHINE_ACT_SESSION_KEY) if key in info}

    def call() -> T:
        with db.BridgedSession() as session:
            session.info.update(carried)
            if actor is not None:
                set_request_actor(session, actor.id)
            return fn(session, *args, **kwargs)

    return await run_in_threadpool(call)


# ── The foreign-thread bridge ─────────────────────────────────────────────────────────────────────────────


def serving_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _serving_loop


def serving_loop_running() -> bool:
    """Whether this process has a serving loop that is running now."""
    return _serving_running() is not None


def run_on_loop(coro: Awaitable[T], *, timeout: float) -> T:
    """Run `coro` on the serving loop from a FOREIGN thread and block that thread for its result, at most `timeout`
    seconds. The coroutine runs in a copy of the caller's context (a bound tenancy scope, an identity escape
    travel with it). Raises when called on an event loop (await it there instead). With no serving loop running
    (a script, a test without the app's lifespan) it runs on a private loop of its own, on the unpooled engine."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        if asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError("run_on_loop() called on an event loop: await the coroutine instead")
    loop = _serving_loop
    if loop is None or loop.is_closed() or not loop.is_running():
        return asyncio.run(asyncio.wait_for(coro, timeout))  # type: ignore[arg-type]
    return _hop(loop, coro, timeout)


#: Held around every hop's `call_soon_threadsafe` and by `stop()` while it unregisters the serving loop.
_hop_lock = threading.Lock()


def _schedule(loop: asyncio.AbstractEventLoop, callback: Callable[[], Any], *, required: bool = True) -> None:
    """`loop.call_soon_threadsafe(callback)` from a foreign thread, only while `loop` is still the registered
    serving loop (or, with none registered, still open): never into a loop that `stop()` has let go of."""
    with _hop_lock:
        live = (loop is _serving_loop or _serving_loop is None) and not loop.is_closed() and loop.is_running()
        if live:
            loop.call_soon_threadsafe(callback)
            return
    if required:
        raise BridgeCancelled("the serving loop is shutting down")


def _hop(
    loop: asyncio.AbstractEventLoop, coro: Awaitable[T], timeout: float,
    orphaned: Optional[Callable[[Any], None]] = None,
) -> T:
    """`run_on_loop`'s mechanism: run `coro` on `loop` (never the calling thread's own) in a copy of the caller's
    context, and block the calling thread for its result. The bridge (`_await_anywhere`) also calls it from a
    thread running a private loop of its own, whose sync caller blocks that loop as a sync driver always did.
    `orphaned`, if given, gets a result that arrives after the caller timed out (on the loop), so a resource it
    holds — a checked-out connection — is given back rather than leaked."""
    context = contextvars.copy_context()
    result: concurrent.futures.Future = concurrent.futures.Future()
    started: list[asyncio.Task] = []
    lock = threading.Lock()

    def start() -> None:
        with lock:
            # A caller that timed out first cancelled the still-pending future: the coroutine never starts.
            if not result.set_running_or_notify_cancel():
                if asyncio.iscoroutine(coro):
                    coro.close()
                return
            task: asyncio.Task[Any] = loop.create_task(coro, context=context)  # type: ignore[arg-type]
            started.append(task)

        def done(finished: asyncio.Task) -> None:
            if finished.cancelled() or finished.exception() is not None:
                # A running future cannot be `cancel()`ed: the caller learns of the cancellation as its exception.
                error = concurrent.futures.CancelledError() if finished.cancelled() else finished.exception()
                try:
                    result.set_exception(error)  # type: ignore[arg-type]
                except concurrent.futures.InvalidStateError:  # the caller already gave up
                    pass
            elif result.done():  # the caller timed out while it ran: nobody will take the result
                if orphaned is not None:
                    orphaned(finished.result())
            else:
                try:
                    result.set_result(finished.result())
                except concurrent.futures.InvalidStateError:  # abandoned by the caller in the same instant
                    if orphaned is not None:
                        orphaned(finished.result())

        task.add_done_callback(done)

    try:
        _schedule(loop, start)
    except BridgeCancelled:
        if asyncio.iscoroutine(coro):
            coro.close()
        raise
    try:
        return result.result(timeout)
    except concurrent.futures.TimeoutError:
        with lock:
            result.cancel()  # still pending: `start` will skip the coroutine
            if started:
                # Running: mark it abandoned, so a result that still arrives goes to `orphaned`. One that landed
                # in the instant since the wait gave up is handed there too.
                try:
                    result.set_exception(TimeoutError("abandoned"))
                except concurrent.futures.InvalidStateError:
                    if orphaned is not None and not result.cancelled() and result.exception() is None:
                        _schedule(loop, lambda: orphaned(result.result()), required=False)
            for task in started:
                _schedule(loop, task.cancel, required=False)
        raise TimeoutError(f"run_on_loop: no result within {timeout:.0f}s") from None
    except concurrent.futures.CancelledError:
        raise asyncio.CancelledError() from None


# ── Sync ORM code on the async pool ─────────────────────────────────────────────────────
# Background work — cron bodies, lease-drain's per-node fan-out, DBOS steps, the threads a route or a cron hands
# work to — is sync ORM code that opens `BridgedSession()`. It draws on the async pool: `sync_bind()` is the bind
# `app/db.py::_BridgeBoundSession` gives such a session.
#  - In a greenlet on the serving loop (`greenlet_spawn`, `AsyncSession.run_sync`): natively — every DBAPI await
#    is SQLAlchemy's `await_only`.
#  - On a FOREIGN thread while the serving loop runs: through `run_on_loop`, statement by statement. The pool is
#    only ever touched on the loop (checkout and checkin each hop there once); the DBAPI awaits in between hop
#    there through the adapted connection's `await_` (`_await_anywhere`).
# A bridged connection carries a bounded lock wait (`app/db.py::BACKGROUND_LOCK_TIMEOUT_MS`)
# while it is checked out, and resets it before it goes back: a cron body parked behind a row lock raises
# `LockNotAvailable` rather than holding its run lock for good. Off the serving loop entirely (a script, a test
# without the app's lifespan), `sync_bind()` is None and the session binds `app/db.py::offline_engine`.

#: A bridged statement's bound — far above any healthy one; a hung DBAPI await must not pin its thread forever.
BRIDGE_STATEMENT_TIMEOUT_SECONDS = 600.0
#: A bridged checkout's bound: the pool's own (`POOL_TIMEOUT_SECONDS`) plus the hop.
BRIDGE_CHECKOUT_TIMEOUT_SECONDS = POOL_TIMEOUT_SECONDS + 5.0


async def _awaiting(awaitable: Awaitable[T]) -> T:
    return await awaitable


def _serving_running() -> Optional[asyncio.AbstractEventLoop]:
    loop = _serving_loop
    return loop if loop is not None and not loop.is_closed() and loop.is_running() else None


def _loop_running_here() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def on_serving_loop() -> bool:
    """Whether the caller runs on the serving loop's thread, inside it."""
    return _on_serving_loop()


def _on_serving_loop() -> bool:
    try:
        return asyncio.get_running_loop() is _serving_loop
    except RuntimeError:
        return False


def _await_anywhere(awaitable: Awaitable[T]) -> T:
    """The pooled connections' `await_`: SQLAlchemy's `await_only` on the serving loop (in a greenlet), and a hop
    to the serving loop from any other thread while it runs — a plain one, or one running a private loop of its
    own (a sync caller inside `asyncio.run`). With no serving loop running, `await_only` as it always was."""
    from sqlalchemy.util.concurrency import await_only

    from sqlalchemy.util.concurrency import in_greenlet

    loop = _serving_running()
    if loop is None and not in_greenlet() and not _loop_running_here():
        # A bridged connection that outlived its serving loop (a shutdown, a test's app restart): its last I/O —
        # the rollback and checkin — runs on a private loop rather than failing and leaking the connection.
        return asyncio.run(_awaiting(awaitable))
    if loop is None or _on_serving_loop():
        return await_only(awaitable)
    try:
        return _hop(loop, _awaiting(awaitable), BRIDGE_STATEMENT_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        # Never a BaseException into sync code (`except Exception` is its contract): the loop is shutting down.
        raise BridgeCancelled("the serving loop cancelled this statement (shutting down)") from None


class BridgeCancelled(RuntimeError):
    """A bridged statement or checkout the serving loop cancelled (it is shutting down)."""


def _adapt_for_threads(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
    """Pool `connect` listener: every pooled DBAPI connection awaits through `_await_anywhere`."""
    from sqlalchemy.dialects.postgresql.psycopg import AsyncAdapt_psycopg_connection

    if type(dbapi_connection) is AsyncAdapt_psycopg_connection:
        dbapi_connection.__class__ = _ThreadAwareAdapt


def _thread_aware_class():  # type: ignore[no-untyped-def]
    from sqlalchemy.dialects.postgresql.psycopg import AsyncAdapt_psycopg_connection

    class ThreadAwareAdapt(AsyncAdapt_psycopg_connection):
        __slots__ = ()
        await_ = staticmethod(_await_anywhere)

    return ThreadAwareAdapt


_ThreadAwareAdapt = _thread_aware_class()


def _on_loop_or_bridged(fn: Callable[[], T], timeout: float, orphaned: Optional[Callable[[Any], None]] = None) -> T:
    """`fn()` (sync, touching the pool) in a greenlet on the serving loop: inline when already in one there."""
    from sqlalchemy.util.concurrency import greenlet_spawn

    from sqlalchemy.util.concurrency import in_greenlet

    loop = _serving_running()
    if loop is None and not in_greenlet() and not _loop_running_here():
        # The serving loop is gone (a shutdown, a test's app restart) under a session bound while it ran.
        return asyncio.run(greenlet_spawn(fn))
    if loop is None or _on_serving_loop():
        return fn()  # in a greenlet on the serving loop (`sync_bind`): SQLAlchemy's own rules apply
    try:
        return _hop(loop, greenlet_spawn(fn), timeout, orphaned)
    except asyncio.CancelledError:
        raise BridgeCancelled("the serving loop cancelled this checkout or checkin (shutting down)") from None


def _bound_lock_wait(dbapi_connection, value: str) -> None:  # type: ignore[no-untyped-def]
    """`SET lock_timeout`, committed on its own: a SET is transactional, so the caller's rollback (or the pool's
    reset) would otherwise undo it. Only ever called with no transaction of the caller's open."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET lock_timeout = {value}")
    finally:
        cursor.close()
    dbapi_connection.commit()


def _bridged_classes():  # type: ignore[no-untyped-def]
    from sqlalchemy.engine.base import Connection, OptionEngine

    class BridgedConnection(Connection):
        """A sync `Connection` over a pooled async connection: checked out and in on the serving loop."""

        def __init__(self, engine, connection=None, **kwargs):  # type: ignore[no-untyped-def]
            if connection is None:
                def checkout():  # type: ignore[no-untyped-def]
                    fairy = engine._proxied.raw_connection()
                    try:
                        _bound_lock_wait(fairy.dbapi_connection, str(db.BACKGROUND_LOCK_TIMEOUT_MS))
                    except BaseException:
                        fairy.invalidate()
                        fairy.close()
                        raise
                    return fairy

                connection = _on_loop_or_bridged(checkout, BRIDGE_CHECKOUT_TIMEOUT_SECONDS, _give_back)
            super().__init__(engine, connection, **kwargs)

        def close(self) -> None:
            if self.closed:
                return
            fairy = self._dbapi_connection

            def checkin() -> None:
                try:
                    if fairy is not None and fairy.is_valid and fairy.dbapi_connection is not None:
                        # The caller's open transaction ends first (rolled back, as the pool's reset would),
                        # so the committed reset below never commits the caller's work.
                        if self._transaction is not None:
                            self._transaction.close()
                        fairy.dbapi_connection.rollback()
                        _bound_lock_wait(fairy.dbapi_connection, "DEFAULT")
                except Exception:  # noqa: BLE001 — a connection that cannot reset is not returned as it was
                    if fairy is not None:
                        fairy.invalidate()
                Connection.close(self)

            _on_loop_or_bridged(checkin, BRIDGE_STATEMENT_TIMEOUT_SECONDS)

    def _give_back(fairy) -> None:  # type: ignore[no-untyped-def]
        """A checkout that landed after its caller timed out (on the loop): return it to the pool, in a greenlet."""
        from sqlalchemy.util.concurrency import greenlet_spawn

        def checkin() -> None:
            fairy.invalidate()  # its lock bound may still be set: never hand it out again
            fairy.close()

        task = asyncio.get_running_loop().create_task(greenlet_spawn(checkin))
        _spawned.add(task)
        task.add_done_callback(_spawned.discard)

    class BridgeEngine(OptionEngine):
        """The pooled engine's sync face for `sync_bind()`: the same pool, dialect and events."""

        _connection_cls = BridgedConnection

    return BridgeEngine


_BridgeEngine = _bridged_classes()
_unbridged = threading.local()


def run_unbridged(fn: Callable[..., T], /, *args: Any) -> T:
    """`fn(*args)` with this thread's sessions on `app/db.py::offline_engine`, never the bridge: for a worker the serving loop
    itself blocks on (a sync caller on the loop thread waiting for it), whose hop to that loop would wait out its
    bound against a loop that cannot answer."""
    _unbridged.on = True
    try:
        return fn(*args)
    finally:
        _unbridged.on = False
_bridge: Optional[Any] = None
_bridge_lock = threading.Lock()  # one bridge per pooled engine, however many threads ask for it first


def sync_bind() -> Optional[Any]:
    """The bind for a `BridgedSession()`: the async pool's sync face, in a greenlet on the
    serving loop or on any other thread while it runs (one running a private loop included); None anywhere else
    (on the serving loop outside a greenlet, or with no serving loop), and on a thread `run_unbridged` marks."""
    global _bridge
    from sqlalchemy.util.concurrency import in_greenlet

    pooled = _pooled
    if pooled is None or _serving_running() is None or getattr(_unbridged, "on", False):
        return None
    if _on_serving_loop() and not in_greenlet():
        return None
    bridge = _bridge
    if bridge is None or bridge._proxied is not pooled.sync_engine:
        with _bridge_lock:
            bridge = _bridge
            if bridge is None or bridge._proxied is not pooled.sync_engine:
                bridge = _bridge = _BridgeEngine(pooled.sync_engine, {})
    return bridge


def spawn(coro_fn: Callable[..., Awaitable[Any]], /, *args: Any, name: str) -> bool:
    """Start `coro_fn(*args)` as a task on the serving loop, from any thread, in a FRESH context (nothing of the
    caller's binding or DBOS context follows it). Fire-and-forget: the task logs its own failure. False when no
    serving loop runs (the caller falls back to running the work itself)."""
    loop = _serving_loop
    if loop is None or loop.is_closed() or not loop.is_running():
        return False

    async def guarded() -> None:
        try:
            await coro_fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a background task never takes the loop down
            log.exception("db_async: background task %s failed", name)

    def start() -> None:
        task = loop.create_task(guarded(), name=name, context=contextvars.Context())
        _spawned.add(task)
        task.add_done_callback(_spawned.discard)

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        start()
        return True
    try:
        _schedule(loop, start)
    except (RuntimeError, BridgeCancelled):  # the loop closed after the check above
        return False
    return True


#: Tasks `spawn` started, held so the loop's weak reference is not their only one.
_spawned: set[asyncio.Task] = set()
