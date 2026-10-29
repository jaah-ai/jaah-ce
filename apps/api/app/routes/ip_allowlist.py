"""An org's IP allowlist — the API behind Settings › Organization › IP allowlist and the customer page's Security tab.

Every rule (what holds, where, who is exempt) is `app/ip_allowlist.py`'s. `GET /api/auth/me/ip-allowlist` tells any
signed-in person whether their own org's list holds for them (Profile › Security), before it ever refuses them. Both doors are `@org_admin`: an Org
Owner/Admin their own org (another org is a 404, as `entity_shape.administered_org_or_404`), an operator any org. A tenant is
refused with 403 naming the plan feature while its plan leaves the IP allowlist out (`plan_terms.require_switch`);
an operator never is, so they can always clear a customer's list (the break-glass).

The PUT replaces the whole list. A non-empty list that leaves out the saver's own address is refused, so nobody
locks themselves out; an operator is exempt from the allowlist, so their save is not held to it.

Audit: `UserActivityMiddleware` records each write (the path names the org; the body the ranges).
"""
from __future__ import annotations

import ipaddress
from datetime import datetime
from functools import partial
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import act_as, ip_allowlist
from app.actors import actor_names, stamp_actor
from app.auth import require_user
from app.clock import utcnow
from app.authz.catalog import Tenancy
from app.authz.declarations import always, org_admin, session_only
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.entity_shape import administered_org_or_404, flush_or_409
from app.models.identity import AdminUser
from app.models.orgs import OrgIpRange

router = APIRouter(tags=["orgs"])


class IpRangeOut(BaseModel):
    id: UUID
    #: Normalized: a bare address reads as /32 (IPv4) or /128 (IPv6), host bits dropped.
    cidr: str
    label: str
    updated_at: datetime
    updated_by: Optional[str]
    #: The changer's email, for the caption's hover; None where the reader's scope cannot see that person.
    updated_by_email: Optional[str]


class IpRangesOut(BaseModel):
    #: Empty = every address is allowed.
    ranges: list[IpRangeOut]
    #: The caller's address as the API sees it, so they can add it.
    your_ip: Optional[str]


class IpAllowlistStatusOut(BaseModel):
    #: Whether the caller's org holds them to a list now (plan switch on, a range saved, the caller not exempt).
    on: bool


class IpRangeIn(BaseModel):
    cidr: str = Field(max_length=64)
    label: str = Field(default="", max_length=ip_allowlist.MAX_LABEL)


class IpRangesIn(BaseModel):
    ranges: list[IpRangeIn]


#: `administered_org_or_404` under the IP allowlist switch.
_org = partial(administered_org_or_404, switch=ip_allowlist.SWITCH)


def _host(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _out(session: Session, org_id: UUID, request: Request) -> IpRangesOut:
    rows = ip_allowlist.stored_ranges(session, org_id)
    ids = {row.updated_by_id for row in rows if row.updated_by_id is not None}
    # Staff reads "Jaah support" to both sides; the operator still finds who in the email on hover.
    names = actor_names(session, ids, withhold_staff=True)
    # Seam-filtered like the names: a tenant reader gets no address for an operator-side changer.
    emails = dict(
        session.execute(select(AdminUser.id, AdminUser.email).where(AdminUser.id.in_(ids))).all()
    ) if ids else {}
    return IpRangesOut(
        ranges=[
            IpRangeOut(id=row.id, cidr=str(row.cidr), label=row.label, updated_at=row.updated_at,
                       updated_by=names.get(row.updated_by_id), updated_by_email=emails.get(row.updated_by_id))
            for row in rows
        ],
        your_ip=_host(request),
    )


def _parsed(body: IpRangesIn) -> list[tuple[ip_allowlist.Network, str]]:
    """The list as networks with trimmed labels: 422 naming the first bad range or the first duplicate."""
    if len(body.ranges) > ip_allowlist.MAX_RANGES:
        raise HTTPException(
            status_code=422, detail=f"At most {ip_allowlist.MAX_RANGES} ranges. Merge some into wider ones."
        )
    parsed: list[tuple[ip_allowlist.Network, str]] = []
    seen: set[ip_allowlist.Network] = set()
    for item in body.ranges:
        try:
            network = ip_allowlist.parse_range(item.cidr)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if network in seen:
            raise HTTPException(status_code=422, detail=f"{network} is in the list twice. Remove one and save again.")
        seen.add(network)
        parsed.append((network, item.label.strip()))
    return parsed


@router.get("/api/orgs/{org_id}/ip-ranges", response_model=IpRangesOut)
@org_admin(Tenancy.CALLER_PLANE)
def get_ip_ranges(
    org_id: str,
    request: Request,
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> IpRangesOut:
    """The org's allowlist, in the order entered, and the caller's own address."""
    org = _org(session, org_id, principal)
    return _out(session, org.id, request)


@router.put("/api/orgs/{org_id}/ip-ranges", response_model=IpRangesOut)
@org_admin(Tenancy.CALLER_PLANE)
def put_ip_ranges(
    org_id: str,
    body: IpRangesIn,
    request: Request,
    actor: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> IpRangesOut:
    """Replace the org's allowlist. Empty turns it off. Refused (422) when it is not empty and leaves out the
    caller's own address, unless the caller is an operator."""
    org = _org(session, org_id, principal)
    parsed = _parsed(body)
    host = _host(request)
    if parsed and not ip_allowlist.exempt(act_as.person(actor)) and not ip_allowlist.allowed(
        (network for network, _ in parsed), host
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Your current address {host or 'unknown'} isn't in the list, so saving would lock you out. "
            "Add it, or a range that contains it, and save again.",
        )
    existing = {ipaddress.ip_network(str(row.cidr)): row for row in ip_allowlist.stored_ranges(session, org.id)}
    wanted = dict(parsed)
    for network, row in existing.items():
        if network not in wanted:
            session.delete(row)
    for network, label in wanted.items():
        row = existing.get(network)
        if row is None:
            row = OrgIpRange(org_id=org.id, cidr=str(network), label=label)
            stamp_actor(row, actor, created=True)
            session.add(row)
        elif row.label != label:
            row.label = label
            row.updated_at = utcnow()
            stamp_actor(row, actor)
    flush_or_409(
        session, "Someone else saved this list just now. Reload it and save again.",
        constraints=frozenset({"uq_org_ip_ranges_org_id_cidr"}),
    )
    session.commit()
    ip_allowlist.invalidate(org.id)
    return _out(session, org.id, request)


@router.get("/api/auth/me/ip-allowlist", response_model=IpAllowlistStatusOut)
@always(Tenancy.BOOTSTRAP)
@session_only
def my_ip_allowlist(
    actor: AdminUser = Depends(require_user), session: Session = Depends(get_session)
) -> IpAllowlistStatusOut:
    """Whether the caller's org restricts the networks they may sign in from. The ranges stay the admins'."""
    person = act_as.person(actor)
    return IpAllowlistStatusOut(
        on=not ip_allowlist.exempt(person) and bool(ip_allowlist.enforced_ranges(session, actor.org_id))
    )
