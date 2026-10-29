"""THE SINGLE HOME for cron parsing, next-fire arithmetic and the interval→expression translation.

Every cron child imports this and none re-implements it: the console's "next fire" label, the
reconciler's registration and any acceptance test must AGREE, and three parsers agreeing is a
coincidence rather than a property.

ONE PARSER, AND IT IS THE ENGINE'S OWN — `dbos._croniter`, the vendored fork DBOS's schedule thread
validates with. `second_at_beginning=True` is not a preference: it decides which end of a 6-field
expression is the seconds field, and DBOS passes it. TIMEZONE IS ALWAYS THE CALLER'S AND NEVER
DEFAULTED — one control-plane-wide knob, no per-cron zone. Only interval → expression is defined; the
reverse is not a function.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

# THE ONLY PARSER IMPORT IN `apps/api/app` — see the module docstring for why it is DBOS's vendored fork
# and not the PyPI package. Private by name, deliberately: the public alternative does not exist, and
# the pin plus the import assertion in the suite are what make depending on it reviewable.
from dbos._croniter import croniter

# A RENDERER, NOT A PARSER — the distinction the module docstring's first rule turns on. This library
# only ever turns an expression into English for `describe_schedule`; it never sees an instant, never
# computes a next fire, and nothing here consults it about whether an expression is valid. Two libraries
# that both parse would be two answers to "when does this fire"; one that only prints is not a second
# opinion (see the pin's comment in `requirements.txt`).
import cron_descriptor

from app.crons import (
    MAX_CRON_EXPRESSION_CHARS,
    SCHEDULE_KIND_CRON,
    SCHEDULE_KIND_INTERVAL,
)

# The unit sizes, in seconds. Every rule below is about which steps one cron field can carry exactly.
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400

# The column is INTEGER, so this is the largest interval a row can store (whole minutes, per `_granularity`);
# above it the API refuses by name rather than letting the INSERT fail as a 500.
MAX_INTERVAL_SECONDS = (2**31 - 1) // _SECONDS_PER_MINUTE * _SECONDS_PER_MINUTE

# Interval instants are measured from here. ANY whole number of seconds is an interval: its firings are the
# multiples of N since this instant (a fixed UTC grid — every 3 hours is 00:00, 03:00, 06:00 UTC), so the
# cadence survives restarts, re-saves and DST without storing an anchor per row.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _divisors_below(limit: int) -> tuple[int, ...]:
    """Every n in 1..limit-1 that divides `limit` exactly."""
    return tuple(n for n in range(1, limit) if limit % n == 0)


# The steps a 6-field cron fires on EXACTLY, aligned to the UTC grid. WHY EXACTNESS: cron's `*/N` step
# restarts at each field boundary, so `*/45 * * * * *` fires at :00 and :45 and then gaps 15 seconds. Only a
# step that divides its field's span lands on every multiple of itself since the epoch. A day is the
# ceiling: months have unequal lengths, so no day-of-month step is exact.
_GRID_STEPS: tuple[int, ...] = (
    _divisors_below(_SECONDS_PER_MINUTE)
    + tuple(minutes * _SECONDS_PER_MINUTE for minutes in _divisors_below(_SECONDS_PER_MINUTE))
    + (_SECONDS_PER_HOUR,)
    + tuple(hours * _SECONDS_PER_HOUR for hours in _divisors_below(24) if hours > 1)
    + (_SECONDS_PER_DAY,)
)


class CronScheduleError(ValueError):
    """Base class, so a caller catches one type for "this schedule is not usable" without importing
    croniter's exception hierarchy — the module boundary this file exists to hold."""


class CronExpressionError(CronScheduleError):
    """The expression is not one this parser accepts. The message carries croniter's own complaint,
    which names the field or the column count rather than only saying "invalid"."""


class IntervalNotExpressibleError(CronScheduleError):
    """The interval is not one this module schedules: not a whole number of seconds, out of range, or finer
    than its size allows (`_granularity`). The message names the rule and the nearest accepted values."""


