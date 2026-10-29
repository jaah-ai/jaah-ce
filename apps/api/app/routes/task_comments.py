"""The Task conversation surface — comments and the roster a mention is picked from.

Reading (the thread, the mention roster) is gated `tasks:view` on the task; WRITING a comment
is gated `tasks:edit` on the task's project, so a Viewer reads the conversation but cannot join it.
The narrower rules are enforced in the handler against the row's author:

    post    tasks:edit on the task
    edit    tasks:edit, and the author and nobody else — not an admin, not the task's owner
    delete  tasks:edit, and the author OR a `tasks:delete` holder on this task
    react   tasks:edit — the posting rule; a reaction is only ever the caller's own (`comment_reactions`)

An admin who could edit could put words under a colleague's badge; deleting only removes them.

Mentions are resolved server-side on every write (`app/comment_mentions.py`; Slack half in
`app/comment_notify.py`); the wire carries the resolved people so the renderer guesses nothing.
The mentions bell is `GET /api/inbox` (`routes/inbox.py`). The thread itself — body rule, page, wire,
write sequence, images — is `app/comment_service.py`, shared with a support ticket's
(`routes/support_comments.py`). An image dropped by an edit or taken by a delete has its bytes reaped here,
after the commit (`reap_unreferenced_object`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import comment_mentions, comment_notify, comment_reactions, comment_service, hipaa
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.object_access import can_access, key_scope_satisfied
from app.authz.principal import Principal, current_principal
from app.authz.visibility import TASK_SPEC, ObjectAccess
from app.identities import display_name
from app.comment_service import (
    MAX_PAGE,
    CommentIn,
    CommentOut,
    MentionCandidateOut,
    MentionOut,
    Subject,
)
from app.db import get_session
from app.entity_shape import get_or_404
from app.user_activity import record_task_note
from app.models.identity import AdminUser
from app.models.task_extras import TaskComment
from app.models.tasks import Task
from app.attachment_storage import reap_unreferenced_object

# No `prefix=`: every decorator spells its whole `/api/...` path.
router = APIRouter(tags=["task-comments"])

__all__ = ["CommentIn", "CommentOut", "MAX_PAGE", "MentionCandidateOut", "MentionOut", "router"]
_NO_TASK = "Task not found"


# ── Shared reads ──────────────────────────────────────────────────────────────────────────────────


def _may_delete_any(session: Session, principal: Principal | None, task_id: int) -> bool:
    """Is this caller a `tasks:delete` holder ON THIS TASK — the one principal besides the author who may
    remove a comment?

    Both halves: `can_access` and `key_scope_satisfied` (`app/authz/`)."""
    return can_access(
        session, principal, "task", task_id, mode=ObjectAccess.WRITE
    ) and key_scope_satisfied(
        session,
        principal,
        TASK_SPEC,
        task_id,
        key=permission_key("tasks", "delete"),
        mode=ObjectAccess.WRITE,
    )


def _render(
    session: Session,
    comments: list[TaskComment],
    *,
    me: AdminUser,
    may_delete_any: bool,
) -> list[CommentOut]:
    """The wire shape for a page of comments (`comment_service.render`)."""
    return comment_service.render(session, comments, me=me, may_delete_any=may_delete_any)  # type: ignore[return-value]


def _answered(
    session: Session, principal: Principal | None, user: AdminUser, task: Task, comment: TaskComment,
    fresh: list[AdminUser],
) -> CommentOut:
    """A written comment, after its commit: the DMs for its `fresh` mentions (never for a comment a later error
    rolled back), then the comment as the caller sees it."""
    session.refresh(comment)
    comment_notify.notify_mentioned(session, task=task, comment=comment, author=user, mentioned=fresh)
    return _render(session, [comment], me=user, may_delete_any=_may_delete_any(session, principal, task.id))[0]


def _mentioned_in(session: Session, comment: TaskComment) -> list[AdminUser]:
    """The people this comment's text names, out of the roster its own task admits. Shared by POST and
    PATCH so they cannot narrow differently (`resolve_mentions` is the cheap equivalent of
    `parse_mentions` over `mentionable_users`)."""
    assert comment.task_id is not None
    return comment_mentions.resolve_mentions(session, comment.body, comment.task_id)


# ── The conversation ──────────────────────────────────────────────────────────────────────────────


@router.get("/api/tasks/{task_id}/comments", response_model=list[CommentOut])
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def list_comments(
    task_id: int,
    request: Request,
    limit: int = 200,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[CommentOut]:
    """This task's comments, oldest first.

    The most recent `limit`, returned chronologically: the conversation is unbounded and the reader
    wants its end. A HIPAA org's read is recorded (`hipaa.record_phi_read`)."""
    comments = comment_service.page(session, Subject("task", task_id), limit)
    out = _render(
        session, comments, me=user, may_delete_any=_may_delete_any(session, principal, task_id)
    )
    task = session.scalar(select(Task).where(Task.id == task_id))
    if task is not None:
        hipaa.record_phi_read(
            session, request, record_type="comments", record_id=task_id, task_id=task_id,
            org_id=task.org_id, user=user,
        )
    return out


