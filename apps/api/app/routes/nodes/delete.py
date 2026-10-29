"""Node deletion: `DELETE /api/nodes/{node_id}` and `delete_node_row`, the teardown every remover shares."""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app import (
    edition, edition_features, ce_caps, node_account_sessions, node_credentials, node_send_intent, node_allocation, slot_teardown,
)
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature
from app.authz.object_access import can_access
from app.authz.principal import Principal, current_principal
from app.authz.visibility import ObjectAccess
from app.db import get_session
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.projects import Project
from app.models.worktrees import Worktree
from app.node_clone import clone_for_slot_path
from app.node_identity import is_uuid_shaped
from app.tenancy.copy import administrator_changes
from app.tenancy.ids import is_tenant_org
from app.routes.nodes.node_wire import _administers_own_hosts

if edition_features.enabled("cloud_nodes"):
    from app import cloud_node_launch, clouds
    from app.cloud_node_launch import CloudNodeError, CloudNodeUpstreamError
if edition_features.enabled("multi_node_hosts"):
    from app import vnodes

router = APIRouter(tags=["nodes"])
log = logging.getLogger("cockpit-nodes")


class NodeDeletedOut(BaseModel):
    """What a node deletion actually came to: 200 with this body, never a bare 204.

    Four consequences an operator must read: slot declarations removed, what was running in them, which
    directories survive on an unreachable host, and whether the machine was stopped. All are outcomes of a
    success, so none is an error and none is hidden."""

    slots_deleted: int
    # One line per slot that was holding something, e.g. "acme #3 (was task 42)": the ledger closed and a
    # `release_slot` intent was minted (`app/slot_teardown.py`).
    holders_ended: list[str] = []
    # One line per slot whose directory survives on the host, with the node's reason; normal for a dead node.
    node_erase_failures: list[str] = []
    instance_terminated: bool = False
    # Why the instance was not stopped: not CP-launched, no instance id or region, no recorded AWS account,
    # or AWS refused. None when it was terminated (`instance_terminated`).
    instance_detail: Optional[str] = None
    # `?uninstall=true` on a self-hosted machine: the uninstall frame reached its socket (`app/node_send_intent.py::
    # send_uninstall`). The machine acts on it on its own; nothing here waits for that.
    uninstall_requested: bool = False
    # Why it was not sent, with the command to run by hand. None when sent or not asked for.
    uninstall_detail: Optional[str] = None


#: What `delete_node` answers a tenant for a machine a live hosting names.
HOSTED_MACHINE = "This machine is hosted. Delete it from the Billing page."


@router.delete("/api/nodes/{node_id}", response_model=NodeDeletedOut, status_code=200)
@feature("nodes", "delete", tenancy=Tenancy.CALLER_PLANE)
async def delete_node(
    node_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
    uninstall: bool = Query(default=False),
) -> Any:
    """Delete a node: deregister it, cascade its slot declarations, end what was in them, tell the node best
    effort, and terminate the machine when the CP started it. Not gated on occupancy or liveness.
    `uninstall` (a machine, not a co-located node, that was not terminated): tell the machine to remove Jaah
    (step 7). Python callers pass it explicitly — the `Query` default is truthy.

    Superadmin on top of `nodes:delete` (checked before anything resolves): it destroys every project's
    worktrees on the host and terminates an instance, so it must not be a grantable way around
    `erase_slot`/`erase_project`. Refused before the id resolves. A tenant administrator
    (`_administers_own_hosts`) deletes their own
    org's nodes only: the raw lock is re-read through the seam (step 1). No occupancy/liveness gate, by operator
    decision (it would make a live node unretireable): holders are ended
    (`app/slot_teardown.py::end_holders_for_node`) and trees erased best effort, failures reported in
    `NodeDeletedOut`.

    Order:
      1. Lock the parent by id as the first DB statement; the seam read and authz probe run on the
         locked id.
      2. Lock every slot row, free ones included (a free row may be about to become a hold).
      3. End holders in this transaction. Residual: `slot_intents` cascades with the node, but the pushed
         frame carries the intent (`app/node_send_intent.py`), so a connected node still kills the runner;
         only the durable retry for an offline node is lost. `agent_sessions`' FK is `SET NULL`.
      4. Delete, commit, push the intents; nothing past here fails the request.
      5. Erase the trees best effort, sequentially (`slot_teardown.NODE_ERASE_TIMEOUT`).
      6. Terminate only with proof of which machine (`_terminate_node_instance`).

    Parent-then-child locking can deadlock with `lease_drain`; Postgres breaks it (a 500, or one node's
    pass retried). Deregistration is final: a heartbeating node shows as present-but-unregistered.

    The body past authorization is `delete_node_row`, which the org delete cascade calls too."""
    if edition.IS_CE:
        # The community edition has exactly one node, the one its boot creates (`app/ce_bootstrap.py`).
        return ce_caps.not_in_ce()
    node_uuid = _writable_node_uuid(session, node_id, user, principal)
    return await delete_node_row(
        session, node_uuid, user,
        reason=f"operator deleted node {node_id} from the console — end the runner and clear the tree",
        uninstall=uninstall,
        principal=principal,
    )


