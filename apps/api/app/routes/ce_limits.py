"""`GET /api/ce/limits` — the community edition's caps and what they hold now (`app/ce_caps.py::status`), for the
console's limits surface. 404 on the cloud edition.

The caller's own org, entered by the handler itself (`@always(Tenancy.ORG_SCOPED)`): the edition's one org, org zero
(`app/ce_bootstrap.py::CE_ORG_ID`), whose operator seat its one user holds — or a tenant org (the tests' own).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import edition, ce_bootstrap, ce_caps
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app.authz.principal import Principal, current_principal
from app.db import SessionLocal
from app.entity_shape import tenant_org_or_404
from app.tenancy import seam

router = APIRouter(prefix="/api/ce", tags=["ce"])

NOT_CE = "Not available on this edition"


class CapRefusalOut(BaseModel):
    detail: str
    kind: str
    limit: int
    upgrade_url: str


class AccountsOut(BaseModel):
    used: int
    cap: int
    #: What adding one more would be refused with (`ce_caps.CapRefusal.payload`); null while one fits.
    refusal: Optional[CapRefusalOut] = None


class AgentsOut(BaseModel):
    running: int
    cap: int
    #: Ready tasks the last dispatch pass held back at the cap.
    queued: int


class NodeOut(BaseModel):
    online: bool
    last_seen: Optional[str]


class CeLimitsOut(BaseModel):
    accounts: AccountsOut
    agents: AgentsOut
    node: NodeOut
    upgrade_url: str


@router.get("/limits", response_model=CeLimitsOut)
@always(Tenancy.ORG_SCOPED)
def get_ce_limits(principal: Principal | None = Depends(current_principal)) -> Any:
    if not edition.IS_CE:
        raise HTTPException(status_code=404, detail=NOT_CE)
    if principal is not None and principal.org_id == ce_bootstrap.CE_ORG_ID:
        org_id = principal.org_id
    else:
        org_id = tenant_org_or_404(principal, detail=NOT_CE)
    with seam.bound_to(org_id), SessionLocal() as session:
        return ce_caps.status(session, org_id)
