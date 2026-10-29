"""Node presence, written by the node-bus frame path. Always on.

Nodes dial in on `WS /api/node-bus`. Presence is a `node_bus_sockets` row joined to a live
`cp_peers` row (`app/node_presence.py`): `app/routes/node_bus.py` calls `record_ws_connected` once the
socket is registered (the online transition), `record_ws_presence` on every `hello`/`heartbeat`, and
`record_ws_disconnect` on close. The row is written by the peer that holds the socket and read by any
task — `GET /api/nodes`, `GET /api/health/components`, the command router — so N control-plane tasks
agree on who is online. A dead peer's rows fall out through the `cp_peers` join, never by a sweep here.
A half-open socket surfaces in ~40s via the websocket keep-alive (`apps/api/entrypoint.sh`).

Offline is a measurement; `enabled` is a declaration this path never writes, and `app/lease_drain.py`
decides liveness from `worktrees_updated_at`, not this status. A frame is an observation, not an
authority: it cannot mint a `nodes` or `worktrees` row (`app/node_identity.py::is_registered_node`),
change an operator-set column, or execute anything. `hello`'s `node_id` must match its per-node
credential (`app/node_credentials.py`).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from app import node_presence
from app.clock import as_utc
from app.db import SessionLocal
from app.events import emit_console_event
from app.models.nodes import Node
from app.node_identity import ids_by_wire_value, node_org
from app.tenancy import seam

log = logging.getLogger("jaah-discovery")

# The frame path refreshes `nodes.last_seen` and the socket row's `last_seen_at` at most once per node per
# this window, checked before any DB touch.
_LAST_SEEN_REFRESH_INTERVAL = 60.0
# node_id -> monotonic timestamp of its last due refresh. Per-process on purpose: a node's socket lives in
# exactly one process, so the only frames for it arrive here, and a second process holding no socket for
# it has no tick to debounce. Rebuilt on restart at the cost of one early refresh.
_last_seen_refresh: dict[str, float] = {}

# How long a node seen on the bus moments ago still reads `online` with no live socket. Every CP deploy
# drops every node socket and the fleet redials within seconds (`apps/jaahd/node_bus.py`), so without
# this the console flickered the whole fleet Offline on each deploy. Display only: the command router,
# NodeVault and every "can I deliver now" read stay on the exact socket row (`app/node_presence.py`).
RECONNECT_GRACE_SECONDS = 15.0
# The end-of-grace NOTIFY fires this much after the grace, so its refetch lands past the boundary.
_GRACE_NOTIFY_SLACK_SECONDS = 1.0
# The pending end-of-grace NOTIFYs, held so the loop cannot garbage-collect one before it fires.
_grace_notifies: set[asyncio.Task] = set()


def presence_map(session: Session) -> dict[str, dict[str, Optional[str]]]:
    """The live presence snapshot, keyed by the row's uuid (`str(nodes.id)`), as every node read keys.

    Never by name: names are unique per org only, so two orgs' same-named nodes would share one entry.
    Socket rows are keyed by machine key (`node_identity.bus_key`), resolved once here so a registered node
    keyed any other way still lands on its row.

    One node can hold two keys (a misprovisioned daemon dialling under both spellings), so the merge picks
    the live one: online first, then later `last_seen`. An entry resolving to no row keeps its wire value,
    which keeps the presence-only union working."""
    entries = node_presence.snapshot(session)
    ids = ids_by_wire_value(session, [entry["node_id"] for entry in entries])
    merged: dict[str, dict[str, Optional[str]]] = {}
    for entry in entries:
        resolved = ids.get(str(entry["node_id"]))
        key = str(resolved) if resolved is not None else entry["node_id"]
        if _is_fresher(entry, merged.get(key)):
            merged[key] = entry
    return merged


def _is_fresher(
    entry: dict[str, Optional[str]], held: Optional[dict[str, Optional[str]]]
) -> bool:
    """Whether `entry` should displace `held` as one node's presence: online first, then `last_seen`.

    Only reachable when two keys resolve to one node. `last_seen` is ISO-8601 UTC, so lexicographic order
    is chronological; None sorts oldest."""
    if held is None:
        return True
    if (entry.get("status") == "online") != (held.get("status") == "online"):
        return entry.get("status") == "online"
    return (entry.get("last_seen") or "") >= (held.get("last_seen") or "")


def _reset_roster_state_for_tests() -> None:
    """Test-only: clear the in-process last-seen debounce between tests."""
    _last_seen_refresh.clear()


def status_and_last_seen(
    entry: Optional[dict[str, Optional[str]]], persisted: Optional[datetime],
    now: Optional[datetime] = None,
) -> tuple[str, Optional[str]]:
    """The `{status, last_seen}` tri-state one node reads as; the single home for `GET /api/nodes` and
    `GET /api/health/components`, so the badge and the list cannot disagree.

    `entry` is the node's `node_presence.snapshot` row (a live socket) or None; `persisted` is its
    `nodes.last_seen` (None for a presence-only host or a row never seen on the bus).

    - `online`: a live socket; `last_seen` is the socket row's, the fresher of the two.
    - `online` too: no live socket, but `persisted` is within `RECONNECT_GRACE_SECONDS` of `now` — a node
      whose socket just closed (the close stamps `last_seen`, `record_ws_disconnect`) and is redialling.
    - `offline`: no live socket, but the fleet HAS seen this node — a socket row is deleted on close, so
      absence is the offline signal, not a remembered status. `last_seen` is the persisted value.
    - `unknown`: no live socket and never seen (`persisted` None). Never an implicit `offline`: the console
      greys worktrees and disables reach-dependent controls on `offline` only, and a node registered but
      not yet dialled in has nothing to grey (`apps/web/src/types.ts::NodeRecord.status`)."""
    if entry is not None:
        return str(entry["status"]), entry["last_seen"]
    if persisted is None:
        return "unknown", None
    seen = as_utc(persisted)
    if ((now or datetime.now(timezone.utc)) - seen).total_seconds() < RECONNECT_GRACE_SECONDS:
        return "online", persisted.isoformat()
    return "offline", persisted.isoformat()


def _sync_roster(node_id: str) -> bool:
    """Off-loop DB work (via `asyncio.to_thread`) for the due tick: refresh `nodes.last_seen` and touch
    this peer's socket row, one session, one commit.

    No row is ever inserted (registration is CRUD). Returns False when `node_id` has no `nodes` row, for
    the caller's log. Restricted to nodes this peer holds: the callers are the frame path, so a frame
    for `node_id` means this peer holds a socket for it, and the close path, which stamps the moment this
    peer last held one (its row is already gone, so `touch` updates nothing). `node_presence.touch`
    updates only this peer's row (a displaced row is another peer's and stays untouched). `nodes.last_seen`
    means "last seen online" and is still true when displaced — the node is online, on the other peer."""
    # Fleet-wide plane: the node-bus handler binds no org and `to_thread` copies that unbound context, so
    # without this every node resolves as unregistered and `last_seen` is never written. The keys are
    # machine keys, not tenant choices. `detached()` so a future org-bound caller does not make
    # `operator_scope()` raise inside a heartbeat.
    with seam.detached(), seam.operator_scope():
        session = SessionLocal()
        try:
            # The wire resolver: a machine key, and a name-only lookup would report every node
            # as unregistered.
            registered = ids_by_wire_value(session, [node_id])
            if node_id in registered:
                session.execute(
                    update(Node)
                    # Keyed on the resolved id so a uuid-spelled key matches its row.
                    .where(Node.id == registered[node_id])
                    .values(last_seen=datetime.now(timezone.utc))
                )
            node_presence.touch(session, node_id)
            session.commit()
            return node_id in registered
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


async def _refresh_last_seen(node_id: str) -> None:
    """The due tick: `_sync_roster`, at most once per `_LAST_SEEN_REFRESH_INTERVAL`.

    The debounce precedes any DB touch, keeping the heartbeat path DB-free on all but the due tick; the
    due call hops to a worker thread so a saturated pool cannot block the serving loop. Creates no row.
    Required: `app/node_liveness.py` alarms on a stale `last_seen`; this and the close's stamp
    (`record_ws_disconnect`) are its only writers.
    """
    mono = time.monotonic()
    if (mono - _last_seen_refresh.get(node_id, 0.0)) < _LAST_SEEN_REFRESH_INTERVAL:
        return
    # Stamped before the hop: two frames in one tick could both pass the check; a lost refresh costs a minute.
    _last_seen_refresh[node_id] = mono
    try:
        registered = await asyncio.to_thread(_sync_roster, node_id)
    except Exception as exc:
        # A transient DB error must not break the handshake; the next due tick retries.
        log.warning("roster refresh: could not refresh last_seen for %s (%s)", node_id, exc)
        return
    if not registered:
        log.warning(
            "roster refresh: %s is present on the bus but NOT registered, so nothing was persisted for "
            "it — register with POST /api/nodes if real, otherwise its NODE_ID is misconfigured and it "
            "is reporting under a name nobody declared", node_id,
        )


async def record_ws_connected(node_id: str, socket_id: Optional[uuid.UUID] = None) -> None:
    """The socket is registered on this peer: write the presence row, then NOTIFY. The online transition.

    Raises when the row cannot be written: a socket this peer holds that no row names is the split-brain
    the design forbids (presence lives in the database) — the console would call the
    node offline and the command router would find no holder. The caller lets the handshake fail so the
    node reconnects, rather than serving a socket nobody can route to. The NOTIFY stays best-effort.
    """
    await node_presence.record_connected_async(node_id, socket_id)
    await _notify_node_change_off_loop(node_id)


async def record_ws_presence(node_id: str) -> None:
    """A `hello` or `heartbeat` frame arrived on a socket this peer holds.

    No DB touch except on the due tick (`_refresh_last_seen`), and that hops to a worker thread: a DB
    call on the loop can block it for a saturated pool's `pool_timeout` and starve the health probe,
    while threading the whole function would queue heartbeats ahead of `reply` frames. (`to_thread` runs on the
    serving loop's default executor, which DBOS points at its own long-lived pool.) Online is recorded at `record_ws_connected`,
    so no transition is decided here and no NOTIFY is owed."""
    await _refresh_last_seen(node_id)


async def record_ws_disconnect(node_id: str, socket_id: Optional[uuid.UUID] = None) -> None:
    """The socket closed: delete the presence row THAT socket wrote, and — only if one went — stamp
    `nodes.last_seen` and NOTIFY once `RECONNECT_GRACE_SECONDS` have passed.

    The stamp is what holds the node `online` through its redial (`status_and_last_seen`); a NOTIFY now
    would refetch that same `online`, so the refetch is owed at the grace's end, when a node that did not
    come back first reads `offline`. A node that does come back NOTIFYs at its connect.

    A displaced socket's late close must not announce offline: when a node reconnects — to another peer
    or to this one — before this socket's close is seen, the row already names the new socket,
    `record_disconnected` deletes nothing (it is keyed on node AND `socket_id`), and the node stays
    online where it is. Only a delete that actually removed this socket's row is an offline transition
    worth a NOTIFY. `socket_id` is `NodeConnection.socket_id`; without one the delete falls back to
    "this peer's row" (tests).

    Never raises on a DB fault: the caller is `app/routes/node_bus.py`'s `finally`, and a stale row costs
    only until the node's next connect upserts it or this peer dies (`cp_peers` join). That `finally` may
    run under cancellation, which propagates (it must), but the delete is shielded so it still lands: an
    unshielded `to_thread` whose work item is still queued is dropped with the await, and the in-process
    offline write this replaces always landed. Order: the caller has already run `registry.unregister`,
    so a command to this node on this peer gets an immediate 503.
    """
    log.info("jaah-discovery: %s node-bus socket closed — releasing its presence row", node_id)
    try:
        went = await asyncio.shield(node_presence.record_disconnected_async(node_id, socket_id))
    except Exception as exc:
        log.warning(
            "jaah-discovery: could not release %s's presence row (%s) — it reads online until its next "
            "connect or this peer's expiry", node_id, exc,
        )
        return
    if not went:
        return
    try:
        _last_seen_refresh[node_id] = time.monotonic()
        await asyncio.to_thread(_sync_roster, node_id)
    except Exception as exc:
        # Without the stamp the node reads offline at once — the old flicker, not a fault.
        log.warning("jaah-discovery: could not stamp %s's last_seen at close (%s)", node_id, exc)
    task = asyncio.ensure_future(_notify_after_grace(node_id))
    _grace_notifies.add(task)
    task.add_done_callback(_grace_notifies.discard)


async def _notify_after_grace(node_id: str) -> None:
    """The end-of-grace refetch hint. Lost if this peer stops first (a deploy); the node's redial to the
    successor NOTIFYs at its connect, and a node that never redials reads `offline` on the next fetch."""
    await asyncio.sleep(RECONNECT_GRACE_SECONDS + _GRACE_NOTIFY_SLACK_SECONDS)
    await _notify_node_change_off_loop(node_id)


async def _notify_node_change_off_loop(node_id: str) -> None:
    """`_notify_node_change`, off the event loop and best-effort; the one home for both properties.

    Off the loop because it opens a session. Best-effort because the NOTIFY is only a refresh hint and an
    escaping raise could break a caller that matters. The swallow's cost is a stale UI, not a delay: the
    callers fire only on a real transition and the console polls only while SSE is down, so a lost NOTIFY
    can leave a node's status stale until the next transition or page load."""
    try:
        await asyncio.to_thread(_notify_node_change, node_id)
    except Exception as exc:
        log.warning(
            "jaah-discovery: node-status NOTIFY failed (%s) — the console may show a stale node status "
            "until the next event; presence tracking continues", exc,
        )


def _notify_node_change(node_id: str) -> None:
    """Best-effort `cockpit_events` NOTIFY so the browser SSE relay refetches GET /api/nodes. `id=0` is
    the established pattern for a node-scoped event — node status has no relational PK to key off.

    Tagged with the node's org so only that org and the operator are woken; an unstamped node
    goes to the operator plane only. Runs under `detached(), operator_scope()` because the inherited
    context binds no org and an unbound read would resolve no node. Blocking: async callers go through
    `_notify_node_change_off_loop`."""
    with seam.detached(), seam.operator_scope():
        session = SessionLocal()
        try:
            emit_console_event(
                session, entity="node", id=0, action="upserted", org_id=node_org(session, node_id),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
