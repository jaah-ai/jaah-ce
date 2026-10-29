"""What a trigger's owner is told: the moment a trigger goes Failed, and a daily
digest — each as one `trigger_notices` row (the bell's `trigger_failed` / `trigger_digest` kinds,
`routes/inbox.py`) plus one email (`email_notify.prepare_template_email`, sent only once the row commits).

The digest is the `trigger-digest` system activity's body (`run_digest`). Its clock is the activity's
`scheduled_at`, so a test injects the time: first the idle auto-pause (R13's days rule, `trigger_health.idle_due`),
then, per owner and org, one digest covering the 24 h before `scheduled_at` — sent only to an owner who had a
delivery, or a trigger not Active, in that window (A14), and at most once per owner, org and UTC day
(`uq_trigger_notices_daily`). Per org rather than per person: the bell is org-scoped, and one row naming two
orgs' triggers would show one org's names in the other's bell.

Every write runs in `trigger_intake.trigger_session`, bound to a trigger's own org. A notice carries display
data only — names, counts, health text, links — never a secret, a token or an event body.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import after_commit, edition_features, email_notify, email_templates, trigger_health
from app.models.automations import TaskTrigger, TriggerDelivery, TriggerNotice
from app.models.identity import AdminUser
from app.models.projects import Project
from app.one_line import one_line
from app.triggers import (
    DIGEST_WINDOW_HOURS,
    HEALTH_ACTIVE,
    HEALTH_STATES,
    NOTICE_DIGEST,
    NOTICE_FAILED,
    NOTICE_KINDS,
    NOTIFICATION_EVENT,
    OUTCOME_LABELS,
)

# The community edition has no Telegram: `_telegram_after_commit` returns before this name is read.
if edition_features.enabled("telegram"):
    from app import telegram_notify

log = logging.getLogger("app.trigger_notices")

TRIGGERS_PATH = "/triggers"
#: A reason is quoted into a notice and an email; past this it is cut (the delivery log keeps it whole), so text
#: a sender steered into an error message cannot fill a message sent from the deployment's own mailbox.
MAX_REASON_CHARS = 200
_HEALTH_LABELS = {state: state.capitalize() for state in HEALTH_STATES}


def trigger_path(trigger_id: UUID) -> str:
    return f"{TRIGGERS_PATH}/{trigger_id}"


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ── The bell ─────────────────────────────────────────────────────────────────────────────────────────


def wire(row: TriggerNotice) -> dict[str, Any]:
    """The bell's shape: the server's sentence (`text`), a second line (`detail`) and where it opens (`href`)."""
    summary = row.summary or {}
    return {
        "kind": row.kind,
        "trigger_id": str(row.trigger_id) if row.trigger_id else None,
        "text": str(summary.get("text") or ""),
        "detail": str(summary.get("detail") or ""),
        "href": str(summary.get("href") or TRIGGERS_PATH),
    }


def feed(
    session: Session, org_id: UUID, user_id: UUID, *, unread_only: bool, limit: int
) -> list[TriggerNotice]:
    """The caller's own trigger notices in their active org, newest first."""
    # Only the kinds this release reads: a kind a later release writes is skipped, never a bell that fails to load.
    query = (
        select(TriggerNotice)
        .where(TriggerNotice.org_id == org_id, TriggerNotice.user_id == user_id, TriggerNotice.kind.in_(NOTICE_KINDS))
        .order_by(TriggerNotice.created_at.desc(), TriggerNotice.id.desc())
        .limit(limit)
    )
    if unread_only:
        query = query.where(TriggerNotice.read_at.is_(None))
    return list(session.scalars(query).all())


def mark_read(session: Session, org_id: UUID, user_id: UUID, notice_ids: list[UUID]) -> int:
    """Stamp the caller's read of each named notice of their own; anyone else's is skipped silently."""
    if not notice_ids:
        return 0
    result = session.execute(
        update(TriggerNotice)
        .where(
            TriggerNotice.org_id == org_id,
            TriggerNotice.user_id == user_id,
            TriggerNotice.id.in_(notice_ids),
            TriggerNotice.read_at.is_(None),
        )
        .values(read_at=func.now())
    )
    return result.rowcount or 0


def mark_unread(session: Session, org_id: UUID, user_id: UUID, notice_ids: list[UUID]) -> int:
    """Clear the caller's read stamp on each named notice of their own — "mark as unread"."""
    if not notice_ids:
        return 0
    result = session.execute(
        update(TriggerNotice)
        .where(
            TriggerNotice.org_id == org_id,
            TriggerNotice.user_id == user_id,
            TriggerNotice.id.in_(notice_ids),
            TriggerNotice.read_at.is_not(None),
        )
        .values(read_at=None)
    )
    return result.rowcount or 0


