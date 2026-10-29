"""`@First Last` inside a task or ticket comment — who it names, and the rows that make that a fact.

The server resolves a mention; the client never guesses one. The wire carries the resolved people
(`app/comment_service.py::CommentOut.mentions`) and `apps/web/src/lib/commentText.ts` chips exactly
those. Matching is longest name first, first hit wins; anything else stays literal (no fuzzy match).

A task's roster is the caller's own org via the tenancy seam (no hand-written `org_id` filter), minus the
system actor, narrowed to who may read the task. A ticket's is who may view the ticket — its requester, its
org's admins and the operators — and only the operators for an internal note (`ticket_roster`); it spans
the org line, so it is read on `support_access.fleet_session`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import notify_targets, support_access
from app.identities import display_name, label_expr
from app.models.identity import AdminUser
from app.models.support import Ticket
from app.models.task_extras import TaskComment, TaskCommentMention
from app.models.tasks import Task
from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

# Where a mention may start: after whitespace or at the beginning, so an email domain is not one.
_MENTION_START = re.compile(r"(?:(?<=\s)|^)@")

# What may end a name. A letter or digit does not (stops `@Annie` resolving to "Ann"); `-` and `'`
# are excluded because they occur inside names.
_NAME_END = set(" \t\n\r.,;:!?()[]{}<>\"/\\|@#*`~+=&%$")


def _ends_cleanly(rest: str, length: int) -> bool:
    """Is the name that occupies `rest[:length]` a whole word — nothing alphanumeric right after it?"""
    return length >= len(rest) or rest[length] in _NAME_END


def _org_roster(session: Session) -> list[AdminUser]:
    """The caller's own org, alphabetically, minus the system actor — one statement, no access probe.

    No `org_id` predicate: the `@feature` route's tenancy seam binds it (`routes/task_comments.py`'s
    `mention_candidates`)."""
    return list(
        session.scalars(
            select(AdminUser).where(AdminUser.is_system.is_(False)).order_by(label_expr())
        ).all()
    )


def mentionable_users(session: Session, task_id: int) -> list[AdminUser]:
    """Everyone who can be mentioned on this task, alphabetically — what the picker draws and a body
    is matched against.

    Scoped to the task, not just the org: a name `visible_task_clause` does not admit could never be
    delivered, so offering it would silently do nothing. No `org_id` predicate (tenancy seam). A probe
    per row is affordable because the org's staff is bounded; the picker fetches once per mount."""
    return [
        user for user in _org_roster(session)
        if notify_targets.may_read_task(session, user, task_id)
    ]


def ticket_roster(ticket: Ticket, visibility: str, *, body: Optional[str] = None) -> list[AdminUser]:
    """Everyone a comment of `visibility` on `ticket` may name, alphabetically — the ticket's picker AND
    parser roster, so an offered name always chips.

    Who may view the ticket (`support_access.can_view`: its requester, its org's admins, the operators),
    and for an internal note only who may see internal notes (the operators), so a customer is never
    mentioned in one. Candidates are the ticket's org and the operator plane, read on `fleet_session`
    (the operators are in another org than a customer's session sees); each is judged by its own
    `Principal`, the same rule a request of theirs would meet. Returned detached, names and ids loaded.

    `body` is the write path's narrowing (`resolve_mentions`' reason): the candidates are cut to the names
    the text contains BEFORE any principal is built, so a post costs a probe per name it mentions, not per
    colleague; the answer is identical to parsing against the full roster."""
    from app.authz.principal import principal_for_user

    operators = and_(AdminUser.is_superadmin.is_(True), AdminUser.org_id.in_(OPERATOR_PLANE_ORG_IDS))
    # An internal note's readers are the operators alone, so nobody else is even a candidate.
    who = operators if visibility != support_access.PUBLIC else or_(AdminUser.org_id == ticket.org_id, operators)
    with support_access.fleet_session() as session:
        candidates = list(
            session.scalars(
                select(AdminUser).where(AdminUser.is_system.is_(False), who).order_by(label_expr())
            ).all()
        )
        if body is not None:
            candidates = parse_mentions(body, candidates)
        admitted = []
        for user in candidates:
            principal = principal_for_user(session, user)
            if not support_access.can_view(principal, ticket):
                continue
            if visibility != support_access.PUBLIC and not support_access.can_see_internal(principal):
                continue
            admitted.append(user)
        session.expunge_all()
    return admitted


def names_of(user_ids: Iterable[UUID]) -> dict[UUID, str]:
    """`{id: label}` for the people on a ticket's thread (authors, mentions), who may be operators in
    another org than the reader — read on `fleet_session`. The ids come off rows of a ticket the caller
    already passed `can_view` on, and only the label (`identities.label_expr`) is read."""
    ids = {user_id for user_id in user_ids if user_id is not None}
    if not ids:
        return {}
    with support_access.fleet_session() as session:
        return dict(session.execute(select(AdminUser.id, label_expr()).where(AdminUser.id.in_(ids))).all())


