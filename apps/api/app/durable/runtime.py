"""The DBOS runtime this control plane's durable workflows run on.

DBOS owns its schema (`durable_dbos_sys`) and its DDL. `init` wraps the served app's lifespan so launch,
and so startup recovery of PENDING workflows, runs on the serving loop right after the app's own actors
start, making a redeploy mid-flow survivable; the queues are registered straight after launch and the
engine is drained and destroyed after the app's teardown. A queue is declared here as data (`declare_queue`) and
enqueued by name through `enqueue`, the one enqueue primitive. The version is
pinned: recovery selects on `application_version`, and DBOS's source-hash default (or the repo
version) would orphan in-flight work on every change. Each ECS task runs as its own executor (its task id,
`executor_id`), so a stopped task's PENDING workflows are told apart from a live one's: launch recovers only
this executor's, and `recover_abandoned` (driven by `app/cp_ecs.py::record`) re-enqueues those of a task
recorded stopped, without waiting for the next deploy. A rollback to a release from before per-task executors
recovers none of them (it recovers `local` only); the next forward deploy's first pass does. Replicas need no leader: every handoff is a claim in the
system database.
The org read seam is not on DBOS's engine, which reaches only operator-plane `durable_dbos_sys`; step
reads go through `app.db.BridgedSession` (`test_tenancy_plane_sites.py`), binding from the step's own
argument so a recovered replay binds the same org.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Iterator, Optional, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

APP_NAME = "jaah"
SYSTEM_SCHEMA = "durable_dbos_sys"

# Bump only to abandon in-flight workflows on purpose: this is the identity every executor recognises
# its own in-flight work by. Unrelated to the repo's release version.
ACCOUNTS_WORKFLOW_VERSION = "1"

# The cron feature's responsiveness floor, in seconds (see `engine_config`); named so the knob's help
# text and the engine's value are checked in one place.
SCHEDULER_POLLING_INTERVAL_SEC = 5.0

# How often the engine's notifier thread flushes coalesced LISTEN/NOTIFY wake-ups. The library default
# (0.01s) wakes that thread 100 times a second per process with nothing to send — ~2% of the api's CPU.
# It carries `set_event` wake-ups (streams too, unused here); `DBOS.send`/`recv` wake through
# their own DB trigger and are unaffected. The one waiter is the interactive login's `DBOS.get_event` for
# its authorization URL (`app/durable/accounts.py`), which now wakes up to 0.25s later.
NOTIFICATION_COALESCE_SEC = 0.25

_dbos: Any = None

#: The ladder a step whose only expected fault is a transient database or network error retries on.
DB_RETRY: dict[str, Any] = {"retries_allowed": True, "max_attempts": 3, "interval_seconds": 2.0}

#: A queue whose work mostly waits on a vendor (an LLM, Slack) and whose bursts are human fan-out: four keep
#: one slow reply (~30s) from parking the rest, and 30 a minute — half Slack's ~1/s per-channel
#: `chat.postMessage` limit — shapes only a pathological burst (a scripted loop), never normal use.
VENDOR_QUEUE: dict[str, Any] = {
    "concurrency": 4, "limiter": {"limit": 30, "period": 60.0}, "polling_interval_sec": 5.0,
}

#: The idle poll for queues a person waits on (notifications, Slack echoes, titles): 5s, not the engine's 1s.
#: An operator's accepted trade: up to ~5s before such work starts, because every api
#: task polls every queue — the one CPU cost that grows with the task count while requests and crons split
#: across tasks. `VENDOR_QUEUE` carries it; `slack-echo`, `notify-outbox` and `cron-projection` name it.
INTERACTIVE_POLL_SEC = VENDOR_QUEUE["polling_interval_sec"]

#: How often an idle queue is polled, in seconds: 30s, for background work nobody waits on. Every api task polls
#: every registered queue (one engine thread each, waking to reload its config and try a dequeue), so the idle poll
#: is a CPU cost multiplied by the task count. Work a person waits on polls at `INTERACTIVE_POLL_SEC`; a queue with a
#: tighter bound names its own `POLL_SEC` (`cloud_node`, `cloud_change`, `hosted_app`, `live_cast`, token-billing).
BACKGROUND_POLL_SEC = 30.0

#: How long shutdown lets in-flight workflows finish and record their outcome before the engine is torn down.
#: It runs after Granian's graceful window and the app's teardown, inside the api container's
#: `stopTimeout` (set by the ECS Terraform module). A workflow still running past it stays PENDING
#: until `recover_abandoned` sees the task stopped; `_InterruptedAtShutdown` keeps its failed outcome write out
#: of the error log.
SHUTDOWN_DRAIN_SEC = 3

#: The executor id every process ran as before ECS tasks had their own, and still runs as off ECS.
LEGACY_EXECUTOR_ID = "local"


@dataclass(frozen=True)
class QueueSpec:
    """One queue's registration, exactly as `register_queues` hands it to the engine."""

    name: str
    concurrency: int
    limiter: dict[str, Any] | None = None
    #: The per-replica bound beside the global one; None leaves a replica bounded only by `concurrency`.
    worker_concurrency: int | None = None
    #: The engine's idle poll; its default 1s unless the queue opts into `BACKGROUND_POLL_SEC`.
    polling_interval_sec: float = 1.0


