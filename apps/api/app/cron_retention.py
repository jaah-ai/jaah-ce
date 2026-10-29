"""The retention sweep — one daily activity bounding the tables this control plane grows without limit
(`workflow_status`, `cron_runs`, `activity_events` in part, `tenancy_exclusions`, expired
`console_api_key_leases`, `trigger_deliveries`, `trigger_dedup`, `ops_snapshots` and the Live Cast viewer's
`live_viewer_events` and `live_viewer_minutes`), and the single home
for the DBOS private shapes it depends on.

DBOS's collector runs on no schedule, and `crons.retention_mode` governs writes, not lifetime (no bound
reads it, so a mode change is not retroactive). `activity_events` is never a blanket prune (note above
`_DELETE_ACTIVITY_EVENTS_BY_AGE_SQL`); `tenancy_exclusions` rides its knob (note above
`_DELETE_TENANCY_EXCLUSIONS_BY_AGE_SQL`). The DBOS half also deletes unclaimed rows (mirroring
`SystemDatabase._name_filter`), so no second DBOS application may share this schema.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence, TypeVar
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app import edition_features
from app.operator_settings import reads as settings_reads
from app.activity_kinds import SECURITY_EVENT_KINDS
from app.cron_activities import (
    ActivityContext,
    ActivityDescriptor,
    ActivityResult,
    DefaultSchedule,
    register,
)
from app.crons import SCHEDULE_KIND_CRON

if TYPE_CHECKING:
    from app.org_retention import OrgsSwept

log = logging.getLogger("cockpit-cron")

T = TypeVar("T")


# ── The private DBOS shapes, and nothing else ─────────────────────────────────────────────────────────
#
# Four, each in one named function with a canary: the collector and its schema for this sweep, and the
# serializer pair `app/durable/accounts.py` needs to decode `workflow_events`. One home, so a `dbos` bump
# re-points one place.

# Verified against this version (pin: `apps/api/requirements.txt`); only a `dbos` bump can move the
# symbols below.
DBOS_VERSION_VERIFIED = "3.2.0"

# DBOS 2.x's default batch (3.x defaults to 50,000), pinned so a library change cannot turn one sweep
# into one huge transaction.
GC_BATCH_SIZE = 10000

# Per-firing ceiling applied independently by the two `cron_runs` statements and the `activity_events`
# one; `tenancy_exclusions` has its own (`EXCLUSIONS_ROWS_PER_SWEEP`). Worst case per firing: four
# transactions, 3 x 50,000 + 5,000,000 rows, within the 1800s timeout. An unbounded first pass over a
# backlog would time out and roll back, pruning nothing.

# A year of backlog past a 90-day cutoff (~3.5M rows) drains in ~70 nights; the steady state converges
# overnight.

# On the cap statement it is spent worst-offender-first across all crons, so convergence is per table,
# not per cron. The by-age bounds always take the oldest rows first.
MAX_ROWS_PER_SWEEP = 50_000

# The shared ceiling would sit below this table's insert rate (~58-60k rows/day on one org; see
# `app/models/orgs.py::TenancyExclusion`) and never converge. The rate multiplies with active orgs, so this
# is set two orders of magnitude above today's measurement; a few million is still a bounded delete.
# Re-derive, do not nudge: buckets/minute x 1440 x days of arrears one firing should clear.
EXCLUSIONS_ROWS_PER_SWEEP = 5_000_000

# Where DBOS's history lives, for the before/after count. Library-owned names, kept by the version pin;
# the count fails soft.
DBOS_STATUS_TABLE = "workflow_status"
DBOS_APP_NAME_COLUMN = "application_name"

# The schema is read off the launched engine (`dbos_system_schema`), never off
# `app/durable/runtime.py::SYSTEM_SCHEMA`: the engine is what the collector actually writes, so the
# count cannot measure a table nobody is pruning.
_BARE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


class DbosGarbageCollectUnavailable(RuntimeError):
    """A DBOS private shape resolved in this module could not be resolved — the collector, the engine's
    schema name, or either half of the value-decoding pair.

    Loud on purpose: the sweep lets it out, the wrapper records an `error` row and the skip counter
    escalates; swallowing it would be a green sweep while the disk fills.

    One type for all four resolvers (the name is narrower than the meaning) so no caller catches one
    moved symbol and misses another. Callers decide disposition: `count_dbos_history` and the durable
    login's clear fail soft; the canaries in `tests/test_cron_retention.py` are the loud half.
    """


def resolve_garbage_collect() -> Callable[..., None]:
    """DBOS's own workflow-history collector, or `DbosGarbageCollectUnavailable`.

    The single import site of `dbos._workflow_commands`; there is no public collector API. Same
    discipline as `app/cron_schedule.py`'s vendored cron parser.
    """
    try:
        from dbos._workflow_commands import garbage_collect
    except Exception as exc:  # noqa: BLE001 — ImportError today, anything the module raises tomorrow
        raise DbosGarbageCollectUnavailable(
            "dbos._workflow_commands.garbage_collect could not be imported "
            f"(verified against dbos=={DBOS_VERSION_VERIFIED}; see apps/api/requirements.txt). "
            "DBOS has no public collector, so nothing is pruning durable_dbos_sys — find where the "
            f"symbol moved to in the installed dbos and re-point this one call site: {exc!r}"
        ) from exc
    return garbage_collect


def dbos_system_schema(dbos: Any) -> str:
    """The schema the LAUNCHED engine's system database actually writes, or
    `DbosGarbageCollectUnavailable`.

    `dbos.DBOS` exposes no accessor for it. `_sys_db` raises before launch, so callers resolve the
    engine through `_live_dbos` first. Canaried with the collector.
    """
    try:
        schema = dbos._sys_db.schema
    except Exception as exc:  # noqa: BLE001 — DBOSException today, an AttributeError on a rename
        raise DbosGarbageCollectUnavailable(
            "the launched DBOS engine would not name its own system schema via `_sys_db.schema` "
            f"(verified against dbos=={DBOS_VERSION_VERIFIED}; see apps/api/requirements.txt) — the "
            "sweep cannot report what it collected until this one attribute is re-pointed: "
            f"{exc!r}"
        ) from exc
    if not isinstance(schema, str) or not _BARE_IDENTIFIER.fullmatch(schema):
        # `fullmatch`, not `match`: `$` also matches before a trailing newline. Injection is impossible either
        # way (`"` is outside the class); this only rejects identifiers Postgres cannot resolve.
        raise DbosGarbageCollectUnavailable(
            f"the launched DBOS engine names its system schema {schema!r}, which is not a bare "
            "identifier this module is willing to interpolate into a count query"
        )
    return schema


def dbos_system_serializer(dbos: Any) -> Any:
    """The serializer the LAUNCHED engine encodes its system-database values WITH, or
    `DbosGarbageCollectUnavailable`.

    For `app/durable/accounts.py::_stored_auth_projection`, which reads `workflow_events` with a plain
    SELECT (a recorded `DBOS.get_event` would re-persist the device code it clears); the raw `value` is
    this serializer's output, not JSON.

    Must not degrade to "no row": a swallowed move would leave a live one-time authorization factor in
    place. The `deserialize` check catches a half-finished rename. Callers resolve the engine through
    `cron_reconcile.launched_engine` first.
    """
    try:
        serializer = dbos._sys_db.serializer
    except Exception as exc:  # noqa: BLE001 — DBOSException today, an AttributeError on a rename
        raise DbosGarbageCollectUnavailable(
            "the launched DBOS engine would not name its own value serializer via `_sys_db.serializer` "
            f"(verified against dbos=={DBOS_VERSION_VERIFIED}; see apps/api/requirements.txt) — the "
            "durable login's terminal clear cannot decode the projection it reads back, so a device "
            f"code stays in workflow_events until this one attribute is re-pointed: {exc!r}"
        ) from exc
    if not callable(getattr(serializer, "deserialize", None)):
        raise DbosGarbageCollectUnavailable(
            "the launched DBOS engine's `_sys_db.serializer` no longer answers `deserialize` "
            f"(verified against dbos=={DBOS_VERSION_VERIFIED}; see apps/api/requirements.txt) — the "
            "attribute resolved but is not the serializer protocol this codebase decodes with"
        )
    return serializer


def resolve_deserialize_value() -> Callable[..., Any]:
    """DBOS's own value decoder, or `DbosGarbageCollectUnavailable`.

    One of two import sites of `dbos._serialization` (the other: `durable/runtime.py::migrate_system_schema`,
    which needs only its default serializer); caller as in `dbos_system_serializer`. Not
    re-implemented: the `value`/`serialization` pairing spans formats and a copy would drift silently.
    The positional order `(serialized_value, serialization, serializer)` is part of the contract.
    """
    try:
        from dbos._serialization import deserialize_value
    except Exception as exc:  # noqa: BLE001 — ImportError today, anything the module raises tomorrow
        raise DbosGarbageCollectUnavailable(
            "dbos._serialization.deserialize_value could not be imported "
            f"(verified against dbos=={DBOS_VERSION_VERIFIED}; see apps/api/requirements.txt). "
            "A `workflow_events` value is serialized by the library and cannot be decoded without it, "
            "so the durable login's terminal clear cannot read the projection it is there to clear and "
            "a device code stays in that table — find where the symbol moved to and re-point this "
            f"one call site: {exc!r}"
        ) from exc
    return deserialize_value


# ── The policy bounds: what no operator setting can exceed ──────────────────────────────────────────
#
# The single home for each data class's retention MAXIMUM, per PRD D10; the classification policy points
# here rather than restating the numbers. Every age bound this sweep applies is clamped to its class's
# maximum, so an operator setting (a knob, a row's params) can only TIGHTEN retention, never extend it.
# Classes this sweep does not prune (transcripts and vendor session files: their bucket's S3 lifecycle
# rule) are bounded where they live, not here.
#
# Operational (`cron_runs`, DBOS workflow history): 365 days, D10's twelve months for application logs —
# both are the control plane's own run record. Security events (`activity_events`, `tenancy_exclusions`):
# 400 days hot, well inside the archive's 6 years (`SECURITY_ARCHIVE_RETENTION_DAYS` below); the hot copy
# never outlives the archive.
#
# Transcripts (and vendor session files): 180 days. The BYTES are deleted by the bucket's lifecycle rule,
# which mirrors this number as a literal (terraform cannot import it); this sweep retires the ROW's
# `uploaded` status at the same age so the page never offers a download that is gone.
#
# The catalogue (`app/operator_settings/catalog.py::CATALOG`, DB-free) mirrors the two knob bounds as its
# `maximum=`; a test pins them equal. The clamp stays the backstop for an override saved before that.
RETENTION_CLASS_OPERATIONAL = "operational"
RETENTION_CLASS_SECURITY_EVENT = "security_event"
RETENTION_CLASS_TRANSCRIPT = "transcript"
POLICY_MAX_RETENTION_DAYS: dict[str, int] = {
    RETENTION_CLASS_OPERATIONAL: 365,
    RETENTION_CLASS_SECURITY_EVENT: 400,
    RETENTION_CLASS_TRANSCRIPT: 180,
}

# The FLOOR for the security-event kinds alone (D10: "security events ≥ 12 months hot in DB"): the sweep
# never deletes one younger than this, whatever the activity-log knob says. The knob's 400-day clamp above
# stays the ceiling, so a security row lives between 365 and 400 days hot; the archive keeps it 6 years.
SECURITY_EVENT_MIN_RETENTION_DAYS = 365

# How long the security archive (`app/security_archive.py`) keeps an exported security event
# under Object Lock: 6 years, HIPAA §164.316(b)(2). The bucket's lock mirrors this as a literal (terraform
# cannot import it); an infrastructure test pins the two equal. Nothing here sweeps it.
SECURITY_ARCHIVE_RETENTION_DAYS = 2190


def clamp_to_policy(data_class: str, days: Any) -> Any:
    """`days`, or the class's policy maximum when `days` exceeds it. A tighter value passes unchanged."""
    bound = POLICY_MAX_RETENTION_DAYS[data_class]
    if days > bound:
        log.warning("cron retention: %s retention of %s days exceeds the policy bound; clamped to %d",
                    data_class, days, bound)
        return bound
    return days


