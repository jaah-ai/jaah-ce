"""The one read of a template type's active content, for every sender.

`active_fields` is the contract the senders (`app/user_invite.py`, `app/email_notify.py`) call: the type's
active `email_templates` row re-validated over the built-in defaults, or the defaults when no row is
active. Re-validated on READ, not only trusted from the write: a field added to the type after a row was
saved takes its default, and a row a later code change made invalid (a removed placeholder, a field turned
required) degrades to the defaults with a warning rather than sending a broken email or none.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import email_templates
from app.models.notifications import EmailTemplate

log = logging.getLogger("email-templates")


def active_fields(session: Session, type_key: str) -> dict[str, str]:
    """Every field of `type_key`, from its active row where valid, else the type's built-in defaults."""
    ttype = email_templates.TYPES[type_key]
    row = session.scalars(
        select(EmailTemplate).where(EmailTemplate.type == type_key, EmailTemplate.is_active.is_(True))
    ).one_or_none()
    if row is None:
        return ttype.defaults
    stored = row.fields if isinstance(row.fields, dict) else {}
    # A key the type no longer has is dropped here rather than refusing the whole row for it.
    known = {key: value for key, value in stored.items() if key in ttype.defaults}
    try:
        return email_templates.validate_fields(type_key, known)
    except email_templates.InvalidFields as exc:
        log.warning(
            "email-templates: active %s template %r (%s) no longer validates (%s); sending the built-in "
            "defaults", type_key, row.name, row.id, exc,
        )
        return ttype.defaults