# Filled at import by each module's `declare_queue`; registered with the engine only after launch.
_QUEUES: dict[str, QueueSpec] = {}


def declare_queue(
    name: str, *, concurrency: int, limiter: dict[str, Any] | None = None, worker_concurrency: int | None = None,
    polling_interval_sec: float = 1.0,
) -> str:
    """Declare a durable queue and return its name, the handle every enqueue addresses it by.

    Data only: the engine refuses a queue before it is launched, so `register_queues` registers every
    declaration once launch has run. A second declaration of a name must be identical.
    """
    spec = QueueSpec(name=name, concurrency=concurrency, limiter=limiter, worker_concurrency=worker_concurrency,
                     polling_interval_sec=polling_interval_sec)
    existing = _QUEUES.setdefault(name, spec)
    if existing != spec:
        raise ValueError(f"queue {name!r} declared twice with different settings: {existing} vs {spec}")
    return name


async def register_queues() -> None:
    """Register every declared queue with the launched engine. `concurrency` is a GLOBAL bound.

    The async form because the sync `register_queue` refuses to run inside an event loop.
    """
    from dbos import DBOS

    for spec in _QUEUES.values():
        await DBOS.register_queue_async(
            spec.name, global_concurrency=spec.concurrency, worker_concurrency=spec.worker_concurrency,
            limiter=spec.limiter, polling_interval_sec=spec.polling_interval_sec,
        )


@contextmanager
def step_session(caller: str, *, gone: str | None = None, logger: logging.Logger = log) -> Iterator[Any]:
    """A step's attributed session: `exclusion_log.caller(caller)` (a DBOS worker has no request or cron to
    name its reads) around a fresh `BridgedSession`, for the step to `seam.bind_from` on.

    `bind_from` stays at the call site (or in `delegate_for_task`, the one shared site the tenancy scope census
    attributes for the steps that hand one task to a delegate). With `gone`, a `TenancyRowMissing` (the row was deleted while queued) is suppressed —
    logged at INFO on `logger` unless `gone` is empty — and control falls through to the statement after
    the `with`, the step's nothing-to-do return; without it, it propagates.
    """
    # Deferred: every durable module imports this one at DBOS registration time, before the app is wired.
    from app.db import BridgedSession
    from app import write_origin
    from app.tenancy import exclusion_log, seam

    with exclusion_log.caller(caller), write_origin.scope(f"durable:{caller}"), BridgedSession() as session:
        try:
            yield session
        except seam.TenancyRowMissing:
            if gone is None:
                raise
            if gone:
                logger.info(gone)