# ── The two `cron_runs` bounds, as operator knobs ─────────────────────────────────────────────────────

# Mirrored as literals in `app/operator_settings/catalog.py::CATALOG`, which is DB-free and cannot import this
# module; a test asserts the spellings match.
CRON_RUNS_RETENTION_DAYS_KEY = "cron_runs_retention_days"
CRON_RUNS_PER_CRON_CAP_KEY = "cron_runs_per_cron_cap"


def cron_runs_retention_days() -> int:
    """How many days of `cron_runs` history to keep, clamped to the operational policy bound. Re-read per
    sweep, so an edit applies next firing."""
    return clamp_to_policy(
        RETENTION_CLASS_OPERATIONAL, settings_reads.get_int(CRON_RUNS_RETENTION_DAYS_KEY)
    )


# ── The `activity_events` bound, as the third operator knob ───────────────────────────────────────────

# Mirrored in `app/operator_settings/catalog.py::CATALOG` and guarded like the two keys above.
ACTIVITY_EVENTS_RETENTION_DAYS_KEY = "activity_events_retention_days"

# Not read at runtime: `activity_events_retention_days()` falls back to the CATALOG entry's `default=`;
# a test pins the two equal. This is where the number is explained.
#
# A deliberate, generous guess: an audit trail must answer "who changed this, last quarter", and an
# oversized table is cheaper to fix than lost history. Re-tune on the settings page once the live
# table has been measured under capture.
DEFAULT_ACTIVITY_EVENTS_RETENTION_DAYS = 365


def activity_events_retention_days() -> int:
    """How many days of the activity log (and the exclusion log) to keep, clamped to the security-event
    policy bound. Re-read per sweep, so an edit applies next firing."""
    return clamp_to_policy(
        RETENTION_CLASS_SECURITY_EVENT, settings_reads.get_int(ACTIVITY_EVENTS_RETENTION_DAYS_KEY)
    )


# ── The DBOS half's two parameters ────────────────────────────────────────────────────────────────────

# Params, not knobs: they set how hard this cron leans on DBOS's history, so they live on the row, and
# an operator can run a second, sharper sweep without touching the fleet-wide setting.
DEFAULT_DBOS_RETENTION_DAYS = 14.0
DEFAULT_DBOS_ROWS_THRESHOLD = 200_000

# ── Why both floors are where they are: the row is the exactly-once barrier ─────────────────────────
# `dbos/_scheduler.py` dedups a scheduled firing only by the existence of its `sched-<schedule>-<instant>`
# row, so a cutoff that takes a row before every replica reached that instant fires it twice.

# The window is bounded: schedule threads only look forward, and `cron_reconcile._entries` passes
# `automatic_backfill=False`, so replica spread is the loop jitter, at most ten seconds.

# The row floor is sharper: DBOS converts it to a cutoff and takes whichever deletes more. 10,000 rows
# reaches inside ten seconds only past ~86M rows/day. The age floor, 0.01 days, is 14.4 minutes.
MIN_DBOS_RETENTION_DAYS = 0.01
MIN_DBOS_ROWS_THRESHOLD = 10_000

