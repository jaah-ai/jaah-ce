"""An org's own retention windows — the rules behind Settings › Retention and the per-org pass of the nightly
`retention_sweep` (`app/cron_retention.py`).

Three classes, each a nullable `orgs` column in days (NULL = the deployment's window):

  transcripts  the org's uploaded session transcripts: their objects (transcript, prompt artifacts, manifest) are
               deleted from each row's own bucket and, once verifiably gone, the row retired `unavailable` with the
               reason, as the deployment-wide transcript retirement does. A `failed`/`gave_up`/`uploading` session's
               partial objects are deleted too; its status stays as it is.
  events       the org's activity events, through the deployment pass's own statement scoped to the org — so task
               pipeline history is spared and security events keep their 365-day floor
               (`cron_retention.SECURITY_EVENT_MIN_RETENTION_DAYS`) whatever the org says.
  artifacts    the runner-authored documents on the org's finished tasks (`task_manifest.DOCUMENT_KINDS`): row
               deleted, its object reaped once no surviving row shares the key
               (`app/attachment_storage.py::reap_unreferenced_object`).

An org can only SHORTEN: each class's ceiling is the lower of the operator's `org_retention_max_days` and the
deployment's own window for it (the activity log knob; 180 days for transcripts and artifacts, the transcript
bucket's lifecycle). The floor is `org_retention_min_days`. A stored value the operator's bounds later moved past is
clamped into them when read; the console shows the clamped value, which the org's next save writes.

The pass runs only while the org's plan switch `org_retention_enabled` is on; switched off, the stored values stay
and the deployment windows apply. The deployment-wide pass is unchanged either way.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Final, Optional
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, aliased

from app import cron_retention, plan_terms
from app.operator_settings import reads as settings_reads
from app.models.agent_sessions import AgentSession
from app.models.orgs import Org
from app.models.task_extras import TaskAttachment
from app.models.tasks import Task
from app.task_lifecycle import DONE
from app.task_manifest import DOCUMENT_KINDS

log = logging.getLogger("cockpit-cron")

#: The plan switch (`plan_terms.SWITCHES`).
SWITCH: Final = "org_retention_enabled"

#: Operator knobs (`app/operator_settings/catalog.py::CATALOG`, mirrored there as literals).
MIN_DAYS_KEY: Final = "org_retention_min_days"
MAX_DAYS_KEY: Final = "org_retention_max_days"

TRANSCRIPTS: Final = "transcripts"
EVENTS: Final = "events"
ARTIFACTS: Final = "artifacts"

#: Each class's `orgs` column and its name in a refusal.
CLASSES: Final[dict[str, tuple[str, str]]] = {
    TRANSCRIPTS: ("retention_transcripts_days", "Session transcripts"),
    EVENTS: ("retention_events_days", "Activity events"),
    ARTIFACTS: ("retention_artifacts_days", "Task artifacts"),
}

#: Per org per firing: each transcript costs a handful of S3 calls, so a backlog drains over several nights.
SESSIONS_PER_ORG_SWEEP: Final = 200

#: The transcript statuses whose upload never finished: their objects may be partial, and nothing retires them.
PARTIAL_STATUSES: Final = ("failed", "gave_up", "uploading")
ARTIFACTS_PER_ORG_SWEEP: Final = 2000


def deployment_days(data_class: str) -> int:
    """How long the deployment keeps `data_class` with no org setting."""
    if data_class == EVENTS:
        return cron_retention.activity_events_retention_days()
    return cron_retention.POLICY_MAX_RETENTION_DAYS[cron_retention.RETENTION_CLASS_TRANSCRIPT]


@dataclass(frozen=True)
class Bounds:
    """What an org may pick for one class: `min_days..max_days`; `default_days` is what unset means."""

    min_days: int
    max_days: int
    default_days: int


def bounds(data_class: str) -> Bounds:
    """The class's allowed range now. A floor above the ceiling collapses onto the ceiling."""
    default_days = deployment_days(data_class)
    max_days = min(settings_reads.get_int(MAX_DAYS_KEY), default_days)
    return Bounds(min_days=min(settings_reads.get_int(MIN_DAYS_KEY), max_days), max_days=max_days,
                  default_days=default_days)


def stored_days(org: Org, data_class: str) -> Optional[int]:
    return getattr(org, CLASSES[data_class][0])


def clamp(data_class: str, days: int) -> int:
    """`days` held inside the class's current bounds."""
    limits = bounds(data_class)
    return max(limits.min_days, min(days, limits.max_days))


def effective_days(org: Org, data_class: str) -> int:
    """The window the sweep applies to the org's `data_class`: its own value inside the current bounds, else the
    deployment's."""
    days = stored_days(org, data_class)
    return deployment_days(data_class) if days is None else clamp(data_class, days)


