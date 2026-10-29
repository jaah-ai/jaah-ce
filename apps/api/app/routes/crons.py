"""The cron layer under `/api/automations` — the shared writers, Run now, the fleet-wide ledger, the activity
catalogue and the schedule preview.

`write_cron_*` are every cron write's one path (`app/automation_service.py`, `routes/agent_crons.py`).
Validation is total and server-side, runs against the merged row (patch applied, then checked whole),
and every refusal names the field. `_validate` mirrors revision 0137's CHECKs so a violation is a
readable 422; `_check_min_gap` adds the one the database cannot express. No feature flag (AC1.1).
"""

# Authorization is tenant-admin: an `@org_admin(Tenancy.CALLER_PLANE)` declaration (G4) for `authz_guard`
# (undeclared routes are denied; `test_authz_registry.py` asserts completeness). ORG_ADMIN is its own
# guard branch, so no `authz.FEATURES` key is needed. `CALLER_PLANE`: the operator works every org's crons
# (org-zero machinery included); an Org Owner/Admin is bound to their own org and may schedule only activities that act
# inside it (`_refuse_machinery_for_a_tenant`).

# The persona rule is not written here: it is an `enum` on the runner activity's `params` JSON Schema
# (`validate_params`), so the picker and the validator read one declaration.

# Reconcile is post-commit and never fails the write: each successful write asks the `cron-projection`
# queue for a pass (`app/durable/cron_projection.py`). A failed pass logs and raises one deduped alarm;
# nothing retries on a timer, so the schedule stays divergent until the next pass gets through.
from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, NoReturn, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, true, tuple_
from sqlalchemy.orm import Session

from app import automation_service, cron_activities, cron_min_period, cron_runtime, keyset
from app.actors import actor_names, stamp_actor
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin
from app.entity_shape import RefOut, StrictModel, float_or_none, flush_or_409, from_row, get_or_404
from app.cron_runtime import SKIP_ESCALATION_THRESHOLD, cron_timezone, resolved_timezone_name
from app.cron_schedule import (
    MIN_GAP_OCCURRENCES,
    CronScheduleError,
    cron_expression_for_interval,
    describe_schedule,
    min_gap_over_next_occurrences,
    next_fire_for_row,
    validate_cron_expression,
)
from app.crons import (
    DEFAULT_RETENTION_MODE,
    RUN_OUTCOME_ERROR,
    org_for,
    RETENTION_MODES,
    RUN_OUTCOMES,
    SCHEDULE_KIND_CRON,
    SCHEDULE_KIND_INTERVAL,
    SCHEDULE_KINDS,
)
from app.db import get_session
from app.events import emit_console_event
from app.models.automations import Cron, CronRun
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.tasks import Task
from app.org_gate import require_org_active
from app.tenancy import rehome, seam
from app.tenancy.create_surface import named_org
from app.tenancy.ids import ORG_ZERO_ID, is_operator_plane
from app.tenancy.labels import OrgRef, org_ref_of, org_refs
from app.user_activity import record_write

log = logging.getLogger("cockpit-cron")

router = APIRouter(prefix="/api/crons", tags=["crons"])

# ── The health vocabulary ─────────────────────────────────────────────────────────────────────────────
# A derived label, never a column. The order is the precedence: a disabled cron is off, not stalled.
HEALTH_DISABLED = "disabled"
HEALTH_WEDGED = "wedged"
HEALTH_STALLED = "stalled"
HEALTH_OK = "ok"

# The ledger page's default and ceiling. Paginated: the ledger holds tens of thousands of rows.
RUNS_DEFAULT_LIMIT = 50
RUNS_MAX_LIMIT = 200

# Errors in a row, since the cron's last success, that put it on the ledger's failing list. Errors are always
# kept whatever the retention mode (`app/crons.py::should_record`) and `last_succeeded_at` dates the last
# success, so the count is exact even when the successes in between were never recorded.
FAILING_STREAK_THRESHOLD = 3
# How far back the streak looks: a cron that never succeeded has no `last_succeeded_at` to bound it, so it would
# otherwise count every error it ever kept (errors outlive every retention mode).
FAILING_STREAK_WINDOW = timedelta(days=7)

# How many firings the schedule preview answers with (five, what the editor renders), and its ceiling —
# each is a parse plus a walk.
PREVIEW_DEFAULT_COUNT = 5
PREVIEW_MAX_COUNT = 20

# The zone an interval schedule is evaluated in: always UTC (`cron_schedule.next_fire_for_row`), so the
# preview can say which zone produced its instants.
INTERVAL_TIMEZONE_NAME = "UTC"


# The keyset cursor's shape (`app/keyset.py`): `fired_at` is not unique (`fire_now` bursts), so the cursor
# carries the row id as tiebreak, matching the ORDER BY.
_RUNS_CURSOR_SHAPE = ["fired_at", "id"]

# The two `Numeric` columns a PATCH can move — read back as `Decimal`, written as `float` (see
# `_changed_fields`).
_NUMERIC_FIELDS: frozenset[str] = frozenset({"min_gap_seconds", "timeout_seconds"})


# ── Refusals ──────────────────────────────────────────────────────────────────────────────────────────


def _reject(field: str, message: str, status: int = 422) -> NoReturn:
    """One shape for every validation refusal, and it names the field first in `detail`, so every
    handler answers in one actionable format.
    """
    raise HTTPException(status_code=status, detail=f"{field}: {message}")


# ── Wire models ───────────────────────────────────────────────────────────────────────────────────────


class CronDetail(BaseModel):
    """One cron as a writer answers it: the row, plus what a reader cannot compute (next-fire needs the
    parser, the sentence the renderer, the last outcome a join)."""

    id: str
    name: str
    schedule_human: str
    schedule_kind: str
    activity_label: str
    enabled: bool
    next_fire_at: Optional[datetime]
    last_run_at: Optional[datetime]
    last_outcome: Optional[str]
    # The last firing, not the last kept run: under `acted` retention an all-`noop` cron keeps no rows,
    # so `last_run_at` alone would read "never run" for a working cron. This says the cadence is alive;
    # `last_run_at` says what is still readable.
    last_fired_at: Optional[datetime]
    # The newest firing's own sentence (the exception, the suppressing knob, the open task), so a
    # `wedged` cron's cause is visible. Same `DISTINCT ON` as `last_outcome` (`_last_runs`).
    last_detail: Optional[str]
    health: str
    # Which tenant this schedule belongs to. `app/tenancy/labels.py` owns the shape (why `slug` and `name`
    # both travel, and why an unresolvable org is `null`).
    org: Optional[OrgRef]
    description: str
    cron_expression: Optional[str]
    interval_seconds: Optional[int]
    activity_type: str
    activity_params: dict[str, Any]
    retention_mode: str
    min_gap_seconds: Optional[float]
    timeout_seconds: Optional[float]
    is_system: bool
    # The zone the schedule is evaluated in — one control-plane-wide knob, never per cron.
    timezone: str
    schedule_anchor_at: datetime
    last_tick_at: Optional[datetime]
    consecutive_skips: int
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str]
    updated_by: Optional[str]


class CronCreate(StrictModel):
    """The create body. `extra="forbid"` throughout: a silently dropped typo'd field is a schedule the
    operator did not ask for.

    Separate from `CronUpdate` so `org_id` is required here and optional there. Here the value goes to
    `org_for` (B13 may override it for machinery); `CronUpdate`'s is re-decided by `_rehomed`."""

    # Boundary B28: every caller names the org — an operator's create is fleet-wide and stamps nothing
    # (`app/tenancy/write_seam.py::_refuses_unstamped_create`), and a tenant may name only their own
    # (`named_org` refuses another). No ambient default that would silently misfile a cron.
    org_id: UUID = Field(description="The org this cron belongs to.")
    name: str
    description: str = ""
    schedule_kind: str
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    activity_type: str
    activity_params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    # The operator-created default is flat (AC12.3); the sub-5-minute rule for seeded system crons is the
    # migration's and would discard an operator's first firings here.
    retention_mode: str = DEFAULT_RETENTION_MODE
    min_gap_seconds: Optional[float] = None
    timeout_seconds: Optional[float] = None