# ── The failure notice (R13: "at that moment, not only in the digest") ──────────────────────────────


def _member_owner(session: Session, trigger: TaskTrigger) -> Optional[AdminUser]:
    """The trigger's owner when they are still an enabled member of the trigger's own org, else nobody: a
    disabled or moved member is told nothing about an org they no longer belong to."""
    from app.trigger_intake import trigger_owner

    owner = trigger_owner(session, trigger)
    if owner is None or owner.disabled_at is not None or (
        owner.org_id is not None and owner.org_id != trigger.org_id
    ):
        return None
    return owner


def _recipient(session: Session, trigger: TaskTrigger) -> Optional[AdminUser]:
    """`_member_owner` when they can still read the trigger's project, else nobody: one whose project access
    was taken away is told nothing about it."""
    owner = _member_owner(session, trigger)
    return owner if owner is not None and _may_read_project(session, owner, trigger.project_id) else None


def _may_read_project(session: Session, owner: AdminUser, project_id: int) -> bool:
    """May `owner` read `project_id` now? Asked at each notice, not cached, so a revoked grant takes effect."""
    from app.authz.object_access import can_access
    from app.authz.principal import principal_for_user
    from app.authz.visibility import ObjectAccess

    try:
        return can_access(session, principal_for_user(session, owner), "project", project_id, mode=ObjectAccess.READ)
    except Exception:  # noqa: BLE001 — an unanswered probe tells nobody; it must not fail the trigger's own pass
        log.exception("trigger notice: could not check project %s access for %s — not sent", project_id, owner.id)
        return False


def _email_after_commit(session: Session, owner: AdminUser, *, type_key: str, values: dict[str, str],
                        path: str) -> None:
    send = email_notify.prepare_template_email(
        session, owner, event_name=NOTIFICATION_EVENT, type_key=type_key,
        values={"first_name": owner.first_name or "", **values},
        button_url=email_notify.console_path_link(path),
    )
    if send is not None:
        after_commit.on_commit(session, send)


def _telegram_after_commit(session: Session, owner: AdminUser, *, headline: str, message: str, path: str) -> None:
    """The same notice over Telegram (`app/telegram_notify.py`), in the bell's own words, after the commit."""
    if not edition_features.enabled("telegram"):
        return
    send = telegram_notify.prepare_text(
        owner, event_name=NOTIFICATION_EVENT, headline=headline, message=message,
        link=email_notify.console_path_link(path),
    )
    if send is not None:
        after_commit.on_commit(session, send)


def notify_failed(session: Session, trigger: TaskTrigger, reason: str, *, idle: bool = False) -> None:
    """The owner's `trigger_failed` notice, in the caller's transaction, and its email after commit; unless `idle`,
    the org's Owners and Admins are told too (`_alert_admins`: their bell rows in this transaction, the delivery
    after commit), and an owner who is one of them gets only that. A trigger with no live owner and no admin has nobody to tell."""
    told: set[UUID] = set()
    if not idle:
        told = _alert_admins(session, trigger, reason)
    owner = _recipient(session, trigger)
    if owner is None or owner.id in told:
        return
    reason = one_line(reason, MAX_REASON_CHARS)
    project = session.scalar(select(Project.name).where(Project.id == trigger.project_id)) or ""
    path = trigger_path(trigger.id)
    stopped = f"Trigger stopped: {trigger.name}"
    session.add(TriggerNotice(
        org_id=trigger.org_id, user_id=owner.id, trigger_id=trigger.id, kind=NOTICE_FAILED,
        summary={
            "text": stopped,
            "detail": reason,
            "href": path,
            "trigger_name": trigger.name,
            "reason": reason,
        },
    ))
    _email_after_commit(
        session, owner, type_key=email_templates.TRIGGER_FAILED.key, path=path,
        values={"trigger_name": trigger.name, "project_name": project, "reason": reason},
    )
    _telegram_after_commit(session, owner, headline=stopped, message=reason, path=path)


