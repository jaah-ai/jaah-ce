"""Settings → NodeVault on the control plane: the status read, the seal key, and the per-node controls.

  GET  /api/nodevault                                    status: plan flags, keys, nodes and their grants, the
                                                         running operation (org Owner/Admin)
  GET  /api/nodevault/seal-key                           the org's current NodeVault PUBLIC key, to seal a new
                                                         NodeVault credential's secrets (any credentials:edit member)
  POST /api/nodevault/grants/{node_id}/{key_hash}/retry  a failed delivery/retirement goes again (org Owner/Admin)
  POST /api/nodevault/nodes/{node_id}/reset-identity     forget a node's identity key so it can re-enrol (audited)
  POST /api/nodevault/setup                              start Setup: the new key, its backup confirmed, one blob per
                                                         capable node (org Owner/Admin)
  GET  /api/nodevault/credentials                        which credentials can move to NodeVault, and why not
  POST /api/nodevault/migrations                         the setup's migrate choice, or a standalone migration
  GET  /api/nodevault/operations/{id}                    one operation with its per-credential results
  POST /api/nodevault/operations/{id}/items/{cred}/retry a failed credential goes again
  POST /api/nodevault/operations/{id}/abandon            cancel a setup or rotation before promotion, a migration,
                                                         or a retire (§3.5)
  POST /api/nodevault/rotation                           start a rotation: the new key, its backup confirmed, and the
                                                         new private key chain-wrapped under the current key (E2)
  POST /api/nodevault/keys/{hash}/retire                 take an old key no credential uses off every node
  POST /api/nodevault/start-over                         retire a current key no node holds, so Setup can run again
                                                         (org Owner only; audited)

The plan gate is `nodevault_policy.status` and follows the §3.5 freeze table: with the plan off after setup
(`frozen`) the status read, grant retry and reset-identity stay allowed — so existing NodeVault credentials
keep working on every node (AC-1.3) — while the seal key, setup, migration (except `skip`) and item retry are
refused, so no new NodeVault credential is made — and so are rotation and retire; abandon and `skip` stay allowed so a frozen org can always
clear or finish its one running operation.
An org that never set NodeVault up and whose plan does not include it is refused everywhere with the one
sentence naming the plan (AC-1.2).

Nothing here reads, returns or logs a secret, a private key or a blob: grants are reported by state and
closed-vocabulary error word, keys by their public hash.
"""
from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Row, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import after_commit, credential_consumers, email_unsubscribe, node_presence, nodevault_grants, nodevault_policy, pool_state
from app.access import ORG_OWNER
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature, org_admin
from app.authz.principal import Principal, current_principal
from app.credential_crypto import nodevault_key_hash as credential_key_hash
from app.credential_keys import WRAP_ALGORITHM, nodevault_key_hash, nodevault_key_id, parse_nodevault_public_key
from app.db import get_session
from app.durable import nodevault as durable_nodevault
from app.models.credentials import (
    NV_GRANT_DELIVERED,
    NV_GRANT_FAILED,
    NV_GRANT_RETIRE_PENDING,
    NV_GRANT_RETIRED,
    NV_GRANT_WAITING,
    NV_KEY_ABANDONED,
    NV_KEY_CURRENT,
    NV_KEY_PENDING,
    NV_KEY_PREVIOUS,
    NV_KEY_RETIRED,
    NV_KEY_RETIRING,
    NV_OP_RUNNING,
    Credential,
    NodeVaultGrant,
    NodeVaultKey,
    NodeVaultOperation,
    NodeVaultOperationItem,
)
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.entity_shape import get_or_404
from app.models.orgs import Org
from app.routes.credentials.credential_wire import SealKeyOut
from app.user_activity import record_write

log = logging.getLogger("app.routes.nodevault")

router = APIRouter(tags=["nodevault"])

NOT_IN_PLAN = "NodeVault isn't included in your plan"
NOT_SET_UP = "NodeVault isn't set up"
NODE_NOT_FOUND = "Node not found"
GRANT_NOT_FOUND = "This node has no record of that key"
GRANT_NOT_FAILED = "Only a failed delivery can be retried"
ALREADY_RUNNING = "A NodeVault rotation is already running"
ALREADY_SET_UP = "NodeVault is already set up"
BACKUP_REQUIRED = "Save and confirm the key backup before you continue"
KEY_IN_USE = "This key is already used by your organization"
NODES_CHANGED = "The list of nodes changed. Refresh and try again."
NO_ONLINE_NODE = "No node that can hold the key is online. Start a node, then try again."
BAD_BLOB = "The key prepared for node {name} is malformed. Refresh and try again."
CHOOSE_ONE = "Choose credentials, all, or skip"
SKIP_ONLY_IN_SETUP = "Skip applies only while setup is waiting for your choice"
NOTHING_TO_MIGRATE = "None of the chosen credentials can move to NodeVault"
UNKNOWN_CREDENTIALS = "Some of the chosen credentials don't exist"
OPERATION_NOT_FOUND = "Operation not found"
ITEM_NOT_FOUND = "This operation has no result for that credential"
ITEM_NOT_FAILED = "Only a failed credential can be retried"
OPERATION_FINISHED = "This operation has already finished"
CANT_ABANDON_PROMOTED = "Setup can't be abandoned once the key is in use. Choose Skip instead."
OPERATION_ABANDONED = "This operation was abandoned. Start a new migration instead."
KEY_NOT_CURRENT = "This operation's key is no longer the current one. Start a new migration instead."
CANT_ABANDON_KIND = "Only a setup or a migration can be abandoned here"
CANT_ABANDON_ROTATED = "The new key is already in use. Choose Skip to go on to the rekey."
KEY_GONE_EVERYWHERE = "The key is already gone from every node, so its retirement can't be undone."
NOT_ROTATED_BLOB = "The new key must be wrapped under your current NodeVault key. Refresh and try again."
KEY_NOT_FOUND = "This organization has no NodeVault key with that hash"
ONLY_OLD_KEYS = "Only an old key can be retired"
NO_ONLINE_HOLDER = "No node that holds the current key is online. Start a node, then try again."
NO_SECRET = "Has no secret to protect"
OWNER_ONLY = "Only an org Owner can start NodeVault over"
KEY_HELD = "A node holds the current key. Rotate the key instead."

#: What each item reason (`durable_nodevault.SKIP_REASONS` / `FAIL_REASONS`) says to the admin.
ITEM_MESSAGES = {
    "already_nodevault": "Already in NodeVault",
    "oauth2": credential_consumers.OAUTH2_REASON,
    "signing_key": credential_consumers.type_ineligible_reason(email_unsubscribe.CREDENTIAL_TYPE),
    "in_use": "Used by Jaah's servers, so it can't be NodeVault",
    "no_secret": NO_SECRET,
    "corrupt": "Its stored secret couldn't be opened. Re-enter the secret, then retry.",
    "verify_failed": "A node couldn't open the re-sealed secret, so it was left as it was. Retry.",
    "changed": "It was edited while it was being moved. Retry.",
    "unavailable": "The move failed for a temporary reason. Retry.",
    "unopenable": "A node couldn't open its secret under the old key. Re-enter the secret to move it to the current key.",
    "not_rekeyed": "It couldn't be moved to the new key. Re-enter the secret to move it to the current key.",
}


def keys_in_use(count: int) -> str:
    """AC-6.7's refusal: how many credentials still name the key."""
    return f"{count} credential still uses this key" if count == 1 else f"{count} credentials still use this key"
#: A key blob's fields and their bound (arch §2: `{v:1,k,alg,ek,n,ct}`; `ct` is a PKCS#8 RSA-4096 key).
_BLOB_KEYS = frozenset({"v", "k", "alg", "ek", "n", "ct"})
_BLOB_FIELD_MAX_CHARS = 16_384

