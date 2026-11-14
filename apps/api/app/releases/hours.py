"""A step's allowed hours (R5): the weekdays and daily wall-clock span in an IANA zone when the step may run. Pure:
stdlib only.

`to` earlier than `from` is an overnight span: Friday 22:00 → 06:00 runs into Saturday morning, and belongs to Friday
(the day it starts). Times are on the wall clock, as freezes are (`app/releases/freezes.py`), so a DST change keeps
09:00 at 09:00 local.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from app.releases.freezes import zone

#: 0 = Monday … 6 = Sunday (`datetime.weekday`).
DAYS = range(7)


class HoursError(ValueError):
    """An allowed-hours value that is not one; `field` names the input (`days`, `from`, `to`, `timezone`)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class Hours:
    days: frozenset[int]
    start: time
    end: time
    tz: str

    def out(self) -> dict[str, Any]:
        """The stored and wire shape (`release_stages.allowed_hours`)."""
        return {"days": sorted(self.days), "from": self.start.strftime("%H:%M"), "to": self.end.strftime("%H:%M"),
                "timezone": self.tz}


def _clock(value: Any, field: str) -> time:
    text = str(value or "")
    try:
        parsed = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        raise HoursError(field, f"{field} is a time like 09:00.") from None
    if len(text) != 5:
        raise HoursError(field, f"{field} is a time like 09:00.")
    return parsed


def parse(value: Any) -> Hours:
    """`{"days": [0..6], "from": "HH:MM", "to": "HH:MM", "timezone": IANA}` as `Hours`; `HoursError` naming the
    field for anything else."""
    if not isinstance(value, dict):
        raise HoursError("allowed_hours", "Allowed hours need days, a start, an end and a time zone.")
    days = value.get("days")
    if not isinstance(days, list) or not days or any(
            isinstance(d, bool) or not isinstance(d, int) or d not in DAYS for d in days):
        raise HoursError("days", "Pick at least one day.")
    start, end = _clock(value.get("from"), "from"), _clock(value.get("to"), "to")
    if start == end:
        raise HoursError("to", "The end must differ from the start.")
    tz = str(value.get("timezone") or "")
    try:
        zone(tz)
    except ValueError:
        raise HoursError("timezone", "Pick a known time zone.") from None
    return Hours(frozenset(days), start, end, tz)


def _at(day: date, clock: time, tz_name: str) -> datetime:
    return datetime.combine(day, clock).replace(tzinfo=zone(tz_name)).astimezone(timezone.utc)


def allowed(hours: Hours, now: datetime) -> bool:
    """Whether `now` falls inside the allowed hours."""
    local = now.astimezone(zone(hours.tz)).replace(tzinfo=None)
    clock, weekday = local.time(), local.weekday()
    if hours.start < hours.end:
        return weekday in hours.days and hours.start <= clock < hours.end
    # Overnight: today's span from `start`, or yesterday's span still running until `end`.
    return (weekday in hours.days and clock >= hours.start) or ((weekday - 1) % 7 in hours.days and clock < hours.end)


def next_allowed(hours: Optional[Hours], now: datetime) -> Optional[datetime]:
    """None when the step may run now (or has no allowed hours); else when it next may (UTC)."""
    if hours is None or allowed(hours, now):
        return None
    today = now.astimezone(zone(hours.tz)).date()
    for offset in range(8):
        day = today + timedelta(days=offset)
        if day.weekday() not in hours.days:
            continue
        start = _at(day, hours.start, hours.tz)
        if not allowed(hours, start):
            # The start falls in a spring-forward gap: the span opens at the first instant after it.
            start = datetime.combine(day, hours.start).replace(tzinfo=zone(hours.tz), fold=1) \
                .astimezone(timezone.utc)
        if start > now:
            return start
    return None  # unreachable: a non-empty day set opens within eight days
