"""A node the operator runs for a tenant: a machine the tenant uses, not reconfigures.

Three kinds of node are the operator's to configure:

  * an allocated node — `Node.operator_allocated`, set when the operator moves a node or a Multi-Node Host out of
    the operator plane (`app/tenancy/transfer_node.py`) and flipped afterwards by the operator alone
    (`PUT /api/nodes/{id}`);
  * a Jaah Cloud machine — a node a live hosting names (`NodeHosting.node_id` or `.host_node_id`, status not
    `hosted_machines.ENDED_STATUSES`): a rented Dedicated host, a Shared or bundled node. A co-located node a tenant adds on its
    rented host has no hosting of its own, so it is not one;
  * a node on a shared machine — carried by a self-hosted operator shared host (`placement(...).shared_machine`).

On any of them, a tenant seat:

  * uses it — its projects are placed on it (`PUT /api/nodes/{id}/projects` is unchanged) and its tasks run there;
  * changes only `TENANT_FIELDS`: its name, description and tags, Enabled/Draining and its NodeLinks;
  * never changes anything else — its auto-update override, the machine's parameters (MicroVM CPU, RAM and disk,
    its home and launch record, its desired config).

Nor does a tenant re-issue its credential, add or change its CI runner pool, or mark it a MicroVM node
(`locked_for`/`refuse`). An allocated node is never deleted by a tenant (`refuse_allocated`); a Jaah Cloud machine
is ended through its hosting (`routes/nodes/delete.py::delete_node`). The operator is never locked.

The one home for that rule: the routes ask `locked_for`, `refuse`, `refuse_allocated`, `refuse_locked_fields` and
`jaah_cloud`, never the column or the hosting table. `placement` says where an operator's node runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional
from uuid import UUID

from fastapi import HTTPException

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features
from app.entity_shape import optional_uuid
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.worktrees import VirtualNode
from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS, is_operator_plane

#: The `PUT /api/nodes/{id}` fields a tenant still changes on an operator's node (decided with the owner).
TENANT_FIELDS = frozenset({"name", "description", "tags", "enabled", "draining"})


#: The field the operator flips; never a tenant's, on any node.
FIELD = "operator_allocated"

#: What a tenant reads at every refusal: who can act.
SETTINGS_LOCKED = "This node is run by the operator. Only the operator can change its settings."
DELETE_LOCKED = "This node was allocated by the operator. Only the operator can delete it."
CREDENTIAL_LOCKED = "This node is run by the operator. Only the operator can re-issue its credential."
OPERATOR_ONLY = "Only the operator can allocate a node."
OPERATOR_PLANE = "An operator node has no tenant to allocate it to. Move it to the tenant with Change org."
OPERATOR_CLOUD = "This machine runs in the operator's cloud account. Only the operator can delete it."


def operator(actor: AdminUser) -> bool:
    return is_operator_plane(actor.org_id, actor.is_superadmin)


def locked_for(row: Node, actor: AdminUser) -> bool:
    """Is `row` the operator's node (allocated, a Jaah Cloud machine, or carried by a shared machine) and `actor`
    not the operator?"""
    if operator(actor):
        return False
    if bool(row.operator_allocated) or row.id in jaah_cloud([row.id]):
        return True
    placed = placement([row.id]).get(row.id)
    return placed is not None and placed.shared_machine


def refuse(row: Node, actor: AdminUser, detail: str) -> None:
    """403 with `detail` when `row` is locked for `actor`."""
    if locked_for(row, actor):
        raise HTTPException(status_code=403, detail=detail)


def refuse_allocated(row: Node, actor: AdminUser, detail: str) -> None:
    """403 with `detail` when `row` is allocated and `actor` is not the operator — the delete door's question, where
    a Jaah Cloud machine gets the hosting's own answer instead."""
    if bool(row.operator_allocated) and not operator(actor):
        raise HTTPException(status_code=403, detail=detail)


def jaah_cloud(node_ids: Iterable[Optional[UUID]]) -> set[UUID]:
    """The ids among `node_ids` that are Jaah Cloud machines (a live hosting names them), in one statement.

    Read on the operator plane, as `delete_node` reads it: a hosting names the machine whatever org reads it."""
    ids = {node_id for node_id in node_ids if node_id is not None}
    if not ids:
        return set()
    if not edition_features.enabled("hostings"):
        return set()  # The community edition has no hostings.
    from app import hosted_machines  # Local: it imports the launch path.

    return hosted_machines.hosted_among(ids)


Location = Literal["hosting_vpc", "main_vpc"]


@dataclass(frozen=True)
class Placement:
    """Where a node's machine runs, when it is not the tenant's own infrastructure."""

    location: Location
    #: Carried by an operator-plane host that is not CP-launched: a machine the operator runs, not a Jaah Cloud
    #: instance (the node page's banner and the Nodes list's badge word it so).
    shared_machine: bool


