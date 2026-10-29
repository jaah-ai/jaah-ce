"""The reconciler — `crons` projected onto DBOS's `workflow_schedules`, a derived tick source.

`crons` is the sole source of truth and the only thing the UI reads; nothing joins `durable_dbos_sys`.

One pass, run by the `cron-projection` queue (`app/durable/cron_projection.py`, which every write asks): list
what DBOS holds, `apply_schedules` the desired set, pause/resume per `enabled`,
then delete `cron:*` schedules with no row. The list comes first because resume must know what was
paused: resuming re-anchors the cron (AC3.1: no replay of what it missed while off).

`apply_schedules` is all-or-nothing and raises, so one bad row would degrade every cron.
"""

# Three defences: (a) only project activity types this process registered, (b) validate expression
# and zone first, (c) wrap the call — log, raise one deduped alarm, and finish the pass rather than fail it.
# A skip under (a) of an ENABLED row is reported as `unregistered_enabled`, and the queue asks a delayed
# follow-up pass for it (`app/durable/cron_projection.py`): a replica that registered the type converges it.

# Passes never overlap (the queue's global concurrency is 1); a status set racing an edit is fixed by the
# pass that edit asked for.
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update

from app import cron_activities, cron_alarms
from app.cron_runtime import (
    SCHEDULE_NAME_PREFIX,
    resolved_timezone_name,
    schedule_name,
)
from app.cron_schedule import (
    CronScheduleError,
    cron_expression_for_interval,
    validate_cron_expression,
)
from app.crons import SCHEDULE_KIND_CRON
from app.db import SessionLocal
from app.models.automations import Cron

log = logging.getLogger("cockpit-cron")

_ACTIVE = "ACTIVE"
_PAUSED = "PAUSED"


def launched_engine() -> Any:
    """The launched DBOS runtime itself, or None — the single home for both halves of that question.

    Returns the object so a caller needing it (`app/cron_retention.py`) does not do a second,
    non-atomic lookup that could hand back a since-destroyed instance. `app/durable/runtime.py`'s handle
    is a different fact (constructed, maybe unlaunched) and must not be paired with this.
    """
    try:
        from dbos._dbos import _dbos_global_instance as engine
    except Exception:  # noqa: BLE001 — no dbos in this process at all
        return None
    return engine if engine is not None and getattr(engine, "_launched", False) else None


@dataclass
class ReconcileResult:
    """What one pass did — returned so the caller can report it and tests can assert convergence."""

    applied: int = 0
    paused: int = 0
    resumed: int = 0
    deleted: int = 0
    unregistered: list[str] = field(default_factory=list)
    #: How many of `unregistered` are enabled — the rows a later pass on a newer replica would schedule.
    unregistered_enabled: int = 0
    invalid: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class _Desired:
    """One cron's projection: the schedule DBOS should hold for it, already validated."""

    cron_id: Any
    name: str
    expression: str
    tz_name: Optional[str]
    enabled: bool
    # Set for an interval row: `cron_tick.tick_is_due` drops the registered step's off-grid ticks with it.
    interval_seconds: Optional[int] = None


def _project(session, tz_name: str) -> tuple[list[_Desired], list[str], int, list[str], set[str]]:
    """`(desired, unregistered, unregistered_enabled, invalid, known)` for every cron row — `known` is every
    row's schedule name, projected or not.

    Defences (a) and (b): rows naming an unregistered activity (rolling deploy) or an unparseable
    expression/zone are skipped and logged, never reaching the all-or-nothing apply.
    """
    desired: list[_Desired] = []
    unregistered: list[str] = []
    unregistered_enabled = 0
    invalid: list[str] = []
    known: set[str] = set()
    for cron in session.scalars(select(Cron)).all():
        # Before the skips: an unprojectable row still exists, so its schedule is no orphan (else one
        # under-registered replica would stop a cron every other replica runs).
        known.add(schedule_name(cron.id))
        try:
            cron_activities.get(cron.activity_type)
        except cron_activities.UnknownActivityError:
            unregistered.append(f"{cron.name} ({cron.activity_type})")
            unregistered_enabled += bool(cron.enabled)
            continue
        try:
            if cron.schedule_kind == SCHEDULE_KIND_CRON:
                expression = validate_cron_expression(cron.cron_expression or "")
                # A `cron` row is local time; an interval is always UTC (a DST fall-back visits the
                # repeated local hour once, `app/cron_schedule.py`).
                zone: Optional[str] = tz_name
            else:
                expression = cron_expression_for_interval(cron.interval_seconds)
                zone = None
        except CronScheduleError as exc:
            invalid.append(f"{cron.name}: {exc}")
            continue
        desired.append(_Desired(
            cron_id=cron.id, name=schedule_name(cron.id), expression=expression,
            tz_name=zone, enabled=cron.enabled,
            interval_seconds=cron.interval_seconds if zone is None else None,
        ))
    return desired, unregistered, unregistered_enabled, invalid, known


