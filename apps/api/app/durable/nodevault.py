"""NodeVault key delivery and node retirement as durable workflows.

One DBOS workflow per (node, key) placement, under the deterministic id
`nodevault-<deliver|retire-node>:{node}:{key_hash}:{generation}` (`app/nodevault_grants.py::workflow_id`,
which starts them). The bodies here register themselves with that starter (`nodevault_grants.register`).

`deliver` — put one NodeVault key on one node:
  * step 0 produces the grant's blob when it has none: the rotation's `chain` blob when the key has one AND
    the node already holds its predecessor, else a `peer` blob — an online node that holds the key wraps it
    to this node's identity key (`nodevault.wrap_for_node`). With neither, the workflow waits on
    `HOLDER_ONLINE` in a re-check loop and the console reads the grant as waiting for a holder; it never
    gives up (AC-3.4).
  * then `nodevault.install_key`: `offline` waits on `NODE_ONLINE` (re-check loop) and parks after
    `PARK_AFTER_SECONDS` leaving the grant `waiting`, so the node's next contact restarts it
    (`ensure_grants`); any other failure retries with backoff, `MAX_ATTEMPTS` in all, then the grant is
    `failed` with the node's closed-vocabulary word; `delivered` writes the grant, the
    `nodevault.key_delivered` audit event (node, hash) and signals the org's running operation.
`retire-node` — take one key off one node: the same offline loop around `nodevault.retire_key`; a deleted
node counts as retired. Its last step deletes an `abandoned` key's rows once no grant of it is un-retired,
under `SELECT … FOR UPDATE` on the key row so concurrent finishers delete exactly once.

Every reply's `held` (the node's truth) reconciles all of that node's grants (`_reconcile`).

THE FLOOR: no step input or output carries a value, a data key, a private key, a passphrase, a backup or a
blob — steps take and return ids, hashes, states and closed-vocabulary words only; a blob moves between the
grant row and the node inside one step and is never returned (AC-7.2). Node calls hop to the serving loop
as `app/durable/accounts.py::dispatch` does; only the dispatch crosses, and the `command_audit_log` row is
written on the step's own thread with the NodeVault ciphertext redacted (`commands._credential_safe_args`).
DB work runs through `SessionLocal` bound to the node's org (`seam.bind_from`), never holding a connection
across a node call.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional
from uuid import UUID

from dbos import DBOS
from dbos._error import DBOSWorkflowCancelledError
from fastapi import HTTPException
from sqlalchemy import delete, exists, func, null, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import node_presence, nodevault_grants, user_activity
from app.db import SessionLocal
from app.durable import runtime
from app.entity_shape import optional_uuid
from app.models.credentials import (
    NODEVAULT_GRANT_ERRORS,
    NODEVAULT_LIVE_KEY_STATES,
    NODEVAULT_TRANSIENT_GRANT_ERRORS,
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
    NodeVaultGrant,
    NodeVaultKey,
    NodeVaultOperation,
)
from app.models.nodes import Node
from app.tenancy import exclusion_log, seam

log = logging.getLogger(__name__)

# ── Topics (`DBOS.send` / `DBOS.recv`) ────────────────────────────────────────────────────────────────
#: To a node's own grant workflows: that node just dialled in.
NODE_ONLINE = "nodevault.node_online"
#: To every running grant workflow of the org and its running operation: a key-holding node dialled in.
HOLDER_ONLINE = "nodevault.holder_online"
#: To the org's running operation: a grant reached `delivered` / `retired`.
GRANT_DELIVERED = "nodevault.grant_delivered"
GRANT_RETIRED = "nodevault.grant_retired"

# ── Timing (module constants so the DBOS-harness tests can shrink them) ──────────────────────────────
#: Every `recv` is a re-check loop: a lost signal costs this much latency, never progress.
RECHECK_SECONDS = 6 * 3600.0
#: An offline node's workflow parks after this long in total; the grant stays put for `ensure_grants`.
PARK_AFTER_SECONDS = 7 * 86400.0
#: Install/retire attempts that FAILED (not offline) before the grant is marked `failed`.
MAX_ATTEMPTS = 8
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_CAP_SECONDS = 1800.0
#: One node call's bound; the node's handlers are in-process crypto, well under it.
NODE_CALL_TIMEOUT = 60.0

#: `command_audit_log.actor` and the exclusion-log caller for every node call made here.
ACTOR = "durable:nodevault"

# The node verbs (`apps/jaahd/nodevault_commands.py`, §4.3).
INSTALL_KEY = "nodevault.install_key"
RETIRE_KEY = "nodevault.retire_key"
WRAP_FOR_NODE = "nodevault.wrap_for_node"
VERIFY = "nodevault.verify"
HELD_KEYS = "nodevault.held_keys"

# Step outcomes — a closed vocabulary; a node failure is `failed:<grant error word>`.
DONE, OFFLINE, TIMEOUT = "done", "offline", "timeout"
DELIVERED, RETIRED, GONE, NO_BLOB = "delivered", "retired", "gone", "no_blob"
READY, NO_HOLDER = "ready", "no_holder"
FAILED_PREFIX = "failed:"

# Workflow results (what `get_result` answers; tests and the console's diagnostics read them).
ENDED, PARKED, FAILED = "ended", "parked", "failed"

#: The node refused the BLOB itself (not the call): the next attempt rebuilds one instead of resending it.
_BLOB_REFUSED_ERRORS = ("hash_mismatch", "wrong_recipient", "missing_wrapping_key", "open_failed", "malformed")
# Node words outside the grant vocabulary, folded onto the nearest honest one (`verify`'s `wrong_key` is
# the only one an install can plausibly answer).
_NODE_WORD_ALIASES = {"wrong_key": "open_failed", "missing_key": "missing_wrapping_key"}


def grant_error(word: Any) -> str:
    """The `nodevault_grants.last_error` word for a node's (or the bus's) refusal word — always one of
    `NODEVAULT_GRANT_ERRORS`, so the CHECK constraint can never refuse the record of a failure."""
    word = _NODE_WORD_ALIASES.get(word, word) if isinstance(word, str) else None
    return word if word in NODEVAULT_GRANT_ERRORS else "malformed"


def backoff_seconds(failures: int) -> float:
    """The durable sleep before attempt `failures + 1`: exponential from `BACKOFF_BASE_SECONDS`, capped."""
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, failures - 1)))


# ── Sessions ─────────────────────────────────────────────────────────────────────────────────────────


@contextmanager
def _node_session(node_id: str | UUID) -> Iterator[tuple[Session, UUID]]:
    """A fresh session bound to the node's OWN org, derived from its row (`seam.bind_from`), and that org.
    Raises `seam.TenancyRowMissing` when the node is gone. The one door every step's DB work goes through:
    a DBOS worker thread has nothing bound, and the node is the subject of every grant workflow."""
    with SessionLocal() as session, seam.bind_from(session, Node, UUID(str(node_id))) as org_id:
        yield session, org_id


@contextmanager
def _org_session(org_id: UUID) -> Iterator[Session]:
    """A fresh session bound to `org_id`, an org a step already derived from a node row (`org_of`, the
    grant's own org) — for work whose node may be gone: the operation signal and the abandoned-key cleanup."""
    with SessionLocal() as session, seam.bound_to(org_id):
        yield session


# ── The node call ────────────────────────────────────────────────────────────────────────────────────


def _call_node(node_id: str, command_type: str, args: dict[str, Any]) -> tuple[str, Optional[dict[str, Any]]]:
    """Send one NodeVault verb to `node_id`; `(outcome, result)` where outcome is `done`, `offline`, `timeout`
    or `failed:<word>`. Called from inside a step only.

    The `commands.dispatch_and_audit_sync` split, run on the serving loop: the bus key is resolved and the
    audit row written on THIS thread in their own sessions, and only `_dispatch` crosses to the loop
    (`runtime._CaptureServingLoop` says why the loop). The audit copy is redacted
    (`commands._credential_safe_args` / `_credential_safe_result`); the live reply stays in this frame."""
    from app.durable import accounts
    from app.node_identity import bus_key
    from app.routes.commands import (
        _audit_after_dispatch,
        _credential_safe_args,
        _credential_safe_result,
        _dispatch,
    )

    with exclusion_log.caller(f"{ACTOR}.{command_type}"):
        with _node_session(UUID(node_id)) as (session, _org):
            bus_target = bus_key(session, node_id)
        loop = accounts._serving_loop
        if loop is None:
            raise RuntimeError(
                "durable nodevault: the serving loop was never captured, so the node bus is unreachable from "
                "a workflow step; app.durable.runtime.init must run before any workflow starts"
            )
        future = asyncio.run_coroutine_threadsafe(
            _dispatch(node_id, command_type, args, bus_target=bus_target, timeout=NODE_CALL_TIMEOUT), loop
        )
        result: Optional[dict[str, Any]]
        try:
            reply = future.result(timeout=NODE_CALL_TIMEOUT + 10)
        except HTTPException as exc:
            outcome = {503: OFFLINE, 504: TIMEOUT}.get(exc.status_code, FAILED_PREFIX + "malformed")
            status, result, audit_result = "dispatched", None, {"reason": exc.detail}
        except concurrent.futures.TimeoutError:
            future.cancel()
            outcome, status, result, audit_result = TIMEOUT, "dispatched", None, {"reason": "no reply"}
        else:
            status = str(reply.get("status") or "failed")
            result = reply.get("result") if isinstance(reply.get("result"), dict) else None
            if status == "done":
                outcome = DONE
            elif status == "rejected":
                outcome = FAILED_PREFIX + "rejected"
            else:
                outcome = FAILED_PREFIX + grant_error((result or {}).get("error"))
            audit_result = _credential_safe_result(command_type, result)
        with _node_session(UUID(node_id)) as (session, _org):
            _audit_after_dispatch(
                session, node_id, command_type, _credential_safe_args(command_type, args), status,
                audit_result, ACTOR,
            )
    return outcome, result


def _held(result: Optional[dict[str, Any]]) -> Optional[list[str]]:
    held = (result or {}).get("held")
    if not isinstance(held, list) or not all(isinstance(h, str) for h in held):
        return None
    return held


# ── Signals ──────────────────────────────────────────────────────────────────────────────────────────


def send_to_running(workflow_ids: Iterable[Optional[str]], topic: str, message: Any = None) -> int:
    """`DBOS.send` `message` on `topic` to each of `workflow_ids` that is still PENDING; how many were sent.
    Never raises: a signal is an optimisation over a re-check loop, so a lost one costs latency only."""
    ids = sorted({wid for wid in workflow_ids if wid})
    if not ids:
        return 0
    try:
        running = [w.workflow_id for w in DBOS.list_workflows(workflow_ids=ids, status=["PENDING"], load_input=False,
                                                              load_output=False)]
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("nodevault: could not list workflows to signal %s (%s)", topic, type(exc).__name__)
        return 0
    sent = 0
    for wid in running:
        try:
            DBOS.send(wid, message, topic=topic)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — finished between the list and the send
            log.info("nodevault: %s not delivered to %s (%s)", topic, wid, type(exc).__name__)
    return sent


def _running_operation_workflow(session: Session, org_id: UUID) -> Optional[str]:
    return session.scalar(
        select(NodeVaultOperation.workflow_id).where(
            NodeVaultOperation.org_id == org_id, NodeVaultOperation.state == NV_OP_RUNNING
        )
    )


# ── Grant reads and the reconcile ────────────────────────────────────────────────────────────────────


def _grant(session: Session, node_id: UUID, key_hash: str, *, lock: bool = False) -> Optional[NodeVaultGrant]:
    stmt = select(NodeVaultGrant).where(NodeVaultGrant.node_id == node_id, NodeVaultGrant.key_hash == key_hash)
    return session.scalar(stmt.with_for_update() if lock else stmt)


def _live_key(session: Session, org_id: UUID, key_hash: str) -> Optional[NodeVaultKey]:
    """The key if a node may be given it now: `current`/`previous`, or `pending` only while the setup or
    rotation that minted it is running (one left behind by a failed operation is never delivered)."""
    key = _key(session, org_id, key_hash)
    if key is None or key.state not in NODEVAULT_LIVE_KEY_STATES:
        return None
    if key.state == NV_KEY_PENDING and not session.scalar(select(exists().where(
        NodeVaultOperation.org_id == org_id, NodeVaultOperation.key_hash == key_hash,
        NodeVaultOperation.kind.in_(("setup", "rotation")), NodeVaultOperation.state == NV_OP_RUNNING,
    ))):
        return None
    return key


def restart_state(session: Session, org_id: UUID, key_hash: str) -> str:
    """The state a failed grant goes back to (an admin's Retry, a node's dial after a transient failure): `waiting`
    for a key a node may still be given, else `retire_pending` — so a failed RETIREMENT is never turned into a
    delivery. The one rule both callers share."""
    return NV_GRANT_WAITING if _live_key(session, org_id, key_hash) is not None else NV_GRANT_RETIRE_PENDING


def _key(session: Session, org_id: UUID, key_hash: str) -> Optional[NodeVaultKey]:
    return session.scalar(
        select(NodeVaultKey).where(NodeVaultKey.org_id == org_id, NodeVaultKey.key_hash == key_hash)
    )


def _audit_delivered(session: Session, org_id: UUID, node: Node, key_hash: str) -> None:
    """AC-9.1: "key delivered to a node (node, hash)" — ids and the public hash only."""
    user_activity.record_write(
        session, None, "nodevault.key_delivered",
        f"NodeVault key {key_hash[:12]} delivered to node {node.name}",
        data={"node_id": str(node.id), "key_hash": key_hash}, org_id=org_id,
    )


def _reconcile(session: Session, node: Node, held: list[str]) -> list[tuple[str, str]]:
    """Bring every grant of `node` in line with the keys it reports holding; the `(topic, key_hash)`
    signals owed to the org's running operation. In the caller's transaction.

    Only FORWARD moves: waiting + held → delivered (audited); retire_pending + not held → retired. Never
    delivered → waiting: replies from one node are reconciled on separate threads and may commit out of order,
    so an older `held` must not undo a delivery a newer one recorded. Failed and retired grants are left to
    their owners, and nothing here starts a workflow (DBOS refuses a start from inside a step)."""
    held_set = set(held)
    owed: list[tuple[str, str]] = []
    grants = list(session.scalars(
        select(NodeVaultGrant).where(NodeVaultGrant.node_id == node.id).with_for_update()
    ))
    for grant in grants:
        if grant.state == NV_GRANT_WAITING and grant.key_hash in held_set:
            grant.state, grant.last_error, grant.delivered_at = NV_GRANT_DELIVERED, None, func.now()
            _audit_delivered(session, node.org_id, node, grant.key_hash)
            owed.append((GRANT_DELIVERED, grant.key_hash))
        elif grant.state == NV_GRANT_RETIRE_PENDING and grant.key_hash not in held_set:
            grant.state, grant.last_error = NV_GRANT_RETIRED, None
            owed.append((GRANT_RETIRED, grant.key_hash))
    session.flush()
    return owed


def _signal_operation(org_id: UUID, owed: list[tuple[str, str]], node_id: str) -> None:
    if not owed:
        return
    with _org_session(org_id) as session:
        op_wid = _running_operation_workflow(session, org_id)
    for topic, key_hash in owed:
        send_to_running([op_wid], topic, {"node_id": node_id, "key_hash": key_hash})


def _apply_held(node_id: str, held: list[str]) -> Optional[UUID]:
    """`_reconcile` in its own committed transaction, then the signals it owes; the node's org, or None when
    the node is gone. A node that just became a holder wakes the deliveries waiting for one on that key — a
    delivery is a holder coming online as much as a dial is."""
    try:
        with _node_session(UUID(node_id)) as (session, org_id):
            node = session.scalar(select(Node).where(Node.id == UUID(node_id)))
            if node is None:
                return None
            owed = _reconcile(session, node, held)
            session.commit()
            now_held = sorted({key_hash for topic, key_hash in owed if topic == GRANT_DELIVERED})
            # Other nodes' deliveries of the newly held keys (a peer can now wrap them), and this node's own
            # waiting deliveries (one may chain from a key it just received).
            waiting = session.execute(
                select(NodeVaultGrant.node_id, NodeVaultGrant.key_hash, NodeVaultGrant.generation).where(
                    NodeVaultGrant.org_id == org_id, NodeVaultGrant.state == NV_GRANT_WAITING,
                    NodeVaultGrant.key_hash.in_(now_held) | (NodeVaultGrant.node_id == node.id),
                )
            ).all() if now_held else []
    except seam.TenancyRowMissing:
        return None
    _signal_operation(org_id, owed, node_id)
    send_to_running(
        [nodevault_grants.workflow_id(nodevault_grants.DELIVER, n, k, g) for n, k, g in waiting], HOLDER_ONLINE,
    )
    return org_id


# ── Steps ────────────────────────────────────────────────────────────────────────────────────────────


@DBOS.step()
def now() -> float:
    """Wall-clock seconds, recorded so a replayed workflow measures its park deadline from the same start."""
    return time.time()


@DBOS.step()
def org_of(node_id: str) -> Optional[str]:
    """The node's org (recorded at the workflow's start, so a node deleted later still names it)."""
    try:
        with _node_session(UUID(node_id)) as (session, org_id):
            return str(org_id)
    except seam.TenancyRowMissing:
        return None


def chain_predecessor(chain_blob: Optional[dict[str, Any]]) -> Optional[str]:
    """The key hash a rotation chain blob is wrapped under (`k = "nv:<current hash>"`), or None."""
    from app.credential_keys import nodevault_key_hash_of

    key_id = (chain_blob or {}).get("k")
    return nodevault_key_hash_of(key_id) if isinstance(key_id, str) else None


@DBOS.step(**runtime.DB_RETRY)
def produce_blob(node_id: str, key_hash: str, generation: int) -> dict[str, Any]:
    """Step 0: make sure the grant carries a blob. `{"state": ready|no_holder|gone, "kind": <blob kind>}`.

    A `chain` blob when the key has one and this node already holds its predecessor; else a `peer` blob from
    the first online holder that answers. The blob is written to the grant here and never returned."""
    node_uuid = UUID(node_id)
    try:
        with _node_session(node_uuid) as (session, org_id):
            grant = _grant(session, node_uuid, key_hash, lock=True)
            if grant is None or grant.generation != generation:
                return {"state": GONE}
            if grant.state == NV_GRANT_DELIVERED or (grant.state == NV_GRANT_WAITING and grant.blob is not None):
                return {"state": READY, "kind": grant.blob_kind}
            if grant.state != NV_GRANT_WAITING:
                return {"state": GONE}
            key = _live_key(session, org_id, key_hash)
            if key is None:
                return {"state": GONE}
            predecessor = chain_predecessor(key.chain_blob)
            if predecessor is not None:
                pred_grant = _grant(session, node_uuid, predecessor)
                chain = key.chain_blob if pred_grant is not None and pred_grant.state == NV_GRANT_DELIVERED else None
            else:
                chain = None
            node = session.scalar(select(Node).where(Node.id == node_uuid))
            target_key = node.nodevault_identity_key if node is not None else None
            if not target_key:
                return {"state": GONE}
            online = set(node_presence.connected_node_ids(session))
            holders = [
                str(holder) for holder in session.scalars(
                    select(NodeVaultGrant.node_id)
                    .where(
                        NodeVaultGrant.org_id == org_id,
                        NodeVaultGrant.key_hash == key_hash,
                        NodeVaultGrant.state == NV_GRANT_DELIVERED,
                        NodeVaultGrant.node_id != node_uuid,
                    )
                    # Oldest holder first: a node that only just claimed the key is the last one asked.
                    .order_by(NodeVaultGrant.delivered_at.asc().nulls_last())
                )
                if str(holder) in online
            ]
            session.rollback()   # no connection held across the node calls below
    except seam.TenancyRowMissing:
        return {"state": GONE}

    if chain is not None:
        kind = _store_blob(node_uuid, key_hash, generation, chain, "chain", None)
        return {"state": GONE} if kind is None else {"state": READY, "kind": kind}
    request = {"org_id": str(org_id), "target_node_id": node_id, "target_identity_key": target_key,
               "key_hashes": [key_hash]}
    for holder in holders:
        outcome, result = _call_node(holder, WRAP_FOR_NODE, request)
        if outcome != DONE:
            continue
        blob = ((result or {}).get("blobs") or {}).get(key_hash)
        if not isinstance(blob, dict):
            # The CP thought this node held the key and it does not: let its own delivery put it back.
            _demote_holder(holder, key_hash)
            continue
        kind = _store_blob(node_uuid, key_hash, generation, blob, "peer", target_key)
        return {"state": GONE} if kind is None else {"state": READY, "kind": kind}
    if node_id in online:
        # Last resort before waiting: the node itself may still hold the key — a retirement abandoned before it
        # reached this node puts every grant back to `waiting`, holders included. Its own `held` settles it.
        outcome, result = _call_node(node_id, HELD_KEYS, {})
        held = _held(result) if outcome == DONE else None
        if held is not None and key_hash in held and _apply_held(node_id, held) is not None:
            return {"state": READY, "kind": None}
    return {"state": NO_HOLDER}


class BlobWriteFailed(RuntimeError):
    """A grant's blob could not be written. Carries only the database error's TYPE: a SQLAlchemy error keeps its
    bound parameters — the blob — and DBOS pickles a failed step's exceptions into its system tables."""


def _store_blob(
    node_uuid: UUID, key_hash: str, generation: int, blob: dict[str, Any], kind: str, recipient_key: Optional[str],
) -> Optional[str]:
    """Write `blob` onto the grant unless it was superseded; the kind the grant now carries, or None when it was
    (a new generation, a settled grant, a gone node). The recipient is re-checked UNDER THE LOCK: an identity
    reset between the step's read and this write must not leave the key wrapped to the identity it revoked."""
    try:
        with _node_session(node_uuid) as (session, _org):
            node = session.scalar(select(Node).where(Node.id == node_uuid).with_for_update())
            grant = _grant(session, node_uuid, key_hash, lock=True)
            if (node is None or grant is None or grant.generation != generation or grant.state != NV_GRANT_WAITING
                    or (recipient_key is not None and node.nodevault_identity_key != recipient_key)):
                return None
            if grant.blob is None:
                grant.blob, grant.blob_kind = blob, kind
                session.commit()
            return grant.blob_kind
    except seam.TenancyRowMissing:
        return None
    except SQLAlchemyError as exc:
        failure = type(exc).__name__
    raise BlobWriteFailed(failure)   # outside the `except`: no `__context__` carrying the parameters


def _demote_holder(holder: str, key_hash: str) -> None:
    """The CP thought `holder` held the key and its `wrap_for_node` says it does not: its grant goes back to
    `waiting`, and its delivery is restarted from a thread of its own — DBOS refuses a workflow start from
    inside a step, and losing this restart costs only latency (the holder's next dial re-enrols it)."""
    try:
        with _node_session(UUID(holder)) as (session, _org):
            grant = _grant(session, UUID(holder), key_hash, lock=True)
            if grant is None or grant.state != NV_GRANT_DELIVERED:
                return
            grant.state, grant.delivered_at = NV_GRANT_WAITING, None
            session.commit()
    except seam.TenancyRowMissing:
        return
    threading.Thread(target=_ensure_grants_detached, args=(holder,), name="nodevault-restart", daemon=True).start()


def _ensure_grants_detached(node_id: str) -> None:
    """`ensure_grants(node)` committed in its own session, outside any DBOS context. Never raises."""
    try:
        with _node_session(UUID(node_id)) as (session, _org):
            nodevault_grants.ensure_grants(session, UUID(node_id))
            session.commit()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("nodevault: re-enrolment of node %s failed (%s)", node_id, type(exc).__name__)


def _nik_key_id(session: Session, node_uuid: UUID) -> Optional[str]:
    """`nik:<fingerprint>` of the node's CURRENT identity key — the wrapping key id a node or peer blob must name."""
    fingerprint = session.scalar(select(Node.nodevault_identity_fp).where(Node.id == node_uuid))
    return f"nik:{fingerprint}" if fingerprint else None


@DBOS.step(**runtime.DB_RETRY)
def install(node_id: str, key_hash: str, generation: int) -> str:
    """Send the grant's blob with `nodevault.install_key`; the outcome word (`delivered`, `offline`, `timeout`,
    `failed:<word>`, `no_blob`, `gone`). A delivery is recorded by the reconcile of the node's `held`."""
    node_uuid = UUID(node_id)
    try:
        with _node_session(node_uuid) as (session, org_id):
            grant = _grant(session, node_uuid, key_hash, lock=True)
            if grant is None or grant.generation != generation:
                return GONE
            already = grant.state == NV_GRANT_DELIVERED
            if not already and grant.state != NV_GRANT_WAITING:
                return GONE
            if not already and grant.blob is None:
                return NO_BLOB
            if not already and grant.blob_kind != "chain" and grant.blob.get("k") != _nik_key_id(session, node_uuid):
                # Wrapped to an identity this node no longer has (reset since): never sent, rebuilt instead.
                grant.blob, grant.blob_kind = None, None
                session.commit()
                return NO_BLOB
            key = _live_key(session, org_id, key_hash)
            if key is None:
                return GONE
            args = {"org_id": str(org_id), "key_hash": key_hash, "public_key": key.public_key,
                    "blob": grant.blob, "blob_kind": grant.blob_kind}
            session.rollback()
    except seam.TenancyRowMissing:
        return GONE
    if already:
        # A replay after the delivery committed: the operation may not have heard, so say it again.
        _signal_operation(org_id, [(GRANT_DELIVERED, key_hash)], node_id)
        return DELIVERED
    outcome, result = _call_node(node_id, INSTALL_KEY, args)
    del args
    if outcome != DONE:
        return outcome
    held = _held(result)
    if held is None:
        return FAILED_PREFIX + "malformed"
    if _apply_held(node_id, held) is None:
        return GONE
    return DELIVERED if key_hash in held else FAILED_PREFIX + "malformed"


@DBOS.step(**runtime.DB_RETRY)
def retire(node_id: str, key_hash: str, generation: int, org_id: Optional[str]) -> str:
    """Take the key off the node with `nodevault.retire_key`; `retired`, `offline`, `timeout`,
    `failed:<word>` or `gone`. A deleted node counts as retired."""
    node_uuid = UUID(node_id)
    try:
        with _node_session(node_uuid) as (session, _org):
            grant = _grant(session, node_uuid, key_hash)
            if grant is None or grant.generation != generation:
                return GONE
            if grant.state == NV_GRANT_RETIRED:
                if org_id:
                    _signal_operation(UUID(org_id), [(GRANT_RETIRED, key_hash)], node_id)
                return RETIRED
            if grant.state != NV_GRANT_RETIRE_PENDING:
                return GONE
            session.rollback()
    except seam.TenancyRowMissing:
        if org_id:
            _signal_operation(UUID(org_id), [(GRANT_RETIRED, key_hash)], node_id)
        return RETIRED
    outcome, result = _call_node(node_id, RETIRE_KEY, {"key_hash": key_hash})
    if outcome != DONE:
        return outcome
    held = _held(result)
    if held is None:
        return FAILED_PREFIX + "malformed"
    if _apply_held(node_id, held) is None:
        return RETIRED
    return RETIRED if key_hash not in held else FAILED_PREFIX + "malformed"


@DBOS.step(**runtime.DB_RETRY)
def record_error(node_id: str, key_hash: str, generation: int, error: str, final: bool) -> None:
    """Note `error` on the grant (a waiting grant stays waiting); `final` marks it `failed`. A permanent
    failure of a delivery drops its blob, so the next attempt rebuilds one instead of resending what was refused."""
    node_uuid = UUID(node_id)
    try:
        with _node_session(node_uuid) as (session, _org):
            grant = _grant(session, node_uuid, key_hash, lock=True)
            if (grant is None or grant.generation != generation
                    or grant.state not in (NV_GRANT_WAITING, NV_GRANT_RETIRE_PENDING)):
                return
            grant.last_error = grant_error(error)
            if grant.state == NV_GRANT_WAITING and grant.last_error in _BLOB_REFUSED_ERRORS:
                # The node refused THIS blob: the next attempt (or a Retry) builds a fresh one, possibly from
                # another holder, instead of resending what was refused.
                grant.blob, grant.blob_kind = None, None
            if final:
                grant.state = NV_GRANT_FAILED
            session.commit()
    except seam.TenancyRowMissing:
        return


@DBOS.step(**runtime.DB_RETRY)
def cleanup_abandoned_key(org_id: str, key_hash: str) -> bool:
    """Delete an `abandoned` key's key row and all its grants once none is un-retired; True when THIS call
    deleted them. `SELECT … FOR UPDATE` on the key row first, so concurrent finishers serialize and exactly
    one deletes (the others find the row gone). A normally retired key keeps its rows for audit."""
    org = UUID(org_id)
    with _org_session(org) as session:
        return delete_abandoned_key_if_clear(session, org, key_hash, commit=True)


def retire_key_row(session: Session, key: NodeVaultKey) -> None:
    """Mark `key` retired, in the caller's transaction (a finished retire, or Start over). The rotation that
    followed it left the next key's private half chain-wrapped under it: no node can use that blob any more, and
    a copy of a retired key must not keep unlocking the next one, so those blobs go too."""
    key.state, key.retired_at = NV_KEY_RETIRED, func.now()
    chained = list(session.scalars(select(NodeVaultKey).where(
        NodeVaultKey.org_id == key.org_id, NodeVaultKey.chain_blob["k"].astext == f"nv:{key.key_hash}",
    ).with_for_update()))
    for successor in chained:
        successor.chain_blob = null()   # SQL NULL, not a JSON `null`
    if chained:
        session.execute(update(NodeVaultGrant).where(
            NodeVaultGrant.org_id == key.org_id, NodeVaultGrant.blob_kind == "chain",
            NodeVaultGrant.key_hash.in_([k.key_hash for k in chained]),
        ).values(blob=null()))


def delete_abandoned_key_if_clear(session: Session, org_id: UUID, key_hash: str, *, commit: bool) -> bool:
    """The abandoned-key check (also run by reset-identity, in its own transaction): see
    `cleanup_abandoned_key`. `commit=False` leaves the delete in the caller's transaction."""
    key = session.scalar(
        select(NodeVaultKey)
        .where(NodeVaultKey.org_id == org_id, NodeVaultKey.key_hash == key_hash)
        .with_for_update()
    )
    if key is None or key.state != NV_KEY_ABANDONED:
        return False
    unretired = session.scalar(select(exists().where(
        NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == key_hash,
        NodeVaultGrant.state != NV_GRANT_RETIRED,
    )))
    if unretired:
        return False
    session.execute(delete(NodeVaultGrant).where(NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == key_hash))
    session.delete(key)
    if commit:
        session.commit()
    else:
        session.flush()
    log.info("nodevault: abandoned key %s removed from org %s", key_hash[:12], org_id)
    return True


# ── Workflows ────────────────────────────────────────────────────────────────────────────────────────


def _wait(topic: str, deadline: Optional[float]) -> bool:
    """One re-check wait on `topic`; False when the park deadline has passed (nothing was waited)."""
    timeout = RECHECK_SECONDS
    if deadline is not None:
        remaining = deadline - now()
        if remaining <= 0:
            return False
        timeout = min(timeout, remaining)
    DBOS.recv(topic, timeout_seconds=timeout)
    return True


@nodevault_grants.register(nodevault_grants.DELIVER)
@DBOS.workflow()
def deliver_key(node_id: str, key_hash: str, generation: int) -> str:
    """`nodevault-deliver:{node}:{key_hash}:{gen}` (module docstring)."""
    deadline = now() + PARK_AFTER_SECONDS
    failures = 0
    while True:
        blob = produce_blob(node_id, key_hash, generation)
        if blob["state"] == GONE:
            return ENDED
        if blob["state"] == NO_HOLDER:
            # Never parks: a holder dialling in is not this node's contact, so nothing else would restart it.
            _wait(HOLDER_ONLINE, None)
            continue
        outcome = install(node_id, key_hash, generation)
        if outcome == DELIVERED:
            return DELIVERED
        if outcome == GONE:
            return ENDED
        if outcome == NO_BLOB:
            continue
        if outcome == OFFLINE:
            record_error(node_id, key_hash, generation, OFFLINE, False)
            if not _wait(NODE_ONLINE, deadline):
                return PARKED
            continue
        failures += 1
        word = outcome[len(FAILED_PREFIX):] if outcome.startswith(FAILED_PREFIX) else outcome
        if failures >= MAX_ATTEMPTS:
            record_error(node_id, key_hash, generation, word, True)
            return FAILED
        record_error(node_id, key_hash, generation, word, False)
        DBOS.sleep(backoff_seconds(failures))


@nodevault_grants.register(nodevault_grants.RETIRE_NODE)
@DBOS.workflow()
def retire_node_key(node_id: str, key_hash: str, generation: int) -> str:
    """`nodevault-retire-node:{node}:{key_hash}:{gen}` (module docstring)."""
    org_id = org_of(node_id)
    deadline = now() + PARK_AFTER_SECONDS
    failures = 0
    while True:
        outcome = retire(node_id, key_hash, generation, org_id)
        if outcome in (RETIRED, GONE):
            break
        if outcome == OFFLINE:
            record_error(node_id, key_hash, generation, OFFLINE, False)
            if not _wait(NODE_ONLINE, deadline):
                return PARKED
            continue
        failures += 1
        word = outcome[len(FAILED_PREFIX):] if outcome.startswith(FAILED_PREFIX) else outcome
        if failures >= MAX_ATTEMPTS:
            record_error(node_id, key_hash, generation, word, True)
            return FAILED
        record_error(node_id, key_hash, generation, word, False)
        DBOS.sleep(backoff_seconds(failures))
    if org_id is not None:
        cleanup_abandoned_key(org_id, key_hash)
    return RETIRED if outcome == RETIRED else ENDED


# ── Reopening a failed grant ─────────────────────────────────────────────────────────────────────────


def reopen_failed_grant(session: Session, grant: NodeVaultGrant) -> str:
    """Put a FAILED grant back in play, in the caller's transaction: `restart_state` decides waiting or
    retire_pending, and the generation moves on so the next start is a fresh workflow id — never mistaken for
    the run that just failed it, even while that run is still returning. The caller runs `ensure_grants`."""
    grant.state = restart_state(session, grant.org_id, grant.key_hash)
    grant.last_error = None
    grant.generation = int(grant.generation) + 1
    session.flush()
    return grant.state


# ── The node's contact (`routes/node_bus.py`'s hello) ───────────────────────────────────────────────


def on_node_online(wire_node_id: str) -> None:
    """A node dialled in: reset its transient-failed grants, `ensure_grants(node)`, then signal NODE_ONLINE to
    its own running grant workflows and — when it holds a key — HOLDER_ONLINE to every running grant workflow
    of its org and the org's running operation. Allowed whatever the plan (D11/AC-1.3). Never raises."""
    node_uuid = optional_uuid(wire_node_id)
    if node_uuid is None:
        return   # an unregistered node keyed by a name holds no grant
    try:
        # Detached first: the caller's own scope (none on the node bus; a test harness's operator plane) must
        # not decide which org this unit of work binds — the node's row does. Entitlement (`seam.detached`):
        # `wire_node_id` is the machine key of the socket the node's OWN credential opened
        # (`routes/node_bus.py::_handshake`), so the org bound is the caller's own.
        with exclusion_log.caller(f"{ACTOR}.on_node_online"), seam.detached():
            try:
                with _node_session(node_uuid) as (session, org_id):
                    # The node row first, as `ensure_grants` and the identity POST lock it: one lock order.
                    session.scalar(select(Node.id).where(Node.id == node_uuid).with_for_update())
                    for grant in list(session.scalars(select(NodeVaultGrant).where(
                        NodeVaultGrant.node_id == node_uuid, NodeVaultGrant.state == NV_GRANT_FAILED,
                        NodeVaultGrant.last_error.in_(NODEVAULT_TRANSIENT_GRANT_ERRORS),
                    ).with_for_update())):
                        reopen_failed_grant(session, grant)
                    nodevault_grants.ensure_grants(session, node_uuid)
                    session.commit()
                    open_grants = session.execute(
                        select(NodeVaultGrant.node_id, NodeVaultGrant.key_hash, NodeVaultGrant.generation,
                               NodeVaultGrant.state)
                        .where(NodeVaultGrant.org_id == org_id,
                               NodeVaultGrant.state.in_((NV_GRANT_WAITING, NV_GRANT_RETIRE_PENDING,
                                                         NV_GRANT_DELIVERED)))
                    ).all()
                    op_wid = _running_operation_workflow(session, org_id)
                    retry_prefixes = _sealed_item_retry_prefixes(session, org_id)
            except seam.TenancyRowMissing:
                return
    except Exception as exc:  # noqa: BLE001 — a node's dial must never fail on NodeVault bookkeeping
        log.warning("nodevault: node-online handling for %s failed (%s)", wire_node_id, type(exc).__name__,
                    exc_info=True)
        return
    own: list[str] = []
    others: list[str] = []
    holds = False
    for grant_node, key_hash, generation, state in open_grants:
        if state == NV_GRANT_DELIVERED:
            holds = holds or grant_node == node_uuid
            continue
        kind = nodevault_grants.DELIVER if state == NV_GRANT_WAITING else nodevault_grants.RETIRE_NODE
        wid = nodevault_grants.workflow_id(kind, grant_node, key_hash, generation)
        (own if grant_node == node_uuid else others).append(wid)
    send_to_running(own, NODE_ONLINE)
    if holds:
        send_to_running([*others, op_wid, *_running_with_prefixes(retry_prefixes)], HOLDER_ONLINE)


def _sealed_item_retry_prefixes(session: Session, org_id: UUID) -> list[str]:
    """The item-Retry workflow prefixes of the org's operations with an item waiting on a verify (`sealed`)."""
    from app.models.credentials import NodeVaultOperationItem

    ops = session.scalars(select(NodeVaultOperationItem.operation_id).where(
        NodeVaultOperationItem.org_id == org_id, NodeVaultOperationItem.state == "sealed",
    ).distinct())
    return [f"nodevault-migrate-item:{op}:" for op in ops]


def _running_with_prefixes(prefixes: list[str]) -> list[str]:
    if not prefixes:
        return []
    try:
        return [w.workflow_id for w in DBOS.list_workflows(
            workflow_id_prefix=prefixes, status=["PENDING"], load_input=False, load_output=False)]
    except Exception as exc:  # noqa: BLE001 — a signal is an optimisation over a re-check loop
        log.warning("nodevault: could not list item retries to wake (%s)", type(exc).__name__)
        return []


# ══ Operations: setup and migration ══════════════════════════════════════════════════════════════════
#
# `nodevault-setup:{op_id}` — `distribute` (the route has already stored the `pending` key, the operation and
# one `node` grant per capable node carrying the browser's blob, and started each grant's delivery): wait on
# GRANT_DELIVERED in a re-check loop, restarting the key's parked deliveries on every timeout, until a NODE has
# acknowledged the key (`delivered` — never on a send); then promote the key to `current`. `migrate_choice`:
# wait on MIGRATE_CHOICE in a re-check loop that never times out terminally; the route records the choice as
# item rows and moves the step on, so the signal is only a wake-up. `migrating`: the per-credential phase
# below. `result`: the counts, the op `done`, `nodevault.setup_completed`.
# `nodevault-migrate:{op_id}` — the same `migrating` and `result` for KMS credentials left after setup.
# `nodevault-migrate-item:{op_id}:{credential_id}:{n}` — one failed item's Retry.
#
# One item = (1) `seal_item`: KMS open + NodeVault seal inside `credential_crypto.reseal_secret_to_nodevault`,
# ciphertext staged on the item; (2) `verify_item`: a node holding the key trial-opens the staged envelopes
# (`nodevault.verify`), waiting HOLDER_ONLINE while none is online; (3) `swap_item`: one transaction replaces the
# tier-2 envelopes, sets `nodevault`, nulls `staged` and audits `nodevault.credential_migrated`. The KMS
# envelope goes ONLY after a node proved the NodeVault one opens (a CP-side shape check cannot see an AAD or
# destination mismatch). An item that fails is `failed` with a closed-vocabulary reason and never stops the
# others (AC-7.3). Steps take and return ids and words: the plaintext exists only inside step (1)'s reseal call,
# and a step that fails raises an exception carrying a type name only (`ItemStepFailed`).

#: To the org's running setup operation: the admin recorded what to migrate (`POST /api/nodevault/migrations`).
MIGRATE_CHOICE = "nodevault.migrate_choice"
#: An operation's own waits (`distribute`, `migrate_choice`) re-check this often.
OP_RECHECK_SECONDS = 24 * 3600.0
#: How long a step waits for the parked deliveries it restarts to be re-enrolled.
_RESTART_JOIN_SECONDS = 60.0

SETUP, MIGRATION, ROTATION, RETIRE = "setup", "migration", "rotation", "retire"
STEP_DISTRIBUTE, STEP_MIGRATE_CHOICE, STEP_MIGRATING, STEP_RESULT = "distribute", "migrate_choice", "migrating", "result"
STEP_PROMOTE, STEP_REKEY, STEP_RETIRING = "promote", "rekey", "retiring"
OP_DONE, OP_FAILED = "done", "failed"

ITEM_PENDING, ITEM_SEALED, ITEM_DONE, ITEM_SKIPPED, ITEM_FAILED = "pending", "sealed", "done", "skipped", "failed"
#: `nodevault_operation_items.reason` — why an item was skipped (it can't be NodeVault) …
SKIP_REASONS = ("already_nodevault", "oauth2", "signing_key", "in_use", "no_secret")
#: … or failed (each has a Retry). `corrupt`: its KMS envelope would not open; `verify_failed`: a node could not
#: open the NodeVault envelope, so the KMS one was kept; `changed`: edited between seal and swap; `unavailable`:
#: a temporary failure (KMS, the database, the key no longer sealable).
FAIL_REASONS = ("corrupt", "verify_failed", "changed", "unavailable")
OK, SEALED = "ok", "sealed"

_OPERATION_PREFIX = {
    SETUP: "nodevault-setup", MIGRATION: "nodevault-migrate", ROTATION: "nodevault-rotate", RETIRE: "nodevault-retire",
}
#: An operation's completion audit event and how its summary names it.
_COMPLETED = {
    SETUP: ("nodevault.setup_completed", "NodeVault setup"),
    MIGRATION: ("nodevault.migration_completed", "NodeVault migration"),
    ROTATION: ("nodevault.rotation_completed", "NodeVault rotation"),
}
ITEM_RETRY_PREFIX = "nodevault-migrate-item"


class ItemStepFailed(RuntimeError):
    """A migration step failed for a reason that may pass (KMS, the database). Carries the error's TYPE only:
    a database error keeps its bound parameters (staged ciphertext), a decode error the bytes it could not
    decode, and DBOS pickles a failed step's exception into its system tables."""


def operation_workflow_id(kind: str, op_id: UUID | str) -> str:
    """`nodevault-setup:{op}` / `nodevault-migrate:{op}` — the one spelling of an operation's workflow id."""
    return f"{_OPERATION_PREFIX[kind]}:{op_id}"


def item_retry_workflow_id(op_id: UUID | str, credential_id: UUID | str, n: int) -> str:
    return f"{ITEM_RETRY_PREFIX}:{op_id}:{credential_id}:{int(n)}"


@contextmanager
def _op_session(op_id: str | UUID) -> Iterator[tuple[Session, UUID]]:
    """A fresh session bound to the operation's own org (derived from its row). Raises `seam.TenancyRowMissing`
    when the operation is gone."""
    with SessionLocal() as session, seam.bind_from(session, NodeVaultOperation, UUID(str(op_id))) as org_id:
        yield session, org_id


def _operation(session: Session, op_id: str | UUID, *, lock: bool = False) -> Optional[NodeVaultOperation]:
    stmt = select(NodeVaultOperation).where(NodeVaultOperation.id == UUID(str(op_id)))
    return session.scalar(stmt.with_for_update() if lock else stmt)


def _item(session: Session, op_id: str | UUID, credential_id: str | UUID, *, lock: bool = False):
    from app.models.credentials import NodeVaultOperationItem

    stmt = select(NodeVaultOperationItem).where(
        NodeVaultOperationItem.operation_id == UUID(str(op_id)),
        NodeVaultOperationItem.credential_id == UUID(str(credential_id)),
        NodeVaultOperationItem.phase == "migrate",
    )
    return session.scalar(stmt.with_for_update() if lock else stmt)


def _audit_operation(session: Session, org_id: UUID, kind: str, summary: str, data: dict[str, Any]) -> None:
    """AC-9.1's operation events (setup completed, credential migrated): ids, hashes and counts only — no
    request carries them, the workflow decides them."""
    user_activity.record_write(session, None, kind, summary, data=data, org_id=org_id)


def ineligibility(session: Session, credential: Any) -> Optional[str]:
    """Why `credential` can't move to NodeVault, as a `SKIP_REASONS` word, or None when it can: not already
    NodeVault, not OAuth2 (D7) nor a type the control plane signs with, opened by no control-plane consumer (D1), and holding a secret to protect."""
    from app import credential_consumers
    from app.credential_crypto import SealedSecret

    if credential.nodevault:
        return "already_nodevault"
    config = credential.config or {}
    type_reason = credential_consumers.oauth2_reason(credential.type, config)
    if type_reason is not None:
        # A type with its own cause (a signing key the control plane verifies with) is not OAuth2.
        return "oauth2" if type_reason == credential_consumers.OAUTH2_REASON else "signing_key"
    if credential_consumers.consumers_of_row(session, credential):
        return "in_use"
    if not any(isinstance(value, SealedSecret) for value in config.values()):
        return "no_secret"
    return None


def _secrets_digest(credential: Any) -> str:
    """A digest of what the staged envelopes were sealed FROM: the stored KMS envelopes (ciphertext), the type
    and the destination their AAD binds. A different digest at swap time means the row was edited since."""
    import hashlib
    import json

    from app.credential_crypto import SealedSecret
    from app.credentials.registry import sealing_destination

    config = credential.config or {}
    material = {
        "type": credential.type,
        "destination": sealing_destination(credential.type, config),
        "secrets": {k: v.envelope for k, v in sorted(config.items()) if isinstance(v, SealedSecret)},
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def item_counts(session: Session, op_id: UUID) -> dict[str, int]:
    """The operation's per-credential counts, read off its item rows (AC-4.8: they match the DB). `total` and
    the first three count the migrate phase; `rekeyed` and `rekey_failed` a rotation's rekey phase."""
    from app.models.credentials import NodeVaultOperationItem

    rows = session.execute(
        select(NodeVaultOperationItem.phase, NodeVaultOperationItem.state, func.count()).where(
            NodeVaultOperationItem.operation_id == op_id,
        ).group_by(NodeVaultOperationItem.phase, NodeVaultOperationItem.state)
    ).all()
    migrate = {state: int(n) for phase, state, n in rows if phase == "migrate"}
    rekey = {state: int(n) for phase, state, n in rows if phase == "rekey"}
    return {
        "migrated": migrate.get(ITEM_DONE, 0),
        "skipped": migrate.get(ITEM_SKIPPED, 0),
        "failed": migrate.get(ITEM_FAILED, 0),
        "total": sum(migrate.values()),
        "rekeyed": rekey.get(ITEM_DONE, 0),
        "rekey_failed": rekey.get(ITEM_FAILED, 0),
    }


# ── Operation steps ──────────────────────────────────────────────────────────────────────────────────


@DBOS.step(**runtime.DB_RETRY)
def distribution_state(op_id: str) -> str:
    """`delivered` once a node has ACKNOWLEDGED the operation's key (a `delivered` grant — never a send),
    `waiting` before, `gone` when the operation is no longer running (abandoned)."""
    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id)
            if op is None or op.state != NV_OP_RUNNING or op.step != STEP_DISTRIBUTE:
                return GONE if op is None or op.state != NV_OP_RUNNING else DELIVERED
            held = session.scalar(select(exists().where(
                NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == op.key_hash,
                NodeVaultGrant.state == NV_GRANT_DELIVERED,
            )))
            return DELIVERED if held else "waiting"
    except seam.TenancyRowMissing:
        return GONE


@DBOS.step(**runtime.DB_RETRY)
def restart_key_deliveries(op_id: str) -> int:
    """Re-enrol every node whose grant on the operation's key is still unsettled — waiting for a delivery, or
    for a retirement when the operation is a retire — so a workflow that parked (an offline node past
    `PARK_AFTER_SECONDS`) or was dropped by a version bump runs again. How many nodes it re-enrolled."""
    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id)
            if op is None or op.state != NV_OP_RUNNING or op.key_hash is None:
                return 0
            unsettled = NV_GRANT_RETIRE_PENDING if op.kind == RETIRE else NV_GRANT_WAITING
            nodes = sorted({str(n) for n in session.scalars(select(NodeVaultGrant.node_id).where(
                NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == op.key_hash,
                NodeVaultGrant.state == unsettled,
            ))})
    except seam.TenancyRowMissing:
        return 0
    return _ensure_grants_for(nodes)