def _writable_node_uuid(session: Session, node_id: str, user: AdminUser, principal: Principal | None) -> UUID:
    """The node a mutating door may act on, locked: 403 unless `user` administers their own hosts (a tenant reads
    who can act, never the tier's name), 404 for a malformed id, an unknown node, or one this caller cannot see
    (D13: not reachable by URL; the door's own declaration still decides whether at all). No DB read before the
    lock, and the visibility check after it on purpose: the lock must be the first statement."""
    if not _administers_own_hosts(user, principal):
        raise HTTPException(
            status_code=403,
            detail=administrator_changes("Nodes") if is_tenant_org(user.org_id) else "superadmin required",
        )
    unknown = HTTPException(status_code=404, detail=f"unknown node '{node_id}'")
    if not is_uuid_shaped(node_id):
        raise unknown
    node_uuid = UUID(node_id)
    _lock_node_or_404(session, node_uuid)
    if not can_access(session, principal, "node", node_uuid, mode=ObjectAccess.WRITE):
        raise unknown
    return node_uuid


def _lock_node_or_404(session: Session, node_uuid: UUID) -> None:
    """`SELECT … FOR UPDATE` the node row, then re-read it through the seam: the raw `text()` lock is not
    narrowed by the read seam, so another org's node is the same 404 as an unknown one. Re-locking a row this
    transaction already holds is a no-op, so the route and `delete_node_row` may both call it."""
    locked_id = session.execute(
        text("SELECT id FROM nodes WHERE id = :id FOR UPDATE"), {"id": node_uuid}
    ).scalar_one_or_none()
    if locked_id is None or session.scalar(select(Node.id).where(Node.id == locked_id)) is None:
        raise HTTPException(status_code=404, detail=f"unknown node '{node_uuid}'")


