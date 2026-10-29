"""The bell's feed: `GET /api/inbox`, `POST /api/inbox/seen` and `POST /api/inbox/unseen` (access-model C7).

Eleven kinds, each DERIVED from the row it is about — there is no notifications table (G6):
- `mention` — the caller's `task_comment_mentions` rows (`comment_mentions.mention_feed`), on a task
  (`task_id`/`task_title`) or a support ticket (`ticket_id`/`ticket_title`); `read_at` is the row's own;
- `reaction` — someone else's emoji on the caller's comment (`comment_reactions.reaction_feed`), shaped as a
  mention with `author_name` the reactor and `emoji` the key; `read_at` is the reaction row's own;
- `access_request` — a pending request the caller may decide (`access_requests.pending_for`); `read_at` is
  always null and the item is gone once anyone decides it;
- `access_decision` — the caller's own decided request; `read_at` is `requester_seen_at`, which `seen` stamps,
  after which it leaves the unread feed;
- `support_ticket` — a ticket with news for the caller since their `ticket_reads` stamp (or ever): for its
  requester a public post by anyone else (`last_responder_public_at`), for an operator the ticket itself or
  the customer side's latest post (`max(created_at, last_customer_activity_at)`) on a ticket not yet
  solved or closed. An Org Admin who did not raise the ticket gets none; `id` is the ticket's;
- `support_task_done` — an operator's: a linked task finished (`ticket_task_links.done_at`), nobody has
  acknowledged it (`done_acked_at`), and the caller has not read the ticket since; `id` is the link's.
- `billing` — an Org Owner/Admin's: their tenant org's unresolved `billing_notices` (`billing/notices.py::feed`);
  `read_at` is their `billing_notice_reads` stamp, which `seen` writes; `notice` is the wire shape the
  Billing page reads (`notices.wire`). Nobody else — a member, an operator — gets any.
- `trigger_failed` / `trigger_digest` / `automation_alert` — the caller's own `trigger_notices` rows in the active
  org (`app/trigger_notices.py::feed`): a trigger the system stopped, the daily digest, and (an Owner's or Admin's)
  an automation failing or stalling (`app/automation_alerts.py`); `read_at` is the row's own, which `seen` stamps;
  `trigger_notice` is `trigger_notices.wire` (the server's sentence and link).
- `api_key_expiring` — the caller's own unrevoked API key in the active org reminded since they last opened it
  (`api_keys.reminder_feed`: a reminder stamp later than `reminder_seen_at`); `id` is the key's, `seen` stamps
  `reminder_seen_at`; `api_key_notice` is `api_keys.reminder_wire` (the server's sentence and link).
Every item carries `org_name`, the readable name of the org it belongs to (`app/org_names.py`): the caller's
active org, except that a mention, a reaction or a support item names its task's or ticket's (an operator's
tickets are every org's). A person in several orgs sees only the active org's bell — another org's items
wait until they act in it.
`unread_only=false` (the Unread | All switch's All) adds the read rows of every kind that keeps a read stamp —
mentions, reactions, decisions, billing and the trigger kinds — and `unseen` clears that stamp ("mark as unread").
Pending requests, both support kinds and key reminders keep no read history: they are listed while unread only.
Both support kinds are unread by construction, and `seen` upserts the ticket's `ticket_reads` row — the
same stamp the ticket page writes (`routes/support.py::stamp_read`), so the list's unread marker agrees.

`@always(Tenancy.ORG_SCOPED)` like `routes/access_requests.py`, and for its reason: a requester with no
role at all must still hear the answer. The mention arm stays narrowed to tasks and tickets the caller
reads now — an internal note's to operators.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from app import (
    access_requests,
    api_keys,
    comment_mentions,
    comment_notify,
    comment_reactions,
    edition_features,
    org_names,
    support_access,
    trigger_notices,
)
from app import support_lifecycle as lc
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app.authz.principal import Principal, current_principal
from app.entity_shape import optional_uuid
from app.models.support import Ticket, TicketRead, TicketTaskLink
from app.routes.access_requests import caller_session
from app.tenancy.labels import org_refs
from app.tenancy.ids import is_tenant_org

# The community edition has no billing: `_billing_reader` is False there, so this name is never read.
if edition_features.enabled("billing"):
    from app.billing import notices as billing_notices

router = APIRouter(prefix="/api/inbox", tags=["inbox"])

Kind = Literal[
    "mention", "reaction", "access_request", "access_decision", "support_ticket", "support_task_done", "billing",
    "trigger_failed", "trigger_digest", "automation_alert", "api_key_expiring",
]
SUPPORT_KINDS = ("support_ticket", "support_task_done")
TRIGGER_KINDS = ("trigger_failed", "trigger_digest", "automation_alert")

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class InboxItem(BaseModel):
    """One bell entry. `kind` decides which of the optional fields are set (the C10 render seam)."""

    kind: Kind
    #: A mention's or reaction's integer id, a request's uuid, a ticket's id or a link's — unique within its `kind`.
    id: str
    created_at: datetime | None
    read_at: datetime | None
    #: The readable name of the org this item belongs to (`app/org_names.py`); `None` only for a dangling org id.
    org_name: str | None = None
    # mention
    task_id: int | None = None
    task_title: str | None = None
    ticket_id: int | None = None
    ticket_title: str | None = None
    # support_ticket / support_task_done (with `ticket_id`/`ticket_title` above)
    ticket_status: str | None = None
    comment_id: int | None = None
    author_name: str | None = None
    excerpt: str | None = None
    # reaction: the key (`models.REACTION_EMOJIS`); `author_name` is who reacted
    emoji: str | None = None
    # access_request / access_decision
    request: dict | None = None
    # billing: `billing/notices.py::wire`
    notice: dict | None = None
    # trigger_failed / trigger_digest / automation_alert: `trigger_notices.py::wire`
    trigger_notice: dict | None = None
    # api_key_expiring: `api_keys.py::reminder_wire`
    api_key_notice: dict | None = None


class SeenItem(BaseModel):
    kind: Kind
    id: str


class SeenIn(BaseModel):
    items: list[SeenItem] = Field(default_factory=list, max_length=comment_mentions.MAX_READ_IDS)
    #: Stamp every unread mention of the caller's read, whatever `items` names — "mark all read".
    all_mentions: bool = False
    #: The same for reactions on the caller's comments.
    all_reactions: bool = False


@router.get("", response_model=list[InboxItem])
@always(Tenancy.ORG_SCOPED)
def get_inbox(
    unread_only: bool = True, limit: int = 50, principal: Principal | None = Depends(current_principal)
) -> list[InboxItem]:
    """The caller's bell, newest first: mentions, reactions, their own decided requests, billing and trigger
    notices (unread only by default), pending requests they may decide, and the support kinds and key reminders
    (unread only, always)."""
    limit = max(1, min(limit, comment_mentions.MAX_READ_IDS))
    with caller_session(principal) as (session, user):
        mentions = comment_mentions.mention_feed(session, user.id, principal, unread_only=unread_only, limit=limit)
        reactions = comment_reactions.reaction_feed(session, user.id, principal, unread_only=unread_only, limit=limit)
        # Per row: an operator's ticket mentions and reactions live in the tickets' orgs, not the active one.
        refs = org_refs(session, [
            (entry.task or entry.ticket).org_id for entry in (*mentions, *reactions)  # type: ignore[union-attr]
        ])

        def subject_org(entry) -> str | None:
            ref = refs.get((entry.task or entry.ticket).org_id)
            return ref.name if ref is not None else None

        items: list[InboxItem] = [
            InboxItem(
                kind="mention", id=str(entry.mention.id), created_at=entry.mention.created_at,
                read_at=entry.mention.read_at, comment_id=entry.comment.id, author_name=entry.author_name,
                excerpt=comment_notify.excerpt(entry.comment.body), org_name=subject_org(entry),
                **(
                    {"task_id": entry.task.id, "task_title": entry.task.title}
                    if entry.task is not None
                    else {"ticket_id": entry.ticket.id, "ticket_title": entry.ticket.title}
                ),
            )
            for entry in mentions
        ]
        items += [
            InboxItem(
                kind="reaction", id=str(entry.reaction.id), created_at=entry.reaction.created_at,
                read_at=entry.reaction.read_at, comment_id=entry.comment.id, author_name=entry.reactor_name,
                excerpt=comment_notify.excerpt(entry.comment.body), emoji=entry.reaction.emoji,
                org_name=subject_org(entry),
                **(
                    {"task_id": entry.task.id, "task_title": entry.task.title}
                    if entry.task is not None
                    else {"ticket_id": entry.ticket.id, "ticket_title": entry.ticket.title}
                ),
            )
            for entry in reactions
        ]
        pending = access_requests.pending_for(session, principal, limit=limit)
        decided = access_requests.decisions(session, user.id, unread_only=unread_only, limit=limit)
        names = access_requests.names_of(
            session, [uid for row in [*(r for r, _s in pending), *decided] for uid in (row.requester_id, row.decided_by_id)]
        )
        for row, scope in pending:
            items.append(InboxItem(
                kind="access_request", id=str(row.id), created_at=row.created_at, read_at=None,
                request=access_requests.wire(row, scope=scope, names=names, show_name=True),
            ))
        for row in decided:
            scope, show = access_requests.readable_scope(session, principal, row)
            items.append(InboxItem(
                kind="access_decision", id=str(row.id), created_at=row.decided_at or row.created_at,
                read_at=row.requester_seen_at,
                request=access_requests.wire(row, scope=scope, names=names, show_name=show),
            ))
        if _billing_reader(principal):
            assert principal is not None and principal.org_id is not None
            items += [
                InboxItem(
                    kind="billing", id=str(notice.id), created_at=notice.created_at, read_at=read_at,
                    notice=billing_notices.wire(notice),
                )
                for notice, read_at in billing_notices.feed(
                    session, principal.org_id, user.id, unread_only=unread_only, limit=limit
                )
            ]
        if principal is not None and principal.org_id is not None:
            items += [
                InboxItem(
                    kind=row.kind, id=str(row.id), created_at=row.created_at, read_at=row.read_at,
                    trigger_notice=trigger_notices.wire(row),
                )
                for row in trigger_notices.feed(
                    session, principal.org_id, user.id, unread_only=unread_only, limit=limit
                )
            ]
            items += [
                InboxItem(
                    kind="api_key_expiring", id=str(row.id), created_at=reminded_at, read_at=None,
                    api_key_notice=api_keys.reminder_wire(session, row),
                )
                for row, reminded_at in api_keys.reminder_feed(session, principal.org_id, user.id, limit=limit)
            ]
        # The rest are the caller's own, in the org they are acting in.
        active = org_names.org_name(session, principal.org_id if principal is not None else None)
        for item in items:
            if item.kind not in ("mention", "reaction"):
                item.org_name = active
    # Its own plane, after the caller's: the operator's tickets are every org's (`support_session`).
    items.extend(_support_items(principal, limit))
    items.sort(key=lambda item: item.created_at or _EPOCH, reverse=True)
    return items[:limit]


def _billing_reader(principal: Principal | None) -> bool:
    """An Owner/Admin of a TENANT org — the only people a billing notice is for (an operator's org has none)."""
    return (
        edition_features.enabled("billing") and principal is not None and principal.org_id is not None and is_tenant_org(principal.org_id)
        and not principal.is_operator and principal.is_org_admin
    )


def _support_items(principal: Principal | None, limit: int) -> list[InboxItem]:
    """The two support kinds for this caller, newest first — see the module docstring for the rules. None in an
    edition without the Help assistant."""
    if not edition_features.enabled("support_assistant"):
        return []
    from app.routes.support import support_session

    with support_session(principal) as (session, user):
        assert principal is not None
        read = and_(TicketRead.ticket_id == Ticket.id, TicketRead.user_id == user.id)
        if principal.is_operator:
            stamp = func.greatest(Ticket.created_at, func.coalesce(Ticket.last_customer_activity_at, Ticket.created_at))
            # Only tickets still in play: a customer's reply reopens a solved one (`support_lifecycle`), so
            # this drops nothing new — only every settled ticket an operator never opened.
            where = [Ticket.status.not_in((lc.SOLVED, lc.CLOSED))]
        else:
            stamp = Ticket.last_responder_public_at
            where = [Ticket.requester_id == user.id, stamp.is_not(None)]
        rows = session.execute(
            select(Ticket.id, Ticket.title, Ticket.status, stamp, Ticket.org_id)
            .outerjoin(TicketRead, read)
            .where(*where, or_(TicketRead.last_read_at.is_(None), stamp > TicketRead.last_read_at))
            .order_by(stamp.desc())
            .limit(limit)
        ).all()
        if not principal.is_operator:
            done = []
        else:
            done = session.execute(
                select(TicketTaskLink.id, Ticket.id, Ticket.title, Ticket.status, TicketTaskLink.done_at, Ticket.org_id)
                .join(Ticket, Ticket.id == TicketTaskLink.ticket_id)
                .outerjoin(TicketRead, read)
                .where(
                    TicketTaskLink.done_at.is_not(None),
                    TicketTaskLink.done_acked_at.is_(None),
                    or_(TicketRead.last_read_at.is_(None), TicketTaskLink.done_at > TicketRead.last_read_at),
                )
                .order_by(TicketTaskLink.done_at.desc())
                .limit(limit)
            ).all()
        # One read for every ticket's org: an operator's tickets span orgs.
        refs = org_refs(session, [row[-1] for row in (*rows, *done)])
        items = [
            InboxItem(
                kind="support_ticket", id=str(ticket_id), created_at=at, read_at=None,
                ticket_id=ticket_id, ticket_title=title, ticket_status=status,
                org_name=refs[org_id].name if org_id in refs else None,
            )
            for ticket_id, title, status, at, org_id in rows
        ]
        items.extend(
            InboxItem(
                kind="support_task_done", id=str(link_id), created_at=at, read_at=None,
                ticket_id=ticket_id, ticket_title=title, ticket_status=status,
                org_name=refs[org_id].name if org_id in refs else None,
            )
            for link_id, ticket_id, title, status, at, org_id in done
        )
        return items


def _support_seen(principal: Principal | None, ticket_ids: list[int], link_ids: list[int]) -> int:
    """Stamp the caller's read of each named ticket (a `support_task_done` names its link). Returns how
    many tickets were stamped; one the caller may not view is skipped silently, as a 404 would say nothing."""
    if not ticket_ids and not link_ids:
        return 0
    if not edition_features.enabled("support_assistant"):
        return 0
    from app.routes.support import stamp_read, support_session

    with support_session(principal) as (session, user):
        assert principal is not None
        ids = set(ticket_ids)
        if link_ids:
            ids.update(session.scalars(select(TicketTaskLink.ticket_id).where(TicketTaskLink.id.in_(link_ids))))
        tickets = [
            ticket for ticket in session.scalars(select(Ticket).where(Ticket.id.in_(ids)).order_by(Ticket.id))
            if support_access.can_view(principal, ticket)
        ]
        for ticket in tickets:
            stamp_read(session, ticket, user)
        session.commit()
        return len(tickets)


class _Named:
    """`items` split by kind into the ids each stamp takes; a malformed id is skipped, never a 422."""

    def __init__(self, items: list[SeenItem]) -> None:
        def ints(kinds: tuple[str, ...], digits: int) -> list[int]:
            return [
                int(i.id) for i in items
                if i.kind in kinds and i.id.isascii() and i.id.isdigit() and len(i.id) <= digits
            ]

        def uuids(kinds: tuple[str, ...]) -> list[UUID]:
            return [parsed for item in items if item.kind in kinds and (parsed := optional_uuid(item.id)) is not None]

        self.mentions = ints(("mention",), 9)
        self.reactions = ints(("reaction",), 9)
        self.tickets = ints(("support_ticket",), 18)
        self.links = ints(("support_task_done",), 18)
        self.decisions = uuids(("access_decision",))
        self.billing = uuids(("billing",))
        self.triggers = uuids(TRIGGER_KINDS)
        self.keys = uuids(("api_key_expiring",))


@router.post("/seen")
@always(Tenancy.ORG_SCOPED)
def mark_seen(body: SeenIn, principal: Principal | None = Depends(current_principal)) -> dict[str, int]:
    """Stamp items seen: a mention or reaction read (`comment_mentions.mark_read`,
    `comment_reactions.mark_read`), a decision seen by its requester, a billing notice read by an
    Owner/Admin, a trigger notice read by its owner, an API key's reminder by its owner. A pending request has nothing to stamp — it leaves the bell when it is decided. `all_mentions` / `all_reactions` read every unread one
    of the caller's, not just the listed ones."""
    named = _Named(body.items)
    org_id = principal.org_id if principal is not None else None
    with caller_session(principal) as (session, user):
        mentions = comment_mentions.mark_read(
            session, user.id, None if body.all_mentions else named.mentions, principal=principal
        ) if body.all_mentions or named.mentions else 0
        reactions = comment_reactions.mark_read(
            session, user.id, None if body.all_reactions else named.reactions, principal=principal
        ) if body.all_reactions or named.reactions else 0
        decisions = access_requests.mark_decisions_seen(session, user.id, named.decisions)
        billing = (
            billing_notices.mark_read(session, org_id, user.id, named.billing)
            if named.billing and _billing_reader(principal) and org_id is not None
            else 0
        )
        triggers = trigger_notices.mark_read(session, org_id, user.id, named.triggers) if org_id is not None else 0
        keys = api_keys.mark_reminders_seen(session, org_id, user.id, named.keys) if named.keys and org_id is not None else 0
        session.commit()
    return {
        "mention": mentions, "reaction": reactions, "access_decision": decisions, "billing": billing,
        "trigger": triggers, "api_key": keys,
        "support": _support_seen(principal, named.tickets, named.links),
    }


class UnseenIn(BaseModel):
    items: list[SeenItem] = Field(default_factory=list, max_length=comment_mentions.MAX_READ_IDS)


@router.post("/unseen")
@always(Tenancy.ORG_SCOPED)
def mark_unseen(body: UnseenIn, principal: Principal | None = Depends(current_principal)) -> dict[str, int]:
    """Clear the read stamp on the named items — "mark as unread" — so they rejoin the unread feed. Only the
    kinds that keep a read stamp (module docstring); any other kind is skipped. Scoped to the caller in each
    statement, like `seen`."""
    named = _Named(body.items)
    org_id = principal.org_id if principal is not None else None
    with caller_session(principal) as (session, user):
        mentions = (
            comment_mentions.mark_unread(session, user.id, named.mentions, principal=principal) if named.mentions else 0
        )
        reactions = (
            comment_reactions.mark_unread(session, user.id, named.reactions, principal=principal)
            if named.reactions else 0
        )
        decisions = access_requests.mark_decisions_unseen(session, user.id, named.decisions)
        billing = (
            billing_notices.mark_unread(session, org_id, user.id, named.billing)
            if named.billing and _billing_reader(principal) and org_id is not None
            else 0
        )
        triggers = trigger_notices.mark_unread(session, org_id, user.id, named.triggers) if org_id is not None else 0
        session.commit()
    return {"mention": mentions, "reaction": reactions, "access_decision": decisions, "billing": billing, "trigger": triggers}