def _ensure_grants_for(nodes: list[str]) -> int:
    """`ensure_grants` for each node, each on a thread of its own (`_ensure_grants_detached`): it starts
    workflows, which DBOS refuses from inside a step. Joined for at most `_RESTART_JOIN_SECONDS`; how many."""
    threads = [threading.Thread(target=_ensure_grants_detached, args=(node,), name="nodevault-restart", daemon=True)
               for node in nodes]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + _RESTART_JOIN_SECONDS
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return len(nodes)


@DBOS.step(**runtime.DB_RETRY)
def promote_key(op_id: str) -> str:
    """The setup's key `pending` → `current` and the step → `migrate_choice`, in one transaction under the
    operation's lock (an abandon takes the same lock and refuses once the step has moved on). `done`, or `gone`
    when the operation was abandoned first."""
    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id, lock=True)
            if op is None or op.state != NV_OP_RUNNING:
                return GONE
            if op.step != STEP_DISTRIBUTE:
                return DONE   # a replay after the promotion committed
            key = session.scalar(select(NodeVaultKey).where(
                NodeVaultKey.org_id == org_id, NodeVaultKey.key_hash == op.key_hash,
            ).with_for_update())
            if key is None or key.state != NV_KEY_PENDING:
                return GONE
            key.state = NV_KEY_CURRENT
            op.step = STEP_MIGRATE_CHOICE
            session.commit()
            return DONE
    except seam.TenancyRowMissing:
        return GONE


