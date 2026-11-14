"""Node edits: `PUT /api/nodes/{node_id}` with its field guards, and lifting a transfer fence."""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import (
    credential_consumers,
    edition_features,
    hosts,
    ce_caps,
    jaah_discovery,
    node_allocation,
)
from app.actors import stamp_actor
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature, superadmin_only
from app.authz.object_access import can_access
from app.authz.principal import AsyncCaller, Principal, current_caller
from app.authz.visibility import ObjectAccess, sees_all_nodes
from app.db_async import on_session, on_thread
from app.entity_shape import StrictModel, commit_row, or_404, refused_as
from app.events import emit_console_event
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.node_identity import is_uuid_shaped
from app.tenancy.copy import administrator_changes
from app.tenancy.ids import is_operator_user, is_tenant_org
from app.routes.nodes import reads
from app.routes.nodes.node_wire import (
    NodeCounts,
    NodeDetailOut,
    _administers_own_hosts,
    _cloud_node_errors,
    _hosting_facts,
    _microvm_editable,
    _narrow_for_tenant,
    _node_out,
    _on_a_foreign_host,
    _session_kvm_editable,
)
from app.routes.nodes.reads import _counts_map, _node_row

if edition_features.enabled("cloud_nodes"):
    from app import cloud_boot_watch, cloud_lifecycle, cloud_node_launch
    from app.cloud_node_enrolled import ENROLLED_KEY
    from app.cloud_node_launch import SHARED_STORE_CREDENTIAL_KEY, CloudNodeError
    from app.clouds import INFRA_PROVIDER_KEY

router = APIRouter(tags=["nodes"])
log = logging.getLogger("cockpit-nodes")


# Refused on both writers of `nodes.name`: `NODE_ID` tries the uuid branch first, so a uuid-shaped name
# would resolve the agent plane onto whichever row owns that id (`node_identity.is_uuid_shaped`).
_UUID_SHAPED_NAME_REFUSAL = (
    "a node's name must not be uuid-shaped: `/etc/jaah/node.env` accepts either the name or the row's "
    "uuid, so a uuid-shaped name would resolve to whichever node owns that id instead of this one. Pick "
    "a hostname-shaped name."
)


class NodeTransferFenceOut(BaseModel):
    """`POST /api/nodes/{node_id}/lift-transfer-fence`: whether a fence stood and was lifted."""

    lifted: bool


class NodeVmSizeIn(StrictModel):
    """A Whole-node VM's size fields; one left out is unchanged, null resets it to the default."""

    vcpus: Optional[int] = None
    ram_mib: Optional[int] = None
    disk_gib: Optional[int] = None