async def delete_node_row(
    session: Session,
    node_uuid: UUID,
    actor: AdminUser,
    *,
    reason: str,
    force: bool = False,
    keep_row: bool = False,
    uninstall: bool = False,
    principal: Principal | None = None,
) -> NodeDeletedOut:
    """`delete_node`'s body past authorization (steps 1-7 of its docstring): the caller has decided `actor`
    may delete this node. `reason` is the sentence each ended holder records.

    `force` is the org delete cascade's (`routes/orgs.py`): the whole org goes, so the refusals that protect
    one node of a living org are skipped (an allocated node, a hosted machine, the operator-cloud lock). A
    CP-launched instance is still terminated and holders are still ended. A host still carrying nodes is
    refused even then: the caller deletes those first (the FK is RESTRICT).

    `keep_row` is the org soft delete's (`tenancy/org_lifecycle.py::wind_down`): the node is torn down
    exactly as here, but its `nodes` row stays — disabled, its credential revoked, hidden with its org
    (`app/models/orgs.py::in_live_org`) and shown again, offline, by Restore. `_keep_row` does by hand what the row's delete
    would have cascaded."""
    _lock_node_or_404(session, node_uuid)
    locked_id = node_uuid
    allocated = session.get(Node, locked_id)
    # The node's name, for labels only (logs, the erase audit, a terminated instance's tag lookup, the tailnet
    # hostname fallback); every address below is the uuid.
    node_name = allocated.name if allocated is not None else str(locked_id)
    if not force:
        # An allocated node is the operator's to delete (`app/node_allocation.py`).
        if allocated is not None:
            node_allocation.refuse_allocated(allocated, actor, node_allocation.DELETE_LOCKED)
        # A HOSTED MACHINE IS ENDED BY ITS HOSTING, never by this door (`app/hosted_machines.py::refuse_delete`).
        if edition_features.enabled("hostings"):  # The community edition has no hostings.
            from app import hosted_machines

            operator = principal is not None and principal.is_operator
            hosted_machines.refuse_delete(locked_id, operator=operator, tenant_detail=HOSTED_MACHINE)
        if allocated is not None:
            node_allocation.refuse_operator_cloud(session, allocated, actor)

    # A Multi-Node Host is refused while it carries a live node (the FK is RESTRICT; this gives a sentence). Each
    # co-located node is deleted by its own press. Its pending teardowns go with it (step 4): a host that is
    # going never finishes them, and an offline one would hold it in "Deleting" forever.
    # The community edition has no Multi-Node Hosts.
    carried = (
        vnodes.host_vnode_count(session, locked_id, live_only=True)
        if edition_features.enabled("multi_node_hosts") else None
    )
    if carried is not None:
        if carried:
            raise HTTPException(
                status_code=409,
                detail=f"Delete its {carried} node{'' if carried == 1 else 's'} first.",
            )
    # A co-located node's row is marked `deleting` before the `nodes` row goes, so the FK's `SET NULL` flips the
    # host's desired state atomically (`app/vnodes.py::mark_deleting`), at step 4.

    # Step 2: every slot row this node declares, locked, free ones included. The project name rides along for
    # the intent, erase dispatch and report.
    slots = session.execute(
        select(Worktree, Project.name)
        .join(Project, Project.id == Worktree.project_id)
        .where(Worktree.node_id == locked_id)
        .with_for_update(of=Worktree)
        .order_by(Project.name, Worktree.number)
    ).all()
    # Everything the post-commit hops need is resolved here, before the cascade, since it dies with the rows:
    #   · the holder's address (`task_id`/`launch_session_id`/`pid`) is on the `worktrees` row;
    #   · the clone path derives from `nodes.jaah_home` (`pool_slots._resolve_node_clone`) and would
    #     resolve to `""`, reporting every tree unerasable;
    #   · the dispatch address must be the node's uuid (`node_identity.bus_key`); with the row gone the name
    #     would miss an open socket.
    holds = [(slot_teardown.snapshot(row, session), project) for row, project in slots]
    # Per project, not per slot: both facts are a project's. Keyed by id: a name is unique per org only.
    per_project = {
        project_id: (
            _node_clone_for(session, locked_id, project_id),
            session.scalar(select(Project.stack_down_cmd).where(Project.id == project_id)),
        )
        for project_id in {row.project_id for row, _project in slots}
    }
    # The holder travels with the tree (an unerased tree owes that fact); `describe_holder` reads the
    # pre-delete snapshot.
    node_home = session.scalar(select(Node.jaah_home).where(Node.id == locked_id))
    # Each tree is erased where its row says it is (`node_clone.clone_for_slot_path`), falling back to the
    # project's derived clone.
    trees = [
        (
            project, row.number,
            clone_for_slot_path(node_home, row.path, row.number) or per_project[row.project_id][0],
            per_project[row.project_id][1],
            slot_teardown.describe_holder(snap),
        )
        for (snap, _p), (row, project) in zip(holds, slots, strict=True)
    ]
    # Folder slots, read before the cascade: the node erases them on its folder path.
    folder_project_ids = {(project, row.number): row.project_id for row, project in slots if not row.has_repo}

    # Step 3: end what is inside, in the delete's own transaction.
    issued, holders_ended = slot_teardown.end_holders_for_node(session, holds, reason=reason)
    if holders_ended:
        log.warning(
            "delete node %s: cascading %s slot row(s) and ending %s live hold(s): %s",
            node_name, len(trees), len(holders_ended), "; ".join(holders_ended),
        )
    # Step 4: the node row goes; its `worktrees` rows cascade (`ON DELETE CASCADE`) — or, `keep_row`, `_keep_row`.
    row = session.get(Node, locked_id)
    # Copied before the delete: step 6 needs it after the commit.
    infra = dict(row.infra or {})
    vnode = vnodes.mark_deleting(session, locked_id, actor.id) if edition_features.enabled("multi_node_hosts") else None
    dropped: list[vnodes.Teardown] = []
    if carried is not None:
        dropped = vnodes.drop_pending_teardowns(session, locked_id)
        vnodes.drop_host_marker(session, locked_id)
    # An add still in progress on this machine ends with it, or its order keeps reserving the name.
    if edition_features.enabled("cloud_nodes"):  # The community edition has no node orders.
        from app import node_orders  # deferred: node_orders imports this package (`cloud`, `register`, `node_wire`)

        node_orders.end_for_deleted_node(session, locked_id)
    # The accounts signed in on it lose that session with its config dir: the Accounts page says so.
    node_account_sessions.mark_sessions_lost(session, locked_id, row.org_id)
    if keep_row:
        _keep_row(session, row)
    else:
        session.delete(row)
    emit_console_event(session, entity="node", id=locked_id, action="deleted")
    session.commit()
    slot_teardown.push_intents(issued)
    if dropped:
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        await run_in_threadpool(vnodes.remove_moved_devices, dropped)
    if vnode is not None:
        # Past the commit, best effort: wake the host, and resolve the tailnet device by hostname while the name
        # is known if the host never reported its id.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        # The lookup before the nudge: a woken host can report `destroyed` within seconds.
        await run_in_threadpool(vnodes.record_device_before_delete, node_name, vnode.virtual_node_id)
        await run_in_threadpool(vnodes.nudge_host, vnode.host_node_id)

    # Step 5: best effort, sequential (each hop audits through this one Session). Short-circuited on the first
    # unreachable answer: a connected-but-wedged node costs a full `NODE_ERASE_TIMEOUT` per slot. A node that
    # answered and refused does not short-circuit (`slot_teardown.TreeErase`). The uuid is the address.
    erase_failures: list[str] = []
    unreachable: Optional[str] = None
    for project, number, clone, stack_down_cmd, held_by in trees:
        # An unerased tree's runner is still executing: the CP half (ledger, hold) happened, but the process is
        # killed only by `worktree_erase`. Named here so "3 holds ended" is not read as all done (as
        # `pool_slots.py::_detail_with_the_holders_fate`).
        still_running = (
            f"; the {held_by} hold is gone from the control plane, but that runner may still be RUNNING "
            f"in the tree — stop it on the host"
        ) if held_by else ""
        if unreachable is not None:
            erase_failures.append(f"{project} #{number}: {unreachable}{still_running}")
            continue
        if not clone:
            erase_failures.append(
                f"{project} #{number}: no clone path could be derived, so the directory was left "
                f"alone{still_running}"
            )
            continue
        erased = await slot_teardown.erase_tree_on_node(
            session,
            node_id=str(locked_id),
            node_label=node_name,
            project=project,
            number=number,
            clone_path=clone,
            stack_down_cmd=stack_down_cmd,
            actor_email=actor.email,
            folder_project_id=folder_project_ids.get((project, number)),
        )
        if erased.confirmed:
            continue
        erase_failures.append(f"{project} #{number}: {erased.reason}{still_running}")
        if not erased.reached:
            unreachable = erased.reason

    # Step 6: the machine.
    terminated, instance_detail = await _terminate_node_instance(session, node_name, infra)
    # Step 7: a self-hosted machine asked to deprovision. A co-located node is torn down by its host, and a
    # cloud machine is ended by its instance — one whose terminate failed keeps its software, never runs on bare.
    uninstall_requested, uninstall_detail = False, None
    if uninstall is True and vnode is None and not infra.get("instance_id"):
        uninstall_detail = await node_send_intent.send_uninstall(str(locked_id))
        uninstall_requested = uninstall_detail is None
    return NodeDeletedOut(
        slots_deleted=len(trees),
        holders_ended=holders_ended,
        node_erase_failures=erase_failures,
        instance_terminated=terminated,
        instance_detail=instance_detail,
        uninstall_requested=uninstall_requested,
        uninstall_detail=uninstall_detail,
    )


