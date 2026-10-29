"""How many static worktrees an org's project may hold on one node — THE ONE plan gate every write that adds a
static slot calls.

`cap(session, org_id)` is the org's static slots — its override, else its plan's `static_slots`
(`app/plan_terms.py::static_slots`; plans default it to 0 = none), or None — no cap — on the operator plane, whose
own fleet is not sold a plan. No org, or a tenant org that does not exist, reads as 0 (fail-closed, as
`is_tenant_org`).

`refusal(...)` is the one wording of a refused write — the assignment's `static_slots` override
(`routes/assignments.py`), the one surface that adds a static slot. It
refuses only a value that CHANGES and lands above the cap, so an unchanged value from before the plan was lowered
saves as is.
Nothing here retires a slot already held: lowering a plan stops new static slots, it never kills one.

The console hides every control that adds a static slot while the viewer's org's cap is 0 (`AdminOut.static_slots`).
Nothing here writes or commits.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app import plan_terms
from app.models.orgs import Org
from app.tenancy.ids import is_tenant_org

#: The largest static count accepted anywhere (an assignment's override, a plan): the column is a 32-bit integer
#: and the port grid's index ceiling bounds what is ever declared, so a larger number is a typo — a field error,
#: never a 500.
MAX_STATIC_SLOTS = 10_000


def cap(session: Session, org_id: Optional[UUID]) -> Optional[int]:
    """`org_id`'s static-slot cap per (project, node); None = no cap (the operator plane)."""
    if not is_tenant_org(org_id):
        return None
    org = session.get(Org, org_id) if org_id is not None else None
    return plan_terms.static_slots(session, org) if org is not None else 0


def refusal(limit: Optional[int], wanted: Optional[int], current: Optional[int] = None) -> Optional[str]:
    """Why `wanted` static slots may not be saved under `limit`, or None when it may."""
    if limit is None or wanted is None or wanted <= limit or wanted == current:
        return None
    if limit == 0:
        return "Static slots are not included in this org's plan"
    return f"This org's plan allows at most {limit} static slots per project per node"
