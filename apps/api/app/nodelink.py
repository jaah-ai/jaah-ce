"""NodeLink's legacy single-share wire, and telling a node to converge its NodeLinks.

What a node should mount is served over its own token at `GET /api/agent/nodelinks` (`app/nodelinks.py::
agent_answer`); the node's root mount unit (`jaah-shared-mount converge`) fetches it at daemon start
and whenever the CP sends the `nodelink_converge` verb (`request_converge`). The verb carries no arguments: the
node reads its desired state itself, so no request input reaches a root `mount`.

`GET /api/agent/nodelink` (`desired_for_node`) is the OLD single-share wire, kept for packages that predate
the list: it answers from the node's attachment to a ready NodeLink at `{jaah-home}/auth/.shared`
(`nodelinks.ACCOUNTS_MOUNT_PATH`), with that NodeLink's own store and access point.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features, tenant_defaults
from app.models.nodelinks import NodeNodeLink
from app.models.nodes import Node

log = logging.getLogger("app.nodelink")

#: The node-bus verb that wakes a node's NodeLink converge (`apps/jaahd`); no arguments.
CONVERGE_COMMAND = "nodelink_converge"


def desired_for_node(session: Session, node: Node) -> dict[str, Any]:
    """What `node` should mount on the old single-share wire: `{enabled, efs_id, access_point_id, region,
    mount_ip}`, `enabled` tri-state. `True` with the ids when the node is attached to a ready NodeLink at
    `nodelinks.ACCOUNTS_MOUNT_PATH` (`nodelinks.desired_for_node`); `None` ("no opinion": the node keeps its
    own jaahd.env share, e.g. a hand-configured one) when its org may use NodeLink but has no ready store;
    else `False` (authoritative: unmount). The ids are served only with `True`."""
    off: dict[str, Any] = {
        "enabled": False, "efs_id": None, "access_point_id": None, "region": None, "mount_ip": None,
    }
    if not edition_features.enabled("nodelink"):
        return off  # The community edition has no NodeLink.
    from app import nodelinks

    if not tenant_defaults.shared_store_allowed(session, node.org_id):
        return off
    entry = next(
        (e for e in nodelinks.desired_for_node(session, node)
         if e["mount_path_template"] == nodelinks.ACCOUNTS_MOUNT_PATH),
        None,
    )
    if entry is None:
        return off if nodelinks.ready_stores(session, node.org_id) else {**off, "enabled": None}
    return {
        "enabled": True,
        "efs_id": entry["efs_id"],
        "access_point_id": entry["access_point_id"],
        "region": entry["region"],
        "mount_ip": entry["mount_ip"],
    }


def request_converge(session: Session, node_ids: Iterable[UUID], *, actor: str = "system") -> int:
    """Send `nodelink_converge` to each of `node_ids`, best effort; returns how many were sent.

    Call after the caller's commit: the node reads its desired state back over its token. A co-located
    node is never sent the verb (it refuses it): its HOST mounts its NodeLinks, so the host's converge loop
    is nudged instead (`vnodes.nudge_host`). A failure is logged
    and never raised: an offline node converges at its next boot, when jaahd drops the same marker. A failed
    send rolls the session back, so the caller's own (already committed) answer still reads cleanly.

    Each node is addressed by its uuid, never its name: names repeat across orgs and never resolve, so the
    command would reach no socket and its audit row would not take the node's org."""
    if not edition_features.enabled("nodelink"):
        return 0  # The community edition has no NodeLink.
    from app import vnodes

    # Deferred: `app.routes.commands` imports most of the app.
    from app.routes.commands import dispatch_and_audit_sync

    wanted = sorted({node_id for node_id in node_ids if node_id is not None})
    if not wanted:
        return 0
    names = session.execute(select(Node.id, Node.name).where(Node.id.in_(wanted))).all()
    sent = 0
    for node_id, name in names:
        host_node_id = vnodes.host_of(node_id)
        if host_node_id is not None:
            try:
                vnodes.nudge_host(host_node_id)
                sent += 1
            except Exception:  # noqa: BLE001 — best effort; the host's next poll applies it
                log.warning("nodelink: could not nudge the host of node %s", name, exc_info=True)
            continue
        try:
            dispatch_and_audit_sync(session, str(node_id), CONVERGE_COMMAND, {}, actor)
            sent += 1
        except Exception:  # noqa: BLE001 — best effort by contract; the node's own start converges it
            session.rollback()
            log.warning("nodelink: could not send or audit %s to node %s", CONVERGE_COMMAND, name, exc_info=True)
    return sent


def converge_nodes_of_orgs(session: Session, org_ids: Iterable[UUID], *, actor: str = "system") -> int:
    """Re-converge every node of `org_ids` attached to a NodeLink, after an org's effective NodeLink term
    changed (its override, its plan, or its plan's `nodelink_allowed`). Best effort, after the commit."""
    wanted = sorted(set(org_ids))
    if not wanted:
        return 0
    node_ids = session.scalars(
        select(NodeNodeLink.node_id).where(NodeNodeLink.org_id.in_(wanted)).distinct()
    ).all()
    return request_converge(session, node_ids, actor=actor)