_SWEEP_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dbos_retention_days": {
            "type": "number",
            "title": "DBOS history retention (days)",
            "description": (
                "How much finished workflow history DBOS keeps. Running, queued and delayed workflows "
                "are never collected, whatever this says."
            ),
            "default": DEFAULT_DBOS_RETENTION_DAYS,
            # Fractional so the sweep can be proven end to end within one firing; safe for the reason above.
            "minimum": MIN_DBOS_RETENTION_DAYS,
            # The operational policy bound; the clamp in `_invoke_retention_sweep` is the backstop.
            "maximum": 365.0,
        },
        "dbos_rows_threshold": {
            "type": "integer",
            "title": "DBOS history row cap",
            "description": (
                "A hard ceiling on the rows kept, applied on top of the age cutoff — whichever bound "
                "deletes more wins. It is the backstop for a burst the age bound is too generous for."
            ),
            "default": DEFAULT_DBOS_ROWS_THRESHOLD,
            # This bound becomes a cutoff and DBOS takes whichever deletes more, so a low threshold overrides the
            # age bound (see the floor rationale above).
            "minimum": MIN_DBOS_ROWS_THRESHOLD,
            "maximum": 100_000_000,
        },
    },
    "additionalProperties": False,
}


# ── What one sweep did ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunsSwept:
    """The `cron_runs` half's exact result. `by_age` is history ageing out; `by_cap` is one cron writing
    faster than the ledger holds."""

    by_age: int = 0
    by_cap: int = 0
    ceiling: int = MAX_ROWS_PER_SWEEP

    @property
    def total(self) -> int:
        return self.by_age + self.by_cap

    @property
    def truncated(self) -> bool:
        """Whether either bound stopped at its per-firing ceiling, so rows MAY remain ("may": deleting exactly
        `ceiling` and finishing looks the same)."""
        return self.by_age >= self.ceiling or self.by_cap >= self.ceiling


@dataclass(frozen=True)
class ActivityEventsSwept:
    """The `activity_events` half's result: one number, one bound (no per-actor or per-kind cap)."""

    by_age: int = 0
    ceiling: int = MAX_ROWS_PER_SWEEP

    @property
    def truncated(self) -> bool:
        """Whether the bound stopped at its per-firing ceiling (see `RunsSwept.truncated`)."""
        return self.by_age >= self.ceiling


@dataclass(frozen=True)
class TenancyExclusionsSwept:
    """The `tenancy_exclusions` half's result: one number, one bound. A per-class cap would delete exactly
    the evidence the seam's enforcement rests on.

    The number is buckets deleted, not exclusions: each row carries an `excluded_count`, and what the
    operator needs is how much of the trail aged out."""

    by_age: int = 0
    ceiling: int = EXCLUSIONS_ROWS_PER_SWEEP

    @property
    def truncated(self) -> bool:
        """Whether the bound stopped at its per-firing ceiling (see `RunsSwept.truncated`)."""
        return self.by_age >= self.ceiling


@dataclass(frozen=True)
class HistorySwept:
    """The DBOS half's result, measured: `garbage_collect` returns `None`, so it is a count either side.
    `before`/`after` are None when no count could be taken (unlaunched schema, or a moved shape)."""

    before: Optional[int] = None
    after: Optional[int] = None
    skipped: str = ""

    @property
    def removed(self) -> Optional[int]:
        if self.before is None or self.after is None:
            return None
        return max(0, self.before - self.after)


# ── The `cron_runs` sweep ─────────────────────────────────────────────────────────────────────────────

# Both statements order before they limit, so a truncated firing always consumes the oldest rows.

# A window function, not a per-cron loop: one plan over `ix_cron_runs_cron_id_fired_at`. `id DESC`
# breaks `fired_at` ties so survivors are deterministic.
_DELETE_BY_CAP_SQL = """
DELETE FROM cron_runs WHERE id IN (
    SELECT id FROM (
        SELECT id, row_number() OVER (
            PARTITION BY cron_id ORDER BY fired_at DESC, id DESC
        ) AS rank_in_cron
        FROM cron_runs
    ) ranked
    WHERE ranked.rank_in_cron > :cap
    ORDER BY ranked.rank_in_cron DESC
    LIMIT :ceiling
)
"""

# No index serves it: `ix_cron_runs_cron_id_fired_at` leads on `cron_id`, so this is a nightly
# sequential scan. An index on `fired_at` would fix it (DDL, deferred).
_DELETE_BY_AGE_SQL = """
DELETE FROM cron_runs WHERE id IN (
    SELECT id FROM cron_runs WHERE fired_at < :cutoff ORDER BY fired_at LIMIT :ceiling
)
"""


def sweep_cron_runs(
    session: Session,
    *,
    max_age_days: int,
    per_cron_cap: int,
    now: Optional[datetime] = None,
    ceiling: int = MAX_ROWS_PER_SWEEP,
) -> RunsSwept:
    """Apply BOTH `cron_runs` bounds and return what each removed. Does NOT commit — the caller's
    transaction owns that; the activity uses `sweep_committed_cron_runs`.

    A row survives only by satisfying both; age runs first so the cap ranks what remains.
    `retention_mode` is not read (see the module docstring). Each bound removes at most `ceiling` rows.
    """
    # A low-cadence cron is bounded by age alone (no keep-newest-N floor); specified behaviour, a per-cron
    # floor is a follow-up.
    at = now or datetime.now(timezone.utc)
    cutoff = at - timedelta(days=max_age_days)
    # `max(0, ...)`, not `or 0`: a driver's `-1` rowcount would otherwise make the total negative.
    by_age = max(0, session.execute(
        text(_DELETE_BY_AGE_SQL), {"cutoff": cutoff, "ceiling": ceiling}
    ).rowcount or 0)
    by_cap = max(0, session.execute(
        text(_DELETE_BY_CAP_SQL), {"cap": per_cron_cap, "ceiling": ceiling}
    ).rowcount or 0)
    if by_age or by_cap:
        log.info("cron retention: removed %d cron_runs row(s) older than %s and %d over the %d cap",
                 by_age, cutoff.isoformat(), by_cap, per_cron_cap)
    return RunsSwept(by_age=by_age, by_cap=by_cap, ceiling=ceiling)


def _committed(sweep: Callable[..., Any], session_factory, **kwargs: Any) -> Any:
    """Run a caller-transactional `sweep` on a session of its OWN, committed before it returns (the
    activity's entry point; the uncommitted form stays caller-transactional for tests)."""
    from app.db import SessionLocal

    with (session_factory or SessionLocal)() as session:
        swept = sweep(session, **kwargs)
        session.commit()
    return swept


def _committed_form(sweep: Callable[..., T]) -> Callable[..., T]:
    """`sweep_committed_<x>` for a caller-transactional `sweep_<x>`: the sweep's own keyword arguments, run through
    `_committed` on a session of its own. `_committed` is resolved when the form is called, not here, so tests may
    still monkeypatch it. Named `sweep_committed_<x>` from the sweep's own name, which is also the module attribute
    each form is bound to."""

    def committed(*, session_factory=None, **kwargs: Any) -> T:
        return _committed(sweep, session_factory, **kwargs)

    committed.__name__ = committed.__qualname__ = sweep.__name__.replace("sweep_", "sweep_committed_", 1)
    committed.__doc__ = f"`{sweep.__name__}`, committed on its own session."
    return committed


