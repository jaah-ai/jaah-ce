"""NodeVault key placement: the ONE enrolment entry point (`ensure_grants`) and the one starter of the
per-(node, key) DBOS workflows (`start_grant_workflow`).

`nodevault_grants` is the desired and observed placement of every NodeVault key on every node. Delivery and
retirement are each a DBOS workflow per grant (operator decision E3), with the deterministic id
`nodevault-<deliver|retire-node>:{node}:{key_hash}:{generation}`. Their BODIES live with the rest of the
NodeVault workflows (`app/durable/nodevault.py`), which register them here by name (`register`); this
module only decides WHICH grant needs a workflow and starts it — so a node, a fetch miss, a rotation or a
Retry all converge through the same idempotent call, and a workflow a version bump or a 7-day park left
behind is restarted on the node's next contact.

`ensure_grants(session, node_id)` (callers: the identity POST, `hello`, a fetch miss, the rotate workflow,
the grant Retry route):
  * a node that is `capable` and has an identity key, in an org that is SET UP (it has a `current` key) or
    has a running setup operation, gets a `waiting` grant for every LIVE key it lacks (current, previous,
    and a pending one while the setup or rotation that minted it runs), and every `waiting` grant of it gets
    `start_grant_workflow`;
  * every `retire_pending` grant gets `start_grant_workflow` regardless of either precondition, so an
    abandoned first setup still clears its key off every node, capable or not;
  * a Multi-Node Host never holds a key (`key_holder_clause`): every grant it still has goes `retire_pending`.

`start_grant_workflow(session, grant)` reads the DBOS status of the grant's current workflow id: unsettled
(PENDING, ENQUEUED, DELAYED) → nothing; absent → start it; finished while the grant is still unsettled
(parked, failed, cancelled, or done without settling it) → `generation + 1` in the SAME transaction and
start the new id. Starts run after the caller's commit (`app/after_commit.py`), so a workflow never reads a
grant row that is not there yet; DBOS dedups a start by id, so a repeated start is harmless.

Nothing here touches key material: a grant's `blob` is ciphertext and is never read, logged or passed.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from uuid import UUID

from dbos import DBOS, SetWorkflowID
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app import after_commit, pool_state
from app.models.credentials import (
    NODEVAULT_LIVE_KEY_STATES,
    NV_GRANT_RETIRE_PENDING,
    NV_GRANT_RETIRED,
    NV_GRANT_WAITING,
    NV_KEY_CURRENT,
    NV_KEY_PENDING,
    NV_OP_RUNNING,
    NodeVaultGrant,
    NodeVaultKey,
    NodeVaultOperation,
)
from app.models.nodes import Node

log = logging.getLogger("app.nodevault_grants")


def key_holder_clause() -> tuple[Any, ...]:
    """THE ONE DEFINITION of a node that can hold a NodeVault key: capable, with a registered identity, and
    not a Multi-Node Host — a host runs no work, so it never needs the key (`app/vnodes.py`)."""
    return (Node.nodevault_capable.is_(True), Node.nodevault_identity_key.is_not(None), pool_state.not_a_vnode_host())

#: The two grant workflows, by the name their body registers under and their id spells.
DELIVER = "deliver"
RETIRE_NODE = "retire-node"
WORKFLOW_KINDS = (DELIVER, RETIRE_NODE)

#: DBOS statuses of a workflow that has not settled: one is (or will be) running, so nothing is started.
#: `DELAYED` is an enqueued workflow waiting out a delay — unsettled exactly like `ENQUEUED`.
UNSETTLED_STATUSES = ("PENDING", "ENQUEUED", "DELAYED")

# The registered bodies: kind → the DBOS workflow function, called as `fn(node_id, key_hash, generation)`.
_WORKFLOWS: dict[str, Callable[..., Any]] = {}


def register(kind: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register the workflow body for `kind` (`deliver` / `retire-node`). Applied OUTSIDE
    `@DBOS.workflow()` by `app/durable/nodevault.py`. Registering a second body for a kind is a bug."""
    if kind not in WORKFLOW_KINDS:
        raise ValueError(f"unknown NodeVault grant workflow kind {kind!r}")

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if kind in _WORKFLOWS and _WORKFLOWS[kind] is not fn:
            raise RuntimeError(f"a NodeVault {kind} workflow is already registered")
        _WORKFLOWS[kind] = fn
        return fn

    return decorate


def workflow_id(kind: str, node_id: UUID | str, key_hash: str, generation: int) -> str:
    """`nodevault-<kind>:{node}:{key_hash}:{generation}` — the one spelling of a grant workflow's id."""
    if kind not in WORKFLOW_KINDS:
        raise ValueError(f"unknown NodeVault grant workflow kind {kind!r}")
    return f"nodevault-{kind}:{node_id}:{key_hash}:{int(generation)}"


def kind_for(grant: NodeVaultGrant) -> Optional[str]:
    """The workflow a grant in its current state needs, or None for a settled grant (delivered, failed,
    retired): `failed` is reset by the admin's Retry or `hello`, never re-run on its own."""
    if grant.state == NV_GRANT_WAITING:
        return DELIVER
    if grant.state == NV_GRANT_RETIRE_PENDING:
        return RETIRE_NODE
    return None


