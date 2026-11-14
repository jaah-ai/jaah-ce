"""Client error telemetry: what a flagged console session reports, and how long it is kept.

An operator flags an org (`orgs.client_telemetry`) or one membership (`admin_users.client_telemetry`); a session
under either reports its render errors, chunk-load failures, connection stalls and uncaught errors to
`POST /api/client-events` (`routes/client_events.py`), and an operator reads them on Audit > Client errors. An
unflagged session's report is accepted and dropped, so a stale tab whose flag was turned off stores nothing.

Bounds: `MAX_EVENTS_PER_POST` per request, `MAX_DETAIL_BYTES` per event's `detail`, and `HOURLY_CAP` stored per
member per rolling hour (the excess is dropped, not refused: a looping error must not turn into a retry storm).
Rows are kept `RETENTION` and deleted by the daily `client-events-prune` cron (`prune`).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app import act_as
from app.clock import utcnow
from app.models.audit import ClientEvent
from app.models.identity import AdminUser
from app.models.orgs import Org

MAX_EVENTS_PER_POST = 50
#: One event's `detail`, JSON-serialized, in UTF-8 bytes.
MAX_DETAIL_BYTES = 16 * 1024
#: Stored per member per rolling hour.
HOURLY_CAP = 300
RETENTION = timedelta(days=30)

#: Text columns are clipped to these, never refused: a long error message is still worth keeping.
MAX_ROUTE = 500
MAX_MESSAGE = 1000
MAX_BUILD_VERSION = 64
MAX_USER_AGENT = 500


def is_flagged(session: Session, user: AdminUser) -> bool:
    """Whether this membership's console reports: its own switch, or its org's. An operator acting as an org
    (`act_as.operator_org`) reports on that org's switch alone: the seat copies the operator's own membership
    switch, which says nothing about the org acted as."""
    if user.client_telemetry and act_as.operator_org(user) is None:
        return True
    if user.org_id is None:
        return False
    return bool(session.scalar(select(Org.client_telemetry).where(Org.id == user.org_id)))


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    return value[:limit] if value is not None else None


def store(
    session: Session,
    user: AdminUser,
    events: list[dict[str, Any]],
    *,
    user_agent: Optional[str],
    client_ip: Optional[str],
) -> int:
    """Stage `events` (already validated) for a flagged `user`, up to what `HOURLY_CAP` leaves this hour; returns how
    many were staged. The caller commits."""
    # Serialize this member's count-then-insert: two concurrent posts would each see the same room.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('client_events:' || :user_id))"), {"user_id": str(user.id)}
    )
    since = utcnow() - timedelta(hours=1)
    stored_this_hour = session.scalar(
        select(func.count()).select_from(ClientEvent).where(
            ClientEvent.user_id == user.id, ClientEvent.created_at >= since
        )
    ) or 0
    room = max(0, HOURLY_CAP - stored_this_hour)
    kept = events[:room]
    for event in kept:
        session.add(ClientEvent(
            org_id=user.org_id,
            user_id=user.id,
            kind=event["kind"],
            route=_clip(event.get("route"), MAX_ROUTE),
            message=_clip(event.get("message") or "", MAX_MESSAGE),
            detail=event.get("detail") or {},
            build_version=_clip(event.get("build_version"), MAX_BUILD_VERSION),
            user_agent=_clip(user_agent, MAX_USER_AGENT),
            client_ip=client_ip,
            occurred_at=event["occurred_at"],
        ))
    return len(kept)


def prune(session: Session) -> int:
    """Delete every report older than `RETENTION`; returns how many went. The `client-events-prune` cron's job, run
    fleet-wide on the operator plane."""
    cutoff = utcnow() - RETENTION
    deleted = session.execute(delete(ClientEvent).where(ClientEvent.created_at < cutoff)).rowcount or 0
    session.commit()
    return deleted
