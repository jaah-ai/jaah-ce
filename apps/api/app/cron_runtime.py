"""The invocation wrapper: the one seam every cron firing passes through, plus `fire_now_sync`.

A DBOS schedule thread enqueues `sched-cron:<uuid>-<iso>` (deterministic, so racing replicas collide
and one runs); a DBOS worker thread calls `run_cron_tick`, where everything that makes a firing safe
lives. `app/cron_reconcile.py` projects `crons` onto DBOS schedules, as a pass on the `cron-projection`
queue every write and each startup asks for (`app/durable/cron_projection.py`); the projection persists in
DBOS's schema, so there is no periodic backstop.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
import math
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from decimal import Decimal
from typing import Any, Callable, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import automation_alerts, cron_activities, cron_alarms, cron_min_period
from app.operator_settings import reads as settings_reads
from app.cron_schedule import CronScheduleError, next_fire_for_row
from app.crons import (
    RUN_OUTCOME_ACTED,
    RUN_OUTCOME_ERROR,
    RUN_OUTCOME_MISSED,
    RUN_OUTCOME_NOOP,
    RUN_OUTCOME_SKIPPED,
    SKIP_ALREADY_RUNNING,
    SKIP_BELOW_MIN_PERIOD,
    SKIP_DISABLED,
    SKIP_PREVIOUS_TASK_OPEN,
    should_record,
)
from app.db import SessionLocal, background_executor, background_lane
from app.entity_shape import new_uuid7
from app import db_credentials
from app.models.automations import Cron, CronRun
from app.tenancy import exclusion_log, seam

log = logging.getLogger("cockpit-cron")

# The order in `run_cron_tick` is the design: heartbeat first (no early return skips it), then the
# session-scoped lock on its own connection, then the dedicated executor (see `_bounded_call`).

# ── Names and bounds ──────────────────────────────────────────────────────────────────────────────────

# Namespaced so the reconciler's orphan sweep never deletes a schedule this feature did not create.
SCHEDULE_NAME_PREFIX = "cron:"

# The run lock's own key space: the `(int4, int4)` advisory space is disjoint from the bigint one,
# so it cannot collide with `hashtext`-keyed holders (a 32-bit hash, so prefixes alone could). Fixed forever.
CRON_RUN_LOCK_CLASSID = 0x63726F6E  # 'cron'
_RUN_LOCK_PREFIX = "cron_run:"

# Opened before the hard bound exists, so it needs its own; a blackholed DB would park a thread per tick.
LOCK_CONNECT_TIMEOUT_SECONDS = 5

# The last-resort bound; generous, since a short bound reports a healthy activity as an error.
DEFAULT_TIMEOUT_SECONDS = 300.0

# The wedge threshold (AC10.3); counts `already_running`/`previous_task_open`/`error` only.
SKIP_ESCALATION_THRESHOLD = 3

# Ten rows is the acceptance criterion; the scan cap bounds a long outage's cost, and the overflow
# text says so rather than inventing a number.
MISSED_ROW_CAP = 10
MISSED_SCAN_CAP = 1000

# An exception `repr` is arbitrary third-party text shown to an operator.
MAX_DETAIL_CHARS = 2000

# One control-plane-wide zone, read per evaluation. The knob and its default live in
# `app/operator_settings/catalog.py`'s CATALOG; the constant is the last resort for an unreadable store or an
# unresolvable name. UTC, because firing in the wrong zone is recoverable and not firing is not.
CRON_TIMEZONE_SETTING_KEY = "cron_timezone"
DEFAULT_CRON_TIMEZONE = "UTC"

# The tail of the `error` row a firing that outran its hard timeout leaves; read back by the automation status
# (`automation_service.stuck_run`) to tell a firing still holding the lock from one that merely failed.
ABANDONED_DETAIL_TAIL = "was abandoned"

# Latency only; correctness rests on the lock and the hold.
FIRE_COALESCE_SECONDS = 1.0

# Why `fire_now_sync` enqueued nothing, reported through its `on_miss` so Run now can name the cause.
FIRE_MISS_UNSCHEDULED = "unscheduled"  # the engine holds no schedule for the row (pre-reconcile, or deleted)
FIRE_MISS_COALESCED = "coalesced"  # another fire of this cron was enqueued within `FIRE_COALESCE_SECONDS`
FIRE_MISS_OTHER = "other"  # the row is gone, disabled or floorless, or the trigger raised something else


def schedule_name(cron_id: UUID | str) -> str:
    """The DBOS schedule name for one cron row, derived so no caller spells it differently."""
    return f"{SCHEDULE_NAME_PREFIX}{cron_id}"


@dataclass(frozen=True)
class TickResult:
    """What one delivered tick came to, returned so tests can assert the decision (hold vs drop)
    without reading the ledger."""

    outcome: str
    skip_reason: Optional[str] = None
    detail: str = ""
    recorded: bool = False
    held_seconds: float = 0.0


@dataclass(frozen=True)
class _Decision:
    """What the body decided this firing came to, before it is recorded.

    Separate so `_invoke` settles in one place, letting the abandoned-firing check stop a timed-out body
    writing a second ledger row.
    """

    outcome: str
    skip_reason: Optional[str] = None
    detail: str = ""
    run_id: Optional[UUID] = None
    started_at: Optional[datetime] = None
    task_id: Optional[int] = None
    # The run row was written up front by the fire engine's path (`_run_fire`): settled by update, not insert.
    provisional: bool = False


class _RunClaim:
    """The handoff between a firing's body and its calling thread: which run row the body wrote up front.

    The body writes and commits that row, and records its id, under `guard`, and only while the firing is not yet
    abandoned; the calling thread reads the id under the same `guard` after declaring the abandon. So an overrun
    either finds the id (and settles that row) or the body never writes one: never two rows for one firing, and
    never another firing's row.
    """

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.run_id: Optional[UUID] = None


# ── The global timezone ───────────────────────────────────────────────────────────────────────────────


def cron_timezone_name() -> str:
    """The configured zone's IANA name, or `UTC`. Never raises.

    Read per call (`operator_settings.reads.get_str`, 3s cache), never snapshotted, so a save takes effect
    without a restart.
    """
    try:
        chosen = settings_reads.get_str(CRON_TIMEZONE_SETTING_KEY)
    except Exception:  # noqa: BLE001 — KeyError (no such knob) and any read failure land the same way
        return DEFAULT_CRON_TIMEZONE
    return chosen.strip() or DEFAULT_CRON_TIMEZONE


def cron_timezone() -> tzinfo:
    """The configured zone as a `tzinfo`, falling back to UTC with a warning on an unresolvable name.
    """
    name = cron_timezone_name()
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — any resolver failure, including a stripped tzdata
        log.warning("cron: timezone %r is not resolvable on this host — evaluating in UTC", name)
        return timezone.utc


def resolved_timezone_name() -> str:
    """The zone name the wrapper will actually evaluate in, guaranteed resolvable.

    Handed to DBOS instead of the raw knob: `apply_schedules` is all-or-nothing and one bad string would
    stop the projection for every cron.
    """
    return getattr(cron_timezone(), "key", DEFAULT_CRON_TIMEZONE)


# ── The run lock ──────────────────────────────────────────────────────────────────────────────────────

_TRY_LOCK_SQL = (
    "SELECT pg_try_advisory_lock(%(classid)s, k) AS locked, k "
    "FROM (SELECT hashtext(%(name)s) AS k) AS keyed"
)


class RunLock:
    """A session-scoped `pg_try_advisory_lock` on its own dedicated connection.

    Session-scoped so a dying process frees the lock (no lease expiry to tune). Its own connection because
    a pooled connection carrying a session lock hands it to the next checkout.
    """

    def __init__(self, cron_id: UUID | str) -> None:
        self.name = f"{_RUN_LOCK_PREFIX}{cron_id}"
        self._conn: Optional[psycopg.Connection] = None
        self._key: Optional[int] = None
        self._guard = threading.Lock()

    def acquire(self) -> bool:
        """True iff this process now holds the lock. False means another firing of the same cron is live."""
        # Through `db_credentials`, not `psycopg` directly: a raw dialer outside the `do_connect`
        # chokepoint, so the rotating RDS credential is injected here.
        conn = db_credentials.psycopg_connect(
            autocommit=True, connect_timeout=LOCK_CONNECT_TIMEOUT_SECONDS
        )
        try:
            with conn.cursor() as cur:
                cur.execute(_TRY_LOCK_SQL, {"classid": CRON_RUN_LOCK_CLASSID, "name": self.name})
                locked, key = cur.fetchone()
        except Exception:
            conn.close()
            raise
        if not locked:
            conn.close()
            return False
        self._conn, self._key = conn, key
        return True

    def release(self) -> None:
        """Unlock and close. Idempotent and never raises: it runs from a `finally` and a future's
        done-callback, and a raise would lose the close as well as the unlock."""
        with self._guard:
            conn, key = self._conn, self._key
            self._conn, self._key = None, None
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", (CRON_RUN_LOCK_CLASSID, key))
        except Exception as exc:  # noqa: BLE001 — closing the connection frees it regardless
            log.warning("cron: advisory unlock of %s failed (%s); closing the connection", self.name, exc)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _end_lock_holders(conn: psycopg.Connection, name: str) -> bool:
    """End every backend holding run lock `name` (any process, any replica), in one statement so no other
    session can take that pid in between. True iff one was ended. `pg_locks` shows a two-int4 advisory key as
    `classid`/`objid` with `objsubid = 2`; the int4 → oid cast keeps a negative hash's bit pattern."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND objsubid = 2 AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND classid = %(classid)s::int4::oid AND objid = hashtext(%(name)s)::oid",
            {"classid": CRON_RUN_LOCK_CLASSID, "name": name},
        )
        return any(row[0] for row in cur.fetchall())