class NodeUpdateIn(StrictModel):
    """PUT body — editable fields ONLY. `extra='forbid'` rejects any unknown or read-only field
    (status/last_seen/config_snapshot/counts/…) with a 422. No state-conditional restrictions."""

    # NOT NULL and UNIQUE, so an empty string is refused (the unreadable-label failure `EntityMixin`
    # prevents); a duplicate is refused by the DB. Renaming is allowed and unguarded by owner ruling; the
    # hazard (a renamed node keeps presenting its old name, stops resolving, and looks alive while landing
    # nothing) is recorded on `Node`.
    name: Optional[str] = None
    # NOT NULL DEFAULT '', so an emptied description is stored as `''`, never NULL.
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    enabled: Optional[bool] = None
    # The drain command, editable as `enabled` is (`Optional`: omitted means unchanged). `update_node`
    # applies two rules to the resulting values: `draining=t, enabled=f` is refused with a sentence, and
    # disabling a draining node clears the drain (`app/models/nodes.py::Node.draining`). Not on `NodeCreateIn`:
    # a node is never registered mid-wind-down.
    draining: Optional[bool] = None
    # No `nodelink_enabled`: the switch is retired (mounts follow a node's NodeLink attachments,
    # `routes/nodelinks.py`), and the node read no longer serves it. No `nodelink_root`: where the node mounts
    # its account-token share is the node's own report (`nodes.shared_accounts_root`). A body naming either is
    # a 422 (`StrictModel`), on the PUT and the create.
    role: Optional[str] = None
    jaah_home: Optional[str] = None
    # No `capacity`: its last key, the node's Max runners, was read by nothing and is retired. An older
    # client still sending it is not refused, and it is not kept (`_drop_the_retired_capacity`).
    infra: Optional[dict[str, Any]] = None
    # Editable because it is data: what a node should run is a row edit.
    config_desired: Optional[dict[str, Any]] = None
    # This node's auto-update override of Settings; an explicit null clears it (follow Settings). Stored but not
    # consulted on a node on a Multi-Node Host, which follows its host (`app/node_autoupdate.py`): refusing it
    # would tell a tenant its node is co-located.
    autoupdate_override: Optional[bool] = None
    # MicroVM mode and the node's VM size override (null clears one), gated by `_require_authority_for_microvm`;
    # a flip is refused with `microvm_mode.blocker`'s sentence. Not on `NodeCreateIn`: a node is marked later.
    microvm: Optional[bool] = None
    # The node's shape (`app/node_shape.py`): `microvm` above is its Legacy/MicroVM half, both through one writer.
    shape: Optional[Literal["legacy", "microvm", "node_vm"]] = None
    microvm_vcpus: Optional[int] = None
    microvm_ram_mib: Optional[int] = None
    microvm_disk_gib: Optional[int] = None
    # A Whole-node VM's own size (`app/node_vm_sizing.py`; each field null = back to the default), gated as the
    # MicroVM fields are and written through `node_vm_sizing.change`.
    node_vm_size: Optional[NodeVmSizeIn] = None
    # KVM for sessions on a co-located node, gated by `_check_session_kvm`: the host's owner only, and on only
    # where the host has `/dev/kvm`. Not on `NodeCreateIn`: a node is co-located by its host's door.
    session_kvm: Optional[bool] = None
    # The operator's allocation of this node to its tenant (`app/node_allocation.py`); a non-operator change is a 403.
    operator_allocated: Optional[bool] = None
    # How many tasks may run here at once (`nodes.max_running_tasks`, the `launch_gates.TASKS` gate); 0 (or null)
    # clears it, stored NULL. Editable as `description` is: the tenant's own throttle on its own node.
    max_running_tasks: Optional[int] = Field(default=None, ge=0, le=64)

    @model_validator(mode="before")
    @classmethod
    def _drop_the_retired_capacity(cls, data: Any) -> Any:
        """`capacity` (a node's Max runners) is retired; a body from a console built before it is dropped, not
        refused as extra."""
        if isinstance(data, dict) and "capacity" in data:
            data = {key: value for key, value in data.items() if key != "capacity"}
        return data


def _credential_in_reach(session: Session, credential_id: UUID, expected_type: str) -> Credential:
    """`cloud_node_launch.credential_of_type`, after a seam-scoped select of the id: the seam narrows that
    resolver's `session.get` except on an identity-map hit, which would let a tenant sign with another
    org's credential already loaded in this session. A credential outside the caller's org reads as
    missing, with the resolver's own sentence."""
    if session.scalar(select(Credential.id).where(Credential.id == credential_id)) is None:
        raise CloudNodeError(cloud_node_launch.NO_SUCH_CREDENTIAL)
    credential = cloud_node_launch.credential_of_type(session, credential_id, expected_type)
    # Recorded on `Node.infra`, which the control plane signs launches and provisions shares with (D1).
    credential_consumers.refuse_if_nodevault(credential, credential_consumers.CLOUD_NODE_LAUNCH)
    return credential


@router.post("/api/nodes/{node_id}/lift-transfer-fence", response_model=NodeTransferFenceOut)
@superadmin_only(Tenancy.OPERATOR)
@on_session
def lift_node_transfer_fence(
    session: Session,
    node_id: str,
    user: AdminUser = Depends(require_user),
) -> NodeTransferFenceOut:
    """Let a node an org transfer fenced take work again before it has confirmed the old org's cleanup (PRD R3.4):
    the operator's call for a node that will never answer (`durable/node_transfer.py` keeps asking meanwhile).
    Operator only — the operator moved it. Audited with one `node_transfer_fence_lifted` row in the node's org;
    a node not fenced is a no-op that writes nothing."""
    from app.user_activity import record_write

    row = or_404(_node_row(session, node_id), detail=f"unknown node '{node_id}'")
    lifted = session.execute(
        update(Node).where(Node.id == row.id, Node.org_transfer_cleanup_pending.is_(True))
        .values(org_transfer_cleanup_pending=False).execution_options(synchronize_session=False)
    ).rowcount > 0
    if lifted:
        record_write(session, user, NODE_TRANSFER_FENCE_LIFTED_KIND,
                     f"Lifted the org-transfer fence on node {row.name} before it confirmed the cleanup",
                     data={"node_id": str(row.id), "node": row.name}, in_org=row.org_id)
        emit_console_event(session, entity="node", id=row.id, action="upserted")
    session.commit()
    log.info("nodes: org-transfer fence on node=%s lifted=%s by %s", row.name, lifted, user.id)
    return NodeTransferFenceOut(lifted=lifted)