@DBOS.step(**runtime.DB_RETRY)
def migrate_choice_state(op_id: str) -> str:
    """`made` once the route recorded the admin's choice (it moves the step on), `pending` before, `gone`
    when the operation is no longer running."""
    try:
        with _op_session(op_id) as (session, _org):
            op = _operation(session, op_id)
    except seam.TenancyRowMissing:
        return GONE
    if op is None or op.state != NV_OP_RUNNING:
        return GONE
    return "pending" if op.step == STEP_MIGRATE_CHOICE else "made"


@DBOS.step(**runtime.DB_RETRY)
def items_to_migrate(op_id: str) -> list[str]:
    """The ids of the operation's items not yet settled (pending or sealed), in a stable order; the step is
    moved to `migrating`. Empty when the operation is no longer running."""
    from app.models.credentials import NodeVaultOperationItem

    try:
        with _op_session(op_id) as (session, _org):
            op = _operation(session, op_id, lock=True)
            if op is None or op.state != NV_OP_RUNNING:
                return []
            if op.step != STEP_MIGRATING:
                op.step = STEP_MIGRATING
            ids = [str(cid) for cid in session.scalars(
                select(NodeVaultOperationItem.credential_id).where(
                    NodeVaultOperationItem.operation_id == op.id, NodeVaultOperationItem.phase == "migrate",
                    NodeVaultOperationItem.state.in_((ITEM_PENDING, ITEM_SEALED)),
                ).order_by(NodeVaultOperationItem.created_at, NodeVaultOperationItem.credential_id)
            )]
            session.commit()
            return ids
    except seam.TenancyRowMissing:
        return []


