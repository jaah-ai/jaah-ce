"""The one UTC clock. Modules bind it under their own `_now` name (`from app.clock import utcnow as _now`,
or `epoch as _now` where the module counts seconds) so a test can still freeze or advance one module's clock
with `monkeypatch.setattr(module, "_now", ...)`."""

import time
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(*, timespec: str = "auto") -> str:
    """`utcnow` as an ISO-8601 string, for a JSON stamp; `timespec` as `datetime.isoformat` takes it."""
    return utcnow().isoformat(timespec=timespec)


def epoch() -> float:
    """The wall clock in epoch seconds: `utcnow` for a TOTP step, a session stamp, a step deadline."""
    return time.time()


def as_utc(stamp: datetime) -> datetime:
    """`stamp` as an aware datetime; a naive one is read as UTC."""
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def utc_iso_or_none(value: Optional[datetime]) -> Optional[str]:
    """An optional moment as a JSON stamp: an aware UTC ISO-8601 string (a naive one read as UTC, `as_utc`),
    or None when it is None. `entity_shape.iso_or_none` is the pass-through projection of a column instead."""
    return None if value is None else as_utc(value).astimezone(timezone.utc).isoformat()


def iso_z(moment: datetime) -> str:
    """`moment` as `2026-10-13T02:00:00Z`: UTC, second precision — the spelling an AWS query parameter and a
    compliance manifest take."""
    return as_utc(moment).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