# A FIXED reference instant for validation-only parses. Deliberately not `now()`: whether an expression is
# usable must not depend on the clock, or a validator's failures cannot be reproduced from its input.
_VALIDATION_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)



def _next_fire(expression: str, at: datetime) -> datetime:
    """The one place an expression meets the parser: construct AND evaluate, wrapping BOTH.

    EVALUATING IS THE ACCEPTANCE TEST, not parsing. `0 0 30 2 *` is a well-formed expression naming
    February 30th: croniter builds it happily and `is_valid` says yes, then `get_next` raises
    `CroniterBadDateError` because the instant does not exist. Validating by construction alone would
    accept that row at the edge and leave every later reader — a list page rendering next-fire for N rows
    — to meet a croniter exception nothing catches. So `get_next` is inside the guard, and no caller ever
    sees a type from croniter's hierarchy.
    """
    candidate = (expression or "").strip()
    if not candidate:
        raise CronExpressionError("cron expression is empty")
    if len(candidate) > MAX_CRON_EXPRESSION_CHARS:
        raise CronExpressionError(
            f"cron expression is {len(candidate)} characters; the limit is {MAX_CRON_EXPRESSION_CHARS}"
        )
    try:
        return croniter(candidate, at, second_at_beginning=True).get_next(datetime)
    except Exception as exc:  # every croniter type, CroniterBadDateError included
        # croniter's own message names the offending field, the column count or the impossible date,
        # which "invalid cron expression" would not — so it is carried rather than replaced.
        raise CronExpressionError(f"{candidate!r} is not a usable cron expression: {exc}") from exc


def validate_cron_expression(expression: str) -> str:
    """Return `expression` stripped, or raise `CronExpressionError`.

    Acceptance is croniter's own, with `second_at_beginning=True`, so an expression this returns is one
    DBOS will also accept when the reconciler registers it. We add NO field-count rule: croniter already
    refuses anything that is not 5, 6 or 7 columns, and a stricter rule here would refuse expressions the
    engine would happily run. What we DO add is that the expression must have a next fire — see
    `_next_fire`.
    """
    _next_fire(expression, _VALIDATION_EPOCH)
    return expression.strip()


def _granularity(interval_seconds: int) -> int:
    """The unit an interval must be a whole number of: any seconds below an hour, whole minutes from an hour
    up (the operator's call). WHY: the registered step is the largest grid step dividing N, so
    3601s would register `*/1` and wake the engine every second, forever, to fire once an hour. From an hour
    up the engine therefore wakes at most once a minute; below it, at most once a second, as a sub-minute
    interval always could."""
    return 1 if interval_seconds < _SECONDS_PER_HOUR else _SECONDS_PER_MINUTE


def _checked_interval(interval_seconds: int) -> int:
    """`interval_seconds`, or `IntervalNotExpressibleError`. NO `int()` cast: `True` is an int equal to 1
    (a one-second cadence) and `60.9` would round — request bodies reach here."""
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        raise IntervalNotExpressibleError(
            f"interval must be a whole number of seconds, got {interval_seconds!r}"
        )
    if not 1 <= interval_seconds <= MAX_INTERVAL_SECONDS:
        raise IntervalNotExpressibleError(
            f"interval must be between 1 and {MAX_INTERVAL_SECONDS} seconds, got {interval_seconds}"
        )
    unit = _granularity(interval_seconds)
    if interval_seconds % unit:
        lower = interval_seconds - interval_seconds % unit
        raise IntervalNotExpressibleError(
            f"an interval of an hour or more must be whole minutes; {interval_seconds}s is not — the nearest are "
            f"{lower}s and {lower + unit}s"
        )
    return interval_seconds