def delegate_for_task(
    label: str, task_id: int, *, gone: str, logger: logging.Logger, call: Callable[[Any], T], fallback: T,
) -> T:
    """A step handing ONE task's work to a delegate module: `call(org)` under `step_session(label, gone=gone)`
    and `seam.bind_from(session, Task, task_id)`; `fallback` when the row is gone.

    The org is derived from the task and bound around the delegation (a ContextVar region for every session
    the delegate opens; unbound, every read would answer "no such task" and the work would silently stop
    fleet-wide) — one PK lookup, cheaper than pushing the seam into the delegate. A vanished row is the
    delegate's own nothing-to-do: `bind_from` reads the row first, and letting it raise would become a failed
    step that never converges. The session only derives the org: `rollback()` releases the pooled connection
    before the delegate's slow vendor call (`app/tenancy/seam.py::bind_from`); the scope survives it."""
    from app.models.tasks import Task
    from app.tenancy import seam

    with step_session(label, gone=gone, logger=logger) as session, seam.bind_from(session, Task, task_id) as org:
        session.rollback()
        return call(org)
    return fallback


class _CaptureServingLoop:
    """Records the serving event loop on the lifespan startup scope.

    A DBOS step runs synchronously on a DBOS worker thread, but the node bus resolves reply futures on
    the serving loop without `call_soon_threadsafe`; a step using its own `asyncio.run` would hang to
    its deadline silently. `accounts.dispatch` hops back onto this captured loop instead.

    Captured at lifespan rather than off the first request: queue dequeues, startup recovery and
    scheduled ticks run with no request in flight.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            from app.durable import accounts

            accounts.set_serving_loop(asyncio.get_running_loop())
        await self.app(scope, receive, send)


def engine_config(*, system_schema: str | None = None) -> dict[str, Any]:
    """Exactly the `DBOSConfig` `init` hands the engine.

    A function so a test can assert the wiring without a runtime or database connection.
    """
    from app.config import DB_POOL_RECYCLE_SECONDS, db_pool_sizes
    from app.db import database_url

    return {
        "name": APP_NAME,
        "system_database_url": database_url(),
        "dbos_system_schema": system_schema or SYSTEM_SCHEMA,
        "application_version": ACCOUNTS_WORKFLOW_VERSION,
        # This ECS task's id; off ECS None, the library's `local` (one process, recovered at its own launch).
        "executor_id": executor_id(),
        # Bounds the lag between a schedule (or global timezone) being saved and the new cadence firing:
        # `dynamic_scheduler_loop` re-lists `workflow_schedules` on this interval. The library default (30s)
        # feels broken; this is one poll thread per process, so 5s costs one `SELECT` every five seconds.
        # `_dbos_config.py` moves this key into `runtimeConfig`.
        "scheduler_polling_interval_sec": SCHEDULER_POLLING_INTERVAL_SEC,
        "notification_coalesce_sec": NOTIFICATION_COALESCE_SEC,
        # DBOS owns its schema's DDL, so a fresh environment and a library upgrade need no out-of-band step.
        "run_migrations": True,
        # Our tracing is not OTLP; enabling it pulls in unused optional packages.
        "enable_otlp": False,
        "log_level": "INFO",
        # The system-database engine's pool, per task: an ENVIRONMENT decision beside the app pool's (see
        # `app/config.py::db_pool_sizes`). Unset -> None -> the library default (20).
        "sys_db_pool_size": db_pool_sizes().dbos_pool_size,
        # Merged over DBOS's own engine defaults (dbos 3.2.0 `configure_db_engine_parameters`), so only the age bound
        # (`app/config.py::DB_POOL_RECYCLE_SECONDS`) changes; its pool size stays `sys_db_pool_size`'s.
        "db_engine_kwargs": {"pool_recycle": DB_POOL_RECYCLE_SECONDS},
    }


def executor_id() -> str | None:
    """The DBOS executor this process runs as: its ECS task id (`app.cp_peers.self_task_id`), else `None`."""
    from app import cp_peers

    return cp_peers.self_task_id()


def recover_abandoned(stopped_executor_ids: set[str]) -> dict[str, int]:
    """Re-enqueue every PENDING workflow of this version whose executor is in `stopped_executor_ids`.

    DBOS recovers only at launch, and only the launching executor's own work: a task that stopped with a
    workflow in flight (past `SHUTDOWN_DRAIN_SEC`) left it PENDING under an executor nobody runs as any more.
    The caller names the stopped executors from POSITIVE evidence (an executor it knows nothing of is left
    alone: declaring a live one dead runs its work twice); this process's own executor is never one. Refused
    when the engine does not run as `executor_id()`, the id every other process's sweep tells this one by.
    Recovery is the engine's own: it flips the row to ENQUEUED only while it is still PENDING on a named
    executor, so a second sweep, or one racing a dequeue, re-runs nothing. The workflow resumes from its last
    recorded step on whichever process dequeues it.

    Called off any workflow's context (the cron body's own thread): inside one, `list_workflows` would be a
    checkpointed step and a replay would act on a stale list."""
    from dbos import DBOS

    own = executor_id() or LEGACY_EXECUTOR_ID
    if DBOS.executor_id != own:
        log.error("durable: the engine runs as executor %r, not %r — recovering nothing", DBOS.executor_id, own)
        return {"abandoned_executors": 0, "recovered": 0}
    pending = DBOS.list_workflows(
        status="PENDING", app_version=ACCOUNTS_WORKFLOW_VERSION, application_name=APP_NAME,
        load_input=False, load_output=False,
    )
    abandoned = sorted({w.executor_id for w in pending if w.executor_id in stopped_executor_ids - {own}})
    if not abandoned:
        return {"abandoned_executors": 0, "recovered": 0}
    recovered = len(DBOS._recover_pending_workflows(abandoned))
    log.info("durable: re-enqueued %d PENDING workflow(s) of stopped executor(s) %s", recovered, ", ".join(abandoned))
    return {"abandoned_executors": len(abandoned), "recovered": recovered}


def init(app: Any, *, system_schema: str | None = None) -> Any:
    """Construct the DBOS runtime and bind it to this app's lifespan. Idempotent.

    `system_schema` exists only for the test suite, which must not share a schema with a running local
    stack — both carry the same version pin and would recover each other's workflows. The real wiring
    is a single line in `app/main.py`."""
    global _dbos
    if _dbos is not None:
        return _dbos

    from dbos import DBOS

    # Every module in this package is imported for its decorator side effects, registering workflows,
    # steps and queues before launch. An unregistered workflow can be neither recovered nor scheduled, and
    # several are started only off the broadcaster, so discovery makes a forgotten import impossible.
    import importlib
    import pkgutil

    import app.durable as package

    for module in pkgutil.iter_modules(package.__path__):
        if module.name != "runtime":
            importlib.import_module(f"{package.__name__}.{module.name}")

    config = engine_config(system_schema=system_schema)

    app.add_middleware(_CaptureServingLoop)
    _dbos = DBOS(config=config)
    app.router.lifespan_context = _durable_lifespan(app.router.lifespan_context)
    log.info(
        "durable runtime constructed: schema=%s workflow_version=%s",
        system_schema or SYSTEM_SCHEMA,
        ACCOUNTS_WORKFLOW_VERSION,
    )
    return _dbos


class _InterruptedAtShutdown(logging.Filter):
    """Turns the engine's ERROR for a workflow that outlived `DBOS.destroy` into one INFO line.

    Such a workflow's thread keeps running (the interpreter joins it at exit), finishes, and fails its outcome
    write against the torn-down system database, which the engine logs as an exception. Nothing is lost: its
    row stays PENDING under this task's executor, and once ECS reports the task stopped `recover_abandoned`
    hands it to a live one. On the
    `dbos` logger only from shutdown until the next launch, so a startup-ordering bug still logs at ERROR.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        from dbos import DBOS
        from dbos._error import DBOSException

        exc = record.exc_info[1] if record.exc_info else None
        if not (isinstance(exc, DBOSException) and "before DBOS was launched" in str(exc)):
            return True
        # A body that raised first reaches here only as the context of the failed ERROR-outcome write.
        body_error = type(exc.__context__).__name__ if exc.__context__ else None
        log.info(
            "durable workflow %s was still running when the engine shut down%s; it stays PENDING until a live "
            "task recovers it once this one has stopped", DBOS.workflow_id or "(unknown id)",
            f" (its body raised {body_error})" if body_error else "",
        )
        return False