def release_run_lock(cron_id: UUID | str, *, by: object = None) -> bool:
    """An operator's recovery for a firing that outlived its hard timeout and still holds the run lock:
    end the database session holding it, so the next tick runs. True iff a holder was ended.

    The run history stays: the abandoned firing's timeout `error` row and every skip after it are kept. What it
    does not do is stop the abandoned body's thread: whatever it is still waiting on finishes alongside the next
    firing, the double run the lock otherwise prevents, which is why it is an operator's act and never automatic.
    Every lock wait on a cron body's lane is bounded (`app/db.py::BACKGROUND_LOCK_TIMEOUT_MS`), so that body is
    not parked on the database."""
    name = f"{_RUN_LOCK_PREFIX}{cron_id}"
    conn = db_credentials.psycopg_connect(autocommit=True, connect_timeout=LOCK_CONNECT_TIMEOUT_SECONDS)
    try:
        ended = _end_lock_holders(conn, name)
        if ended:
            log.warning("cron %s: operator %s released the run lock of a firing that had not returned", cron_id, by)
        return ended
    finally:
        conn.close()


# ── The hard timeout ──────────────────────────────────────────────────────────────────────────────────


def _bounded_call(
    fn: Callable[[], Any], timeout: float, label: str, on_abandon: Callable[[Any], None]
) -> tuple[Any, bool]:
    """Run `fn()` under a hard wall-clock bound. Returns `(value, False)`, or `(None, True)` on expiry.

    The mechanism is pinned: `asyncio.wait_for` cannot bound a `concurrent.futures.Future`, and a
    self-bounding coroutine returns while the body runs on, so freeing the lock from it double-runs. Only
    the executor future resolves when the work actually returns.

    `on_abandon` gets that future on every exit where the body is still live (expiry, unusable bound, any
    exception here); freeing the lock on those paths is the double-run the bound stops.
    """
    executor = background_executor(1, label)
    try:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout), False
        except concurrent.futures.TimeoutError:
            on_abandon(future)
            return None, True
        except (OverflowError, ValueError):
            # An inexpressible bound is an unbounded firing; treat as an overrun so the lock follows the live
            # body. `_numeric` already refuses such values; this is the floor.
            log.error("cron: %s got an unusable bound %r — treating this firing as an overrun",
                      label, timeout)
            on_abandon(future)
            return None, True
        except BaseException:
            if not future.done():
                on_abandon(future)
            raise
    finally:
        # Never `with` or `wait=True`: joining the pool would wait out the abandoned firing.
        executor.shutdown(wait=False)


# ── The dropped-fire follow-up ────────────────────────────────────────────────────────────────────────
# A fire arriving while the same cron's pass is mid-flight is recorded `already_running` and
# dropped, so the work it woke would wait a full cadence. The dropped fire sets a flag and the lock
# holder re-runs once on its way out. One bit per cron: N drops earn one follow-up. Process-local, so a
# cross-replica drop gets none (accepted; the interval is the backstop). The follow-up pays the min-gap
# floor like any firing; without a floor it is one back-to-back pass, bounded by the hard timeout.
_followup_wanted: set[str] = set()
_followup_guard = threading.Lock()


def _want_follow_up(cron_id: UUID | str) -> None:
    """Record that a fire for this cron was dropped. One key per cron, cleared by the next pass, so no
    pruning is needed."""
    with _followup_guard:
        _followup_wanted.add(str(cron_id))


def _take_follow_up(cron_id: UUID | str) -> bool:
    """Read-and-clear the flag: True iff a fire was dropped while this pass ran. Both halves under the
    guard, so two firings cannot both claim it."""
    key = str(cron_id)
    with _followup_guard:
        wanted = key in _followup_wanted
        _followup_wanted.discard(key)
    return wanted


