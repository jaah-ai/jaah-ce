"""A comment thread — a Task's or a support Ticket's — as one implementation both subjects' routes call.

The rows are `task_comments` / `task_comment_mentions` either way (a row names its task XOR its ticket;
revision 0302), so an upgrade here reaches both surfaces. What stays per subject is WHO may do what, which
the routes decide before they call in: `app/routes/task_comments.py` (task keys) and
`app/routes/support_comments.py` (`app/support_access.py`). This module holds the rest — the body rule, the
page read, the wire, and the write sequence (row, then its mention rows, in one transaction).

A ticket's thread also carries `visibility`: an `internal` note is an operator's, and every read here that a
ticket caller makes passes `public_only` unless they may see internal notes.

Images are a TASK comment's only (a ticket's `image_ids` is a 422): a comment owns the `comment_image` rows
it names in `image_ids` (staged by `routes/attachments.py::create_comment_image_ticket`, linked here via
`TaskAttachment.comment_id`), and its body may be empty only when it carries one. An edit's `image_ids`
replaces the set; an image the edit drops or the comment's delete takes goes with its bytes — the route
reaps `image_keys` read before the write, after the commit. A staged image no post or edit claims within a day goes
with its bytes too (`reap_unclaimed_images`, the hourly `comment-image-reap` cron).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app import attachment_storage, comment_mentions
from app.entity_shape import get_or_404, iso_or_none, RefOut
from app.identities import label_expr
from app.models.identity import AdminUser
from app.models.task_extras import (
    ATTACHMENT_KIND_COMMENT_IMAGE,
    REACTION_EMOJIS,
    TaskAttachment,
    TaskComment,
    TaskCommentMention,
    TaskCommentReaction,
    VISIBILITY_PUBLIC,
)
from app.task_artifacts import uploaded_clause

# Bounded: `body` is `Text` and reachable by every reader of the subject.
MAX_BODY = 8000

# Most comments one read returns; bounds the hottest read on this surface.
MAX_PAGE = 500

# Most images one comment carries.
MAX_IMAGES = 10

# How long a staged image waits for a post or edit to claim it before `reap_unclaimed_images` takes it: a draft
# comment is a short-lived act.
UNCLAIMED_IMAGE_GRACE = timedelta(hours=24)
# Rows one reap batch deletes and commits before reaping their objects.
_REAP_BATCH = 500

TASK = "task"
TICKET = "ticket"


@dataclass(frozen=True)
class Subject:
    """What a comment is on: one task or one ticket."""

    kind: Literal["task", "ticket"]
    id: int

    @property
    def column(self):
        return TaskComment.task_id if self.kind == TASK else TaskComment.ticket_id

    def ids(self) -> dict[str, int]:
        """The row's subject column, as constructor arguments."""
        return {"task_id": self.id} if self.kind == TASK else {"ticket_id": self.id}


# ── The wire ──────────────────────────────────────────────────────────────────────────────────────


class MentionOut(BaseModel):
    """One person a comment names, already resolved. The renderer chips exactly these.

    A name only: nothing on this surface navigates to a person, so an id could only be misused."""

    name: str


class ReactionOut(BaseModel):
    """One emoji on one comment, folded over everyone who chose it (`app/comment_reactions.py`).

    Names, not ids, as `MentionOut`: the chip's hover says who. `mine` is the caller's, for the toggle."""

    emoji: str
    count: int
    mine: bool
    names: list[str] = Field(default_factory=list)


class CommentImageOut(BaseModel):
    """One image a comment carries; its bytes are `GET /api/tasks/{task_id}/attachments/{id}/content`."""

    id: int
    filename: str
    content_type: Optional[str] = None


class CommentOut(BaseModel):
    """One task comment, as the tab draws it.

    No author id: the badge derives from the display name and nothing navigates to a person.
    `author_is_me` is the server's "may I edit this", since the caller's identity is not on the wire.
    """

    id: int
    task_id: int
    body: str
    author_name: str
    author_is_me: bool
    # The server's verdict for drawing a control; the DELETE re-applies the rule itself.
    may_delete: bool
    created_at: Optional[str] = None
    # NULL for a comment nobody has revised; the "edited" mark reads the absence.
    edited_at: Optional[str] = None
    mentions: list[MentionOut] = Field(default_factory=list)
    # Only the emoji someone chose, in palette order (`app/models/task_extras.py::REACTION_EMOJIS`).
    reactions: list[ReactionOut] = Field(default_factory=list)
    images: list[CommentImageOut] = Field(default_factory=list)