#: The activity kind an operator's fence lift writes (`activity_kinds.py`).
NODE_TRANSFER_FENCE_LIFTED_KIND = "node_transfer_fence_lifted"


# Editable `NodeUpdateIn` fields whose column is `nullable=False`, minus `name` (422s on empty) and
# `description` (coerces null to `''`). `role` and `jaah_home` are genuinely nullable. Read off `Node`'s
# columns so it cannot drift from the model.
_NOT_NULLABLE_EDITABLE_FIELDS = (
    "tags", "enabled", "draining",
    "infra", "config_desired", "microvm", "operator_allocated", "session_kvm",
)


def _reject_an_explicit_null_on_a_not_null_column(fields: dict[str, Any]) -> None:
    """An explicit `null` on one of `_NOT_NULLABLE_EDITABLE_FIELDS` is a 422 naming the field.

    `Optional` means an omitted field is unchanged, not that null is accepted: the columns are NOT NULL,
    and a null would reach the commit as an uncaught `IntegrityError` (`app/routes/accounts.py::update_account`
    answers the same with a sentence). Refused, not dropped: nothing clears a checkbox or JSON object to
    null."""
    nulled = [name for name in _NOT_NULLABLE_EDITABLE_FIELDS if name in fields and fields[name] is None]
    if not nulled:
        return
    raise HTTPException(
        status_code=422,
        detail=(
            f"{', '.join(nulled)} cannot be null — the column is NOT NULL, so absence is not a state "
            f"for it (docs/standards/coding-standards.md). Send a value, or omit the field to leave it "
            f"unchanged."
        ),
    )


# The four `infra` keys that authorize a termination (the launch record), matching
# `_terminate_node_instance`'s proofs; edit the two together. `region` is included:
# `ec2.terminate_instances` returns NotFound as success, so a wrong region falsely reports termination
# (`cloud_node_launch.terminate_cloud_node`). The node form never authors a region, and
# `_merge_a_non_superadmins_infra` carries a stored one through.
_LAUNCH_PROOF_KEYS = ("instance_id", "region", "launched_by", "credential_id")
# Where the instance runs beyond its id and region (`app/clouds/__init__.py::instance_ref_of`): the cloud, its zone
# and scope pick which instance a termination signs for, and `after_enrolled_at` records the first-connect hook
# (`app/cloud_node_enrolled.py`) — all written by the launch path only.
_INSTANCE_REF_KEYS: tuple[str, ...] = ("zone", "scope")
if edition_features.enabled("cloud_nodes"):
    _INSTANCE_REF_KEYS = (INFRA_PROVIDER_KEY, "zone", "scope", ENROLLED_KEY)

# What a launch was: the cloud connection, instance type, market and (a hosted host's) root volume the launch
# workflow reads back from the row (`app/durable/cloud_node.py::_intent`). A launch door writes them; a tenant
# changing one between registration and the launch, or after, would run a machine nobody chose (on a connection
# nobody granted) — so a non-operator change is refused outright and an omitting Save carries them through.
_LAUNCH_SPEC_KEYS = ("connection_id", "instance_type", "market", "root_volume_gb")