@DBOS.step(**runtime.DB_RETRY)
def finish_operation(op_id: str) -> str:
    """`result`: the counts written, the operation `done`, the completion audited. A no-op for an operation
    that is no longer running (abandoned), so it never revives one."""
    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id, lock=True)
            if op is None or op.state != NV_OP_RUNNING:
                return GONE
            counts = item_counts(session, op.id)
            op.counts = {**(op.counts or {}), **counts}
            op.step, op.state = STEP_RESULT, OP_DONE
            kind, what = _COMPLETED[op.kind]
            summary = f"{what} finished: {counts['migrated']} migrated, {counts['skipped']} skipped, {counts['failed']} failed"
            if op.kind == ROTATION:
                summary += f", {counts['rekeyed']} rekeyed, {counts['rekey_failed']} not rekeyed"
            _audit_operation(
                session, org_id, kind, summary,
                {"operation_id": str(op.id), "key_hash": op.key_hash, "prev_key_hash": op.prev_key_hash, **counts},
            )
            session.commit()
            return DONE
    except seam.TenancyRowMissing:
        return GONE


@DBOS.step(**runtime.DB_RETRY)
def refresh_counts(op_id: str) -> None:
    """An item Retry finished after its operation did: bring the operation's stored counts up to date."""
    try:
        with _op_session(op_id) as (session, _org):
            op = _operation(session, op_id, lock=True)
            if op is None:
                return
            op.counts = {**(op.counts or {}), **item_counts(session, op.id)}
            session.commit()
    except seam.TenancyRowMissing:
        return