def _delete_oldest(
    session: Session, model: Any, stamp_column: Any, cutoff: datetime, ceiling: int, *extra_where: Any,
) -> int:
    """Delete `model` rows stamped before `cutoff` (and matching `extra_where`), oldest first, at most `ceiling`;
    the number removed. A Core statement on the mapped table, so the write seam sees it. Caller-transactional.

    `max(0, ...)`, not `or 0`: a driver's `-1` rowcount would otherwise make a total negative."""
    return max(0, session.execute(
        delete(model).where(model.id.in_(
            select(model.id).where(*extra_where, stamp_column < cutoff).order_by(stamp_column).limit(ceiling)
        ))
    ).rowcount or 0)


# See `_invoke_retention_sweep` for how these are sequenced.
sweep_committed_cron_runs = _committed_form(sweep_cron_runs)


# ── The `activity_events` sweep ───────────────────────────────────────────────────────────────────────

# The scope predicate is the design: `routes/tasks/activity.py::get_task_activity` and
# `task_run_metrics.py::_state_trails` read every `task:%`/`task_note:%` row as permanent history, so
# those are spared. `_` stays unescaped to match the reader's own `like("task_note:%")`. Only
# `created_at` is indexed, so once the backlog drains each firing walks the task rows for nothing;
# deferred DDL: a partial index `WHERE kind NOT LIKE 'task:%' AND kind NOT LIKE 'task_note:%'`, and
# `activity_events.id` (int4) will need `bigint`.
#
# Security events (`app/activity_kinds.py::SECURITY_EVENT_KINDS`) are cut at `:security_cutoff`, never
# younger than `SECURITY_EVENT_MIN_RETENTION_DAYS` however short the knob, so the hot copy covers the
# year SOC 2 asks for (PRD 5.4) and the archive (`app/security_archive.py`) holds the rest.
#
# `{org}` is empty for the deployment-wide pass and `AND org_id = :org_id` for one org's own, shorter window
# (`app/org_retention.py`): one statement, so the two passes can never spare different rows.
_DELETE_ACTIVITY_EVENTS_BY_AGE_SQL = """
DELETE FROM activity_events WHERE id IN (
    SELECT id FROM activity_events
    WHERE created_at < :cutoff{org}
      AND kind NOT LIKE 'task:%'
      AND kind NOT LIKE 'task_note:%'
      AND (kind <> ALL(:security_kinds) OR created_at < :security_cutoff)
    ORDER BY created_at
    LIMIT :ceiling
)
"""


def sweep_activity_events(
    session: Session,
    *,
    max_age_days: int,
    now: Optional[datetime] = None,
    ceiling: int = MAX_ROWS_PER_SWEEP,
    org_id: Optional[UUID] = None,
) -> ActivityEventsSwept:
    """Prune the activity log by age, excluding the task-pipeline history and holding security events to
    their one-year floor — see the note above `_DELETE_ACTIVITY_EVENTS_BY_AGE_SQL`. With `org_id`, that org's
    rows alone.

    Does NOT commit (caller-transactional, like `sweep_cron_runs`); the activity uses
    `sweep_committed_activity_events`.
    """
    # The statement's own invariant: a non-positive window would take the whole prunable population in one
    # firing. Unreachable through the activity (7-day floor, validated on write), so it raises on the
    # programming error rather than clamping. `sweep_cron_runs` lacks this guard; follow-up.
    if max_age_days <= 0:
        raise ValueError(
            f"activity_events retention window must be positive, got {max_age_days} — a cutoff at or "
            "after now() would delete the whole prunable activity log in one firing"
        )
    at = now or datetime.now(timezone.utc)
    cutoff = at - timedelta(days=max_age_days)
    # `max(0, ...)`, never `or 0` (see `sweep_cron_runs`).
    security_cutoff = min(cutoff, at - timedelta(days=SECURITY_EVENT_MIN_RETENTION_DAYS))
    params: dict[str, Any] = {
        "cutoff": cutoff,
        "security_cutoff": security_cutoff,
        "security_kinds": sorted(SECURITY_EVENT_KINDS),
        "ceiling": ceiling,
    }
    if org_id is not None:
        params["org_id"] = org_id
    statement = _DELETE_ACTIVITY_EVENTS_BY_AGE_SQL.format(org="" if org_id is None else " AND org_id = :org_id")
    by_age = max(0, session.execute(text(statement), params).rowcount or 0)
    if by_age:
        log.info("cron retention: removed %d activity_events row(s) older than %s%s "
                 "(task pipeline history excluded, security events kept to %s)",
                 by_age, cutoff.isoformat(), "" if org_id is None else f" for org {org_id}",
                 security_cutoff.isoformat())
    return ActivityEventsSwept(by_age=by_age, ceiling=ceiling)


sweep_committed_activity_events = _committed_form(sweep_activity_events)


# ── The `tenancy_exclusions` sweep ────────────────────────────────────────────────────────────────────

# No scope predicate: one population of machine-written counters. Single home for why it shares
# `activity_events_retention_days()`: the counter and event halves of one trail need one window. Cost:
# shortening it permanently deletes the seam's exclusion evidence (the knob's `help=` in
# `app/operator_settings/catalog.py::CATALOG` says so). Cuts on `occurred_at`, oldest first, served only while
# `occurred_at` leads `uq_tenancy_exclusions_bucket`.
_DELETE_TENANCY_EXCLUSIONS_BY_AGE_SQL = """
DELETE FROM tenancy_exclusions WHERE id IN (
    SELECT id FROM tenancy_exclusions WHERE occurred_at < :cutoff ORDER BY occurred_at LIMIT :ceiling
)
"""


def sweep_tenancy_exclusions(
    session: Session,
    *,
    max_age_days: int,
    now: Optional[datetime] = None,
    ceiling: int = EXCLUSIONS_ROWS_PER_SWEEP,
) -> TenancyExclusionsSwept:
    """Prune the tenancy seam's durable exclusion log by age — a blanket bound, correct here (see the note
    above `_DELETE_TENANCY_EXCLUSIONS_BY_AGE_SQL`).

    Does NOT commit (caller-transactional); the activity uses `sweep_committed_tenancy_exclusions`.
    """
    # The statement's own invariant, as in `sweep_activity_events`: a non-positive window would delete the
    # whole exclusion log, which nothing re-derives. Unreachable through the activity, so it raises.
    if max_age_days <= 0:
        raise ValueError(
            f"tenancy_exclusions retention window must be positive, got {max_age_days} — a cutoff at "
            "or after now() would delete the whole exclusion log in one firing"
        )
    at = now or datetime.now(timezone.utc)
    cutoff = at - timedelta(days=max_age_days)
    # `max(0, ...)`, never `or 0` (see `sweep_cron_runs`).
    by_age = max(0, session.execute(
        text(_DELETE_TENANCY_EXCLUSIONS_BY_AGE_SQL), {"cutoff": cutoff, "ceiling": ceiling}
    ).rowcount or 0)
    if by_age:
        log.info("cron retention: removed %d tenancy_exclusions bucket(s) older than %s",
                 by_age, cutoff.isoformat())
    return TenancyExclusionsSwept(by_age=by_age, ceiling=ceiling)


sweep_committed_tenancy_exclusions = _committed_form(sweep_tenancy_exclusions)


# ── The expired-transcript sweep ──────────────────────────────────────────────────────────────────────

# The bucket expires `transcripts/` and `artifacts/` 180 days after UPLOAD; a session's upload follows its
# start, so cutting on `started_at` retires the row no later than its bytes go — never a dead download.
# `unavailable` with the row's own reason is the honest state: no schema change, and the page already renders
# the recorded error.
TRANSCRIPT_EXPIRED_ERROR = "deleted by the transcript retention policy (180 days)"

_EXPIRE_TRANSCRIPTS_SQL = """
UPDATE sessions SET transcript_status = 'unavailable', transcript_error = :why
WHERE id IN (
    SELECT id FROM sessions
    WHERE transcript_status = 'uploaded' AND started_at < :cutoff
    ORDER BY started_at
    LIMIT :ceiling
)
"""


