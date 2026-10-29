"""Telling somebody they were mentioned in a task or ticket comment — the best-effort Slack DM and email half.

The `task_comment_mentions` row (the in-app notification) is written by
`app/comment_mentions.py::sync_mentions` in the comment's own transaction; this module only DMs.

The recipient is named, never derived: it asks `notify_targets.target_for_user` and sends nothing when
that is empty — falling through to the shared operator webhook would broadcast a remark to the channel.
`mention` is a notification event (`app/preferences.py::NOTIFICATION_EVENTS`), deliberately absent from
`alert_relay.ALERT_KINDS`, and writes no `alerts` row (that table's unique key is a dedup window).

Every failure is caught and logged: a Slack outage must not fail the comment POST.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from typing import Any, Optional

from app import alert_relay, edition_features, email_notify, notify_targets, org_names
from app.config import get_settings
from app.durable import notify_outbox
from app.identities import display_name
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.support import Ticket
from app.models.task_extras import TaskComment
from app.models.tasks import Task

# The community edition has no Telegram: its one read sits behind the same check.
if edition_features.enabled("telegram"):
    from app import telegram_notify

log = logging.getLogger("app.comment_notify")

# Long enough to say what it is about; the DM is a pointer to the console, not a copy.
_EXCERPT = 180

MAX_FANOUT = 10
"""The most people one comment DMs. See `notify_mentioned` for why this is bounded at all."""


def excerpt(body: str) -> str:
    """One comment reduced to a line — what a DM quotes and what the mentions bell shows."""
    flat = " ".join(body.split())
    return flat if len(flat) <= _EXCERPT else flat[: _EXCERPT - 1].rstrip() + "…"


def ticket_console_url(ticket_id: int, comment_id: Optional[int] = None) -> str:
    """A ticket's page in the console (`/support/tickets/<id>`), optionally at one comment."""
    base = f"{get_settings().frontend_url.rstrip('/')}/support/tickets/{ticket_id}"
    return f"{base}#comment-{comment_id}" if comment_id is not None else base


def notify_mentioned(
    session: Session,
    *,
    task: Optional[Task] = None,
    ticket: Optional[Ticket] = None,
    comment: TaskComment,
    author: AdminUser,
    mentioned: list[AdminUser],
) -> int:
    """DM everyone newly named in `comment` on its subject — `task` or `ticket`, exactly one. Returns how
    many were delivered.

    `mentioned` is the new names only (`comment_mentions.sync_mentions`' return, author excluded), so an
    edit notifies only the people it added. A ticket's DM is not a task alert (no task headline): it says
    who mentioned the reader on which ticket; its page's link rides `payload["console_url"]`, which the
    relay draws only beside a task headline today — a ticket card is the support notification's (deferred).
    """
    if (task is None) == (ticket is None):
        raise ValueError("notify_mentioned needs exactly one subject: task or ticket")
    if len(mentioned) > MAX_FANOUT:
        # Bounded because `relay_alert` is a blocking Slack call (with a `Retry-After` retry) running
        # inside the POST; an unbounded mention list would hold a threadpool worker for minutes. Only the
        # DMs are capped — every bell row is already written — and the drop is logged.
        log.warning(
            "mention DM fan-out capped at %s of %s (comment=%s)",
            MAX_FANOUT, len(mentioned), comment.id,
        )
    # Function-local to keep a wide import closure off this path.
    from app.task_external_comment import _console_task_url

    # A `select`, not `session.get`, so the tenancy seam scopes the read.
    # Best-effort, as the rest of the card: a failed read drops the headline's lead, never the fan-out.
    project_name = None
    if task is not None:
        try:
            project_name = session.scalar(select(Project.name).where(Project.id == task.project_id))
        except Exception:  # noqa: BLE001
            log.warning("mention DM: could not read the project of task %s", task.id, exc_info=True)
    # The subject's org, which every DM names: the reader may belong to several orgs.
    subject = task if task is not None else ticket
    org_name = org_names.org_name(session, subject.org_id)  # type: ignore[union-attr]
    delivered = 0
    for user in mentioned[:MAX_FANOUT]:
        # The recipient's own row carries their delivery settings (`notify_targets.target_for_user` reads
        # nothing else of theirs), so an operator named on a customer's ticket resolves like anyone.
        target, reason = notify_targets.target_for_user(session, user, event="mention")
        if ticket is not None:
            event = _ticket_event(ticket, comment, author, org_name)
        else:
            assert task is not None
            # No `url`: that field is the github allowlist's. The console link rides in
            # `payload["console_url"]`, which the relay origin-checks.
            # The line every task alert reads, composed by `alert_relay.task_headline_mrkdwn` from these
            # trusted fields. `question_body` is its body slot; a brief recipient's DM drops it.
            event = alert_relay.message("mention", issue=str(task.id), payload={
                "task_id": task.id,
                "comment_id": comment.id,
                "task_title": task.title,
                "project": project_name,
                "org": org_name,
                "actor": display_name(author),
                # Escaped: tenant prose reaches an mrkdwn field here and the relay only neutralizes URLs,
                # so `<@U01ADMIN>`, `<!here>` or `<!channel>` would become live mentions.
                # `alert_relay.inert_mrkdwn` is the recipe; the author's name goes through it too.
                "question_body": alert_relay.inert_mrkdwn(excerpt(comment.body)),
                "console_url": f"{_console_task_url(task.id)}?tab=comments#comment-{comment.id}",
            })
        # The email and Telegram legs on their own: a user may have either on and no Slack
        # (`app/email_notify.py`, `app/telegram_notify.py`).
        personal = email_notify.deliver_to_user(user, event, event_name="mention")
        if edition_features.enabled("telegram"):
            personal = telegram_notify.deliver_to_user(user, event, event_name="mention") or personal
        if target is None:
            # Ordinary, not an error: most people have no Slack method, and muted is a choice. Debug, so
            # the log is not flooded per comment per colleague.
            log.debug(
                "mention not relayed to %s over Slack (comment=%s reason=%s)", user.id, comment.id, reason
            )
            delivered += int(personal)
            continue
        try:
            if notify_outbox.slack(event, target) or personal:
                delivered += 1
        except alert_relay.AlertRelayError:
            # Best-effort — see the module docstring.
            log.warning("mention DM failed for %s (comment=%s)", user.id, comment.id)
            delivered += int(personal)
    return delivered


def _ticket_event(
    ticket: Ticket, comment: TaskComment, author: AdminUser, org_name: Optional[str] = None
) -> dict[str, Any]:
    """The DM for a mention on a ticket. Tenant prose (title, author, excerpt) is escaped as in the task
    arm; `reason` is the message, since a ticket is not a task and has no task headline. It leads with the
    ticket's org (`org_names.org_lead`)."""
    title = alert_relay.inert_mrkdwn(ticket.title)
    return alert_relay.message(
        "mention",
        f"{org_names.org_lead(org_name)}"
        f"{alert_relay.inert_mrkdwn(display_name(author))} mentioned you on ticket #{ticket.id} {title}: "
        f"{alert_relay.inert_mrkdwn(excerpt(comment.body))}",
        payload={
            "ticket_id": ticket.id,
            "comment_id": comment.id,
            "actor": display_name(author),
            "console_url": ticket_console_url(ticket.id, comment.id),
        },
    )