# ── Item steps ───────────────────────────────────────────────────────────────────────────────────────


def _settle_item(session: Session, item: Any, state: str, reason: Optional[str]) -> str:
    item.state, item.reason, item.staged = state, reason, None
    session.commit()
    return state


@DBOS.step(**runtime.DB_RETRY)
def seal_item(op_id: str, credential_id: str) -> str:
    """(1) Re-seal the credential's secrets under the operation's NodeVault key and stage the ciphertext on the
    item; `sealed`, or the item's settled state (`skipped` it can no longer be NodeVault, `failed` corrupt or
    key gone, or `done` on a replay), or `gone`. The KMS envelopes are untouched here."""
    from app.credential_crypto import SealedSecret, is_nodevault_key_id, reseal_secret_to_nodevault
    from app.credential_keys import NodeVaultSealed, nodevault_provider
    from app.models.credentials import NODEVAULT_SEALABLE_KEY_STATES, Credential

    failure: Optional[str] = None
    try:
        with _op_session(op_id) as (session, _org):
            op = _operation(session, op_id)
            item = _item(session, op_id, credential_id, lock=True)
            if op is None or item is None:
                return GONE
            if item.state != ITEM_PENDING:
                return item.state
            credential = session.scalar(
                select(Credential).where(Credential.id == UUID(credential_id)).with_for_update()
            )
            if credential is None:
                return GONE
            reason = ineligibility(session, credential)
            if reason is not None:
                return _settle_item(session, item, ITEM_SKIPPED, reason)
            if nodevault_provider(session, op.org_id, op.key_hash or "", states=NODEVAULT_SEALABLE_KEY_STATES) is None:
                return _settle_item(session, item, ITEM_FAILED, "unavailable")
            secrets = {f: v for f, v in (credential.config or {}).items() if isinstance(v, SealedSecret)}
            if any(is_nodevault_key_id(v.key_id) for v in secrets.values()):
                return _settle_item(session, item, ITEM_SKIPPED, "already_nodevault")
            fields = sorted(secrets)
            staged: dict[str, Any] = {}
            try:
                for field in fields:
                    staged[field] = reseal_secret_to_nodevault(session, credential, field, op.key_hash).envelope
            except NodeVaultSealed:
                return _settle_item(session, item, ITEM_SKIPPED, "already_nodevault")
            except ValueError:
                # The KMS envelope would not open (tampered, moved, re-pointed) or the value is not text: a
                # permanent failure of THIS item only. The exception (which may hold undecodable bytes) dies here.
                staged.clear()
                return _settle_item(session, item, ITEM_FAILED, "corrupt")
            item.staged = {"key_hash": op.key_hash, "digest": _secrets_digest(credential), "fields": staged}
            item.state, item.reason = ITEM_SEALED, None
            session.commit()
            return ITEM_SEALED
    except seam.TenancyRowMissing:
        return GONE
    except Exception as exc:  # noqa: BLE001 — re-raised below as its type only (`ItemStepFailed`)
        failure = type(exc).__name__
    raise ItemStepFailed(failure)


def _verify_request(session: Session, item: Any, credential: Any) -> list[dict[str, Any]]:
    """`nodevault.verify`'s items for the staged envelopes: each with the tier-2 AAD rebuilt from the CURRENT
    row, exactly as a runner fetch hands it to a node (`credential_crypto.nodevault_wire`)."""
    import base64

    from app.credential_crypto import tier2_aad
    from app.credentials.registry import sealing_destination

    destination = sealing_destination(credential.type, credential.config or {})
    return [
        {"ref": field, "envelope": envelope,
         "aad": base64.b64encode(tier2_aad(credential.id, field, destination)).decode()}
        for field, envelope in sorted(((item.staged or {}).get("fields") or {}).items())
    ]


@DBOS.step(**runtime.DB_RETRY)
def verify_item(op_id: str, credential_id: str) -> str:
    """(2) A node that holds the key trial-opens the staged envelopes under the row's AAD. `ok`; `no_holder`
    when no holder answered (the workflow waits HOLDER_ONLINE); `failed` when a holder could not open them (the
    item is `failed`/`verify_failed` and keeps its KMS envelope) or the row changed since the seal; the item's
    state when it is not `sealed`; or `gone`."""
    failure: Optional[str] = None
    try:
        return _verify_item(op_id, credential_id)
    except Exception as exc:  # noqa: BLE001 — re-raised below as its type only (`ItemStepFailed`)
        failure = type(exc).__name__
    raise ItemStepFailed(failure)


