"""The create surface's half of the org boundary: what a route does with the org its caller named.

Two different refusals. The first (is this a real, enabled tenant) is the only check of its kind: `org_id`
has no foreign key and the write seam never questions a caller-set value, so without it a made-up uuid is
a 201. The second (is a different org already in force) is a legibility layer over `write_seam`, which
stays the backstop: a route that forgets this call is still refused one flush later, as an opaque 500.

The second exists because `authz._bind_read_scope` binds a superadmin to their own org even on operator
routes whose org picker lists every tenant; picking another would otherwise 500. It self-retires once the
read-seam flip binds `OPERATOR` (then `current_scope()` is not a `UUID`). It is not coverage.
A census test lists the routes that must call it.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.orgs import Org
from app.tenancy.seam import current_scope


def named_org(session: Session, org_id: UUID) -> UUID:
    """The org this create names, checked to be a real, enabled tenant; 409 if a different org is in force.

    Returns the value unchanged so the call site reads `org_id=named_org(session, body.org_id)` and cannot
    be dropped as a bare guard.

    The existence check happens nowhere else (no FK; the write seam's unbound arm accepts any caller-set
    `org_id`). On `nodes` a bad `org_id` is unrepairable: `trg_nodes_org_id_immutable` refuses later
    UPDATEs. A disabled org is refused too; `ui/OrgSelect` hiding it is a courtesy, not a control.
    `Org` is the tenancy axis, so this read is not narrowed by the read seam.

    422 for a missing/disabled org (bad input), 409 for a reachable-org conflict with the session.
    Existence is checked first so a typo'd uuid is reported as such."""
    org = session.get(Org, org_id)
    if org is None or not org.enabled:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No enabled organization with id {org_id}. A new row must name a tenant that exists "
                "and is in service — pick one under Customers "
                "(app/tenancy/create_surface.py::named_org)."
            ),
        )
    scope = current_scope()
    if isinstance(scope, UUID) and scope != org_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This session is bound to org {scope}, so it cannot create a row in org {org_id}. "
                "Operator-plane writes are still scoped to the signed-in operator's own org until the "
                "read seam binds the fleet-wide plane for a superadmin; create it from that org, or "
                "wait for that change (app/tenancy/create_surface.py::named_org)."
            ),
        )
    return org_id
