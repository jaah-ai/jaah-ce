"""Client error telemetry over HTTP (`app/client_events.py` is the single home of what is kept and for how long).

`POST /api/client-events`: any signed-in session reports; only a flagged one stores. `GET /api/client-events` and
`GET /api/client-events/flagged`: the operator's Audit > Client errors tab, every org's reports and who is flagged.
`GET`/`PUT /api/orgs/{org_id}/client-telemetry`: the org switch (the membership switch rides `PATCH /api/users/{id}`).
Every read and the org switch are `@superadmin_only(Tenancy.OPERATOR)`: the subject is the fleet's console health,
and a report carries another tenant's routes and error text.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import client_events, db_async, keyset, login_funnel
from app.actors import stamp_actor
from app.auth import require_superadmin
from app.authz.catalog import Tenancy
from app.authz.declarations import always, superadmin_only
from app.authz.principal import Principal, current_principal
from app.db_async import on_session
from app.entity_shape import from_row, names_of
from app.models.audit import ClientEvent
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.routes.caller import caller_session
from app.routes.orgs import _live_one, _one

router = APIRouter(tags=["client-events"])

ClientEventKind = Literal[
    "render_error", "chunk_error", "stall", "uncaught_error", "page_load", "navigation", "slow_read", "slow_write"
]


class ClientEventIn(BaseModel):
    kind: ClientEventKind
    occurred_at: datetime
    # Bounded loosely (the store clips to the column limits) so an oversized body is refused before any work.
    route: Optional[str] = Field(None, max_length=2 * client_events.MAX_ROUTE)
    message: str = Field("", max_length=4 * client_events.MAX_MESSAGE)
    detail: dict[str, Any] = Field(default_factory=dict)
    build_version: Optional[str] = Field(None, max_length=client_events.MAX_BUILD_VERSION)

    @field_validator("detail")
    @classmethod
    def _detail_fits(cls, detail: dict[str, Any]) -> dict[str, Any]:
        size = len(json.dumps(detail, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8"))
        if size > client_events.MAX_DETAIL_BYTES:
            raise ValueError(f"detail is {size} bytes; the limit is {client_events.MAX_DETAIL_BYTES}")
        return detail


class ClientEventsIn(BaseModel):
    events: list[ClientEventIn] = Field(min_length=1, max_length=client_events.MAX_EVENTS_PER_POST)


class AcceptedOut(BaseModel):
    accepted: int


class ClientEventOut(BaseModel):
    id: int
    kind: str
    org_id: Optional[UUID] = None
    org_name: Optional[str] = None
    user_id: Optional[UUID] = None
    user_email: Optional[str] = None
    route: Optional[str] = None
    message: str
    detail: dict[str, Any]
    build_version: Optional[str] = None
    user_agent: Optional[str] = None
    client_ip: Optional[str] = None
    occurred_at: datetime
    created_at: datetime


class ClientEventsPage(BaseModel):
    items: list[ClientEventOut]
    #: Pass as `before_id` for the next (older) page; null on the last.
    next_before_id: Optional[int] = None


class FlaggedOrgOut(BaseModel):
    id: UUID
    name: str


class FlaggedUserOut(BaseModel):
    id: UUID
    email: str
    org_id: Optional[UUID] = None
    org_name: Optional[str] = None


class FlaggedOut(BaseModel):
    orgs: list[FlaggedOrgOut]
    users: list[FlaggedUserOut]


class TelemetrySwitch(BaseModel):
    enabled: bool


@router.post("/api/client-events", response_model=AcceptedOut)
@always(Tenancy.ORG_SCOPED)
async def post_client_events(
    body: ClientEventsIn,
    request: Request,
    principal: Principal | None = Depends(current_principal),
) -> AcceptedOut:
    """Store the batch when this session is flagged; otherwise accept and drop it (`accepted: 0`). `ALWAYS` binds
    nothing, so the session is the caller's own org's (`caller_session`)."""
    events = [event.model_dump() for event in body.events]
    user_agent, client_ip = request.headers.get("user-agent"), login_funnel.source_ip(request)
    return AcceptedOut(accepted=await db_async.run_in_session(_store, principal, events, user_agent, client_ip))


def _store(
    session: Session, principal: Principal | None, events: list[dict[str, Any]], user_agent: Optional[str],
    client_ip: Optional[str],
) -> int:
    """`post_client_events`' DB half: the store when the caller is flagged, on their own org's session."""
    with caller_session(principal, session) as (session, user):
        if not client_events.is_flagged(session, user):
            return 0
        accepted = client_events.store(session, user, events, user_agent=user_agent, client_ip=client_ip)
        session.commit()
        return accepted


def _user_emails(session: Session, user_ids: set[UUID]) -> dict[UUID, str]:
    if not user_ids:
        return {}
    return {user.id: user.email for user in session.scalars(select(AdminUser).where(AdminUser.id.in_(user_ids)))}


@router.get("/api/client-events", response_model=ClientEventsPage)
@superadmin_only(Tenancy.OPERATOR)
@on_session
def list_client_events(
    session: Session,
    org_id: Optional[UUID] = None,
    user_id: Optional[UUID] = None,
    kind: Optional[ClientEventKind] = None,
    before_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500),
    _: AdminUser = Depends(require_superadmin),
) -> ClientEventsPage:
    """Every org's reports, newest first, one keyset page on `id`."""
    query = select(ClientEvent).order_by(ClientEvent.id.desc())
    if org_id is not None:
        query = query.where(ClientEvent.org_id == org_id)
    if user_id is not None:
        query = query.where(ClientEvent.user_id == user_id)
    if kind is not None:
        query = query.where(ClientEvent.kind == kind)
    if before_id is not None:
        query = query.where(ClientEvent.id < before_id)
    rows, more = keyset.page(session, query, limit)
    orgs = names_of(session, Org, (row.org_id for row in rows))
    emails = _user_emails(session, {row.user_id for row in rows if row.user_id is not None})
    return ClientEventsPage(
        items=[
            from_row(
                ClientEventOut, row,
                org_name=orgs.get(row.org_id) if row.org_id else None,
                user_email=emails.get(row.user_id) if row.user_id else None,
            )
            for row in rows
        ],
        next_before_id=rows[-1].id if more else None,
    )


