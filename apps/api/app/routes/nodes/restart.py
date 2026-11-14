"""Hard restart of one node: `POST /api/nodes/{node_id}/restart` — every edition. A Multi-Node Host's restart (every
node on it, or the host itself) is `app/routes/host_restart.py`, cloud-only, on this module's `restart_daemon`.

A node restart is the `node_restart` bus command (`apps/jaahd/restart_ops.py`): the daemon answers, then exits for
its service manager to start it afresh; its runners and sessions keep running. Nothing asks first about running
work — the operator asked for an immediate restart, and no task is ended by it.

Who may restart a node: an org's Owner/Admin their own org's nodes, the operator every org's (`@org_admin`, then
`_writable_node_uuid`'s WRITE check).
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin
from app.authz.principal import AsyncCaller, current_caller
from app.db_async import on_thread
from app.models.nodes import Node
from app.routes.nodes.delete import _writable_node_uuid

router = APIRouter(tags=["nodes"])
log = logging.getLogger("cockpit-nodes")

#: The bus command `apps/jaahd/restart_ops.py` serves.
RESTART_COMMAND = "node_restart"
#: The daemon answers at once and exits after; a connected node silent this long is reported unreachable. Short, so a
#: host's nodes, restarted one after another, stay within one request.
RESTART_TIMEOUT = 8.0

NOT_CONNECTED = "{name} isn't connected, so it can't be restarted."
NOT_RESTARTED = "{name} didn't restart: {reason}"


class RestartOut(BaseModel):
    restarted: list[str] = Field(description="The nodes whose Jaah service was restarted.")
    unreachable: list[str] = Field(description="The nodes that weren't connected, so weren't restarted.")
    skipped: int = Field(description="Nodes on the host that belong to other organizations, left alone.")
    rebooted: bool = Field(description="Whether the host's machine was rebooted.")


def restart_daemon(session: Session, node_id: UUID, actor: str) -> Optional[str]:
    """Send `node_restart` to one node: None once it answered, else why not — `NOT_CONNECTED`'s marker `""` for a node
    off the bus, or the node's or dispatch's own words (a daemon too old to know the verb answers `failed`). Audited
    either way."""
    from app.routes.commands import dispatch_and_audit_blocking
    from app import node_bus

    try:
        reply = dispatch_and_audit_blocking(
            session, str(node_id), RESTART_COMMAND, {}, actor, timeout=RESTART_TIMEOUT,
        )
    except node_bus.NodeNotConnected:
        return ""
    except HTTPException as exc:
        log.info("restart: node %s not restarted (%s)", node_id, exc.detail)
        return "" if exc.status_code == 503 else str(exc.detail)
    if str(reply.get("status")) in ("failed", "refused"):
        raw = reply.get("result")
        result: dict = raw if isinstance(raw, dict) else {}
        return str(result.get("reason") or result.get("stderr") or "it refused the restart. Update the node first.")
    return None


def name_of(session: Session, node_id: UUID) -> str:
    return session.scalar(select(Node.name).where(Node.id == node_id)) or str(node_id)


@router.post("/api/nodes/{node_id}/restart", response_model=RestartOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
@on_thread
def restart_node(session: Session, node_id: str, caller: AsyncCaller = Depends(current_caller)) -> RestartOut:
    """Restart the node's Jaah service now. 503 when it isn't connected."""
    node_uuid = _writable_node_uuid(session, node_id, caller.actor, caller.principal)
    name = name_of(session, node_uuid)
    session.commit()  # the authz lock ends here: never held across the node's round trip
    why = restart_daemon(session, node_uuid, caller.actor.email)
    if why == "":
        raise HTTPException(status_code=503, detail=NOT_CONNECTED.format(name=name))
    if why is not None:
        raise HTTPException(status_code=502, detail=NOT_RESTARTED.format(name=name, reason=why))
    return RestartOut(restarted=[name], unreachable=[], skipped=0, rebooted=False)
