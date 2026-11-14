"""The email leg of a notification: the same events Slack carries, sent as system mail (`app/system_mail.py`, SES).

A channel type beside Slack and Telegram (`notification_channels`), not a fallback for either: a person with
several channels gets each. Each leg walks the same recipient chain (`notify_targets.alert_candidates`) on its own,
so a candidate with no Email channel wanting the event is skipped exactly as one without Slack is. Each Email
channel is the login email or an address the person proved (`notify_targets.email_routes`).

Callers: `alert_record.record_alert` (task transitions, questions, the shipped and assignment
cards), `comment_notify.notify_mentioned`, the per-person choke point `user_notify.deliver` (whose important notices
use `deliver_important`), a billing notice's durable email effect (`deliver_billing`, from
`app/billing/notices.py::_email`), which writes to each of an org's Owner/Admins — no chain — and fans the same
words out over Telegram (`_over_telegram`, as `deliver_billing_message` does), and
the trigger owner's notices (`prepare_template_email`, from `app/trigger_notices.py`), one named person each.
Email NEVER falls through to a shared channel — an alert with no email recipient simply sends no email —
and a send failure is logged, never raised: the Slack leg and the alert row do not depend on it. Rendering is
here, on the caller's thread; the send itself is durable and asynchronous (`durable/notify_outbox.py`).

Each email is plain text plus HTML. The message is untrusted task prose, re-rendered through
the relay's own plain-text functions so the email names the subject exactly as the Slack message and the
webhook do; the active `notification` template (`app/email_template_store.py`) supplies the subject, the
button's label and the footer, and `app/email_templates.py::render_notification` wraps it in the layout,
escaping the prose in the HTML part.
"""
from __future__ import annotations

import html
import logging
from typing import Any, Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app import alert_relay, config, edition_features, email_template_store, email_templates, mail_message, notify_targets, org_names, system_mail
from app.config import get_settings
from app.durable import notify_outbox
from app.models.billing import BillingNotice
from app.models.identity import AdminUser

log = logging.getLogger("app.email_notify")

# The built-in footer, which the operator's template may replace.
FOOTER = email_templates.NOTIFICATION.field("footer").default


def console_url(event: dict[str, Any]) -> Optional[str]:
    """The console link for this event, through the relay's origin check, or `None`.

    A producer's own `payload["console_url"]` first (a mention), else the Task's page off the trusted
    payload id — never `event["task_id"]`, which any `NODE_TOKEN` holder may set to scope dedup."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    url = payload.get("console_url")
    task_id = payload.get("task_id")
    if not url and isinstance(task_id, int) and not isinstance(task_id, bool):
        from app.trackers.writeback import task_url

        url = task_url(task_id)
    return alert_relay._console_link(str(url)) if url else None


def _notification_fields() -> dict[str, str]:
    """The active `notification` template's fields, on a short session of their own (the callers hold
    none, or one bound to another org). A read fault falls back to the built-in text rather than losing
    the email."""
    from app.db import BridgedSession

    try:
        with BridgedSession() as session:
            return email_template_store.active_fields(session, email_templates.NOTIFICATION.key)
    except Exception:
        log.exception("email notify: the notification template could not be read — using the built-in one")
        return email_templates.NOTIFICATION.defaults


def plain_parts(event: dict[str, Any]) -> tuple[str, str]:
    """One event as plain text: its headline and the message after it, through the relay's own plain-text
    functions. Shared by every plain-text leg (this one and `app/telegram_notify.py`)."""
    # The mention producer pre-escapes its excerpt for Slack mrkdwn (`alert_relay.inert_mrkdwn`), and
    # `&lt;` would print literally in a plain-text email, where `<` is inert. Only that field: a title
    # someone typed as `&amp;` must print as typed.
    payload = event.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("question_body"), str):
        event = {**event, "payload": {**payload, "question_body": html.unescape(payload["question_body"])}}
    if event.get("kind") == "mention" and isinstance(event.get("reason"), str):
        # A ticket mention carries its escaped excerpt in `reason` (`comment_notify._ticket_event`).
        event = {**event, "reason": html.unescape(event["reason"])}
    full_text = alert_relay.format_message(event)
    headline = alert_relay.task_headline(event) or full_text.splitlines()[0]
    # `format_message` leads with the headline; the message is what follows it, so the card never shows
    # the headline twice. A text that does not lead with it (no producer today) is kept whole.
    rest = full_text[len(headline):].lstrip("\n") if full_text.startswith(headline) else full_text
    if event.get("kind") == "support" and rest:
        # A ticket email's `reason` is its own plain message, headline first (`durable/support_notify_queue.py::
        # email_event`): the fact, not the kind's word, is the subject.
        headline, _, rest = rest.partition("\n")
    return headline, rest


def compose(
    event: dict[str, Any], *, detail: str, fields: Optional[dict[str, str]] = None,
    unsubscribe_url: Optional[str] = None,
) -> email_templates.Rendered:
    """The subject, text and HTML for one event, in `fields` (a validated `notification` field set; the
    built-in one when omitted). `brief` keeps the headline and link and drops the prose, as the Slack
    leg's `_brief_text` does."""
    headline, rest = plain_parts(event)
    return email_templates.render_notification(
        fields if fields is not None else email_templates.NOTIFICATION.defaults,
        headline=headline,
        message="" if detail == "brief" else rest,
        link=console_url(event),
        unsubscribe_url=unsubscribe_url,
    )


