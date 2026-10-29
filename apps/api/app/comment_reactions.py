"""Emoji reactions on a task's or a ticket's comment — 👍 is the "like" — and the comment author's notice.

The set is fixed (`app/models/task_extras.py::REACTION_EMOJIS`; the web's glyphs are `apps/web/src/lib/reactions.ts`).
A row is one (comment, person, emoji); the wire folds them per emoji (`comment_service.render`). WHO may
react is the subject's posting rule, decided by the route before it calls in (`routes/task_comments.py`,
`routes/support_comments.py`), exactly as for a comment.

The row doubles as the AUTHOR's notification: the bell's `reaction` arm (`reaction_feed`, read by
`routes/inbox.py`) and a best-effort Slack DM (`notify_author`, event `reaction`). Removing a reaction
stamps its row `removed_at` (it leaves the chips and the bell) and re-adding one restores it, so a notice
is owed only for a person's FIRST emoji EVER on a comment — a second emoji, or a toggle, is not news — and
never for reacting to your own comment.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import alert_relay, comment_mentions, comment_notify, notify_targets, org_names, support_access
from app.durable import notify_outbox
from app.identities import display_name
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.support import Ticket
from app.models.task_extras import REACTION_EMOJIS, TaskComment, TaskCommentReaction
from app.models.tasks import Task

log = logging.getLogger("app.comment_reactions")

#: Each emoji's Slack spelling, for the DM. Total over `REACTION_EMOJIS` (the suite pins it).
SLACK_SHORTCODES: dict[str, str] = {
    "thumbs_up": ":thumbsup:",
    "heart": ":heart:",
    "laugh": ":smile:",
    "tada": ":tada:",
    "surprised": ":open_mouth:",
    "sad": ":cry:",
    "eyes": ":eyes:",
    "rocket": ":rocket:",
}

#: The most reaction ids one `mark_read` call may name — the bell's own page size, as for mentions.
MAX_READ_IDS = comment_mentions.MAX_READ_IDS


def emoji_or_422(key: str) -> str:
    """`key`, if it is one of the fixed set — the readable half of `ck_task_comment_reactions_emoji`."""
    if key not in REACTION_EMOJIS:
        raise HTTPException(status_code=422, detail=f"Unknown reaction {key[:40]!r}")
    return key


# ── Writes (the caller commits, then notifies) ────────────────────────────────────────────────────


def add(session: Session, comment: TaskComment, user: AdminUser, emoji: str) -> bool:
    """Stage `user`'s `emoji` on `comment`. Idempotent; returns whether the AUTHOR is owed a notice — not
    their own comment, and the reactor has never reacted to it before (removed rows count)."""
    # The comment row locked first: two first emoji from two tabs would otherwise both read "none yet" and
    # both owe the author a notice. Released at the caller's commit.
    session.execute(select(TaskComment.id).where(TaskComment.id == comment.id).with_for_update())
    mine = {
        row.emoji: row
        for row in session.scalars(
            select(TaskCommentReaction).where(
                TaskCommentReaction.comment_id == comment.id, TaskCommentReaction.user_id == user.id
            )
        ).all()
    }
    if emoji in mine:
        # Restored, never re-announced: the author was told (or not) the first time.
        mine[emoji].removed_at = None
        return False
    try:
        # A savepoint: a double-click racing its twin hits the unique key, and that is "already there".
        with session.begin_nested():
            session.add(
                TaskCommentReaction(
                    comment_id=comment.id,
                    task_id=comment.task_id,
                    ticket_id=comment.ticket_id,
                    user_id=user.id,
                    emoji=emoji,
                    org_id=comment.org_id,
                    # Born read unless it is news: your own comment's reaction never is, nor a second emoji
                    # from someone the author already heard from — one bell item per reactor, as one DM.
                    read_at=datetime.now(timezone.utc) if mine or user.id == comment.author_id else None,
                )
            )
    except IntegrityError:
        return False
    return not mine and user.id != comment.author_id


def remove(session: Session, comment: TaskComment, user: AdminUser, emoji: str) -> None:
    """Stage the removal of `user`'s `emoji` on `comment` (only ever the caller's own). Idempotent. A stamp,
    not a delete — see the module docstring."""
    row = session.scalar(
        select(TaskCommentReaction).where(
            TaskCommentReaction.comment_id == comment.id,
            TaskCommentReaction.user_id == user.id,
            TaskCommentReaction.emoji == emoji,
            TaskCommentReaction.removed_at.is_(None),
        )
    )
    if row is not None:
        row.removed_at = datetime.now(timezone.utc)


# ── The Slack half ────────────────────────────────────────────────────────────────────────────────


def notify_author(
    session: Session,
    *,
    task: Optional[Task] = None,
    ticket: Optional[Ticket] = None,
    comment: TaskComment,
    reactor: AdminUser,
    emoji: str,
) -> bool:
    """DM `comment`'s author that `reactor` reacted — best-effort, after the commit, as a mention's DM
    (`comment_notify`). Sent only while the author may still read the subject, because the card carries
    its title and the comment's text. Returns whether it was delivered."""
    if (task is None) == (ticket is None):
        raise ValueError("notify_author needs exactly one subject: task or ticket")
    try:
        # A `select`, not `session.get`: the tenancy seam scopes the read. The author may sit in another
        # org than the caller on a ticket, so that read is the fleet one.
        if ticket is not None:
            author = _ticket_author(ticket, comment)
        else:
            author = session.scalar(select(AdminUser).where(AdminUser.id == comment.author_id))
            if author is not None and not notify_targets.may_read_task(session, author, task.id):
                author = None
        if author is None:
            return False
        target, reason = notify_targets.target_for_user(session, author, event="reaction")
        if target is None:
            log.debug("reaction not relayed to %s (comment=%s reason=%s)", author.id, comment.id, reason)
            return False
        glyph = SLACK_SHORTCODES[emoji]
        # Escaped: tenant prose reaches an mrkdwn field (`comment_notify.notify_mentioned`'s reason).
        quoted = alert_relay.inert_mrkdwn(comment_notify.excerpt(comment.body))
        actor = alert_relay.inert_mrkdwn(display_name(reactor))
        event = _ticket_event(
            ticket, comment, actor, glyph, quoted, org_names.org_name(session, ticket.org_id)
        ) if ticket is not None else _task_event(
            session, task, comment, reactor, glyph, quoted
        )
        return bool(notify_outbox.slack(event, target))
    except alert_relay.AlertRelayError:
        log.warning("reaction DM failed (comment=%s)", comment.id)
    except Exception:  # noqa: BLE001 — a Slack or lookup failure must never fail the reaction.
        log.warning("reaction DM errored (comment=%s)", comment.id, exc_info=True)
    return False


