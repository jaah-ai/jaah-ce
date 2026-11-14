"""The one UTC clock. Modules bind it under their own `_now` name (`from app.clock import utcnow as _now`,
or `epoch as _now` where the module counts seconds) so a test can still freeze or advance one module's clock
with `monkeypatch.setattr(module, "_now", ...)`."""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, TypeVar, overload

_Span = TypeVar("_Span", int, float, timedelta)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(*, timespec: str = "auto") -> str:
    """`utcnow` as an ISO-8601 string, for a JSON stamp; `timespec` as `datetime.isoformat` takes it."""
    return utcnow().isoformat(timespec=timespec)


def epoch() -> float:
    """The wall clock in epoch seconds: `utcnow` for a TOTP step, a session stamp, a step deadline."""
    return time.time()


def from_epoch_ms(epoch_ms: float) -> datetime:
    """An epoch-millisecond stamp (DBOS's `created_at`, Gmail's `internalDate`, a journal entry's clock, a vendor
    CLI's `expiry_date`) as an aware UTC datetime — the ONE `/ 1000` in the tree. Raises as `datetime.fromtimestamp`
    does (`OverflowError`, `OSError`, `ValueError`); a caller that tolerates a bad field takes `epoch_ms_or_none`."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)


@overload
def to_epoch_ms(moment: datetime) -> int: ...
@overload
def to_epoch_ms(moment: None) -> None: ...
def to_epoch_ms(moment: Optional[datetime]) -> Optional[int]:
    """`moment` as an epoch-millisecond stamp (a CloudWatch query bound, a vendor CLI's `expiry_date`, an EMF
    event, a retention cutoff), a naive one read as UTC (`as_utc`) — `from_epoch_ms`'s inverse. None stays None, so an optional bound needs no wrapper."""
    if moment is None:
        return None
    return int(as_utc(moment).timestamp() * 1000)


def epoch_ms_or_none(value: Any) -> Optional[datetime]:
    """`from_epoch_ms` of a number or a digit string, else None: absent, a bool (an `int` to Python), text that is
    not all digits, or a stamp the platform cannot represent. The tolerant read — a bad field costs one timestamp,
    never the page or the credential."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and not (value.strip().isascii() and value.strip().isdigit()):
        return None  # `isdigit` alone admits `²`, which `int` refuses
    try:
        return from_epoch_ms(int(value) if isinstance(value, str) else value)
    except (OverflowError, OSError, ValueError):  # ValueError: a digit string past `int`'s length limit
        return None


def capped_doubling(attempt: int, base: _Span, cap: _Span) -> _Span:
    """The ONE backoff curve: the wait before retry `attempt` (counted from 0) is `base`, doubled per attempt,
    never above `cap`. A negative attempt reads as 0 — the first wait, never a fraction of it or a zero that
    hot-loops; the exponent stops at 32, past which every cap is already reached and a float `base` would
    overflow. Each caller's own count (from 0 or from 1, what a count of zero means) is its one line around this.
    A `timedelta` base and cap give a `timedelta` wait (the project-memory distiller's)."""
    return min(cap, base * 2 ** min(32, max(0, attempt)))


@overload
def as_utc(stamp: datetime) -> datetime: ...
@overload
def as_utc(stamp: None) -> None: ...
def as_utc(stamp: Optional[datetime]) -> Optional[datetime]:
    """`stamp` as an aware datetime; a naive one is read as UTC (Postgres hands back aware values; a wire
    field may not). None stays None, so an optional column needs no wrapper."""
    if stamp is None:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def to_utc(stamp: datetime) -> datetime:
    """`as_utc`, then IN UTC: an aware stamp from another zone is converted, so a date read off it (an evidence
    file's day, a daily window) is the UTC day, not the sender's."""
    return as_utc(stamp).astimezone(timezone.utc)


def utc_iso_or_none(value: Optional[datetime]) -> Optional[str]:
    """An optional moment as a JSON stamp: an aware UTC ISO-8601 string (a naive one read as UTC, `as_utc`),
    or None when it is None. `entity_shape.iso_or_none` is the pass-through projection of a column instead."""
    return None if value is None else as_utc(value).astimezone(timezone.utc).isoformat()


def iso_z(moment: datetime) -> str:
    """`moment` as `2026-10-13T02:00:00Z`: UTC, second precision — the spelling an AWS query parameter and a
    compliance manifest take."""
    return utc_iso_z(moment, timespec="seconds")


def utc_iso_z(moment: datetime, *, timespec: str = "auto") -> str:
    """`moment` as an aware UTC ISO-8601 stamp ending in `Z` (a naive one read as UTC): the JSON spelling of
    `exec_dashboard`, session events and the efficiency bundle. `timespec` as `datetime.isoformat` takes it
    (`auto`: microseconds when present); `iso_z` is the fixed second-precision twin."""
    return as_utc(moment).astimezone(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_utc(value: object) -> Optional[datetime]:
    """An ISO-8601 string as an aware UTC datetime; None for anything else (a wire field that may be absent or odd)."""
    if not isinstance(value, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def jsonable(value: Any) -> Any:
    """A DB value as JSON carries it: an aware ISO stamp for a datetime, the JSON scalars and containers as they
    are, `str()` for the rest (UUIDs). The one shape every security-evidence export writes."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    return str(value)