_INTERRUPTED_AT_SHUTDOWN = _InterruptedAtShutdown()


#: How long `destroy_engine` waits for the notification listener to leave once its backend is ended. It normally
#: leaves within half a second. The served shutdown's budget is short: it runs after Granian's graceful window and
#: `SHUTDOWN_DRAIN_SEC`, inside the api container's `stopTimeout`. Tests (`strict`) wait longer.
LISTENER_STOP_SEC = 2.0
STRICT_LISTENER_STOP_SEC = 15.0


def _stop_notification_listener(dbos: Any, budget_sec: float) -> None:
    """End the engine's LISTEN thread on its OWN thread, so `DBOS.destroy` finds no connection to close.

    dbos 3.2.0's destroy (`_cleanup_connections`) closes the listener's connection from the calling thread while
    the listener is still inside psycopg's `notifies()` on it: with psycopg's C implementation that frees the
    `PGconn` under the waiting thread, a use-after-free free-threaded CPython does not serialize. So: stop the
    listener's loop, end its backend from another connection, and wait. Its wait then fails on its own thread, its
    `finally` closes its own connection, and the loop exits. Raises when it cannot: the caller decides whether that
    is fatal. The real fix is upstream — `_cleanup_connections` should stop its own listener thread.
    """
    from sqlalchemy import text

    sys_db = dbos._sys_db_field
    if sys_db is None or not getattr(sys_db, "use_listen_notify", False):
        return
    listener = next((t for t in dbos._background_threads
                     if getattr(t, "_target", None) == sys_db.run_notification_listener), None)
    if listener is None:
        # A finished thread drops `_target`: none left alive without one means the listener already exited.
        # (Conductor's websocket is a Thread subclass with no `_target` either.)
        if not any(t.is_alive() and getattr(t, "_target", None) is None and t is not dbos.conductor_websocket
                   for t in dbos._background_threads):
            return
        # A dbos upgrade renamed what this reads: say so, never silently leave the race in place.
        raise RuntimeError("no DBOS notification listener thread found — re-check _stop_notification_listener")
    sys_db._run_background_processes = False
    ended: set[int] = set()
    deadline = time.monotonic() + budget_sec
    while listener.is_alive() and time.monotonic() < deadline:
        # Read per pass (the listener may be between connections, then on a new one), and under the lock its own
        # `_cleanup_connections` closes under, so this read never overlaps that close.
        with sys_db._listener_thread_lock:
            raw = getattr(sys_db.notification_conn, "dbapi_connection", None)
            pid = None if raw is None or raw.closed else raw.info.backend_pid
        if pid and pid not in ended:  # once each: a reused pid may be another process's backend
            ended.add(pid)
            with sys_db.engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = 5000"))
                conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        listener.join(timeout=0.5)
    if listener.is_alive():
        raise RuntimeError(f"the DBOS notification listener did not stop within {budget_sec:g}s")