#: The cascaded children a kept row (`_keep_row`) keeps: the node's own history, which a restored node charts.
_KEPT_HISTORY = frozenset({"node_host_metrics", "node_disk_events", "node_session_usage"})
#: The `SET NULL` pointers that place something ON the node (an account's sessions, a vnode, a hosting), cleared
#: as the row's delete would; every other `SET NULL` pointer (alerts, audit, sessions, telemetry, orders) is
#: history and keeps naming the row it still names.
_PLACEMENTS = frozenset({("accounts", "node_id"), ("virtual_nodes", "node_id"), ("node_rentals", "node_id"),
                         ("node_rentals", "host_node_id")})


def _node_references() -> list[tuple[type, Any, str, str]]:
    """Every mapped `(class, column attribute, its table's name, ondelete)` whose FK points at `nodes.id`, read
    from the mappers so a new child is covered the day it is added."""
    found = []
    for mapper in Node.registry.mappers:
        for fk in mapper.local_table.foreign_keys:
            if fk.column.table.name == "nodes":
                attribute = mapper.get_property_by_column(fk.parent).class_attribute
                found.append((mapper.class_, attribute, mapper.local_table.name, (fk.ondelete or "").upper()))
    return found


def _keep_row(session: Session, row: Node) -> None:
    """`delete_node_row(keep_row=True)`'s step 4 in place of the row's delete, doing what its FKs would: every
    `CASCADE` child but `_KEPT_HISTORY` goes (slots, intents, runners, leases, NodeLink and project assignments,
    VMs…) and every `_PLACEMENTS` pointer is cleared — so a co-located node's host tears its VM down, and nothing
    re-converges onto, or holds an account or Compliance Delete on, a machine that is gone. Then the row stops
    being anyone's: credential revoked (`node_credentials.revoke`), disabled, out of its group and off the share."""
    for cls, column, table, ondelete in _node_references():
        if ondelete == "CASCADE" and table not in _KEPT_HISTORY:
            session.execute(delete(cls).where(column == row.id))
        elif ondelete == "SET NULL" and (table, column.key) in _PLACEMENTS:
            session.execute(update(cls).where(column == row.id).values({column.key: None}))
    node_credentials.revoke(row)
    row.enabled = False
    row.node_group_capacity_id = None
    # Mounts nothing now: Compliance Delete's census (`tenancy/offboard.py::_store_nodes`) must not wait on it.
    row.shared_accounts_root = ""