def sweep_expired_transcripts(
    session: Session, *, now: Optional[datetime] = None, ceiling: int = MAX_ROWS_PER_SWEEP,
) -> int:
    """Mark `uploaded` sessions past the transcript bound `unavailable`. Caller-transactional."""
    at = now or datetime.now(timezone.utc)
    cutoff = at - timedelta(days=POLICY_MAX_RETENTION_DAYS[RETENTION_CLASS_TRANSCRIPT])
    expired = max(0, session.execute(
        text(_EXPIRE_TRANSCRIPTS_SQL),
        {"why": TRANSCRIPT_EXPIRED_ERROR, "cutoff": cutoff, "ceiling": ceiling},
    ).rowcount or 0)
    if expired:
        log.info("cron retention: retired %d expired transcript(s) started before %s",
                 expired, cutoff.isoformat())
    return expired


sweep_committed_expired_transcripts = _committed_form(sweep_expired_transcripts)


# ── The trigger tables' sweep ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TriggersSwept:
    deliveries: int
    events: int
    dedup: int

    @property
    def total(self) -> int:
        return self.deliveries + self.events + self.dedup


def sweep_trigger_tables(
    session: Session, *, now: Optional[datetime] = None, ceiling: int = MAX_ROWS_PER_SWEEP,
) -> TriggersSwept:
    """Bound the trigger tables (constants in `app/triggers.py`): delivery rows go after 90 days, the stored
    event body inside one after 30 (the readable preview stays for the log), dedup entries after 90 (A5).
    Oldest first, at most `ceiling` rows per statement. Core statements on mapped tables, so the write seam
    sees them. Caller-transactional."""
    from sqlalchemy import delete, select, tuple_, update

    from app.models.automations import TriggerDedup, TriggerDelivery
    from app.triggers import DEDUP_RETENTION_DAYS, DELIVERY_RETENTION_DAYS, EVENT_RETENTION_DAYS, QUEUED, TRANSIENT_STATES

    at = now or datetime.now(timezone.utc)

    deliveries = max(0, session.execute(
        delete(TriggerDelivery).where(TriggerDelivery.id.in_(
            select(TriggerDelivery.id)
            .where(TriggerDelivery.received_at < at - timedelta(days=DELIVERY_RETENTION_DAYS))
            .order_by(TriggerDelivery.received_at).limit(ceiling)
        ))
    ).rowcount or 0)
    events = max(0, session.execute(
        update(TriggerDelivery).where(TriggerDelivery.id.in_(
            select(TriggerDelivery.id)
            .where(
                TriggerDelivery.received_at < at - timedelta(days=EVENT_RETENTION_DAYS),
                TriggerDelivery.event.is_not(None),
                # A row still waiting to file renders its event later; it is never emptied under it.
                TriggerDelivery.state.not_in((*TRANSIENT_STATES, QUEUED)),
            )
            .order_by(TriggerDelivery.received_at).limit(ceiling)
        )).values(event=None)
    ).rowcount or 0)
    dedup_cut = at - timedelta(days=DEDUP_RETENTION_DAYS)
    dedup = max(0, session.execute(
        delete(TriggerDedup).where(tuple_(TriggerDedup.trigger_id, TriggerDedup.dedup_key).in_(
            select(TriggerDedup.trigger_id, TriggerDedup.dedup_key)
            .where(TriggerDedup.first_seen_at < dedup_cut)
            .order_by(TriggerDedup.first_seen_at).limit(ceiling)
        ))
    ).rowcount or 0)
    if deliveries or events or dedup:
        log.info("cron retention: trigger deliveries %d removed, %d event bodies purged, %d dedup entries removed",
                 deliveries, events, dedup)
    return TriggersSwept(deliveries=deliveries, events=events, dedup=dedup)


sweep_committed_trigger_tables = _committed_form(sweep_trigger_tables)


# ── The age-out sweeps: ops history, tenant email sends, the delivery log ──────────────────────

def _age_out_sweep(name: str, label: str, aged: Callable[[], tuple[Any, Any, int, Sequence[Any]]]) -> Callable[..., int]:
    """`sweep_<x>` for a table whose rows simply age out: `aged()` — resolved on each call, so the model imports stay
    lazy — names the model, its stamp column, the retention days and the partitions, one `_delete_oldest` of at most
    `ceiling` rows per partition predicate (`()` sweeps the table whole). Logs `label` when anything was removed."""

    def sweep(session: Session, *, now: Optional[datetime] = None, ceiling: int = MAX_ROWS_PER_SWEEP) -> int:
        model, stamp, days, partitions = aged()
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
        removed = sum(_delete_oldest(session, model, stamp, cutoff, ceiling, *where) for where in partitions)
        if removed:
            log.info("cron retention: %d %s older than %s removed", removed, label, cutoff.isoformat())
        return removed

    sweep.__name__ = sweep.__qualname__ = name
    sweep.__doc__ = f"Delete the {label} older than their bound (`_age_out_sweep`), at most `ceiling` per partition."
    return sweep


def _aged_ops_snapshots() -> tuple[Any, Any, int, Sequence[Any]]:
    """`ops_snapshots` at the operational bound (365 days), partitioned per kind so each statement rides
    `uq_ops_snapshots_subject_hour`'s leading `(kind, taken_at)` instead of scanning the table."""
    from app.models.operator_dashboard import OpsSnapshot
    from app.ops_snapshots import OPS_SNAPSHOT_KINDS

    return (OpsSnapshot, OpsSnapshot.taken_at, POLICY_MAX_RETENTION_DAYS[RETENTION_CLASS_OPERATIONAL],
            [(OpsSnapshot.kind == kind,) for kind in OPS_SNAPSHOT_KINDS])


#: How long an org's sent-mail record is kept (`app/tenant_gmail/`): a year, the security record's floor.
TENANT_EMAIL_SENDS_RETENTION_DAYS = 365


def _aged_tenant_email_sends() -> tuple[Any, Any, int, Sequence[Any]]:
    from app.models.tenant_email import TenantEmailSend

    return TenantEmailSend, TenantEmailSend.created_at, TENANT_EMAIL_SENDS_RETENTION_DAYS, [()]


def _aged_email_deliveries() -> tuple[Any, Any, int, Sequence[Any]]:
    """`email_deliveries` at `email_deliveries.RETENTION_DAYS` (90)."""
    from app.email_deliveries import RETENTION_DAYS
    from app.models.notifications import EmailDelivery

    return EmailDelivery, EmailDelivery.created_at, RETENTION_DAYS, [()]


sweep_ops_snapshots = _age_out_sweep("sweep_ops_snapshots", "ops snapshot(s)", _aged_ops_snapshots)
sweep_committed_ops_snapshots = _committed_form(sweep_ops_snapshots)
sweep_tenant_email_sends = _age_out_sweep("sweep_tenant_email_sends", "tenant email send(s)", _aged_tenant_email_sends)
sweep_committed_tenant_email_sends = _committed_form(sweep_tenant_email_sends)
sweep_email_deliveries = _age_out_sweep("sweep_email_deliveries", "email deliveries", _aged_email_deliveries)
sweep_committed_email_deliveries = _committed_form(sweep_email_deliveries)


# ── The DMARC aggregate report sweep ─────────────────────────────────────────────────────