def cron_expression_for_interval(interval_seconds: int) -> str:
    """The 6-field expression the engine REGISTERS for an interval row: the largest grid step dividing it.

    When the interval is itself a grid step (10s, 5 minutes, 3 hours, a day) the expression IS its cadence.
    Otherwise it fires on a finer step that lands on every multiple of the interval, and the ticks between
    are dropped by `interval_tick_is_due` before they run — so 7 hours registers hourly and runs every
    seventh hour, 45 seconds registers `*/15` and runs every third. The largest dividing step keeps those
    dropped ticks as few as the cron syntax allows.
    """
    interval = _checked_interval(interval_seconds)
    step = max(candidate for candidate in _GRID_STEPS if interval % candidate == 0)
    if step < _SECONDS_PER_MINUTE:
        return f"*/{step} * * * * *"
    if step < _SECONDS_PER_HOUR:
        return f"0 */{step // _SECONDS_PER_MINUTE} * * * *"
    if step == _SECONDS_PER_HOUR:
        return "0 0 * * * *"  # `0 */60 * * * *` is not a legal minute step
    if step < _SECONDS_PER_DAY:
        return f"0 0 */{step // _SECONDS_PER_HOUR} * * *"
    return "0 0 0 * * *"


def _seconds_since_epoch(at: datetime) -> int:
    return (at - _EPOCH) // timedelta(seconds=1)


def interval_tick_is_due(interval_seconds: int, scheduled_at: datetime) -> bool:
    """Whether a tick of the registered expression falls on the interval's UTC grid.

    `scheduled_at` is the instant DBOS named; it emits aware UTC, and a naive one is read as UTC rather
    than as this process's zone, which is the only zone DBOS could have meant.
    """
    interval = _checked_interval(interval_seconds)
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    return _seconds_since_epoch(scheduled_at) % interval == 0


def _next_interval_fire(interval_seconds: int, after: datetime) -> datetime:
    """The first multiple of the interval since the epoch strictly after `after`, in UTC."""
    interval = _checked_interval(interval_seconds)
    _require_aware(after)
    multiples = _seconds_since_epoch(after) // interval + 1
    return _EPOCH + timedelta(seconds=multiples * interval)


_UNIT_NAMES = (
    (_SECONDS_PER_DAY, "day"),
    (_SECONDS_PER_HOUR, "hour"),
    (_SECONDS_PER_MINUTE, "minute"),
    (1, "second"),
)


def _describe_interval(interval_seconds: int) -> str:
    """"Every 7 hours" — in the largest unit dividing it exactly, so 5400s reads "Every 90 minutes"."""
    interval = _checked_interval(interval_seconds)
    size, name = next((size, name) for size, name in _UNIT_NAMES if interval % size == 0)
    count = interval // size
    return f"Every {name}" if count == 1 else f"Every {count} {name}s"


def _require_aware(after: datetime) -> None:
    if after.tzinfo is None or after.tzinfo.utcoffset(after) is None:
        raise CronScheduleError(
            "next_fire_at needs a timezone-aware `after`; a naive datetime has no defined instant"
        )


def next_fire_at(expression: str, after: datetime, tz: tzinfo) -> datetime:
    """The first firing of `expression` strictly after `after`, evaluated in `tz`.

    `after` MUST be timezone-aware. A naive datetime is refused rather than assumed: it would mean
    "whatever zone this process happens to be in", which is the class of bug that shows up twice a year
    and never in a test.

    The instant is converted INTO `tz` before parsing, so day-of-week and hour fields mean what they
    mean in the operator's zone. The return value is aware and expressed in `tz`; a caller storing it
    in a `timestamptz` column does not need to convert, and one rendering it should not.
    """
    _require_aware(after)
    return _next_fire(expression, after.astimezone(tz))


