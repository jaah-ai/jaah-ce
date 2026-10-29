"""An org's display name as a notification carries it — the one home for naming the org a message is about.

A person may belong to several orgs (one `admin_users` row each), and every org's notifications keep reaching
them on that org's own channels whichever org they are acting in. So each message names its org: the task
headline (`notify_text.build_task_line`, via the trusted `payload["org"]`), the question card's header, a
ticket's line, a billing notice. `org_name` is the loader every producer calls; `org_label` is the operator
alarm's form of the same name. Both answer the READABLE name, never a slug or a clipped fragment.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger("app.org_names")


def org_name(session: Session, org_id: Optional[UUID]) -> Optional[str]:
    """`org_id`'s display name, raw (tenant-written: each renderer inerts it for its own surface), or `None`
    for a NULL id or one with no `orgs` row — a message then simply names no org rather than a wrong one.

    A `select`, which the read seam does not constrain (`orgs` has no `org_id`), so it answers from any
    session — including a recipient's bound to another org. Never raises: the name is a courtesy, so a failed
    read costs the message its org, never the message. In a savepoint, so the failure does not abort the
    caller's transaction either."""
    if org_id is None:
        return None
    from app.models.orgs import Org

    try:
        with session.begin_nested():
            return session.scalar(select(Org.name).where(Org.id == org_id)) or None
    except Exception:  # noqa: BLE001 — see the docstring
        log.warning("org name for %s could not be read — the message names no org", org_id, exc_info=True)
        return None


def org_lead(name: Optional[str]) -> str:
    """`"<name> · "` to lead a one-line message that has no task headline (a ticket's DM, a billing notice),
    neutralized and bounded exactly as the headline's org (`notify_text.build_task_line`), so `Acme.io` stays
    readable; `""` for no name."""
    from app import notify_text
    from app.alert_relay import _escape_mrkdwn  # pyright: ignore[reportPrivateUsage]

    text = notify_text._fit((name or "").replace("|", ""), notify_text.ORG_MAX, _escape_mrkdwn)  # pyright: ignore[reportPrivateUsage]
    return f"{text} · " if notify_text._has_glyph(text) else ""  # pyright: ignore[reportPrivateUsage]


def org_label(name: Optional[str], org_id: UUID) -> str:
    """A tenant's name as an operator alarm may carry it: tenant-written, so inert in Slack mrkdwn and bounded."""
    from app.alert_relay import inert_mrkdwn

    return inert_mrkdwn(name, limit=80) if name else str(org_id)
