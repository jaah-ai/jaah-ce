"""What a trigger's owner is told: the moment a trigger goes Failed — one `trigger_notices` row (the bell's
`trigger_failed` kind, `routes/inbox.py`) plus one email (`email_notify.prepare_template_email`, sent only once the
row commits). The bell still reads `trigger_digest` rows written before the daily digest was retired (PRD R9: a
digest asks nothing of its reader).

The idle auto-pause (R13's days rule, `trigger_health.idle_due`) is the `trigger-digest` system activity's body
(`apply_idle_rule`). Its clock is the activity's `scheduled_at`, so a test injects the time.

Every write runs in `trigger_intake.trigger_session`, bound to a trigger's own org. A notice carries display
data only — names, counts, health text, links — never a secret, a token or an event body.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import after_commit, edition_features, email_notify, email_templates, trigger_health
from app.automation_alerts import AUTOMATIONS_PATH, automation_path
from app.models.automations import TaskTrigger, TriggerNotice
from app.models.identity import AdminUser
from app.models.projects import Project
from app.one_line import one_line
from app.triggers import NOTICE_FAILED, NOTICE_KINDS, NOTIFICATION_EVENT

# The community edition has no Telegram: `_telegram_after_commit` returns before this name is read.
if edition_features.enabled("telegram"):
    from app import telegram_notify

log = logging.getLogger("app.trigger_notices")

#: A reason is quoted into a notice and an email; past this it is cut (the delivery log keeps it whole), so text
#: a sender steered into an error message cannot fill a message sent from the deployment's own mailbox.
MAX_REASON_CHARS = 200


def trigger_path(trigger: TaskTrigger) -> str:
    """Where a trigger's notice opens: the runs of the automation it is the event child of, or the Automations list
    for a trigger older than automations (`TaskTrigger.automation_id` is still nullable)."""
    return automation_path(trigger.automation_id) if trigger.automation_id else AUTOMATIONS_PATH


# ── The bell ─────────────────────────────────────────────────────────────────────────────────────────


def wire(row: TriggerNotice) -> dict[str, Any]:
    """The bell's shape: the server's sentence (`text`), a second line (`detail`) and where it opens (`href`)."""
    summary = row.summary or {}
    href = str(summary.get("href") or AUTOMATIONS_PATH)
    # A notice stored before the Triggers pages retired still names them: it opens the Automations list.
    if href.startswith(("/triggers", "/crons/")):
        href = AUTOMATIONS_PATH
    return {
        "kind": row.kind,
        "trigger_id": str(row.trigger_id) if row.trigger_id else None,
        "text": str(summary.get("text") or ""),
        "detail": str(summary.get("detail") or ""),
        "href": href,
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


def set_read(
    session: Session, org_id: UUID, user_id: UUID, notice_ids: list[UUID], *, read: bool, model: Any = TriggerNotice
) -> int:
    """Flip the caller's own named notices that are not already `read`; anyone else's is skipped silently.
    `model` is the notice table: this one, or another bell's of the same shape (`releases/notify.py`)."""
    if not notice_ids:
        return 0
    result = session.execute(
        update(model)
        .where(
            model.org_id == org_id, model.user_id == user_id, model.id.in_(notice_ids),
            model.read_at.is_(None) if read else model.read_at.is_not(None),
        )
        .values(read_at=func.now() if read else None)
    )
    return result.rowcount or 0


def mark_read(session: Session, org_id: UUID, user_id: UUID, notice_ids: list[UUID]) -> int:
    """Stamp the caller's read of each named notice of their own; anyone else's is skipped silently."""
    return set_read(session, org_id, user_id, notice_ids, read=True)


def mark_unread(session: Session, org_id: UUID, user_id: UUID, notice_ids: list[UUID]) -> int:
    """Clear the caller's read stamp on each named notice of their own — "mark as unread"."""
    return set_read(session, org_id, user_id, notice_ids, read=False)


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
    path = trigger_path(trigger)
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

    href = trigger_path(trigger)
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


# ── The idle auto-pause ──────────────────────────────────────────────────────────────────────────


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
            log.exception("trigger idle rule: failed for trigger %s", trigger_id)
    return stopped