class TicketCommentOut(BaseModel):
    """One ticket comment: `CommentOut` with `ticket_id` for `task_id`, and `visibility` (`internal`
    reaches operators only)."""

    id: int
    ticket_id: int
    visibility: Literal["public", "internal"]
    body: str
    author_name: str
    author_is_me: bool
    may_delete: bool
    created_at: Optional[str] = None
    edited_at: Optional[str] = None
    mentions: list[MentionOut] = Field(default_factory=list)
    reactions: list[ReactionOut] = Field(default_factory=list)


class CommentIn(BaseModel):
    """`image_ids`: the confirmed `comment_image` rows this comment carries — a task's only. On an edit,
    absent leaves the set alone and a list replaces it."""

    body: str = ""
    image_ids: Optional[list[int]] = Field(default=None, max_length=MAX_IMAGES)


class MentionCandidateOut(RefOut):
    """One person the composer may offer. Name and id only — every reader of the subject reaches this
    roster."""


# ── Reads ─────────────────────────────────────────────────────────────────────────────────────────


def clean_body(raw: str, *, has_images: bool = False) -> str:
    """The body as it will be stored, or a 422 saying which rule it broke.

    Empty only beside an image — the one home of that rule since 0303 dropped `ck_task_comments_body`."""
    body = raw.strip()
    if not body and not has_images:
        raise HTTPException(status_code=422, detail="Comment is empty")
    if len(body) > MAX_BODY:
        raise HTTPException(status_code=422, detail=f"Comment is over {MAX_BODY} characters")
    return body


def page(session: Session, subject: Subject, limit: int, *, public_only: bool = False) -> list[TaskComment]:
    """This subject's comments, oldest first: the most recent `limit`, returned chronologically — the
    conversation is unbounded and the reader wants its end."""
    capped = max(1, min(limit, MAX_PAGE))
    statement = select(TaskComment).where(subject.column == subject.id)
    if public_only:
        statement = statement.where(TaskComment.visibility == VISIBILITY_PUBLIC)
    newest = list(session.scalars(statement.order_by(TaskComment.id.desc()).limit(capped)).all())
    return list(reversed(newest))


def comment_or_404(
    session: Session, subject: Subject, comment_id: int, *, public_only: bool = False
) -> TaskComment:
    """This subject's comment, or a 404.

    Both ids in the predicate, so another subject's comment cannot resolve through this one's gate; an
    internal note a caller may not see is as absent as a missing one."""
    where = [subject.column == subject.id]
    if public_only:
        where.append(TaskComment.visibility == VISIBILITY_PUBLIC)
    return get_or_404(session, TaskComment, comment_id, detail="Comment not found", where=where)


def image_keys(session: Session, comment_id: int) -> list[tuple[Optional[str], str]]:
    """The stored `(bucket, key)` of this comment's images — read before a write that may drop them, reaped after
    its commit (`app/attachment_storage.py::reap_unreferenced_object` skips a key a surviving row still names)."""
    return [
        (bucket, key)
        for bucket, key in session.execute(
            select(TaskAttachment.s3_bucket, TaskAttachment.s3_key).where(TaskAttachment.comment_id == comment_id)
        ).all()
        if key
    ]


def reap_unclaimed_images(session: Session, *, older_than: timedelta = UNCLAIMED_IMAGE_GRACE) -> int:
    """Delete every confirmed `comment_image` no comment claimed within `older_than` of its upload, then its bytes.

    `FOR UPDATE SKIP LOCKED`: a post or edit claiming the image holds its row (`_claimed_images`), so the claim
    wins and this pass leaves the row for the next one; a claimed row (`comment_id` set) is never selected. Batches of
    `_REAP_BATCH`, each deleted and committed, then its objects reaped behind the still-referenced check: a pass that
    dies orphans at most one batch's objects. Run fleet-wide by the `comment-image-reap` cron on the operator plane.
    Returns how many rows went."""
    cutoff = datetime.now(timezone.utc) - older_than
    reaped = 0
    while True:
        rows = session.execute(
            select(TaskAttachment.id, TaskAttachment.s3_bucket, TaskAttachment.s3_key)
            .where(
                TaskAttachment.kind == ATTACHMENT_KIND_COMMENT_IMAGE,
                TaskAttachment.comment_id.is_(None),
                uploaded_clause(),
                TaskAttachment.uploaded_at < cutoff,
            )
            .order_by(TaskAttachment.id)
            .limit(_REAP_BATCH)
            .with_for_update(skip_locked=True)
        ).all()
        if not rows:
            session.rollback()
            return reaped
        session.execute(delete(TaskAttachment).where(TaskAttachment.id.in_([row.id for row in rows])))
        session.commit()
        for row in rows:
            if row.s3_key:
                attachment_storage.reap_unreferenced_object(session, row.s3_key, row.s3_bucket)
        reaped += len(rows)
        if len(rows) < _REAP_BATCH:
            return reaped