class CronUpdate(StrictModel):
    """The PATCH body. Every field optional; `model_fields_set`, never a `None` value, distinguishes
    "not provided" from "clear it". `min_gap_seconds: null` means no floor, distinct from the runtime
    default."""

    # Lands on the row unjudged (`_apply` setattrs every provided field), so `write_cron_update` takes it
    # back off the instance and carries it as a local; only `_rehomed` may decide where the row goes
    # (`_rehome_or_conflict`, `_entitled_to_rehome`).
    org_id: Optional[UUID] = None
    name: Optional[str] = None
    description: Optional[str] = None
    schedule_kind: Optional[str] = None
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    activity_type: Optional[str] = None
    activity_params: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None
    retention_mode: Optional[str] = None
    min_gap_seconds: Optional[float] = None
    timeout_seconds: Optional[float] = None


class CronFireOut(BaseModel):
    """What a Run-now attempt actually did; both answers are a 200.

    `fire_now_sync` never raises and returns `False` for a miss (schedule not yet registered, coalesced
    duplicate) — an expected outcome, not a server fault. `run_cron_now` answers the two permanent
    causes as 409s first, so anything left here is worth retrying, and `detail` says why.
    """

    fired: bool
    detail: str


class FleetCronRef(RefOut):
    """One schedule as the fleet-wide ledger's cron filter needs it: the id to filter by, the name to
    show — not a `CronDetail`, which would compute next-fire and health for a dropdown.
    """


class FleetCronRunOut(BaseModel):
    """One firing on the fleet-wide ledger. `skip_reason` (rendered as prose) and `detail` (the evidence
    behind it) both travel."""

    id: str
    fired_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    outcome: str
    skip_reason: Optional[str]
    detail: str
    # `cron_runs.task_id`, so a firing links to the work it created (AC12.4).
    task_id: Optional[int]
    #: Which schedule fired: `cron_id` for the drawer link and filter, `cron_name` for display (from the
    #: page's own join, not a per-row lookup).
    cron_id: str
    cron_name: str


class FailingCronOut(RefOut):
    """One enabled cron whose last `consecutive_errors` firings all raised (`FAILING_STREAK_THRESHOLD` or more), counted
    within `FAILING_STREAK_WINDOW`."""

    consecutive_errors: int


class FleetCronRunsPage(BaseModel):
    """One page of the fleet-wide ledger, newest first, plus the vocabularies its filter bar is built
    from.

    `next_before` is the cursor for the following page, or None at the end. No total count: counting the
    busiest table costs a full scan per page. `crons` and `outcomes` ride the page so
    the filter bar draws on first paint, and both ignore the filters so selecting one option never hides
    the others.
    """

    runs: list[FleetCronRunOut]
    limit: int
    next_before: Optional[str]
    #: Every schedule the caller can read, fired or not, by name: a filter that hid a never-fired cron read as
    #: if it did not exist.
    crons: list[FleetCronRef]
    #: The outcomes actually present, not `RUN_OUTCOMES`; the closed vocabulary still validates the query
    #: parameter.
    outcomes: list[str]
    #: Every enabled cron at or past `FAILING_STREAK_THRESHOLD` errors since its last success (within
    #: `FAILING_STREAK_WINDOW`), worst first.
    #: Ignores the filters, like `crons`: the alarm is population-wide.
    failing: list[FailingCronOut]
    failing_threshold: int


class SchedulePreviewIn(StrictModel):
    """A schedule the operator is still typing — the preview request, not a row.

    Spelled exactly as `CronCreate`/`CronUpdate` spell the schedule fields, so a preview that answered
    cannot be followed by a refused save. `extra="forbid"` for the same reason as those models.
    """

    schedule_kind: str
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    count: int = Field(default=PREVIEW_DEFAULT_COUNT, ge=1, le=PREVIEW_MAX_COUNT)


class SchedulePreviewOut(BaseModel):
    """What that schedule would do — the sentence, the zone, and the next few firings.

    `next_fire_at` is a list because several firings distinguish cadences (`30 6 * * 1` vs
    `30 6 * * 1,2` agree on the first).
    """

    schedule_human: str
    timezone: str
    next_fire_at: list[datetime]


class DefaultScheduleOut(BaseModel):
    """The cadence an activity declares for a cron newly created against it
    (`cron_activities.DefaultSchedule`), spelled in `CronCreate`'s own field names.

    Renamed on the wire (`kind` → `schedule_kind`) so the create form fills its body without
    translating. Projected so the form opens each activity on its designed cadence rather than one flat
    interval (a Runner on a few-minute interval would file work at that rate).
    """

    schedule_kind: str
    cron_expression: Optional[str]
    interval_seconds: Optional[int]


class ActivityTypeOut(BaseModel):
    """One activity, projected straight off `cron_activities.ActivityDescriptor`, so the picker, params
    form, runtime dispatch and this projection read one declaration."""

    type: str
    label: str
    description: str
    is_system: bool
    creatable: bool
    params: dict[str, Any]
    # Never None: `ActivityDescriptor` requires it and `DefaultSchedule` enforces the exactly-one rule of
    # `ck_crons_exactly_one_schedule`.
    default_schedule: DefaultScheduleOut
    # So the Run-now control can refuse itself: `run_cron_now` is a permanent 409
    # (`FIRE_NO_MIN_GAP`) when neither the row nor this value sets a floor
    # (`cron_runtime._effective_min_gap`). None means no declared floor.
    min_gap_seconds: Optional[float]


class ActivityTypesOut(BaseModel):
    activity_types: list[ActivityTypeOut]


# ── Projection helpers ────────────────────────────────────────────────────────────────────────────────


def _activity_label(cron: Cron) -> str:
    """The registered label, falling back to the raw type for a row naming an unregistered activity
    (a retired activity renders identifiably, never a 500)."""
    try:
        return cron_activities.get(cron.activity_type).label
    except cron_activities.UnknownActivityError:
        return cron.activity_type


def _schedule_human(cron: Cron) -> str:
    """The sentence, or the raw expression when the row cannot be described.

    Raw-SQL writers and the seeding revision can bypass validation, and one malformed row must not 500
    the whole list.
    """
    try:
        return describe_schedule(cron.schedule_kind, cron.cron_expression, cron.interval_seconds)
    except CronScheduleError:
        # Both columns can be NULL for an unrecognised `schedule_kind` (raw-SQL writers).
        if cron.cron_expression:
            return cron.cron_expression
        if cron.interval_seconds is not None:
            return f"every {float_or_none(cron.interval_seconds):g} seconds"
        return f"unreadable {cron.schedule_kind!r} schedule"


def _next_fire(cron: Cron, now: datetime) -> Optional[datetime]:
    """When this cron fires next, or None.

    None for a disabled cron: a paused schedule has no next firing. The list renders "— disabled".
    """
    if not cron.enabled:
        return None
    try:
        return next_fire_for_row(
            cron.schedule_kind, cron.cron_expression, cron.interval_seconds, now, cron_timezone()
        )
    except CronScheduleError:
        return None


# The floor under "two consecutive gaps" when judging a cron overdue: 90s exceeds the engine's jitter
# (`_ScheduleThread`, up to 10% of the sleep) plus a rolling deploy's handover.
MIN_STALL_SECONDS = 90.0


def overdue_by(cron: Cron, at: datetime) -> Optional[float]:
    """Seconds since this cron's last heartbeat, if that is longer than two of its own gaps.

    References `last_tick_at`, falling back to `schedule_anchor_at` for a never-fired cron, so a
    never-delivered tick is measurable. Its one reader is `_health`.
    """
    reference = cron.last_tick_at or cron.schedule_anchor_at
    if reference is None:
        return None
    try:
        first = next_fire_for_row(
            cron.schedule_kind, cron.cron_expression, cron.interval_seconds, reference, cron_timezone()
        )
        second = next_fire_for_row(
            cron.schedule_kind, cron.cron_expression, cron.interval_seconds, first, cron_timezone()
        )
    except CronScheduleError:
        # An unparseable schedule has no expectation; the reconciler reports it.
        return None
    # Two full gaps, not "the second boundary from here" (which can be one gap when the reference sits
    # just before a boundary). The floor keeps fast cadences believable.
    gap = (second - first).total_seconds()
    allowance = max((second - reference).total_seconds(), 2 * gap, MIN_STALL_SECONDS)
    silent = (at - reference).total_seconds()
    return silent if silent > allowance else None


def _health(cron: Cron, now: datetime) -> str:
    """This cron's one-word verdict, derived from facts that each already have a home.

    `consecutive_skips` uses the runtime's own `SKIP_ESCALATION_THRESHOLD`, so the label and the
    wedged-cron alarm cannot disagree.
    """
    if not cron.enabled:
        return HEALTH_DISABLED
    if cron.consecutive_skips >= SKIP_ESCALATION_THRESHOLD:
        return HEALTH_WEDGED
    if overdue_by(cron, now) is not None:
        return HEALTH_STALLED
    return HEALTH_OK