def _node_clone_for(session: Session, node_id: UUID, project_id: int) -> str:
    """Where this project's clone lives on this node, or `""` (`pool_slots._resolve_node_clone`, deferred
    import for its stated routes↔routes reason). Called only before the cascade: after the node row is
    deleted it answers `""` for every project."""
    from app.routes.pool_slots import _resolve_node_clone  # noqa: PLC0415

    project_row = session.scalar(select(Project).where(Project.id == project_id))
    if project_row is None:
        return ""
    return _resolve_node_clone(session, project_row, str(node_id))


async def _terminate_node_instance(
    session: Session, node_id: str, infra: dict[str, Any]
) -> tuple[bool, Optional[str]]:
    """Stop the machine behind a just-deleted node. Returns `(terminated, reason_it_was_not)`.

    Never raises (the deletion is committed) and never guesses: instance ids are account-scoped and
    `ec2.terminate_instances` treats NotFound as success, so a wrong guess leaves a machine billing forever.
    Three facts must be positively on the row, each refusing with its own sentence:
      1. An instance id and a region (absent: self-hosted, or an edited `infra`).
      2. `launched_by == "control-plane"` (else it may be terraform-managed).
      3. The signing credential, which is the account (older rows carry none;
         `node_group_reconcile._terminate_one` refuses a mismatch for the same reason).

    `run_in_threadpool`, not `asyncio.to_thread`: see `app/node_bus.py::resolve_caller_async`."""
    if not edition_features.enabled("cloud_nodes"):
        return False, None  # The community edition's node is the user's own machine: there is none to stop.
    from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

    instance_id = str(infra.get("instance_id") or "").strip()
    region = str(infra.get("region") or "").strip()
    if not instance_id or not region:
        return False, (
            "no instance id and region are recorded for this node, so there is no machine the control "
            "plane can prove it started — nothing was terminated (a self-hosted node has none by design)"
        )
    if str(infra.get("launched_by") or "") != "control-plane":
        return False, (
            f"instance {instance_id} is not recorded as control-plane launched, so terminating it could "
            f"destroy a machine something else manages — stop it by hand if it is yours"
        )
    credential_id = str(infra.get("credential_id") or "").strip()
    if not credential_id:
        return False, (
            f"instance {instance_id} carries no record of which AWS credential launched it, and an "
            f"instance id is account-scoped — asking the wrong account would answer 'not found' and be "
            f"mistaken for a machine already stopped, so nothing was terminated; stop {instance_id} in "
            f"{region} by hand"
        )
    try:
        aws = cloud_node_launch.credential_of_type(
            session, UUID(credential_id), cloud_node_launch.credential_type_for(clouds.provider_key_of(infra))
        )
    except (CloudNodeError, ValueError) as exc:
        return False, (
            f"the AWS credential recorded for instance {instance_id} is unusable ({exc}), so nothing was "
            f"terminated — stop {instance_id} in {region} by hand"
        )
    try:
        await run_in_threadpool(
            cloud_node_launch.terminate_cloud_node,
            credential=aws,
            region=region,
            instance_id=instance_id,
            node_name=node_id,
            # Where it runs (`clouds.instance_ref_of`); a row from before multi-cloud names none and is AWS.
            **{arg: infra[key] for arg, key in (("provider", clouds.INFRA_PROVIDER_KEY), ("zone", "zone"),
                                                ("scope", "scope")) if infra.get(key)},
        )
    except (CloudNodeError, CloudNodeUpstreamError) as exc:
        log.warning("delete node %s: row removed, instance %s not terminated: %s",
                    node_id, instance_id, exc)
        return False, f"AWS did not terminate {instance_id}: {exc} — stop it by hand in {region}"
    return True, None