@router.post("/api/tasks/{task_id}/comments", response_model=CommentOut, status_code=201)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def create_comment(
    task_id: int,
    payload: CommentIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CommentOut:
    """Post a comment. `tasks:edit` IS THE GATE — see the module docstring."""
    task = get_or_404(session, Task, task_id, detail=_NO_TASK)
    comment, fresh = comment_service.create(
        session,
        Subject("task", task_id),
        author=user,
        body=payload.body,
        mentioned=lambda comment: _mentioned_in(session, comment),
        image_ids=payload.image_ids,
    )
    session.commit()
    return _answered(session, principal, user, task, comment, fresh)


@router.patch("/api/tasks/{task_id}/comments/{comment_id}", response_model=CommentOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def update_comment(
    task_id: int,
    comment_id: int,
    payload: CommentIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CommentOut:
    """Edit your own comment. THE AUTHOR AND NOBODY ELSE — see the module docstring's asymmetry note."""
    comment = comment_service.comment_or_404(session, Subject("task", task_id), comment_id)
    if comment.author_id != user.id:
        raise HTTPException(status_code=403, detail="Only the author can edit a comment")
    task = get_or_404(session, Task, task_id, detail=_NO_TASK)
    before = comment_service.image_keys(session, comment.id)
    fresh = comment_service.edit(
        session,
        comment,
        body=payload.body,
        mentioned=lambda comment: _mentioned_in(session, comment),
        image_ids=payload.image_ids,
    )
    session.commit()
    # A kept image's key is still named by its row, so the reap skips it.
    for bucket, key in before:
        reap_unreferenced_object(session, key, bucket)
    return _answered(session, principal, user, task, comment, fresh)


@router.delete("/api/tasks/{task_id}/comments/{comment_id}", status_code=204)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def delete_comment(
    task_id: int,
    comment_id: int,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> Response:
    """Delete a comment — the author's own, or anyone's for a `tasks:delete` holder on this task.

    Mention and image rows go by CASCADE, closing unread bell entries for it; the images' bytes are
    reaped after the commit (their keys read first)."""
    comment = comment_service.comment_or_404(session, Subject("task", task_id), comment_id)
    mine = comment.author_id == user.id
    if not mine and not _may_delete_any(session, principal, task_id):
        raise HTTPException(status_code=403, detail="Not allowed to delete this comment")
    if not mine:
        # The one privileged action here, and it erases its own evidence — so note who removed it.
        # `record_task_note` (task feed; `task_note:` keeps it out of the transition stream), staged in
        # the same transaction as the delete. The body is not copied: the note records the act.
        record_task_note(
            session,
            task_id,
            "comment_deleted",
            "Deleted another user's comment",
            extra={"comment_id": comment.id, "author_id": str(comment.author_id)},
            user=user,
        )
    keys = comment_service.image_keys(session, comment.id)
    session.delete(comment)
    session.commit()
    for bucket, key in keys:
        reap_unreferenced_object(session, key, bucket)
    return Response(status_code=204)


def _react(
    session: Session, principal: Principal | None, user: AdminUser, task_id: int, comment_id: int, emoji: str,
    *, on: bool,
) -> CommentOut:
    """Add (`on`) or remove the caller's `emoji` on this task's comment, then notify its author."""
    emoji = comment_reactions.emoji_or_422(emoji)
    comment = comment_service.comment_or_404(session, Subject("task", task_id), comment_id)
    owed = comment_reactions.add(session, comment, user, emoji) if on else False
    if not on:
        comment_reactions.remove(session, comment, user, emoji)
    session.commit()
    if owed:
        # After the commit, as a mention's DM.
        comment_reactions.notify_author(
            session, task=get_or_404(session, Task, task_id, detail=_NO_TASK), comment=comment, reactor=user,
            emoji=emoji,
        )
    return _render(
        session, [comment], me=user, may_delete_any=_may_delete_any(session, principal, task_id)
    )[0]


@router.put("/api/tasks/{task_id}/comments/{comment_id}/reactions/{emoji}", response_model=CommentOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def add_reaction(
    task_id: int,
    comment_id: int,
    emoji: str,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CommentOut:
    """React with `emoji` (a key of `models.REACTION_EMOJIS`). Idempotent; answers the comment."""
    return _react(session, principal, user, task_id, comment_id, emoji, on=True)


@router.delete("/api/tasks/{task_id}/comments/{comment_id}/reactions/{emoji}", response_model=CommentOut)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
def remove_reaction(
    task_id: int,
    comment_id: int,
    emoji: str,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CommentOut:
    """Take back your own `emoji`. Idempotent; answers the comment."""
    return _react(session, principal, user, task_id, comment_id, emoji, on=False)


@router.get("/api/tasks/{task_id}/mention-candidates", response_model=list[MentionCandidateOut])
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def mention_candidates(
    task_id: int,
    _user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[MentionCandidateOut]:
    """Everyone this task's comments may name, alphabetically — what the composer's `@` picker draws.

    Per task, not per org (`comment_mentions.mentionable_users`): the picker and the parser read the
    same roster, so an offered name always chips and notifies."""
    return [
        MentionCandidateOut(id=str(user.id), name=display_name(user))
        for user in comment_mentions.mentionable_users(session, task_id)
    ]