def _detail_out(session: Session, cron: Cron) -> CronDetail:
    now = datetime.now(timezone.utc)
    names = actor_names(session, [cron.created_by_id, cron.updated_by_id])
    # The newest run: walks `ix_cron_runs_cron_id_fired_at` (DESC declared for this). `detail`'s width is
    # capped at write time (`cron_runtime.MAX_DETAIL_CHARS`).
    last = session.execute(
        select(CronRun.fired_at, CronRun.outcome, CronRun.detail)
        .where(CronRun.cron_id == cron.id)
        .order_by(CronRun.fired_at.desc(), CronRun.id.desc())
        .limit(1)
    ).first()
    return CronDetail(
        id=str(cron.id),
        name=cron.name,
        schedule_human=_schedule_human(cron),
        schedule_kind=cron.schedule_kind,
        activity_label=_activity_label(cron),
        enabled=cron.enabled,
        next_fire_at=_next_fire(cron, now),
        last_run_at=last.fired_at if last else None,
        last_outcome=last.outcome if last else None,
        # A column on the row: tells "never fired" from "fired, kept nothing" with no extra read.
        last_fired_at=cron.last_fired_at,
        # None means no kept run; a run with no sentence reads `''` (`cron_runs.detail` is NOT NULL).
        last_detail=last.detail if last else None,
        health=_health(cron, now),
        description=cron.description,
        cron_expression=cron.cron_expression,
        interval_seconds=cron.interval_seconds,
        activity_type=cron.activity_type,
        activity_params=cron.activity_params or {},
        retention_mode=cron.retention_mode,
        min_gap_seconds=float_or_none(cron.min_gap_seconds),
        timeout_seconds=float_or_none(cron.timeout_seconds),
        is_system=cron.is_system,
        # One id, one resolver — the same org indicator shape projects use
        # (`routes/projects.py::connect_existing_project`).
        org=org_ref_of(org_refs(session, [cron.org_id]), cron.org_id),
        timezone=resolved_timezone_name(),
        schedule_anchor_at=cron.schedule_anchor_at,
        last_tick_at=cron.last_tick_at,
        consecutive_skips=cron.consecutive_skips,
        created_at=cron.created_at,
        updated_at=cron.updated_at,
        created_by=names.get(cron.created_by_id),
        updated_by=names.get(cron.updated_by_id),
    )


# ── Validation ────────────────────────────────────────────────────────────────────────────────────────


def _descriptor_for(cron: Cron, provided: frozenset[str]) -> Optional[cron_activities.ActivityDescriptor]:
    """This row's activity descriptor, or None when the row names one this process has not registered.

    None is legitimate only when the request did not touch the activity (a rolling deploy must not
    block `{"enabled": false}`). A request setting `activity_type` or `activity_params` is refused by
    name.
    """
    try:
        return cron_activities.get(cron.activity_type)
    except cron_activities.UnknownActivityError as exc:
        if not ({"activity_type", "activity_params"} & provided):
            return None
        # `args[0]`, not `str(exc)`: `KeyError.__str__` is a repr, which would add quotes and escapes.
        _reject("activity_type", exc.args[0] if exc.args else f"unknown activity {cron.activity_type!r}")


def _validate(cron: Cron, provided: frozenset[str]) -> None:
    """Every rule, against the merged row — never against the request body.

    `provided` is the caller's `model_fields_set`, needed to tell a sent off-kind schedule column
    (refused) from a stored leftover (nulled), and whether an unregistered activity is being asked about.

    Validating the merged state makes PATCH correct without a second rule set and keeps create and
    update identical. Mirrors revision 0137's CHECKs as named-field 422s, plus `_check_min_gap`.
    """
    name = (cron.name or "").strip()
    if not name:
        _reject("name", "a cron must have a name")
    cron.name = name
    # The three NOT NULL columns a PATCH can reach with `null`: `description` and `activity_params`
    # clear to their empty value; `enabled` is refused (no tri-state switch).
    cron.description = (cron.description or "").strip()
    cron.activity_params = cron.activity_params or {}
    if cron.enabled is None:
        _reject("enabled", "must be true or false, not null")

    descriptor = _descriptor_for(cron, provided)
    _check_schedule(cron, provided)

    if descriptor is not None:
        try:
            cron_activities.validate_params(descriptor, cron.activity_params)
            # `project_ids` naming at least one project — beyond the schema subset. Deferred
            # import (cycle; see `_configured_project_ids`).
            from app.cron_activity_runner import ACTIVITY_TYPE as RUNNER, check_project_keys

            if descriptor.type == RUNNER:
                check_project_keys(cron.activity_params)
        except cron_activities.ParamsError as exc:
            # The registry's message names the offending key (including the persona `enum` refusal), so
            # it is carried as-is.
            _reject("activity_params", str(exc))

    if cron.retention_mode not in RETENTION_MODES:
        _reject(
            "retention_mode",
            f"must be one of {list(RETENTION_MODES)}, got {cron.retention_mode!r}",
        )

    for field in ("min_gap_seconds", "timeout_seconds"):
        value = float_or_none(getattr(cron, field))
        if value is None:
            continue
        # `isfinite` first: JSON `NaN` passes every Python comparison and Postgres orders it above all
        # numbers, yielding a 500 or an unsatisfiable floor.
        if not math.isfinite(value) or value <= 0:
            _reject(
                field,
                "must be a positive, finite number of seconds; send null for no floor / the runtime "
                "default — zero is not a floor of no seconds, and NaN is not a duration",
            )

    _check_min_gap(cron)


#: The request fields that change how often a cron fires — the ones the minimum period judges.
_SCHEDULE_FIELDS = frozenset({"schedule_kind", "cron_expression", "interval_seconds"})


def _check_min_period(session: Session, cron: Cron, org_id: UUID) -> None:
    """Refuse a schedule faster than `org_id`'s minimum cron period (`app/cron_min_period.py`).

    Judged against the org the row lands in. Only a save that sets the schedule or moves the org asks
    (the callers decide), so a row saved before the minimum was raised can still be renamed or disabled;
    the runtime throttles its firings instead. Queries, so the caller holds `no_autoflush`."""
    period = cron_min_period.resolve(session, org_id)
    try:
        gap = cron_min_period.schedule_gap(cron.schedule_kind, cron.cron_expression, cron.interval_seconds)
    except CronScheduleError:
        # `_check_schedule` accepted it, so it is well formed; an expression with no next firings to
        # measure fires too rarely to breach any minimum.
        return
    if gap < period.effective:
        field = "interval_seconds" if cron.schedule_kind == SCHEDULE_KIND_INTERVAL else "cron_expression"
        _reject(field, cron_min_period.below_minimum_detail(gap, period))


def _reject_off_kind_column(cron: Cron, provided: frozenset[str]) -> None:
    """Refuse the schedule column that does not belong to the row's final kind — only when this
    request sent it (see `_check_schedule`)."""
    off_kind = (
        ("cron_expression", SCHEDULE_KIND_INTERVAL, "interval_seconds")
        if cron.schedule_kind == SCHEDULE_KIND_INTERVAL
        else ("interval_seconds", SCHEDULE_KIND_CRON, "cron_expression")
    )
    field, kind, instead = off_kind
    if cron.schedule_kind in SCHEDULE_KINDS and field in provided and getattr(cron, field) is not None:
        _reject(
            field,
            f"this cron's schedule_kind is {kind!r}, which carries {instead} — send {instead} instead, "
            f"or set schedule_kind in the same request to switch it",
        )


def _check_schedule(cron: Cron, provided: frozenset[str]) -> None:
    """The discriminator, the column it requires, and the parser — plus the nulling that makes
    `ck_crons_exactly_one_schedule` true by construction.

    A stored off-kind column is nulled (a row switching kind must lose it). A sent off-kind column is
    refused by name: nulling it would answer 200 while silently ignoring the caller's change.
    """
    _reject_off_kind_column(cron, provided)
    if cron.schedule_kind not in SCHEDULE_KINDS:
        _reject(
            "schedule_kind",
            f"must be one of {list(SCHEDULE_KINDS)}, got {cron.schedule_kind!r}",
        )
    if cron.schedule_kind == SCHEDULE_KIND_CRON:
        cron.interval_seconds = None
        if not (cron.cron_expression or "").strip():
            _reject(
                "cron_expression",
                f"a {SCHEDULE_KIND_CRON!r} schedule needs a cron expression",
            )
        try:
            cron.cron_expression = validate_cron_expression(cron.cron_expression or "")
        except CronScheduleError as exc:
            # The parser's own complaint is carried. The parser is never named here:
            # `test_cron_schedule.py` asserts exactly one module under `app/` mentions it.
            _reject("cron_expression", str(exc))
        return
    cron.cron_expression = None
    if cron.interval_seconds is None:
        _reject(
            "interval_seconds",
            f"an {SCHEDULE_KIND_INTERVAL!r} schedule needs interval_seconds",
        )
    try:
        cron_expression_for_interval(cron.interval_seconds)
    except CronScheduleError as exc:
        # The translation's message names the accepted range — carried as-is.
        _reject("interval_seconds", str(exc))


