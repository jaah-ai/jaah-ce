"""The security-event archive: each UTC day's security events, exported once to the Object Lock bucket
(SOC 2 G-16, PRD 5.4).

WHAT: every `activity_events` row whose kind is in `app/activity_kinds.py::SECURITY_EVENT_KINDS`, one
gzipped JSON Lines object per UTC day at `security-events/<YYYY>/<MM>/<DD>/security-events-<date>.jsonl.gz`.
The bucket holds each object under an Object Lock default
retention on main, and its policy denies every delete; from the SOC 2 window (`compliance_locks`) the lock
is COMPLIANCE mode, so nobody — root included — can shorten or delete it inside the retention period.

ONCE PER DAY, SELF-HEALING: a firing exports every complete day of the last `LOOKBACK_DAYS` whose object is
not there yet. A missed night is filled the next; an object is never rewritten (a rewrite would be a second
locked version, not a correction). A day is "complete" `SETTLE` after its midnight, so a row committing late
in its transaction is not left out. An empty day still writes an empty object: the run of dated objects is
the auditor's evidence that the export ran.

NO SECRET LEAVES IN `data`: rows are written by sites that already promise none, and this export does not
take their word for it — `data` goes through the capture middleware's key redaction
(`app/user_activity.py::redact`) and every serialized line through the shared pattern redaction
(`app/secret_redact.py`), which raises rather than export unredacted.

OFF UNTIL CONFIGURED: no `SECURITY_ARCHIVE_BUCKET`/`AWS_REGION` is a `noop` naming why — the cron row
ships enabled everywhere (revision 0320). A bucket set with no credentials resolving is NOT unconfigured: it
raises, so the run is an `error` and the starvation alarm fires instead of a `noop` stamping success.
"""
from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import attachment_storage
from app.activity_kinds import SECURITY_EVENT_KINDS
from app.attachment_storage import Target
from app.clock import jsonable

log = logging.getLogger("app.security_archive")

ACTIVITY = "security-event-export"
#: 01:40 daily in the configured zone: off the hour and half hour, before the 03:20 retention sweep.
CRON_EXPRESSION = "0 40 1 * * *"
TIMEOUT_SECONDS = 600
MIN_GAP_SECONDS = 30

KEY_PREFIX = "security-events"
#: How far back a firing looks for a day with no object. Longer than any outage we expect to sit out; well
#: inside the 365-day hot floor, so every day it can fill is still in the database.
LOOKBACK_DAYS = 30
#: How long after a day's midnight it counts as complete.
SETTLE = timedelta(hours=1)
#: Refuse rather than buffer an unbounded day: a day this large is an incident to look at, not to archive
#: silently in pieces. Sized for the CP task's memory (a day is held whole before it is compressed); the
#: other days of the pass are still written (`export`).
MAX_ROWS_PER_DAY = 100_000

_COLUMNS = (
    "id", "created_at", "kind", "action_type", "user_id", "actor_label", "org_id", "summary", "path",
    "method", "status", "data",
)
_SELECT_DAY_SQL = f"""
SELECT {", ".join(_COLUMNS)} FROM activity_events
WHERE kind = ANY(:kinds) AND created_at >= :start AND created_at < :end
ORDER BY created_at, id
LIMIT :limit
"""


def day_key(day: date) -> str:
    """Where one day's export lives in the archive bucket."""
    return f"{KEY_PREFIX}/{day:%Y/%m/%d}/security-events-{day:%Y-%m-%d}.jsonl.gz"


def archive_target() -> Optional[Target]:
    """The archive bucket as a storage target (`attachment_storage.container_target`), or `None` when the export is
    not configured."""
    from app.config import get_settings

    return attachment_storage.container_target(
        get_settings().security_archive_bucket,
        unresolved="SECURITY_ARCHIVE_BUCKET is set but no AWS credentials resolved; the security archive was not written",
    )


def rows_for_day(session: Session, day: date) -> list[dict[str, Any]]:
    """One UTC day's security events, oldest first, `data` key-redacted."""
    from app.user_activity import redact

    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    result = session.execute(
        text(_SELECT_DAY_SQL),
        {
            "kinds": sorted(SECURITY_EVENT_KINDS), "start": start, "end": start + timedelta(days=1),
            "limit": MAX_ROWS_PER_DAY + 1,
        },
    ).mappings().all()
    if len(result) > MAX_ROWS_PER_DAY:
        raise RuntimeError(
            f"{day.isoformat()} holds over {MAX_ROWS_PER_DAY:,} security events; not exporting a partial day"
        )
    rows = []
    for row in result:
        record = {column: jsonable(row[column]) for column in _COLUMNS}
        record["data"] = redact(record["data"])
        rows.append(record)
    return rows


def encode(rows: list[dict[str, Any]]) -> bytes:
    """Gzipped JSON Lines, each line pattern-redacted. `mtime=0` so the same rows give the same bytes."""
    from app.secret_redact import redact as redact_text

    lines = []
    for row in rows:
        line, _ = redact_text(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        lines.append(line + "\n")
    return gzip.compress("".join(lines).encode("utf-8"), mtime=0)


def days_due(now: datetime) -> list[date]:
    """The complete days a firing at `now` is responsible for, oldest first."""
    last = (now.astimezone(timezone.utc) - SETTLE).date() - timedelta(days=1)
    return [last - timedelta(days=offset) for offset in range(LOOKBACK_DAYS - 1, -1, -1)]


@dataclass(frozen=True)
class ExportReport:
    exported_days: int = 0
    exported_events: int = 0
    already_archived: int = 0

    def summary(self) -> str:
        return (
            f"exported {self.exported_days} day(s), {self.exported_events} security event(s); "
            f"{self.already_archived} day(s) already archived"
        )


def export(
    session: Session,
    where: Target,
    *,
    now: Optional[datetime] = None,
    exists: Callable[[Target, str], bool] = attachment_storage.exists,
    put: Callable[..., None] = attachment_storage.put,
) -> ExportReport:
    """Write every due day the bucket does not hold yet. `exists`/`put` are the storage seam.

    A day that cannot be written does not stop the others: it is retried next firing, and once the rest
    are written the pass raises naming it, so the run records a failure an operator sees."""
    exported_days = exported_events = already = 0
    failed: list[str] = []
    for day in days_due(now or datetime.now(timezone.utc)):
        key = day_key(day)
        try:
            if exists(where, key):
                already += 1
                continue
            rows = rows_for_day(session, day)
            put(where, key, encode(rows), content_md5=True)
        except Exception as exc:  # noqa: BLE001 — isolated per day, re-raised below
            failed.append(f"{day.isoformat()}: {type(exc).__name__}: {exc}")
            continue
        exported_days += 1
        exported_events += len(rows)
        log.info("security archive: wrote %s (%d event(s))", key, len(rows))
    report = ExportReport(exported_days, exported_events, already)
    if failed:
        raise RuntimeError(f"{report.summary()}; {len(failed)} day(s) not archived — " + "; ".join(failed))
    return report


def run(session: Session, *, now: Optional[datetime] = None) -> Optional[ExportReport]:
    """The cron's body: `None` when unconfigured, else what one export pass came to."""
    where = archive_target()
    if where is None:
        return None
    return export(session, where, now=now)