def destroy_engine(*, strict: bool = False) -> None:
    """Shut the launched engine down without dbos's listener race: the ONE way this codebase destroys an engine.

    Stops dequeuing, gives in-flight workflows `SHUTDOWN_DRAIN_SEC` to record their outcome (destroy's own drain,
    run here so the listener is stopped only after it: the stopped flag also stops the notifier, and a workflow
    waiting on an event would otherwise wait out a polling interval), stops the notification listener on its own
    thread (`_stop_notification_listener`), then destroys. A stop that fails still destroys; `strict` (tests)
    then raises it and waits `STRICT_LISTENER_STOP_SEC` for the listener, the served shutdown logs it. No engine, or one never launched, is just destroyed.
    """
    global _dbos
    from dbos import DBOS
    from dbos import _dbos as dbos_module

    engine = dbos_module._dbos_global_instance
    failure: Exception | None = None
    if engine is not None and engine._launched:
        for event in engine.background_thread_stop_events:
            event.set()
        drain = time.monotonic() + SHUTDOWN_DRAIN_SEC
        while engine._active_workflows_set.activeList() and time.monotonic() < drain:
            time.sleep(0.1)
        if active := engine._active_workflows_set.activeList():
            log.info("durable engine shutdown: %d workflows still active after the drain: %s", len(active), active)
        try:
            _stop_notification_listener(engine, STRICT_LISTENER_STOP_SEC if strict else LISTENER_STOP_SEC)
        except Exception as exc:  # destroy runs regardless: a leaked engine is worse than the race
            failure = exc
    DBOS.destroy()
    _dbos = None
    if failure is not None:
        if strict:
            raise failure
        log.warning("durable engine shutdown: the notification listener was not stopped first (%s)", failure)