# The same protection, wider: `shared_storage_credential_id` is not a launch proof, so it is a second constant. It
# names an AWS credential on the row (`cloud_node_launch.SHARED_STORE_CREDENTIAL_KEY`), the same kind of claim as
# the superadmin-only `shared_accounts_enabled`; `nodes:edit` on one node must not re-point it. `account_id` is
# written by the launch and the node-ssm backfill only, and `image_id` (the image it booted) by the launch only: a
# tenant without host administration may not author them, and no tenant drops them on save. Both the authorship
# refusal and the carry-through cover these via this tuple. So does the boot-log sentence the sweep writes
# (`cloud_boot_watch.BOOT_REPORT_KEY`): the host page shows it as the system's own diagnosis; and the launch's
# break-glass warning (`cloud_node_launch.BREAK_GLASS_WARNING_KEY`), the node page's.
# The community edition launches no machine, so it has no share credential, machine state or boot log to guard.
_SUPERADMIN_ONLY_INFRA_KEYS: tuple[str, ...] = (
    _LAUNCH_PROOF_KEYS + ("account_id", "image_id") + _LAUNCH_SPEC_KEYS + _INSTANCE_REF_KEYS
)
if edition_features.enabled("cloud_nodes"):
    _SUPERADMIN_ONLY_INFRA_KEYS = (
        _LAUNCH_PROOF_KEYS + (SHARED_STORE_CREDENTIAL_KEY, "account_id", "image_id") + _LAUNCH_SPEC_KEYS
        + _INSTANCE_REF_KEYS + cloud_lifecycle.INFRA_KEYS
        + (cloud_boot_watch.BOOT_REPORT_KEY, cloud_node_launch.BREAK_GLASS_WARNING_KEY)
    )

# Host-level fields: what the machine is and how it is run, as distinct from identity (`name`/`description`/
# `tags`) and service switches (`enabled`/`draining`). `_narrow_for_tenant` blanks most of them for a
# non-superadmin tenant reader, who therefore may not write them either.
_HOST_FIELDS = ("jaah_home", "infra", "role", "config_desired", "autoupdate_override")


def _require_a_tenant_superadmin_for_host_fields(
    fields: dict[str, Any], actor: AdminUser, principal: Principal | None
) -> None:
    """403 when a tenant-org caller who is not its administrator names a `_HOST_FIELDS` key, whatever their
    `nodes:edit`.

    A tenant administrator (`_administers_own_hosts`) administers their own org's hosts; the operator's own staff keep `nodes:edit` over
    operator hosts. Naming one is enough, even unchanged (comparing would be an oracle on a value this
    reader is not served). Runs before the launch-proof guard, so the reader hears all of `infra` is not
    theirs."""
    if not is_tenant_org(actor.org_id) or _administers_own_hosts(actor, principal):
        return
    if any(key in fields for key in _HOST_FIELDS):
        raise HTTPException(status_code=403, detail=administrator_changes("A node's host settings"))