def _entries(desired: list[_Desired]) -> list[dict]:
    """The `apply_schedules` input. `automatic_backfill=False` explicitly on every entry: True would
    turn downtime into thousands of real ticks."""
    from app.durable.cron_tick import cron_tick

    return [
        {
            "schedule_name": item.name,
            "workflow_fn": cron_tick,
            "schedule": item.expression,
            "context": {"cron_id": str(item.cron_id)}
            | ({"interval_seconds": item.interval_seconds} if item.interval_seconds else {}),
            "automatic_backfill": False,
            "cron_timezone": item.tz_name,
        }
        for item in desired
    ]


def reconcile_cron_schedules(*, session_factory=SessionLocal) -> ReconcileResult:
    """Bring DBOS's schedules into line with `crons`. Never raises — see the module docstring.

    Run only as the `cron-projection` queue's step (`app/durable/cron_projection.py`), which is what asks
    for it and keeps two passes from overlapping.
    """
    result = ReconcileResult()
    try:
        tz_name = resolved_timezone_name()
        with session_factory() as session:
            desired, result.unregistered, result.unregistered_enabled, result.invalid, known = _project(
                session, tz_name)
        before = _existing()
        _apply(desired, result)
        _set_statuses(desired, before, result, session_factory)
        _delete_orphans(known, before, result)
    except Exception as exc:  # noqa: BLE001 — defence (c): a divergent projection must not fail the pass
        log.exception("cron: schedule reconciliation failed")
        result.error = repr(exc)
    _report(result, session_factory)
    return result


def _existing() -> dict[str, str]:
    """`{schedule_name: status}` for every `cron:*` schedule this application owns.

    Filtered on the `application_name` `dynamic_scheduler_loop` uses; other names are never threaded.
    """
    from dbos import DBOS
    from dbos._utils import GlobalParams

    schedules = DBOS.list_schedules(schedule_name_prefix=SCHEDULE_NAME_PREFIX, application_name=GlobalParams.app_name)
    return {s["schedule_name"]: s.get("status") or _ACTIVE for s in schedules}


def _apply(desired: list[_Desired], result: ReconcileResult) -> None:
    """The all-or-nothing upsert. A no-op on an empty desired set, sparing DBOS a call every tick."""
    if not desired:
        return
    from dbos import DBOS

    DBOS.apply_schedules(_entries(desired))
    result.applied = len(desired)


def _set_statuses(
    desired: list[_Desired], before: dict[str, str], result: ReconcileResult, session_factory
) -> None:
    """`pause`/`resume` per `enabled`.

    A resume re-anchors the cron (AC3.1): `schedule_anchor_at` is what missed-tick reconstruction and
    liveness measure from, so a stale one would invent `missed` rows and a false stall.
    """
    from dbos import DBOS

    for item in desired:
        was = before.get(item.name)
        if item.enabled and was == _PAUSED:
            # Re-anchor committed before the resume (the schedule is live on return), per item so a
            # later raise cannot strand a resumed cron on a stale anchor.
            _re_anchor(session_factory, item.cron_id)
            DBOS.resume_schedule(item.name)
            result.resumed += 1
        elif not item.enabled and was != _PAUSED:
            DBOS.pause_schedule(item.name)
            result.paused += 1


def _delete_orphans(known: set[str], before: dict[str, str], result: ReconcileResult) -> None:
    """Drop every `cron:*` schedule with no row behind it. Namespaced by prefix, and keyed off `known`
    (every row), so a skip is never a deletion."""
    from dbos import DBOS

    if not known and before:
        # An empty read is not "there are no crons"; sweeping on it turns a blip into a total outage.
        log.warning("cron: no cron rows read while DBOS holds %d schedule(s) — not sweeping this pass",
                    len(before))
        return
    for name in before:
        if name in known:
            continue
        DBOS.delete_schedule(name)
        result.deleted += 1
        log.info("cron: deleted orphaned schedule %s", name)


