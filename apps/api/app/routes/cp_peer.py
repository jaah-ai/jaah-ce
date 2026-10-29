"""The control plane's INTERNAL peer surface — one API task driving a node socket another task holds.

Why it exists: a node's WebSocket lives in exactly one process (`app/node_bus.py`), but with N ECS tasks a
command may land on any of them. The non-holder forwards to the holder here (`app/cp_peers.py` is the
client half; placement is `app/node_presence.py`), so every task answers correctly and a deploy overlap
no longer 503s. Three legs: a correlated request, a reply-less notify, and the owner leg of a proxied
console dial-back.

WHO MAY CALL: only another control-plane task, proving itself with the CP's own `cp_self` token
(`cp_peers.self_token`), which `routes/agent/auth.py::_check_token` resolves to a caller that derives NO node
(`AgentCaller.is_derived` is False). A NODE's per-node token is refused with 403 even though it
authenticates on the agent surface: these routes address arbitrary node ids, so a node that could call
them could drive every other node's socket. `Tenancy.OPERATOR`, and honestly so — the caller is the
fleet's own control plane, and a forwarded command is already authorized on the task that forwards it.

Every leg uses `registry.local`, never `registry.route`: a forwarded call must terminate on the holder,
or two peers with a stale placement row could bounce it between them. Under `/api/agent/` so the
NODE_TOKEN gate and the INFRA path rule (`test_authz_policy.py`) cover it and no ALB or nginx rule
changes; the ALB never routes a peer's call — a peer dials the task's own address directly.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import node_bus, node_credentials
from app.db import get_session
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.node_bus import FRAME_CMD, registry, resolve_caller_async
from app.routes.agent.auth import _check_token

log = logging.getLogger("app.routes.cp_peer")

# No router prefix: the two POST paths are what `app/cp_peers.py` posts to.
router = APIRouter(tags=["cp-peer"])

_NOT_A_PEER = "only a control-plane peer may drive another task's node sockets"


def _peer_only(
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> node_credentials.AgentCaller:
    """`_check_token`, then the refusal every leg shares: a caller whose credential names a node is a
    node, not a peer.

    Also hands the request's pooled connection back: `_check_token`'s credential read autobegan a
    transaction that would otherwise pin a connection for the whole forwarded command (up to the node's
    own timeout — 120s for a converge), and a fan-out of forwards would then exhaust this task's pool
    and starve its health probe and its own presence writes. Nothing after this reads the session."""
    if caller.is_derived:
        log.warning("cp-peer: refused node %s — a node token cannot drive other nodes", caller.node_id)
        raise HTTPException(status_code=403, detail=_NOT_A_PEER)
    session.rollback()
    return caller


class ForwardRequestIn(BaseModel):
    node_id: str = Field(min_length=1)
    envelope: dict[str, Any]
    timeout: float = Field(gt=0)
    frame_type: str = FRAME_CMD


class ForwardRequestOut(BaseModel):
    # The reply frame's bytes, decoded — what `node_bus.Reply.data` carries on the forwarding side.
    data: str


class ForwardNotifyIn(BaseModel):
    node_id: str = Field(min_length=1)
    frame: dict[str, Any]


@router.post("/api/agent/cp-peer/node-bus/request", response_model=ForwardRequestOut)
@infra(Tenancy.OPERATOR)
async def forward_request(
    body: ForwardRequestIn,
    _: node_credentials.AgentCaller = Depends(_peer_only),
) -> ForwardRequestOut:
    """Send `envelope` down THIS process's socket for `node_id` and return the correlated reply.

    503 when this process holds no socket for the node (the placement row was stale, or the socket died
    between the peer's read and this call — `NodeNotConnected` either way), 504 when the node is silent
    past `timeout`; the forwarding peer maps them back onto the same two exceptions."""
    conn = registry.local(body.node_id)
    if conn is None:
        raise HTTPException(status_code=503, detail=f"node '{body.node_id}' is not connected to this task")
    try:
        reply = await conn.request(body.envelope, timeout=body.timeout, frame_type=body.frame_type)
    except node_bus.NodeNotConnected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except node_bus.NodeRequestTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    return ForwardRequestOut(data=reply.data.decode("utf-8"))


@router.post("/api/agent/cp-peer/node-bus/notify", status_code=204)
@infra(Tenancy.OPERATOR)
async def forward_notify(
    body: ForwardNotifyIn,
    _: node_credentials.AgentCaller = Depends(_peer_only),
) -> Response:
    """Send one reply-less `frame` down THIS process's socket for `node_id`. 204 once written; 503 when
    no socket here takes it."""
    conn = registry.local(body.node_id)
    if conn is None:
        raise HTTPException(status_code=503, detail=f"node '{body.node_id}' is not connected to this task")
    try:
        await conn.notify(body.frame)
    except node_bus.NodeNotConnected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(status_code=204)