def _row_schedule(
    schedule_kind: str,
    cron_expression: str | None,
    interval_seconds: int | None,
) -> tuple[str | None, int | None]:
    """`(expression, None)` for a `cron` row, `(None, interval)` for an `interval` row — validated.

    SINGLE-HOMED because more than one reader of a stored row needs it and they must not disagree:
    `next_fire_for_row` turns it into an instant, `describe_schedule` turns it into a sentence. The
    row-level refusals live here for the same reason: an unknown discriminator, either kind missing the
    column its discriminator promises, and an interval that is not a whole number of seconds in range. A
    second reader adding its own softer guard is how "the API accepted it and the reconciler could not
    register it" happens.
    """
    if schedule_kind == SCHEDULE_KIND_CRON:
        if not cron_expression:
            raise CronScheduleError("a 'cron' row must carry a cron_expression")
        return cron_expression, None
    if schedule_kind == SCHEDULE_KIND_INTERVAL:
        if interval_seconds is None:
            raise CronScheduleError("an 'interval' row must carry interval_seconds")
        return None, _checked_interval(interval_seconds)
    raise CronScheduleError(
        f"unknown schedule_kind {schedule_kind!r}; expected "
        f"{SCHEDULE_KIND_CRON!r} or {SCHEDULE_KIND_INTERVAL!r}"
    )


def next_fire_for_row(
    schedule_kind: str,
    cron_expression: str | None,
    interval_seconds: int | None,
    after: datetime,
    tz: tzinfo,
) -> datetime:
    """Next fire for a `crons` ROW, whichever way it declares its schedule.

    The one entry point every reader of a stored row should use, so "an interval row fires on its UTC grid"
    is stated once here rather than re-derived by each caller. `tz` applies to a `cron` row ONLY. The
    argument shape mirrors the columns rather than taking the ORM object, so a raw-SQL reader and a route
    serializer call the same function.
    """
    # AN INTERVAL ROW IS ALWAYS UTC, NEVER THE CALLER'S ZONE. An interval is a DURATION; evaluated in
    # wall-clock time a DST fall-back visits the repeated local hour once and opens an hour-long hole. Only
    # a `cron` row means "06:30 local". The grid is computed directly rather than by walking the
    # registered expression, whose in-between ticks `interval_tick_is_due` drops.
    expression, interval = _row_schedule(schedule_kind, cron_expression, interval_seconds)
    if interval is not None:
        return _next_interval_fire(interval, after)
    return next_fire_at(expression, after, tz)


def describe_schedule(
    schedule_kind: str,
    cron_expression: str | None,
    interval_seconds: int | None,
) -> str:
    """A human-readable sentence for a `crons` ROW — "Every 5 minutes", "At 06:30 AM, only on Monday".

    What the console's cron list and detail pages print BESIDE the raw expression, because `15,45 9-17 *
    * 1-5` is not a thing an operator can check at a glance. The argument shape mirrors
    `next_fire_for_row`, so the sentence and the instant beside it are computed from the same row. An
    `interval` row is phrased from its own number ("Every 7 hours"), never from the registered expression,
    which may be a finer step whose extra ticks are dropped.
    """
    # A GENUINELY UNUSABLE ROW STILL RAISES, with the same types `next_fire_for_row` raises: an unknown
    # kind, a `cron` row carrying no expression, an out-of-range interval. Softening them here would make
    # this the one reader that accepts a row nothing can run.
    expression, interval = _row_schedule(schedule_kind, cron_expression, interval_seconds)
    if interval is not None:
        return _describe_interval(interval)
    try:
        # `locale_code` pinned: cron-descriptor>=2 otherwise reads the SERVER's locale, so the same row
        # would read "06:30 AM" on one host and "06:30" on another.
        return cron_descriptor.ExpressionDescriptor(
            expression, cron_descriptor.Options(locale_code="en_US")
        ).get_description()
    except Exception:  # noqa: BLE001 — a cosmetic label must not be able to fail a page
        # Swallowed ON PURPOSE and only here: the renderer is a separate library with its own idea of which
        # expressions it can phrase, and one row's label must not 500 a list page. The honest degraded
        # answer is the raw expression the page prints beside it anyway.
        return expression


# ── The compact phrase ────────────────────────────────────────────────────────────────────────────────