def _may_send(event: dict[str, Any]) -> bool:
    """Whether this event may go out at all (quietly False: most local stacks and every test run have email off)."""
    if not get_settings().alert_relay_enabled or alert_relay.is_test_flagged(event):
        return False
    return system_mail.configured()


def _send(
    address: str, event: dict[str, Any], detail: str, fields: dict[str, str], *, user: Optional[AdminUser] = None,
    event_name: Optional[str] = None, essential: bool = False, recipient_user_id: Optional[UUID] = None,
    channel_id: Optional[UUID] = None,
) -> bool:
    """Render this event's email now and hand the send to the outbox (SES settings are re-read there). `user`,
    `event_name` and `channel_id`, when known, name which channel's setting an unsubscribe turns off; `essential` is
    PRD R6's flag."""
    from app import email_unsubscribe

    target = (
        email_unsubscribe.UnsubscribeTarget(user_id=user.id, event=event_name, channel_id=channel_id)
        if user is not None and event_name else None
    )
    try:
        url = None if essential else email_unsubscribe.url_for(target)
        rendered = compose(event, detail=detail, fields=fields, unsubscribe_url=url)
    except Exception:
        log.exception("email notify: kind=%s could not be rendered — not sent", event.get("kind"))
        return False
    what = f"kind={event.get('kind')}"
    return notify_outbox.email(
        address, rendered.subject, rendered.text, rendered.html, what=what,
        mail_type=email_templates.NOTIFICATION.key, essential=essential, unsubscribe=None if essential else target,
        recipient_user_id=recipient_user_id,
    )


def _send_event(event: dict[str, Any], user: AdminUser, route: notify_targets.Route, event_name: str) -> bool:
    """`notify_targets.Send` for this leg: the template is read once a recipient is found."""
    return _send(
        route.address, event, route.level, _notification_fields(), user=user, event_name=event_name,
        channel_id=route.channel.id if route.channel is not None else None,
    )


#: This leg's entry points (`notify_targets.Leg`): the gate and the resolver are read through their modules at call
#: time, so a test's patch of either holds. What differs from Telegram: a person may unsubscribe one channel from
#: one event's email (`_send`'s target), unless the mail is essential.
LEG = notify_targets.Leg(
    lambda event: _may_send(event), lambda: notify_targets.email_routes, _send_event, notify_targets.EMAIL
)
deliver_alert = LEG.deliver_alert
deliver_to_user = LEG.deliver_to_user
can_send = LEG.can_send
routes = LEG.routes