def _follow_up_after_release(cron_id: UUID | str) -> bool:
    """Enqueue the one follow-up pass a dropped fire earned. True iff a tick was enqueued.

    Called only once the run lock is released: a trigger issued while holding it would be dropped too.

    Not routed through `fire_now_sync`: its `_coalesce` window is often where the dropped fire came from,
    so it would swallow the follow-up; the flag already coalesces to one per pass. Its other pre-checks
    do not apply either.

    Never raises (runs from a `finally` and a done-callback); a missed enqueue costs one interval.

    Run in a FRESH, EMPTY `contextvars.Context`: both callers run inside the `cron_tick` workflow, whose
    DBOS context is a context variable, and `DBOS.trigger_schedule` refuses any call made with a workflow
    context set ("cannot be called from within a workflow"). dbos 3.2.0 has no in-workflow form, and the
    follow-up is deliberately not a durable step of this tick, so it is issued as from outside one.
    """
    if not _take_follow_up(cron_id):
        return False
    try:
        from dbos import DBOS

        contextvars.Context().run(DBOS.trigger_schedule, schedule_name(cron_id))
    except Exception as exc:  # noqa: BLE001 — loss-tolerant; the interval is the correctness backstop
        log.warning("cron %s: the follow-up for a dropped fire did not enqueue (%s)", cron_id, exc)
        return False
    log.debug("cron %s: enqueued the follow-up pass a dropped fire earned", cron_id)
    return True


def _release_then_follow_up(lock: RunLock, cron_id: UUID) -> None:
    """Free the run lock, then enqueue any follow-up: the single spelling of that order for both exit paths."""
    lock.release()
    _follow_up_after_release(cron_id)


# ── The wrapper ───────────────────────────────────────────────────────────────────────────────────────


def run_cron_tick(
    cron_id: UUID, scheduled_at: datetime, *, session_factory=SessionLocal, cause: str = "schedule"
) -> TickResult:
    """One delivered tick, end to end. Never raises: a firing that blew up is a `cron_runs` row.

    `scheduled_at` is the instant the schedule named, not now; the ledger records both. `cause` is `manual` for an
    automation's Run now (`routes/automations.py`), which the fire engine records on the run. Runs in the
    background lane (`app/db.py`), its bounded body too (`_bounded_call`).
    """
    with background_lane():
        return _run_cron_tick(cron_id, scheduled_at, session_factory=session_factory, cause=cause)


def _run_cron_tick(cron_id: UUID, scheduled_at: datetime, *, session_factory, cause: str) -> TickResult:
    _stamp_heartbeat(cron_id, session_factory)

    lock = RunLock(cron_id)
    try:
        acquired = lock.acquire()
    except Exception as exc:  # noqa: BLE001 — a lock connection we could not open is an errored firing
        log.exception("cron %s: could not take the run lock", cron_id)
        return _record_outside(
            cron_id, scheduled_at, RUN_OUTCOME_ERROR, None,
            f"could not take the run lock: {exc!r}", session_factory,
        )
    if not acquired:
        # Set the flag before recording (its own transaction): the holder may release mid-record, and a
        # later flag would have no consumer.
        _want_follow_up(cron_id)
        log.info("cron %s: a previous firing is still running — skipping this tick", cron_id)
        return _record_outside(
            cron_id, scheduled_at, RUN_OUTCOME_SKIPPED, SKIP_ALREADY_RUNNING,
            "a previous firing of this cron had not returned", session_factory,
        )

    handed_off = False
    abandoned = threading.Event()
    claim = _RunClaim()

    def abandon(future) -> None:
        """Hand the lock to the live body: it is freed when the real work returns, never before.

        The follow-up rides this callback, not the `finally`: triggered from the calling thread it would land
        on the lock the abandoned body still holds and be dropped again.
        """
        nonlocal handed_off
        handed_off = True
        abandoned.set()
        future.add_done_callback(lambda _f: _release_then_follow_up(lock, cron_id))

    try:
        timeout = _effective_timeout(cron_id, session_factory)
        if timeout is None:
            log.info("cron %s: the row is gone — this tick records nothing", cron_id)
            return TickResult(outcome=RUN_OUTCOME_SKIPPED, skip_reason=SKIP_DISABLED, detail="row deleted")
        value, overran = _bounded_call(
            lambda: _invoke(cron_id, scheduled_at, timeout, session_factory, abandoned, cause, claim),
            timeout,
            f"cron-{cron_id}",
            abandon,
        )
        if overran:
            log.warning("cron %s: firing exceeded its %.0fs bound; the lock is held until it returns",
                        cron_id, timeout)
            # `abandoned` is already set (`abandon` ran inside `_bounded_call`), so the body writes no row after this.
            with claim.guard:
                provisional_id = claim.run_id
            return _record_outside(
                cron_id, scheduled_at, RUN_OUTCOME_ERROR, None,
                f"exceeded its {timeout:.0f}s timeout and {ABANDONED_DETAIL_TAIL}", session_factory,
                provisional_id=provisional_id, overran=True,
            )
        return value
    except Exception as exc:  # noqa: BLE001 — `future.result()` re-raises the body's own failure
        # Everything the body does not catch (a dropped connection, an overflow). Nothing may reach the DBOS
        # worker, and a firing without a row would read as healthy.
        log.exception("cron %s: the firing itself raised", cron_id)
        return _record_outside(
            cron_id, scheduled_at, RUN_OUTCOME_ERROR, None,
            f"the firing raised: {exc!r}", session_factory,
        )
    finally:
        # The ordinary exit; a handed-off firing's lock and follow-up belong to `abandon`'s callback.
        if not handed_off:
            _release_then_follow_up(lock, cron_id)


def _stamp_heartbeat(cron_id: UUID, session_factory) -> None:
    """`last_tick_at = now()`, first and in its own bounded transaction.

    The liveness backstop reads only this column (`cron_runs` may be empty by retention), so it is
    stamped before anything can return early. `lock_timeout` keeps it from parking a connection behind
    a settling firing's row lock.
    """
    try:
        with session_factory() as session:
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            session.execute(
                text("UPDATE crons SET last_tick_at = now() WHERE id = :id"), {"id": cron_id}
            )
            session.commit()
    except Exception:  # noqa: BLE001 — a missed heartbeat is a false stall alarm, never a failed firing
        log.exception("cron %s: could not stamp last_tick_at", cron_id)


def _numeric(value: Any) -> Optional[float]:
    """A `Numeric` column as a positive, finite float, or None.

    NaN and ±Infinity are refused here because the column's `> 0` CHECK admits both, and either removes
    the hard bound the run lock relies on. None keeps its distinct meaning (registration value; not
    wakeable), never zero.
    """
    if value is None:
        return None
    try:
        out = float(value) if isinstance(value, Decimal) else float(value)
    except (TypeError, ValueError, OverflowError):
        log.error("cron: bound %r is not a number — falling back to the default", value)
        return None
    if not math.isfinite(out) or out <= 0:
        log.error("cron: bound %r is not a usable positive number — falling back to the default", value)
        return None
    return out