def _alert_admins(session: Session, trigger: TaskTrigger, reason: str) -> set[UUID]:
    """Stage the org's Owners' and Admins' bell rows in the stop's own transaction, owe their important delivery to
    its commit, and return who they are. Under a SAVEPOINT: a failed read stages nothing, tells nobody new, and leaves
    the stop and the owner's own notice to proceed."""
    from app import automation_alerts

    href = (
        automation_alerts.automation_path(trigger.automation_id) if trigger.automation_id else trigger_path(trigger.id)
    )
    try:
        with session.begin_nested():
            people, send = automation_alerts.stage(
                session, trigger.org_id, text=f"Automation stopped: {trigger.name}", detail=reason, href=href,
                trigger_id=trigger.id,
            )
    except Exception:  # noqa: BLE001 — the stop must land; the owner is still told below
        log.exception("trigger notice: could not alert the Owners and Admins of trigger %s", trigger.id)
        return set()
    if send is not None:
        after_commit.on_commit(session, send)
    return set(people)


# ── The daily digest ─────────────────────────────────────────────────────────────────────────────────


@dataclass
class DigestReport:
    idle_failed: int = 0
    digests: int = 0
    already_sent: int = 0

    @property
    def acted(self) -> bool:
        return bool(self.idle_failed or self.digests)

    def summary(self) -> str:
        parts = [
            f"{_plural(self.idle_failed, 'trigger')} stopped as idle",
            f"{_plural(self.digests, 'digest')} sent",
        ]
        if self.already_sent:
            parts.append(f"{self.already_sent} already sent today")
        return ", ".join(parts)


def _day(scheduled_at: datetime) -> str:
    return scheduled_at.astimezone(timezone.utc).date().isoformat()


def apply_idle_rule(scheduled_at: datetime) -> int:
    """R13's days rule over every enabled trigger that has it on; returns how many it stopped."""
    from app.trigger_intake import fleet_session, trigger_session

    with fleet_session() as session:
        ids = list(session.scalars(
            select(TaskTrigger.id).where(
                TaskTrigger.auto_pause_idle_days.is_not(None),
                TaskTrigger.paused_at.is_(None),
                TaskTrigger.failed_at.is_(None),
            ).order_by(TaskTrigger.id)
        ))
    stopped = 0
    for trigger_id in ids:
        try:
            with trigger_session(trigger_id) as (session, _org):
                trigger = session.scalar(select(TaskTrigger).where(TaskTrigger.id == trigger_id).with_for_update())
                if trigger is None or not trigger_health.idle_due(trigger, scheduled_at):
                    session.rollback()
                    continue
                trigger_health.mark_failed(session, trigger, trigger_health.idle_reason(trigger), idle=True)
                session.commit()
                stopped += 1
        except Exception:  # noqa: BLE001 — one trigger's fault must not stop the rest
            log.exception("trigger-digest: the idle rule failed for trigger %s", trigger_id)
    return stopped


def _trigger_block(trigger: TaskTrigger, counts: dict[str, int], over_budget: int, owner: AdminUser
                   ) -> tuple[dict[str, Any], str]:
    """One trigger's digest entry (stored) and its email paragraph."""
    full_name = " ".join(part for part in (owner.first_name, owner.last_name) if part) or owner.email
    paused_by = full_name if trigger.paused_by_id == owner.id else None
    state, reason = trigger_health.trigger_health(trigger, paused_by_name=paused_by)
    reason = one_line(reason, MAX_REASON_CHARS) if reason else None
    health = _HEALTH_LABELS.get(state, state) + (f": {reason}" if reason else "")
    total = sum(counts.values())
    count_text = ", ".join(
        f"{n} {OUTCOME_LABELS.get(outcome, outcome).lower()}" for outcome, n in sorted(counts.items())
    ) or "no deliveries"
    lines = [f"{trigger.name} — {health}", count_text]
    if over_budget:
        lines.append(f"{_plural(over_budget, 'run')} over budget")
    link = email_notify.console_path_link(trigger_path(trigger.id))
    if link:
        lines.append(link)
    entry = {
        "id": str(trigger.id), "name": trigger.name, "health": state, "reason": reason, "deliveries": total,
        "counts": dict(sorted(counts.items())), "over_budget": over_budget, "href": trigger_path(trigger.id),
    }
    return entry, "\n".join(lines)


