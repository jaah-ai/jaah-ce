"""A tenant org's isolation defaults (D15, SOC 2 G-28/G-21/G-30) — the one home for what a TENANT org gets
when nobody chose otherwise, the operator exception that widens it, and the NodeLink plan term.

1. **Dedicated nodes.** A tenant's work never runs on a Multi-Node Host owned by another org: no co-located
   node is created for it there (`app/vnodes.py::place_virtual_node`, which the trial node also goes
   through), no task is placed on one it already has (`app/placement_grants.py::allowed_nodes`, which
   static leasing and the claim floor both read), and no slot, pool or CI runner is put
   on one (`app/vnodes.py::refuse_work_on_host`). `orgs.colocation_allowed` is the operator's exception.
   A Shared pool host — an operator host marked for Shared hostings (`is_shared_pool_host`) — is exempt:
   Shared is shared by design, so the Shared offering, a Shared hosting and the trial node place there and
   take work there without the exception (risk register R-SHARED). Every other foreign host stays refused.
2. **NodeLink (shared filesystems) is a plan term.** A tenant org may use it only when its plan's
   `nodelink_allowed` (or the operator's per-org `orgs.nodelink_allowed_override`) says so
   (`plan_terms.nodelink_allowed`). Without it no NodeLink or store of its is created or provisioned
   (`app/nodelinks.py`, `routes/org_shared_store.py`), no node of its is attached to one, and every node of
   its is served an empty list, so it unmounts them all (`nodelinks.desired_for_node`). The retired `orgs.shared_store_allowed` exception is no longer read
   (revision 0405 carried every holder onto the override).
3. **Account auth kind** is not a tenant default: a new account takes OAuth wherever the provider offers it,
   for every org (`default_auth_kind`); which (provider, auth kind) pairs an org may use at all is
   its plan's provider grid (`app/plan_terms.py`).

Org zero (Operator; `tenancy/ids.py::OPERATOR_PLANE_ORG_IDS`) keeps NodeLink
and Multi-Node-Host co-location: every reader asks `is_tenant_org` first. Existing tenant orgs
were not flipped: revision 0318 granted each exception to the orgs already using it, and
a SQL report lists every tenant org's standing so each is migrated or recorded as an
accepted exception in the risk register."""
from __future__ import annotations

from typing import Iterable, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from app.models.orgs import Org
from app.models.worktrees import VnodeHost
from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS, is_tenant_org

#: What the create doors (a co-located node, the trial node) answer when a tenant org holds no exception.
#: Names no host and no other org: the tenant reads the trial node's note verbatim.
FOREIGN_HOST_REFUSED = (
    "This organization runs on dedicated nodes only. An operator can record an exception for it."
)
#: What the work doors answer for a node already co-located without the exception. Neutral on purpose: the
#: tenant must not learn its node shares a machine; the operator reads the Isolation tab.
NODE_TAKES_NO_WORK = "This node cannot take work for this organization. Ask an operator."
#: What a door answers when a tenant org whose NodeLink is off asks for it.
SHARED_STORE_OFF = "NodeLink is off for this organization. An operator can turn it on."

# Raw SQL keyed by id, as `app/vnodes.py`'s marker facts are: an operator host's `virtual_nodes` rows
# live in org zero, so an ORM read through the seam would answer "not co-located" to a tenant caller and
# skip the refusal. Returns ids and bools, never rows (a site on the tenancy-bypass census).
#: Which of the given nodes are co-located on a host of ANOTHER org whose own org is a tenant holding no
#: co-location exception. Org zero is exempt here as in `colocation_allowed`, and a Shared pool
#: host as in `is_shared_pool_host` (the last clause is that rule, in SQL).
_FOREIGN_HOSTED_AMONG_SQL = text(
    "SELECT v.node_id FROM virtual_nodes v "
    "JOIN vnode_hosts h ON h.node_id = v.host_node_id "
    "JOIN orgs o ON o.id = v.tenant_org_id "
    "WHERE v.node_id IN :ids AND h.org_id <> v.tenant_org_id AND NOT o.colocation_allowed "
    "AND o.id NOT IN :operator_orgs "
    "AND NOT (h.shared_capacity IS NOT NULL AND h.org_id IN :operator_orgs)"
).bindparams(bindparam("ids", expanding=True), bindparam("operator_orgs", expanding=True))


def _flags(session: Session, org_id: Optional[UUID]) -> bool:
    if org_id is None:
        return False
    # `orgs` is the tenancy axis, so the seam does not narrow this read.
    row = session.execute(select(Org.colocation_allowed).where(Org.id == org_id)).first()
    return bool(row[0]) if row is not None else False


def colocation_allowed(session: Session, org_id: Optional[UUID]) -> bool:
    """May `org_id`'s nodes live on a Multi-Node Host another org owns? Always for an operator org; for a
    tenant only with the operator's recorded exception. `None` is a tenant without one (fail-closed)."""
    return not is_tenant_org(org_id) or _flags(session, org_id)


def shared_store_allowed(session: Session, org_id: Optional[UUID]) -> bool:
    """May `org_id` use NodeLink (the shared credential store)? Always for an operator org; a tenant only when
    its plan term says so (`plan_terms.nodelink_allowed`). `None` is a tenant without it (fail-closed)."""
    if not is_tenant_org(org_id):
        return True
    if org_id is None:
        return False
    from app import plan_terms  # local: plan_terms pulls in operator settings and user activity

    # `orgs` is the tenancy axis, so the seam does not narrow this read.
    org = session.get(Org, org_id)
    return org is not None and plan_terms.nodelink_allowed(session, org)


def is_shared_pool_host(host: VnodeHost) -> bool:
    """Is `host` in the Shared hosting pool — an operator host marked for Shared hostings? Any org's node may
    live and work there without the co-location exception (module docstring, item 1)."""
    return host.shared_capacity is not None and not is_tenant_org(host.org_id)


def require_colocation_allowed(session: Session, org_id: Optional[UUID], host: VnodeHost) -> None:
    """422 unless `org_id` may have a node on `host`, a Multi-Node Host another org owns."""
    if not is_shared_pool_host(host) and not colocation_allowed(session, org_id):
        raise HTTPException(status_code=422, detail=FOREIGN_HOST_REFUSED)


def require_shared_store_allowed(session: Session, org_id: Optional[UUID]) -> None:
    """403 unless `org_id` may use NodeLink."""
    if not shared_store_allowed(session, org_id):
        raise HTTPException(status_code=403, detail=SHARED_STORE_OFF)


def foreign_hosted_among(session: Session, node_ids: Iterable[UUID]) -> frozenset[UUID]:
    """Which of `node_ids` no work may be placed on: co-located on another org's host that is not a Shared
    pool host, the node's org a tenant without the exception. One statement."""
    wanted = sorted({node_id for node_id in node_ids if node_id is not None})
    if not wanted:
        return frozenset()
    params = {"ids": wanted, "operator_orgs": sorted(OPERATOR_PLANE_ORG_IDS)}
    return frozenset(row[0] for row in session.execute(_FOREIGN_HOSTED_AMONG_SQL, params))


def default_auth_kind(accepted: Iterable[str]) -> str:
    """The kind a new account takes when its creator names none, in any org: OAuth when the provider accepts
    it, else the provider's first kind (a key-only vendor lands on its key)."""
    from app import account_auth

    kinds = tuple(accepted) or (account_auth.AUTH_KIND_OAUTH,)
    if account_auth.AUTH_KIND_OAUTH in kinds:
        return account_auth.AUTH_KIND_OAUTH
    return kinds[0]