def refusal(data_class: str, days: int) -> Optional[str]:
    """The sentence refusing `days` for `data_class`, or None when it is allowed."""
    limits = bounds(data_class)
    if limits.min_days <= days <= limits.max_days:
        return None
    # Worded as the form's hint: the platform's default may itself sit outside the range, so it is not offered as a value.
    return (f"{CLASSES[data_class][1]}: enter {limits.min_days} to {limits.max_days} days. "
            f"Empty keeps the platform’s {limits.default_days} days.")


# ── The per-org pass ─────────────────────────────────────────────────────────────────────────────────


def _transcript_why(days: int) -> str:
    return f"deleted by your organization's retention setting ({days} days)"


def sweep_transcripts(session: Session, org_id: UUID, *, days: int, now: datetime,
                      limit: int = SESSIONS_PER_ORG_SWEEP) -> int:
    """Delete the objects of the org's uploaded transcripts started more than `days` ago, then retire each row whose
    objects are verifiably gone. COMMITS: the selection is read and released before any S3 call, so no transaction
    stays open across them, and the retirement commits on its own.

    A session whose member set cannot be known (no readable manifest) or whose objects still answer stays `uploaded`
    and is tried again next night; no row is ever retired while its bytes remain. Skips (0) when object storage is
    not configured here."""
    from app import attachment_storage
    from app.tenancy import offboard

    if attachment_storage.target() is None:
        return 0
    cutoff = now - timedelta(days=days)
    rows = session.execute(
        select(AgentSession.id, AgentSession.agent_session_id, AgentSession.started_at,
               AgentSession.transcript_manifest_key, AgentSession.transcript_bucket,
               AgentSession.transcript_attempts)
        .where(AgentSession.org_id == org_id, AgentSession.transcript_status == "uploaded",
               AgentSession.started_at < cutoff)
        .order_by(AgentSession.started_at)
        .limit(limit)
    ).all()
    session.rollback()
    gone: list[int] = []
    for row in rows:
        keys, unverifiable = offboard.transcript_keys([tuple(row)[1:]])
        if unverifiable:
            continue
        offboard.delete_objects(keys)
        if not offboard.objects_remaining(keys):
            gone.append(row.id)
    if len(gone) < len(rows):
        log.warning("cron retention: org %s: %d transcript(s) past its %d-day window could not be deleted; "
                    "retried next firing", org_id, len(rows) - len(gone), days)
    if not gone:
        return 0
    session.execute(
        update(AgentSession)
        .where(AgentSession.id.in_(gone), AgentSession.transcript_status == "uploaded")
        .values(transcript_status="unavailable", transcript_error=_transcript_why(days))
    )
    session.commit()
    return len(gone)


def sweep_partial_transcripts(session: Session, org_id: UUID, *, days: int, now: datetime,
                              limit: int = SESSIONS_PER_ORG_SWEEP) -> int:
    """Delete whatever objects remain of the org's unfinished uploads (`PARTIAL_STATUSES`) started more than `days`
    ago: `(sessions with an object deleted)`. The row and its status are left as they are — nothing marks it swept,
    so the band stops where the bucket's lifecycle takes over (the deployment transcript window), and each night
    samples it in a fresh order, so a band wider than `limit` is covered over a few nights rather than the same rows
    re-read forever. An upload whose lease is still live is spared. Reads and releases the selection before any S3
    call. Skips (0) when object storage is not configured here."""
    from app import attachment_storage
    from app.session_transcripts import LEASE_TTL
    from app.tenancy import offboard

    if attachment_storage.target() is None:
        return 0
    rows = session.execute(
        select(AgentSession.agent_session_id, AgentSession.started_at, AgentSession.transcript_manifest_key,
               AgentSession.transcript_bucket, AgentSession.transcript_attempts)
        .where(AgentSession.org_id == org_id, AgentSession.transcript_status.in_(PARTIAL_STATUSES),
               AgentSession.started_at < now - timedelta(days=days),
               AgentSession.started_at >= now - timedelta(days=deployment_days(TRANSCRIPTS)),
               or_(AgentSession.transcript_status != "uploading", AgentSession.transcript_leased_at < now - LEASE_TTL))
        .order_by(func.md5(func.concat(AgentSession.agent_session_id, now.date().isoformat())))
        .limit(limit)
    ).all()
    session.rollback()
    swept = left = 0
    for row in rows:
        # The members may be unknowable (no readable manifest): delete every one that answers all the same.
        keys, _unverifiable = offboard.transcript_keys([tuple(row)])
        if not keys:
            continue
        offboard.delete_objects(keys)
        if offboard.objects_remaining(keys):
            left += 1
        else:
            swept += 1
    if left:
        log.warning("cron retention: org %s: %d unfinished upload(s) past its %d-day window kept objects a delete "
                    "could not remove; retried next firing", org_id, left, days)
    return swept