def _require_superadmin_to_author_a_launch_proof(
    session: Session,
    fields: dict[str, Any],
    stored: Optional[dict[str, Any]],
    actor: AdminUser,
    principal: Principal | None,
) -> None:
    """`infra`'s launch-proof and share keys (`_SUPERADMIN_ONLY_INFRA_KEYS`) are superadmin-only on this
    route on the operator plane; every other `infra` key stays `nodes:edit`'s. 403 when a caller failing
    `_administers_own_hosts` names one; a tenant administrator qualifies on their own org's node, which
    the route's seam alone reaches.

    `DELETE /api/nodes/{id}` terminates the EC2 instance these keys name, in the account and with the
    credential they name, and `ec2.terminate_instances` reads NotFound as success, so a forged proof is a
    silent termination of a machine the CP never started:
      · `instance_id` — names the victim.
      · `launched_by` — proof 2, "the CP started this"; stamping it makes terraform machines terminable.
      · `credential_id` — the account (proof 3); the CP would sign with a credential never granted.
      · `region` — a wrong region is a false success (see `_LAUNCH_PROOF_KEYS`).
    So for a tenant the credential behind any launch proof they change — the one they send, else the stored
    one `_merge_a_non_superadmins_infra` would carry through — and a changed share credential must resolve
    through their seam (422 otherwise). So must the STORED `credential_id` when any launch proof changes:
    else a tenant swaps in their own credential and orphans an operator-launched instance. A tenant thus
    only re-points proofs at machines in an account their own org holds, and a node the operator launched
    keeps its record, which `_terminate_node_instance` signs for unchanged.

    For a non-superadmin, naming one is enough, even unchanged (comparing would be an oracle on a value they
    are not served). Safe only because omitting a proof does not destroy it: `infra` is replaced wholesale,
    so `_merge_a_non_superadmins_infra` prevents loss by omission (a node without its launch record can
    never be wound down). A non-`dict` `infra` falls through to model validation."""
    infra = fields.get("infra")
    if not isinstance(infra, dict):
        return
    named = [key for key in _SUPERADMIN_ONLY_INFRA_KEYS if key in infra]
    if not named:
        return
    if not _administers_own_hosts(actor, principal):
        # A tenant seat reads who can act, never the tier's name (`app/tenancy/copy.py`).
        if is_tenant_org(actor.org_id):
            raise HTTPException(
                status_code=403, detail=administrator_changes("A node's launch record and shared store")
            )
        raise HTTPException(
            status_code=403,
            detail=(
                f"superadmin required to write infra.{', infra.'.join(named)} — these are the facts "
                f"DELETE /api/nodes/{{id}} terminates a cloud instance on, and the share a node mounts its "
                f"credential store from. Omit them (whatever is stored is kept), or ask a superadmin."
            ),
        )
    if is_operator_user(actor):
        # The operator's own verdict is unchanged: its seam reaches every org's credential anyway.
        return
    if not edition_features.enabled("cloud_nodes"):
        return  # The community edition has no clouds (no launch record to guard), and its one org holds every credential.
    # By change, not presence: a tenant administrator is served the non-credential proofs, so their Save
    # round-trips them unchanged, and a stored credential they cannot reach must not refuse that Save.
    stored = stored or {}
    changed = [key for key in _SUPERADMIN_ONLY_INFRA_KEYS if key in infra and infra[key] != stored.get(key)]
    if any(key in changed for key in _LAUNCH_SPEC_KEYS):
        raise HTTPException(
            status_code=403,
            detail="A node's cloud connection, instance type and billing are set when it launches and cannot be changed",
        )
    # Written by the launch only (`_INSTANCE_REF_KEYS`): a tenant re-pointing zone or scope would aim the boot watch's
    # and a termination's signed calls at another instance under the same credential.
    if any(key in changed for key in _INSTANCE_REF_KEYS):
        raise HTTPException(
            status_code=403,
            detail="A node's cloud, zone and project are set when it launches and cannot be changed",
        )
    if any(key in changed for key in cloud_lifecycle.INFRA_KEYS):
        raise HTTPException(
            status_code=403,
            detail="A node's machine state is set by its Stop, Start, Change type and Grow disk actions",
        )
    to_check: list[tuple[str, Any]] = []
    if any(key in changed for key in _LAUNCH_PROOF_KEYS):
        to_check.append(("credential_id", infra.get("credential_id", stored.get("credential_id"))))
        if stored.get("credential_id") is not None:
            to_check.append(("credential_id", stored["credential_id"]))
    if SHARED_STORE_CREDENTIAL_KEY in changed:
        to_check.append((SHARED_STORE_CREDENTIAL_KEY, infra[SHARED_STORE_CREDENTIAL_KEY]))
    for key, value in to_check:
        try:
            with _cloud_node_errors():
                _credential_in_reach(
                    session, UUID(str(value)), cloud_node_launch.AWS_CREDENTIAL_TYPE
                )
        except credential_consumers.NodeVaultUnavailable:
            raise  # a `ValueError` too, but its own 409 names the consumer (`main.py`)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"infra.{key} must name an AWS credential of your organization",
            ) from None


def _merge_a_non_superadmins_infra(
    fields: dict[str, Any], stored: Optional[dict[str, Any]], actor: AdminUser
) -> None:
    """Carry the stored launch proofs through a non-operator's `infra` payload, in place.

    `infra` is written wholesale, so an omitted key is deleted: an API caller sending a partial object would erase a
    cloud node's launch record (wedging `node_group_reconcile` and making `delete_node` skip a billing machine). The
    console's node page sends no `infra` at all; this is the server-side floor for every other caller.

    Non-operator only: an operator's dict stays authoritative (launch paths re-record proofs; an operator may
    clear a stale one). Below that tier the stored value is the only correct one — a tenant superadmin is
    not served the credential references (`_TENANT_WITHHELD_INFRA_KEYS`), so their Save omits them. Omission
    only: a named key is the guard's. A `None`/non-dict `infra` is left for `NodeUpdateIn`."""
    if is_operator_user(actor) or "infra" not in fields:
        return
    sent = fields.get("infra")
    if not isinstance(sent, dict) or not isinstance(stored, dict):
        return
    for key in _SUPERADMIN_ONLY_INFRA_KEYS:
        if key not in sent and key in stored:
            sent[key] = stored[key]


