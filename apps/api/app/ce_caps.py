"""The community edition's caps on the control plane: at most `edition.CE_MAX_ACCOUNTS` AI provider accounts in total
and `edition.CE_MAX_AGENTS` agents at once (PRD §7: literals, never a setting, env var, flag or UI row).

Each cap is held twice on this side, by layers that refuse independently:
* Accounts — `require_account_headroom` at `POST /api/accounts` and before the sign-in wizard dispatches
  (`routes/commands.py`), then the `accounts_ce_cap` AFTER INSERT trigger (revision 202610262000), which
  catches every other insert path.
* Agents — `org_limits` governs `runner_slots` by `CE_MAX_AGENTS` (`_Entitlement.term`): the placer's
  `DispatchBudget` holds an over-cap ready task back as `ready` (it queues, never fails), and a console session
  at the cap is refused (`org_limits.require_headroom`, here as `refusal("agents")`). Over HTTP it answers the
  cap payload; the console WebSocket closes with its sentence as the reason (a close reason holds 123 bytes).

A refusal is `CapRefusal`, rendered by `app/main.py` as `409 {detail, kind, limit, upgrade_url}`. A cloud-only surface
answers `not_in_ce()`'s 404 instead: every removed route prefix (`app/main.py::_register_not_in_ce`) and each
door the edition shuts inside a shared router (registering or deleting a node: it has exactly one).
`GET /api/ce/limits` (`app/routes/ce_limits.py`) reads `status`. Nothing here runs on the cloud edition.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import edition, node_presence, org_limits
from app.entity_shape import iso_or_none
from app.models.credentials import Account
from app.models.nodes import Node

CapKind = Literal["accounts", "agents"]

_LIMITS: dict[str, int] = {"accounts": edition.CE_MAX_ACCOUNTS, "agents": edition.CE_MAX_AGENTS}
_COPY: dict[str, str] = {
    "accounts": f"Jaah Community Edition allows {edition.CE_MAX_ACCOUNTS} accounts. For more, use Jaah Cloud.",
    "agents": f"Jaah Community Edition runs {edition.CE_MAX_AGENTS} agents at once. For more, use Jaah Cloud.",
}

#: What a cloud-only surface answers in the community edition, with the cloud sign-up link (`not_in_ce`).
NOT_IN_CE = "Not in Jaah Community Edition. Available on Jaah Cloud."

#: The constraint the accounts trigger names in its error (revision 202610252247).
TRIGGER_CONSTRAINT = "accounts_ce_cap"

#: `{org: tasks}` the last dispatch pass held back at the Agents cap (`record_held_back`): the queue. One api
#: process serves the community edition, so the placer and the status read share it.
_held_back: dict[UUID, int] = {}


class CapRefusal(HTTPException):
    """A CE cap's 409, carrying the cap payload (`app/main.py` renders it)."""

    def __init__(self, kind: CapKind) -> None:
        super().__init__(status_code=409, detail=_COPY[kind])
        self.kind = kind
        self.limit = _LIMITS[kind]
        self.upgrade_url = edition.CLOUD_SIGNUP_URL

    def payload(self) -> dict[str, Any]:
        return {"detail": self.detail, "kind": self.kind, "limit": self.limit, "upgrade_url": self.upgrade_url}


def not_in_ce() -> JSONResponse:
    """The community edition's 404 for a cloud-only surface: one sentence naming Jaah Cloud, and its sign-up link."""
    return JSONResponse({"detail": NOT_IN_CE, "upgrade_url": edition.CLOUD_SIGNUP_URL}, status_code=404)


def refusal(kind: CapKind) -> CapRefusal:
    return CapRefusal(kind)


def http_refusal(detail: str) -> HTTPException:
    """The 409 for a refusal sentence a caller carried as text (the console's claim): the cap payload when it is
    the CE Agents cap's own, else a plain 409."""
    if edition.IS_CE and detail == _COPY["agents"]:
        return refusal("agents")
    return HTTPException(status_code=409, detail=detail)


def account_count(session: Session, org_id: UUID) -> int:
    """The org's `accounts` rows, whatever their state or provider (S4). The community edition holds one org, so these
    are all of them; the trigger counts the whole table."""
    return int(session.scalar(select(func.count()).select_from(Account).where(Account.org_id == org_id)) or 0)


def accounts_full(session: Session, org_id: UUID) -> bool:
    return account_count(session, org_id) >= edition.CE_MAX_ACCOUNTS


def require_account_headroom(session: Session, org_id: UUID) -> None:
    """409 `CapRefusal("accounts")` when one more account would pass the cap. Locks the org row first (held to the
    caller's commit), so two concurrent creates count one after the other."""
    org_limits.lock_org(session, org_id)
    if accounts_full(session, org_id):
        raise refusal("accounts")


def record_held_back(held: dict[UUID, int]) -> None:
    """Keep the dispatch pass's held-back tally (`org_limits.DispatchBudget.held_back`), replacing the last."""
    global _held_back
    _held_back = dict(held)


def _node(session: Session, org_id: UUID) -> dict[str, Any]:
    """The local node's heartbeat: online when any of the org's nodes sits on a live bus socket."""
    nodes = session.execute(select(Node.id, Node.name, Node.last_seen).where(Node.org_id == org_id)).all()
    # A node dials the bus by its uuid or its name (`nodelinks.py::_online_keys`).
    online = {key.lower() for key in node_presence.connected_node_ids(session)}
    seen: Optional[Any] = max((n.last_seen for n in nodes if n.last_seen is not None), default=None)
    return {
        "online": any(str(n.id) in online or n.name.lower() in online for n in nodes),
        "last_seen": iso_or_none(seen),
    }


def status(session: Session, org_id: UUID) -> dict[str, Any]:
    """The pinned `GET /api/ce/limits` body. A full account cap carries the refusal an add would meet, so the
    console says it before the form is filled."""
    used = account_count(session, org_id)
    full = used >= edition.CE_MAX_ACCOUNTS
    return {
        "accounts": {"used": used, "cap": edition.CE_MAX_ACCOUNTS,
                     "refusal": refusal("accounts").payload() if full else None},
        "agents": {
            "running": org_limits.usage(session, org_id, "runner_slots"),
            "cap": edition.CE_MAX_AGENTS,
            "queued": _held_back.get(org_id, 0),
        },
        "node": _node(session, org_id),
        "upgrade_url": edition.CLOUD_SIGNUP_URL,
    }