def _ticket_author(ticket: Ticket, comment: TaskComment) -> Optional[AdminUser]:
    """The comment's author, if they may still see it on this ticket (detached)."""
    from app.authz.principal import principal_for_user

    with support_access.fleet_session() as fleet:
        author = fleet.scalar(select(AdminUser).where(AdminUser.id == comment.author_id))
        if author is None:
            return None
        principal = principal_for_user(fleet, author)
        visible = support_access.can_view(principal, ticket) and (
            comment.visibility == support_access.PUBLIC or support_access.can_see_internal(principal)
        )
        fleet.expunge_all()
    return author if visible else None


def _task_event(
    session: Session, task: Task, comment: TaskComment, reactor: AdminUser, glyph: str, quoted: str
) -> dict:
    from app.task_external_comment import _console_task_url

    project_name = session.scalar(select(Project.name).where(Project.id == task.project_id))
    # The task headline ("… : Reacted") from trusted fields; the body slot says who and what.
    return alert_relay.message("reaction", issue=str(task.id), payload={
        "task_id": task.id,
        "comment_id": comment.id,
        "task_title": task.title,
        "project": project_name,
        "org": org_names.org_name(session, task.org_id),
        "actor": display_name(reactor),
        "question_body": f"{alert_relay.inert_mrkdwn(display_name(reactor))} {glyph} {quoted}",
        "console_url": f"{_console_task_url(task.id)}?tab=comments#comment-{comment.id}",
    })


def _ticket_event(
    ticket: Ticket, comment: TaskComment, actor: str, glyph: str, quoted: str, org_name: Optional[str] = None
) -> dict:
    return alert_relay.message(
        "reaction",
        f"{org_names.org_lead(org_name)}{actor} reacted {glyph} to your comment on ticket #{ticket.id} "
        f"{alert_relay.inert_mrkdwn(ticket.title)}: {quoted}",
        payload={
            "ticket_id": ticket.id,
            "comment_id": comment.id,
            "actor": actor,
            "console_url": comment_notify.ticket_console_url(ticket.id, comment.id),
        },
    )


# ── The bell ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FeedEntry:
    """One reaction on the bell, with the comment it is on and that comment's subject."""

    reaction: TaskCommentReaction
    comment: TaskComment
    task: Optional[Task]
    ticket: Optional[Ticket]
    reactor_name: str


def _authored_by(user_id: UUID):
    """Standing reactions on `user_id`'s own comments, by anybody else — the reader's reaction notices."""
    return and_(
        TaskComment.author_id == user_id,
        TaskCommentReaction.user_id != user_id,
        TaskCommentReaction.removed_at.is_(None),
    )


def reaction_feed(
    session: Session, user_id: UUID, principal, *, unread_only: bool = True, limit: int = 50
) -> list[FeedEntry]:
    """Reactions to `user_id`'s comments, newest first — the bell's `reaction` arm. Scoped and re-checked
    by `comment_mentions.feed_rows`, as `mention_feed`."""
    rows = comment_mentions.feed_rows(
        session, TaskCommentReaction, [_authored_by(user_id)], principal, unread_only=unread_only, limit=limit
    )
    names = comment_mentions.names_of(reaction.user_id for reaction, *_rest in rows)
    return [
        FeedEntry(
            reaction=reaction, comment=comment, task=task, ticket=ticket,
            reactor_name=names.get(reaction.user_id, "") or "",
        )
        for reaction, comment, task, ticket in rows
    ]


def mark_read(
    session: Session, user_id: UUID, reaction_ids: Optional[list[int]] = None, *, principal=None
) -> int:
    """Stamp the reaction notices on `user_id`'s comments read; `None` means all. Returns how many moved
    (`comment_mentions.stamp_read`)."""
    return comment_mentions.stamp_read(session, TaskCommentReaction, _notices_of(user_id), reaction_ids, principal)


def mark_unread(session: Session, user_id: UUID, reaction_ids: list[int], *, principal=None) -> int:
    """Clear the read stamp on the named reaction notices on `user_id`'s comments — "mark as unread"."""
    return comment_mentions.stamp_read(
        session, TaskCommentReaction, _notices_of(user_id), reaction_ids, principal, read=False
    )


def _notices_of(user_id: UUID) -> list:
    """The reaction rows that are `user_id`'s notices: someone else's, on a comment they wrote."""
    return [
        TaskCommentReaction.comment_id.in_(select(TaskComment.id).where(TaskComment.author_id == user_id)),
        TaskCommentReaction.user_id != user_id,
    ]
