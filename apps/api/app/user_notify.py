"""Telling one named person something: THE per-person notification choke point.

A notifier hands over the person, the event and its `event_name` (a `preferences.NOTIFICATION_EVENTS` key) and
never names a channel: `deliver` takes none. The choke point resolves the person's own push channels — Slack
(`notify_targets.target_for_user`), email (`email_notify.deliver_to_user`) and Telegram
(`telegram_notify.deliver_to_user`) — each by their Notifications preferences. `important=True` forces the email
leg: it goes to the verified login email whatever the Email method and the event preference say
(`email_notify.deliver_important`); the other legs still follow the preferences. No recipient chain and no
fall-through: an unresolved leg is skipped, never sent to a shared channel.

THE IN-APP HALF IS NOT SENT HERE. There is no notifications table (`app/routes/inbox.py`, G6): the bell derives
each kind from the notifier's own rows, so a notifier that needs an in-app item writes its own row and the inbox
grows a kind that reads it.

Every call stages ONE `notification_sent` activity row for the person (`activity_events`, shown on `/audit`):
`event_name`, `important` and each channel's outcome. The caller's session carries it; the caller commits.
Every leg is best-effort and the call never raises on a send fault: the sends themselves are durable
(`app/durable/notify_outbox.py`).

The older notifiers still hand-roll their per-person legs (`comment_notify.notify_mentioned`,
`governance/notify.py`) and are not routed here yet.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

from sqlalchemy.orm import Session

from app import alert_relay, edition_features, email_notify, notify_targets
from app.config import get_settings
from app.durable import notify_outbox
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser

# The community edition has no Telegram: `_telegram` returns before this name is read.
if edition_features.enabled("telegram"):
    from app import telegram_notify

log = logging.getLogger("app.user_notify")

#: The `activity_events.kind` each call records.
ACTIVITY_KIND = "notification_sent"

#: The Slack leg while the relay is switched off or the event is test-flagged (the email and Telegram legs say
#: `no_email_config` / `no_telegram_config` for the same deployment state).
RELAY_OFF = "skipped: relay_off"

#: Every leg of a call to a disabled membership or a system row: nobody is told about an org they were removed from.
RECIPIENT_DISABLED = "skipped: recipient_disabled"


@dataclasses.dataclass(frozen=True)
class Delivery:
    """One call's per-channel outcome. `sent`: handed to the durable outbox (`app/durable/notify_outbox.py`, whose
    own log records a later vendor refusal). `skipped: <reason>`: the deployment or the person's settings rule the
    leg out. `failed`: the leg resolved a destination and the hand-off did not happen."""

    slack: str
    email: str
    telegram: str

    def as_data(self) -> dict[str, str]:
        return dataclasses.asdict(self)


def deliver(
    session: Session, user: AdminUser, event: dict[str, Any], *, event_name: str, important: bool = False
) -> Delivery:
    """Send `event` to `user` on every channel they configured for `event_name`; with `important`, email always.

    `event` is an `alert_relay.message(...)` event. Returns the per-channel `Delivery` and stages its
    `notification_sent` row on `session` (the caller commits). The sends are handed off at once, so a caller that
    rolls back afterwards loses the row, not the notice."""
    if user.disabled_at is not None or user.is_system:
        delivery = Delivery(slack=RECIPIENT_DISABLED, email=RECIPIENT_DISABLED, telegram=RECIPIENT_DISABLED)
    else:
        delivery = Delivery(
            slack=_slack(session, user, event, event_name),
            email=_email(user, event, event_name, important=important),
            telegram=_telegram(user, event, event_name),
        )
    _record(session, user, event_name, important, delivery)
    return delivery


def _slack(session: Session, user: AdminUser, event: dict[str, Any], event_name: str) -> str:
    if not get_settings().alert_relay_enabled or alert_relay.is_test_flagged(event):
        return RELAY_OFF
    try:
        target, reason = notify_targets.target_for_user(session, user, event=event_name)
        if target is None:
            return f"skipped: {reason}"
        return "sent" if notify_outbox.slack(event, target) else "failed"
    except alert_relay.AlertRelayError:
        log.warning("user notify: the Slack leg of %s failed for %s", event_name, user.id)
    except Exception:
        log.exception("user notify: the Slack leg of %s failed for %s", event_name, user.id)
    return "failed"


def _email(user: AdminUser, event: dict[str, Any], event_name: str, *, important: bool) -> str:
    try:
        if important:
            return email_notify.deliver_important(user, event, event_name=event_name)
        if not email_notify.can_send(event):
            return "skipped: no_email_config"
        destination, reason = notify_targets.email_destination(user, event=event_name)
        if destination is None:
            return f"skipped: {reason}"
        return "sent" if email_notify.deliver_to_user(user, event, event_name=event_name) else "failed"
    except Exception:
        log.exception("user notify: the email leg of %s failed for %s", event_name, user.id)
        return "failed"


def _telegram(user: AdminUser, event: dict[str, Any], event_name: str) -> str:
    if not edition_features.enabled("telegram"):
        return f"skipped: {notify_targets.REASON_CHANNEL_NOT_TELEGRAM}"
    try:
        if not telegram_notify.can_send(event):
            return "skipped: no_telegram_config"
        destination, reason = notify_targets.telegram_destination(user, event=event_name)
        if destination is None:
            return f"skipped: {reason}"
        return "sent" if telegram_notify.deliver_to_user(user, event, event_name=event_name) else "failed"
    except Exception:
        log.exception("user notify: the Telegram leg of %s failed for %s", event_name, user.id)
        return "failed"


def _record(session: Session, user: AdminUser, event_name: str, important: bool, delivery: Delivery) -> None:
    """Stage the call's one `notification_sent` row in the person's membership org. The system acted, so no actor
    (`user_id` is the actor on `/audit`, as `task_notify` files its delivery notes); the person is `recipient_id`."""
    legs = ", ".join(f"{channel} {outcome}" for channel, outcome in delivery.as_data().items())
    session.add(ActivityEvent(
        user_id=None,
        action_type="write",
        kind=ACTIVITY_KIND,
        summary=f"{event_name} notification{' (important)' if important else ''}: {legs}",
        data={
            "recipient_id": str(user.id), "event_name": event_name, "important": important,
            "channels": delivery.as_data(),
        },
        org_id=user.org_id,
    ))