def parse_mentions(body: str, roster: Iterable[AdminUser]) -> list[AdminUser]:
    """The people `body` names, in the order they first appear, each at most once.

    Pure, so it runs on POST, PATCH and in tests without a database (the roster rows carry their identity,
    so `identities.display_name` reads off a detached row). Case-sensitive on the derived label. A label
    must be followed by whitespace, punctuation or end-of-string.

    An ambiguous label resolves to nobody: the derived label is not unique (two people may share a name
    pair, or one's label is another's email), and picking the first would tell the author they reached
    someone they may not have. No chip shows it did not take."""
    roster = list(roster)
    labels = {user.id: display_name(user) for user in roster}
    named = [user for user in roster if labels[user.id]]
    ambiguous = {
        labels[user.id] for user in named if sum(1 for other in named if labels[other.id] == labels[user.id]) > 1
    }
    by_length = sorted(
        (user for user in named if labels[user.id] not in ambiguous),
        key=lambda user: len(labels[user.id]),
        reverse=True,
    )
    found: dict[UUID, AdminUser] = {}
    for match in _MENTION_START.finditer(body):
        rest = body[match.end() :]
        for user in by_length:
            name = labels[user.id]
            if rest.startswith(name) and _ends_cleanly(rest, len(name)):
                found.setdefault(user.id, user)
                break
    return list(found.values())


def resolve_mentions(session: Session, body: str, task_id: int) -> list[AdminUser]:
    """The people this body names on this task — the write path's roster, and why it is not
    `mentionable_users`.

    `may_read_task` costs several uncached statements per user, so this narrows the roster to names the
    text contains first and probes only those. The answer is identical to parsing against
    `mentionable_users`."""
    roster = _org_roster(session)
    named = parse_mentions(body, roster)
    return [user for user in named if notify_targets.may_read_task(session, user, task_id)]


def sync_mentions(
    session: Session,
    comment: TaskComment,
    mentioned: list[AdminUser],
    *,
    admits: Optional[Callable[[AdminUser], bool]] = None,
) -> list[AdminUser]:
    """Hold this comment's mention rows equal to what its text now says. Returns the people who were
    not already named — the ones a notification is owed to.

    Append-only within a comment: deleting on removal let an author re-notify someone without limit by
    toggling a name. A sent DM cannot be recalled anyway, and `read_at` survives a re-save. The author
    is never notified of, or given a row for, their own mention.

    Task visibility is gated upstream in `mentionable_users`; a row is a notification carrying task
    content, and the rows are what the wire chips, so an unreachable name stays plain text. `admits` is
    that re-check: a task comment's default is `may_read_task`; a ticket comment's caller passes its
    `ticket_roster` membership, and must (`may_read_task` admits everyone for a task-less row).
    """
    if admits is None:
        if comment.task_id is None:
            raise ValueError("a ticket comment's mentions need its roster: pass `admits`")
        task_id = comment.task_id
        admits = lambda user: notify_targets.may_read_task(session, user, task_id)  # noqa: E731
    wanted = {
        user.id: user
        for user in mentioned
        if user.id != comment.author_id and admits(user)
    }
    existing = {
        row.user_id: row
        for row in session.scalars(
            select(TaskCommentMention).where(TaskCommentMention.comment_id == comment.id)
        ).all()
    }
    fresh = [user for user_id, user in wanted.items() if user_id not in existing]
    for user in fresh:
        session.add(
            TaskCommentMention(
                comment_id=comment.id,
                task_id=comment.task_id,
                ticket_id=comment.ticket_id,
                user_id=user.id,
                org_id=comment.org_id,
            )
        )
    return fresh


MAX_READ_IDS = 200
"""The most mention ids one `mark_read` call may name — the bell's own page size."""


def stamp_read(
    session: Session, model, owned: list, ids: Optional[list[int]], principal, *, read: bool = True
) -> int:
    """Stamp the reader's unread `model` rows (`owned`) read; `ids=None` means all of them. Returns how
    many moved. The bell's one read stamp, for mentions and reactions alike. `read=False` is the reverse —
    "mark as unread": it clears the stamp on the named read rows (never all of them; `ids` is required).

    Scoped to one reader in the statement itself (`owned`). An operator's ticket rows live in the tickets'
    orgs, which their own session does not see, so for an operator `principal` those are stamped on
    `fleet_session` too — still keyed on `owned`. That half commits on its own; a read stamp is idempotent,
    so a later failure of the caller's transaction costs nothing but a retry."""
    if not read and ids is None:
        raise ValueError("mark as unread names its rows")

    def unread(statement):
        statement = statement.where(*owned, model.read_at.is_(None) if read else model.read_at.is_not(None))
        if ids is not None:
            # Capped: caller input lands in `IN (...)`, and the bell renders at most `GET /api/inbox`'s limit.
            statement = statement.where(model.id.in_(ids[:MAX_READ_IDS]))
        return statement

    now = datetime.now(timezone.utc) if read else None
    rows = list(session.scalars(unread(select(model))).all())
    for row in rows:
        row.read_at = now
    moved = len(rows)
    if principal is not None and principal.is_operator:
        seen = {row.id for row in rows}
        with support_access.fleet_session() as fleet:
            for row in fleet.scalars(unread(select(model)).where(model.ticket_id.is_not(None))).all():
                if row.id not in seen:
                    row.read_at = now
                    moved += 1
            fleet.commit()
    return moved


