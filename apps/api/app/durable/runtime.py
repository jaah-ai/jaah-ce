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
reads go through `app.db.SessionLocal` (`test_tenancy_plane_sites.py`), binding from the step's own
argument so a recovered replay binds the same org.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Iterator, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

APP_NAME = "jaah"
# The name rows carried before `APP_NAME`. Migration 202610291800 moved every row; a release still draining
# during that deploy can write more under it; launch and retention adopt those (`cron_retention.adopt_retired_dbos_rows`).
RETIRED_APP_NAME = "cto-cockpit-durable"
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

#: How often an idle queue is polled, in seconds. Every registered queue is one engine thread per api task,
#: waking this often to reload its config and try a dequeue — ~8% of the api's CPU at the engine's 1s default
#: across a dozen mostly-empty queues; 10s, not 5s, halves those idle polls again, at
#: up to ~10s before queued work starts. Work a person waits on polls at `INTERACTIVE_POLL_SEC` instead; this
#: is for background work nobody waits on. A queue with a tighter bound names its own `POLL_SEC` (the durable
#: `cloud_node` and `live_cast` modules, and the token-billing effect's).
BACKGROUND_POLL_SEC = 10.0

#: How long shutdown lets in-flight workflows finish and record their outcome before the engine is torn down.
#: It runs after uvicorn's graceful window and the app's teardown, inside the api container's
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
    name its reads) around a fresh `SessionLocal`, for the step to `seam.bind_from` on.

    `bind_from` stays at the call site (or in `delegate_for_task`, the one shared site the tenancy scope census
    attributes for the steps that hand one task to a delegate). With `gone`, a `TenancyRowMissing` (the row was deleted while queued) is suppressed —
    logged at INFO on `logger` unless `gone` is empty — and control falls through to the statement after
    the `with`, the step's nothing-to-do return; without it, it propagates.
    """
    # Deferred: every durable module imports this one at DBOS registration time, before the app is wired.
    from app.db import SessionLocal
    from app.tenancy import exclusion_log, seam

    with exclusion_log.caller(caller), SessionLocal() as session:
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
    from app.config import db_pool_sizes
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
        # `app/db.py::_pool_kwargs`). Unset -> None -> the library default (20).
        "sys_db_pool_size": db_pool_sizes().dbos_pool_size,
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


async def _drain_and_destroy() -> None:
    """Stop dequeuing, give in-flight workflows `SHUTDOWN_DRAIN_SEC` to record their outcome, then destroy.

    On a worker thread because the drain blocks: an async workflow runs on the serving loop and must keep
    stepping through it, and `accounts.dispatch` hops a step back onto that loop.
    """
    from dbos import DBOS
    from dbos._logger import dbos_logger

    dbos_logger.addFilter(_INTERRUPTED_AT_SHUTDOWN)
    await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=SHUTDOWN_DRAIN_SEC)


# When the previous release may still be draining after a deploy: adopt what it wrote under the retired name
# then, rather than at the next daily retention sweep.
_ADOPT_AFTER_LAUNCH_SEC = (0.0, 60.0, 300.0, 900.0)


async def _adopt_retired_rows_while_draining() -> None:
    from app.cron_reconcile import launched_engine
    from app.cron_retention import adopt_retired_dbos_rows

    waited = 0.0
    for at in _ADOPT_AFTER_LAUNCH_SEC:
        await asyncio.sleep(at - waited)
        waited = at
        engine = launched_engine()
        if engine is None:
            return
        await asyncio.to_thread(adopt_retired_dbos_rows, engine)


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
        from app.durable import cron_projection, task_title_queue

        adoption: asyncio.Task[None] | None = None
        try:
            async with inner(app) as state:
                boot_phases.done("app.startup")
                dbos_logger.removeFilter(_INTERRUPTED_AT_SHUTDOWN)
                DBOS.launch()
                await register_queues()
                cron_projection.request_projection()
                task_title_queue.request_filed_title_sweep()
                adoption = asyncio.get_running_loop().create_task(_adopt_retired_rows_while_draining())
                boot_phases.done("dbos.launch")
                boot_phases.ready()
                yield state
        finally:
            if adoption is not None:
                adoption.cancel()
            await _drain_and_destroy()

    return lifespan


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

    from app.db import engine

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

    from app.db import engine

    if not workflow_names or not inspect(engine).has_table("workflow_status", schema=system_schema):
        return 0
    client = _client(system_schema)
    ids = [status.workflow_id for status in client.list_workflows(
        name=list(workflow_names), status=list(UNFINISHED_STATUSES), load_input=False, load_output=False)]
    if ids:
        client.cancel_workflows(ids)
    return len(ids)


@functools.cache
def _client(schema: str) -> Any:
    """One client per schema, over the app's own engine, so it opens no pool of its own."""
    from dbos import DBOSClient

    from app.db import engine

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