@router.get("/api/client-events/flagged", response_model=FlaggedOut)
@superadmin_only(Tenancy.OPERATOR)
@on_session
def list_flagged(session: Session, _: AdminUser = Depends(require_superadmin)) -> FlaggedOut:
    """Every org and membership whose console reports, by name."""
    orgs = session.execute(
        select(Org.id, Org.name).where(Org.client_telemetry.is_(True)).order_by(Org.name)
    ).all()
    users = list(session.scalars(select(AdminUser).where(AdminUser.client_telemetry.is_(True))))
    names = names_of(session, Org, (user.org_id for user in users))
    return FlaggedOut(
        orgs=[FlaggedOrgOut(id=row.id, name=row.name) for row in orgs],
        users=sorted(
            (
                from_row(FlaggedUserOut, user, org_name=names.get(user.org_id)) for user in users
            ),
            key=lambda flagged: flagged.email.lower(),
        ),
    )


@router.get("/api/orgs/{org_id}/client-telemetry", response_model=TelemetrySwitch)
@superadmin_only(Tenancy.OPERATOR)
@on_session
def get_org_client_telemetry(
    session: Session, org_id: str, _: AdminUser = Depends(require_superadmin),
) -> TelemetrySwitch:
    row = _one(session, org_id)
    return TelemetrySwitch(enabled=row.client_telemetry)


@router.put("/api/orgs/{org_id}/client-telemetry", response_model=TelemetrySwitch)
@superadmin_only(Tenancy.OPERATOR)
@on_session
def put_org_client_telemetry(
    session: Session,
    org_id: str,
    body: TelemetrySwitch,
    user: AdminUser = Depends(require_superadmin),
) -> TelemetrySwitch:
    """Turn the org's reporting on or off; it stays as set until changed."""
    row = _live_one(session, org_id)
    if row.client_telemetry != body.enabled:
        row.client_telemetry = body.enabled
        stamp_actor(row, user)
        session.commit()
    return TelemetrySwitch(enabled=row.client_telemetry)
