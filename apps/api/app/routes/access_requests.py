"""Access requests: file one, list the boxes, approve or deny (access-model C7; rules in
`app/access_requests.py`).

`@always(Tenancy.ORG_SCOPED)`: the whole point is a caller who holds NO role on what they ask for — often no
role anywhere — so no feature key can floor these routes. The guard resolves no principal and binds no org,
so each handler requires a signed-in user and enters that user's own org itself (`seam.bound_to`), under
which another org's scope, request or user is indistinguishable from none. Every decision past that is
`may_manage` (`access_requests.may_decide`), the one gate every binding write asks.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import act_as
from app import access_requests, edition_features

# The community edition has no Slack: `_decide` reads this name only when the `slack` feature is on.
if edition_features.enabled("slack"):
    from app import slack_access_cards
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app.authz.principal import Principal, current_principal
from app.db import SessionLocal
from app.durable import notify_outbox
from app.entity_shape import StrictModel, UuidRefOut
from app.models.identity import AdminUser
from app.routes.access import ScopeIn
from app.tenancy import seam

router = APIRouter(prefix="/api/access/requests", tags=["access"])


class RequestIn(StrictModel):
    scope: ScopeIn
    #: Omitted → the least role there is (a refusal's hint names the least role that would admit it).
    role: str | None = None
    note: str | None = Field(default=None, max_length=access_requests.NOTE_MAX)


class DecisionIn(BaseModel):
    #: Never, by default (A2): an approval is not forced to be time-boxed.
    expires_at: datetime | None = None


class Person(UuidRefOut):
    pass


class RoleRef(BaseModel):
    slug: str
    name: str


class ScopeRef(BaseModel):
    type: str
    id: str
    #: None when the caller may neither read nor decide the scope — asking discloses no name.
    name: str | None


class RequestOut(BaseModel):
    id: UUID
    requester: Person
    role: RoleRef
    scope: ScopeRef
    note: str | None
    state: str
    created_at: datetime
    decided_at: datetime | None
    decided_by: Person | None
    binding_id: UUID | None


@contextmanager
def caller_session(principal: Principal | None) -> Iterator[tuple[Session, AdminUser]]:
    """A session bound to the caller's own org, and the caller's user row — 401 without a signed-in user
    (the agent principal included)."""
    if principal is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    with seam.bound_to(principal.org_id), SessionLocal() as session:
        user = act_as.own_row(session, principal)
        if user is None or user.is_system:
            raise HTTPException(status_code=401, detail="unauthorized")
        yield session, user


def _out(session: Session, principal: Principal, rows) -> list[dict]:
    """Rows on the wire. A `(row, scope)` pair is one the caller may decide, so it shows the scope's name;
    a bare row shows it only if the caller may read or decide the scope (`readable_scope`)."""
    resolved = []
    for item in rows:
        if isinstance(item, tuple):
            row, scope = item
            resolved.append((row, scope, True))
        else:
            scope, show = access_requests.readable_scope(session, principal, item)
            resolved.append((item, scope, show))
    names = access_requests.names_of(
        session, [uid for row, _s, _v in resolved for uid in (row.requester_id, row.decided_by_id)]
    )
    return [access_requests.wire(row, scope=scope, names=names, show_name=show) for row, scope, show in resolved]


@router.post("", response_model=RequestOut)
@always(Tenancy.ORG_SCOPED)
def create_request(
    body: RequestIn, response: Response, principal: Principal | None = Depends(current_principal)
) -> dict:
    """Ask for `role` (default: the least) at `scope`. 201 with the new request, or 200 with the open one
    already asking the same thing. The scope's approvers are DMed a click-to-decide card."""
    with caller_session(principal) as (session, user):
        row, created = access_requests.create_request(
            session, user, scope_type=body.scope.type, scope_id=body.scope.id, role_slug=body.role, note=body.note,
        )
        session.commit()
        if created:
            # Durable and asynchronous (`durable/notify_outbox.py`): the request answers without waiting on Slack.
            notify_outbox.forward("access_request_cards", str(row.id))
            session.refresh(row)
        response.status_code = 201 if created else 200
        (out,) = _out(session, principal, [row])
        return out


@router.get("", response_model=list[RequestOut])
@always(Tenancy.ORG_SCOPED)
def list_requests(
    box: Literal["pending", "mine", "history"] = "pending",
    principal: Principal | None = Depends(current_principal),
) -> list[dict]:
    """`pending`: the open requests the caller may decide, and only those. `history`: the decided ones the
    caller may decide. `mine`: the caller's own, every state."""
    # Requests are decided in the caller's own org, so an operator's list is never widened; their org
    # filter only hides it when that org is unselected.
    orgs = seam.request_org_filter()
    if orgs is not None and principal is not None and principal.org_id not in orgs:
        return []
    with caller_session(principal) as (session, user):
        if box == "pending":
            rows = access_requests.pending_for(session, principal)
        elif box == "history":
            rows = access_requests.history_for(session, principal)
        else:
            rows = access_requests.mine(session, user.id)
        return _out(session, principal, rows)


def _decide(request_id: UUID, verdict: str, expires_at: datetime | None, principal: Principal | None) -> dict:
    with caller_session(principal) as (session, user):
        row, _changed = access_requests.decide(session, user, principal, request_id, verdict, expires_at=expires_at)
        session.commit()
        # Also on a repeat: a card update that failed the first time is retried, as a repeat click does.
        if edition_features.enabled("slack"):
            slack_access_cards.after_decision(session, row)
        (out,) = _out(session, principal, [row])
        return out


@router.post("/{request_id}/approve", response_model=RequestOut)
@always(Tenancy.ORG_SCOPED)
def approve_request(
    request_id: UUID, body: DecisionIn | None = None, principal: Principal | None = Depends(current_principal)
) -> dict:
    """Approve: one binding for the requester (optional expiry, default never). Approving again returns
    the same request and writes nothing; a denied request is a 409. 404 for one the caller may not decide."""
    return _decide(request_id, access_requests.APPROVED, body.expires_at if body else None, principal)


@router.post("/{request_id}/deny", response_model=RequestOut)
@always(Tenancy.ORG_SCOPED)
def deny_request(request_id: UUID, principal: Principal | None = Depends(current_principal)) -> dict:
    """Deny: no binding. Denying again returns the same request; an approved request is a 409."""
    return _decide(request_id, access_requests.DENIED, None, principal)