def _status_of(wid: str) -> Optional[str]:
    status = DBOS.get_workflow_status(wid)
    return None if status is None else str(status.status)


def _start(kind: str, wid: str, node_id: str, key_hash: str, generation: int) -> None:
    fn = _WORKFLOWS.get(kind)
    if fn is None:
        # The bodies ship with the NodeVault workflows; until they are registered the grant stays as it is
        # and the next `ensure_grants` starts it.
        log.warning("no NodeVault %s workflow is registered; %s not started", kind, wid)
        return
    with SetWorkflowID(wid):
        DBOS.start_workflow(fn, node_id, key_hash, generation)


def start_grant_workflow(session: Session, grant: NodeVaultGrant) -> Optional[str]:
    """Make sure the workflow this grant needs is running; the id it will run under, or None when the grant
    needs none. Bumps `generation` in the caller's transaction when the previous workflow finished without
    settling the grant; the start itself runs after the caller commits."""
    kind = kind_for(grant)
    if kind is None:
        return None
    wid = workflow_id(kind, grant.node_id, grant.key_hash, grant.generation)
    status = _status_of(wid)
    if status in UNSETTLED_STATUSES:
        return None
    if status is not None:
        # Finished, and the grant still wants a workflow: that run is spent, so a fresh id supersedes it.
        grant.generation = int(grant.generation) + 1
        session.flush()
        wid = workflow_id(kind, grant.node_id, grant.key_hash, grant.generation)
    node_id, key_hash, generation = str(grant.node_id), grant.key_hash, int(grant.generation)
    after_commit.on_commit(
        session, lambda: _start(kind, wid, node_id, key_hash, generation), key=f"nodevault-grant:{wid}"
    )
    return wid


def _org_is_set_up_or_setting_up(session: Session, org_id: UUID) -> bool:
    """`set up` = the org has a `current` key (the one definition); a running setup op counts too, its
    `pending` key then being live for enrolment."""
    has_current = exists().where(NodeVaultKey.org_id == org_id, NodeVaultKey.state == NV_KEY_CURRENT)
    setting_up = exists().where(
        NodeVaultOperation.org_id == org_id,
        NodeVaultOperation.kind == "setup",
        NodeVaultOperation.state == NV_OP_RUNNING,
    )
    return bool(session.execute(select(has_current | setting_up)).scalar())


def ensure_grants(session: Session, node_id: UUID) -> list[str]:
    """Enrol `node_id` in every live NodeVault key of its org and (re)start every workflow its grants need
    (module docstring). Idempotent. Writes in the caller's transaction — the caller commits, and the starts
    run after that commit. Returns the workflow ids it scheduled, for logging and tests."""
    # The node row locked first, so two callers enrolling one node (its identity POST racing its `hello`)
    # serialize here instead of colliding on `uq_nodevault_grants_node_key`.
    node = session.scalar(select(Node).where(Node.id == node_id).with_for_update())
    if node is None:
        return []
    grants = list(
        session.scalars(
            select(NodeVaultGrant).where(NodeVaultGrant.node_id == node_id).with_for_update()
        )
    )
    enrollable = bool(session.scalar(select(exists().where(Node.id == node_id, *key_holder_clause()))))
    if not session.scalar(select(exists().where(Node.id == node_id, pool_state.not_a_vnode_host()))):
        # A host never holds a key: whatever it was given before it became one is taken off.
        for grant in grants:
            if grant.state not in (NV_GRANT_RETIRE_PENDING, NV_GRANT_RETIRED):
                grant.state, grant.last_error, grant.blob, grant.blob_kind = NV_GRANT_RETIRE_PENDING, None, None, None
    set_up = enrollable and _org_is_set_up_or_setting_up(session, node.org_id)
    if set_up:
        held = {grant.key_hash for grant in grants}
        # current and previous always; a `pending` key only while the setup or rotation that minted it is
        # running, so one left behind by a failed operation is never delivered.
        in_flight = select(NodeVaultOperation.key_hash).where(
            NodeVaultOperation.org_id == node.org_id,
            NodeVaultOperation.kind.in_(("setup", "rotation")),
            NodeVaultOperation.state == NV_OP_RUNNING,
        )
        live = session.scalars(
            select(NodeVaultKey.key_hash)
            .where(
                NodeVaultKey.org_id == node.org_id,
                NodeVaultKey.state.in_(NODEVAULT_LIVE_KEY_STATES),
                (NodeVaultKey.state != NV_KEY_PENDING) | NodeVaultKey.key_hash.in_(in_flight),
            )
            .order_by(NodeVaultKey.created_at)
        )
        for key_hash in live:
            if key_hash in held:
                continue
            grant = NodeVaultGrant(
                org_id=node.org_id, node_id=node_id, key_hash=key_hash, state=NV_GRANT_WAITING, generation=0
            )
            session.add(grant)
            grants.append(grant)
        session.flush()
    scheduled: list[str] = []
    for grant in grants:
        if grant.state == NV_GRANT_RETIRE_PENDING or (grant.state == NV_GRANT_WAITING and set_up):
            wid = start_grant_workflow(session, grant)
            if wid is not None:
                scheduled.append(wid)
    return scheduled