def _effective_timeout(cron_id: UUID, session_factory) -> Optional[float]:
    """This firing's bound: the row's, else the registration's, else the runtime default. None means the
    row is gone: nothing to run or record.

    On the operator plane for `_invoke`'s reason: this is the first tenant-owned read of a delivered tick.
    Its own session, so nothing else inherits the escape."""
    with seam.operator_scope(), session_factory() as session:
        cron = session.get(Cron, cron_id)
        if cron is None:
            return None
        own = _numeric(cron.timeout_seconds)
        if own is not None:
            return own
        # A fire-engine automation runs under its action's bound, whatever activity its child carries.
        from app import automation_actions, automation_fire

        automation = automation_fire.fire_capable(session, cron.automation_id)
        if automation is not None:
            return float(automation_actions.get(automation.action_type).timeout_seconds)
        try:
            descriptor = cron_activities.get(cron.activity_type)
        except cron_activities.UnknownActivityError:
            # The body records the error; it only needs a bound to do it under.
            return DEFAULT_TIMEOUT_SECONDS
    return _numeric(descriptor.timeout_seconds) or DEFAULT_TIMEOUT_SECONDS


def _effective_min_gap(cron: Cron) -> Optional[float]:
    """This cron's floor: the row's, else the registration's. None means not wakeable (the mirror of
    `wake_min_gap > 0`), never "a floor of zero"."""
    own = _numeric(cron.min_gap_seconds)
    if own is not None:
        return own
    try:
        return _numeric(cron_activities.get(cron.activity_type).min_gap_seconds)
    except cron_activities.UnknownActivityError:
        return None


def _invoke(
    cron_id: UUID, scheduled_at: datetime, timeout: float, session_factory, abandoned: threading.Event,
    cause: str = "schedule", claim: Optional[_RunClaim] = None,
) -> TickResult:
    """Steps 4-7, on the executor's thread and in its own session.

    Its own session because the calling thread abandons this one on timeout; a session shared across the
    two threads is the corruption the bound exists to avoid.
    """
    deadline = time.monotonic() + timeout
    # The delivery machinery is fleet-wide: this thread binds nothing (DBOS spawns it), and unbound reads
    # are `false()`, so every cron would report its row gone. `operator_scope()`, not `bind_from`: one
    # engine delivers every org's ticks, and it is write-neutral (`OPERATOR` stamps new rows like unbound;
    # binding the cron's org would change `cron_runs` writes). Cannot raise: `prior` is `None` on this
    # thread. The activity itself leaves this escape (`detached()` in `_run_activity`).
    with seam.operator_scope(), session_factory() as session:
        cron = session.get(Cron, cron_id)
        if cron is None:
            return _gone()

        # Before the hold, so a disabled cron does not pay its floor first.
        if _held_off(cron, cause):
            return _settle(session, cron, _disabled(), scheduled_at, 0.0, abandoned)

        # Before the hold too: a throttled firing does no work, so it waits for nothing.
        throttle = _min_period_throttle(session, cron)
        if throttle is not None and _inside_min_period(cron, throttle):
            return _settle(session, cron, _below_min_period(throttle), scheduled_at, 0.0, abandoned)

        held = _hold_for_min_gap(session, cron, deadline)
        if time.monotonic() >= deadline:
            # The floor consumed the whole bound; the calling thread already declared this firing abandoned.
            log.error("cron %s: its min-gap floor is at or above its %.0fs bound — nothing can run",
                      cron_id, timeout)
            return TickResult(outcome=RUN_OUTCOME_ERROR, detail="the min-gap floor consumed the bound",
                              held_seconds=held)

        # Re-read after the hold and the DBOS hop: params and zone are not in the schedule context, and
        # `enabled` may have changed. The only place a paused-but-triggered firing is refused, unless it is a Run now
        # (`trigger_schedule` ignores status; `_held_off`).
        session.expire_all()
        cron = session.get(Cron, cron_id)
        if cron is None:
            return _gone()
        decision = _decide(
            session, cron, scheduled_at, cron_timezone(), throttled=throttle is not None,
            cause=cause, deadline=deadline, abandoned=abandoned, claim=claim,
        )
        return _settle(session, cron, decision, scheduled_at, held, abandoned)


def _held_off(cron: Cron, cause: str) -> bool:
    """A paused (disabled) cron fires only on an automation's Run now: pausing stops the schedule, not the operator.
    A system function or noop never gets here paused (`routes/crons.py::run_cron_now` refuses it)."""
    from app.automation_fire import CAUSE_MANUAL

    return not cron.enabled and cause != CAUSE_MANUAL


def _gone() -> TickResult:
    return TickResult(outcome=RUN_OUTCOME_SKIPPED, skip_reason=SKIP_DISABLED, detail="row deleted")


def _disabled() -> _Decision:
    return _Decision(RUN_OUTCOME_SKIPPED, SKIP_DISABLED, "the cron is disabled")


def _adopted_parent(session: Session, cron: Cron) -> Optional[UUID]:
    """The cron's parent automation id. Every current writer links one; a tenant cron with none (written by a
    release without automations and never fired since — a disabled or not-yet-due row) is adopted here, in this
    transaction, as `trigger_intake._automation` adopts a trigger — so every Runner cron fires through the engine
    and the Runner activity carries params only (`cron_activity_runner`). A bag its own activity refuses is not adopted: the activity branch records that refusal, as it did before the engine."""
    if cron.automation_id is None and not cron.is_system:
        from app import automation_service

        from app import cron_activity_runner

        try:
            descriptor = cron_activities.get(cron.activity_type)
            cron_activities.validate_params(descriptor, cron.activity_params)
            if cron.activity_type == cron_activity_runner.ACTIVITY_TYPE:
                cron_activity_runner.check_project_keys(cron.activity_params)
        except (cron_activities.UnknownActivityError, cron_activities.ParamsError):
            return None
        if descriptor.is_system:
            return None
        automation_service.sync_cron(session, cron)
    return cron.automation_id


