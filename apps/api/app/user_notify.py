"""Telling one named person something: THE per-person notification choke point.

A notifier hands over the person, the event and its `event_name` (a catalog event, `app/notification_catalog.py`)
and never names a channel: `deliver` takes none. The choke point sends on every one of the person's channels
(`app/notification_channels.py`) that wants the event — Slack (`notify_targets.targets_for_user`), email
(`email_notify.deliver_to_user`) and Telegram (`telegram_notify.deliver_to_user`) — each at its own level.
`important=True` forces the email leg: it goes to the verified login email whatever the Email channels say
(`email_notify.deliver_important`); the other legs still follow their settings. No recipient chain and no
fall-through: an unresolved channel is skipped, never sent to a shared channel.

THE IN-APP HALF IS NOT SENT HERE. There is no notifications table (`app/routes/inbox.py`, G6): the bell derives
each kind from the notifier's own rows, so a notifier that needs an in-app item writes its own row and the inbox
grows a kind that reads it.

Every call stages ONE `notification_sent` activity row for the person (`notify_targets.notification_sent`, shown on
`/audit`): `event_name`, `important` and each channel's outcome — a person whose every channel is off still gets
one, each channel `skipped: …-muted`. The caller's session carries it; the caller commits.
Every leg is best-effort and the call never raises on a send fault: the sends themselves are durable
(`app/durable/notify_outbox.py`).

The older notifiers still hand-roll their per-person legs (`comment_notify.notify_mentioned`,
`governance/notify.py`) and are not routed here yet.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app import alert_relay, edition_features, email_notify, notify_targets
from app.config import get_settings
from app.durable import notify_outbox
from app.models.identity import AdminUser

# The community edition has no Telegram: `_telegram` returns before this name is read.
if edition_features.enabled("telegram"):
    from app import telegram_notify

log = logging.getLogger("app.user_notify")

#: The `activity_events.kind` each call records.
ACTIVITY_KIND = notify_targets.NOTIFICATION_SENT

#: The Slack leg while the relay is switched off or the event is test-flagged (the email and Telegram legs say
#: `no_email_config` / `no_telegram_config` for the same deployment state).
RELAY_OFF = "skipped: relay_off"

#: Every leg of a call to a disabled membership or a system row: nobody is told about an org they were removed from.
RECIPIENT_DISABLED = "skipped: recipient_disabled"


@dataclasses.dataclass(frozen=True)
class Delivery:
    """One call's outcome. `channels`: every channel's `{channel_id, type, outcome}` — `sent` (handed to the durable
    outbox, `app/durable/notify_outbox.py`, whose own log records a later vendor refusal), `skipped: <reason>` (the
    deployment or the channel's settings rule it out) or `failed` (a destination resolved and the hand-off did not
    happen). `slack`/`email`/`telegram`: the first channel of that type's outcome, as before channels."""

    slack: str
    email: str
    telegram: str
    channels: tuple[dict[str, Any], ...] = ()

    def as_data(self) -> dict[str, str]:
        return {"slack": self.slack, "email": self.email, "telegram": self.telegram}


def deliver(
    session: Session, user: AdminUser, event: dict[str, Any], *, event_name: str, important: bool = False,
    subject: Optional[dict[str, Any]] = None,
) -> Delivery:
    """Send `event` to `user` on every channel of theirs that wants `event_name`; with `important`, email the login
    address always.

    `event` is an `alert_relay.message(...)` event; `subject` rides on the activity row (`notify_targets.
    notification_sent`). Returns the `Delivery` and stages its `notification_sent` row on
    `session` (the caller commits). The sends are handed off at once, so a caller that rolls back afterwards loses
    the row, not the notice."""
    rows: list[dict[str, Any]] = []
    if user.disabled_at is not None or user.is_system:
        rows = [{"channel_id": None, "type": kind, "outcome": RECIPIENT_DISABLED}
                for kind in (notify_targets.SLACK, notify_targets.EMAIL, notify_targets.TELEGRAM)]
    else:
        outcomes = notify_targets.Outcomes()
        _slack(session, user, event, event_name, outcomes)
        _email(session, user, event, event_name, important, outcomes)
        _telegram(session, user, event, event_name, outcomes)
        rows = outcomes.rows_for(user)
    session.add(notify_targets.notification_sent(user, event_name, rows, important=important, subject=subject))
    return Delivery(
        slack=_first(rows, notify_targets.SLACK), email=_first(rows, notify_targets.EMAIL),
        telegram=_first(rows, notify_targets.TELEGRAM), channels=tuple(rows),
    )