# Three-letter day names indexed by cron's own numbering (0 = Sunday; 7 is Sunday again and is folded to 0
# before indexing). The names a day-of-week field may spell are matched case-insensitively against these.
_DAY_NAMES: tuple[str, ...] = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_DAY_BY_NAME: dict[str, int] = {name.lower(): number for number, name in enumerate(_DAY_NAMES)}
_WEEKDAYS = frozenset({1, 2, 3, 4, 5})
_WEEKEND = frozenset({0, 6})
_EVERY_DAY = frozenset(range(7))
_HOURS_PER_DAY = 24
_MINUTES_PER_HOUR = 60
# The compact unit per `_UNIT_NAMES`' sizes: a day is "Daily", an hour "Hourly", the rest "Every N<unit>".
_SHORT_UNITS = (
    (_SECONDS_PER_DAY, "d", "Daily"),
    (_SECONDS_PER_HOUR, "h", "Hourly"),
    (_SECONDS_PER_MINUTE, "m", None),
    (1, "s", None),
)
# Up to this many irregular clock times are spelled out ("Daily 06:00, 12:00, 18:00"); more collapse to a
# count, because a list row has one line and four times already overrun it.
_MAX_LISTED_TIMES = 3
_STEP = re.compile(r"^\*/(\d+)$")
_FIXED = re.compile(r"^\d+$")


def describe_schedule_short(
    schedule_kind: str,
    cron_expression: str | None,
    interval_seconds: int | None,
) -> str | None:
    """A COMPACT phrase for a `crons` ROW — "Daily 09:00", "Every 3h", "Weekdays 08:30" — or None.

    What a LIST ROW prints, where `describe_schedule`'s sentence ("At 12:00 AM, 03:00 AM, 06:00 AM, …
    and 09:00 PM") is right but too long to scan a column of. Same argument shape and the same
    `_row_schedule` reading as `describe_schedule`: an interval row is phrased from its own number
    ("Every 7h"), never from the finer expression the engine registers for it, so the phrase can never
    disagree with the sentence beside it.

    NONE MEANS "NO SHORT WORDING EXISTS", and the caller shows the sentence instead. Only a REGULAR
    expression has one — a cadence, one or a few clock times a day, named days, one day a month. An
    irregular one (`15,45 9-17 * * 1-5`, a month set, both day fields set, `L`/`W`/`#`) has no phrase
    that is both short and true, and a short lie is worse than a long truth. 24-hour clock, zero-padded,
    NO ZONE: the sentence it falls back to, and the hover beside it, carry the zone.

    A ROW THAT IS NOT A SCHEDULE STILL RAISES, with `describe_schedule`'s types — `_row_schedule` is
    outside the guard for the same reason it is there. Everything after it is cosmetic and wrapped, so a
    field shape this rule set never anticipated degrades to None and not to a list page that 500s.
    """
    expression, interval = _row_schedule(schedule_kind, cron_expression, interval_seconds)
    if interval is not None:
        return _short_interval(interval)
    try:
        return _short_phrase(expression.split())
    except Exception:  # noqa: BLE001 — a cosmetic label must not be able to fail a page
        return None


def _short_interval(interval: int) -> str:
    """`_describe_interval`'s unit choice, compact: a whole day is "Daily", a whole hour "Hourly", else
    "Every N<unit>" in the largest unit dividing it (5400s is "Every 90m")."""
    size, unit, whole = next((size, unit, whole) for size, unit, whole in _SHORT_UNITS if interval % size == 0)
    count = interval // size
    return whole if count == 1 and whole else f"Every {count}{unit}"