async def _drain_and_destroy() -> None:
    """Stop dequeuing, give in-flight workflows `SHUTDOWN_DRAIN_SEC` to record their outcome, then destroy
    (`destroy_engine`).

    On a worker thread because the drain blocks: an async workflow runs on the serving loop and must keep
    stepping through it, and `accounts.dispatch` hops a step back onto that loop.
    """
    from dbos._logger import dbos_logger

    dbos_logger.addFilter(_INTERRUPTED_AT_SHUTDOWN)
    await asyncio.to_thread(destroy_engine)


def _durable_lifespan(inner: Any) -> Any:
    """`inner` (the app's own lifespan) with the engine's launch and teardown around it.

    Launch runs after the app's actors start, directly on the serving loop — never on a worker thread:
    the engine adopts the running loop as the one its coroutines are submitted to. Each replica asks for one
    cron projection pass once its queues are registered (`app/durable/cron_projection.py`): a row this
    process never projected (just seeded, or whose write-path ask failed) reaches the engine there. The
    drain and destroy run after the app's teardown, even when that teardown raised.
    """

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[Any]:
        from dbos import DBOS
        from dbos._logger import dbos_logger

        # Lazy, like `DBOS`: every durable module imports this one.
        from app import boot_phases
        from app.durable import cron_projection, jaah_repo_prefix, task_title_queue

        try:
            async with inner(app) as state:
                boot_phases.done("app.startup")
                dbos_logger.removeFilter(_INTERRUPTED_AT_SHUTDOWN)
                DBOS.launch()
                await register_queues()
                cron_projection.request_projection()
                task_title_queue.request_filed_title_sweep()
                jaah_repo_prefix.request_startup_sweep()
                boot_phases.done("dbos.launch")
                boot_phases.ready()
                yield state
        finally:
            await _drain_and_destroy()

    return lifespan


def reraise_engine_control(exc: BaseException) -> None:
    """The engine's own control flow (a cancel, a recovery conflict) is never a failed run: let it through. Only a
    step whose retries ran out is a failure its workflow records."""
    from dbos import error as dbos_error

    if isinstance(exc, dbos_error.DBOSException) and not isinstance(exc, dbos_error.DBOSMaxStepRetriesExceeded):
        raise exc