def _check_min_gap(cron: Cron) -> None:
    """The rule the database cannot hold: a floor must be smaller than the smallest gap the schedule
    produces.

    One path for both kinds: for `interval` it reproduces `ck_crons_min_gap_below_interval`; for `cron`
    it is the only enforcement. Twenty occurrences (`app/cron_schedule.py::MIN_GAP_OCCURRENCES`), since
    mixed-gap expressions would otherwise pass or fail depending on when Save was pressed. Refused, not
    clamped: a floor at or above the smallest gap silently changes the cadence.
    """
    floor = float_or_none(cron.min_gap_seconds)
    if floor is None:
        return
    try:
        smallest = min_gap_over_next_occurrences(
            cron.schedule_kind,
            cron.cron_expression,
            cron.interval_seconds,
            datetime.now(timezone.utc),
            cron_timezone(),
        )
    except CronScheduleError as exc:
        # Refused, not accepted: validation parses from a fixed epoch and this walks from `now()`, so an
        # exhausted expression reaches here; accepting would admit any floor where this is the only
        # enforcement.
        _reject(
            "min_gap_seconds",
            f"this schedule's smallest gap could not be measured ({exc}), so a floor cannot be checked "
            f"against it — clear min_gap_seconds or use a schedule that fires within the next "
            f"{MIN_GAP_OCCURRENCES} occurrences",
        )
    if floor >= smallest:
        _reject(
            "min_gap_seconds",
            f"{floor:g}s is at or above this schedule's smallest gap ({smallest:g}s, measured over the "
            f"next {MIN_GAP_OCCURRENCES} firings) — every firing but the first would be held into the "
            f"next one",
        )


def _apply(cron: Cron, body: CronUpdate) -> dict[str, Any]:
    """Copy the provided fields onto the row, returning what each one held before.

    `model_fields_set`, not a `None` test (`min_gap_seconds: null` means no floor). Returns prior
    values because "did it move" is only answerable after `_validate` normalizes (`_changed_fields`).
    """
    before: dict[str, Any] = {}
    for field in CronUpdate.model_fields:
        if field not in body.model_fields_set:
            continue
        before[field] = getattr(cron, field)
        setattr(cron, field, getattr(body, field))
    return before


def _changed_fields(cron: Cron, before: dict[str, Any]) -> list[str]:
    """Which of the patched fields actually moved, compared after normalization.

    The audit summary's subject and the gate on stamping, so a no-op patch does not rewrite provenance.
    `Numeric` columns go through `float_or_none`: `Decimal("60.1") != 60.1`, so a re-sent value would
    otherwise read as changed.
    """
    return [
        field
        for field, previous in before.items()
        if _comparable(field, getattr(cron, field)) != _comparable(field, previous)
    ]


def _comparable(field: str, value: Any) -> Any:
    """`value` in the form that compares equal to itself across the `Decimal`/`float` boundary."""
    return float_or_none(value) if field in _NUMERIC_FIELDS else value


def _reconcile_later(background: BackgroundTasks) -> None:
    """Ask for the projection pass once the write has committed (after the response). Deferred import:
    the queue module drags DBOS in. The pass itself runs on the queue, fleet-wide, never in this request."""
    from app.durable import cron_projection

    background.add_task(cron_projection.request_projection)


# ── The activity catalogue ────────────────────────────────────────────────────────────────────────────


@router.get("/activity-types", response_model=ActivityTypesOut)
@org_admin(Tenancy.CALLER_PLANE)
def list_activity_types(
    actor: AdminUser = Depends(require_user),
) -> ActivityTypesOut:
    """The activity registry, projected — no vocabulary of its own.

    Every descriptor, not only creatable ones: existing crons (e.g. the system jobs) point at
    non-creatable activities and must be nameable. `creatable` lets the picker filter. A tenant is not
    offered machinery (`_refuse_machinery_for_a_tenant`) and sees no system cron to name.
    """
    operator = _is_operator(actor)
    return ActivityTypesOut(
        activity_types=[
            from_row(
                ActivityTypeOut, descriptor,
                default_schedule=DefaultScheduleOut(
                    schedule_kind=descriptor.default_schedule.kind,
                    cron_expression=descriptor.default_schedule.cron_expression,
                    interval_seconds=descriptor.default_schedule.interval_seconds,
                ),
            )
            for descriptor in cron_activities.all_descriptors()
            if operator or not descriptor.is_system
        ],
    )


@router.post("/preview", response_model=SchedulePreviewOut)
@org_admin(Tenancy.CALLER_PLANE)
def preview_schedule(
    body: SchedulePreviewIn,
    _actor: AdminUser = Depends(require_user),
) -> SchedulePreviewOut:
    """What an unsaved schedule would do — the sentence and its next firings, in the configured zone.

    Server-side because the engine's parser (`second_at_beginning=True`) and zone
    (`cron_runtime.cron_timezone()`) cannot be reproduced reliably in a browser; the preview walks the
    same function as a saved row's `next_fire_at`.

    Read-only: the transient `Cron` is a
    validation vehicle never added to the session, so the preview refuses exactly what a save would.
    Takes no `session`.
    """
    draft = Cron(
        schedule_kind=body.schedule_kind,
        cron_expression=body.cron_expression,
        interval_seconds=body.interval_seconds,
    )
    # The caller's own field set minus `count`; a draft has no stored row, so every column is "sent".
    _check_schedule(draft, frozenset(body.model_fields_set) - {"count"})

    tz = cron_timezone()
    cursor = datetime.now(timezone.utc)
    fires: list[datetime] = []
    try:
        for _ in range(body.count):
            cursor = next_fire_for_row(
                draft.schedule_kind, draft.cron_expression, draft.interval_seconds, cursor, tz
            )
            fires.append(cursor)
    except CronScheduleError as exc:
        # Reachable despite `_check_schedule` passing: validation uses a fixed epoch, this walks from
        # `now()`, so an expression can run out mid-walk. (The parser is never named here — see above.)
        _reject(
            "cron_expression" if draft.schedule_kind == SCHEDULE_KIND_CRON else "interval_seconds",
            f"this schedule has no further firings to preview: {exc}",
        )
    return SchedulePreviewOut(
        schedule_human=describe_schedule(
            draft.schedule_kind, draft.cron_expression, draft.interval_seconds
        ),
        # The zone the walk actually used: interval rows are evaluated in UTC, so the caption
        # must not claim the control-plane zone.
        timezone=(
            resolved_timezone_name()
            if draft.schedule_kind == SCHEDULE_KIND_CRON
            else INTERVAL_TIMEZONE_NAME
        ),
        next_fire_at=fires,
    )


def _creatable_descriptor(activity_type: str) -> cron_activities.ActivityDescriptor:
    """The descriptor a CREATE may point at. Refuses an unknown name and a non-creatable one alike."""
    try:
        descriptor = cron_activities.get(activity_type)
    except cron_activities.UnknownActivityError as exc:
        _reject("activity_type", exc.args[0] if exc.args else f"unknown activity {activity_type!r}")
    if not descriptor.creatable:
        _reject(
            "activity_type",
            f"activity {descriptor.type!r} ({descriptor.label}) is not one a cron may be created for; "
            f"it exists to be pointed at by a cron that already names it",
        )
    return descriptor


def _is_operator(actor: AdminUser) -> bool:
    """Whether this superadmin is the deployment's operator (`is_operator_plane`), not a tenant's admin."""
    return is_operator_plane(actor.org_id, actor.is_superadmin)


def _refuse_machinery_for_a_tenant(actor: AdminUser, is_system: bool, label: str) -> None:
    """422 when a tenant superadmin points a cron at a system activity.

    A firing's reach is its activity's: a non-system activity runs bound to the cron's org
    (`cron_runtime._run_fire`), but a system one (lease-drain, task-pipeline, retention —
    `cron_system_activities`, `cron_retention`) sweeps every org from org zero. Only the operator may
    schedule, edit onto, or fire one."""
    if is_system and not _is_operator(actor):
        _reject(
            "activity_type",
            f"{label} is control-plane machinery that acts across every organization, so only an "
            f"operator may schedule or run it — pick an activity that acts inside your own organization",
        )