#: The PUT fields `_require_authority_for_microvm` gates: the mark and the node's VM size override
#: (`NodeUpdateIn`'s, the `microvm_sizing.column`s).
_MICROVM_FIELDS = ("microvm", "microvm_vcpus", "microvm_ram_mib", "microvm_disk_gib")


def _require_authority_for_microvm(
    session: Session, fields: dict[str, Any], row: Node, actor: AdminUser, principal: Principal | None
) -> None:
    """403 on a CHANGE to the MicroVM mark or the node's VM size by a caller `_microvm_editable` refuses: a
    tenant admin on a shared node (A6), or anyone who does not administer the node's org. Change, not presence:
    the edit form resends every field, and the reader can already read these values."""
    changed = [name for name in _MICROVM_FIELDS if name in fields and fields[name] != getattr(row, name)]
    if fields.get("shape") is not None and fields["shape"] != _shape_now(session, row):
        changed.append("shape")
    if fields.get("node_vm_size"):
        changed.append("node_vm_size")
    if not changed:
        return
    if not edition_features.enabled("microvm"):
        raise ce_caps.not_in_edition()  # The community edition has no MicroVMs.
    from app import microvm_mode

    if _microvm_editable(session, row, actor, principal):
        return
    if microvm_mode.is_shared_node(session, row):
        raise HTTPException(status_code=403, detail=microvm_mode.SHARED_NODE)
    raise HTTPException(status_code=403, detail=administrator_changes("A node's MicroVM settings"))


def _shape_now(session: Session, row: Node) -> str:
    """The node's shape (`node_shape.shape_of`); `legacy` where MicroVM is not shipped."""
    if not edition_features.enabled("microvm"):
        return "legacy"
    from app import node_shape

    return node_shape.shape_of(session, row)


def _apply_shape_change(session: Session, updates: dict[str, Any], row: Node) -> Optional[UUID]:
    """Take `shape` and `microvm` out of `updates` (the setattr loop must not write them), size-check the VM
    override, and move the node to the shape asked through its one writer (`node_shape.change`): `microvm: true`
    asks for MicroVM, `false` for Legacy from MicroVM. A refusal is a 409 with that writer's one sentence —
    `microvm_mode.turn_on`'s re-check under lock (a worktree leased since) included. Returns the host to nudge
    after the commit when the change rebuilds the node there (to or from Whole-node VM), else None."""
    shape = updates.pop("shape", None)
    microvm = updates.pop("microvm", None)
    if not edition_features.enabled("microvm"):
        return None  # The community edition has no MicroVMs: `_require_authority_for_microvm` refused any change.
    from app import microvm_sizing, node_shape

    with refused_as(422, ValueError):
        updates.update(microvm_sizing.validated(updates))
    if shape is None and microvm is not None:
        current = node_shape.shape_of(session, row)
        shape = node_shape.MICROVM if microvm else (node_shape.LEGACY if current == node_shape.MICROVM else current)
    if shape is None:
        return None
    with refused_as(409, ValueError, session=session):
        return node_shape.change(session, row, shape)


NOT_A_NODE_VM = "This node isn't a Whole-node VM."


def _apply_node_vm_size(session: Session, size: Optional[dict[str, Any]], row: Node) -> Optional[UUID]:
    """Write a Whole-node VM's size (`node_vm_sizing.change`): 409 with its one sentence (a held task, a disk
    shrink, the disk before its host reported it), 422 for a value out of bounds. Returns the host to nudge, which
    restarts the VM at the new size."""
    if not size:
        return None
    if not edition_features.enabled("microvm"):
        return None  # Unreached: `_require_authority_for_microvm` refused the change.
    from app import node_shape, node_vm_sizing
    from app.models.worktrees import VirtualNode

    if node_shape.shape_of(session, row) != node_shape.NODE_VM:
        raise HTTPException(status_code=409, detail=NOT_A_NODE_VM)
    # Locked: two saves at once must not both pass the shrink and task checks.
    vnode = session.scalar(select(VirtualNode).where(VirtualNode.node_id == row.id).with_for_update())
    if vnode is None:  # pragma: no cover - `shape_of` needs one
        raise HTTPException(status_code=409, detail=NOT_A_NODE_VM)
    with refused_as(422, ValueError):
        own = node_vm_sizing.validated(size)
    with refused_as(409, ValueError, session=session):
        node_vm_sizing.change(session, row, vnode, own)
    return vnode.host_node_id