#: The grant states the status read reports — a retired grant is history, not a placement.
_REPORTED_GRANT_STATES = (NV_GRANT_WAITING, NV_GRANT_DELIVERED, NV_GRANT_FAILED, NV_GRANT_RETIRE_PENDING)
#: The count keys an operation always answers, whatever its workflow has written so far.
_COUNT_KEYS = ("migrated", "skipped", "failed", "rekeyed", "total", "nodes_pending")


# ── Shapes (§4.1) ────────────────────────────────────────────────────────────────────────────────────


class KeyOut(BaseModel):
    hash: str
    created_at: datetime


class CurrentKeyOut(KeyOut):
    credential_count: int


class PreviousKeyOut(KeyOut):
    state: str   # previous | retiring
    credential_count: int


class GrantOut(BaseModel):
    key_hash: str
    state: str
    last_error: Optional[str]
    # A waiting delivery no online node can make yet (AC-3.4): "Waiting for a node that holds the key".
    waiting_for_holder: bool


class NodeOut(BaseModel):
    id: UUID
    name: str
    online: bool
    identity_key: Optional[str]
    identity_fp: Optional[str]
    capable: bool
    grants: list[GrantOut]


class OperationOut(BaseModel):
    id: UUID
    kind: str
    state: str
    step: str
    key_hash: Optional[str]
    prev_key_hash: Optional[str]
    waiting_for_holder: bool
    counts: dict[str, int]


class NodeVaultStatusOut(BaseModel):
    allowed: bool
    frozen: bool
    setup: bool
    current_key: Optional[CurrentKeyOut]
    previous_keys: list[PreviousKeyOut]
    nodes: list[NodeOut]
    operation: Optional[OperationOut]
    # Start over is offered: the caller is the org Owner, no operation runs and no node holds the current key.
    can_start_over: bool


class GrantRetryOut(BaseModel):
    state: str


class ResetIdentityOut(BaseModel):
    node_id: UUID
    grants_removed: int


# ── The gate ─────────────────────────────────────────────────────────────────────────────────────────


def _org(session: Session, user: AdminUser) -> Org:
    org = session.get(Org, user.org_id)
    if org is None:   # a session always names its org; a vanished one is not this route's problem to explain
        raise HTTPException(status_code=403, detail=NOT_IN_PLAN)
    return org


def _require_plan_or_setup(session: Session, org: Org) -> nodevault_policy.Status:
    """The §3.5 rows that stay allowed while `frozen`: refused unless `nodevault_policy.readable`."""
    status = nodevault_policy.status(session, org)
    if not nodevault_policy.readable(session, org, status):
        raise HTTPException(status_code=403, detail=NOT_IN_PLAN)
    return status


def _running_operation(session: Session, org_id: UUID) -> Optional[NodeVaultOperation]:
    return session.scalar(
        select(NodeVaultOperation).where(
            NodeVaultOperation.org_id == org_id, NodeVaultOperation.state == NV_OP_RUNNING
        )
    )


# ── GET /api/nodevault ───────────────────────────────────────────────────────────────────────────────


def _credential_counts(session: Session, org_id: UUID) -> dict[str, int]:
    """How many of the org's credentials are sealed under each NodeVault key (their envelopes' `k`, A9).
    Opens tier 1 only — the tier-2 envelopes stay sealed and only their key ids are read."""
    counts: dict[str, int] = {}
    for credential in session.scalars(
        select(Credential).where(Credential.org_id == org_id, Credential.nodevault.is_(True))
    ):
        key_hash = credential_key_hash(credential.config or {})
        if key_hash is not None:
            counts[key_hash] = counts.get(key_hash, 0) + 1
    return counts


def _current_key_held(session: Session, org_id: UUID, key_hash: str) -> bool:
    """Start over's precondition, its one home: some node — online or not — has the key delivered. A grant
    still waiting, failed or retiring holds nothing a node could open with."""
    return bool(session.scalar(select(func.count()).where(
        NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == key_hash, NodeVaultGrant.state == NV_GRANT_DELIVERED,
    )))


def _is_owner(principal: Optional[Principal]) -> bool:
    """The org Owner (an operator acting as the org is one, `authz/principal.py`)."""
    return principal is not None and (principal.is_operator or principal.org_role == ORG_OWNER)


def _waiting_for_holder(
    grant: Row, keys: dict[str, NodeVaultKey], delivered: dict[str, set[UUID]], online: set[str],
) -> bool:
    """Derived, never stored, so it cannot go stale: a waiting grant with no blob whose key no OTHER online
    node holds and that cannot be chained from a predecessor this node holds."""
    if grant.state != NV_GRANT_WAITING or not grant.no_blob:
        return False
    key = keys.get(grant.key_hash)
    predecessor = durable_nodevault.chain_predecessor(key.chain_blob if key is not None else None)
    if predecessor is not None and grant.node_id in delivered.get(predecessor, set()):
        return False
    holders = delivered.get(grant.key_hash, set()) - {grant.node_id}
    return not any(str(holder) in online for holder in holders)


@router.get("/api/nodevault", response_model=NodeVaultStatusOut)
@org_admin(Tenancy.CALLER_PLANE)
def get_nodevault(
    user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
    principal: Optional[Principal] = Depends(current_principal),
) -> NodeVaultStatusOut:
    """The whole Settings → NodeVault page in one read (§4.1)."""
    org = _org(session, user)
    status = _require_plan_or_setup(session, org)
    keys = {
        key.key_hash: key
        for key in session.scalars(select(NodeVaultKey).where(NodeVaultKey.org_id == org.id))
    }
    # Columns, not rows: the status read never loads a blob, only whether there is one.
    grants = session.execute(
        select(
            NodeVaultGrant.node_id, NodeVaultGrant.key_hash, NodeVaultGrant.state, NodeVaultGrant.last_error,
            NodeVaultGrant.blob.is_(None).label("no_blob"),
        ).where(
            NodeVaultGrant.org_id == org.id, NodeVaultGrant.state.in_(_REPORTED_GRANT_STATES)
        ).order_by(NodeVaultGrant.created_at)
    ).all()
    delivered: dict[str, set[UUID]] = {}
    for grant in grants:
        if grant.state == NV_GRANT_DELIVERED:
            delivered.setdefault(grant.key_hash, set()).add(grant.node_id)
    online = set(node_presence.connected_node_ids(session))
    by_node: dict[UUID, list[GrantOut]] = {}
    waiting_keys: set[str] = set()
    for grant in grants:
        waiting = _waiting_for_holder(grant, keys, delivered, online)
        if waiting:
            waiting_keys.add(grant.key_hash)
        by_node.setdefault(grant.node_id, []).append(GrantOut(
            key_hash=grant.key_hash, state=grant.state, last_error=grant.last_error, waiting_for_holder=waiting,
        ))

    current = next((key for key in keys.values() if key.state == NV_KEY_CURRENT), None)
    counts = _credential_counts(session, org.id)
    previous = sorted(
        (key for key in keys.values() if key.state in (NV_KEY_PREVIOUS, NV_KEY_RETIRING)),
        key=lambda key: key.created_at, reverse=True,
    )
    # A Multi-Node Host runs no work and never holds the key, so it is not listed (`app/vnodes.py`).
    nodes = session.scalars(select(Node).where(Node.org_id == org.id, pool_state.not_a_vnode_host()).order_by(Node.name))
    operation = _running_operation(session, org.id)
    if (operation is not None and operation.kind == durable_nodevault.ROTATION
            and operation.step == durable_nodevault.STEP_REKEY):
        # The rekey needs ONE online node holding both keys; with none, the page says it waits for one.
        both = delivered.get(operation.key_hash, set()) & delivered.get(operation.prev_key_hash or "", set())
        if not any(str(node_id) in online for node_id in both):
            waiting_keys.add(operation.key_hash)
    return NodeVaultStatusOut(
        allowed=status.allowed,
        frozen=status.frozen,
        setup=status.setup,
        current_key=CurrentKeyOut(hash=current.key_hash, created_at=current.created_at,
                                  credential_count=counts.get(current.key_hash, 0)) if current else None,
        previous_keys=[
            PreviousKeyOut(hash=key.key_hash, created_at=key.created_at, state=key.state,
                           credential_count=counts.get(key.key_hash, 0))
            for key in previous
        ],
        nodes=[
            NodeOut(
                id=node.id, name=node.name, online=str(node.id) in online,
                identity_key=node.nodevault_identity_key, identity_fp=node.nodevault_identity_fp,
                capable=bool(node.nodevault_capable), grants=by_node.get(node.id, []),
            )
            for node in nodes
        ],
        operation=_operation_out(session, operation, grants, waiting_keys) if operation is not None else None,
        can_start_over=(current is not None and operation is None and _is_owner(principal)
                        and not _current_key_held(session, org.id, current.key_hash)),
    )