def _short_phrase(fields: list[str]) -> str | None:
    if len(fields) == 6:
        # A 6-field expression (what `cron_expression_for_interval` writes) leads with seconds. A seconds
        # step is the sub-minute cadence; seconds fixed at 0 is just the five-field case below, and any
        # other fixed second has no phrase (`30 0 9 * * *` fires at 09:00:30, which "Daily 09:00" hides).
        seconds, fields = fields[0], fields[1:]
        step = _step_of(seconds, _SECONDS_PER_MINUTE)
        if step is not None:
            return f"Every {step}s" if all(field == "*" for field in fields) else None
        if _fixed(seconds, 59) != 0:
            return None
    if len(fields) != 5:
        return None
    minute_field, hour_field, dom, month, dow = fields
    if month != "*":
        return None
    if dom == "*" and dow == "*":
        return _daily_phrase(minute_field, hour_field)
    minute, hour = _fixed(minute_field, 59), _fixed(hour_field, 23)
    if minute is None or hour is None:
        return None
    time = f"{hour:02d}:{minute:02d}"
    if dom == "*":
        days = _days_of(dow)
        if days is None:
            return None
        if days == _WEEKDAYS:
            return f"Weekdays {time}"
        if days == _WEEKEND:
            return f"Weekends {time}"
        if days == _EVERY_DAY:
            return f"Daily {time}"
        return f"{', '.join(_DAY_NAMES[day] for day in sorted(days))} {time}"
    if dow == "*":
        day_of_month = _fixed(dom, 31)
        if day_of_month is None or day_of_month == 0:
            return None
        return f"Monthly {_ordinal(day_of_month)} {time}"
    return None


def _daily_phrase(minute_field: str, hour_field: str) -> str | None:
    """The `* * *` day half: a cadence, or one or more clock times every day."""
    if minute_field == "*":
        return "Every minute" if hour_field == "*" else None
    minute_step = _step_of(minute_field, _MINUTES_PER_HOUR)
    if minute_step is not None:
        return f"Every {minute_step}m" if hour_field == "*" else None
    minute = _fixed(minute_field, 59)
    if minute is None:
        return None
    past = f" at :{minute:02d}" if minute else ""
    if hour_field == "*":
        return f"Hourly{past}"
    hour_step = _step_of(hour_field, _HOURS_PER_DAY)
    if hour_step is not None:
        return f"Every {hour_step}h{past}"
    hours = _hours_of(hour_field)
    if hours is None:
        return None
    if len(hours) == 1:
        return f"Daily {hours[0]:02d}:{minute:02d}"
    stride = _stride_from_midnight(hours)
    if stride is not None:
        return f"Every {stride}h{past}"
    if len(hours) <= _MAX_LISTED_TIMES:
        return "Daily " + ", ".join(f"{hour:02d}:{minute:02d}" for hour in hours)
    return f"{len(hours)}×/day"


def _stride_from_midnight(hours: list[int]) -> int | None:
    """`0,3,6,…,21` IS "Every 3h": a list that starts at midnight, steps evenly and fills the day is the
    same schedule as `*/3`, and an operator who typed it out should read the cadence, not eight times."""
    stride = hours[1] - hours[0]
    if hours[0] != 0 or stride == 0 or _HOURS_PER_DAY % stride:
        return None
    if hours != list(range(0, _HOURS_PER_DAY, stride)):
        return None
    return stride


def _step_of(field: str, cycle: int) -> int | None:
    """A `*/N` field's N — only when N divides the field's cycle. `*/7 * * * *` is NOT "Every 7m": it fires at
    :56 and then :00, a 4-minute gap at the top of every hour, and the phrase would hide it (the hour list's
    `_stride_from_midnight` applies the same rule)."""
    match = _STEP.match(field)
    step = int(match.group(1)) if match else 0
    return step if step and cycle % step == 0 else None


def _fixed(field: str, limit: int) -> int | None:
    """The field as one number within `0..limit`, or None for any other shape (a list, a range, a step)."""
    if not _FIXED.match(field):
        return None
    value = int(field)
    return value if value <= limit else None


def _hours_of(field: str) -> list[int] | None:
    """A comma list of fixed hours, ascending and de-duplicated, or None when any item is not a fixed hour."""
    hours: set[int] = set()
    for item in field.split(","):
        hour = _fixed(item, 23)
        if hour is None:
            return None
        hours.add(hour)
    return sorted(hours)


def _day_number(token: str) -> int | None:
    """One day-of-week token as `0..6`: a number (7 folds to Sunday) or a name, in any case."""
    if _FIXED.match(token):
        number = int(token)
        return number % 7 if number <= 7 else None
    return _DAY_BY_NAME.get(token.lower())


