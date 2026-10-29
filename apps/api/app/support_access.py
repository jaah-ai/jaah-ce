"""Who may do what to a support ticket — the one authorization home for the support surface.

Pure functions over a `Principal` and a row, so a route, a notifier and a test ask the same question. No
permission key: a ticket is reached by being its requester, an Org Admin of its org, or the operator,
never by a grant (`app/access_catalog.py` is untouched).

    operator    `Principal.is_operator` (`app/tenancy/ids.py::is_operator_plane`) — every ticket, every move
    org_admin   `Principal.is_org_admin` in the ticket's org — view, comment, close; never internal notes,
                priority, context or task links
    requester   the ticket's own requester — view, comment

The actor class is decided by PERMISSION precedence (operator > org_admin > requester): an Org Admin who
raised the ticket acts as the admin. A caller with no class cannot view the ticket, and a route answers
that with 404, never 403, so a ticket id reveals nothing. The move table these feed is
`app/support_lifecycle.py`.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app import access
from app import support_lifecycle as lc
from app.authz.principal import Principal
from app.models.support import Ticket
from app.models.task_extras import VISIBILITY_INTERNAL, VISIBILITY_PUBLIC

# The comment/attachment `visibility` vocabulary; its CHECK is `app/models/task_extras.py::subject_checks`.
PUBLIC = VISIBILITY_PUBLIC
INTERNAL = VISIBILITY_INTERNAL

# The list scopes (`GET /api/support/tickets?scope=`).
SCOPE_MINE = "mine"
SCOPE_ORG = "org"
SCOPE_ALL = "all"


def actor_class(principal: Principal, ticket: Ticket) -> Optional[str]:
    """This caller's class on this ticket, or `None` when they may not see it at all."""
    if principal.is_operator:
        return lc.OPERATOR
    if principal.is_org_admin and principal.org_id == ticket.org_id:
        return lc.ORG_ADMIN
    if ticket.requester_id == principal.user_id:
        return lc.REQUESTER
    return None


def reader_class(principal: Principal, ticket: Ticket) -> Optional[str]:
    """Which UNREAD rule applies to this reader — operator > requester > org_admin, a different order
    from `actor_class` on purpose: an admin who raised the ticket waits on staff's answer like any
    requester."""
    if principal.is_operator:
        return lc.OPERATOR
    if ticket.requester_id == principal.user_id:
        return lc.REQUESTER
    if principal.is_org_admin and principal.org_id == ticket.org_id:
        return lc.ORG_ADMIN
    return None


def can_view(principal: Principal, ticket: Ticket) -> bool:
    return actor_class(principal, ticket) is not None


def can_comment(principal: Principal, ticket: Ticket, visibility: str) -> bool:
    """The permission half of posting; whether the ticket's STATUS admits a reply is
    `support_lifecycle.may_reply` (a 409, not a 403)."""
    if visibility == INTERNAL:
        return can_see_internal(principal)
    return visibility == PUBLIC and can_view(principal, ticket)


def can_set_status(principal: Principal, ticket: Ticket, target: str) -> bool:
    """May this caller ever write `target`? Whether it is a legal move FROM the current status is
    `support_lifecycle.may_set` (a 409)."""
    actor = actor_class(principal, ticket)
    return actor is not None and target in lc.settable_targets(actor)


def can_set_priority(principal: Principal) -> bool:
    return principal.is_operator


def can_link(principal: Principal) -> bool:
    return principal.is_operator


def can_see_internal(principal: Principal) -> bool:
    return principal.is_operator


def can_see_context(principal: Principal) -> bool:
    return principal.is_operator


def can_list(principal: Principal, scope: str) -> bool:
    """`mine` is anyone's; `org` an Org Admin's (their own org); `all` the operator's."""
    if scope == SCOPE_MINE:
        return True
    if scope == SCOPE_ORG:
        return principal.is_org_admin
    if scope == SCOPE_ALL:
        return principal.is_operator
    return False


def requester_role(principal: Principal) -> str:
    """The requester's role as a ticket's context records it — computed here, never taken from a client."""
    if principal.is_operator:
        return lc.OPERATOR
    if principal.org_role == access.ORG_OWNER:
        return "org_owner"
    if principal.org_role == access.ORG_ADMIN:
        return lc.ORG_ADMIN
    return "member"