def render(
    session: Session,
    comments: list[TaskComment],
    *,
    me: AdminUser,
    may_delete_any: bool,
    names: Optional[Callable[[set[UUID]], dict[UUID, str]]] = None,
) -> list[BaseModel]:
    """The wire shape for a page of comments; names, mentions and reactions resolved in three queries, not
    per row.

    `names` resolves display names; the default reads them on the caller's session, which sees exactly the
    task's org. A ticket's thread spans the org line (an operator's reply on a customer's ticket), so its
    caller passes `comment_mentions.names_of`."""
    if not comments:
        return []
    mention_rows = list(
        session.execute(
            select(TaskCommentMention.comment_id, TaskCommentMention.user_id)
            .where(TaskCommentMention.comment_id.in_([comment.id for comment in comments]))
            .order_by(TaskCommentMention.id)
        ).all()
    )
    reaction_rows = list(
        session.execute(
            select(TaskCommentReaction.comment_id, TaskCommentReaction.user_id, TaskCommentReaction.emoji)
            .where(
                TaskCommentReaction.comment_id.in_([comment.id for comment in comments]),
                TaskCommentReaction.removed_at.is_(None),
            )
        ).all()
    )
    images_by_comment: dict[int, list[CommentImageOut]] = {}
    for image in session.scalars(
        select(TaskAttachment)
        .where(TaskAttachment.comment_id.in_([comment.id for comment in comments]))
        .order_by(TaskAttachment.id)
    ).all():
        images_by_comment.setdefault(image.comment_id, []).append(
            CommentImageOut(id=image.id, filename=image.filename, content_type=image.content_type)
        )
    wanted = (
        {comment.author_id for comment in comments}
        | {user_id for _c, user_id in mention_rows}
        | {user_id for _c, user_id, _e in reaction_rows}
    )
    if names is None:
        resolved = dict(
            session.execute(select(AdminUser.id, label_expr()).where(AdminUser.id.in_(wanted))).all()
        )
    else:
        resolved = names(wanted)
    by_comment: dict[int, list[MentionOut]] = {}
    for comment_id, user_id in mention_rows:
        # A person no longer resolvable is not chipped (a deleted user's rows CASCADE anyway).
        if user_id in resolved:
            by_comment.setdefault(comment_id, []).append(MentionOut(name=resolved[user_id]))
    reactions = _fold_reactions(reaction_rows, resolved, me)
    out: list[BaseModel] = []
    for comment in comments:
        fields = {
            "id": comment.id,
            "body": comment.body,
            # `author_id` is ON DELETE RESTRICT; the fallback covers a restore or hand-edit.
            "author_name": resolved.get(comment.author_id, "") or "",
            "author_is_me": comment.author_id == me.id,
            "may_delete": may_delete_any or comment.author_id == me.id,
            "created_at": iso_or_none(comment.created_at),
            "edited_at": iso_or_none(comment.edited_at),
            "mentions": by_comment.get(comment.id, []),
            "reactions": reactions.get(comment.id, []),
        }
        if comment.ticket_id is not None:
            out.append(TicketCommentOut(ticket_id=comment.ticket_id, visibility=comment.visibility, **fields))
        else:
            out.append(
                CommentOut(task_id=comment.task_id, images=images_by_comment.get(comment.id, []), **fields)
            )
    return out


def _fold_reactions(rows, resolved: dict[UUID, str], me: AdminUser) -> dict[int, list[ReactionOut]]:
    """`(comment_id, user_id, emoji)` rows as each comment's chips: one per emoji chosen, palette order,
    reactors alphabetical. A reactor no longer resolvable still counts (the chip is about the emoji) but is
    not named."""
    people: dict[tuple[int, str], set[UUID]] = {}
    for comment_id, user_id, emoji in rows:
        people.setdefault((comment_id, emoji), set()).add(user_id)
    folded: dict[int, list[ReactionOut]] = {}
    for comment_id, emoji in sorted(people, key=lambda pair: (pair[0], REACTION_EMOJIS.index(pair[1]))):
        who = people[(comment_id, emoji)]
        folded.setdefault(comment_id, []).append(
            ReactionOut(
                emoji=emoji,
                count=len(who),
                mine=me.id in who,
                names=sorted(resolved[user] for user in who if resolved.get(user)),
            )
        )
    return folded


# ── Writes (the caller commits, then notifies — no DM for a comment a later error rolled back) ────────