def placement(node_ids: Iterable[Optional[UUID]]) -> dict[UUID, Placement]:
    """`{node id: Placement}` for each of `node_ids` that runs on a machine the operator runs, in four statements
    (two operator-plane sessions: its own and `jaah_cloud`'s).
    An absent key is the tenant's own infrastructure.

    The location is the MACHINE's (the node itself, or the Multi-Node Host carrying it), so every node a host carries
    reads the same one and one org's nodes on it can share one Jaah-managed NodeLink:

      * `hosting_vpc` — the machine is CP-launched (`hosts._cp_launched`) and either launched through the hosting
        connection (`hosting_provisioning.hosting_connection_id`) or named by a live hosting, itself or any node it
        carries (`jaah_cloud`);
      * `main_vpc` — otherwise, carried by an operator-plane host: a self-hosted operator shared host
        (`shared_machine`) or a CP-launched operator host in neither of those.

    Read on the operator plane, as `jaah_cloud` is: a tenant's org scope hides the operator host carrying its node."""
    ids = {node_id for node_id in node_ids if node_id is not None}
    if not ids:
        return {}
    if not edition_features.enabled("cloud_nodes"):
        return {}  # The community edition's one node is the user's own machine.
    from app import hosts, vnodes  # Local: both import the node routes' neighbours.
    from app.hosting_provisioning import hosting_connection_id  # Local: it imports the launch path.

    with vnodes.fleet_session() as fleet:
        carried = dict(fleet.execute(
            select(VirtualNode.node_id, VirtualNode.host_node_id).where(VirtualNode.node_id.in_(ids))
        ).all())
        host_ids = set(carried.values())
        # Every node each host carries, not only the asked ones: a hosting naming any of them names the machine.
        siblings = fleet.execute(
            select(VirtualNode.node_id, VirtualNode.host_node_id).where(VirtualNode.host_node_id.in_(host_ids))
        ).all() if host_ids else []
        machines = {row.id: row for row in fleet.scalars(select(Node).where(Node.id.in_(ids | host_ids)))}
    named = jaah_cloud(ids | host_ids | {node_id for node_id, _host in siblings})
    hosted_machines = named | {host_id for node_id, host_id in siblings if node_id in named}
    hosting_connection = hosting_connection_id()
    out: dict[UUID, Placement] = {}
    for node_id in ids:
        host_id = carried.get(node_id)
        machine = machines.get(host_id if host_id is not None else node_id)
        if machine is None:
            continue
        launched = hosts._cp_launched(machine)
        connection = (machine.infra or {}).get("connection_id") if isinstance(machine.infra, dict) else None
        on_hosting_connection = hosting_connection is not None and connection == str(hosting_connection)
        if launched and (machine.id in hosted_machines or on_hosting_connection):
            out[node_id] = Placement("hosting_vpc", shared_machine=False)
        elif host_id is not None and machine.org_id in OPERATOR_PLANE_ORG_IDS:
            out[node_id] = Placement("main_vpc", shared_machine=not launched)
    return out


def refuse_locked_fields(fields: dict[str, Any], row: Node, actor: AdminUser) -> None:
    """Settle a `PUT /api/nodes/{id}` payload, in place, against the operator's lock.

    `operator_allocated` is the operator's on every node: a non-operator CHANGING it is a 403. On a node `locked_for`
    the actor (allocated, Jaah Cloud or shared machine), a tenant CHANGING any field outside `TENANT_FIELDS` is a 403; one re-sent unchanged (the edit form resends what
    it shows) is dropped from the payload, so nothing of the locked half is ever written. A JSON field (`infra`)
    counts as unchanged when every key sent equals the stored one — the form sends a subset, and writing it back
    would delete the rest (it is replaced wholesale)."""
    if operator(actor):
        if fields.get(FIELD) and row.org_id in OPERATOR_PLANE_ORG_IDS:
            raise HTTPException(status_code=422, detail=OPERATOR_PLANE)
        return
    if FIELD in fields:
        if bool(fields[FIELD]) != bool(row.operator_allocated):
            raise HTTPException(status_code=403, detail=OPERATOR_ONLY)
        fields.pop(FIELD)
    if not locked_for(row, actor):
        return
    for name in [name for name in fields if name not in TENANT_FIELDS]:
        if not _unchanged(fields[name], getattr(row, name, None)):
            raise HTTPException(status_code=403, detail=SETTINGS_LOCKED)
        fields.pop(name)


def drop_on_create(fields: dict[str, Any]) -> None:
    """`POST /api/nodes` registers a node unallocated: allocating is a move or the operator's later PUT, never a
    registration (a tenant would otherwise fake the operator's decision and lock itself out). Dropped from the
    payload, so `NodeCreateIn` keeps sharing `NodeUpdateIn`'s fields."""
    fields.pop(FIELD, None)


def refuse_operator_cloud(session: Session, row: Node, actor: AdminUser) -> None:
    """403 for a non-operator deleting ANY node whose launch credential is the operator plane's (an allocated
    machine the operator later unlocked): the tenant's plane cannot sign that account's termination, so the
    instance would outlive its row on the operator's bill. `delete_node` asks it after the hosting refusal, whose
    sentence names the Billing page."""
    if operator(actor):
        return
    credential_id = (row.infra or {}).get("credential_id") if isinstance(row.infra, dict) else None
    if not credential_id:
        return
    if not edition_features.enabled("cloud_nodes"):
        return  # The community edition has no operator plane.
    from app import vnodes  # Local: `vnodes` imports the node routes' neighbours.

    with vnodes.fleet_session() as fleet:
        org_id = fleet.scalar(select(Credential.org_id).where(Credential.id == optional_uuid(credential_id)))
    if org_id in OPERATOR_PLANE_ORG_IDS:
        raise HTTPException(status_code=403, detail=OPERATOR_CLOUD)


def _unchanged(sent: Any, stored: Any) -> bool:
    if isinstance(sent, dict):
        stored = stored if isinstance(stored, dict) else {}
        return all(stored.get(key) == value for key, value in sent.items())
    return sent == stored