def enqueue(
    queue_name: str,
    workflow_id: str,
    func: Any,
    *args: Any,
    enqueue_options: dict[str, Any] | None = None,
) -> Any:
    """Enqueue `func(*args)` on the queue named `queue_name` under `workflow_id`; the workflow's handle.

    The one enqueue primitive, and it raises. The enqueue happens inside `SetWorkflowID`, which collapses
    a duplicate enqueue into an idempotent attach. `enqueue_options` goes to `SetEnqueueOptions`
    (deduplication).
    """
    from dbos import DBOS, SetEnqueueOptions, SetWorkflowID

    # The engine accepts any name and parks a row on an unregistered one forever; only declared ones register.
    if queue_name not in _QUEUES:
        raise ValueError(f"enqueue onto undeclared queue {queue_name!r}")
    options = SetEnqueueOptions(**enqueue_options) if enqueue_options else nullcontext()
    with options, SetWorkflowID(workflow_id):
        return DBOS.enqueue_workflow(queue_name, func, *args)


#: The schema `init` launched the engine on; a test that launches its own engine on a throwaway schema sets it.
system_schema: str = SYSTEM_SCHEMA


def enqueue_in_transaction(
    session: Any, queue_name: str, workflow_id: str, workflow_name: str, *args: Any, priority: int | None = None,
    deduplication_id: str | None = None,
) -> None:
    """Enqueue the workflow registered as `workflow_name` INSIDE `session`'s open transaction: it exists iff that
    transaction commits, so a rolled-back write starts nothing and a committed one always runs.

    The engine's system schema lives in the app's own database (`engine_config`), which is what lets one
    transaction span both. Through `DBOSClient` because the engine itself has no in-transaction enqueue.
    """
    if queue_name not in _QUEUES:
        raise ValueError(f"enqueue onto undeclared queue {queue_name!r}")
    options: dict[str, Any] = {
        "queue_name": queue_name, "workflow_name": workflow_name, "workflow_id": workflow_id,
        "app_version": ACCOUNTS_WORKFLOW_VERSION,
    }
    if priority is not None:
        options["priority"] = priority
    if deduplication_id is not None:
        # A second enqueue under a live one's id raises; a caller that tolerates it holds a savepoint.
        options["deduplication_id"] = deduplication_id
    _client(system_schema).enqueue_in_transaction(session, options, *args)


def migrate_system_schema() -> None:
    """Create or upgrade the engine's system schema without launching it — for a database no served app has
    launched on yet (the test fixture template), whose seed enqueues in-transaction (`enqueue_in_transaction`)."""
    from dbos._serialization import DefaultSerializer
    from dbos._sys_db import SystemDatabase

    from app.db import offline_engine as engine

    sys_db = SystemDatabase.create(
        system_database_url=engine.url.render_as_string(), engine_kwargs={}, engine=engine,
        schema=SYSTEM_SCHEMA, serializer=DefaultSerializer(), executor_id=None,
        # As the served launch has it (DBOS's default): the flag decides whether the migrations create the NOTIFY
        # trigger `send`/`recv` wake on, and a later launch on a clone never adds it. No listener starts here.
        use_listen_notify=True,
    )
    try:
        sys_db.run_migrations()
    finally:
        sys_db.destroy()


#: The statuses of a run the engine would still start or recover: what `cancel_unfinished` cancels.
UNFINISHED_STATUSES: tuple[str, ...] = ("ENQUEUED", "PENDING")


def cancel_unfinished(workflow_names: tuple[str, ...]) -> int:
    """Cancel every enqueued or pending run of `workflow_names` and return how many — for a workflow whose module
    this process does not carry, which the engine could otherwise never finish. 0 where the engine's schema does not
    exist yet (a database no served app has launched on). Runs on its own connection, outside any caller's
    transaction."""
    from sqlalchemy import inspect

    from app.db import offline_engine as engine

    if not workflow_names or not inspect(engine).has_table("workflow_status", schema=system_schema):
        return 0
    client = _client(system_schema)
    ids = [status.workflow_id for status in client.list_workflows(
        name=list(workflow_names), status=list(UNFINISHED_STATUSES), load_input=False, load_output=False)]
    if ids:
        client.cancel_workflows(ids)
    return len(ids)