def _verify_item(op_id: str, credential_id: str) -> str:
    from app.models.credentials import Credential

    try:
        with _op_session(op_id) as (session, org_id):
            item = _item(session, op_id, credential_id)
            if item is None:
                return GONE
            if item.state != ITEM_SEALED:
                return item.state
            credential = session.scalar(select(Credential).where(Credential.id == UUID(credential_id)))
            if credential is None:
                return GONE
            if (item.staged or {}).get("digest") != _secrets_digest(credential):
                item = _item(session, op_id, credential_id, lock=True)
                if item is not None and item.state == ITEM_SEALED:
                    _settle_item(session, item, ITEM_FAILED, "changed")
                return ITEM_FAILED
            key_hash = item.staged["key_hash"]
            request = {"items": _verify_request(session, item, credential)}
            online = set(node_presence.connected_node_ids(session))
            holders = [
                str(holder) for holder in session.scalars(
                    select(NodeVaultGrant.node_id).where(
                        NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == key_hash,
                        NodeVaultGrant.state == NV_GRANT_DELIVERED,
                    ).order_by(NodeVaultGrant.delivered_at.asc().nulls_last())
                ) if str(holder) in online
            ]
            session.rollback()   # no connection held across the node calls below
    except seam.TenancyRowMissing:
        return GONE

    refused = not request["items"]   # nothing staged: nothing a node could prove, never a pass
    for holder in ([] if refused else holders):
        outcome, result = _call_node(holder, VERIFY, request)
        if outcome in (OFFLINE, TIMEOUT):
            continue
        if outcome != DONE:
            refused = True   # this node refused the whole batch; another may not
            continue
        replies = (result or {}).get("items")
        verdicts = {r.get("ref"): r for r in replies if isinstance(r, dict)} if isinstance(replies, list) else {}
        wanted = [entry["ref"] for entry in request["items"]]
        if wanted and all(verdicts.get(ref, {}).get("ok") is True for ref in wanted):
            return OK
        errors = {verdicts.get(ref, {}).get("error") for ref in wanted if verdicts.get(ref, {}).get("ok") is not True}
        if errors <= {"missing_key"}:
            # The CP thought it held the key and it does not: its own delivery puts it back; another may verify.
            _demote_holder(holder, key_hash)
            continue
        refused = True
        break
    if not refused:
        return NO_HOLDER
    try:
        with _op_session(op_id) as (session, _org):
            item = _item(session, op_id, credential_id, lock=True)
            if item is not None and item.state == ITEM_SEALED:
                _settle_item(session, item, ITEM_FAILED, "verify_failed")
    except seam.TenancyRowMissing:
        return GONE
    return ITEM_FAILED


@DBOS.step(**runtime.DB_RETRY)
def swap_item(op_id: str, credential_id: str) -> str:
    """(3) In ONE transaction: the credential's tier-2 envelopes become the verified NodeVault ones (the KMS
    envelopes are gone from that moment), `nodevault` is set, `staged` nulled, the item `done`, and
    `nodevault.credential_migrated` audited (credential, from KMS → key hash). Re-checks under the row lock
    that nothing changed since the seal. `done`, the item's state, or `gone`."""
    from app.credential_crypto import SealedSecret
    from app.credential_keys import nodevault_provider
    from app.models.credentials import NODEVAULT_SEALABLE_KEY_STATES, Credential

    failure: Optional[str] = None
    try:
        with _op_session(op_id) as (session, org_id):
            item = _item(session, op_id, credential_id, lock=True)
            if item is None:
                return GONE
            if item.state != ITEM_SEALED:
                return item.state
            credential = session.scalar(
                select(Credential).where(Credential.id == UUID(credential_id)).with_for_update()
            )
            if credential is None:
                return GONE
            if item.staged.get("digest") != _secrets_digest(credential):
                return _settle_item(session, item, ITEM_FAILED, "changed")
            # Re-checked under the row lock: a consumer may have been pointed at it, or the key retired, while
            # the verify waited for a holder — the KMS envelope must not go then.
            reason = ineligibility(session, credential)
            if reason is not None:
                return _settle_item(session, item, ITEM_SKIPPED, reason)
            key_hash = item.staged["key_hash"]
            if not item.staged.get("fields") or nodevault_provider(
                session, org_id, key_hash, states=NODEVAULT_SEALABLE_KEY_STATES,
            ) is None:
                return _settle_item(session, item, ITEM_FAILED, "unavailable")
            config = dict(credential.config or {})
            for field, envelope in item.staged["fields"].items():
                config[field] = SealedSecret(envelope)
            credential.config = config
            credential.nodevault = True
            item.state, item.reason, item.staged = ITEM_DONE, None, None
            _audit_operation(
                session, org_id, "nodevault.credential_migrated",
                f"Credential {credential.name} moved from KMS to NodeVault key {key_hash[:12]}",
                {"credential_id": str(credential.id), "operation_id": str(op_id), "from": "kms", "key_hash": key_hash},
            )
            session.commit()
            return ITEM_DONE
    except seam.TenancyRowMissing:
        return GONE
    except Exception as exc:  # noqa: BLE001 — re-raised below as its type only (`ItemStepFailed`)
        failure = type(exc).__name__
    raise ItemStepFailed(failure)


@DBOS.step(**runtime.DB_RETRY)
def fail_item(op_id: str, credential_id: str, reason: str) -> None:
    """Record a failure the item's own step could not (it kept raising): `failed` with `reason`, staged
    ciphertext dropped. A settled item is left alone."""
    try:
        with _op_session(op_id) as (session, _org):
            item = _item(session, op_id, credential_id, lock=True)
            if item is not None and item.state in (ITEM_PENDING, ITEM_SEALED):
                _settle_item(session, item, ITEM_FAILED, reason)
    except seam.TenancyRowMissing:
        return


# ── Operation workflows ──────────────────────────────────────────────────────────────────────────────


def _migrate_one(op_id: str, credential_id: str) -> str:
    """seal → verify (waiting for a holder) → swap for one item; its final state word. Never raises: a step that
    keeps failing marks the item `failed`/`unavailable`, and the next item goes on (AC-7.3)."""
    try:
        state = seal_item(op_id, credential_id)
        if state != ITEM_SEALED:
            return state
        while True:
            verdict = verify_item(op_id, credential_id)
            if verdict != NO_HOLDER:
                break
            DBOS.recv(HOLDER_ONLINE, timeout_seconds=RECHECK_SECONDS)
        if verdict != OK:
            return verdict
        return swap_item(op_id, credential_id)
    except DBOSWorkflowCancelledError:
        raise   # an abandon: the item is the abandon's to settle
    except Exception:  # noqa: BLE001 — a step's exhausted retries; its exception carries a type name only
        fail_item(op_id, credential_id, "unavailable")
        return ITEM_FAILED


def _migrate_all(op_id: str) -> None:
    for credential_id in items_to_migrate(op_id):
        _migrate_one(op_id, credential_id)


def _await_migrate_choice(op_id: str) -> bool:
    """Wait at `migrate_choice` (a re-check loop that never times out terminally) until the admin's choice is
    recorded; False when the operation stopped running first."""
    while True:
        choice = migrate_choice_state(op_id)
        if choice == GONE:
            return False
        if choice == "made":
            return True
        DBOS.recv(MIGRATE_CHOICE, timeout_seconds=OP_RECHECK_SECONDS)


@DBOS.workflow()
def setup_operation(op_id: str) -> str:
    """`nodevault-setup:{op_id}` (the section header)."""
    while True:
        state = distribution_state(op_id)
        if state == GONE:
            return ENDED
        if state == DELIVERED:
            break
        if DBOS.recv(GRANT_DELIVERED, timeout_seconds=OP_RECHECK_SECONDS) is None:
            restart_key_deliveries(op_id)
    if promote_key(op_id) != DONE:
        return ENDED
    if not _await_migrate_choice(op_id):
        return ENDED
    _migrate_all(op_id)
    return DONE if finish_operation(op_id) == DONE else ENDED


@DBOS.workflow()
def migrate_operation(op_id: str) -> str:
    """`nodevault-migrate:{op_id}`: KMS credentials left after setup move to the org's current key."""
    _migrate_all(op_id)
    return DONE if finish_operation(op_id) == DONE else ENDED


@DBOS.workflow()
def migrate_item_retry(op_id: str, credential_id: str, n: int) -> str:
    """`nodevault-migrate-item:{op_id}:{credential_id}:{n}` — one failed item's Retry (the route reset it to
    `pending`); the operation's stored counts follow."""
    state = _migrate_one(op_id, credential_id)
    refresh_counts(op_id)
    return state


# ══ Operations: rotation, rekey and retire ═══════════════════════════════════════════════════════════
#
# `nodevault-rotate:{op_id}` — the route has stored the new key `pending` with the browser's chain blob (the
# new private key wrapped under the CURRENT key, E2) and enrolled every capable node. `distribute`: re-enrol
# them all (`ensure_grants`: a holder of the current key resolves to `chain`, any other node to `peer`), then
# `promote`: wait until every capable node ONLINE at the check holds the new key `delivered` — at least one
# must — and in ONE transaction the new key becomes `current` and the old one `previous`. An offline node
# catches up later by `chain` and meanwhile still opens everything not yet rekeyed. Then `migrate_choice` and
# `migrating` exactly as in setup (the operation's `key_hash` is the new key), then `rekey`, then `result`.
# `rekey` (a phase of the rotate workflow): batches of at most `REKEY_BATCH` secrets whose envelope names the
# previous key go to an online node holding BOTH keys (`nodevault.rekey`); the node returns only each secret's
# data key rewrapped to the new public key, and ONE transaction per credential swaps `ek` and `k` — only while
# the stored envelope is still exactly the one sent, so a rerun or a duplicate reply changes nothing. The
# query repeats until none is left, which also sweeps a secret a browser sealed under the old key while the
# promotion committed. A secret no holder can open is a `failed` item and is never retried in a loop.
# `nodevault-retire:{op_id}` — `retiring`: refused unless the key is `previous` and no credential names it; the
# key → `retiring` (the credential door then refuses seals under it), then the count is taken AGAIN after that
# commit — a seal that raced it puts the key back while every node still holds it — and only then does every
# grant go `retire_pending`. Then wait (GRANT_RETIRED, a 24h re-check loop) until every grant is `retired`; only then is the
# key `retired`. An offline node keeps the operation `retiring`, its pending count on the page.

#: Secrets per `nodevault.rekey` call — the node's own bound (`apps/jaahd/nodevault_commands.py::MAX_ITEMS`).
REKEY_BATCH = 50
#: How often `promote` re-reads which capable nodes are online: a node going OFFLINE sends no signal, and it
#: is what unblocks the wait.
PROMOTE_RECHECK_SECONDS = 300.0
#: A rekey batch's step that keeps failing (KMS-free, so the database) is retried after this long, at most
#: `REKEY_MAX_FAILURES` times in a row; then the phase ends and what is left stays on the previous key.
REKEY_RETRY_SECONDS = 60.0
REKEY_MAX_FAILURES = 5
REKEY = "nodevault.rekey"
EMPTY, PROGRESS, WAITING, IN_USE = "empty", "progress", "waiting", "in_use"
#: A rewrapped data key is an RSA-4096 OAEP ciphertext: 512 bytes, 684 base64 characters.
_EK_BYTES = 512
_EK_MAX_CHARS = 1024
_UNOPENABLE_WORDS = ("open_failed", "malformed", "wrong_key")


def capable_node_ids(session: Session, org_id: UUID) -> set[str]:
    """The org's nodes that can hold a NodeVault key (`nodevault_grants.key_holder_clause`)."""
    return {str(n) for n in session.scalars(select(Node.id).where(
        Node.org_id == org_id, *nodevault_grants.key_holder_clause(),
    ))}


def _nv_secrets(credential: Any, key_hash: str) -> dict[str, Any]:
    """`{field: SealedSecret}` of `credential`'s secrets sealed under NodeVault key `key_hash`."""
    from app.credential_crypto import SealedSecret
    from app.credential_keys import nodevault_key_id

    key_id = nodevault_key_id(key_hash)
    return {f: v for f, v in sorted((credential.config or {}).items())
            if isinstance(v, SealedSecret) and v.key_id == key_id}


def credentials_on_key(session: Session, org_id: UUID, key_hash: str) -> list[Any]:
    """The org's credentials with at least one secret sealed under NodeVault key `key_hash`, in id order. Opens
    tier 1 only — the tier-2 envelopes stay sealed and only their key ids are read."""
    from app.models.credentials import Credential

    return [c for c in session.scalars(select(Credential).where(
        Credential.org_id == org_id, Credential.nodevault.is_(True),
    ).order_by(Credential.id)) if _nv_secrets(c, key_hash)]


def _rekey_item(session: Session, op_id: str | UUID, credential_id: str | UUID):
    from app.models.credentials import NodeVaultOperationItem

    return session.scalar(select(NodeVaultOperationItem).where(
        NodeVaultOperationItem.operation_id == UUID(str(op_id)),
        NodeVaultOperationItem.credential_id == UUID(str(credential_id)),
        NodeVaultOperationItem.phase == "rekey",
    ).with_for_update())


