"""Deploy freezes: whether a release may start now, and until when it may not. Pure: stdlib only.

A one-off freeze (`repeat = never`) is the window [starts_at, ends_at). A repeating one repeats the window's wall-clock
times every day, week or month in its IANA `timezone` (computed on the wall clock, so a DST change keeps "Friday 18:00
→ Monday 08:00" at those local times); its window must be shorter than its period (`PERIOD_LIMIT`). A monthly one
starts on the same day of every month, and on the month's last day in a month that lacks that day (the 31st → 30 April,
28 or 29 February).

Which stages a freeze covers, and who it lets through, are `Window`'s other fields, judged by
`app/releases/service.py::freeze_on`; this module answers only "when".
"""
from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NEVER, DAILY, WEEKLY, MONTHLY = "never", "daily", "weekly", "monthly"
REPEATS = (NEVER, DAILY, WEEKLY, MONTHLY)
WEEK = timedelta(days=7)
#: A repeating window must be shorter than this (R7.2): a day, a week, 28 days (the shortest month).
PERIOD_LIMIT = {DAILY: timedelta(days=1), WEEKLY: WEEK, MONTHLY: timedelta(days=28)}
_STEP = {DAILY: timedelta(days=1), WEEKLY: WEEK}


@dataclass(frozen=True)
class Window:
    """One freeze. `weekly` is the old spelling of `repeat = weekly`; `repeat` (when given) wins.
    `scope_stage_ids` None covers every stage after the integration stage."""

    starts_at: datetime
    ends_at: datetime
    weekly: bool
    timezone: str
    repeat: Optional[str] = None
    name: str = ""
    freeze_id: Optional[UUID] = None
    scope_stage_ids: Optional[frozenset[UUID]] = None
    let_pressed: bool = False
    let_queued: bool = True

    @property
    def repeats(self) -> str:
        if self.repeat is not None and self.repeat != NEVER:
            return self.repeat
        return WEEKLY if self.weekly else NEVER


def zone(name: str) -> ZoneInfo:
    """The IANA zone `name`; `ValueError` for one that is not."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {name!r}") from exc


def too_long(repeat: str, length: timedelta) -> bool:
    """Whether a window of `length` is not shorter than its repeat's period (a one-off may be any length)."""
    limit = PERIOD_LIMIT.get(repeat)
    return limit is not None and length >= limit


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _month_start(start_local: datetime, year: int, month: int) -> datetime:
    """The monthly occurrence of `start_local` in (year, month): same day, clamped to the month's last day."""
    return start_local.replace(year=year, month=month, day=min(start_local.day, _last_day(year, month)))


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + n
    return index // 12, index % 12 + 1


def _local(window: Window) -> tuple[ZoneInfo, datetime, timedelta]:
    tz = zone(window.timezone)
    start_local = window.starts_at.astimezone(tz).replace(tzinfo=None)
    length = window.ends_at.astimezone(tz).replace(tzinfo=None) - start_local
    return tz, start_local, length


def _utc(local: datetime, tz: ZoneInfo) -> datetime:
    return local.replace(tzinfo=tz).astimezone(timezone.utc)


def occurrence_covering(window: Window, now: datetime) -> Optional[tuple[datetime, datetime]]:
    """The UTC bounds of the occurrence of `window` covering `now`, or None when none covers it."""
    repeat = window.repeats
    if repeat == NEVER:
        return (window.starts_at, window.ends_at) if window.starts_at <= now < window.ends_at else None
    tz, start_local, length = _local(window)
    if not timedelta(0) < length or too_long(repeat, length):
        return None
    now_local = now.astimezone(tz).replace(tzinfo=None)
    if now_local < start_local:
        return None
    if repeat in _STEP:
        step = _STEP[repeat]
        # The latest start at or before now, on the wall clock.
        candidates = [start_local + ((now_local - start_local) // step) * step]
    else:
        # A window shorter than 28 days that covers now started this month or the one before.
        candidates = [_month_start(start_local, *_add_months(now_local.year, now_local.month, n)) for n in (0, -1)]
    for occurrence in candidates:
        if start_local <= occurrence <= now_local < occurrence + length:
            return _utc(occurrence, tz), _utc(occurrence + length, tz)
    return None


def _occurrence_end(window: Window, now: datetime) -> Optional[datetime]:
    """When the occurrence of `window` covering `now` ends, or None when none covers it."""
    covering = occurrence_covering(window, now)
    return covering[1] if covering is not None else None


def _next_occurrence_start(window: Window, now: datetime) -> Optional[datetime]:
    """The first start of `window` strictly after `now` (UTC), or None for a one-off already begun."""
    repeat = window.repeats
    if repeat == NEVER:
        return window.starts_at if window.starts_at > now else None
    tz, start_local, length = _local(window)
    if not timedelta(0) < length or too_long(repeat, length):
        return None
    now_local = now.astimezone(tz).replace(tzinfo=None)
    if now_local < start_local:
        return window.starts_at
    if repeat in _STEP:
        step = _STEP[repeat]
        return _utc(start_local + ((now_local - start_local) // step + 1) * step, tz)
    for n in (0, 1):
        occurrence = _month_start(start_local, *_add_months(now_local.year, now_local.month, n))
        if occurrence > now_local:
            return _utc(occurrence, tz)
    return None


def next_start(windows: Iterable[Window], now: datetime,
               horizon: timedelta = WEEK) -> Optional[tuple[datetime, Window]]:
    """The soonest freeze start after `now` within `horizon` ("Freeze starts Fri 18:00"), with its window."""
    best: Optional[tuple[datetime, Window]] = None
    for window in windows:
        start = _next_occurrence_start(window, now)
        if start is not None and start <= now + horizon and (best is None or start < best[0]):
            best = (start, window)
    return best


def frozen_until(windows: Iterable[Window], now: datetime) -> Optional[datetime]:
    """When the freeze covering `now` lifts — following overlapping and back-to-back windows — or None."""
    windows = list(windows)
    until: Optional[datetime] = None
    at = now
    for _ in range(len(windows) + 1):
        ends = [end for w in windows if (end := _occurrence_end(w, at)) is not None]
        if not ends:
            break
        until = max(ends)
        at = until
    return until