def _project_reachable_or_reject(
    session: Session, cron: Cron, *, org_id: Optional[UUID] = None
) -> None:
    """Refuse, at the write, a cron whose configured project its own org cannot reach.

    `org_id` is the org the row will be in when the request finishes — on the patch path not yet
    `cron.org_id`, since a move goes through `app/tenancy/rehome.py` after the flush. `None` means the
    row's own (create, where `org_for` already stamped it).

    A firing binds the cron's org (`cron_runtime._run_fire`), so a project in another org is
    invisible and every firing would error. This stops that state being minted.

    For the operator the read is unfiltered (`CALLER_PLANE` binds `seam.OPERATOR`), so "does not exist"
    and "exists elsewhere" are named apart — safe fleet-wide, unlike at the firing
    (`automation_fire.file_task_target`). A tenant's read is narrowed to their org, so another org's
    project reads as "does not exist" and nothing crosses the boundary. Projects come from `_configured_project_ids`; every one
    is checked and the first offender named.
    """
    lands_in = cron.org_id if org_id is None else org_id
    for pid in _configured_project_ids(cron.activity_params):
        project_org = session.scalar(select(Project.org_id).where(Project.id == pid))
        if project_org == lands_in:
            continue
        if project_org is None:
            _reject("activity_params", f"project #{pid} does not exist")
        _reject(
            "activity_params",
            f"project #{pid} belongs to another organization, and a cron can only file work into a "
            f"project in its own org — pick a project in this cron's org, or move the cron to that "
            f"project's org on its Advanced tab",
        )


def _configured_project_ids(params: Any) -> list[int]:
    """The projects a params bag names ([] for none) — the one shape test every project-facing guard
    uses. Takes the bag so `write_cron_update` can compare committed params too.

    Rule home: `cron_activity_runner.configured_project_ids` (deferred import: that module imports the
    route layer)."""
    from app.cron_activity_runner import configured_project_ids

    return configured_project_ids(params)


def _owner_in_org_or_reject(session: Session, cron: Cron, *, org_id: UUID) -> None:
    """Refuse a move that would leave this cron owned by an operator in the org it just left.

    A firing refuses when the owner's org differs from the automation's (`automation_fire._owner_or_reason`),
    and a move re-files the row without re-owning it (`stamp_actor` keeps authorship). Without this,
    Save answers 200 and every firing breaks. Asked together with `_project_reachable_or_reject`.

    Creates are not guarded here: a superadmin creating a cron for another tenant is B28 by design.

    The owner is the cron's `created_by_id`, as the fire engine files (`automation_fire._owner_or_reason`); these return rather than
    refuse: `is_system` (no owner), an unresolvable or system-actor `created_by_id` (not repairable
    here), an owner with no org. Only when the cron names a project.

    A filtered `select()`, never `session.get` (an unfilterable cross-org oracle); unfiltered for the
    operator, narrowed to their org for a tenant. No identity escape.
    """
    if not _configured_project_ids(cron.activity_params) or cron.is_system or cron.created_by_id is None:
        return
    owner = session.execute(
        select(AdminUser.is_system, AdminUser.org_id).where(AdminUser.id == cron.created_by_id)
    ).first()
    if owner is None or owner.is_system or owner.org_id is None or owner.org_id == org_id:
        return
    _reject(
        "org_id",
        f"this cron is owned by an operator in another organization ({owner.org_id}), and a firing files "
        f"work as its owner — moved there it would be refused on every firing instead of running. Move "
        f"it to its owner's org, or have an operator in that org schedule it",
    )


def _cron_task_in_flight_or_conflict(session: Session, cron: Cron) -> None:
    """Refuse a move while the task this cron most recently filed is still open.

    A move leaves the cron's tasks in the old org (`created_by_cron_id` is unkeyed, not derived), and
    the firing binds the new org, so the fire engine's per-target overlap check would see nothing and
    the next firing would duplicate running work.

    Uses the engine's own `NOT_BLOCKING_STATES` (`awaiting_response` does not block), imported inside
    the function (cycle). Newest row only, `id` DESC. The read is unfiltered
    for the operator and org-narrowed for a tenant (`_project_reachable_or_reject`).

    Left dangling after a move, not a breach: completed tasks' `created_by_cron_id` and
    `cron_runs.task_id` point into the old org.
    """
    from app.automation_fire import NOT_BLOCKING_STATES

    # Newest row PER configured project; no projects → the cron's newest.
    scopes = [Task.project_id == pid for pid in _configured_project_ids(cron.activity_params)]
    for scope in scopes or [true()]:
        row = session.execute(
            select(Task.id, Task.title, Task.state)
            .where(Task.created_by_cron_id == cron.id, scope)
            .order_by(Task.id.desc())
            .limit(1)
        ).first()
        if row is not None and row.state not in NOT_BLOCKING_STATES:
            break
    else:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"org_id: this cron's task #{row.id} ({row.title!r}) is still {row.state}, and moving the "
            f"cron would leave it behind — the next firing would not see it and would file a duplicate. "
            f"Wait for it to finish, or stop it, then move the cron"
        ),
    )


def _entitled_to_rehome(*, was_org: Optional[UUID], actor_org: Optional[UUID]) -> bool:
    """Whether this caller may move a cron row whose committed org is `was_org` onto another org.

    Allowed: none (a row being created), the operator plane (machinery, D-5), and the caller's own org.
    Single-homed because `_plane_for_write` and `write_cron_update` both decide it; the two-org arm in
    `test_crons_routes.py` guards the cross-org write a split rule once allowed."""
    if was_org is None or was_org == ORG_ZERO_ID:
        return True
    return actor_org is not None and was_org == actor_org


def _rehomed(
    session: Session,
    cron: Cron,
    *,
    was_org: Optional[UUID],
    actor_org: Optional[UUID],
    named: Optional[UUID],
    operator: bool,
) -> UUID:
    """The org an explicit `org_id` patch may actually put this row on — or the refusal it earns.

    A re-decision returning the value, since `_apply` already wrote the caller's uuid and the caller
    took it back off. In order:
      * no org (`{"org_id": null}`) — refused first: the NOT NULL column cannot be unfiled.
      * machinery (`is_system`) — 422: system crons live on the operator plane (`app/crons.py::org_for`).
      * another tenant's cron — 403, per `_entitled_to_rehome`.
      * `named_org` — 422 for a made-up or wound-down destination (no FK to `orgs`), before…
      * the destination entitlement, so a caller cannot push a row into a tenant they could not pull it
        back from. Org zero → own org and anything → org zero stay open — for the operator only: a
        tenant's cron stays in the tenant's own org (the operator plane is not a tenant's to write).

    Decides only; `_rehome_or_conflict` moves the row (`app/tenancy/rehome.py`).
    """
    if named is None:
        _reject("org_id", "a cron belongs to an org — name one, or leave the field out")
    if cron.is_system:
        _reject(
            "org_id",
            "this cron is control-plane machinery, and machinery lives on the operator plane whatever "
            "org is named — its org follows its activity, which is not an operator's to override",
        )
    if not _entitled_to_rehome(was_org=was_org, actor_org=actor_org):
        raise HTTPException(
            status_code=403,
            detail=(
                "org_id: this cron belongs to another org, and re-filing it somewhere else is not this "
                "caller's to do"
            ),
        )
    if not operator and named != actor_org:
        raise HTTPException(
            status_code=403,
            detail="org_id: a cron in your organization stays in your organization",
        )
    target = named_org(session, named)
    if not _entitled_to_rehome(was_org=target, actor_org=actor_org):
        raise HTTPException(
            status_code=403,
            detail=(
                "org_id: a cron can only be re-filed onto the operator plane or onto this caller's own "
                "org — pushing it into another tenant is not this caller's to do, and nobody here could "
                "pull it back"
            ),
        )
    return target