def _re_anchor(session_factory, cron_id) -> None:
    """Stamp `schedule_anchor_at = now()` for one cron and commit (before the resume; see
    `_set_statuses`)."""
    with session_factory() as session:
        cron = session.get(Cron, cron_id)
        if cron is None:
            return
        cron.schedule_anchor_at = datetime.now(timezone.utc)
        session.commit()


def _report(result: ReconcileResult, session_factory) -> None:
    """Raise the deduped `cron-reconcile-failed` alarm, or resolve it once a pass gets through.

    Skipped rows are not a failure: they are logged and keep their existing schedule (an enabled unregistered
    one gets a follow-up pass, `app/durable/cron_projection.py`).
    """
    if result.unregistered:
        log.warning("cron: %d row(s) name an activity this process has not registered: %s",
                    len(result.unregistered), ", ".join(result.unregistered))
    if result.invalid:
        log.warning("cron: %d row(s) carry an unusable schedule: %s",
                    len(result.invalid), ", ".join(result.invalid))
    try:
        with session_factory() as session:
            if result.ok:
                cron_alarms.resolve_alarm(
                    session, dedup_key=cron_alarms.RECONCILE_FAILED_KEY,
                    summary="a reconcile pass completed",
                )
                return
            cron_alarms.raise_alarm(
                session,
                dedup_key=cron_alarms.RECONCILE_FAILED_KEY,
                title="🚨 The cron reconciler could not project the schedule set",
                body=(
                    "`app/cron_reconcile.py` could not bring DBOS's `workflow_schedules` into line with "
                    "the `crons` table, so the schedules the engine is running may not match what the "
                    "console shows.\n\n"
                    f"Last failure: `{result.error}`\n\n"
                    "Nothing retries on a timer: the next pass (a cron write, another ask or a "
                    "restart) RESOLVES this row the moment it gets through. A pass that keeps failing "
                    "usually means one row the projection cannot express — check the control-plane log "
                    "for the `cron:` warnings just before this."
                ),
            )
    except Exception:  # noqa: BLE001 — alarm delivery is best-effort and never fails a pass
        log.exception("cron: could not deliver the reconcile-failed alarm")


# ── The timezone hook: re-phase, then re-project ──────────────────────────────────────────────────────


def rephase_anchors(session) -> int:
    """Stamp `schedule_anchor_at = now()` on every `cron`-kind row and commit. Returns the row count.

    The anchor is the floor `cron_runtime._backfill_missed_ledger` reconstructs from, so a zone change
    without it invents `missed` rows. Interval rows are excluded: they are UTC regardless of the zone.

    One statement, atomic against a concurrent tick. No provenance column moves: `schedule_anchor_at`
    is outside the `updated_at` trigger's columns (`app/crons.py::CRON_MACHINERY_COLUMNS`), so rows are not
    restamped as edited by a human.
    """
    updated = session.execute(
        update(Cron)
        .where(Cron.schedule_kind == SCHEDULE_KIND_CRON)
        .values(schedule_anchor_at=datetime.now(timezone.utc))
    ).rowcount
    session.commit()
    return updated


def apply_timezone_change(session) -> bool:
    """The settings PUT's hook, once the global cron timezone has actually changed. True when a pass was asked.

    Anchors commit before DBOS is told, so no re-aimed schedule fires against stale phasing.

    Never raises: the zone is already committed, and a 500 would make the retry see no change and skip
    the hook for good. A failed re-phase is not retried and asks for no pass; schedules converge on the next
    ask (a cron write or a restart), anchors do not.
    """
    from app.durable.cron_projection import request_projection

    try:
        rephased = rephase_anchors(session)
    except Exception:  # noqa: BLE001 — see the docstring: the zone is already saved
        log.exception(
            "cron: the timezone changed but the anchors could not be re-phased — the next tick may "
            "reconstruct the shift as missed occurrences"
        )
        session.rollback()
        return False
    asked = request_projection()
    log.info("cron: timezone changed to %s — re-phased %d cron(s), projection %s",
             resolved_timezone_name(), rephased, "requested" if asked else "not requested")
    return asked


if __name__ == "__main__":
    # `./dc` after a direct `crons` write: ask the serving api for a pass. This process has no engine.
    from app.durable.cron_projection import request_projection_in_transaction

    with SessionLocal() as _session:
        request_projection_in_transaction(_session)
        _session.commit()