def _days_of(field: str) -> frozenset[int] | None:
    """The day-of-week field as a set of `0..6`, from singles and ranges in either spelling; None for a
    shape with no plain reading (`#`, `L`, a step, a reversed range)."""
    days: set[int] = set()
    for item in field.split(","):
        first, dash, last = item.partition("-")
        start = _day_number(first)
        if start is None:
            return None
        if not dash:
            days.add(start)
            continue
        end = _day_number(last)
        if end is None:
            return None
        # A range is read on cron's numbering BEFORE folding, so `6-7` is Saturday and Sunday — which is
        # why `_day_number` cannot fold 7 for us here: folded, the range would read backwards.
        start_raw = 7 if first == "7" else start
        end_raw = 7 if last == "7" else end
        if end_raw < start_raw:
            return None
        days.update(day % 7 for day in range(start_raw, end_raw + 1))
    return frozenset(days)


def _ordinal(day: int) -> str:
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


# TWENTY, AND NOT TWO. An irregular expression samples differently depending where in its cycle you
# start: `15,45 9-17 * * 1-5` gaps 30 minutes between its two firings inside an hour, and 15.5 hours
# between 17:45 and the next morning's 09:15. A two-occurrence probe would therefore accept or reject the
# SAME `min_gap_seconds` depending on the minute Save was pressed, and a validator whose verdict depends
# on the clock is not a validator.

# Twenty firings (nineteen gaps) covers a full cycle of every expression this feature admits, which makes
# the answer a property of the SCHEDULE rather than of when it was asked. The suite pins exactly that:
# that expression answers 1800.0 sampled from a weekday morning, a Friday afternoon and a Saturday alike,
# while a two-occurrence probe from the Friday one answers 228600.0.
MIN_GAP_OCCURRENCES: int = 20


def min_gap_over_next_occurrences(
    schedule_kind: str,
    cron_expression: str | None,
    interval_seconds: int | None,
    after: datetime,
    tz: tzinfo,
    occurrences: int = MIN_GAP_OCCURRENCES,
) -> float:
    """The SMALLEST gap, in seconds, between consecutive firings across the next `occurrences` firings.

    WHY IT EXISTS: `min_gap_seconds` is a floor on how often a cron may actually do work, and the
    database can only half-enforce it. `ck_crons_min_gap_below_interval` (revision 0137) compares it
    against `interval_seconds` — a column a `cron`-expression row does not have and cannot have, since
    "the gap" of an expression is not a stored number but something you must walk the schedule to learn,
    and a CHECK cannot walk a schedule. So for the majority case THE API IS THE ONLY ENFORCEMENT POINT.

    `occurrences` DEFAULTS TO TWENTY and that number is the whole point — see `MIN_GAP_OCCURRENCES`.
    Returns a float of seconds; 20 firings means 19 gaps.
    """
    # ONE PARSER STILL. The walk is repeated calls to `next_fire_for_row` with the previous firing as
    # `after`; nothing is opened here and the interval→expression translation is not re-implemented, so
    # an interval row's UTC rule and a cron row's zone rule hold by construction rather than by a second
    # copy somebody has to keep in step. `after` must be aware — `next_fire_at` already refuses a naive
    # one, and a second guard would be a second place for that rule to be stated differently.
    if occurrences < 2:
        raise ValueError(
            f"occurrences must be at least 2 — one gap needs two firings — got {occurrences}"
        )

    # The first gap is computed outside the loop so `smallest` is a real number from the start rather
    # than a sentinel every later line has to re-check.
    previous = next_fire_for_row(schedule_kind, cron_expression, interval_seconds, after, tz)
    following = next_fire_for_row(schedule_kind, cron_expression, interval_seconds, previous, tz)
    smallest = (following - previous).total_seconds()

    for _ in range(occurrences - 2):
        previous = following
        following = next_fire_for_row(schedule_kind, cron_expression, interval_seconds, previous, tz)
        smallest = min(smallest, (following - previous).total_seconds())
    return smallest