def mark_read(
    session: Session, user_id: UUID, mention_ids: Optional[list[int]] = None, *, principal=None
) -> int:
    """Stamp this reader's mentions read; `None` means all of them. Returns how many moved (`stamp_read`)."""
    return stamp_read(session, TaskCommentMention, [TaskCommentMention.user_id == user_id], mention_ids, principal)


def mark_unread(session: Session, user_id: UUID, mention_ids: list[int], *, principal=None) -> int:
    """Clear this reader's read stamp on the named mentions — "mark as unread". Returns how many moved."""
    return stamp_read(
        session, TaskCommentMention, [TaskCommentMention.user_id == user_id], mention_ids, principal, read=False
    )


def feed_rows(
    session: Session, model, owned: list, principal, *, unread_only: bool, limit: int, author_join: bool = False
) -> list[tuple]:
    """The reader's `model` rows (`owned`) as `(row, comment, task, ticket)`, newest first, capped — the
    bell's one feed query, for mentions and reactions alike.

    Readable now, not just when written: a grant can be revoked, so a task's are filtered through
    `app/authz/visibility.py::scoped_task_select`, and a ticket's re-checked with `support_access.can_view` — and
    `can_see_internal` for an internal note, which never reaches anyone else. Hidden entries are not
    deleted — the grant may return.

    Two arms, merged: a task row joins its task on the caller's session; a ticket row joins its ticket, on
    the caller's session — or on `fleet_session` for an operator, whose ticket rows live in the tickets'
    orgs. `author_join` also inner-joins the task arm's comment author on the caller's session (the
    mention arm's historical shape)."""
    from app.authz.visibility import scoped_task_select

    capped = max(1, min(limit, MAX_READ_IDS))
    readable = select(scoped_task_select(principal).subquery().c.id)

    def arm(subject, subject_id, comment_subject_id):
        statement = (
            select(model, TaskComment, subject)
            # Both ids, so a row can only ever surface its own subject's comment.
            .join(TaskComment, and_(TaskComment.id == model.comment_id, comment_subject_id == subject_id))
            .join(subject, subject.id == subject_id)
            .where(*owned)
            .order_by(model.id.desc())
            .limit(capped)
        )
        return statement.where(model.read_at.is_(None)) if unread_only else statement

    tasks = arm(Task, model.task_id, TaskComment.task_id).where(model.task_id.in_(readable))
    if author_join:
        tasks = tasks.join(AdminUser, AdminUser.id == TaskComment.author_id)
    rows = [(row, comment, task, None) for row, comment, task in session.execute(tasks).all()]
    if principal is not None:
        tickets = arm(Ticket, model.ticket_id, TaskComment.ticket_id)
        if not support_access.can_see_internal(principal):
            # The re-check below is the rule; these predicates keep hidden rows from eating the page.
            tickets = tickets.where(TaskComment.visibility == support_access.PUBLIC)
            if not principal.is_org_admin:
                tickets = tickets.where(Ticket.requester_id == principal.user_id)
        if principal.is_operator:
            with support_access.fleet_session() as fleet:
                found = list(fleet.execute(tickets).all())
                fleet.expunge_all()
        else:
            found = list(session.execute(tickets).all())
        rows += [
            (row, comment, None, ticket)
            for row, comment, ticket in found
            if support_access.can_view(principal, ticket)
            and (comment.visibility == support_access.PUBLIC or support_access.can_see_internal(principal))
        ]
    rows.sort(key=lambda found_row: found_row[0].id, reverse=True)
    return rows[:capped]


@dataclass(frozen=True)
class FeedEntry:
    """One mention on the bell, with what it is about: a task's (`task`) or a ticket's (`ticket`)."""

    mention: TaskCommentMention
    comment: TaskComment
    task: Optional[Task]
    ticket: Optional[Ticket]
    author_name: str


def mention_feed(
    session: Session, user_id: UUID, principal, *, unread_only: bool = True, limit: int = 50
) -> list[FeedEntry]:
    """`user_id`'s own mentions with their comment, subject and author, newest first — the bell's mention
    arm, read by `GET /api/inbox`. Scoped and re-checked by `feed_rows`."""
    rows = feed_rows(
        session, TaskCommentMention, [TaskCommentMention.user_id == user_id], principal,
        unread_only=unread_only, limit=limit, author_join=True,
    )
    names = names_of(comment.author_id for _m, comment, *_rest in rows)
    return [
        FeedEntry(
            mention=mention, comment=comment, task=task, ticket=ticket,
            author_name=names.get(comment.author_id, "") or "",
        )
        for mention, comment, task, ticket in rows
    ]