def _operation_out(
    session: Session, operation: NodeVaultOperation, grants: list[Row], waiting_keys: set[str],
) -> OperationOut:
    # The workflow may die between the operation's commit and its start; the page's poll restarts it.
    if operation.state == NV_OP_RUNNING:
        durable_nodevault.ensure_operation_started(operation.kind, operation.id)
    counts = {name: 0 for name in _COUNT_KEYS}
    counts.update({k: int(v) for k, v in (operation.counts or {}).items() if k in counts and isinstance(v, int)})
    # Read off the item rows, so the counts match the DB while the workflow is still writing them (AC-4.8).
    counts.update(durable_nodevault.item_counts(session, operation.id))
    if operation.key_hash is not None:
        settled = NV_GRANT_RETIRED if operation.kind == "retire" else NV_GRANT_DELIVERED
        counts["nodes_pending"] = sum(
            1 for grant in grants if grant.key_hash == operation.key_hash and grant.state != settled
        )
    return OperationOut(
        id=operation.id, kind=operation.kind, state=operation.state, step=operation.step,
        key_hash=operation.key_hash, prev_key_hash=operation.prev_key_hash,
        waiting_for_holder=operation.key_hash in waiting_keys, counts=counts,
    )


# ── GET /api/nodevault/seal-key ──────────────────────────────────────────────────────────────────────


@router.get("/api/nodevault/seal-key", response_model=SealKeyOut)
@feature("credentials", "edit", own_credential=True)
def get_nodevault_seal_key(
    user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
) -> SealKeyOut:
    """The org's CURRENT NodeVault public key, for `credentialSeal.sealSecret` to seal a NodeVault
    credential's secrets in the browser. Public by construction. Refused while frozen: no new NodeVault
    credential is made once the plan no longer includes it (§3.5)."""
    org = _org(session, user)
    status = nodevault_policy.status(session, org)
    if not status.allowed:
        raise HTTPException(status_code=403, detail=NOT_IN_PLAN)
    current = session.scalar(
        select(NodeVaultKey).where(NodeVaultKey.org_id == org.id, NodeVaultKey.state == NV_KEY_CURRENT)
    )
    if current is None:
        raise HTTPException(status_code=409, detail=NOT_SET_UP)
    # Stored only after `credential_keys.parse_nodevault_public_key` admitted it, and its hash recomputed from it.
    return SealKeyOut(
        key_id=nodevault_key_id(current.key_hash), algorithm=WRAP_ALGORITHM, public_key=current.public_key,
    )


# ── POST /api/nodevault/grants/{node_id}/{key_hash}/retry ────────────────────────────────────────────