def _claimed_images(
    session: Session, subject: Subject, *, uploader: UUID, image_ids: list[int], comment_id: Optional[int]
) -> list[TaskAttachment]:
    """The rows `image_ids` names, or one 422 for any that is not claimable.

    Claimable: this task's confirmed `comment_image`, either staged by the uploader and unlinked or
    already on `comment_id` (an edit keeping it). One message for every miss, so a probe learns nothing
    about another's image. A ticket's comment claims none (422).

    Locked (`FOR UPDATE`): two concurrent claims of one staged image serialize, and the loser's re-read
    under the lock sees the winner's `comment_id` and refuses — so an image never lands on two comments."""
    if subject.kind != TASK:
        raise HTTPException(status_code=422, detail="Images are for task comments only")
    wanted = set(image_ids)
    if not wanted:
        return []
    staged = (TaskAttachment.comment_id.is_(None)) & (TaskAttachment.uploaded_by_id == uploader)
    rows = list(
        session.scalars(
            select(TaskAttachment)
            .where(
                TaskAttachment.id.in_(wanted),
                TaskAttachment.task_id == subject.id,
                TaskAttachment.kind == ATTACHMENT_KIND_COMMENT_IMAGE,
                uploaded_clause(),
                staged if comment_id is None else or_(staged, TaskAttachment.comment_id == comment_id),
            )
            .with_for_update()
            # A row the session already holds would otherwise keep its pre-lock `comment_id`.
            .execution_options(populate_existing=True)
        ).all()
    )
    # Re-checked on the locked rows: Postgres re-evaluates the WHERE after a lock wait, but only the
    # locked rows are the claim, and this makes that the code's own guarantee rather than the engine's.
    rows = [row for row in rows if row.comment_id is None or row.comment_id == comment_id]
    if len(rows) != len(wanted):
        raise HTTPException(status_code=422, detail="Image not found")
    return rows


Roster = Callable[[TaskComment], list[AdminUser]]
"""The people a comment's text names, out of the roster its subject admits."""


def create(
    session: Session,
    subject: Subject,
    *,
    author: AdminUser,
    body: str,
    mentioned: Roster,
    visibility: str = VISIBILITY_PUBLIC,
    admits: Optional[Callable[[AdminUser], bool]] = None,
    image_ids: Optional[list[int]] = None,
) -> tuple[TaskComment, list[AdminUser]]:
    """Stage a new comment, its mention rows and its images' link. Returns it and the people newly named
    (owed a DM)."""
    images = (
        _claimed_images(session, subject, uploader=author.id, image_ids=image_ids, comment_id=None)
        if image_ids is not None
        else []
    )
    comment = TaskComment(
        body=clean_body(body, has_images=bool(images)), author_id=author.id, **subject.ids()
    )
    if subject.kind == TICKET:
        comment.visibility = visibility
    session.add(comment)
    # Flush before the mention rows: `org_id` is filled by this table's BEFORE INSERT trigger and
    # `sync_mentions` copies it.
    session.flush()
    session.refresh(comment)
    for image in images:
        image.comment_id = comment.id
    fresh = comment_mentions.sync_mentions(session, comment, mentioned(comment), admits=admits)
    return comment, fresh


def edit(
    session: Session,
    comment: TaskComment,
    *,
    body: str,
    mentioned: Roster,
    admits: Optional[Callable[[AdminUser], bool]] = None,
    image_ids: Optional[list[int]] = None,
) -> list[AdminUser]:
    """Stage an edit of `comment` and reconcile its mentions and, when `image_ids` is given, replace its
    images (a dropped one's row is deleted; the caller reaps its bytes). Returns the people newly named."""
    current = list(
        session.scalars(select(TaskAttachment).where(TaskAttachment.comment_id == comment.id)).all()
    )
    if image_ids is None:
        has_images = bool(current)
    else:
        subject = (
            Subject(TASK, comment.task_id)
            if comment.task_id is not None
            else Subject(TICKET, comment.ticket_id)
        )
        kept = _claimed_images(
            session, subject, uploader=comment.author_id, image_ids=image_ids, comment_id=comment.id
        )
        has_images = bool(kept)
    comment.body = clean_body(body, has_images=has_images)
    if image_ids is not None:
        kept_ids = {image.id for image in kept}
        for image in kept:
            image.comment_id = comment.id
        for image in current:
            if image.id not in kept_ids:
                session.delete(image)
    # Set on every edit, even one that changes nothing visible.
    comment.edited_at = datetime.now(timezone.utc)
    session.flush()
    return comment_mentions.sync_mentions(session, comment, mentioned(comment), admits=admits)
