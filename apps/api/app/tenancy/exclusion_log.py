"""The durable exclusion log's writer: an in-process aggregator and the flush that upserts it.

The count over time behind gate G6 (an empty log across ≥ 7 days before a first external tenant);
the seam's console trail only says which paths. Row: `app/models/orgs.py::TenancyExclusion`.

`record()` runs inside `do_orm_execute`, so it only mutates a dict under a lock; a timed flush writes.
Buckets are (class, scope_kind, org, route, minute); the unique constraint plus an additive upsert let
processes converge on one row. `scope_kind_of()` is shared with the seam's console reasons.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterator, NamedTuple, Optional
from uuid import UUID

from app.clock import as_utc

log = logging.getLogger("tenancy-exclusion-log")


# ── `scope_kind`: what the scope was, and therefore whether the exclusion was legitimate ─────────────

#: Nothing claimed an org. The predicate is `false()` and the seam enforces, so the read returns
#: nothing. The value an operator hunts.
SCOPE_KIND_UNBOUND = "unbound"

#: Inside `identity_scope()` on a class outside `IDENTITY_CLASSES`: `false()` too. The caller bound the
#: escape and reached past it.
SCOPE_KIND_IDENTITY_DENIED = "identity_denied"

#: A real org bound and the read restricted to it: the correct case, recorded so wrongly-refused reads
#: can be compared against the volume of correctly-refused ones.
SCOPE_KIND_ORG = "org"

#: Operator scope: no predicate. Never recorded for a read; recorded for an unstamped write.
SCOPE_KIND_OPERATOR = "operator"

#: Write only: an org was bound but the `Derived` instance's parent has no `org_id` to inherit, so the
#: child stays NULL (stamping the bound org would violate the composite key; see
#: `app/tenancy/write_seam.py`). Separate from `unbound` because the remedy differs: stamp the parent,
#: not bind a caller.
SCOPE_KIND_DERIVED_UNSTAMPED = "derived_unstamped"

#: Inside `identity_scope()` on a class the escape covers — no predicate applies. Never reaches the table.
SCOPE_KIND_IDENTITY_OPEN = "identity_open"

#: Every value the column can carry, across both producers. `identity_open` is read-only and
#: `derived_unstamped` write-only, so the totality test asserts the union. What reaches the table is
#: per mode (`RECORDED_SCOPE_KINDS`).
SCOPE_KINDS: tuple[str, ...] = (
    SCOPE_KIND_UNBOUND,
    SCOPE_KIND_IDENTITY_DENIED,
    SCOPE_KIND_ORG,
    SCOPE_KIND_OPERATOR,
    SCOPE_KIND_IDENTITY_OPEN,
    SCOPE_KIND_DERIVED_UNSTAMPED,
)

#: The scopes a read excludes under, and so the set a `read` row reaches the table with. Most-alarming
#: first (the page's filter order).
EXCLUDING_SCOPE_KINDS: tuple[str, ...] = (
    SCOPE_KIND_UNBOUND,
    SCOPE_KIND_IDENTITY_DENIED,
    SCOPE_KIND_ORG,
)

#: The scopes a write is unstamped under. The mirror of the read set: `operator` is in, `org` out.
#: Gating writes on the read set would drop operator-plane creates, the largest population. `operator`
#: is kept apart from `unbound` so deliberate fleet-wide binds do not drown forgotten ones
#: (`app/routes/tenancy_exclusions.py`).
UNSTAMPED_SCOPE_KINDS: tuple[str, ...] = (
    SCOPE_KIND_UNBOUND,
    SCOPE_KIND_IDENTITY_DENIED,
    SCOPE_KIND_OPERATOR,
    SCOPE_KIND_DERIVED_UNSTAMPED,
)


def scope_kind_of(scope: object, *, restricted: bool) -> str:
    """Which of `SCOPE_KINDS` this (scope, restricted) pair is.

    `restricted` (the caller's `criteria is not None`) is passed in because the identity escape leaves
    `IDENTITY_CLASSES` unfiltered and everything else shut, which the scope alone cannot tell apart.
    The read producer; `write_scope_kind_of` is the write one.

    Takes `object` to keep this module free of a seam import (the seam imports this one). The seam
    module is memoised because this runs twice per tenant-owned class per ORM select.
    """
    seam = _seam()

    if scope is None:
        return SCOPE_KIND_UNBOUND
    if scope is seam.IDENTITY:
        return SCOPE_KIND_IDENTITY_DENIED if restricted else SCOPE_KIND_IDENTITY_OPEN
    if scope is seam.OPERATOR:
        return SCOPE_KIND_OPERATOR
    return SCOPE_KIND_ORG


def write_scope_kind_of(scope: object, *, derived_unstamped: bool = False) -> str:
    """Which of `SCOPE_KINDS` an unstamped write happened under. The write path's `scope_kind_of`.

    `derived_unstamped` is passed in by the caller that knows and wins over the scope (the finding is
    about the parent). No `restricted`: a flush has no per-class predicate; a write inside the identity
    escape is unstamped regardless and reports `identity_denied`. A real org returns `org`, which
    `RECORDED_SCOPE_KINDS` drops for `unbound_write`, keeping this function total over `Scope`."""
    if derived_unstamped:
        return SCOPE_KIND_DERIVED_UNSTAMPED

    seam = _seam()

    if scope is None:
        return SCOPE_KIND_UNBOUND
    if scope is seam.IDENTITY:
        return SCOPE_KIND_IDENTITY_DENIED
    if scope is seam.OPERATOR:
        return SCOPE_KIND_OPERATOR
    return SCOPE_KIND_ORG


#: The seam module, resolved once (see `scope_kind_of`).
_SEAM = None


def _seam():
    global _SEAM
    if _SEAM is None:
        from app.tenancy import seam

        _SEAM = seam
    return _SEAM


# ── `mode`: which kind of statement the predicate reached ────────────────────────────────────────────

#: An ORM SELECT that would have returned less than it asked for.
MODE_READ = "read"

#: A write reaching a tenant-owned table with no org to stamp, written by
#: `app/tenancy/write_seam.py::stamp_org` (home of why it is counted, not refused).
MODE_UNBOUND_WRITE = "unbound_write"

MODES: tuple[str, ...] = (MODE_READ, MODE_UNBOUND_WRITE)


# ── Which scope kinds reach the table, per mode ───────────────────────────────────────────────────────

#: The gate `record()` applies, keyed on `mode`. A map so a new mode must state its answer; a mode with
#: no entry records nothing, and the test pins the key set to `MODES`.
RECORDED_SCOPE_KINDS: dict[str, tuple[str, ...]] = {
    MODE_READ: EXCLUDING_SCOPE_KINDS,
    MODE_UNBOUND_WRITE: UNSTAMPED_SCOPE_KINDS,
}

#: Every value that can land in the column, across both modes, most-alarming first: the page's filter
#: vocabulary and the route's validation set.
ALL_RECORDED_SCOPE_KINDS: tuple[str, ...] = (
    SCOPE_KIND_UNBOUND,
    SCOPE_KIND_IDENTITY_DENIED,
    SCOPE_KIND_DERIVED_UNSTAMPED,
    SCOPE_KIND_OPERATOR,
    SCOPE_KIND_ORG,
)


# ── `route_or_job`: who was calling ──────────────────────────────────────────────────────────────────

#: No caller bound a label (startup, tests, tools). Not NULL, so the column never reads as unreliable.
UNATTRIBUTED = "unattributed"

#: `route_or_job` is `String(200)`; longer labels are truncated, not dropped.
_LABEL_MAX = 200

_CALLER: ContextVar[str] = ContextVar("tenancy_exclusion_caller", default=UNATTRIBUTED)


def caller_label() -> str:
    """The label the current context is attributed to, or `UNATTRIBUTED`."""
    return _CALLER.get()


def bind_caller(label: str) -> None:
    """Attribute this context to `label`. Call it only from an `async def`, or from the same call stack
    that will do the reads.

    As with `seam.bind`: a `def` dependency or `run_in_threadpool` runs on a copied context in a
    worker thread, so the set dies with it. Not test-gated, since the cost is a label, not a scope.
    """
    _CALLER.set(_fit(label))


@contextmanager
def caller(label: str) -> Iterator[None]:
    """Attribute the reads made inside this block to `label`, and restore whatever was there on exit.

    For synchronous callers (cron activity, tool); `bind_caller` is for a request.
    """
    token = _CALLER.set(_fit(label))
    try:
        yield
    finally:
        _CALLER.reset(token)


def _fit(label: str) -> str:
    label = (label or "").strip() or UNATTRIBUTED
    return label if len(label) <= _LABEL_MAX else label[: _LABEL_MAX - 1] + "…"


def label_for_connection(scope: dict) -> str:
    """`"GET /api/tasks"` for an HTTP request, `"WS /api/console/{slot}"` for a socket.

    The route template, never the resolved path, so buckets aggregate and task ids stay out of the log.
    The raw-path fallback guards against a FastAPI change (the matched route normally sets
    `scope["route"]`); re-verify rather than delete it. Falls back to `UNATTRIBUTED` with no path. Reads
    the ASGI scope as a mapping because the call site must also work on WebSocket routes.
    """
    route = scope.get("route")
    template = getattr(route, "path", None) or scope.get("path")
    if not template:
        return UNATTRIBUTED
    if scope.get("type") == "websocket":
        return _fit(f"WS {template}")
    method = scope.get("method") or "?"
    return _fit(f"{method} {template}")


# ── The buffer ───────────────────────────────────────────────────────────────────────────────────────

#: `(occurred_at, mapped_class, scope_kind, bound_org, route_or_job, mode) -> count`: exactly the
#: unique constraint's columns, in order.
_Bucket = tuple[datetime, str, str, Optional[UUID], str, str]

_BUFFER: dict[_Bucket, int] = {}
_LOCK = threading.Lock()

#: Distinct buckets one process holds between flushes before new ones are folded (see `_overflow`), so
#: an unforeseen minute cannot grow the buffer without limit.
_BUFFER_CAP = 4096

#: Where an over-cap bucket's count goes: folded into one row that says the breakdown was lost, never
#: dropped. The overflow key omits org and route, so its space (minute × class × scope_kind × mode) is
#: small and the dict stays bounded.
_OVERFLOW_LABEL = "(breakdown dropped: buffer cap reached)"


def _overflow(bucket: _Bucket) -> _Bucket:
    """The bucket an over-cap event is folded into: same minute, class, scope and mode; no org, no route."""
    occurred_at, mapped_class, scope_kind, _org, _route, mode = bucket
    return (occurred_at, mapped_class, scope_kind, None, _OVERFLOW_LABEL, mode)


def record(
    *,
    mapped_class: str,
    scope_kind: str,
    bound_org: Optional[UUID],
    mode: str = MODE_READ,
    count: int = 1,
    route_or_job: Optional[str] = None,
    at: Optional[datetime] = None,
) -> None:
    """Add `count` to this event's bucket. In memory only — no I/O, no session, no ORM.

    Called inside `do_orm_execute`, so it must not raise or be slow: a lock around a dict mutation
    (sync handlers run on a 40-thread pool). Scopes the `mode` has nothing to say about are dropped here
    per `RECORDED_SCOPE_KINDS`, the rule's one home.
    """
    if scope_kind not in RECORDED_SCOPE_KINDS.get(mode, ()) or count <= 0:
        return
    bucket: _Bucket = (
        _minute(at),
        mapped_class,
        scope_kind,
        bound_org,
        _fit(route_or_job if route_or_job is not None else caller_label()),
        mode,
    )
    with _LOCK:
        if bucket not in _BUFFER and len(_BUFFER) >= _BUFFER_CAP:
            bucket = _overflow(bucket)
        _BUFFER[bucket] = _BUFFER.get(bucket, 0) + count


def _minute(at: Optional[datetime]) -> datetime:
    """The bucket's timestamp: `at` (or now) truncated to the minute, in UTC.

    Always UTC and aware, so processes in different zones share a bucket.
    """
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(second=0, microsecond=0)


def buffered() -> int:
    """How many distinct buckets are waiting to be flushed. For tests and for the flusher's own log."""
    with _LOCK:
        return len(_BUFFER)


def reset() -> None:
    """Discard the buffer without writing it. For tests — never a production path."""
    with _LOCK:
        _BUFFER.clear()


# ── When this log began, and whether it is still being written ───────────────────────────────────────

#: The `system_state` key holding the instant `tenancy_exclusions` came into existence, written once by
#: revision 0180. It floors the page's "no exclusions since" window, which would otherwise claim a full
#: retention period (365 days) of evidence never gathered. A fixed instant, not `min(occurred_at)`,
#: which would shrink as the log went quiet. Combined in `app/routes/tenancy_exclusions.py`.
LOG_STARTED_AT_KEY = "tenancy_exclusion_log_started_at"


def log_started_at(*, session_factory=None) -> Optional[datetime]:
    """The instant this log began covering anything, or None if the marker is missing.

    None is a real answer (pre-marker DB, hand-deleted row): the caller falls back to the retention
    window alone.
    """
    from app.db import SessionLocal
    from app.models.control_plane import SystemState

    factory = session_factory or SessionLocal
    with factory() as session:
        row = session.get(SystemState, LOG_STARTED_AT_KEY)
    # Every shape a hand-edited JSONB row can take is a `None`, never a 500 in the page's request.
    try:
        raw = (row.value or {}).get("at") if row is not None else None
        moment = datetime.fromisoformat(raw) if raw else None
    except (AttributeError, TypeError, ValueError):
        log.warning(
            "tenancy exclusion log: %s does not hold an instant (%r) — the exclusion page falls back to "
            "the retention window alone", LOG_STARTED_AT_KEY, row.value if row is not None else None,
        )
        return None
    if moment is None:
        return None
    return as_utc(moment)


class WriterHealth(NamedTuple):
    """Whether this process's flusher is still landing its writes.

    Process-local: each replica flushes on its own; still useful because the failures it catches are
    systemic. Served because the flusher and `read_seam` swallow failures, and otherwise "the log is
    empty" and "the log stopped being written" look identical."""

    ok: bool
    #: The last time a flush committed, or None if none has in this process's life.
    last_ok_at: Optional[datetime]
    #: How many flushes have failed in a row. Zero whenever the last one succeeded.
    consecutive_failures: int
    #: The last failure, reduced to `TypeName: message` by `_first_line`.
    last_error: Optional[str]


#: Consecutive failures before the page stops trusting the log: one transient error during a deploy is
#: normal; three minutes of silence is not.
UNHEALTHY_AFTER_FAILURES = 3

_HEALTH_LOCK = threading.Lock()
_LAST_FLUSH_OK_AT: Optional[datetime] = None
_CONSECUTIVE_FAILURES = 0
_LAST_FLUSH_ERROR: Optional[str] = None


def writer_health() -> WriterHealth:
    """This process's flusher, as an operator surface should read it."""
    with _HEALTH_LOCK:
        return WriterHealth(
            ok=_CONSECUTIVE_FAILURES < UNHEALTHY_AFTER_FAILURES,
            last_ok_at=_LAST_FLUSH_OK_AT,
            consecutive_failures=_CONSECUTIVE_FAILURES,
            last_error=_LAST_FLUSH_ERROR,
        )


#: Max length of an operator-facing error string.
_ERROR_MAX = 300


def _first_line(error: BaseException) -> str:
    """`TypeName: message`, with everything SQLAlchemy appends after the first line removed.

    `StatementError.__str__` appends `[SQL: ...]` and `[parameters: ...]` lines, which must not reach an
    operator's browser (`hide_parameters=True` holds only for `app/db.py`'s engine). The first line is
    psycopg's diagnosis; the full detail stays in the flusher's `log.exception`.
    """
    first = str(error).split("\n", 1)[0].strip()
    detail = f"{type(error).__name__}: {first}" if first else type(error).__name__
    return detail if len(detail) <= _ERROR_MAX else detail[: _ERROR_MAX - 1] + "\u2026"


def _record_flush_outcome(error: Optional[Exception]) -> None:
    """One home for the health counters, so the two callers cannot move them differently."""
    global _LAST_FLUSH_OK_AT, _CONSECUTIVE_FAILURES, _LAST_FLUSH_ERROR
    with _HEALTH_LOCK:
        if error is None:
            _LAST_FLUSH_OK_AT = datetime.now(timezone.utc)
            _CONSECUTIVE_FAILURES = 0
            _LAST_FLUSH_ERROR = None
            return
        _CONSECUTIVE_FAILURES += 1
        _LAST_FLUSH_ERROR = _first_line(error)


def reset_health() -> None:
    """Forget the health counters. For tests — never a production path."""
    global _LAST_FLUSH_OK_AT, _CONSECUTIVE_FAILURES, _LAST_FLUSH_ERROR
    with _HEALTH_LOCK:
        _LAST_FLUSH_OK_AT = None
        _CONSECUTIVE_FAILURES = 0
        _LAST_FLUSH_ERROR = None


# ── The flush ────────────────────────────────────────────────────────────────────────────────────────

def _upsert_statement():
    """`INSERT ... ON CONFLICT ON CONSTRAINT <the bucket key> DO UPDATE SET excluded_count = old + new`.

    Adding, not replacing, so per-process buffers sum across the fleet. Built from
    `TenancyExclusion.__table__`, not typed SQL, so a widened bucket key cannot leave this inserting NULL
    into a new column (a silent failure, since flush errors are swallowed). `constraint=` skips index
    inference, so no other unique index can satisfy it. Built per call (once a minute).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.orgs import TenancyExclusion

    table = TenancyExclusion.__table__
    stmt = pg_insert(table)
    return stmt.on_conflict_do_update(
        constraint=_bucket_constraint(table),
        set_={"excluded_count": table.c.excluded_count + stmt.excluded.excluded_count},
    )


def _bucket_constraint(table):
    """The bucket key on `tenancy_exclusions`, found rather than named.

    The table's one unique constraint; raises rather than guessing if a second ever appears."""
    from sqlalchemy import UniqueConstraint

    found = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    if len(found) != 1:
        raise RuntimeError(
            f"expected exactly one unique constraint on {table.name} (the aggregation bucket), "
            f"found {[c.name for c in found]!r} — name the intended one explicitly"
        )
    return found[0]


def flush(*, session_factory=None) -> int:
    """Write every buffered bucket and return how many were written. Safe to call with nothing buffered.

    The buffer is drained before the write: a failed flush loses its counts (logged, bounded by the
    interval) rather than risking a double count, which would be a false positive on the gating signal.

    Always its own session (background task, or shutdown). Binds no scope: `TenancyExclusion` is
    `Shared`, and `read_seam` skips non-selects, so the flush cannot log itself; and `seam.bind` is
    async-only while this runs on a worker thread.
    """
    from app.db import SessionLocal

    with _LOCK:
        if not _BUFFER:
            return 0
        batch = list(_BUFFER.items())
        _BUFFER.clear()

    factory = session_factory or SessionLocal
    rows = [
        {
            "occurred_at": occurred_at,
            "mapped_class": mapped_class,
            "scope_kind": scope_kind,
            "bound_org": bound_org,
            "route_or_job": route_or_job,
            "mode": mode,
            "excluded_count": count,
        }
        for (occurred_at, mapped_class, scope_kind, bound_org, route_or_job, mode), count in batch
    ]
    # The outcome is recorded here so any caller (test, shutdown) moves the same counters; the `raise`
    # is unconditional. `Exception`, not `BaseException`: cancellation is teardown, not a writer failure.
    try:
        with factory() as session:
            session.execute(_upsert_statement(), rows)
            session.commit()
    except Exception as exc:
        _record_flush_outcome(exc)
        raise
    _record_flush_outcome(None)
    return len(rows)


# ── The flusher ──────────────────────────────────────────────────────────────────────────────────────

#: One flush per minute, the bucket resolution: more buys only writes; less risks more loss on a kill.
FLUSH_INTERVAL_SECONDS = 60


async def _flush_forever(stop: asyncio.Event) -> None:
    """Flush on the interval until asked to stop, then flush once more.

    Every failure is swallowed and logged, so a transient error cannot silently end the trail. The stop
    test is at the bottom, so a stop during the interval wait still gets a final flush on every deploy.
    """
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=FLUSH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        stopping = stop.is_set()
        try:
            written = await asyncio.to_thread(flush)
        except Exception:  # noqa: BLE001 — see the docstring
            log.exception("tenancy exclusion log: a flush failed; its counts are lost")
        else:
            if written:
                log.info("tenancy exclusion log: wrote %d bucket(s)", written)
        # `stopping` is read before the flush: a stop landing during the flush gets one more iteration
        # (its wait returns at once), so counts buffered meanwhile are not dropped.
        if stopping:
            return


async def start_exclusion_flusher(app) -> None:
    """Lifespan startup hook: spawn the flusher. Mirrors the other actors in `app/main.py::_lifespan`."""
    stop = asyncio.Event()
    app.state.exclusion_flusher_stop = stop
    app.state.exclusion_flusher_task = asyncio.create_task(
        _flush_forever(stop), name="tenancy:exclusion-flush"
    )


async def stop_exclusion_flusher(app) -> None:
    """Lifespan shutdown hook: ask the loop to stop, let it make its final flush, then cancel.

    The await before the cancel carries the last partial minute to the table; the bounded cancel is the
    backstop for a flush wedged on a departing database.
    """
    stop = getattr(app.state, "exclusion_flusher_stop", None)
    task = getattr(app.state, "exclusion_flusher_task", None)
    if stop is not None:
        stop.set()
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=FLUSH_INTERVAL_SECONDS / 4)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown swallows everything
            pass
    app.state.exclusion_flusher_task = None
