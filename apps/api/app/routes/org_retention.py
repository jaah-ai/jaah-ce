"""An org's own retention windows — the API behind Settings › Retention.

Every rule (bounds, what each class covers, the sweep) is `app/org_retention.py`'s. Both doors are `@org_admin`: an
Org Owner/Admin their own org (another org is a 404), an operator any org. A tenant is refused with 403 naming the plan
feature while its plan leaves Retention out (`plan_terms.require_switch`); an operator never is.

The PUT replaces all three values; `null` = the deployment's window. A value outside the class's range is refused
(422) naming the range. Audit: `UserActivityMiddleware` records each write.
"""
from __future__ import annotations

from functools import partial
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import org_retention
from app.actors import stamp_actor
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin
from app.authz.principal import Principal, current_principal
from app.clock import utcnow
from app.db import get_session
from app.entity_shape import administered_org_or_404
from app.models.identity import AdminUser
from app.models.orgs import Org

router = APIRouter(tags=["orgs"])


class RetentionClassOut(BaseModel):
    #: The org's own window held inside the current range (what the sweep applies); null = `default_days`.
    days: Optional[int]
    #: The stored value when the range has since moved past it (`days` is then its clamp); else null.
    clamped_from: Optional[int] = None
    #: The deployment's window, what unset means.
    default_days: int
    min_days: int
    max_days: int


class RetentionOut(BaseModel):
    transcripts: RetentionClassOut
    events: RetentionClassOut
    artifacts: RetentionClassOut


class RetentionIn(BaseModel):
    transcripts: Optional[int] = None
    events: Optional[int] = None
    artifacts: Optional[int] = None


#: `administered_org_or_404` under the Retention switch.
_org = partial(administered_org_or_404, switch=org_retention.SWITCH)


def _out(org: Org) -> RetentionOut:
    def one(data_class: str) -> RetentionClassOut:
        limits = org_retention.bounds(data_class)
        stored = org_retention.stored_days(org, data_class)
        days = None if stored is None else org_retention.clamp(data_class, stored)
        return RetentionClassOut(days=days, clamped_from=stored if stored != days else None,
                                 default_days=limits.default_days, min_days=limits.min_days, max_days=limits.max_days)

    return RetentionOut(**{data_class: one(data_class) for data_class in org_retention.CLASSES})


@router.get("/api/orgs/{org_id}/retention", response_model=RetentionOut)
@org_admin(Tenancy.CALLER_PLANE)
def get_retention(
    org_id: str,
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> RetentionOut:
    """The org's three windows and the range each may take."""
    return _out(_org(session, org_id, principal))


@router.put("/api/orgs/{org_id}/retention", response_model=RetentionOut)
@org_admin(Tenancy.CALLER_PLANE)
def put_retention(
    org_id: str,
    body: RetentionIn,
    actor: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> RetentionOut:
    """Replace the org's three windows. 422 naming the range when a value falls outside it."""
    org = _org(session, org_id, principal)
    wanted = body.model_dump()
    for data_class, days in wanted.items():
        why = None if days is None else org_retention.refusal(data_class, days)
        if why:
            raise HTTPException(status_code=422, detail=why)
    for data_class, days in wanted.items():
        setattr(org, org_retention.CLASSES[data_class][0], days)
    org.updated_at = utcnow()
    stamp_actor(org, actor)
    session.commit()
    return _out(org)