def sweep_dmarc_reports(
    session: Session, *, now: Optional[datetime] = None, ceiling: int = MAX_ROWS_PER_SWEEP,
) -> int:
    """Delete the reports whose window ended more than `dmarc.RETENTION_DAYS` (365) ago: first their rows, at most
    `ceiling`, then those reports left without rows, at most `ceiling` (a report holds up to `dmarc.MAX_RECORDS`
    rows, so deleting reports alone would cascade far past the ceiling); the next pass takes the rest. The number of
    reports removed. Core statements on the mapped tables, so the write seam sees them. Caller-transactional.
    Nothing to sweep in an edition without `system_mail`, which stores no report."""
    if not edition_features.enabled("system_mail"):
        return 0
    from sqlalchemy import delete, exists, select

    from app.dmarc import RETENTION_DAYS
    from app.models.notifications import DmarcReport, DmarcReportRow

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)
    expired = select(DmarcReport.id).where(DmarcReport.date_end < cutoff)
    session.execute(delete(DmarcReportRow).where(DmarcReportRow.id.in_(
        select(DmarcReportRow.id).where(DmarcReportRow.dmarc_report_id.in_(expired)).limit(ceiling)
    )))
    removed = max(0, session.execute(
        delete(DmarcReport).where(DmarcReport.id.in_(
            expired.where(~exists().where(DmarcReportRow.dmarc_report_id == DmarcReport.id))
            .order_by(DmarcReport.date_end).limit(ceiling)
        ))
    ).rowcount or 0)
    if removed:
        log.info("cron retention: %d DMARC report(s) older than %s removed", removed, cutoff.isoformat())
    return removed


sweep_committed_dmarc_reports = _committed_form(sweep_dmarc_reports)


# ── The Live Cast viewer's activity sweep ───────────────────────────────────────────────────

# Presence rows run one per open tab per minute (1,440 a tab-day), so a busy day of public viewers outgrows the
# shared 50,000-row ceiling and two days' bound would never converge. Re-derive, do not nudge: concurrent tabs x
# 1,440 x days of arrears one firing should clear — 2,000,000 is ~700 tabs open all day for two days.
LIVE_MINUTES_ROWS_PER_SWEEP = 2_000_000
# Events are capped per IP (`routes/live_public.py::EVENTS_PER_IP_WINDOW`: at most 864,000 a day from one
# address), but a popular day can still add far more than 50,000; the same 2,000,000 is a day's arrears at ~1,400
# events a minute, so the 400-day bound converges unless more than two addresses each spend their whole budget
# all day.
LIVE_EVENTS_ROWS_PER_SWEEP = 2_000_000
# Each DELETE removes at most this many rows and commits, so neither table is swept in one multi-million-row
# transaction (lock and WAL held for minutes); the firing loops batches up to each table's ceiling.
LIVE_BATCH_ROWS = 50_000


@dataclass(frozen=True)
class LiveViewersSwept:
    events: int
    minutes: int

    @property
    def total(self) -> int:
        return self.events + self.minutes


def sweep_live_viewer_activity(
    session: Session, *, now: Optional[datetime] = None, events_limit: int = LIVE_BATCH_ROWS,
    minutes_limit: int = LIVE_BATCH_ROWS,
) -> LiveViewersSwept:
    """One batch of the viewer measurement's bound (constants in `app/live_cast/models.py`): events go after 400 days
    (the dashboard's 1y range and its slack), presence rows after 2 (the realtime view reads 30 minutes). Oldest
    first, at most `events_limit` / `minutes_limit` rows (0 skips that table). Core statements on mapped tables, so
    the write seam sees them. Caller-transactional; `sweep_committed_live_viewer_activity` loops it."""
    from sqlalchemy import delete, select, tuple_

    from app.live_cast.models import EVENT_RETENTION_DAYS, MINUTE_RETENTION_DAYS, LiveViewerEvent, LiveViewerMinute

    at = now or datetime.now(timezone.utc)
    events = minutes = 0
    if events_limit > 0:
        events = max(0, session.execute(
            delete(LiveViewerEvent).where(LiveViewerEvent.id.in_(
                select(LiveViewerEvent.id)
                .where(LiveViewerEvent.at < at - timedelta(days=EVENT_RETENTION_DAYS))
                .order_by(LiveViewerEvent.at).limit(events_limit)
            ))
        ).rowcount or 0)
    if minutes_limit > 0:
        key = (LiveViewerMinute.minute, LiveViewerMinute.visitor_id, LiveViewerMinute.tab_id)
        minutes = max(0, session.execute(
            delete(LiveViewerMinute).where(tuple_(*key).in_(
                select(*key)
                .where(LiveViewerMinute.minute < at - timedelta(days=MINUTE_RETENTION_DAYS))
                .order_by(LiveViewerMinute.minute).limit(minutes_limit)
            ))
        ).rowcount or 0)
    return LiveViewersSwept(events=events, minutes=minutes)


def sweep_committed_live_viewer_activity(
    *, session_factory=None, now: Optional[datetime] = None, batch: int = LIVE_BATCH_ROWS,
    events_ceiling: int = LIVE_EVENTS_ROWS_PER_SWEEP, minutes_ceiling: int = LIVE_MINUTES_ROWS_PER_SWEEP,
) -> LiveViewersSwept:
    """`sweep_live_viewer_activity` in committed batches of at most `batch` rows a table, until each table has
    nothing older than its bound left (a short batch) or has reached its ceiling for this firing."""
    at = now or datetime.now(timezone.utc)
    events = minutes = 0
    events_open = minutes_open = True
    while True:
        events_limit = min(batch, events_ceiling - events) if events_open else 0
        minutes_limit = min(batch, minutes_ceiling - minutes) if minutes_open else 0
        if events_limit <= 0 and minutes_limit <= 0:
            break
        swept = _committed(sweep_live_viewer_activity, session_factory, now=at,
                           events_limit=max(0, events_limit), minutes_limit=max(0, minutes_limit))
        events += swept.events
        minutes += swept.minutes
        events_open = events_limit > 0 and swept.events == events_limit
        minutes_open = minutes_limit > 0 and swept.minutes == minutes_limit
    if events or minutes:
        log.info("cron retention: live viewer events %d removed, presence minutes %d removed", events, minutes)
    return LiveViewersSwept(events=events, minutes=minutes)


# ── The console key leases sweep ──────────────────────────────────────────────────────────────────────


def sweep_committed_console_key_leases(*, session_factory=None) -> int:
    """`console_key_leases.sweep_expired`, committed on its own session. Hygiene: the key route's lease arm
    already ignores an expired row, so this bounds the table and grants or revokes nothing."""
    from app import console_key_leases

    return _committed(console_key_leases.sweep_expired, session_factory)


# ── The DBOS sweep ────────────────────────────────────────────────────────────────────────────────────


def count_dbos_history(dbos: Any, *, session_factory=None) -> Optional[int]:
    """How many `workflow_status` rows this application owns, or None if that cannot be answered.

    Reads the schema off the launched engine, so it counts the table `garbage_collect` writes.

    Fails soft, even on a moved schema: the count is evidence and the collector prunes regardless. Not
    silent: an ERROR log and "row count unavailable" on the ledger; CI's canary catches the move. Its
    own short session, so a missing relation cannot poison a caller's transaction.
    """
    from app.db import SessionLocal
    from app.durable import runtime as durable_runtime

    try:
        schema = dbos_system_schema(dbos)
    except DbosGarbageCollectUnavailable as exc:
        log.error("cron retention: %s", exc)
        return None
    factory = session_factory or SessionLocal
    # This app's rows plus unclaimed pre-upgrade ones (`IS NULL`); omitting those would report "0 removed"
    # after deleting thousands. The 3.x collector applies no application filter at all — equal here only
    # because `durable_dbos_sys` holds this one application.
    query = text(
        f'SELECT count(*) FROM "{schema}"."{DBOS_STATUS_TABLE}" '
        f'WHERE "{DBOS_APP_NAME_COLUMN}" = :app OR "{DBOS_APP_NAME_COLUMN}" IS NULL'
    )
    try:
        with factory() as session:
            return int(session.execute(query, {"app": durable_runtime.APP_NAME}).scalar() or 0)
    except Exception as exc:  # noqa: BLE001 — a count is an observation, never a reason to stop sweeping
        log.info("cron retention: could not count DBOS history (%s)", exc)
        return None