@router.post("/api/nodevault/grants/{node_id}/{key_hash}/retry", response_model=GrantRetryOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def retry_grant(
    node_id: UUID, key_hash: str,
    user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
) -> GrantRetryOut:
    """A failed grant goes again: back to `waiting` — or `retire_pending` when its key is being retired or
    was abandoned — and `ensure_grants` restarts its workflow under a fresh generation (§3.3)."""
    org = _org(session, user)
    _require_plan_or_setup(session, org)
    # The node row first, as `ensure_grants` and a node's dial lock it: one lock order.
    session.scalar(select(Node.id).where(Node.id == node_id, Node.org_id == org.id).with_for_update())
    grant = session.scalar(
        select(NodeVaultGrant).where(
            NodeVaultGrant.org_id == org.id, NodeVaultGrant.node_id == node_id, NodeVaultGrant.key_hash == key_hash,
        ).with_for_update()
    )
    if grant is None:
        raise HTTPException(status_code=404, detail=GRANT_NOT_FOUND)
    if grant.state != NV_GRANT_FAILED:
        raise HTTPException(status_code=409, detail=GRANT_NOT_FAILED)
    state = durable_nodevault.reopen_failed_grant(session, grant)
    nodevault_grants.ensure_grants(session, node_id)
    session.commit()
    return GrantRetryOut(state=state)


# ── POST /api/nodevault/nodes/{node_id}/reset-identity ───────────────────────────────────────────────


@router.post("/api/nodevault/nodes/{node_id}/reset-identity", response_model=ResetIdentityOut)
@org_admin(Tenancy.CALLER_PLANE)
def reset_node_identity(
    node_id: UUID, user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
) -> ResetIdentityOut:
    """Forget a node's identity key (§3.5): clears it, deletes the node's grants, cancels their workflows and
    runs the abandoned-key cleanup for each key it held a grant on. The node's next identity POST registers
    its new key and `ensure_grants` re-enrols it by `peer`. Always a deliberate, audited admin act (D13).

    Sends the node nothing: a reset follows a wiped keystore (the identity changed because the keys went with
    it), and a key a node still holds after one is outside what the CP can see or revoke (D9)."""
    org = _org(session, user)
    _require_plan_or_setup(session, org)
    node = get_or_404(session, Node, node_id, detail=NODE_NOT_FOUND, where=(Node.org_id == org.id,), lock=True)
    # Key rows before grant rows, sorted — the abandoned-key cleanup's order — so the two never deadlock.
    held_keys = sorted(set(session.scalars(select(NodeVaultGrant.key_hash).where(NodeVaultGrant.node_id == node_id))))
    if held_keys:
        session.scalars(select(NodeVaultKey.id).where(
            NodeVaultKey.org_id == org.id, NodeVaultKey.key_hash.in_(held_keys),
        ).order_by(NodeVaultKey.key_hash).with_for_update()).all()
    grants = list(session.scalars(select(NodeVaultGrant).where(NodeVaultGrant.node_id == node_id).with_for_update()))
    workflows = [
        nodevault_grants.workflow_id(kind, node_id, grant.key_hash, grant.generation)
        for grant in grants for kind in nodevault_grants.WORKFLOW_KINDS
    ]
    key_hashes = sorted({grant.key_hash for grant in grants})
    for grant in grants:
        session.delete(grant)
    session.flush()
    previous_fp = node.nodevault_identity_fp
    node.nodevault_identity_key = None
    node.nodevault_identity_fp = None
    node.nodevault_identity_at = None
    for key_hash in key_hashes:
        durable_nodevault.delete_abandoned_key_if_clear(session, org.id, key_hash, commit=False)
    record_write(
        session, user, "nodevault.identity_reset", f"Reset the NodeVault identity of node {node.name}",
        data={"node_id": str(node_id), "identity_fp": previous_fp, "grants_removed": len(grants)}, org_id=org.id,
    )
    # Before the commit: once it lands, the node's next identity POST re-creates its grants at generation 0 —
    # the same workflow ids — and a cancel after that would land on the NEW runs.
    _cancel(workflows)
    session.commit()
    return ResetIdentityOut(node_id=node_id, grants_removed=len(grants))


def _cancel(workflow_ids: list[str]) -> None:
    """Cancel each grant workflow that is still running. Best-effort: the grant rows are already gone, so a
    workflow that outlives this ends on its own next step (it finds no grant)."""
    from dbos import DBOS

    try:
        running = [w.workflow_id for w in DBOS.list_workflows(
            workflow_ids=workflow_ids, status=["PENDING", "ENQUEUED", "DELAYED"], load_input=False, load_output=False,
        )] if workflow_ids else []
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("nodevault: could not list grant workflows to cancel (%s)", type(exc).__name__)
        return
    for wid in running:
        try:
            DBOS.cancel_workflow(wid)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.info("nodevault: could not cancel %s (%s)", wid, type(exc).__name__)


# ── Operations: setup, migration, results, abandon ──────────────────────────────────────────────────


class SetupIn(BaseModel):
    public_key: str
    source: str = Field(pattern="^(generate|upload)$")
    backup_confirmed: bool
    # node id → the key wrapped by the browser to that node's identity key (ciphertext; arch §2 key blob).
    blobs: dict[str, dict]


class OperationStartedOut(BaseModel):
    operation_id: UUID


class MigrationIn(BaseModel):
    credential_ids: Optional[list[UUID]] = Field(default=None, max_length=1000)
    all: Optional[bool] = None
    skip: Optional[bool] = None


class EligibleCredentialOut(BaseModel):
    id: UUID
    name: str
    type: str
    state: str   # kms | nodevault
    key_hash: Optional[str]


class IneligibleCredentialOut(BaseModel):
    id: UUID
    name: str
    type: str
    reason: str


class NodeVaultCredentialsOut(BaseModel):
    eligible: list[EligibleCredentialOut]
    ineligible: list[IneligibleCredentialOut]


class ItemOut(BaseModel):
    credential_id: UUID
    name: str
    phase: str
    state: str
    reason: Optional[str]
    # The reason as a sentence for the admin; None when there is none.
    message: Optional[str]


class OperationDetailOut(OperationOut):
    items: list[ItemOut]


class OperationStateOut(BaseModel):
    state: str


def _already_running(operation: NodeVaultOperation) -> JSONResponse:
    """AC-6.6: the one 409, with a link to the running operation."""
    return JSONResponse(status_code=409, content={"detail": ALREADY_RUNNING, "operation_id": str(operation.id)})


def _audit(session: Session, user: AdminUser, org: Org, kind: str, summary: str, data: dict) -> None:
    """AC-9.1's operator events (setup started, key generated/uploaded, backup confirmed, abandoned): hashes,
    ids and words the CP derived — the request body is suppressed from capture on this surface."""
    record_write(session, user, kind, summary, data=data, org_id=org.id)


def _blob_problem(blob: object, wrapping_key_id: str) -> bool:
    """True unless `blob` has the key blob's shape (§2) and names `wrapping_key_id` — a node's CURRENT identity
    (`nik:<fp>`, setup) or the org's current NodeVault key (`nv:<hash>`, a rotation's chain). Shape only: the CP
    cannot open a blob, and the node refuses one whose key does not hash to the key hash."""
    if not isinstance(blob, dict) or set(blob) != _BLOB_KEYS or blob.get("v") != 1:
        return True
    if blob.get("alg") != WRAP_ALGORITHM or blob.get("k") != wrapping_key_id:
        return True
    for field in ("ek", "n", "ct"):
        value = blob.get(field)
        if not isinstance(value, str) or not value or len(value) > _BLOB_FIELD_MAX_CHARS:
            return True
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return True
    return False


def _org_operation(session: Session, org: Org, operation_id: UUID, *, lock: bool = False) -> NodeVaultOperation:
    return get_or_404(
        session, NodeVaultOperation, operation_id, detail=OPERATION_NOT_FOUND,
        where=(NodeVaultOperation.org_id == org.id,), lock=lock,
    )


@router.post("/api/nodevault/setup", response_model=OperationStartedOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def start_nodevault_setup(
    body: SetupIn, user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
):
    """Start Setup (§3.4 `nodevault-setup`): store the key `pending` (its hash recomputed here, never taken
    from the caller), one `node` grant per capable node carrying the browser's blob, and the operation; the
    deliveries and the operation's workflow start after the commit. The key becomes `current` only once a node
    has ACKNOWLEDGED it."""
    org = _org(session, user)
    status = nodevault_policy.status(session, org)
    if not status.allowed:
        raise HTTPException(status_code=403, detail=NOT_IN_PLAN)
    running = _running_operation(session, org.id)
    if running is not None:
        return _already_running(running)
    if status.setup:
        raise HTTPException(status_code=409, detail=ALREADY_SET_UP)
    if body.backup_confirmed is not True:
        raise HTTPException(status_code=422, detail=BACKUP_REQUIRED)
    try:
        key_hash = nodevault_key_hash(parse_nodevault_public_key(body.public_key))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    # The nodes first, locked in one order, as `ensure_grants` and a node's dial lock them.
    capable = {
        str(node.id): node for node in session.scalars(
            select(Node).where(
                Node.org_id == org.id, *nodevault_grants.key_holder_clause(),
            ).order_by(Node.id).with_for_update()
        )
    }
    missing, extra = sorted(set(capable) - set(body.blobs)), sorted(set(body.blobs) - set(capable))
    if missing or extra:
        # The browser refetches the node list, wraps for the new set and resubmits (§3.5 setup blob race).
        return JSONResponse(status_code=422, content={"detail": NODES_CHANGED, "missing": missing, "extra": extra})
    for node_id, node in capable.items():
        if _blob_problem(body.blobs[node_id], f"nik:{node.nodevault_identity_fp}"):
            raise HTTPException(status_code=422, detail=BAD_BLOB.format(name=node.name))
    online = set(node_presence.connected_node_ids(session))
    if not online & set(capable):
        raise HTTPException(status_code=409, detail=NO_ONLINE_NODE)

    key = session.scalar(
        select(NodeVaultKey).where(NodeVaultKey.org_id == org.id, NodeVaultKey.key_hash == key_hash).with_for_update()
    )
    grants: dict[str, NodeVaultGrant] = {}
    stale_workflows: list[str] = []
    if key is not None:
        # A re-run with the same key: only a `pending` one a failed setup left, or an `abandoned` one (§3.2).
        if key.state not in (NV_KEY_PENDING, NV_KEY_ABANDONED):
            raise HTTPException(status_code=409, detail=KEY_IN_USE)
        for grant in session.scalars(select(NodeVaultGrant).where(
            NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == key_hash,
        ).order_by(NodeVaultGrant.node_id).with_for_update()):
            if str(grant.node_id) not in capable:
                # No blob is asked for a node that can't hold the key; whatever it got before is taken off.
                if grant.state != NV_GRANT_RETIRED:
                    grant.state, grant.last_error, grant.blob, grant.blob_kind = NV_GRANT_RETIRE_PENDING, None, None, None
                continue
            stale_workflows += [
                nodevault_grants.workflow_id(kind, grant.node_id, key_hash, grant.generation)
                for kind in nodevault_grants.WORKFLOW_KINDS
            ]
            # A fresh generation: the new deliveries never share an id with a cancelled run.
            grant.generation = int(grant.generation) + 1
            grant.state, grant.last_error, grant.blob, grant.blob_kind = NV_GRANT_WAITING, None, None, None
            grant.delivered_at = None
            grants[str(grant.node_id)] = grant
        key.state, key.source, key.chain_blob = NV_KEY_PENDING, body.source, None
    else:
        key = NodeVaultKey(
            name=key_hash[:12], org_id=org.id, key_hash=key_hash, public_key=body.public_key.strip(),
            state=NV_KEY_PENDING, source=body.source,
        )
        session.add(key)
    key.backup_confirmed_at = func.now()
    for node_id in capable:
        grant = grants.get(node_id)
        if grant is None:
            grant = NodeVaultGrant(org_id=org.id, node_id=UUID(node_id), key_hash=key_hash,
                                   state=NV_GRANT_WAITING, generation=0)
            session.add(grant)
        grant.blob, grant.blob_kind = body.blobs[node_id], "node"
    operation = NodeVaultOperation(org_id=org.id, kind=durable_nodevault.SETUP, step=durable_nodevault.STEP_DISTRIBUTE,
                                   key_hash=key_hash, counts={})
    session.add(operation)
    try:
        session.flush()
    except IntegrityError:
        # Another setup won the one-running-operation index between the check and this insert.
        session.rollback()
        running = _running_operation(session, org.id)
        if running is not None:
            return _already_running(running)
        raise
    operation.workflow_id = durable_nodevault.operation_workflow_id(durable_nodevault.SETUP, operation.id)
    if stale_workflows:
        # The superseded generations' runs; after the commit, so a refused request cancels nothing.
        after_commit.on_commit(session, lambda: durable_nodevault.cancel_workflows(stale_workflows),
                               key=f"nodevault-stale:{key_hash}")
    for grant in session.scalars(select(NodeVaultGrant).where(
        NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == key_hash,
    )):
        nodevault_grants.start_grant_workflow(session, grant)
    verb = "generated" if body.source == "generate" else "uploaded"
    _audit(session, user, org, "nodevault.setup_started", f"Started NodeVault setup with key {key_hash[:12]}",
           {"operation_id": str(operation.id), "key_hash": key_hash, "nodes": len(capable)})
    _audit(session, user, org, f"nodevault.key_{verb}", f"NodeVault key {key_hash[:12]} {verb} in the browser",
           {"key_hash": key_hash, "source": body.source})
    _audit(session, user, org, "nodevault.backup_confirmed", f"Confirmed the backup of NodeVault key {key_hash[:12]}",
           {"key_hash": key_hash})
    op_id = operation.id
    after_commit.on_commit(session, lambda: durable_nodevault.start_operation(durable_nodevault.SETUP, op_id),
                           key=f"nodevault-op:{op_id}")
    session.commit()
    return OperationStartedOut(operation_id=op_id)


@router.get("/api/nodevault/credentials", response_model=NodeVaultCredentialsOut)
@org_admin(Tenancy.CALLER_PLANE)
def list_nodevault_credentials(
    user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
) -> NodeVaultCredentialsOut:
    """The Migrate step's list: every credential of the org that is or can be NodeVault, and every one that
    can't, with the reason (`credential_consumers` is the one home of eligibility). Reads tier 1 only."""
    org = _org(session, user)
    _require_plan_or_setup(session, org)
    eligible: list[EligibleCredentialOut] = []
    ineligible: list[IneligibleCredentialOut] = []
    for credential in session.scalars(
        select(Credential).where(Credential.org_id == org.id).order_by(Credential.name, Credential.id)
    ):
        config = credential.config or {}
        if credential.nodevault:
            eligible.append(EligibleCredentialOut(
                id=credential.id, name=credential.name, type=credential.type, state="nodevault",
                key_hash=credential_key_hash(config),
            ))
            continue
        word = durable_nodevault.ineligibility(session, credential)
        reason = None
        if word == "in_use":
            reason = credential_consumers.consumers_reason(credential_consumers.consumers_of_row(session, credential))
        elif word is not None:
            reason = ITEM_MESSAGES[word]
        if reason is None:
            eligible.append(EligibleCredentialOut(
                id=credential.id, name=credential.name, type=credential.type, state="kms", key_hash=None,
            ))
        else:
            ineligible.append(IneligibleCredentialOut(
                id=credential.id, name=credential.name, type=credential.type, reason=reason,
            ))
    return NodeVaultCredentialsOut(eligible=eligible, ineligible=ineligible)


def _record_choice(
    session: Session, org: Org, operation: NodeVaultOperation, body: MigrationIn,
) -> int:
    """The chosen credentials as the operation's item rows — `pending`, or `skipped` with the reason each can't
    move. How many are pending. Unknown ids are a 422; with `all`, credentials already NodeVault are left out."""
    query = select(Credential).where(Credential.org_id == org.id)
    if body.credential_ids is not None:
        wanted = set(body.credential_ids)
        query = query.where(Credential.id.in_(wanted))
    credentials = list(session.scalars(query.order_by(Credential.name, Credential.id)))
    if body.credential_ids is not None and {c.id for c in credentials} != wanted:
        raise HTTPException(status_code=422, detail=UNKNOWN_CREDENTIALS)
    pending = 0
    for credential in credentials:
        reason = durable_nodevault.ineligibility(session, credential)
        if body.all and reason == "already_nodevault":
            continue
        session.add(NodeVaultOperationItem(
            org_id=org.id, operation_id=operation.id, credential_id=credential.id, phase="migrate",
            state=durable_nodevault.ITEM_PENDING if reason is None else durable_nodevault.ITEM_SKIPPED, reason=reason,
        ))
        pending += reason is None
    session.flush()
    return pending


@router.post("/api/nodevault/migrations", response_model=OperationStartedOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def start_nodevault_migration(
    body: MigrationIn, user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
):
    """The admin's migrate choice. While the running setup waits at `migrate_choice` it is recorded on that
    operation (its items; the step moves on) and the workflow is woken; otherwise a standalone `migration`
    operation moves the chosen KMS credentials to the current key. `skip` only in the first case — and
    allowed even when frozen, so a frozen org can finish its setup (§3.5)."""
    if sum((body.credential_ids is not None, bool(body.all), bool(body.skip))) != 1:
        raise HTTPException(status_code=422, detail=CHOOSE_ONE)
    org = _org(session, user)
    status = _require_plan_or_setup(session, org)
    running = session.scalar(select(NodeVaultOperation).where(
        NodeVaultOperation.org_id == org.id, NodeVaultOperation.state == NV_OP_RUNNING,
    ).with_for_update())
    if running is not None and running.step == durable_nodevault.STEP_MIGRATE_CHOICE:
        if not body.skip and not status.allowed:
            raise HTTPException(status_code=403, detail=NOT_IN_PLAN)
        if not body.skip and _record_choice(session, org, running, body) == 0 and body.credential_ids is not None:
            raise HTTPException(status_code=422, detail=NOTHING_TO_MIGRATE)
        running.step = durable_nodevault.STEP_MIGRATING
        wid = running.workflow_id
        after_commit.on_commit(
            session, lambda: durable_nodevault.send_to_running([wid], durable_nodevault.MIGRATE_CHOICE),
            key=f"nodevault-choice:{running.id}",
        )
        session.commit()
        return OperationStartedOut(operation_id=running.id)
    if running is not None:
        return _already_running(running)
    if body.skip:
        raise HTTPException(status_code=422, detail=SKIP_ONLY_IN_SETUP)
    if not status.allowed:
        raise HTTPException(status_code=403, detail=NOT_IN_PLAN)
    current = session.scalar(
        select(NodeVaultKey).where(NodeVaultKey.org_id == org.id, NodeVaultKey.state == NV_KEY_CURRENT)
    )
    if current is None:
        raise HTTPException(status_code=409, detail=NOT_SET_UP)
    operation = NodeVaultOperation(org_id=org.id, kind=durable_nodevault.MIGRATION,
                                   step=durable_nodevault.STEP_MIGRATING, key_hash=current.key_hash, counts={})
    session.add(operation)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        running = _running_operation(session, org.id)
        if running is not None:
            return _already_running(running)
        raise
    if _record_choice(session, org, operation, body) == 0:
        raise HTTPException(status_code=422, detail=NOTHING_TO_MIGRATE)
    operation.workflow_id = durable_nodevault.operation_workflow_id(durable_nodevault.MIGRATION, operation.id)
    op_id = operation.id
    after_commit.on_commit(session, lambda: durable_nodevault.start_operation(durable_nodevault.MIGRATION, op_id),
                           key=f"nodevault-op:{op_id}")
    session.commit()
    return OperationStartedOut(operation_id=op_id)


@router.get("/api/nodevault/operations/{operation_id}", response_model=OperationDetailOut)
@org_admin(Tenancy.CALLER_PLANE)
def get_nodevault_operation(
    operation_id: UUID, user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
) -> OperationDetailOut:
    """One operation — running or closed — with its per-credential results: the Result step (AC-4.8). Counts
    are read off the rows; `nodes_holding`/`nodes_total` say how many of the org's nodes hold its key."""
    org = _org(session, user)
    _require_plan_or_setup(session, org)
    operation = _org_operation(session, org, operation_id)
    grants = session.execute(
        select(NodeVaultGrant.node_id, NodeVaultGrant.key_hash, NodeVaultGrant.state,
               NodeVaultGrant.blob.is_(None).label("no_blob"))
        .where(NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == operation.key_hash)
    ).all() if operation.key_hash else []
    out = _operation_out(session, operation, grants, set())
    out.counts["nodes_holding"] = sum(1 for grant in grants if grant.state == NV_GRANT_DELIVERED)
    # Out of the nodes that CAN hold a key (capable, with an identity): the others never will (AC-4.8).
    out.counts["nodes_total"] = int(session.scalar(select(func.count()).select_from(Node).where(
        Node.org_id == org.id, *nodevault_grants.key_holder_clause(),
    )))
    rows = session.execute(
        select(NodeVaultOperationItem, Credential.name)
        .join(Credential, Credential.id == NodeVaultOperationItem.credential_id)
        .where(NodeVaultOperationItem.operation_id == operation.id)
        .order_by(Credential.name, NodeVaultOperationItem.credential_id)
    ).all()
    items = [
        ItemOut(credential_id=item.credential_id, name=name, phase=item.phase, state=item.state, reason=item.reason,
                message=ITEM_MESSAGES.get(item.reason) if item.reason else None)
        for item, name in rows
    ]
    if operation.state != NV_OP_RUNNING:
        unsettled = [i.credential_id for i in items if i.state in (durable_nodevault.ITEM_PENDING,
                                                                     durable_nodevault.ITEM_SEALED)]
        if unsettled:
            durable_nodevault.ensure_item_retries_started(operation.id, unsettled)
    return OperationDetailOut(**out.model_dump(), items=items)


@router.post("/api/nodevault/operations/{operation_id}/items/{credential_id}/retry",
             response_model=OperationStateOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def retry_migration_item(
    operation_id: UUID, credential_id: UUID,
    user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
):
    """A failed credential goes again as `nodevault-migrate-item:{op}:{credential}:{n}` (AC-7.3). Refused while
    frozen (§3.5) and while ANOTHER operation runs."""
    org = _org(session, user)
    if not _require_plan_or_setup(session, org).allowed:
        raise HTTPException(status_code=403, detail=NOT_IN_PLAN)
    operation = _org_operation(session, org, operation_id, lock=True)
    running = _running_operation(session, org.id)
    if running is not None and running.id != operation.id:
        return _already_running(running)
    if operation.state == durable_nodevault.OP_FAILED:
        raise HTTPException(status_code=409, detail=OPERATION_ABANDONED)
    if not session.scalar(select(NodeVaultKey.id).where(
        NodeVaultKey.org_id == org.id, NodeVaultKey.key_hash == operation.key_hash, NodeVaultKey.state == NV_KEY_CURRENT,
    )):
        raise HTTPException(status_code=409, detail=KEY_NOT_CURRENT)
    item = session.scalar(select(NodeVaultOperationItem).where(
        NodeVaultOperationItem.operation_id == operation.id, NodeVaultOperationItem.credential_id == credential_id,
        NodeVaultOperationItem.phase == "migrate",
    ).with_for_update())
    if item is None:
        raise HTTPException(status_code=404, detail=ITEM_NOT_FOUND)
    if item.state != durable_nodevault.ITEM_FAILED:
        raise HTTPException(status_code=409, detail=ITEM_NOT_FAILED)
    item.state, item.reason, item.staged = durable_nodevault.ITEM_PENDING, None, None
    op_id = operation.id
    after_commit.on_commit(session, lambda: durable_nodevault.start_item_retry(op_id, credential_id),
                           key=f"nodevault-item:{op_id}:{credential_id}")
    session.commit()
    return OperationStateOut(state=durable_nodevault.ITEM_PENDING)


@router.post("/api/nodevault/operations/{operation_id}/abandon", response_model=OperationStateOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def abandon_nodevault_operation(
    operation_id: UUID, user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
):
    """Abandon (§3.5), allowed whatever the plan. A SETUP or ROTATION before promotion: its new key →
    `abandoned` (never sealed under, never delivered again), every grant → `retire_pending` so the key is taken
    off every node that may have installed it, and the rows are deleted by the last `retire-node` to finish —
    nothing is deleted up front. A MIGRATION at any step: done items stay done, unsettled ones are dropped with
    their staged ciphertext — and so does a setup or rotation in its migrating step, or a rotation in its rekey
    (the new key stays current). A RETIRE while some node may still hold the key: the key back to `previous`
    and re-delivered to every node it was taken off. The operation closes `failed`, and the abandon is
    audited."""
    org = _org(session, user)
    _require_plan_or_setup(session, org)
    operation = _org_operation(session, org, operation_id, lock=True)
    if operation.state != NV_OP_RUNNING:
        raise HTTPException(status_code=409, detail=OPERATION_FINISHED)
    workflows = [operation.workflow_id] if operation.workflow_id else []
    prefixes: list[str] = []
    # A setup's or rotation's MIGRATING step is a migration, and a rotation's REKEY waits on a holder of both keys
    # the same way (either can wait on a holder that never returns): abandoned like one, the new key staying
    # current and a credential not yet rekeyed staying on the previous key. Between promotion and the choice,
    # Skip is the way forward.
    migrating = operation.kind == durable_nodevault.MIGRATION or (
        operation.kind in (durable_nodevault.SETUP, durable_nodevault.ROTATION)
        and operation.step in (durable_nodevault.STEP_MIGRATING, durable_nodevault.STEP_REKEY)
    )
    if operation.kind in (durable_nodevault.SETUP, durable_nodevault.ROTATION) and not migrating:
        if operation.kind == durable_nodevault.SETUP and operation.step != durable_nodevault.STEP_DISTRIBUTE:
            raise HTTPException(status_code=409, detail=CANT_ABANDON_PROMOTED)
        if operation.kind == durable_nodevault.ROTATION and operation.step not in (
                durable_nodevault.STEP_DISTRIBUTE, durable_nodevault.STEP_PROMOTE):
            raise HTTPException(status_code=409, detail=CANT_ABANDON_ROTATED)
        key_hash = operation.key_hash
        node_ids = sorted(set(session.scalars(select(NodeVaultGrant.node_id).where(
            NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == key_hash))))
        # Node rows, then the key row, then grants — `ensure_grants`' and the cleanup's lock order.
        if node_ids:
            session.scalars(select(Node.id).where(Node.id.in_(node_ids)).order_by(Node.id).with_for_update()).all()
        key = session.scalar(select(NodeVaultKey).where(
            NodeVaultKey.org_id == org.id, NodeVaultKey.key_hash == key_hash).with_for_update())
        if key is not None:
            key.state = NV_KEY_ABANDONED
        grants = list(session.scalars(select(NodeVaultGrant).where(
            NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == key_hash,
        ).order_by(NodeVaultGrant.node_id).with_for_update()))
        for grant in grants:
            workflows.append(nodevault_grants.workflow_id(nodevault_grants.DELIVER, grant.node_id, key_hash,
                                                          grant.generation))
            grant.state, grant.last_error, grant.blob, grant.blob_kind = NV_GRANT_RETIRE_PENDING, None, None, None
        session.flush()
        for grant in grants:
            nodevault_grants.start_grant_workflow(session, grant)
    elif migrating:
        prefixes.append(f"{durable_nodevault.ITEM_RETRY_PREFIX}:{operation.id}:")
        session.execute(delete(NodeVaultOperationItem).where(
            NodeVaultOperationItem.operation_id == operation.id,
            NodeVaultOperationItem.state.in_((durable_nodevault.ITEM_PENDING, durable_nodevault.ITEM_SEALED)),
        ))
    elif operation.kind == durable_nodevault.RETIRE:
        key_hash = operation.key_hash
        node_ids = sorted(set(session.scalars(select(NodeVaultGrant.node_id).where(
            NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == key_hash))))
        if node_ids:
            session.scalars(select(Node.id).where(Node.id.in_(node_ids)).order_by(Node.id).with_for_update()).all()
        key = session.scalar(select(NodeVaultKey).where(
            NodeVaultKey.org_id == org.id, NodeVaultKey.key_hash == key_hash).with_for_update())
        if key is not None and key.state == NV_KEY_RETIRING and not session.scalar(select(func.count()).where(
            NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == key_hash, NodeVaultGrant.state != NV_GRANT_RETIRED,
        )):
            # No node may still hold it: putting it back would make a key nobody can open sealable again.
            raise HTTPException(status_code=409, detail=KEY_GONE_EVERYWHERE)
        if key is not None and key.state in (NV_KEY_PREVIOUS, NV_KEY_RETIRING):
            # The runs restore_previous supersedes (it bumps each grant's generation).
            workflows += [
                nodevault_grants.workflow_id(nodevault_grants.RETIRE_NODE, node_id, key_hash, generation)
                for node_id, generation in session.execute(select(NodeVaultGrant.node_id, NodeVaultGrant.generation)
                                                           .where(NodeVaultGrant.org_id == org.id,
                                                                  NodeVaultGrant.key_hash == key_hash))
            ]
            for node_id in durable_nodevault.restore_previous(session, org.id, key):
                nodevault_grants.ensure_grants(session, node_id)
    else:
        raise HTTPException(status_code=409, detail=CANT_ABANDON_KIND)
    operation.state = durable_nodevault.OP_FAILED
    operation.counts = {**(operation.counts or {}), **durable_nodevault.item_counts(session, operation.id)}
    _audit(session, user, org, "nodevault.operation_abandoned", f"Abandoned the NodeVault {operation.kind}",
           {"operation_id": str(operation.id), "kind": operation.kind, "key_hash": operation.key_hash,
            "step": operation.step})
    # After the commit: every workflow re-checks its rows at its next step, so one that outlives the cancel
    # finds the operation failed, the grants retiring or its item gone, and ends.
    after_commit.on_commit(session, lambda: durable_nodevault.cancel_workflows(workflows, prefixes),
                           key=f"nodevault-abandon:{operation.id}")
    session.commit()
    return OperationStateOut(state=operation.state)


# ── Rotation and retire ──────────────────────────────────────────────────────────────────────────────


class RotationIn(BaseModel):
    public_key: str
    source: str = Field(pattern="^(generate|upload)$")
    backup_confirmed: bool
    # The new private key wrapped by the browser under the org's CURRENT NodeVault key (ciphertext; §2 key blob,
    # recipient `chain:<current hash>`, E2). The CP cannot open it; only a node holding the current key can.
    chain_blob: dict


def _flush_operation(session: Session, org: Org, operation: NodeVaultOperation) -> Optional[JSONResponse]:
    """Insert the operation; the 409 when another one won the one-running-operation index first."""
    session.add(operation)
    failure: Optional[str] = None
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        running = _running_operation(session, org.id)
        if running is not None:
            return _already_running(running)
        failure = type(exc).__name__
    if failure is not None:
        # Type only, outside the `except`: the error's bound parameters hold the flush's rows — a chain blob.
        raise RuntimeError(f"NodeVault operation insert failed ({failure})")
    operation.workflow_id = durable_nodevault.operation_workflow_id(operation.kind, operation.id)
    return None


@router.post("/api/nodevault/rotation", response_model=OperationStartedOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def start_nodevault_rotation(
    body: RotationIn, user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
):
    """Start a rotation (§3.4 `nodevault-rotate`): store the new key `pending` (its hash recomputed here) with
    its chain blob, and enrol every capable node in it — a node holding the current key gets the chain blob, any
    other node a peer's wrap. The key becomes `current` only once every capable node online at the check has
    ACKNOWLEDGED it; the old key stays on every node as `previous` until an admin retires it."""
    org = _org(session, user)
    if not nodevault_policy.status(session, org).allowed:
        raise HTTPException(status_code=403, detail=NOT_IN_PLAN)
    running = _running_operation(session, org.id)
    if running is not None:
        return _already_running(running)
    current = session.scalar(select(NodeVaultKey).where(
        NodeVaultKey.org_id == org.id, NodeVaultKey.state == NV_KEY_CURRENT).with_for_update())
    if current is None:
        raise HTTPException(status_code=409, detail=NOT_SET_UP)
    if body.backup_confirmed is not True:
        raise HTTPException(status_code=422, detail=BACKUP_REQUIRED)
    try:
        key_hash = nodevault_key_hash(parse_nodevault_public_key(body.public_key))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    # AC-6.2: the new private key reaches nodes only wrapped under the CURRENT key — never in a form the CP opens.
    if _blob_problem(body.chain_blob, nodevault_key_id(current.key_hash)):
        raise HTTPException(status_code=422, detail=NOT_ROTATED_BLOB)

    # The nodes first, locked in one order, as `ensure_grants` and a node's dial lock them.
    capable = [node.id for node in session.scalars(select(Node).where(
        Node.org_id == org.id, *nodevault_grants.key_holder_clause(),
    ).order_by(Node.id).with_for_update())]
    online = set(node_presence.connected_node_ids(session))
    holders = {str(n) for n in session.scalars(select(NodeVaultGrant.node_id).where(
        NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == current.key_hash,
        NodeVaultGrant.state == NV_GRANT_DELIVERED,
    ))}
    if not {str(n) for n in capable} & holders & online:
        raise HTTPException(status_code=409, detail=NO_ONLINE_HOLDER)

    key = session.scalar(
        select(NodeVaultKey).where(NodeVaultKey.org_id == org.id, NodeVaultKey.key_hash == key_hash).with_for_update()
    )
    stale_workflows: list[str] = []
    if key is not None:
        # A re-run with the same key: only a `pending` one a failed operation left, or an `abandoned` one (§3.2).
        if key.state not in (NV_KEY_PENDING, NV_KEY_ABANDONED):
            raise HTTPException(status_code=409, detail=KEY_IN_USE)
        for grant in session.scalars(select(NodeVaultGrant).where(
            NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == key_hash,
        ).order_by(NodeVaultGrant.node_id).with_for_update()):
            stale_workflows += [
                nodevault_grants.workflow_id(kind, grant.node_id, key_hash, grant.generation)
                for kind in nodevault_grants.WORKFLOW_KINDS
            ]
            grant.generation = int(grant.generation) + 1
            grant.last_error, grant.blob, grant.blob_kind, grant.delivered_at = None, None, None, None
            if grant.node_id in capable:
                grant.state = NV_GRANT_WAITING
            elif grant.state != NV_GRANT_RETIRED:
                grant.state = NV_GRANT_RETIRE_PENDING
        key.state, key.source = NV_KEY_PENDING, body.source
    else:
        key = NodeVaultKey(
            name=key_hash[:12], org_id=org.id, key_hash=key_hash, public_key=body.public_key.strip(),
            state=NV_KEY_PENDING, source=body.source,
        )
        session.add(key)
    key.chain_blob = body.chain_blob
    key.backup_confirmed_at = func.now()
    operation = NodeVaultOperation(org_id=org.id, kind=durable_nodevault.ROTATION,
                                   step=durable_nodevault.STEP_DISTRIBUTE, key_hash=key_hash,
                                   prev_key_hash=current.key_hash, counts={})
    refused = _flush_operation(session, org, operation)
    if refused is not None:
        return refused
    if stale_workflows:
        after_commit.on_commit(session, lambda: durable_nodevault.cancel_workflows(stale_workflows),
                               key=f"nodevault-stale:{key_hash}")
    for node_id in capable:
        nodevault_grants.ensure_grants(session, node_id)
    for grant in session.scalars(select(NodeVaultGrant).where(
        NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == key_hash,
        NodeVaultGrant.state == NV_GRANT_RETIRE_PENDING,
    )):
        nodevault_grants.start_grant_workflow(session, grant)
    verb = "generated" if body.source == "generate" else "uploaded"
    _audit(session, user, org, "nodevault.rotation_started",
           f"Started NodeVault rotation from key {current.key_hash[:12]} to {key_hash[:12]}",
           {"operation_id": str(operation.id), "key_hash": key_hash, "prev_key_hash": current.key_hash,
            "nodes": len(capable)})
    _audit(session, user, org, f"nodevault.key_{verb}", f"NodeVault key {key_hash[:12]} {verb} in the browser",
           {"key_hash": key_hash, "source": body.source})
    _audit(session, user, org, "nodevault.backup_confirmed", f"Confirmed the backup of NodeVault key {key_hash[:12]}",
           {"key_hash": key_hash})
    op_id = operation.id
    after_commit.on_commit(session, lambda: durable_nodevault.start_operation(durable_nodevault.ROTATION, op_id),
                           key=f"nodevault-op:{op_id}")
    session.commit()
    return OperationStartedOut(operation_id=op_id)


@router.post("/api/nodevault/keys/{key_hash}/retire", response_model=OperationStartedOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def retire_nodevault_key(
    key_hash: str, user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
):
    """Retire an old key (§3.4 `nodevault-retire`, D8): refused unless it is `previous` and no credential is
    sealed under it (AC-6.7). The workflow takes it off every node — an offline one when it reconnects — and
    marks it `retired` only once every node confirmed. It cannot be undone once it completes."""
    org = _org(session, user)
    if not nodevault_policy.status(session, org).allowed:
        raise HTTPException(status_code=403, detail=NOT_IN_PLAN)
    key = session.scalar(select(NodeVaultKey).where(
        NodeVaultKey.org_id == org.id, NodeVaultKey.key_hash == key_hash).with_for_update())
    if key is None:
        raise HTTPException(status_code=404, detail=KEY_NOT_FOUND)
    if key.state != NV_KEY_PREVIOUS:
        raise HTTPException(status_code=409, detail=ONLY_OLD_KEYS)
    # The count before the running-operation check: mid-rotation the admin is told WHY (AC-6.7).
    in_use = len(durable_nodevault.credentials_on_key(session, org.id, key_hash))
    if in_use:
        raise HTTPException(status_code=409, detail=keys_in_use(in_use))
    running = _running_operation(session, org.id)
    if running is not None:
        return _already_running(running)
    operation = NodeVaultOperation(org_id=org.id, kind=durable_nodevault.RETIRE, step=durable_nodevault.STEP_RETIRING,
                                   key_hash=key_hash, counts={})
    refused = _flush_operation(session, org, operation)
    if refused is not None:
        return refused
    _audit(session, user, org, "nodevault.retire_started", f"Started retiring old NodeVault key {key_hash[:12]}",
           {"operation_id": str(operation.id), "key_hash": key_hash})
    op_id = operation.id
    after_commit.on_commit(session, lambda: durable_nodevault.start_operation(durable_nodevault.RETIRE, op_id),
                           key=f"nodevault-op:{op_id}")
    session.commit()
    return OperationStartedOut(operation_id=op_id)


@router.post("/api/nodevault/start-over", response_model=OperationStateOut)
@org_admin(Tenancy.CALLER_PLANE)
def start_nodevault_over(
    user: AdminUser = Depends(require_user), session: Session = Depends(get_session),
    principal: Optional[Principal] = Depends(current_principal),
):
    """Start over (D4): the way out for an org whose current key no node holds. The org Owner only, and only
    while no node has the key delivered and no operation runs. The key goes `retired` and every grant of it
    `retire_pending` (each node's `retire-node` confirms it is gone), so the org is no longer set up and Setup
    can run with a fresh key. Credentials sealed under the key stay sealed — nothing can open them — and must be
    re-entered. Allowed whatever the plan, like abandon, so a frozen org is never stuck. Audited."""
    org = _org(session, user)
    _require_plan_or_setup(session, org)
    if not _is_owner(principal):
        raise HTTPException(status_code=403, detail=OWNER_ONLY)
    running = _running_operation(session, org.id)
    if running is not None:
        return _already_running(running)
    current = session.scalar(select(NodeVaultKey.key_hash).where(
        NodeVaultKey.org_id == org.id, NodeVaultKey.state == NV_KEY_CURRENT))
    if current is None:
        raise HTTPException(status_code=409, detail=NOT_SET_UP)
    # Node rows, then the key row, then grants — `ensure_grants`' lock order. The holder check reads the LOCKED
    # grants, so a delivery `_reconcile` records now either commits first (and refuses this) or waits for it.
    node_ids = sorted(set(session.scalars(select(NodeVaultGrant.node_id).where(
        NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == current))))
    if node_ids:
        session.scalars(select(Node.id).where(Node.id.in_(node_ids)).order_by(Node.id).with_for_update()).all()
    key = session.scalar(select(NodeVaultKey).where(
        NodeVaultKey.org_id == org.id, NodeVaultKey.key_hash == current).with_for_update())
    if key is None or key.state != NV_KEY_CURRENT:
        raise HTTPException(status_code=409, detail=NOT_SET_UP)
    grants = list(session.scalars(select(NodeVaultGrant).where(
        NodeVaultGrant.org_id == org.id, NodeVaultGrant.key_hash == current, NodeVaultGrant.state != NV_GRANT_RETIRED,
    ).order_by(NodeVaultGrant.node_id).with_for_update()))
    if _current_key_held(session, org.id, current):
        raise HTTPException(status_code=409, detail=KEY_HELD)
    durable_nodevault.retire_key_row(session, key)
    # The runs this supersedes; a fresh generation, so the new retire-node runs never share their ids.
    workflows = [nodevault_grants.workflow_id(kind, grant.node_id, current, grant.generation)
                 for grant in grants for kind in nodevault_grants.WORKFLOW_KINDS]
    for grant in grants:
        grant.generation = int(grant.generation) + 1
        grant.state, grant.last_error, grant.blob, grant.blob_kind = NV_GRANT_RETIRE_PENDING, None, None, None
    session.flush()
    for grant in grants:
        nodevault_grants.start_grant_workflow(session, grant)
    lost = len(durable_nodevault.credentials_on_key(session, org.id, current))
    _audit(session, user, org, "nodevault.started_over", f"Started NodeVault over, retiring key {current[:12]}",
           {"key_hash": current, "credentials_lost": lost, "nodes": len(grants)})
    after_commit.on_commit(session, lambda: durable_nodevault.cancel_workflows(workflows),
                           key=f"nodevault-start-over:{current}")
    session.commit()
    return OperationStateOut(state=NV_KEY_RETIRED)
