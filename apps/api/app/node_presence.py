"""Node presence = the shared socket table.

`node_bus_sockets` says which control-plane peer holds each node's live WebSocket, and that row joined to
a LIVE `cp_peers` row (`app/cp_peers.py`, heartbeat within `PEER_STALE_SECONDS`) is presence: online =
a row on a live peer, offline = no such row. One table answers both "who forwards this command" and "is
this node online", so the two can never disagree, and a peer that dies takes its rows with it by cascade.

Keys are the bus machine key the node presented (text), not a `nodes` foreign key: an unregistered node
is present on the bus and keyed by the value it dialled with; whether it is registered is
`app/node_identity.py`'s question.

Sync functions take a `Session` and do not commit (the caller owns the transaction); the `*_async` twins
open their own session, commit, and hop off the loop with `asyncio.to_thread`, for the WebSocket handlers.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import delete, exists, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import cp_peers
from app.models.control_plane import CpPeer
from app.models.nodes import NodeBusSocket


def _peer_is_live() -> Any:
    return CpPeer.heartbeat_at > func.now() - timedelta(seconds=cp_peers.PEER_STALE_SECONDS)


def record_connected(session: Session, node_id: str, socket_id: Optional[uuid.UUID] = None) -> None:
    """This peer now holds `node_id`'s socket. A node redialling onto ANY peer (this one included)
    displaces the old row (peer, socket, connected_at and last_seen_at all move), so the newest socket
    wins fleet-wide as it did in-process. `socket_id` is the holder's per-connection id
    (`node_bus.NodeConnection.socket_id`); a caller without one (a test seeding presence) gets a fresh one."""
    socket_id = socket_id or uuid.uuid4()
    # This peer's own row first (an idempotent upsert): a process paused past the prune window has had its
    # row deleted by a live peer, and the socket row's foreign key would refuse every connect until the
    # next heartbeat re-registered it. One statement per connect, which is rare, buys that self-healing.
    cp_peers.register(session)
    stmt = pg_insert(NodeBusSocket).values(
        node_id=node_id, peer_id=cp_peers.self_id(), socket_id=socket_id,
        connected_at=func.now(), last_seen_at=func.now(),
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[NodeBusSocket.node_id],
            set_={
                "peer_id": stmt.excluded.peer_id,
                "socket_id": stmt.excluded.socket_id,
                "connected_at": stmt.excluded.connected_at,
                "last_seen_at": stmt.excluded.last_seen_at,
            },
        )
    )


def reassert_connected(session: Session, node_id: str, socket_id: uuid.UUID) -> None:
    """Put back the row for a socket this peer still holds, without evicting a newer holder.

    The peer heartbeat calls this for every local socket (`cp_peers.heartbeat_tick`): a peer pruned as
    dead during a DB outage had its rows cascaded away, and nothing else re-creates them while the
    socket lives. The upsert wins only over a row this peer wrote or a row whose peer is no longer live —
    a node that meanwhile redialled onto another live peer keeps that newer row."""
    stmt = pg_insert(NodeBusSocket).values(
        node_id=node_id, peer_id=cp_peers.self_id(), socket_id=socket_id,
        connected_at=func.now(), last_seen_at=func.now(),
    )
    # CORRELATED to the conflicting row BY NAME. Naming `NodeBusSocket.peer_id` here puts `node_bus_sockets`
    # in the subquery's own FROM (and `.correlate()` does not reach into an INSERT's ON CONFLICT clause),
    # so the EXISTS asked "does ANY socket row sit on ANY live peer" — true whenever a second node is
    # online, and the take-over branch never fired (found by review). The literal column is Postgres's
    # spelling for the existing row inside ON CONFLICT DO UPDATE ... WHERE, and only `cp_peers` is scanned.
    live_holder = select(CpPeer.id).where(
        CpPeer.id == literal_column("node_bus_sockets.peer_id"), _peer_is_live()
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[NodeBusSocket.node_id],
            set_={
                "peer_id": stmt.excluded.peer_id,
                "socket_id": stmt.excluded.socket_id,
                "last_seen_at": stmt.excluded.last_seen_at,
            },
            where=or_(NodeBusSocket.peer_id == cp_peers.self_id(), ~exists(live_holder)),
        )
    )


# A row this peer wrote must be at least this old before `retract_lost` may delete it: the handler
# registers the socket on the loop and records the row from a worker thread, so a socket that arrived
# after the heartbeat's snapshot can have a row younger than one interval. Two intervals is past any
# such hop; a row wrongly kept meanwhile is retracted on the next tick.
RETRACT_GRACE_SECONDS = 2 * cp_peers.HEARTBEAT_INTERVAL_SECONDS


def retract_lost(session: Session, held_socket_ids: list[uuid.UUID]) -> int:
    """Delete this peer's rows for sockets it no longer holds. Returns how many went.

    The close-time delete (`record_disconnected`) is best effort: a DB blip at teardown, or a connect
    hop committing after the disconnect hop's delete, leaves a row naming a live peer for a socket that
    is gone — which presence reads as ONLINE for this peer's whole life, and dispatch then forwards to
    it and 503s. The heartbeat calls this with the sockets the process actually holds (a snapshot taken
    on the loop, `cp_peers._heartbeat_forever`) so a lost row is gone within one interval."""
    stmt = delete(NodeBusSocket).where(
        NodeBusSocket.peer_id == cp_peers.self_id(),
        NodeBusSocket.connected_at < func.now() - timedelta(seconds=RETRACT_GRACE_SECONDS),
    )
    if held_socket_ids:
        stmt = stmt.where(NodeBusSocket.socket_id.not_in(held_socket_ids))
    return session.execute(stmt).rowcount


def record_disconnected(session: Session, node_id: str, socket_id: Optional[uuid.UUID] = None) -> bool:
    """`node_id`'s socket closed. Deletes only the row THAT socket wrote (`socket_id`), so a displaced
    socket's late close — on another peer, or on this same peer after a quick redial — cannot evict the
    new holder's row. Without a `socket_id` (a test tearing down what it seeded) it deletes whatever row
    this peer holds. True iff a row went."""
    owned = (
        NodeBusSocket.socket_id == socket_id if socket_id is not None
        else NodeBusSocket.peer_id == cp_peers.self_id()
    )
    result = session.execute(delete(NodeBusSocket).where(NodeBusSocket.node_id == node_id, owned))
    return result.rowcount > 0


def touch(session: Session, node_id: str) -> None:
    """A heartbeat arrived on this peer's socket for `node_id`: refresh `last_seen_at` (only on the row
    this peer holds)."""
    session.execute(
        update(NodeBusSocket)
        .where(NodeBusSocket.node_id == node_id, NodeBusSocket.peer_id == cp_peers.self_id())
        .values(last_seen_at=func.now())
    )


def holder_of(session: Session, node_id: str) -> Optional[tuple[uuid.UUID, str]]:
    """`(peer_id, address)` of the live peer holding `node_id`'s socket, or `None` — a row on a stale
    peer is not a holder."""
    row = session.execute(
        select(NodeBusSocket.peer_id, CpPeer.address)
        .join(CpPeer, CpPeer.id == NodeBusSocket.peer_id)
        .where(NodeBusSocket.node_id == node_id, _peer_is_live())
    ).first()
    return None if row is None else (row[0], row[1])


def connected_node_ids(session: Session) -> list[str]:
    """Every node whose socket sits on a live peer, sorted."""
    return list(
        session.scalars(
            select(NodeBusSocket.node_id)
            .join(CpPeer, CpPeer.id == NodeBusSocket.peer_id)
            .where(_peer_is_live())
            .order_by(NodeBusSocket.node_id)
        )
    )


def snapshot(session: Session) -> list[dict[str, Any]]:
    """`[{"node_id", "status": "online", "last_seen"}]` for every node on a live peer, sorted by node id
    — the shape the in-process presence registry's `snapshot()` had. `last_seen` is ISO-8601 UTC."""
    rows = session.execute(
        select(NodeBusSocket.node_id, NodeBusSocket.last_seen_at)
        .join(CpPeer, CpPeer.id == NodeBusSocket.peer_id)
        .where(_peer_is_live())
        .order_by(NodeBusSocket.node_id)
    ).all()
    return [
        {
            "node_id": node_id,
            "status": "online",
            "last_seen": last_seen_at.astimezone(timezone.utc).isoformat(),
        }
        for node_id, last_seen_at in rows
    ]


# ── Off-loop twins ──────────────────────────────────────────────────────────────────────────────────


def _committed(fn: Callable[..., Any], *args: Any) -> Any:
    """Run one sync function in its own committed session (a worker thread's whole unit of work)."""
    from app.db import SessionLocal

    with SessionLocal() as session:
        result = fn(session, *args)
        session.commit()
        return result


async def record_connected_async(node_id: str, socket_id: Optional[uuid.UUID] = None) -> None:
    await asyncio.to_thread(_committed, record_connected, node_id, socket_id)


async def record_disconnected_async(node_id: str, socket_id: Optional[uuid.UUID] = None) -> bool:
    return await asyncio.to_thread(_committed, record_disconnected, node_id, socket_id)


async def holder_of_async(node_id: str) -> Optional[tuple[uuid.UUID, str]]:
    return await asyncio.to_thread(_committed, holder_of, node_id)


async def connected_node_ids_async() -> list[str]:
    return await asyncio.to_thread(_committed, connected_node_ids)