def _send_digest(session: Session, org: UUID, owner: AdminUser, triggers: list[TaskTrigger],
                 start: datetime, end: datetime, day: str) -> Optional[bool]:
    """The owner's digest for this org: True when sent, False when already sent today, None on a quiet day."""
    ids = [t.id for t in triggers]
    counts: dict[UUID, dict[str, int]] = defaultdict(dict)
    for trigger_id, state, n in session.execute(
        select(TriggerDelivery.trigger_id, TriggerDelivery.state, func.count())
        .where(
            TriggerDelivery.trigger_id.in_(ids),
            TriggerDelivery.received_at >= start,
            TriggerDelivery.received_at < end,
        )
        .group_by(TriggerDelivery.trigger_id, TriggerDelivery.state)
    ):
        counts[trigger_id][state] = n
    over: dict[UUID, int] = dict(session.execute(
        select(TriggerDelivery.trigger_id, func.count())
        .where(
            TriggerDelivery.trigger_id.in_(ids),
            TriggerDelivery.budget_exceeded_usd.is_not(None),
            TriggerDelivery.received_at >= start,
            TriggerDelivery.received_at < end,
        )
        .group_by(TriggerDelivery.trigger_id)
    ).all())
    entries, paragraphs = [], []
    for trigger in sorted(triggers, key=lambda t: t.name.lower()):
        state, _reason = trigger_health.trigger_health(trigger)
        if not counts.get(trigger.id) and state == HEALTH_ACTIVE:
            continue
        entry, paragraph = _trigger_block(trigger, counts.get(trigger.id, {}), over.get(trigger.id, 0), owner)
        entries.append(entry)
        paragraphs.append(paragraph)
    if not entries:
        return None
    deliveries = sum(e["deliveries"] for e in entries)
    attention = sum(1 for e in entries if e["health"] != HEALTH_ACTIVE)
    headline = f"{_plural(len(entries), 'trigger')}, {_plural(deliveries, 'delivery', 'deliveries')}"
    if attention:
        headline += f", {attention} {'needs' if attention == 1 else 'need'} attention"
    digest = f"Trigger digest: {headline}"
    inserted = session.execute(
        insert(TriggerNotice)
        .values(
            org_id=org, user_id=owner.id, kind=NOTICE_DIGEST,
            summary={
                "day": day, "window_start": start.isoformat(), "window_end": end.isoformat(),
                "text": digest,
                "detail": "Last 24 hours",
                "href": TRIGGERS_PATH,
                "triggers": entries,
            },
        )
        .on_conflict_do_nothing()
        .returning(TriggerNotice.id)
    ).first()
    if inserted is None:
        return False
    _email_after_commit(
        session, owner, type_key=email_templates.TRIGGER_DIGEST.key, path=TRIGGERS_PATH,
        values={"headline": headline, "trigger_lines": "\n\n".join(paragraphs)},
    )
    _telegram_after_commit(
        session, owner, headline=digest, message="\n\n".join(paragraphs), path=TRIGGERS_PATH,
    )
    return True


def run_digest(scheduled_at: datetime) -> DigestReport:
    """The `trigger-digest` activity: the idle rule, then each owner's digest for the 24 h before `scheduled_at`."""
    from app.tenancy.seam import TenancyRowMissing
    from app.trigger_intake import fleet_session, trigger_session

    report = DigestReport(idle_failed=apply_idle_rule(scheduled_at))
    end = scheduled_at.astimezone(timezone.utc)
    start = end - timedelta(hours=DIGEST_WINDOW_HOURS)
    day = _day(scheduled_at)
    with fleet_session() as session:
        rows = session.execute(
            select(TaskTrigger.org_id, TaskTrigger.created_by_id, TaskTrigger.id).order_by(TaskTrigger.id)
        ).all()
    groups: dict[tuple[UUID, UUID], list[UUID]] = defaultdict(list)
    for org_id, owner_id, trigger_id in rows:
        groups[(org_id, owner_id)].append(trigger_id)
    for (org_id, owner_id), trigger_ids in groups.items():
        sent: Optional[bool] = None
        # Bound through one of the group's triggers (every trigger in it shares that org); the next one if a
        # trigger was deleted since the read above.
        for bind_id in trigger_ids:
            try:
                with trigger_session(bind_id) as (session, org):
                    triggers = list(session.scalars(
                        select(TaskTrigger).where(TaskTrigger.org_id == org_id, TaskTrigger.created_by_id == owner_id)
                    ))
                    owner = _member_owner(session, triggers[0]) if triggers else None
                    if owner is not None:
                        # Only the triggers whose project the owner can still read (`_recipient`'s rule).
                        triggers = [t for t in triggers if _may_read_project(session, owner, t.project_id)]
                        owner = owner if triggers else None
                    if owner is not None:
                        sent = _send_digest(session, org, owner, triggers, start, end, day)
                    session.commit()
                break
            except TenancyRowMissing:
                continue
            except Exception:  # noqa: BLE001 — one owner's fault must not stop the rest
                log.exception("trigger-digest: the digest failed for owner %s in org %s", owner_id, org_id)
                break
        if sent:
            report.digests += 1
        elif sent is False:
            report.already_sent += 1
    return report
