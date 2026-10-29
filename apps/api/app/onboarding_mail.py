"""The one send helper every onboarding email goes through: verify, account exists, application
received, welcome, password reset and the admins' new-application alert.

The words are the type's active template (`app/email_template_store.py`, edited in Settings > Integrations >
Email templates), else its built-in defaults; the layout is code (`app/email_templates.py::render`). Every
type gets `tutorials_url` from the Sign-up settings here, so a sender never passes it and a change to the
setting reaches every email at once.

`send` never raises: an onboarding email rides a request that must not fail on it (an application, a sign-up
step), so a fault is an `Outcome` plus a log line — one that names the type and never a recipient or a value.
"""
from __future__ import annotations

import logging
from typing import Literal, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import email_template_store, email_templates, system_mail
from app.operator_settings import reads as settings_reads
from app.durable import notify_outbox
from app.models.identity import AdminUser

log = logging.getLogger("app.onboarding_mail")

#: `app/user_invite.py::invitation_outcome`'s vocabulary, less `rate_limited` (each caller owns its limits).
Outcome = Literal["sent", "not_configured", "failed"]

TUTORIALS_KNOB = "signup_tutorials_url"
ALERT_RECIPIENTS_KNOB = "signup_alert_recipients"


def _active_fields(type_key: str) -> dict[str, str]:
    """On a short session of its own: a background send holds none. A read fault sends the defaults."""
    from app.db import SessionLocal

    try:
        with SessionLocal() as session:
            return email_template_store.active_fields(session, type_key)
    except Exception:
        log.exception("onboarding mail: the %s template could not be read — using the built-in one", type_key)
        return email_templates.TYPES[type_key].defaults


def send(type_key: str, to: str, values: Mapping[str, str], *, button_url: Optional[str]) -> Outcome:
    """Email `to` the `type_key` onboarding email. `values`: its placeholders, raw text (`tutorials_url` is
    set here, whatever the caller passed). `button_url` None means the value of the type's button
    placeholder — what `application_received` (whose button is the tutorials link) relies on."""
    try:
        if not system_mail.configured():
            return "not_configured"
        ttype = email_templates.TYPES[type_key]
        filled = {**values, "tutorials_url": settings_reads.get_str(TUTORIALS_KNOB)}
        if button_url is None and ttype.button:
            button_url = filled.get(ttype.button) or None
        rendered = email_templates.render(type_key, _active_fields(type_key), filled, button_url=button_url)
        # Durable and asynchronous (`durable/notify_outbox.py`): `sent` means handed off; SES is called there.
        if not notify_outbox.email(
            to, rendered.subject, rendered.text, rendered.html, what=type_key, mail_type=type_key,
            # PRD R6's flag (`email_templates.TemplateType.essential`). The admins' new-application alert is optional
            # but goes to configured addresses, not under a person's preference: no target, so no unsubscribe link.
            essential=ttype.essential,
            # The code and the button's link (a verify or reset token) never reach the delivery log's subject.
            secrets=[filled.get(ttype.code, "") if ttype.code else "", button_url or ""],
        ):
            return "failed"
    except Exception as exc:
        # The class only: an unexpected exception's text may quote a value.
        log.error("onboarding mail: %s not sent — raised %s", type_key, type(exc).__name__)
        return "failed"
    return "sent"


def send_after_answer(type_key: Optional[str], to: str, values: Mapping[str, str], button_url: Optional[str]) -> None:
    """`send`, as a door's post-answer work (`durable/notify_outbox.py::after_answer`). `type_key` None sends
    nothing: a door whose every branch must cost the same (R-X1) hands off work on the branch that mails nobody."""
    if type_key is not None:
        send(type_key, to, values, button_url=button_url)


def recipients_for_alert(session: Session) -> list[str]:
    """Who the new-application alert goes to: the Sign-up setting's addresses, else every enabled
    superadmin whose login email a sign-in has proven (`email_verified_at`, the rule
    `notify_targets.email_destination` holds), never the system actor."""
    configured = settings_reads.get_str(ALERT_RECIPIENTS_KNOB)
    addresses = [part.strip() for part in configured.split(",") if part.strip()]
    if not addresses:
        addresses = list(session.scalars(
            select(AdminUser.email)
            .where(
                AdminUser.is_superadmin.is_(True),
                AdminUser.disabled_at.is_(None),
                AdminUser.is_system.is_(False),
                AdminUser.email_verified_at.is_not(None),
            )
            .order_by(AdminUser.email)
        ))
    # Case-insensitive de-duplication, first spelling kept: one inbox, one alert.
    unique: dict[str, str] = {}
    for address in addresses:
        unique.setdefault(address.lower(), address)
    return list(unique.values())