SESSION_KVM_NOT_CO_LOCATED = "KVM for sessions applies to a node on a Multi-Node Host only."
SESSION_KVM_HOST_OWNER = "Only the host's owner can change KVM for sessions."
SESSION_KVM_NO_HOST_KVM = "This node's host has no KVM."


def _check_session_kvm(
    session: Session, fields: dict[str, Any], row: Node, actor: AdminUser, principal: Principal | None
) -> Optional[UUID]:
    """The host to nudge when `fields` changes `session_kvm`, else None. 422 off a co-located node, 403 for a
    caller `_session_kvm_editable` refuses, 409 turning it on where the host has no `/dev/kvm` (`nodes.kvm`).
    Change, not presence, as `_require_authority_for_microvm`; turning it off needs only the authority. A tenant
    on another org's host is served False (`_narrow_for_tenant`), so its change is against that and refused as an
    ordinary node's is: no oracle on the value or on the co-location."""
    if "session_kvm" not in fields:
        return None
    fact = _hosting_facts(session, [row.id]).get(row.id)
    co_located = fact is not None and not fact.is_host
    editable = co_located and _session_kvm_editable(session, row, actor, principal)
    narrowed = co_located and _on_a_foreign_host(actor, "co_located", fact.host_org_id if fact else None)
    if fields["session_kvm"] == (False if narrowed else row.session_kvm):
        fields.pop("session_kvm")
        return None
    if not co_located or narrowed:
        # A narrowed caller hears what an ordinary node's does: a 403 would tell it the node is co-located.
        raise HTTPException(status_code=422, detail=SESSION_KVM_NOT_CO_LOCATED)
    if not editable:
        raise HTTPException(status_code=403, detail=SESSION_KVM_HOST_OWNER)
    if not edition_features.enabled("multi_node_hosts"):
        return None  # Unreached: the community edition has no Multi-Node Hosts, so no node is co-located.
    from app import vnodes

    host_node_id = vnodes.host_of(row.id)
    if fields["session_kvm"]:
        # On the operator plane: an operator host's row is invisible under a tenant's binding.
        with vnodes.fleet_session() as fleet:
            if not fleet.scalar(select(Node.kvm).where(Node.id == host_node_id)):
                raise HTTPException(status_code=409, detail=SESSION_KVM_NO_HOST_KVM)
    return host_node_id