def deliver_important(user: AdminUser, event: dict[str, Any], *, event_name: str) -> str:
    """Email one named person an IMPORTANT notice (`app/user_notify.py`): to the login email whatever their Email
    channels say — never to a typed address. The address must still be proven (`AdminUser.email_verified_at`, as in
    `notify_targets.email_routes`), a disabled membership or a pending invitation is still sent nothing, and the
    account-email channel's brief choice for `event_name` (when it has one) still shapes the body. Returns the
    outcome: `sent`, `skipped: <reason>` or `failed`. Never raises."""
    if not _may_send(event):
        return "skipped: no_email_config"
    if user.disabled_at is not None:
        return "skipped: recipient_disabled"
    if user.invitation_id is not None:
        return f"skipped: {notify_targets.REASON_RECIPIENT_MISSING}"
    address = mail_message.single_address(user.email or "")
    if address is None:
        return f"skipped: {notify_targets.REASON_RECIPIENT_MISSING}"
    if user.email_verified_at is None:
        return f"skipped: {notify_targets.REASON_EMAIL_UNVERIFIED}"
    detail = _important_detail(user, event_name)
    # Important: sent whatever the preference says, so essential — nothing to unsubscribe from.
    return "sent" if _send(address, event, detail, _notification_fields(), essential=True, recipient_user_id=user.id) else "failed"


def _important_detail(user: AdminUser, event_name: str) -> str:
    """`brief` when the account-email channel resolves `event_name` brief, else `full` (an important notice is never
    shortened by a channel being off or absent)."""
    try:
        resolved = notify_targets.email_routes(user, event=event_name)
    except Exception:
        log.exception("email notify: the brief/full choice for %s could not be read — sending full", event_name)
        return "full"
    account = next((route for route in resolved.routes if route.channel is not None and route.channel.account_email), None)
    return "brief" if account is not None and account.level == "brief" else "full"


BILLING_EVENT = "billing"
#: A project or group budget at 80% or 100% (`app/budgets.py`): to the same people as `billing`, but optional mail
#: under its own preference, so unsubscribing from it never silences a bill.
BUDGET_EVENT = "budget"
BILLING_FOOTER = "Sent to your organization's Owners and Admins because Billing is on for this address in Profile > Alerts."


def console_path_link(path: str) -> Optional[str]:
    """A console path (`/automations/<id>`) as an absolute link on this deployment's own origin, or None."""
    if not path.startswith("/") or path.startswith("//"):
        return None
    return alert_relay._console_link(config.console_url(path))


def billing_link(href: Any) -> Optional[str]:
    """A notice action's absolute link: a console path through this deployment's own origin, or the stripe.com
    https URL it already is (`notices.hosted_invoice_link` admitted it). Anything else is no link."""
    if not isinstance(href, str):
        return None
    if href.startswith("/") and not href.startswith("//"):
        return console_path_link(href)
    # The community edition has no billing, so no Stripe invoice link.
    if not edition_features.enabled("billing"):
        return None
    from app.billing.notices import hosted_invoice_link

    return hosted_invoice_link(href)


def compose_billing(
    notice: BillingNotice, *, detail: str, fields: Optional[dict[str, str]] = None, org: Optional[str] = None
) -> email_templates.Rendered:
    """The subject, text and HTML for one billing notice, from the copy `app/billing/notices.py` composed —
    the same words the bell and the banner show — in the `notification` template's layout (`fields`, the
    built-in one when omitted). The button is the notice's own action and the footer says why an
    Owner/Admin got it. `brief` keeps the headline and the button. `org` (`org_names.org_name`) leads the
    headline: an Owner of several orgs reads whose bill it is."""
    headline, text, label, link = billing_parts(notice, org=org)
    base = fields if fields is not None else email_templates.NOTIFICATION.defaults
    return email_templates.render_notification(
        {**base, "button_label": label or base["button_label"], "footer": BILLING_FOOTER},
        headline=headline,
        message="" if detail == "brief" else text,
        link=link,
    )


def billing_parts(notice: BillingNotice, *, org: Optional[str] = None) -> tuple[str, str, str, Optional[str]]:
    """One billing notice's headline (led by `org`), text, action label and action link — the words every leg
    sends it in (this one and `app/telegram_notify.py`)."""
    payload = notice.payload or {}
    headline = str(payload.get("headline") or payload.get("text") or "Billing update")
    org_text = " ".join((org or "").split())
    if org_text:
        headline = f"{org_text} · {headline}"
    action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
    return headline, str(payload.get("text") or ""), str(action.get("label") or ""), billing_link(action.get("href"))