def sweep_artifacts(session: Session, org_id: UUID, *, days: int, now: datetime,
                    limit: int = ARTIFACTS_PER_ORG_SWEEP) -> tuple[int, list[tuple[Optional[str], str]]]:
    """Delete the org's task artifact rows created more than `days` ago on FINISHED work: `(rows deleted, their
    objects' (bucket, key))` — each row's own bucket, so the reap deletes where the bytes were written. A task still in flight, or a child of an epic still in flight, keeps its documents — a later
    stage may read them as input (`task_manifest.INPUT_MANIFEST`). The caller reaps the keys AFTER committing (a key
    shared with a surviving row must survive). Caller-transactional."""
    cutoff = now - timedelta(days=days)
    parent = aliased(Task)
    rows = session.execute(
        select(TaskAttachment.id, TaskAttachment.s3_bucket, TaskAttachment.s3_key)
        .join(Task, Task.id == TaskAttachment.task_id)
        .outerjoin(parent, parent.id == Task.parent_task_id)
        .where(TaskAttachment.org_id == org_id, TaskAttachment.kind.in_(DOCUMENT_KINDS),
               TaskAttachment.created_at < cutoff, Task.state == DONE,
               or_(Task.parent_task_id.is_(None), parent.state == DONE))
        .order_by(TaskAttachment.created_at)
        .limit(limit)
    ).all()
    if not rows:
        return 0, []
    session.query(TaskAttachment).filter(TaskAttachment.id.in_([row.id for row in rows])).delete(
        synchronize_session=False
    )
    return len(rows), sorted({(row.s3_bucket, row.s3_key) for row in rows if row.s3_key},
                             key=lambda ref: (ref[1], ref[0] or ""))


@dataclass
class OrgsSwept:
    """The per-org pass's totals, for the Runs-tab line."""

    orgs: int = 0
    events: int = 0
    transcripts: int = 0
    #: Unfinished uploads whose leftover objects were deleted (their rows stay).
    partial_transcripts: int = 0
    artifacts: int = 0
    #: Orgs whose pass raised; logged, and the next org still runs.
    failed: int = 0

    @property
    def total(self) -> int:
        return self.events + self.transcripts + self.partial_transcripts + self.artifacts

    def line(self) -> str:
        return (f"org retention: {self.orgs} org(s) with their own windows — {self.events} activity event(s), "
                f"{self.transcripts} transcript(s), {self.partial_transcripts} unfinished upload(s), "
                f"{self.artifacts} task artifact(s) removed"
                + (f"; {self.failed} org(s) failed (logged), retried next firing" if self.failed else ""))


def _orgs_with_windows(session: Session) -> list[UUID]:
    columns = [getattr(Org, column) for column, _label in CLASSES.values()]
    return list(session.scalars(
        select(Org.id).where(or_(*(column.is_not(None) for column in columns))).order_by(Org.id)
    ))


def _sweep_org(session: Session, org_id: UUID, at: datetime, out: OrgsSwept, reap: Callable) -> None:
    """One org's pass: activity events and artifacts in one transaction, transcripts in their own (`sweep_transcripts`
    commits), artifact objects reaped after the commit."""
    org = session.get(Org, org_id)
    if org is None or not plan_terms.switch_on(session, org_id, SWITCH):
        return
    out.orgs += 1
    windows = {data_class: effective_days(org, data_class) for data_class in CLASSES}
    shorter = {c: d for c, d in windows.items() if stored_days(org, c) is not None and d < deployment_days(c)}
    keys: list[tuple[Optional[str], str]] = []
    if EVENTS in shorter:
        out.events += cron_retention.sweep_activity_events(
            session, max_age_days=shorter[EVENTS], now=at, org_id=org_id
        ).by_age
    if ARTIFACTS in shorter:
        deleted, keys = sweep_artifacts(session, org_id, days=shorter[ARTIFACTS], now=at)
        out.artifacts += deleted
    session.commit()
    reap(session, keys)
    if TRANSCRIPTS in shorter:
        out.transcripts += sweep_transcripts(session, org_id, days=shorter[TRANSCRIPTS], now=at)
        out.partial_transcripts += sweep_partial_transcripts(session, org_id, days=shorter[TRANSCRIPTS], now=at)


def sweep_orgs(*, now: Optional[datetime] = None, session_factory: Optional[Callable[[], Session]] = None) -> OrgsSwept:
    """Apply every org's own windows while its plan switch is on, each org on a session of its own. An org whose
    window equals the deployment's is left to the deployment pass. An org whose pass raises is logged and counted;
    the rest still run."""
    from app.automation_service import reap_attachment_keys
    from app.db import SessionLocal

    at = now or datetime.now(timezone.utc)
    factory = session_factory or SessionLocal
    out = OrgsSwept()
    with factory() as session:
        org_ids = _orgs_with_windows(session)
    for org_id in org_ids:
        with factory() as session:
            try:
                _sweep_org(session, org_id, at, out, reap_attachment_keys)
            except Exception:  # noqa: BLE001 — one org's failure must not starve every org after it
                log.exception("cron retention: the retention pass for org %s failed", org_id)
                session.rollback()
                out.failed += 1
    if out.total:
        log.info("cron retention: %s", out.line())
    return out