def _first(rows: list[dict[str, Any]], channel_type: str) -> str:
    """The first `sent`/`failed` outcome of `channel_type`, else its first skip."""
    of_type = [row["outcome"] for row in rows if row["type"] == channel_type]
    return next((outcome for outcome in of_type if not outcome.startswith("skipped")), of_type[0] if of_type else "")


def _slack(
    session: Session, user: AdminUser, event: dict[str, Any], event_name: str, outcomes: notify_targets.Outcomes
) -> None:
    if not get_settings().alert_relay_enabled or alert_relay.is_test_flagged(event):
        outcomes.outcome(user, notify_targets.SLACK, None, RELAY_OFF)
        return
    try:
        targets, _reason = notify_targets.targets_for_user(session, user, event=event_name, outcomes=outcomes)
    except Exception:
        log.exception("user notify: the Slack leg of %s failed for %s", event_name, user.id)
        outcomes.outcome(user, notify_targets.SLACK, None, "failed")
        return
    for target in targets:
        try:
            ok = notify_outbox.slack(event, target)
        except alert_relay.AlertRelayError:
            log.warning("user notify: the Slack leg of %s failed for %s", event_name, user.id)
            ok = False
        except Exception:
            log.exception("user notify: the Slack leg of %s failed for %s", event_name, user.id)
            ok = False
        outcomes.outcome_by_id(user, notify_targets.SLACK, target.channel_id, "sent" if ok else "failed")


def _email(
    session: Session, user: AdminUser, event: dict[str, Any], event_name: str, important: bool,
    outcomes: notify_targets.Outcomes,
) -> None:
    try:
        if important:
            outcomes.outcome(user, notify_targets.EMAIL, _account_email(session, user),
                             email_notify.deliver_important(user, event, event_name=event_name))
            return
        if not email_notify.can_send(event):
            outcomes.outcome(user, notify_targets.EMAIL, None, "skipped: no_email_config")
            return
        email_notify.deliver_to_user(user, event, event_name=event_name, outcomes=outcomes, session=session)
    except Exception:
        log.exception("user notify: the email leg of %s failed for %s", event_name, user.id)
        outcomes.outcome(user, notify_targets.EMAIL, None, "failed")


def _account_email(session: Session, user: AdminUser) -> Any:
    """The account-email channel, so an important notice's outcome names it; None when it was removed."""
    from app import notification_channels

    return next((row for row in notification_channels.channels_of(session, user, notify_targets.EMAIL)
                 if row.account_email), None)


def _telegram(
    session: Session, user: AdminUser, event: dict[str, Any], event_name: str, outcomes: notify_targets.Outcomes
) -> None:
    if not edition_features.enabled("telegram"):
        outcomes.outcome(user, notify_targets.TELEGRAM, None, f"skipped: {notify_targets.REASON_CHANNEL_NOT_TELEGRAM}")
        return
    try:
        if not telegram_notify.can_send(event):
            outcomes.outcome(user, notify_targets.TELEGRAM, None, "skipped: no_telegram_config")
            return
        telegram_notify.deliver_to_user(user, event, event_name=event_name, outcomes=outcomes, session=session)
    except Exception:
        log.exception("user notify: the Telegram leg of %s failed for %s", event_name, user.id)
        outcomes.outcome(user, notify_targets.TELEGRAM, None, "failed")