def deliver_billing(
    session: Session, notice: BillingNotice, *, recipients: Optional[list[AdminUser]] = None
) -> tuple[int, int]:
    """Email one billing notice to each of its org's Owner/Admins who has Email on and wants `billing`
    (`recipients`, when the caller already read them for this org), then over Telegram in the same words
    (`_over_telegram`). Returns `(sent, failed)` for the emails, failed counting sends the outbox could not take.
    Never raises: the notice is already in the bell."""
    sent, failed = _deliver_to_billing_people(
        session, notice.org_id, recipients, notice.kind,
        lambda detail, fields, org, _unsubscribe_url: compose_billing(notice, detail=detail, fields=fields, org=org),
    )
    # Telegram goes on the attempt that ends the effect: `billing/notices._email` raises when every email failed,
    # so the outbox retries it, and a retried email must never send the Telegram message twice.
    if sent or not failed:
        try:
            headline, text, _label, link = billing_parts(notice)
        except Exception:  # noqa: BLE001 - the emails are out; a retry must not resend them
            log.exception("email notify: billing notice %s could not be worded for Telegram — not sent", notice.kind)
        else:
            _over_telegram(session, notice.org_id, headline=headline, text=text, link=link)
    return sent, failed


def deliver_billing_message(
    session: Session, org_id: UUID, *, headline: str, text: str, button_label: str, href: str,
    recipients: Optional[list[AdminUser]] = None, also: Optional[str] = None, event: str = BILLING_EVENT,
) -> tuple[int, int]:
    """Email the org's Owner/Admins one billing message that is not a notice (an operator's request to add a
    payment method, `routes/billing_orgs.py`; a budget warning, `app/budgets.py`, with `event` `BUDGET_EVENT`):
    `href` is a console path, the button its label. Same recipients and rules as `deliver_billing`, under `event`'s
    preference; `also` (the Stripe customer's billing email, which the operator chose to add) gets the full message
    too unless a person already got it at that address; then over Telegram (`_over_telegram`). Returns
    `(sent, failed)` for the emails. Never raises."""
    def compose(
        detail: str, fields: dict[str, str], org: Optional[str], unsubscribe_url: Optional[str]
    ) -> email_templates.Rendered:
        org_text = " ".join((org or "").split())
        return email_templates.render_notification(
            {**fields, "button_label": button_label, "footer": BILLING_FOOTER},
            headline=f"{org_text} · {headline}" if org_text else headline,
            message="" if detail == "brief" else text,
            link=console_path_link(href),
            unsubscribe_url=unsubscribe_url,
        )

    what = "budget warning" if event == BUDGET_EVENT else "billing message"
    sent, failed = _deliver_to_billing_people(session, org_id, recipients, what, compose, also=also, event=event)
    _over_telegram(session, org_id, headline=headline, text=text, link=console_path_link(href), event=event)
    return sent, failed


def _over_telegram(
    session: Session, org_id: UUID, *, headline: str, text: str, link: Optional[str], event: str = BILLING_EVENT,
) -> None:
    """The Telegram leg of a billing send (`telegram_notify.to_billing_people`), the headline led by the org's name,
    behind the ONE edition gate: the community edition has no `app/telegram_notify.py`. Never raises."""
    if not edition_features.enabled("telegram"):
        return
    from app import telegram_notify  # here, not at the top: `telegram_notify` imports this module

    org = " ".join((org_names.org_name(session, org_id) or "").split())
    telegram_notify.to_billing_people(
        session, org_id, headline=f"{org} · {headline}" if org else headline, text=text, link=link, event=event,
    )


def billing_mail_on() -> bool:
    """Whether billing email may go out at all — the ONE deployment gate both the send and the operator's recipient
    preview (`routes/billing_orgs.py`) read, so a preview never names people unconfigured system mail would not
    reach."""
    return _may_send({"kind": BILLING_EVENT})


def billing_destinations(recipients: list[AdminUser], event: str = BILLING_EVENT) -> list[tuple[str, str]]:
    """`(address, detail)` for every Email channel of `recipients` a billing email reaches
    (`notify_targets.email_routes`: a proven address, `event` — `billing` unless a budget warning's — not off on
    that channel) — the ONE rule the send, the operator's recipient preview (`routes/billing_orgs.py`) and
    `plan_rollover.unsent_reason` read. A lookup that raises skips that person, logged."""
    return [(address, detail) for _user, address, detail, _channel in _billing_people(recipients, event)]