@router.put("/api/nodes/{node_id}", response_model=NodeDetailOut)
@feature("nodes", "edit", tenancy=Tenancy.CALLER_PLANE)
@on_thread
def update_node(
    session: Session,
    node_id: str,
    body: NodeUpdateIn,
    caller: AsyncCaller = Depends(current_caller),
) -> NodeDetailOut:
    """Edit a node's record. `nodes:edit` is unchanged in meaning; a node grant is not an edit right.

    Adds D13 only: a node this caller cannot see is not editable by URL either. The probe is the read clause
    (`NODE_SPEC` has no separate `write_clause`); grant without key is refused, key without grant gets the
    read's 404."""
    # In a thread: `vnodes.host_of`/`fleet_session` and `node_allocation.jaah_cloud` read the operator plane on
    # sync sessions of their own.
    presence = jaah_discovery.presence_map(session)
    row = _node_row(session, node_id)
    if row is not None and not can_access(
        session, caller.principal, "node", row.id, mode=ObjectAccess.WRITE
    ):
        # The file's own message: the kernel's generic `"not found"` would restore the existence oracle.
        raise HTTPException(status_code=404, detail=f"unknown node '{node_id}'")
    if row is None:
        # Edit only, no create-if-missing: registration is explicit and presence is not membership.
        # The presence hint is superadmin-only, as in `get_node`; a scoped caller gets the bare unknown answer.
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown node '{node_id}'"
                + (" — it is connected but not registered; POST /api/nodes to register it"
                   if node_id in presence and sees_all_nodes(caller.principal) else "")
            ),
        )

    updates = body.model_dump(exclude_unset=True)
    # An allocated node's locked half first: a tenant changing it reads the allocation, not a tier refusal.
    node_allocation.refuse_locked_fields(updates, row, caller.actor)
    # Authority before shape, both on the pre-validation dict (as `app/routes/projects.py::update_project`):
    # a caller naming a field it may not author gets the 403, learning nothing about the value.
    _require_authority_for_microvm(session, updates, row, caller.actor, caller.principal)
    kvm_host = _check_session_kvm(session, updates, row, caller.actor, caller.principal)
    _require_a_tenant_superadmin_for_host_fields(updates, caller.actor, caller.principal)
    _require_superadmin_to_author_a_launch_proof(session, updates, row.infra, caller.actor, caller.principal)
    # After the refusal: this only fills in keys the payload omitted (see its docstring).
    _merge_a_non_superadmins_infra(updates, row.infra, caller.actor)
    _reject_an_explicit_null_on_a_not_null_column(updates)
    # `name` must stay a readable label (never an id's shape); `description` defaults to `''`.
    if "name" in updates:
        renamed = (updates["name"] or "").strip()
        if not renamed:
            raise HTTPException(status_code=422, detail="name must not be empty")
        if is_uuid_shaped(renamed):
            raise HTTPException(status_code=422, detail=_UUID_SHAPED_NAME_REFUSAL)
        if renamed != row.name:
            # A named host's machine handle moves only with its display name (`hosts.rename_host`), and no
            # node may take the `-host` shape a host's handle needs.
            if hosts.display_name_of(session, row.id) is not None:
                raise HTTPException(status_code=422, detail=hosts.RENAME_FROM_HOST)
            if hosts.is_host_handle(renamed):
                raise HTTPException(status_code=422, detail=hosts.HOST_SUFFIX_REFUSAL)
        updates["name"] = renamed
    if "description" in updates and updates["description"] is None:
        updates["description"] = ""
    if "max_running_tasks" in updates and (updates["max_running_tasks"] is None or updates["max_running_tasks"] == 0):
        updates["max_running_tasks"] = None  # 0 is "no limit", which the column holds as NULL.
    # The drain/enabled pair is settled against the resulting row, not just the body: a PUT names only what
    # it changes, and the illegal pair can arrive through either field.
    # The clear runs before the refusal: reversed, `{"enabled": false}` on a draining node would be refused.
    if updates.get("enabled") is False and "draining" not in updates:
        # Disabling clears a drain: Disable is the drain's own end state (`app/lease_drain.py::_disable_drained_nodes`
        # flips the same pair). Only when the caller sent no `draining`, so an explicit contradiction still 422s.
        updates["draining"] = False
    next_enabled = updates.get("enabled", row.enabled)
    next_draining = updates.get("draining", row.draining)
    if next_draining and not next_enabled:
        # Refused here although `ck_nodes_draining_implies_enabled` would too: the constraint surfaces as an
        # uncaught `IntegrityError`. The SQL half stays for writers that bypass this route.
        raise HTTPException(
            status_code=422,
            detail=(
                "a node cannot be draining and disabled at once — a drain is the wind-down of a node "
                "that is still IN SERVICE, and a disabled node has already finished winding down. "
                "Drain a node that is enabled, or Disable it outright."
            ),
        )
    rebuild_host = _apply_shape_change(session, updates, row)
    vm_size = updates.pop("node_vm_size", None)
    for field_name, value in updates.items():
        setattr(row, field_name, value)
    # After the loop: a disk grow writes `infra`, which a resent `infra` would otherwise overwrite.
    resize_host = _apply_node_vm_size(session, vm_size, row)
    stamp_actor(row, caller.actor)

    emit_console_event(session, entity="node", id=row.id, action="upserted")
    commit_row(session, row)
    for host in (kvm_host, rebuild_host, resize_host):
        if host is None:
            continue
        if edition_features.enabled("multi_node_hosts"):  # neither is returned without them.
            from app import vnodes

            vnodes.nudge_host(host)

    counts = _counts_map(session, [row.id]).get(
        row.id, NodeCounts(worktrees=0, runners=0, accounts=0)
    )
    out = _node_out(
        session,
        str(row.id),
        row,
        presence,
        counts,
        detail=True,
        published=reads._published_release(session, [row]),
    )
    if isinstance(out, NodeDetailOut):
        out.microvm_editable = _microvm_editable(session, row, caller.actor, caller.principal)
        if not out.microvm_editable:
            out.shape_options = [out.shape]  # nothing this reader may change it to
        out.session_kvm_editable = _session_kvm_editable(session, row, caller.actor, caller.principal)
    return _narrow_for_tenant(out, caller.actor, caller.principal)