def _decide(
    session: Session, cron: Cron, scheduled_at: datetime, tz: tzinfo, *, throttled: bool = False,
    cause: str = "schedule", deadline: Optional[float] = None, abandoned: Optional[threading.Event] = None,
    claim: Optional[_RunClaim] = None,
) -> _Decision:
    """The refusals, the ledger backfill and the run — in the order a firing meets them.

    `throttled` (the row is faster than its org's minimum period) skips the backfill: the occurrences
    since its last firing were skipped on purpose, not missed.

    A tenant automation whose action has a per-target filer goes to the fire engine (`_run_fire`): its Target
    and params are the parent's (validated at save), and its overlap is per target (D10), so the carrier
    activity's params do not apply. System functions and noop keep their activity's own `invoke`, unchanged."""
    if _held_off(cron, cause):
        return _disabled()
    from app import automation_fire

    automation = automation_fire.fire_capable(session, _adopted_parent(session, cron))
    if automation is not None:
        # Nor for a paused cron's Run now: its anchor is reset only on Resume, so the pause would read as missed runs.
        if not throttled and cron.enabled:
            _backfill_missed_ledger(session, cron, scheduled_at, tz)
        return _run_fire(session, cron, automation.id, scheduled_at, cause=cause, deadline=deadline,
                         abandoned=abandoned, claim=claim)
    try:
        descriptor = cron_activities.get(cron.activity_type)
        params = cron_activities.validate_params(descriptor, cron.activity_params)
    except (cron_activities.UnknownActivityError, cron_activities.ParamsError) as exc:
        return _Decision(RUN_OUTCOME_ERROR, None, str(exc))

    if not throttled:
        _backfill_missed_ledger(session, cron, scheduled_at, tz)
    return _run_activity(session, cron, descriptor, params, scheduled_at)


# How early a throttled row may fire against its minimum period: a tenth of it, at most 5s. Without it a
# row whose schedule lands exactly on the minimum would lose every other firing to the few hundred
# milliseconds between the scheduled instant and `last_fired_at` (stamped at activity start).
_MIN_PERIOD_SLACK_FRACTION = 0.1
_MIN_PERIOD_SLACK_MAX_SECONDS = 5.0


def _min_period_throttle(session: Session, cron: Cron) -> Optional[int]:
    """The org's minimum period when this row's schedule is FASTER than it, else None.

    None for a compliant row, so its early wakes and Run now are never throttled: only a row saved before
    the minimum was raised is (`app/cron_min_period.py`). A schedule that cannot be measured is left
    alone, as on save."""
    period = cron_min_period.resolve(session, cron.org_id)
    try:
        gap = cron_min_period.schedule_gap(cron.schedule_kind, cron.cron_expression, cron.interval_seconds)
    except CronScheduleError:
        return None
    return period.effective if gap < period.effective else None


def _inside_min_period(cron: Cron, period: int) -> bool:
    """Has less than `period` (less its slack) passed since this row last fired? Never true for a row that
    has not fired yet."""
    if cron.last_fired_at is None:
        return False
    slack = min(period * _MIN_PERIOD_SLACK_FRACTION, _MIN_PERIOD_SLACK_MAX_SECONDS)
    elapsed = (datetime.now(timezone.utc) - cron.last_fired_at).total_seconds()
    return elapsed < period - slack


def _below_min_period(period: int) -> _Decision:
    return _Decision(
        RUN_OUTCOME_SKIPPED, SKIP_BELOW_MIN_PERIOD,
        f"this schedule is faster than the organization's {period}s minimum period; it fires at most "
        f"once every {period}s until its schedule is slowed",
    )


def _hold_for_min_gap(session: Session, cron: Cron, deadline: float) -> float:
    """Sleep out the remainder of the min-gap floor. Returns how long it held.

    Held, never dropped (dropping would regress early-fire latency). Measured from `last_fired_at`
    (stamped at activity start) and clamped to the firing's deadline.
    """
    floor = _effective_min_gap(cron)
    if not floor or cron.last_fired_at is None:
        return 0.0
    elapsed = (datetime.now(timezone.utc) - cron.last_fired_at).total_seconds()
    remaining = min(floor - elapsed, max(0.0, deadline - time.monotonic()))
    if remaining <= 0:
        return 0.0
    log.debug("cron %s: inside its %.1fs floor — holding %.1fs", cron.id, floor, remaining)
    # End the transaction for the hold, returning the pooled connection; nothing is written yet.
    session.rollback()
    time.sleep(remaining)
    return remaining


def _backfill_missed_ledger(session: Session, cron: Cron, scheduled_at: datetime, tz: tzinfo) -> int:
    """Record the occurrences the engine never delivered: the ledger only, never execution.

    The window is `(max(last_fired_at, schedule_anchor_at), scheduled_at)`, exclusive. `last_fired_at`
    advances at activity start (DBOS's own advances on enqueue), so a version-stranded tick is visible here.
    """
    if not should_record(cron.retention_mode, RUN_OUTCOME_MISSED):
        return 0
    floor = cron.schedule_anchor_at
    if cron.last_fired_at is not None and cron.last_fired_at > floor:
        floor = cron.last_fired_at
    occurrences: list[datetime] = []
    overflow = 0
    cursor, scanned = floor, 0
    try:
        while scanned < MISSED_SCAN_CAP:
            nxt = next_fire_for_row(
                cron.schedule_kind, cron.cron_expression, cron.interval_seconds, cursor, tz
            )
            if nxt >= scheduled_at:
                break
            scanned += 1
            cursor = nxt
            if len(occurrences) < MISSED_ROW_CAP:
                occurrences.append(nxt)
            else:
                overflow += 1
    except (CronScheduleError, OverflowError, ValueError) as exc:
        log.warning("cron %s: cannot reconstruct missed ticks (%s)", cron.id, exc)
        return 0
    if not occurrences:
        return 0
    for index, when in enumerate(occurrences):
        detail = ""
        if index == len(occurrences) - 1 and overflow:
            more = f"at least {overflow}" if scanned >= MISSED_SCAN_CAP else str(overflow)
            detail = f"and {more} further missed occurrence(s), not recorded"
        session.add(CronRun(
            id=new_uuid7(), cron_id=cron.id, fired_at=when,
            outcome=RUN_OUTCOME_MISSED, detail=detail,
        ))
    log.info("cron %s: recorded %d missed occurrence(s) (%d beyond the cap)",
             cron.id, len(occurrences), overflow)
    return len(occurrences)


def _run_activity(
    session: Session, cron: Cron, descriptor, params: dict, scheduled_at: datetime
) -> _Decision:
    """Commit the anchor and the reconstructed gap, then invoke.

    The commit before the invoke is load-bearing: it releases the `crons` row lock the activity would
    otherwise hold for its whole run (queueing every later heartbeat on a pooled connection), and puts
    the `missed` rows beyond an activity failure's rollback.
    """
    run_id = new_uuid7()
    started_at = datetime.now(timezone.utc)
    cron.last_fired_at = started_at
    session.commit()

    context = cron_activities.ActivityContext(
        cron_id=cron.id, cron_name=cron.name, run_id=run_id, params=params, session=session
    )
    try:
        # Every activity runs here, so this is where reads are attributed (by activity type, not the editable
        # name) and where the plane is chosen. Keyed on `descriptor.is_system`: system activities are fleet-wide
        # (`operator_scope()`); others bind nothing. `detached()` first on both arms because `_invoke` already
        # holds `operator_scope()`, which would otherwise leak into a tenant activity (`TenancyScopeConflict`).
        # `nullcontext` keeps this the one call of `descriptor.invoke`.
        plane = seam.operator_scope() if descriptor.is_system else nullcontext()
        with exclusion_log.caller(f"cron:{cron.activity_type}"), seam.detached(), plane:
            result = descriptor.invoke(scheduled_at, context)
    except Exception as exc:  # noqa: BLE001 — an activity that raised is an `error` row, not a crash
        log.exception("cron %s: activity %r raised", cron.id, cron.activity_type)
        # The failed statement may have poisoned the transaction; the anchor and missed rows are committed.
        session.rollback()
        return _Decision(RUN_OUTCOME_ERROR, None, repr(exc), run_id=run_id, started_at=started_at)
    return _Decision(result.outcome, None, result.detail, run_id=run_id, started_at=started_at,
                     task_id=result.task_id)