def _settle_rekey_item(session: Session, org_id: UUID, op_id: str, credential_id: UUID, state: str,
                       reason: Optional[str]) -> None:
    from app.models.credentials import NodeVaultOperationItem

    item = _rekey_item(session, op_id, credential_id)
    if item is None:
        item = NodeVaultOperationItem(org_id=org_id, operation_id=UUID(op_id), credential_id=credential_id,
                                      phase="rekey")
        session.add(item)
    item.state, item.reason, item.staged = state, reason, None


# ── Rotation steps ───────────────────────────────────────────────────────────────────────────────────


@DBOS.step(**runtime.DB_RETRY)
def enrol_rotation_nodes(op_id: str) -> int:
    """`distribute`: `ensure_grants` for every capable node of the org (idempotent — the route already did it
    in its transaction; this restarts what a crash between that commit and the starts lost), then the step
    moves to `promote`. How many nodes."""
    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id)
            if op is None or op.state != NV_OP_RUNNING or op.kind != ROTATION:
                return 0
            nodes = sorted(capable_node_ids(session, org_id))
    except seam.TenancyRowMissing:
        return 0
    enrolled = _ensure_grants_for(nodes)
    try:
        with _op_session(op_id) as (session, _org):
            op = _operation(session, op_id, lock=True)
            if op is not None and op.state == NV_OP_RUNNING and op.step == STEP_DISTRIBUTE:
                op.step = STEP_PROMOTE
                session.commit()
    except seam.TenancyRowMissing:
        return 0
    return enrolled


@DBOS.step(**runtime.DB_RETRY)
def promote_rotation(op_id: str) -> str:
    """`promote`, checked and done under the operation's lock (an abandon takes the same lock and refuses once
    the step moved on): `waiting` while no node has acknowledged the new key, or any capable node that is
    online right now has not; else, in ONE transaction, the current key → `previous`, the new key → `current`
    and the step → `migrate_choice`. `done` (also on a replay after it committed), or `gone`."""
    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id, lock=True)
            if op is None or op.state != NV_OP_RUNNING:
                return GONE
            if op.step not in (STEP_DISTRIBUTE, STEP_PROMOTE):
                return DONE
            capable = capable_node_ids(session, org_id)
            online = capable & set(node_presence.connected_node_ids(session))
            settled = dict(session.execute(select(NodeVaultGrant.node_id, NodeVaultGrant.state).where(
                NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == op.key_hash,
                NodeVaultGrant.state.in_((NV_GRANT_DELIVERED, NV_GRANT_FAILED)),
            )).all())
            delivered = {str(n) for n, state in settled.items() if state == NV_GRANT_DELIVERED}
            # A node whose delivery FAILED (its Retry is on the page) waits like an offline one: it still holds
            # the old key, and holding the promotion for it would wedge the rotation on an admin action.
            failed = {str(n) for n, state in settled.items() if state == NV_GRANT_FAILED}
            if not delivered or online - delivered - failed:
                session.rollback()
                return WAITING
            keys = {key.key_hash: key for key in session.scalars(select(NodeVaultKey).where(
                NodeVaultKey.org_id == org_id, NodeVaultKey.key_hash.in_((op.key_hash, op.prev_key_hash)),
            ).order_by(NodeVaultKey.key_hash).with_for_update())}
            new, old = keys.get(op.key_hash), keys.get(op.prev_key_hash)
            if new is None or new.state != NV_KEY_PENDING:
                _close_failed(session, op)
                session.commit()
                return GONE
            if old is not None and old.state == NV_KEY_CURRENT:
                old.state = NV_KEY_PREVIOUS
                session.flush()   # the one-current index: the old key steps down first
            new.state = NV_KEY_CURRENT
            op.step = STEP_MIGRATE_CHOICE
            session.commit()
            return DONE
    except seam.TenancyRowMissing:
        return GONE


@DBOS.step(**runtime.DB_RETRY)
def begin_rekey(op_id: str) -> str:
    """The step → `rekey`; `done`, or `gone` when the operation is no longer running."""
    try:
        with _op_session(op_id) as (session, _org):
            op = _operation(session, op_id, lock=True)
            if op is None or op.state != NV_OP_RUNNING:
                return GONE
            if op.step != STEP_REKEY:
                op.step = STEP_REKEY
                session.commit()
            return DONE
    except seam.TenancyRowMissing:
        return GONE


@DBOS.step(**runtime.DB_RETRY)
def rekey_batch(op_id: str) -> str:
    """One rekey batch: `progress` (a holder answered; each credential was swapped, failed, or found changed and
    left for the next query), `empty` (nothing left under the old key), `no_holder` (no online node holds both
    keys), or `gone`. The rewrapped data keys live in this frame only; the step returns a word."""
    failure: Optional[str] = None
    try:
        return _rekey_batch(op_id)
    except Exception as exc:  # noqa: BLE001 — re-raised below as its type only (`ItemStepFailed`)
        failure = type(exc).__name__
    raise ItemStepFailed(failure)


def _rekey_batch(op_id: str) -> str:
    import base64

    from app.credential_crypto import tier2_aad
    from app.credential_keys import nodevault_key_id
    from app.credentials.registry import sealing_destination
    from app.models.credentials import NodeVaultOperationItem

    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id)
            if op is None or op.state != NV_OP_RUNNING or op.kind != ROTATION or not op.prev_key_hash:
                return GONE
            old, new = op.prev_key_hash, op.key_hash
            new_key = _key(session, org_id, new)
            if new_key is None or new_key.state != NV_KEY_CURRENT:
                return GONE
            failed = set(session.scalars(select(NodeVaultOperationItem.credential_id).where(
                NodeVaultOperationItem.operation_id == op.id, NodeVaultOperationItem.phase == "rekey",
                NodeVaultOperationItem.state == ITEM_FAILED,
            )))
            batch: list[tuple[UUID, list[dict[str, Any]]]] = []
            size = 0
            for credential in credentials_on_key(session, org_id, old):
                if credential.id in failed:
                    continue
                try:
                    destination = sealing_destination(credential.type, credential.config or {})
                    entries = [
                        {"ref": f"{credential.id}:{field}", "envelope": secret.envelope,
                         "aad": base64.b64encode(tier2_aad(credential.id, field, destination)).decode()}
                        for field, secret in _nv_secrets(credential, old).items()
                    ]
                except Exception:  # noqa: BLE001 — this credential only; its row could not be read into a request
                    _settle_rekey_item(session, org_id, op_id, credential.id, ITEM_FAILED, "not_rekeyed")
                    session.commit()
                    return PROGRESS
                if size + len(entries) > REKEY_BATCH:
                    if batch:
                        break
                    # More secrets than one node call carries: never sent, recorded as a failure.
                    _settle_rekey_item(session, org_id, op_id, credential.id, ITEM_FAILED, "not_rekeyed")
                    session.commit()
                    return PROGRESS
                batch.append((credential.id, entries))
                size += len(entries)
            if not batch:
                return EMPTY
            request = {"old_hash": old, "new_hash": new, "new_public_key": new_key.public_key,
                       "items": [entry for _cid, entries in batch for entry in entries]}
            online = set(node_presence.connected_node_ids(session))
            holders_old = {str(n) for n in session.scalars(select(NodeVaultGrant.node_id).where(
                NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == old,
                NodeVaultGrant.state == NV_GRANT_DELIVERED,
            ))}
            holders = [
                str(n) for n in session.scalars(select(NodeVaultGrant.node_id).where(
                    NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == new,
                    NodeVaultGrant.state == NV_GRANT_DELIVERED,
                ).order_by(NodeVaultGrant.delivered_at.asc().nulls_last()))
                if str(n) in online and str(n) in holders_old
            ]
            session.rollback()   # no connection held across the node calls below
    except seam.TenancyRowMissing:
        return GONE

    for holder in holders:
        outcome, result = _call_node(holder, REKEY, request)
        if outcome in (OFFLINE, TIMEOUT):
            continue
        if outcome != DONE:
            if outcome == FAILED_PREFIX + grant_error("missing_key"):
                # The CP thought it held the old key and it does not: its own delivery puts it back.
                _demote_holder(holder, old)
            continue
        replies = (result or {}).get("items")
        verdicts = ({r.get("ref"): r for r in replies if isinstance(r, dict)}
                    if isinstance(replies, list) else {})
        # Nothing is swapped on the rekeying node's word alone: the same node (it holds the new key) must open
        # each rewrapped envelope under its AAD first, as a migration's verify does — a wrong `ek` would
        # otherwise replace the only one that opens.
        rewrapped = [{**entry, "envelope": {**entry["envelope"], "ek": verdicts[entry["ref"]]["ek"],
                                            "k": nodevault_key_id(new)}}
                     for entry in request["items"] if _ek_shaped((verdicts.get(entry["ref"]) or {}).get("ek"))]
        opened: set[str] = set()
        if rewrapped:
            outcome, result = _call_node(holder, VERIFY, {"items": rewrapped})
            if outcome != DONE:
                continue
            replies = (result or {}).get("items")
            opened = {r.get("ref") for r in replies if isinstance(r, dict) and r.get("ok") is True} \
                if isinstance(replies, list) else set()
        for credential_id, entries in batch:
            _swap_rekeyed(op_id, credential_id, old, new, entries, verdicts, opened)
        return PROGRESS
    return NO_HOLDER


def _ek_shaped(ek: Any) -> bool:
    """A rewrapped data key's shape: base64 of exactly one RSA-4096 OAEP ciphertext."""
    import base64
    import binascii

    if not isinstance(ek, str) or not 0 < len(ek) <= _EK_MAX_CHARS:
        return False
    try:
        return len(base64.b64decode(ek, validate=True)) == _EK_BYTES
    except (binascii.Error, ValueError):
        return False


def _swap_rekeyed(op_id: str, credential_id: UUID, old: str, new: str, entries: list[dict[str, Any]],
                  verdicts: dict[Any, dict[str, Any]], opened: set[Any]) -> None:
    """ONE transaction for one credential: each secret's `ek` and `k` move to the new key ONLY while the stored
    envelope is still exactly the one sent (else it changed since — the next query sends it again) and a node
    OPENED the rewrapped envelope (`opened`); the rekey item `done` and `nodevault.credential_rekeyed` audited,
    or the item `failed` when a secret could not be opened or rewrapped — its envelopes left exactly as they
    were. Idempotent: a replay finds the envelopes already moved and changes nothing."""
    from app.credential_crypto import SealedSecret
    from app.credential_keys import nodevault_key_id
    from app.models.credentials import Credential

    with _op_session(op_id) as (session, org_id):
        credential = session.scalar(select(Credential).where(Credential.id == credential_id).with_for_update())
        if credential is None:
            return
        config = dict(credential.config or {})
        moved: dict[str, SealedSecret] = {}
        problem: Optional[str] = None
        for entry in entries:
            field = entry["ref"].split(":", 1)[1]
            stored = config.get(field)
            if not isinstance(stored, SealedSecret) or stored.envelope != entry["envelope"]:
                return   # rekeyed already, re-sealed or edited since the batch was read
            verdict = verdicts.get(entry["ref"]) or {}
            ek = verdict.get("ek")
            if not _ek_shaped(ek) or entry["ref"] not in opened:
                problem = problem or ("unopenable" if verdict.get("error") in _UNOPENABLE_WORDS else "not_rekeyed")
                continue
            envelope = stored.envelope
            envelope["ek"], envelope["k"] = ek, nodevault_key_id(new)
            moved[field] = SealedSecret(envelope)
        if problem is not None:
            _settle_rekey_item(session, org_id, op_id, credential.id, ITEM_FAILED, problem)
            session.commit()
            return
        config.update(moved)
        credential.config = config
        _settle_rekey_item(session, org_id, op_id, credential.id, ITEM_DONE, None)
        _audit_operation(
            session, org_id, "nodevault.credential_rekeyed",
            f"Credential {credential.name} rekeyed from NodeVault key {old[:12]} to {new[:12]}",
            {"credential_id": str(credential.id), "operation_id": str(op_id), "from": old, "to": new},
        )
        session.commit()