@contextmanager
def _plane_for_write(
    session: Session, cron: Cron, *, was_org: Optional[UUID], actor_org: Optional[UUID]
) -> Iterator[None]:
    """The scope the write of `cron` must run under: the caller's for a tenant's row, nothing for
    machinery.

    A system cron belongs to org zero (`app/crons.py::org_for`), and writing it from an org-bound
    request would be a cross-org write the write seam refuses. Routes here declare `Tenancy.CALLER_PLANE`:
    the operator binds fleet-wide, a tenant binds their own org and can reach no org-zero row
    (`_refuse_machinery_for_a_tenant`, `_rehomed`), so the escape is a fail-safe for the operator.

    `detached()` alone, not with `operator_scope()`: this block reads nothing, and `detached()` can
    only make it read less. It uses the write seam's "nothing is bound" arm.

    Two conditions, both necessary: the write files the row on the operator plane at the flush, and
    the row was the operator plane's, is new, or is the caller's own to promote
    (`was_org == actor_org`). On update the committed org is what is seen, since tenant moves go
    through `app/tenancy/rehome.py`. Non-system rows get no escape.

    The flush settles the whole unit of work, so any other pending tenant-owned instance when the
    escape opens is a loud failure rather than a silent widening. The API test suite's
    tenancy scope-site census lists this escape.
    """
    files_on_operator_plane = cron.org_id == ORG_ZERO_ID
    entitled = _entitled_to_rehome(was_org=was_org, actor_org=actor_org)
    if not (files_on_operator_plane and entitled):
        yield
        return
    # Tenant-owned instances only (`stamp_org` keys off `TENANT_OWNED`). `record_write`'s audit row is
    # one (`ActivityEvent` is `Stored`), which is why `write_cron_delete` flushes before opening the escape.
    from app.tenancy.manifest import TENANT_OWNED

    riding = [
        obj for obj in (set(session.new) | set(session.dirty) | set(session.deleted))
        if obj is not cron and type(obj) in TENANT_OWNED
        and (obj in session.new or obj in session.deleted
             or session.is_modified(obj, include_collections=False))
    ]
    if riding:
        raise RuntimeError(
            "app/routes/crons.py::_plane_for_write was asked to open the tenancy write seam for one "
            f"operator-plane cron, but {len(riding)} other instance(s) are pending in the same unit of "
            f"work and would ride the same flush with the cross-org guard disarmed: "
            f"{sorted({type(o).__name__ for o in riding})}. The escape's entitlement argument covers "
            "exactly this row (apps/api/test_tenancy_scope_sites.py). Flush or expunge the others first."
        )
    with seam.detached():
        yield


def _flush_or_conflict(session: Session, cron: Cron) -> None:
    """Flush, turning the one constraint validation cannot mirror into a readable 409.

    `uq_crons_system_activity_type` is a partial unique index only the database can decide at write
    time, so flush-then-catch rather than SELECT-then-insert (no race window). The detail is built before
    the flush: `session.rollback()` expires the instance, and a re-read would name the pre-patch activity.
    """
    flush_or_409(
        session,
        f"activity_type: a system cron for {cron.activity_type!r} already exists — there may be "
        f"only one, so that double-scheduling it is unrepresentable rather than merely tested",
        constraints=frozenset({"uq_crons_system_activity_type"}),
    )


def _rehome_or_conflict(session: Session, cron: Cron, target: UUID) -> None:
    """Move this cron and its ledger into `target`, through the one module that may move a row's tenant.

    Not `cron.org_id = target`: `cron_runs`' composite non-deferrable `(cron_id, org_id)` key makes any
    two-statement order fail. `app/tenancy/rehome.py` owns the argument and the working shape (one
    data-modifying CTE). `ON UPDATE CASCADE` was tried and reverted: it would be a second tenant-move
    path without `rehome._blockers`' refusals.

    The caller restores the attribute after `_changed_fields` and before `_flush_or_conflict`, so the
    ORM does not issue a competing UPDATE. `session.flush()` first stays as a guard for future child
    writes. `RehomeBlocked` is unreachable for `crons` today but is translated, not swallowed — an
    operator must not be told a failed move succeeded.
    """
    session.flush()
    try:
        moved = rehome.rehome_row(session, Cron.__tablename__, cron.id, target)
    except rehome.RehomeBlocked as blocked:
        raise HTTPException(
            status_code=409,
            detail=f"org_id: {blocked}",
        ) from blocked
    if moved:
        # The instance still holds the pre-move value; expiring the attribute reloads it on access
        # without marking it dirty, so the write seam sees no org change.
        session.expire(cron, ["org_id"])


# ── The fleet-wide ledger ─────────────────────────────────────────────────────────────────────────────