def _run_fire(
    session: Session, cron: Cron, automation_id: UUID, scheduled_at: datetime, *, cause: str,
    deadline: Optional[float], abandoned: Optional[threading.Event] = None, claim: Optional[_RunClaim] = None,
) -> _Decision:
    """The fire engine's schedule arm: close a run an earlier crash left open
    (R-8), write this run's row up front — provisional `acted`, so each target's ledger row can name it — commit,
    then fan out in a session bound to the automation's org. `_settle` finishes the row by update, or — when the
    firing outran its bound — the wrapper's timeout does (`_record_outside`)."""
    from app import automation_fire
    from app.models.automations import Automation

    run_id = new_uuid7()
    started_at = datetime.now(timezone.utc)
    with claim.guard if claim is not None else nullcontext():
        if abandoned is not None and abandoned.is_set():
            # The bound expired before the row was written: the wrapper records the timeout itself, and a row
            # written now would be a second one for this firing. `_settle` sees the flag and records nothing.
            return _Decision(RUN_OUTCOME_ERROR, None, "abandoned before it fired")
        automation_fire.close_interrupted_runs(session, cron.id)
        cron.last_fired_at = started_at
        session.add(CronRun(id=run_id, cron_id=cron.id, fired_at=scheduled_at, started_at=started_at,
                            outcome=RUN_OUTCOME_ACTED, detail=""))
        session.commit()
        if claim is not None:
            claim.run_id = run_id
    remaining = (deadline - time.monotonic()) if deadline is not None else None
    try:
        # Out of `_invoke`'s operator escape, into the automation's own org: every row the firing writes lands there.
        with exclusion_log.caller("cron:automation"), seam.detached(), SessionLocal() as fire_session:
            with seam.bind_from(fire_session, Automation, automation_id):
                automation = fire_session.scalar(select(Automation).where(Automation.id == automation_id))
                run = fire_session.scalar(select(CronRun).where(CronRun.id == run_id))
                result = automation_fire.fire(fire_session, automation, cause=cause, cron_run=run,
                                              timeout_seconds=remaining)
    except Exception as exc:  # noqa: BLE001 — a firing that raised past the engine is settled from its ledger
        log.exception("cron %s: the fire engine raised", cron.id)
        session.rollback()
        verdict = automation_fire.ledger_verdict(session, run_id)
        if verdict is None:
            return _Decision(RUN_OUTCOME_ERROR, None, repr(exc), run_id=run_id, started_at=started_at,
                             provisional=True)
        outcome, detail, task_id = verdict
        return _Decision(outcome, None, f"{detail}; then the firing raised: {exc!r}", run_id=run_id,
                         started_at=started_at, task_id=task_id, provisional=True)
    if result.all_previous_open:
        # Every target still busy: the firing is today's whole-firing skip, which the wedge counter counts.
        return _Decision(RUN_OUTCOME_SKIPPED, SKIP_PREVIOUS_TASK_OPEN, result.detail, run_id=run_id,
                         started_at=started_at, provisional=True)
    return _Decision(result.outcome, None, result.detail, run_id=run_id, started_at=started_at,
                     task_id=result.task_ids[0] if result.task_ids else None, provisional=True)


def _settle(
    session: Session,
    cron: Cron,
    decision: _Decision,
    scheduled_at: datetime,
    held: float,
    abandoned: Optional[threading.Event] = None,
    *,
    session_factory=SessionLocal,
    stuck: bool = False,
) -> TickResult:
    """Record the firing per its retention mode, move the skip counter, commit, then deliver any alarm.

    A firing the calling thread already declared abandoned records nothing: that thread wrote its own
    `error` row, and a second row (plus a reset resolving the fresh alarm) would make the alarm untrustworthy.
    """
    if abandoned is not None and abandoned.is_set():
        session.rollback()
        log.warning("cron %s: an abandoned firing returned after its bound; the timeout row stands",
                    cron.id)
        return TickResult(outcome=decision.outcome, skip_reason=decision.skip_reason,
                          detail=decision.detail, recorded=False, held_seconds=held)

    # Captured before `session.commit()` expires the instance; `_deliver_skip_alarm` runs after it.
    cron_id, cron_name, mode = cron.id, cron.name, cron.retention_mode
    wakeable = _effective_min_gap(cron) is not None
    recorded = should_record(mode, decision.outcome, decision.skip_reason)
    provisional = (
        session.scalar(select(CronRun).where(CronRun.id == decision.run_id))
        if decision.provisional and decision.run_id else None
    )
    if provisional is not None:
        if recorded:
            provisional.outcome = decision.outcome
            provisional.skip_reason = decision.skip_reason
            provisional.detail = (decision.detail or "")[:MAX_DETAIL_CHARS]
            provisional.task_id = decision.task_id
            provisional.finished_at = datetime.now(timezone.utc)
        else:
            # The retention mode keeps no row for this outcome; its target rows go with it (CASCADE).
            session.delete(provisional)
    elif recorded:
        session.add(CronRun(
            id=decision.run_id or new_uuid7(), cron_id=cron_id, fired_at=scheduled_at,
            started_at=decision.started_at,
            finished_at=datetime.now(timezone.utc) if decision.started_at is not None else None,
            outcome=decision.outcome, skip_reason=decision.skip_reason,
            detail=(decision.detail or "")[:MAX_DETAIL_CHARS], task_id=decision.task_id,
        ))
    # Stamped on the database's clock whatever the retention mode keeps: the starvation metric reads it
    # (`app/metrics_emf.py`), and a `noop` is a success that `retention_mode = 'acted'` never records.
    if decision.outcome in (RUN_OUTCOME_ACTED, RUN_OUTCOME_NOOP):
        session.execute(text("UPDATE crons SET last_succeeded_at = now() WHERE id = :id"), {"id": cron_id})
    skips, recovered = _move_skip_counter(
        session, cron_id, decision.outcome, decision.skip_reason, wakeable
    )
    session.commit()
    _deliver_skip_alarm(cron_id, cron_name, skips, recovered, session_factory)
    automation_alerts.after_firing(
        session_factory, cron_id, skips=skips, outcome=decision.outcome, skip_reason=decision.skip_reason, stuck=stuck,
    )
    return TickResult(outcome=decision.outcome, skip_reason=decision.skip_reason,
                      detail=decision.detail or "", recorded=recorded, held_seconds=held)