def _billing_people(
    recipients: list[AdminUser], event: str
) -> list[tuple[Optional[AdminUser], str, str, Optional[UUID]]]:
    """`billing_destinations`' rule under `event`, keeping who each address is and its channel (an optional event's
    unsubscribe)."""
    found: list[tuple[Optional[AdminUser], str, str, Optional[UUID]]] = []
    for user in recipients:
        try:
            resolved = notify_targets.email_routes(user, event=event)
        except Exception:
            log.exception("email notify: recipient lookup failed for a billing notice — skipped")
            continue
        for route in resolved.routes:
            found.append((user, route.address, route.level, route.channel.id if route.channel is not None else None))
    return found


def _deliver_to_billing_people(
    session: Session, org_id: UUID, recipients: Optional[list[AdminUser]], what: str,
    compose: Callable[[str, dict[str, str], Optional[str], Optional[str]], email_templates.Rendered],
    *, also: Optional[str] = None, event: str = BILLING_EVENT,
) -> tuple[int, int]:
    if not billing_mail_on():
        return 0, 0
    from app import access

    sent = failed = 0
    if recipients is None:
        try:
            recipients = access.owners_and_admins(session, org_id)
        except Exception:
            log.exception("email notify: billing recipients could not be read — not sent")
            return 0, 0
    from app import email_unsubscribe

    fields = _notification_fields()
    org = org_names.org_name(session, org_id)
    destinations = _billing_people(recipients, event)
    extra = mail_message.single_address(also or "")
    if extra is not None and extra.lower() not in {address.lower() for _, address, _, _ in destinations}:
        destinations.append((None, extra, "full", None))
    # Essential (PRD R6): a bill is not a notification a person may unsubscribe from; a budget warning is.
    essential = event == BILLING_EVENT
    for user, address, detail, channel_id in destinations:
        target = None if essential or user is None else email_unsubscribe.UnsubscribeTarget(
            user_id=user.id, event=event, channel_id=channel_id
        )
        try:
            rendered = compose(detail, fields, org, email_unsubscribe.url_for(target))
        except Exception:
            log.exception("email notify: billing notice %s could not be rendered — not sent", what)
            continue
        if notify_outbox.email(
            address, rendered.subject, rendered.text, rendered.html, what=f"billing notice {what}",
            mail_type=email_templates.NOTIFICATION.key, essential=essential, unsubscribe=target,
            recipient_user_id=user.id if user is not None else None,
        ):
            sent += 1
        else:
            failed += 1
    return sent, failed


def prepare_template_email(
    session: Session,
    user: AdminUser,
    *,
    event_name: str,
    type_key: str,
    values: dict[str, str],
    button_url: Optional[str],
) -> Optional[Callable[[], bool]]:
    """One templated email to one named person, rendered NOW (inside the caller's transaction, where `user` is
    loaded) and returned as a `send()` for the caller to run once that transaction commits
    (`after_commit.on_commit`) — so a rolled-back write emails nobody. One email per Email channel that wants
    `event_name`; None when there is none (no channel, an unverified address, the event off). `send()` never
    raises."""
    try:
        resolved = notify_targets.email_routes(user, event=event_name, session=session)
    except Exception:
        log.exception("email notify: recipient lookup failed for %s — not sent", type_key)
        return None
    if not resolved.routes:
        return None
    try:
        fields = email_template_store.active_fields(session, type_key)
    except Exception:
        log.exception("email notify: the %s template could not be read — using the built-in one", type_key)
        fields = email_templates.TYPES[type_key].defaults
    from app import email_unsubscribe

    essential = email_templates.TYPES[type_key].essential
    messages = []
    for route in resolved.routes:
        target = None if essential else email_unsubscribe.UnsubscribeTarget(
            user_id=user.id, event=event_name, channel_id=route.channel.id if route.channel is not None else None,
        )
        rendered = email_templates.render(
            type_key, fields, values, button_url=button_url, unsubscribe_url=email_unsubscribe.url_for(target),
        )
        messages.append((route.address, rendered, target))
    user_id = user.id

    def send() -> bool:
        if not _may_send({"kind": type_key}):
            return False
        sent = False
        for address, rendered, target in messages:
            sent = notify_outbox.email(
                address, rendered.subject, rendered.text, rendered.html, what=type_key, mail_type=type_key,
                essential=essential, unsubscribe=target, recipient_user_id=user_id,
            ) or sent
        return sent

    return send