@router.get("/runs", response_model=FleetCronRunsPage)
@org_admin(Tenancy.CALLER_PLANE)
def list_fleet_cron_runs(
    limit: int = Query(default=RUNS_DEFAULT_LIMIT, ge=1, le=RUNS_MAX_LIMIT),
    before: Optional[str] = Query(default=None),
    cron: Optional[UUID] = Query(default=None),
    outcome: Optional[str] = Query(default=None),
    _actor: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> FleetCronRunsPage:
    """Every cron's firings, newest first, one bounded page at a time.

    Keyset, not offset: rows append continuously, so offsets would repeat or skip. The `(fired_at, id)`
    cursor (`app/keyset.py`) matches the ORDER BY, so every page costs the same; `limit` is
    capped by the signature, refused rather than silently clamped. Filters apply before the keyset window,
    so `next_before` resumes within the filtered sequence. Names arrive on one join, not a query per row.

    Cost: `ix_cron_runs_cron_id_fired_at` serves the `cron`-filtered page; the unfiltered page is a
    bounded top-N plus the cron list and one outcome aggregate. If that outgrows the table, add an index on
    `(fired_at DESC, id DESC)` — not a count or an offset.
    """
    # Refused, not silently empty: `outcome` is a published closed vocabulary, so an unknown value is a
    # caller bug (`_reject`).
    if outcome is not None and outcome not in RUN_OUTCOMES:
        _reject("outcome", f"{outcome!r} is not one of {', '.join(RUN_OUTCOMES)}")
    query = (
        select(CronRun, Cron.name)
        .join(Cron, Cron.id == CronRun.cron_id)
        .order_by(CronRun.fired_at.desc(), CronRun.id.desc())
    )
    if cron is not None:
        query = query.where(CronRun.cron_id == cron)
    if outcome is not None:
        query = query.where(CronRun.outcome == outcome)
    if before is not None:
        # Refused rather than ignored, so a bad cursor never silently restarts at page 1.
        fired_at, run_id = keyset.decode_cursor(
            before, _RUNS_CURSOR_SHAPE, is_stamp=lambda name: name == "fired_at", is_uuid=lambda name: name == "id",
            status=422, detail=f"before: {before!r} is not a cursor from this endpoint's `next_before`",
        )
        query = query.where(tuple_(CronRun.fired_at, CronRun.id) < (fired_at, run_id))
    # One row more than asked, discarded — answers "is there another page?" without a COUNT.
    rows = session.execute(query.limit(limit + 1)).all()
    has_more = len(rows) > limit
    page = rows[:limit]
    # The cron picker offers EVERY cron the seam lets this caller read, fired or not — a quiet schedule is
    # exactly the one an operator looks for. Sorted here (code points, not the database collation) by name
    # with id as tiebreak: `crons.name` is not unique, so options would reshuffle.
    schedules = sorted(
        ((str(cron_id), name) for cron_id, name in session.execute(select(Cron.id, Cron.name))),
        key=lambda pair: (pair[1], pair[0]),
    )
    # The outcomes actually present, through the same join as the page, so the seam scopes both alike.
    outcomes = session.scalars(
        select(CronRun.outcome).join(Cron, Cron.id == CronRun.cron_id).distinct()
    ).all()
    # Errors since the last success and within `FAILING_STREAK_WINDOW`, per enabled cron, through the same join so
    # the seam scopes it alike.
    errors = func.count(CronRun.id)
    failing = session.execute(
        select(Cron.id, Cron.name, errors)
        .join(CronRun, CronRun.cron_id == Cron.id)
        .where(
            Cron.enabled,
            CronRun.outcome == RUN_OUTCOME_ERROR,
            CronRun.fired_at > datetime.now(timezone.utc) - FAILING_STREAK_WINDOW,
            or_(Cron.last_succeeded_at.is_(None), CronRun.fired_at > Cron.last_succeeded_at),
        )
        .group_by(Cron.id, Cron.name)
        .having(errors >= FAILING_STREAK_THRESHOLD)
    ).all()
    return FleetCronRunsPage(
        runs=[
            from_row(FleetCronRunOut, run, id=str(run.id), cron_id=str(run.cron_id), cron_name=cron_name)
            for run, cron_name in page
        ],
        limit=limit,
        next_before=(
            keyset.encode_cursor(_RUNS_CURSOR_SHAPE, [page[-1][0].fired_at, page[-1][0].id])
            if has_more and page else None
        ),
        crons=[FleetCronRef(id=cron_id, name=name) for cron_id, name in schedules],
        outcomes=sorted(outcomes),
        failing=[
            FailingCronOut(id=str(cron_id), name=name, consecutive_errors=count)
            for cron_id, name, count in sorted(failing, key=lambda row: (-row[2], row[1], str(row[0])))
        ],
        failing_threshold=FAILING_STREAK_THRESHOLD,
    )


# ── The shared writers ────────────────────────────────────────────────────────────────────────────────
# Every cron write goes through these: `/api/automations` (`app/automation_service.py`) and the agent
# surface (`routes/agent_crons.py`) each decide the activity and org, then call the writer.


def write_cron_create(
    session: Session,
    body: CronCreate,
    actor: AdminUser,
    background: BackgroundTasks,
    *,
    descriptor: cron_activities.ActivityDescriptor,
    org_id: UUID,
    created_by_task_id: Optional[int] = None,
    automation: Optional["automation_service.ParentSpec"] = None,
) -> CronDetail:
    """Create one cron from the decided activity and org on, for the agent surface (`routes/agent_crons.py`)
    and `/api/automations` (`app/automation_service.py`), so every create meets every later refusal.

    `is_system` is stamped from the descriptor, never a request field, so re-creating a deleted seeded cron
    retakes the `uq_crons_system_activity_type` slot. The descriptor and org are decided by the caller, each
    in its own handler body, so the tenant-create census test still reads
    `named_org` where the org arrives. `created_by_task_id` is the agent surface's provenance
    (`Cron.created_by_task_id`); `/api/automations` passes none. The row's parent Automation is created in the same transaction (`sync_cron`), from
    `automation` when an automation save decided it, else derived from the row.
    """
    cron = Cron(
        name=body.name,
        description=body.description,
        schedule_kind=body.schedule_kind,
        cron_expression=body.cron_expression,
        interval_seconds=body.interval_seconds,
        activity_type=body.activity_type,
        activity_params=body.activity_params,
        enabled=body.enabled,
        retention_mode=body.retention_mode,
        min_gap_seconds=body.min_gap_seconds,
        timeout_seconds=body.timeout_seconds,
        is_system=descriptor.is_system,
        org_id=org_id,
    )
    _validate(cron, frozenset(body.model_fields_set))
    # After `org_for`, never against `body.org_id`: a system create lands on the operator plane whatever
    # the caller named, and the project must be reachable from where the row actually landed.
    _project_reachable_or_reject(session, cron)
    _check_min_period(session, cron, cron.org_id)
    stamp_actor(cron, actor, created=True)
    cron.created_by_task_id = created_by_task_id
    session.add(cron)
    # A new row: no committed org (`was_org=None`); the org `org_for` just stamped decides.
    with _plane_for_write(session, cron, was_org=None, actor_org=actor.org_id):
        _flush_or_conflict(session, cron)
    automation_service.sync_cron(session, cron, automation)
    emit_console_event(session, entity="cron", id=cron.id, action="upserted", org_id=cron.org_id)
    session.commit()
    session.refresh(cron)
    _reconcile_later(background)
    return _detail_out(session, cron)


def write_cron_update(
    session: Session,
    cron: Cron,
    body: CronUpdate,
    actor: AdminUser,
    background: BackgroundTasks,
    *,
    via_task_id: Optional[int] = None,
    automation: Optional["automation_service.ParentSpec"] = None,
) -> CronDetail:
    """Patch one cron, for the agent surface (`routes/agent_crons.py`) and `/api/automations`. A system cron is
    patched the same way — `is_system` marks machinery, not a permission (`models.Cron`); the reconcile that
    follows makes an `enabled` flip real (a resume re-anchors the cadence rather than replaying the off window,
    AC3.1). `cron` must already be row-locked by the caller (`SELECT ... FOR UPDATE`): `_check_min_gap` is the
    only enforcement for a cron-expression row, and concurrent patches would each pass their own snapshot. `via_task_id` names
    the runner's task in the audit row, so a model's write is told apart from its executor's own. The parent
    Automation is kept in step before the commit (`sync_cron`), and follows the row on a move."""
    # The committed org, captured before anything moves it: `_plane_for_write` must decide on it, not on
    # the patched row, or a PATCH could carry a tenant's cron through the seam as machinery.
    was_org = cron.org_id
    # The committed flag, for the same reason: `_changed_fields` cannot see `is_system`.
    before_system = cron.is_system
    provided = frozenset(body.model_fields_set)
    # `org_id` and `activity_type` together are refused: a system activity's org follows the activity
    # (`org_for`), so one of the two instructions would be silently discarded.
    if {"org_id", "activity_type"} <= provided:
        _reject(
            "org_id",
            "an org and an activity cannot change in one request: the activity decides whether this cron "
            "is machinery, and machinery lives on the operator plane whatever org is named — send the "
            "two changes separately",
        )
    before = _apply(cron, body)
    # Fields `_validate` normalizes (it strips `name`/`description` on the merged row), recorded so a
    # normalization rewrite appears in `changed` and the audit.
    before.update({f: getattr(cron, f) for f in ("name", "description") if f not in before})
    # Where this row is going, if anywhere — a local, never an attribute. `crons` is a composite key's
    # parent, so a dirty `cron.org_id` would be written at the next autoflush (any query below) and be
    # refused by `fk_cron_runs_cron_id_org` as a 500 (`_rehome_or_conflict`).
    rehome_to: Optional[UUID] = None
    if "activity_type" in provided:
        # The same registry decision as create: both flags come from the descriptor, so the partial
        # unique index always sees machinery and never a squatter.
        was_system = cron.is_system
        descriptor = _creatable_descriptor(cron.activity_type)
        _refuse_machinery_for_a_tenant(
            actor, descriptor.is_system, f"activity {descriptor.type!r} ({descriptor.label})"
        )
        cron.is_system = descriptor.is_system
        # The org follows the flag only when the flag actually moved and only if this caller may move
        # this row (`_entitled_to_rehome`); otherwise re-sending an unchanged `activity_type` would let a
        # caller take ownership of another org's cron. A move onto or off a system activity must carry
        # the org with it; `org_for` is the one home for both directions.
        if cron.is_system != was_system:
            if not _entitled_to_rehome(was_org=was_org, actor_org=actor.org_id):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "activity_type: this cron belongs to another org, and changing whether it is "
                        "machinery would move it — which is not this caller's to do"
                    ),
                )
            # Recorded, not performed: the move goes through the same mover as an explicit `org_id`
            # patch (`_rehome_or_conflict`).
            rehome_to = org_for(is_system=cron.is_system, author_org=actor.org_id)
    _validate(cron, provided)
    # Every querying guard sits after `_validate` and inside one no-autoflush window. After `_validate`,
    # because `_apply` already set every provided field and a query would autoflush an unvalidated
    # merge (e.g. `name: null`) as a 500. No autoflush, because a valid row may still violate
    # `uq_crons_system_activity_type`, which must surface via `_flush_or_conflict` as a 409.
    with session.no_autoflush:
        if "org_id" in provided:
            # Taken straight back off the instance (see `CronUpdate.org_id`), before `_rehomed` queries.
            cron.org_id = was_org
            rehome_to = _rehomed(
                session, cron, was_org=was_org, actor_org=actor.org_id, named=body.org_id,
                operator=_is_operator(actor),
            )
        # Only when this request could have moved the answer: a cron already on an unreachable project
        # must still be disableable. Re-judged when the project, activity or org changes. The project
        # test compares against `before`, not field presence, since `activity_params` is one bag;
        # `in provided` comes first because `before` holds only sent fields
        # (`test_a_cron_already_pointing_at_an_unreachable_project_can_still_be_disabled`).
        project_moved = "activity_params" in provided and (
            _configured_project_ids(cron.activity_params)
            != _configured_project_ids(before.get("activity_params"))
        )
        #
        # Judged against the org the row lands in (`rehome_to`), not the instance's, so a cron moving to
        # its project's org is let through.
        if project_moved or provided & {"activity_type", "org_id"}:
            _project_reachable_or_reject(session, cron, org_id=rehome_to)
        # Against the org the row lands in; only when this request set the schedule or moved the org.
        if provided & _SCHEDULE_FIELDS or (rehome_to is not None and rehome_to != was_org):
            _check_min_period(session, cron, rehome_to or was_org)
        # The two guards a move alone owes; never on a request that leaves the org unchanged.
        if rehome_to is not None and rehome_to != was_org:
            _owner_in_org_or_reject(session, cron, org_id=rehome_to)
            _cron_task_in_flight_or_conflict(session, cron)
    # After `_validate`, which strips `name`/`description`, so a whitespace-only difference is not a
    # change.
    changed = _changed_fields(cron, before)
    # `_changed_fields` cannot see `is_system` (not a request field), so a restated `activity_type` whose
    # descriptor flag changed across a release would otherwise rewrite it past `_plane_for_write` and
    # `stamp_actor`. The org half is handled separately: `cron.org_id` stays `was_org` until the flush.
    if cron.is_system != before_system:
        changed.append("is_system")
    # The move is appended from the intent, since the instance never holds the target: otherwise it
    # would reach `record_write` unnamed, or be swallowed by the no-op branch. Naming the current org is
    # not a move.
    if rehome_to is not None and rehome_to != was_org:
        changed.append("org_id")
    if not changed:
        # Nothing moved: no stamp and no reconcile, so a no-op patch does not re-attribute the row.
        return _detail_out(session, cron)
    stamp_actor(cron, actor)
    # The ORM write first, carrying every patched column but `org_id` (still `was_org`), so
    # `_plane_for_write` judges the committed org; the move below carries no ORM instance.
    with _plane_for_write(session, cron, was_org=was_org, actor_org=actor.org_id):
        _flush_or_conflict(session, cron)
    # The move second, after the ORM write and before the audit row. It raises on refusal, rolling back
    # the whole request.
    if rehome_to is not None and rehome_to != was_org:
        _rehome_or_conflict(session, cron, rehome_to)
    parent = automation_service.sync_cron(session, cron, automation)
    if rehome_to is not None and rehome_to != was_org:
        # The parent lives where its child does; its attachments move with it (`rehome_row`'s children).
        rehome.rehome_row(session, "automations", parent.id, rehome_to)
        session.expire(parent, ["org_id"])
    record_write(
        session, actor, "cron_update", f"Updated cron {cron.name} ({', '.join(changed)})",
        data={"cron_id": str(cron.id), "changed": changed, **_via(via_task_id)},
    )
    emit_console_event(session, entity="cron", id=cron.id, action="upserted", org_id=cron.org_id)
    session.commit()
    session.refresh(cron)
    _reconcile_later(background)
    return _detail_out(session, cron)