def _after(stamp: Optional[datetime], read_at: Optional[datetime]) -> bool:
    return stamp is not None and (read_at is None or stamp > read_at)


def is_unread(
    principal: Principal, ticket: Ticket, last_read_at: Optional[datetime], *, linked_task_done: bool
) -> bool:
    """Has something this reader should see happened since they last read the ticket?

    requester  a public post by anyone but them (`last_responder_public_at`)
    operator   the ticket itself, or the customer side's latest activity — or a linked task finished
               that no operator has acknowledged
    org_admin  any public post (`last_public_at`)

    Precedence is `reader_class`'s. `last_read_at` None means never read."""
    reader = reader_class(principal, ticket)
    if reader == lc.OPERATOR:
        latest = max(
            (stamp for stamp in (ticket.created_at, ticket.last_customer_activity_at) if stamp is not None),
            default=None,
        )
        return linked_task_done or _after(latest, last_read_at)
    if reader == lc.REQUESTER:
        return _after(ticket.last_responder_public_at, last_read_at)
    if reader == lc.ORG_ADMIN:
        return _after(ticket.last_public_at, last_read_at)
    return False


@contextmanager
def fleet_session() -> Iterator[Session]:
    """A fresh session on the fleet-wide plane, for the few reads a ticket needs across the org line —
    the one place the support surface widens (a scope-site census test names it).

    A ticket lives in the requester's org while its operators, their names and a linked task live in the
    operator's, so a caller's own plane cannot see the other side. Every caller hands in ids it already
    passed `can_view` on, or selects by the caller's OWN user id, and reads the narrow columns it needs:
    `linked_task_states` (a task's state), `app/comment_mentions.py` (the ticket's mention roster, the
    names on its thread, and the reader's own ticket mentions), and the notification leg's two ids-only
    reads (`links_for_task`, `operator_recipients`)."""
    from app.db import SessionLocal
    from app.tenancy import seam

    with seam.detached(), seam.operator_scope(), SessionLocal() as session:
        yield session


def linked_task_states(task_ids: Iterable[int]) -> dict[int, str]:
    """`{task_id: state}` for the tasks tickets link to — the ONLY thing read about a linked task.

    A linked task is usually in another org than its ticket (the operator's), so the caller's scope cannot
    see it. This reads on `fleet_session` and selects `tasks.state` alone — never the title or body, which stay the operator's; what an
    operator sees of the task is the link's own title snapshot. The ids come from link rows the caller
    already passed `can_view` on, so the block cannot reach a task nobody linked. A missing id (deleted in
    between) is simply absent."""
    ids = sorted({int(i) for i in task_ids if i is not None})
    if not ids:
        return {}
    from sqlalchemy import select

    from app.models.tasks import Task

    with fleet_session() as session:
        return {task_id: state for task_id, state in session.execute(
            select(Task.id, Task.state).where(Task.id.in_(ids))
        )}


def links_for_task(task_id: int, *, done_unacked: bool = False) -> list[tuple[int, int]]:
    """`[(link_id, ticket_id)]` for every ticket link to this task — ids only, oldest link first; with
    `done_unacked`, only the links whose done is stamped and not yet acknowledged (the "task done" marker).

    The task is the operator's and its links sit in each TICKET'S org (a tenant's, usually), so the done
    workflow (`app/durable/task_done.py::support_links`), bound to nothing, reads them on `fleet_session`
    and then binds each ticket's own org to write. Nothing about the ticket leaves here but its id."""
    from sqlalchemy import select

    from app.models.support import TicketTaskLink

    where = [TicketTaskLink.task_id == task_id]
    if done_unacked:
        where += [TicketTaskLink.done_at.is_not(None), TicketTaskLink.done_acked_at.is_(None)]
    with fleet_session() as session:
        return [(link_id, ticket_id) for link_id, ticket_id in session.execute(
            select(TicketTaskLink.id, TicketTaskLink.ticket_id).where(*where).order_by(TicketTaskLink.id)
        )]


def operator_recipients() -> list[UUID]:
    """Every operator's user id (`access.live_operators`) — who hears of a new ticket, a customer's reply and a
    linked task done. Read fleet-wide because the caller is a tenant's request or an unbound workflow."""
    with fleet_session() as session:
        return [user.id for user in access.live_operators(session)]
