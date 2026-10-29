"""The support ticket's status and priority vocabularies and its transition table — one home for all three.

A ticket is NOT a Task (NC4): it is never dispatched, and `app/task_lifecycle.py` is untouched. This
module copies that one's pattern instead — the vocabulary as constants, the allowed moves as DATA — so a
new move is a row below, never a branch at a call site. The columns carry CHECKs rendered from these
tuples (`app/models/support.py::Ticket`, revision 0298).

Who may make which move is keyed on the caller's ACTOR CLASS for this ticket (`app/support_access.py::
actor_class` decides it). Two kinds of move share the one table:

    SET    an explicit status write (`PATCH /api/support/tickets/{id}`)
    REPLY  the move a PUBLIC reply makes on its own (a user answering a `solved` ticket reopens it)

    requester  no status write at all; a public reply on `pending`/`solved` → `open`; `closed` is final
    org_admin  any non-closed status → `closed`; a public reply moves like the requester's
    operator   any status → any other (reopening `closed` included); a reply moves nothing

Stdlib only at import: revision 0298 renders its CHECKs from here.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = [
    "CLOSED",
    "DEFAULT_PRIORITY",
    "OPEN",
    "OPERATOR",
    "ORG_ADMIN",
    "PENDING",
    "PRIORITIES",
    "PRIORITY_RANK",
    "REPLY",
    "REQUESTER",
    "SET",
    "SOLVED",
    "STATUSES",
    "TRANSITIONS",
    "may_reply",
    "may_set",
    "reply_target",
    "settable_targets",
]

# ── Vocabularies ──────────────────────────────────────────────────────────────────────────────────
OPEN = "open"
PENDING = "pending"  # staff wait on the customer
SOLVED = "solved"  # staff think it is done; a customer reply reopens it
CLOSED = "closed"  # final for customers; only an operator reopens

STATUSES: tuple[str, ...] = (OPEN, PENDING, SOLVED, CLOSED)

PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")
DEFAULT_PRIORITY = "normal"
#: The `sort=priority` order: higher is more urgent.
PRIORITY_RANK: Mapping[str, int] = MappingProxyType({p: i for i, p in enumerate(PRIORITIES)})

# ── Actor classes (decided per ticket by `app/support_access.py::actor_class`) ───────────────────────
REQUESTER = "requester"
ORG_ADMIN = "org_admin"
OPERATOR = "operator"
ACTOR_CLASSES: tuple[str, ...] = (REQUESTER, ORG_ADMIN, OPERATOR)

# ── Move kinds ────────────────────────────────────────────────────────────────────────────────────
SET = "set"
REPLY = "reply"


def _rows() -> dict[tuple[str, str, str], str]:
    rows: dict[tuple[str, str, str], str] = {}
    for source in STATUSES:
        for target in STATUSES:
            if source != target:
                rows[(OPERATOR, source, target)] = SET
    for source in (OPEN, PENDING, SOLVED):
        rows[(ORG_ADMIN, source, CLOSED)] = SET
    for actor in (REQUESTER, ORG_ADMIN):
        for source in (PENDING, SOLVED):
            rows[(actor, source, OPEN)] = REPLY
    return rows


#: `(actor_class, from, to) -> SET | REPLY`. A triple absent here is not a move that actor may make.
TRANSITIONS: Mapping[tuple[str, str, str], str] = MappingProxyType(_rows())


# ── The DDL the vocabularies render (the ORM and revision 0298 both read these, so they cannot drift) ─
def _in_check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


TICKET_TITLE_MAX = 200
TICKET_TITLE_CHECK = "ck_tickets_title_length"
TICKET_TITLE_CHECK_SQL = f"char_length(title) <= {TICKET_TITLE_MAX}"
TICKET_STATUS_CHECK = "ck_tickets_status"
TICKET_STATUS_CHECK_SQL = _in_check("status", STATUSES)
TICKET_PRIORITY_CHECK = "ck_tickets_priority"
TICKET_PRIORITY_CHECK_SQL = _in_check("priority", PRIORITIES)
#: Which side spoke last in public: the customer's (requester or org admin) or staff's (an operator).
USER_SIDE = "user"
STAFF_SIDE = "staff"
REPLY_SIDES: tuple[str, ...] = (USER_SIDE, STAFF_SIDE)
TICKET_REPLY_SIDE_CHECK = "ck_tickets_last_reply_side"
TICKET_REPLY_SIDE_CHECK_SQL = f"last_reply_side IS NULL OR {_in_check('last_reply_side', REPLY_SIDES)}"
#: A ticket's lesson for Jarvis (`app/support_kb/lessons.py`): NULL none; `pending` owed a draft (the ticket was
#: solved or closed); `draft` the model's, awaiting an operator; `approved` served to Jarvis; `dismissed` never.
LESSON_PENDING = "pending"
LESSON_DRAFT = "draft"
LESSON_APPROVED = "approved"
LESSON_DISMISSED = "dismissed"
LESSON_STATUSES: tuple[str, ...] = (LESSON_PENDING, LESSON_DRAFT, LESSON_APPROVED, LESSON_DISMISSED)
#: The statuses a lesson is drafted on, and the only ones an operator may approve one on.
LESSON_TICKET_STATUSES: tuple[str, ...] = (SOLVED, CLOSED)
TICKET_LESSON_CHECK = "ck_tickets_lesson_status"
TICKET_LESSON_CHECK_SQL = f"lesson_status IS NULL OR {_in_check('lesson_status', LESSON_STATUSES)}"
MESSAGE_ROLES: tuple[str, ...] = ("user", "assistant")
MESSAGE_ROLE_CHECK = "ck_support_messages_role"
MESSAGE_ROLE_CHECK_SQL = _in_check("role", MESSAGE_ROLES)


def may_set(actor: str, source: str, target: str) -> bool:
    """May `actor` write status `target` over `source`? `source == target` is a no-op, not a move."""
    return TRANSITIONS.get((actor, source, target)) == SET


def settable_targets(actor: str) -> frozenset[str]:
    """Every status `actor` may ever write — the permission half (`support_access.can_set_status`)."""
    return frozenset(t for (a, _s, t), kind in TRANSITIONS.items() if a == actor and kind == SET)


def may_reply(actor: str, status: str) -> bool:
    """May `actor` post a PUBLIC reply on a ticket in `status`? `closed` is final for customers (a 409)."""
    return actor == OPERATOR or status != CLOSED


def reply_target(actor: str, status: str) -> str:
    """The status a public reply by `actor` leaves the ticket in (`status` itself when no row moves it).

    Raises `ValueError` where `may_reply` refuses, so no caller can reply past a closed ticket by skipping
    the check."""
    if not may_reply(actor, status):
        raise ValueError(f"a {actor} may not reply on a {status} ticket")
    for (a, source, target), kind in TRANSITIONS.items():
        if kind == REPLY and a == actor and source == status:
            return target
    return status