_DBOS_OWNED_TABLES = ("workflow_status", "queues", "workflow_schedules", "operation_outputs")


def adopt_retired_dbos_rows(dbos: Any, *, session_factory=None) -> Optional[int]:
    """Move rows written under `RETIRED_APP_NAME` to `APP_NAME`, so this executor recovers and dequeues them.

    Only a release that was still draining when migration 202610291800 ran writes such rows; once none is
    left this updates nothing. `application_versions` stays with the migration: its owner/version pair is
    unique. Fails soft like the count: a table the engine never created is skipped.
    """
    from app.db import SessionLocal
    from app.durable import runtime as durable_runtime

    try:
        schema = dbos_system_schema(dbos)
    except DbosGarbageCollectUnavailable as exc:
        log.error("cron retention: %s", exc)
        return None
    factory = session_factory or SessionLocal
    moved = 0
    try:
        with factory() as session:
            present = set(session.execute(
                text("SELECT table_name FROM information_schema.columns "
                     "WHERE table_schema = :schema AND column_name = :column"),
                {"schema": schema, "column": DBOS_APP_NAME_COLUMN},
            ).scalars())
            for table in _DBOS_OWNED_TABLES:
                if table in present:
                    moved += session.execute(
                        text(f'UPDATE "{schema}"."{table}" SET "{DBOS_APP_NAME_COLUMN}" = :new '
                             f'WHERE "{DBOS_APP_NAME_COLUMN}" = :old'),
                        {"new": durable_runtime.APP_NAME, "old": durable_runtime.RETIRED_APP_NAME},
                    ).rowcount or 0
            session.commit()
    except Exception as exc:  # noqa: BLE001 — adoption is a repair, never a reason to stop sweeping
        log.warning("cron retention: could not adopt retired DBOS rows (%s)", exc)
        return None
    if moved:
        log.info("cron retention: adopted %d DBOS rows from the retired application name", moved)
    return moved


def sweep_dbos_history(
    dbos: Any,
    *,
    retention_days: float,
    rows_threshold: int,
    now: Optional[datetime] = None,
    session_factory=None,
) -> HistorySwept:
    """Prune DBOS's finished workflow history, and measure the result.

    `dbos` is the launched runtime; an unlaunched one raises rather than pruning nothing quietly.
    """
    at = now or datetime.now(timezone.utc)
    cutoff_ms = int((at - timedelta(days=retention_days)).timestamp() * 1000)
    # A tripwire: 2.x's collector also pruned an application database's `transaction_outputs`, with no
    # application filter — the console's own database, had one been configured. 3.x removed that database,
    # so this cannot fire today; it stays so a release that brings one back is refused loudly.
    if getattr(dbos, "_app_db", None) is not None:
        raise DbosGarbageCollectUnavailable(
            "DBOS now has an application database configured, so garbage_collect would also prune "
            "transaction_outputs in the control-plane database with no application filter — review that "
            "blast radius before letting the retention sweep run again"
        )
    collect = resolve_garbage_collect()
    before = count_dbos_history(dbos, session_factory=session_factory)
    collect(dbos, cutoff_ms, rows_threshold, batch_size=GC_BATCH_SIZE)
    after = count_dbos_history(dbos, session_factory=session_factory)
    log.info("cron retention: DBOS history collected to cutoff %s / %d rows (%s -> %s)",
             datetime.fromtimestamp(cutoff_ms / 1000, timezone.utc).isoformat(), rows_threshold,
             before, after)
    return HistorySwept(before=before, after=after)


def _live_dbos() -> tuple[Any, str]:
    """`(runtime, "")` when DBOS is constructed AND launched here, else `(None, why not)`.

    Unlaunched is ordinary (the lifespan launches DBOS after the scheduler starts); the DBOS half then
    skips rather than raising.
    """
    from app.cron_reconcile import launched_engine

    dbos = launched_engine()
    if dbos is None:
        return None, "the DBOS engine is not launched in this process"
    return dbos, ""


# ── The activity ──────────────────────────────────────────────────────────────────────────────────────