def _move_skip_counter(
    session: Session, cron_id: UUID, outcome: str, skip_reason: Optional[str], wakeable: bool
) -> tuple[Optional[int], bool]:
    """`(new count, recovered)`: the wedge counter, moved with a guarded UPDATE rather than in Python.

    Incremented by outcomes meaning the cron is stuck; reset by a firing that ran. `missed` and the two
    deliberate operator states leave it alone. `already_running` counts only without a min-gap floor: on
    a wakeable cron it is the floor serialising a burst; a real wedge still alarms via its timeout `error`.
    """
    wedged = outcome == RUN_OUTCOME_ERROR or skip_reason == SKIP_PREVIOUS_TASK_OPEN or (
        skip_reason == SKIP_ALREADY_RUNNING and not wakeable
    )
    if wedged:
        count = session.execute(
            text("UPDATE crons SET consecutive_skips = consecutive_skips + 1 WHERE id = :id "
                 "RETURNING consecutive_skips"),
            {"id": cron_id},
        ).scalar()
        return count, False
    if outcome in (RUN_OUTCOME_MISSED, RUN_OUTCOME_SKIPPED):
        return None, False
    cleared = session.execute(
        text("UPDATE crons SET consecutive_skips = 0 WHERE id = :id AND consecutive_skips > 0 "
             "RETURNING id"),
        {"id": cron_id},
    ).scalar()
    return 0, cleared is not None


def _deliver_skip_alarm(
    cron_id: UUID, cron_name: str, skips: Optional[int], recovered: bool, session_factory,
) -> None:
    """Raise the wedged-cron alarm at the threshold, or resolve it on the firing that recovers.

    Only touched at a threshold crossing or a recovery, so a healthy cron pays no round trip.
    """
    if not recovered and (skips is None or skips < SKIP_ESCALATION_THRESHOLD):
        return
    key = cron_alarms.skip_dedup_key(cron_id)
    with session_factory() as session:
        if recovered:
            cron_alarms.resolve_alarm(session, dedup_key=key,
                                      summary=f"cron {cron_name!r} fired successfully again")
            return
        cron_alarms.raise_alarm(
            session,
            dedup_key=key,
            title=f"🚨 Cron {cron_name!r} has skipped {skips} firings in a row",
            body=(
                f"The cron `{cron_name}` (`{cron_id}`) has now recorded {skips} consecutive wedged "
                f"firings — each one was skipped because a previous firing had not returned, because "
                f"the work its last firing created is still open, or because it raised.\n\n"
                f"Open its Runs tab in the console to see which. This row is refreshed on every further "
                f"skip and RESOLVES ITSELF the moment the cron fires successfully again."
            ),
        )


def _record_outside(
    cron_id: UUID, scheduled_at: datetime, outcome: str, skip_reason: Optional[str],
    detail: str, session_factory, *, provisional_id: Optional[UUID] = None, overran: bool = False,
) -> TickResult:
    """`_settle` for the three decisions the calling thread makes: the lock it could not take, the
    firing that outran its bound, and the one that raised. Each needs its own session.

    `provisional_id` (the overrun): the run row a fire-engine firing already wrote up front (`_run_fire`, handed
    over through `_RunClaim`), which the timeout settles rather than adding a second row for the same firing. The
    abandoned body may still commit target rows under it; the row's headline stays the timeout. None (a system
    activity, or the bound expired before the engine wrote one) records a fresh row as before; R-8's next-tick
    close stays the fallback for a crash in which this wrapper never ran.

    `overran`: a fresh row is stamped with the abandoned firing's start (`last_fired_at`, which only a lock holder
    writes, so it is that firing's) and finished at the abandon. `automation_service.stuck_run` compares it with
    `last_fired_at` to tell a holder still live from one some later firing has replaced.

    On the operator plane for `_invoke`'s reason: the DBOS worker thread binds nothing, and an unbound
    read would record nothing. Write-neutral."""
    try:
        with seam.operator_scope(), session_factory() as session:
            cron = session.get(Cron, cron_id)
            if cron is None:
                return TickResult(outcome=outcome, skip_reason=skip_reason, detail=detail)
            decision = _Decision(outcome, skip_reason, detail,
                                 started_at=cron.last_fired_at if overran else None)
            provisional = session.get(CronRun, provisional_id) if provisional_id is not None else None
            if provisional is not None and provisional.finished_at is None:
                decision = _Decision(outcome, skip_reason, detail, run_id=provisional.id,
                                     started_at=provisional.started_at, provisional=True)
            return _settle(
                session, cron, decision, scheduled_at, 0.0, session_factory=session_factory, stuck=overran,
            )
    except Exception:  # noqa: BLE001 — never let bookkeeping raise out of a firing
        log.exception("cron %s: could not record a %s/%s firing", cron_id, outcome, skip_reason)
        return TickResult(outcome=outcome, skip_reason=skip_reason, detail=detail)


# ── fire_now ──────────────────────────────────────────────────────────────────────────────────────────

_fire_seen: dict[str, float] = {}
_fire_guard = threading.Lock()


def _coalesce(cron_id: UUID | str) -> bool:
    """True if this call should proceed. One enqueue per cron per `FIRE_COALESCE_SECONDS`.

    A latency measure only; the run lock and min-gap hold make a doubled fire harmless. Called only after
    the row is shown to exist, so an unknown id never takes a slot.
    """
    key, now = str(cron_id), time.monotonic()
    with _fire_guard:
        for stale in [k for k, seen in _fire_seen.items() if now - seen > FIRE_COALESCE_SECONDS * 10]:
            del _fire_seen[stale]
        last = _fire_seen.get(key)
        if last is not None and now - last < FIRE_COALESCE_SECONDS:
            return False
        _fire_seen[key] = now
    return True