def _via(task_id: Optional[int]) -> dict[str, int]:
    """The audit `data` key naming the runner's task; empty for a console write, whose row is unchanged."""
    return {} if task_id is None else {"via_task_id": task_id}


def write_cron_delete(
    session: Session,
    cron: Cron,
    actor: AdminUser,
    background: BackgroundTasks,
    *,
    via_task_id: Optional[int] = None,
) -> None:
    """Delete one cron, system rows included (decision D-5), for the agent surface (`routes/agent_crons.py`)
    and `/api/automations`; `via_task_id` as in `write_cron_update`. `cron_runs.cron_id` cascades; the DBOS
    schedule is dropped by the reconciler's orphan sweep, which keeps deletion a single write."""
    cron_id = cron.id
    name = cron.name
    record_write(
        session, actor, "cron_delete", f"Deleted cron {name}",
        data={"cron_id": str(cron.id), "activity_type": cron.activity_type,
              "is_system": cron.is_system, **_via(via_task_id)},
    )
    emit_console_event(session, entity="cron", id=cron.id, action="deleted", org_id=cron.org_id)
    # The audit row is flushed first under the full guard: `ActivityEvent` is tenant-owned and would
    # otherwise ride the escaped flush below. The console event for `entity="cron"` writes no row
    # (`app/events.py`).
    session.flush()
    # The same escape the other writes take: `_refuse_cross_org_deletes` would otherwise refuse deleting
    # an org-zero seeded cron. Explicit flush so the escape closes around this statement.
    # `was_org=cron.org_id` makes the entitlement structurally true — a delete moves nothing; D-5 covers
    # the rest. Whether a tenant's superadmin should delete machinery is deferred.
    # The parent automation goes with its last child (revision 0417), its Task-action files' rows with it; their
    # bytes are reaped after the commit, as a task delete's are.
    keys = automation_service.attachment_keys(session, cron.automation_id)
    with _plane_for_write(session, cron, was_org=cron.org_id, actor_org=actor.org_id):
        session.delete(cron)
        session.flush()
    session.commit()
    automation_service.reap_attachment_keys(session, keys)
    _reconcile_later(background)
    # `actor.id`, not the email: the audit row already attributes it, and email in logs has a different
    # retention and access model.
    log.info("cron %s (%s) deleted by admin %s", cron_id, name, actor.id)


# ── Run now ───────────────────────────────────────────────────────────────────────────────────────────
#
# The exit from `wedged`: at `SKIP_ESCALATION_THRESHOLD` a cron's health flips to `wedged` and an alarm
# goes out; Run now lets the operator fire it after fixing the cause.

# The sentences Run now can answer with, as constants so tests assert the actual words.
# The two 409s are permanent causes read off the row (`cron_runtime._effective_min_gap`, `enabled`) and
# name the field to change. A transient miss names its cause when `fire_now_sync` reports one
# (`cron_runtime.FIRE_MISS_*`); `FIRE_NOT_ENQUEUED` is the rest (a trigger error, a race on the row),
# so it gives the move — retry — only.
FIRE_ENQUEUED = "Firing enqueued — the outcome lands in Runs."
FIRE_NOT_SCHEDULED = "Not scheduled yet — the scheduler hasn't picked this cron up; retry in a minute."
FIRE_COALESCED = "A firing was enqueued a moment ago; nothing new enqueued."
FIRE_NOT_ENQUEUED = "Nothing enqueued; retry shortly."
FIRE_DISABLED = "enabled: this cron is disabled — enable it, then run it."
FIRE_NO_MIN_GAP = "min_gap_seconds: this cron has no out-of-band trigger — set a minimum gap, then run it."
_FIRE_MISS_DETAIL = {
    cron_runtime.FIRE_MISS_UNSCHEDULED: FIRE_NOT_SCHEDULED,
    cron_runtime.FIRE_MISS_COALESCED: FIRE_COALESCED,
}


def run_cron_now(cron_id: UUID, actor: AdminUser, session: Session) -> CronFireOut:
    """Fire this cron once, out of band. The operator's way out of `wedged`. Called by
    `POST /api/automations/{id}/run` (`routes/automations.py::run_automation`) for a schedule whose action
    the fire engine does not run (a system function, `noop`); that route carries the authorization.

    A firing, not a "clear the counter" button: the wedge lives only in `crons.consecutive_skips`,
    moved by real firing outcomes (`cron_runtime._move_skip_counter`), so it clears only if the cause
    is fixed.

    Two up-front 409s for permanent causes (disabled; no effective min-gap floor, so not wakeable),
    each naming the field to change. `fire_now_sync` collapses every miss into `False`, so asking the
    floor here is the only way to separate permanent from transient; what remains is a 200 naming the
    transient cause it reported (`_FIRE_MISS_DETAIL`). A race on the floor costs one retry, so no row lock.

    Nothing is written: the engine delivers the tick on its own thread and session, so no
    `_plane_for_write` escape and no reconcile.
    """
    cron = get_or_404(session, Cron, cron_id, detail=f"no cron with id {cron_id}")
    require_org_active(session, cron.org_id)  # a firing may file and ready a task (`app/org_gate.py`)
    # Unreachable for a tenant today (machinery sits on org zero, which their scope cannot read); kept so
    # the refusal does not rest on where a row happens to be filed.
    _refuse_machinery_for_a_tenant(actor, cron.is_system, f"cron {cron.name!r}")
    if not cron.enabled:
        raise HTTPException(status_code=409, detail=FIRE_DISABLED)
    # `_effective_min_gap` is the one home for "row floor, else registration's, else not wakeable";
    # the underscore means not public, not "copy it".
    if cron_runtime._effective_min_gap(cron) is None:
        raise HTTPException(status_code=409, detail=FIRE_NO_MIN_GAP)
    # Through the module, so the seam a test stubs is the one called.
    misses: list[str] = []
    fired = cron_runtime.fire_now_sync(cron.id, on_miss=misses.append)
    # `actor.id`, never the email (see `write_cron_delete`).
    log.info("cron %s: run-now by admin %s %s", cron_id, actor.id,
             "enqueued a tick" if fired else f"enqueued nothing ({', '.join(misses) or 'no cause'})")
    if fired:
        return CronFireOut(fired=True, detail=FIRE_ENQUEUED)
    return CronFireOut(fired=False, detail=_FIRE_MISS_DETAIL.get(misses[0] if misses else "", FIRE_NOT_ENQUEUED))