def _invoke_retention_sweep(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
    """One sweep: DBOS's history first, then the cron ledger, then the activity log, then the tenancy
    seam's exclusion log.

    The halves are independent and each commits itself (none uses `context.session`), so a moved private
    symbol cannot leave `cron_runs` unpruned, and the wrapper's transaction stays empty.

    A failed collection still fails the whole firing: caught only long enough to run the other halves,
    then re-raised so the skip counter escalates it.
    """
    # Clamped like the knobs: the row's param can tighten DBOS history, never extend it past the bound.
    retention_days = float(clamp_to_policy(
        RETENTION_CLASS_OPERATIONAL,
        float(context.params.get("dbos_retention_days", DEFAULT_DBOS_RETENTION_DAYS)),
    ))
    rows_threshold = int(context.params.get("dbos_rows_threshold", DEFAULT_DBOS_ROWS_THRESHOLD))

    dbos, why_not = _live_dbos()
    history, failure = HistorySwept(skipped=why_not), None
    if dbos is not None:
        adopt_retired_dbos_rows(dbos)
        try:
            history = sweep_dbos_history(
                dbos, retention_days=retention_days, rows_threshold=rows_threshold
            )
        except Exception as exc:  # noqa: BLE001 — re-raised below, once our own half has run
            log.exception("cron retention: the DBOS half failed")
            # `type(exc).__name__: exc`, never `repr`: `cron_runs.detail` is operator-facing, and a `DBAPIError` repr
            # carries the statement, bound parameters and host. The full repr is in `log.exception`.
            history = HistorySwept(skipped=f"it failed: {type(exc).__name__}: {exc}")
            failure = exc

    max_age_days = cron_runs_retention_days()
    # The most rows any one cron may keep. Per-cron because a ten-second heartbeat would otherwise evict a
    # nightly job's history.
    per_cron_cap = settings_reads.get_int(CRON_RUNS_PER_CRON_CAP_KEY)
    runs = sweep_committed_cron_runs(max_age_days=max_age_days, per_cron_cap=per_cron_cap)

    # Committed separately after the ledger prune, so a failure here cannot roll back that prune. Not
    # caught: it propagates to the wrapper, which records an `error` row.
    activity_max_age_days = activity_events_retention_days()
    activity = sweep_committed_activity_events(max_age_days=activity_max_age_days)

    # Reuses `activity_max_age_days` rather than re-reading it: two reads could straddle a save and prune
    # the two tables to different windows (see `_DELETE_TENANCY_EXCLUSIONS_BY_AGE_SQL`). Committed
    # separately and not caught, as above. Uses its own ceiling (see `EXCLUSIONS_ROWS_PER_SWEEP`).
    exclusions = sweep_committed_tenancy_exclusions(
        max_age_days=activity_max_age_days, ceiling=EXCLUSIONS_ROWS_PER_SWEEP,
    )

    # Committed separately and not caught, as above.
    leases = sweep_committed_console_key_leases()

    # Committed separately and not caught, as above.
    transcripts = sweep_committed_expired_transcripts()

    # Committed separately and not caught, as above.
    triggers = sweep_committed_trigger_tables()

    # Committed separately and not caught, as above.
    snapshots = sweep_committed_ops_snapshots()

    # Committed separately and not caught, as above.
    live_viewers = sweep_committed_live_viewer_activity()

    # Committed separately and not caught, as above.
    email_sends = sweep_committed_tenant_email_sends()

    # Committed separately and not caught, as above.
    deliveries = sweep_committed_email_deliveries()

    # Committed separately and not caught, as above.
    dmarc_reports = sweep_committed_dmarc_reports() if edition_features.enabled("system_mail") else None

    # Each org's own, shorter windows (`app/org_retention.py`), one committed transaction per org, after the
    # deployment-wide passes above. Not caught, as above.
    from app import org_retention

    orgs = org_retention.sweep_orgs()

    detail = _detail(
        runs, activity, exclusions, history, leases=leases, transcripts=transcripts, triggers=triggers,
        snapshots=snapshots, live_viewers=live_viewers, orgs=orgs, email_sends=email_sends, deliveries=deliveries,
        dmarc_reports=dmarc_reports,
        max_age_days=max_age_days, per_cron_cap=per_cron_cap,
        activity_max_age_days=activity_max_age_days,
        retention_days=retention_days, rows_threshold=rows_threshold,
    )
    if failure is not None:
        raise DbosGarbageCollectUnavailable(
            f"DBOS's history was not swept, so this firing records an error — the ledger half ran and "
            f"committed anyway: {detail}"
        ) from failure
    # `acted` only when something was deleted, so `retention_mode = 'acted'` does not keep a year of
    # "deleted nothing". A collection that ran but could not be measured is `acted` too.
    unmeasurable = not history.skipped and history.removed is None
    removed = (runs.total + activity.by_age + exclusions.by_age + leases + transcripts + triggers.total
               + snapshots + live_viewers.total + orgs.total + email_sends + (history.removed or 0))
    return ActivityResult.acted(detail) if removed or unmeasurable else ActivityResult.noop(detail)


def _detail(
    runs: RunsSwept,
    activity: ActivityEventsSwept,
    exclusions: TenancyExclusionsSwept,
    history: HistorySwept,
    # Keyword-only: three adjacent numbers whose transposition would type-check silently.
    *,
    leases: int,
    transcripts: int,
    triggers: Optional[TriggersSwept] = None,
    snapshots: Optional[int] = None,
    live_viewers: Optional[LiveViewersSwept] = None,
    orgs: Optional["OrgsSwept"] = None,
    email_sends: Optional[int] = None,
    deliveries: Optional[int] = None,
    dmarc_reports: Optional[int] = None,
    max_age_days: int,
    per_cron_cap: int,
    activity_max_age_days: int,
    retention_days: float,
    rows_threshold: int,
) -> str:
    """The Runs-tab line. Names the bound beside every number, so "removed 0" is readable without the
    settings page."""
    parts = [
        f"cron_runs: {runs.by_age} older than {max_age_days}d, "
        f"{runs.by_cap} over the {per_cron_cap:,}-run per-cron cap"
        + (f" (stopped at this firing's {runs.ceiling:,}-row ceiling; any remainder waits for the "
           "next firing)" if runs.truncated else ""),
        # Says what it spared, or a small count against a large table would read as a failing sweep.
        f"activity_events: {activity.by_age} older than {activity_max_age_days}d "
        "(task pipeline history excluded)"
        + (f" (stopped at this firing's {activity.ceiling:,}-row ceiling; any remainder waits for the "
           "next firing)" if activity.truncated else ""),
        # Names the unit (a bucket carries an `excluded_count`), and repeats the shared window so drift between
        # the two halves is visible on the ledger.
        f"tenancy_exclusions: {exclusions.by_age} bucket(s) older than {activity_max_age_days}d "
        "(the activity log's window, shared)"
        + (f" (stopped at this firing's {exclusions.ceiling:,}-row ceiling; any remainder waits for the "
           "next firing)" if exclusions.truncated else ""),
        f"console_api_key_leases: {leases} expired",
        f"sessions: {transcripts} transcript(s) retired past "
        f"{POLICY_MAX_RETENTION_DAYS[RETENTION_CLASS_TRANSCRIPT]}d",
    ]
    if triggers is not None:
        parts.append(
            f"triggers: {triggers.deliveries} deliveries older than 90d, {triggers.events} event bodies "
            f"older than 30d, {triggers.dedup} dedup entries older than 90d"
        )
    if snapshots is not None:
        parts.append(
            f"ops_snapshots: {snapshots} older than {POLICY_MAX_RETENTION_DAYS[RETENTION_CLASS_OPERATIONAL]}d"
        )
    if live_viewers is not None:
        from app.live_cast.models import EVENT_RETENTION_DAYS, MINUTE_RETENTION_DAYS

        parts.append(
            f"live viewers: {live_viewers.events} events older than {EVENT_RETENTION_DAYS}d, "
            f"{live_viewers.minutes} presence minutes older than {MINUTE_RETENTION_DAYS}d"
        )
    if email_sends is not None:
        parts.append(f"tenant_email_sends: {email_sends} older than {TENANT_EMAIL_SENDS_RETENTION_DAYS}d")
    if deliveries is not None:
        from app.email_deliveries import RETENTION_DAYS as DELIVERIES_RETENTION_DAYS

        parts.append(f"email_deliveries: {deliveries} older than {DELIVERIES_RETENTION_DAYS}d")
    if edition_features.enabled("system_mail"):
        from app.dmarc import RETENTION_DAYS as DMARC_RETENTION_DAYS

        if dmarc_reports is not None:
            parts.append(f"dmarc_reports: {dmarc_reports} older than {DMARC_RETENTION_DAYS}d")
    if orgs is not None:
        parts.append(orgs.line())
    if history.skipped:
        parts.append(f"DBOS history: not swept — {history.skipped}")
    elif history.removed is None:
        parts.append(
            f"DBOS history: collected to {retention_days:g}d / {rows_threshold:,} rows "
            "(row count unavailable)"
        )
    else:
        parts.append(
            f"DBOS history: {history.removed} removed ({history.before:,} -> {history.after:,}) "
            f"at {retention_days:g}d / {rows_threshold:,} rows"
        )
    return "; ".join(parts)


RETENTION_SWEEP_ACTIVITY = register(
    ActivityDescriptor(
        type="retention_sweep",
        label="Retention sweep",
        description=(
            "Prunes the cron ledger, the activity log, the tenancy seam's exclusion log, expired "
            "console key leases, the year-old hourly ops snapshots, the Live Cast viewer's activity, orgs' year-old sent-mail records and DBOS's finished workflow history, and retires sessions whose "
            "transcript the bucket has expired. Then applies each org's own shorter windows (Settings › "
            "Retention) to its transcripts, activity events and task artifacts. Every cron tick is a durable workflow, so without this the "
            "control plane's system schema grows for ever — DBOS ships a collector but never calls it. "
            "The activity log had no bound at all. Age limits for the ledger and the activity log are "
            "set in Settings; the exclusion log rides the activity log's limit; the DBOS "
            "bounds are this cron's own parameters. The task pipeline's own history — an epic's "
            "Activity tab and its run metrics — is never pruned."
        ),
        is_system=True,
        # Creatable so an operator can run a second, sharper sweep, and so a deleted row can come back; revision
        # 0147 seeds the activity.
        creatable=True,
        params=_SWEEP_PARAMS,
        # 03:20 daily in the configured zone: off the hour and half hour, away from the seeded system crons.
        default_schedule=DefaultSchedule(kind=SCHEDULE_KIND_CRON, cron_expression="0 20 3 * * *"),
        invoke=_invoke_retention_sweep,
        # A first sweep over a year of backlog is a batched delete over millions of rows; the 300s default
        # would report a working collector as an error nightly.
        timeout_seconds=1800.0,
    )
)