def fire_now_sync(
    cron_id: UUID, *, session_factory=SessionLocal, on_miss: Optional[Callable[[str], None]] = None
) -> bool:
    """Enqueue one out-of-band firing through the same wrapper. True iff a tick was enqueued.

    Never raises: `trigger_schedule` raises on an unknown schedule (the pre-reconcile window, a deleted
    cron), and a miss must cost latency, never an exception. A miss is also handed to `on_miss` as one
    `FIRE_MISS_*` cause, so a caller that speaks to a person can name it.

    The caller is `routes/crons.py::run_cron_now` (Run now, already operator-plane). It checks the
    permanent refusals first off its own row, since this collapses every cause into False.

    On the operator plane for `_invoke`'s reason (one engine delivers every org's ticks); unbound, the
    row reads as deleted. Write-neutral. `bind_from(Cron, cron_id)` is rejected: bare it refuses an
    already-scoped context, and behind `detached()` it is an existence oracle owing an entitlement
    argument this call site cannot supply.
    """
    def miss(cause: str) -> bool:
        if on_miss is not None:
            on_miss(cause)
        return False

    try:
        with seam.detached(), seam.operator_scope(), session_factory() as session:
            cron = session.get(Cron, cron_id)
            if cron is None or not cron.enabled:
                return miss(FIRE_MISS_OTHER)
            if _effective_min_gap(cron) is None:
                log.debug("cron %s: declares no min-gap floor, so it is not wakeable", cron_id)
                return miss(FIRE_MISS_OTHER)
        if not _coalesce(cron_id):
            return miss(FIRE_MISS_COALESCED)
        from dbos import DBOS
    except Exception as exc:  # noqa: BLE001 — loss-tolerant by contract
        log.warning("cron %s: fire_now did not enqueue (%s)", cron_id, exc)
        return miss(FIRE_MISS_OTHER)
    try:
        DBOS.trigger_schedule(schedule_name(cron_id))
    except Exception as exc:  # noqa: BLE001 — loss-tolerant by contract
        log.warning("cron %s: fire_now did not enqueue (%s)", cron_id, exc)
        # The library's own words for an unknown schedule (`dbos._scheduler.trigger_schedule`); it has no
        # narrower type to catch. Only this call is read so, so a database error never reads as one.
        return miss(FIRE_MISS_UNSCHEDULED if "does not exist" in str(exc) else FIRE_MISS_OTHER)
    log.debug("cron %s: fire_now enqueued an out-of-band tick", cron_id)
    return True


def system_cron_next_fire(activity_type: str, *, session_factory=SessionLocal) -> Optional[datetime]:
    """When the system cron of `activity_type` fires next; None while it is disabled, absent or unschedulable.

    For a tenant's read of a fleet-wide schedule (Compliance settings' "Next run"): the row lives at org zero, so
    a tenant-bound read of it finds nothing and every org reads "not scheduled". Fleet plane, on its
    own session, for `fire_now_activity_sync`'s reason: `detached()` then `operator_scope()`, since the caller has
    decided an org. It reads one row and writes nothing; `Cron.is_system.is_(True)` is the tenancy predicate.
    """
    with seam.detached(), seam.operator_scope(), session_factory() as session:
        cron = session.scalars(
            select(Cron).where(Cron.activity_type == activity_type, Cron.is_system.is_(True), Cron.enabled.is_(True))
        ).first()
        if cron is None:
            return None
        try:
            return next_fire_for_row(cron.schedule_kind, cron.cron_expression, cron.interval_seconds,
                                     datetime.now(timezone.utc), cron_timezone())
        except CronScheduleError:
            return None


def fire_now_activity_sync(activity_type: str, *, session_factory=SessionLocal) -> bool:
    """`fire_now_sync` for the system cron of `activity_type`. True iff a tick was enqueued.

    Keyed on the activity type, never the editable `name` (a rename would silently stop the wake);
    `uq_crons_system_activity_type` makes the lookup unambiguous. Never raises, for `fire_now_sync`'s
    reason: every cadence keeps its interval as the backstop.

    On the fleet plane: there is no row id to derive an org from. `bound_to(ORG_ZERO_ID)` is refused
    because it would tie the wake to where a migration filed the row. `detached()` then
    `operator_scope()`, never the escape alone: callers (a node-plane request, the `to_thread` context
    copy) may have decided an org, and `operator_scope()` refuses to widen one. Unbound, the select
    returns no row and every early wake is silently dropped (single home of that finding).

    `Cron.is_system.is_(True)` is the only tenancy predicate left, so it has its own negative arm in
    `tests/test_tenancy_non_request_planes.py`.
    """
    # Coalesced before the DB is touched: this runs on every committed task transition. An unknown
    # activity taking a slot is harmless; the only callers are `app/task_wake.py`'s two constants.
    if not _coalesce(f"activity:{activity_type}"):
        return False
    try:
        with seam.detached(), seam.operator_scope(), session_factory() as session:
            # One read, with `enabled` pushed into it.
            cron = session.scalars(
                select(Cron).where(
                    Cron.activity_type == activity_type,
                    Cron.is_system.is_(True),
                    Cron.enabled.is_(True),
                )
            ).first()
            if cron is None:
                # Disabled and absent are indistinguishable here; disabled is named first as the likelier state.
                log.debug(
                    "cron: no system cron for %r is enabled (disabled, or no row at all) — nothing to "
                    "fire", activity_type,
                )
                return False
            if _effective_min_gap(cron) is None:
                log.debug("cron %s: declares no min-gap floor, so it is not wakeable", cron.id)
                return False
            cron_id = cron.id
        from dbos import DBOS

        DBOS.trigger_schedule(schedule_name(cron_id))
    except Exception as exc:  # noqa: BLE001 — loss-tolerant by contract
        log.warning("cron: fire_now_activity(%r) did not enqueue (%s)", activity_type, exc)
        return False
    log.debug("cron %s: fire_now_activity enqueued an out-of-band tick", cron_id)
    return True


async def fire_now_activity(activity_type: str, *, session_factory=SessionLocal) -> bool:
    """`fire_now_activity_sync` off the serving loop; the form `app/task_wake.py` calls."""
    try:
        return await asyncio.to_thread(
            fire_now_activity_sync, activity_type, session_factory=session_factory
        )
    except Exception as exc:  # noqa: BLE001 — the sync form already swallows; this is the loop's belt
        log.warning("cron: fire_now_activity(%r) failed off-loop (%s)", activity_type, exc)
        return False


def wake_after_commit(session: Session, activity_type: str) -> None:
    """Fire the system cron of `activity_type` once `session` commits. Loss-tolerant: its interval is the
    backstop — a wake lost to a commit landing before DBOS launches, or to a SIGTERM drain killing the
    daemon thread, costs latency only."""
    from app import after_commit

    after_commit.on_commit(session, lambda: _start_wake(activity_type), key=activity_type)


def _start_wake(activity_type: str) -> None:
    # Off the committing thread: the wake reads the cron row and asks DBOS, and the request must not wait on
    # either. Through `fire_now_activity`, the one seam between "committed" and "a cron fires" (the suite stubs it).
    threading.Thread(target=_wake, args=(activity_type,), name=f"{activity_type}-wake", daemon=True).start()


def _wake(activity_type: str) -> None:
    async def _in_background_lane() -> None:
        # This loop's `to_thread` worker is the wake's DB reader: put it in the background lane (`app/db.py`).
        asyncio.get_running_loop().set_default_executor(background_executor(1, f"{activity_type}-wake"))
        await fire_now_activity(activity_type)

    asyncio.run(_in_background_lane())
