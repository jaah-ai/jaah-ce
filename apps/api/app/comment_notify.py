"""Telling somebody they were mentioned in a task or ticket comment — the best-effort Slack DM and email half.

The `task_comment_mentions` row (the in-app notification) is written by
`app/comment_mentions.py::sync_mentions` in the comment's own transaction; this module only DMs.

The recipient is named, never derived: it asks `notify_targets.targets_for_user` and sends nothing when
that is empty — falling through to the shared operator webhook would broadcast a remark to the channel.
`mention` is a notification event (`app/notification_catalog.py`), deliberately absent from
`alert_relay.ALERT_KINDS`, and writes no `alerts` row (that table's unique key is a dedup window).

Every failure is caught and logged: a Slack outage must not fail the comment POST.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from typing import Any, Optional

from app import alert_relay, edition_features, email_notify, notify_targets, org_names
from app.config import console_url
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
    base = console_url(f"/support/tickets/{ticket_id}")
    return f"{base}#comment-{comment_id}" if comment_id is not None else base


def task_comment_console_url(task_id: int, comment_id: int) -> str:
    """A task comment's place in the console (its Comments tab, at the comment), for `payload["console_url"]`,
    which the relay origin-checks; never the github allowlist's `url` field."""
    from app.trackers.writeback import task_url  # function-local: a wide import closure

    return f"{task_url(task_id)}?tab=comments#comment-{comment_id}"


def ticket_dm(
    kind: str, ticket: Ticket, comment: TaskComment, actor: str, sentence: str, org_name: Optional[str],
) -> dict[str, Any]:
    """The DM for `kind` (mention, reaction) on a ticket: `sentence` (already inert mrkdwn) is the reason, since a
    ticket has no task headline, led by its org (`org_names.org_lead`)."""
    return alert_relay.message(kind, f"{org_names.org_lead(org_name)}{sentence}", payload={
        "ticket_id": ticket.id, "comment_id": comment.id, "actor": actor,
        "console_url": ticket_console_url(ticket.id, comment.id),
    })


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
        # The recipient's own channels carry their delivery settings (`notify_targets.targets_for_user` reads
        # nothing else of theirs), so an operator named on a customer's ticket resolves like anyone.
        targets, reason = notify_targets.targets_for_user(session, user, event="mention")
        if ticket is not None:
            event = _ticket_event(ticket, comment, author, org_name)
        else:
            assert task is not None
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
                "console_url": task_comment_console_url(task.id, comment.id),
            })
        # The email and Telegram legs on their own: a user may have either on and no Slack
        # (`app/email_notify.py`, `app/telegram_notify.py`).
        personal = email_notify.deliver_to_user(user, event, event_name="mention", session=session)
        if edition_features.enabled("telegram"):
            personal = telegram_notify.deliver_to_user(user, event, event_name="mention", session=session) or personal
        if not targets:
            # Ordinary, not an error: most people have no Slack method, and muted is a choice. Debug, so
            # the log is not flooded per comment per colleague.
            log.debug(
                "mention not relayed to %s over Slack (comment=%s reason=%s)", user.id, comment.id, reason
            )
            delivered += int(personal)
            continue
        for target in targets:
            try:
                personal = notify_outbox.slack(event, target) or personal
            except alert_relay.AlertRelayError:
                # Best-effort — see the module docstring.
                log.warning("mention DM failed for %s (comment=%s)", user.id, comment.id)
        delivered += int(personal)
    return delivered


def _ticket_event(
    ticket: Ticket, comment: TaskComment, author: AdminUser, org_name: Optional[str] = None
) -> dict[str, Any]:
    """The DM for a mention on a ticket; tenant prose (title, author, excerpt) is escaped as in the task arm."""
    return ticket_dm(
        "mention", ticket, comment, display_name(author),
        f"{alert_relay.inert_mrkdwn(display_name(author))} mentioned you on ticket #{ticket.id} "
        f"{alert_relay.inert_mrkdwn(ticket.title)}: {alert_relay.inert_mrkdwn(excerpt(comment.body))}",
        org_name,
    )


def task_rows_on_own_session(task_id: int, comment_id: int, user_ids: list[Any]):
    """A session of its own with a task comment's rows loaded — `(session, task, comment, users by id)` — for a
    post-commit DM a route on the event loop runs on a worker thread (`app/db_async.py::defer_off_loop`): the DM's
    reads, its delivery rows and its sends must not run on the loop. The caller closes the session. The task and the
    comment are read under the caller's own scope; the people by id, as `require_user` reads who is asking."""
    from app.db import BridgedSession
    from app.tenancy import seam

    session = BridgedSession()
    task = session.scalar(select(Task).where(Task.id == task_id))
    comment = session.scalar(select(TaskComment).where(TaskComment.id == comment_id))
    with seam.identity_scope():
        users = {u.id: u for u in session.scalars(select(AdminUser).where(AdminUser.id.in_(user_ids)))}
    return session, task, comment, users


def notify_mentioned_on_own_session(task_id: int, comment_id: int, author_id: Any, mentioned_ids: list[Any]) -> int:
    """`notify_mentioned` for a task comment, on a session of its own (`task_rows_on_own_session`). Never raises."""
    try:
        session, task, comment, users = task_rows_on_own_session(task_id, comment_id, [author_id, *mentioned_ids])
        with session:
            if task is None or comment is None or author_id not in users:
                return 0
            mentioned = [users[i] for i in mentioned_ids if i in users]
            return notify_mentioned(session, task=task, comment=comment, author=users[author_id], mentioned=mentioned)
    except Exception:  # noqa: BLE001 — the comment is committed; its DMs are a courtesy
        log.exception("mention DMs for comment %s could not be sent", comment_id)
        return 0