def _rekey_all(op_id: str) -> None:
    """The `rekey` phase: batches until none is left under the old key. Waits HOLDER_ONLINE while no online node
    holds both keys; a batch whose step keeps failing is tried again after `REKEY_RETRY_SECONDS`."""
    if begin_rekey(op_id) != DONE:
        return
    failures = 0
    while True:
        try:
            outcome = rekey_batch(op_id)
        except DBOSWorkflowCancelledError:
            raise
        except Exception:  # noqa: BLE001 — the step's exhausted retries; its exception carries a type name only
            failures += 1
            if failures >= REKEY_MAX_FAILURES:
                return   # never a loop: the result shows what is still on the old key
            DBOS.sleep(REKEY_RETRY_SECONDS)
            continue
        failures = 0
        if outcome in (EMPTY, GONE):
            return
        if outcome == NO_HOLDER:
            DBOS.recv(HOLDER_ONLINE, timeout_seconds=RECHECK_SECONDS)


@DBOS.workflow()
def rotate_operation(op_id: str) -> str:
    """`nodevault-rotate:{op_id}` (the section header)."""
    enrol_rotation_nodes(op_id)
    restarted = now()
    while True:
        state = promote_rotation(op_id)
        if state == GONE:
            return ENDED
        if state == DONE:
            break
        if DBOS.recv(GRANT_DELIVERED, timeout_seconds=PROMOTE_RECHECK_SECONDS) is None and (
                now() - restarted >= OP_RECHECK_SECONDS):
            restart_key_deliveries(op_id)
            restarted = now()
    if not _await_migrate_choice(op_id):
        return ENDED
    _migrate_all(op_id)
    _rekey_all(op_id)
    return DONE if finish_operation(op_id) == DONE else ENDED


# ── Retire steps ─────────────────────────────────────────────────────────────────────────────────────


def _close_failed(session: Session, op: NodeVaultOperation, **counts: int) -> None:
    op.state = OP_FAILED
    op.counts = {**(op.counts or {}), **counts}


def restore_previous(session: Session, org_id: UUID, key: NodeVaultKey) -> list[UUID]:
    """A retiring key back to `previous`, in the caller's transaction: every grant of it that is retiring,
    retired or failed goes back to `waiting` under a fresh generation (so a retire-node run still going finds
    itself superseded) with its blob cleared, so delivery step 0 rebuilds one. The nodes, for the caller's
    `ensure_grants` (§3.5 retire abandon)."""
    key.state, key.retired_at = NV_KEY_PREVIOUS, None
    nodes: list[UUID] = []
    for grant in session.scalars(select(NodeVaultGrant).where(
        NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == key.key_hash,
    ).order_by(NodeVaultGrant.node_id).with_for_update()):
        nodes.append(grant.node_id)
        if grant.state == NV_GRANT_DELIVERED:
            continue
        grant.state, grant.last_error, grant.blob, grant.blob_kind = NV_GRANT_WAITING, None, None, None
        grant.delivered_at = None
        grant.generation = int(grant.generation) + 1
    session.flush()
    return nodes


@DBOS.step(**runtime.DB_RETRY)
def begin_retire(op_id: str) -> str:
    """`retiring`, part one: refused (the operation closes `failed`) unless the key is `previous` and no
    credential names it; else the key → `retiring`, which makes the credential door refuse seals under it.
    No node is touched yet. `retiring` (also on a replay), `in_use`, or `gone`."""
    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id, lock=True)
            if op is None or op.state != NV_OP_RUNNING or op.kind != RETIRE:
                return GONE
            key = session.scalar(select(NodeVaultKey).where(
                NodeVaultKey.org_id == org_id, NodeVaultKey.key_hash == op.key_hash).with_for_update())
            if key is not None and key.state == NV_KEY_RETIRING:
                return STEP_RETIRING
            if key is None or key.state != NV_KEY_PREVIOUS:
                _close_failed(session, op)
                session.commit()
                return GONE
            in_use = len(credentials_on_key(session, org_id, op.key_hash))
            if in_use:
                _close_failed(session, op, in_use=in_use)
                session.commit()
                return IN_USE
            key.state = NV_KEY_RETIRING
            session.commit()
            return STEP_RETIRING
    except seam.TenancyRowMissing:
        return GONE


@DBOS.step(**runtime.DB_RETRY)
def recheck_retire(op_id: str) -> str:
    """`retiring`, part two, AFTER that commit: the count again, so a browser that sealed under the key just
    before it is caught while every node still holds the key — the key goes back to `previous` and nothing was
    taken off anywhere. Only then does every grant go `retire_pending` and each node's `retire-node` start.
    `retiring`, `in_use`, or `gone`."""
    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id, lock=True)
            if op is None or op.state != NV_OP_RUNNING:
                return GONE
            node_ids = sorted(set(session.scalars(select(NodeVaultGrant.node_id).where(
                NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == op.key_hash))))
            # Node rows, then the key row, then grants — `ensure_grants`' and the cleanup's lock order.
            if node_ids:
                session.scalars(select(Node.id).where(Node.id.in_(node_ids)).order_by(Node.id).with_for_update()).all()
            key = session.scalar(select(NodeVaultKey).where(
                NodeVaultKey.org_id == org_id, NodeVaultKey.key_hash == op.key_hash).with_for_update())
            if key is None or key.state != NV_KEY_RETIRING:
                _close_failed(session, op)
                session.commit()
                return GONE
            in_use = len(credentials_on_key(session, org_id, op.key_hash))
            if in_use:
                key.state = NV_KEY_PREVIOUS
                _close_failed(session, op, in_use=in_use)
                session.commit()
                return IN_USE
            for grant in session.scalars(select(NodeVaultGrant).where(
                NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == op.key_hash,
                NodeVaultGrant.state != NV_GRANT_RETIRED,
            ).order_by(NodeVaultGrant.node_id).with_for_update()):
                grant.state, grant.last_error, grant.blob, grant.blob_kind = NV_GRANT_RETIRE_PENDING, None, None, None
            session.commit()
    except seam.TenancyRowMissing:
        return GONE
    _ensure_grants_for([str(n) for n in node_ids])
    return STEP_RETIRING


@DBOS.step(**runtime.DB_RETRY)
def finish_retire(op_id: str) -> str:
    """The key `retired` once EVERY grant of it is `retired` (a deleted node's grant went with it), the
    operation `done` and `nodevault.key_retired` audited, in one transaction; `waiting` while a node still
    holds it; `gone` when the operation stopped running (abandoned)."""
    try:
        with _op_session(op_id) as (session, org_id):
            op = _operation(session, op_id, lock=True)
            if op is None or op.state != NV_OP_RUNNING:
                return GONE
            key = session.scalar(select(NodeVaultKey).where(
                NodeVaultKey.org_id == org_id, NodeVaultKey.key_hash == op.key_hash).with_for_update())
            if key is None or key.state != NV_KEY_RETIRING:
                _close_failed(session, op)
                session.commit()
                return GONE
            pending = int(session.scalar(select(func.count()).select_from(NodeVaultGrant).where(
                NodeVaultGrant.org_id == org_id, NodeVaultGrant.key_hash == op.key_hash,
                NodeVaultGrant.state != NV_GRANT_RETIRED,
            )))
            if pending:
                session.rollback()
                return WAITING
            retire_key_row(session, key)
            op.step, op.state = STEP_RESULT, OP_DONE
            op.counts = {**(op.counts or {}), "nodes_pending": 0}
            _audit_operation(
                session, org_id, "nodevault.key_retired", f"Old NodeVault key {op.key_hash[:12]} retired",
                {"operation_id": str(op.id), "key_hash": op.key_hash},
            )
            session.commit()
            return DONE
    except seam.TenancyRowMissing:
        return GONE


@DBOS.workflow()
def retire_operation(op_id: str) -> str:
    """`nodevault-retire:{op_id}` (the rotation section header)."""
    if begin_retire(op_id) != STEP_RETIRING or recheck_retire(op_id) != STEP_RETIRING:
        return ENDED
    while True:
        state = finish_retire(op_id)
        if state == DONE:
            return DONE
        if state == GONE:
            return ENDED
        if DBOS.recv(GRANT_RETIRED, timeout_seconds=OP_RECHECK_SECONDS) is None:
            restart_key_deliveries(op_id)


_OPERATION_WORKFLOWS: dict[str, Any] = {
    SETUP: setup_operation, MIGRATION: migrate_operation, ROTATION: rotate_operation, RETIRE: retire_operation,
}


# ── Starting them (the routes call these after their commit) ─────────────────────────────────────────


def start_operation(kind: str, op_id: UUID | str) -> None:
    """Start the operation's workflow under its deterministic id; DBOS dedups a repeated start."""
    from dbos import SetWorkflowID

    with SetWorkflowID(operation_workflow_id(kind, op_id)):
        DBOS.start_workflow(_OPERATION_WORKFLOWS[kind], str(op_id))


def ensure_operation_started(kind: str, op_id: UUID | str) -> None:
    """Start a running operation's workflow if DBOS has no record of it (the API died between the commit and the
    start). Best-effort and never raises: the status read calls it on every poll."""
    try:
        if DBOS.get_workflow_status(operation_workflow_id(kind, op_id)) is None:
            start_operation(kind, op_id)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("nodevault: could not check operation %s's workflow (%s)", op_id, type(exc).__name__)


def start_item_retry(op_id: UUID | str, credential_id: UUID | str) -> str:
    """Start the next `nodevault-migrate-item:{op}:{credential}:{n}`; its id. `n` counts the earlier retries of
    this item, so two clicks racing compute the same id and DBOS runs it once."""
    from dbos import SetWorkflowID

    prefix = f"{ITEM_RETRY_PREFIX}:{op_id}:{credential_id}:"
    earlier = DBOS.list_workflows(workflow_id_prefix=prefix, load_input=False, load_output=False)
    wid = item_retry_workflow_id(op_id, credential_id, len(earlier))
    with SetWorkflowID(wid):
        DBOS.start_workflow(migrate_item_retry, str(op_id), str(credential_id), len(earlier))
    return wid


def ensure_item_retries_started(op_id: UUID | str, credential_ids: Iterable[UUID | str]) -> None:
    """Start a Retry for each pending item of a CLOSED operation whose Retry has no unsettled workflow (the API
    died between the Retry's commit and its start). Best-effort and never raises: the result read calls it."""
    for credential_id in credential_ids:
        prefix = f"{ITEM_RETRY_PREFIX}:{op_id}:{credential_id}:"
        try:
            if not DBOS.list_workflows(workflow_id_prefix=prefix, status=["PENDING", "ENQUEUED", "DELAYED"],
                                       load_input=False, load_output=False):
                start_item_retry(op_id, credential_id)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.warning("nodevault: could not restart an item retry of %s (%s)", op_id, type(exc).__name__)


def cancel_workflows(workflow_ids: Iterable[str], prefixes: Iterable[str] = ()) -> None:
    """Cancel each named (or prefix-matched) workflow that has not settled. Best-effort: every workflow here
    re-checks its rows at each step, so one that outlives a cancel ends on its own."""
    try:
        running = {w.workflow_id for w in DBOS.list_workflows(
            workflow_ids=sorted(set(workflow_ids)), status=["PENDING", "ENQUEUED", "DELAYED"],
            load_input=False, load_output=False,
        )} if workflow_ids else set()
        for prefix in prefixes:
            running |= {w.workflow_id for w in DBOS.list_workflows(
                workflow_id_prefix=prefix, status=["PENDING", "ENQUEUED", "DELAYED"],
                load_input=False, load_output=False,
            )}
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("nodevault: could not list workflows to cancel (%s)", type(exc).__name__)
        return
    for wid in sorted(running):
        try:
            DBOS.cancel_workflow(wid)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.info("nodevault: could not cancel %s (%s)", wid, type(exc).__name__)