#: The statuses of a run that ended: what `finished_runs` lists.
FINISHED_STATUSES: tuple[str, ...] = ("SUCCESS", "ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED")


def finished_runs(workflow_name: str, *, limit: int) -> list[Any]:
    """The newest `limit` ended runs of `workflow_name`, newest first, with their outputs (the engine's
    `WorkflowStatus`); empty where the engine's schema does not exist yet. Its own connection, as `cancel_unfinished`."""
    from sqlalchemy import inspect

    from app.db import offline_engine as engine

    if not inspect(engine).has_table("workflow_status", schema=system_schema):
        return []
    return list(_client(system_schema).list_workflows(
        name=workflow_name, status=list(FINISHED_STATUSES), sort_desc=True, limit=limit, load_input=False,
        load_output=True))


def latest_run(workflow_id_prefix: str) -> Optional[tuple[str, int]]:
    """The newest run whose id starts with `workflow_id_prefix`: its status and when it last moved (epoch ms: its
    newest step's end, else its own last update). None when there is none, or the engine's schema does not exist
    yet. Its own connection, as `cancel_unfinished`."""
    from sqlalchemy import inspect

    from app.db import offline_engine as engine

    if not inspect(engine).has_table("workflow_status", schema=system_schema):
        return None
    client = _client(system_schema)
    runs = client.list_workflows(workflow_id_prefix=workflow_id_prefix, sort_desc=True, limit=1, load_input=False,
                                 load_output=False)
    if not runs:
        return None
    run = runs[0]
    moved = [run.updated_at or run.created_at or 0]
    moved += [step["completed_at_epoch_ms"] or step["started_at_epoch_ms"] or 0
              for step in client.list_workflow_steps(run.workflow_id, load_output=False)]
    return str(run.status), max(moved)


def cancel_unfinished_runs(workflow_id_prefix: str) -> int:
    """Cancel every enqueued or pending run whose id starts with `workflow_id_prefix`; how many. Its own connection,
    outside any caller's transaction, as `cancel_unfinished`."""
    from sqlalchemy import inspect

    from app.db import offline_engine as engine

    if not inspect(engine).has_table("workflow_status", schema=system_schema):
        return 0
    client = _client(system_schema)
    ids = [status.workflow_id for status in client.list_workflows(
        workflow_id_prefix=workflow_id_prefix, status=list(UNFINISHED_STATUSES), load_input=False, load_output=False)]
    if ids:
        client.cancel_workflows(ids)
    return len(ids)


@functools.cache
def _client(schema: str) -> Any:
    """One client per schema, over the app's offline engine (`app/db.py`: nothing pools on it in production), so
    it opens no pool of its own. Its in-transaction enqueue runs on the caller's session."""
    from dbos import DBOSClient

    from app.db import offline_engine as engine

    return DBOSClient(system_database_engine=engine, dbos_system_schema=schema, application_name=APP_NAME, lazy=True)


def enqueue_or_warn(
    queue_name: str,
    workflow_id: str,
    func: Any,
    *args: Any,
    log: logging.Logger,
    failure: str,
    consequence: str,
    enqueue_options: dict[str, Any] | None = None,
) -> bool:
    """`enqueue`, as the post-commit enqueue that never raises. True when the engine accepted it.

    An unreachable DBOS must not turn a committed success into a 500 or kill the calling listener loop,
    so a failure is one warning and a False. `failure` names what was not enqueued, `consequence` what
    happens instead.
    """
    try:
        enqueue(queue_name, workflow_id, func, *args, enqueue_options=enqueue_options)
    except Exception as exc:  # noqa: BLE001 — see the docstring: a follow-up never fails its cause
        log.warning("%s (%s: %s) — %s", failure, type(exc).__name__, exc, consequence)
        return False
    return True
