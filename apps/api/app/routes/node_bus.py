"""Node-bus WebSocket endpoint — nodes dial in here and hold one socket each (see `app/node_bus.py`).

Auth note: `SessionAuthMiddleware` does NOT cover the WebSocket ASGI scope (same caveat as
`app/routes/console.py`), so the NODE_TOKEN gate is enforced in-handler on the `hello` frame.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import time

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app import account_removal, console_direct, edition_features, node_bus
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import SessionLocal
from app.node_bus import (
    FRAME_CONSOLE_DIRECT_USED,
    FRAME_HB_ACK,
    FRAME_HEARTBEAT,
    FRAME_HELLO,
    FRAME_REPLY,
    registry,
    resolve_caller_async,
)
from app.node_identity import bus_identity
from app.jaah_discovery import record_ws_connected, record_ws_disconnect, record_ws_presence
from app.tenancy import seam

if edition_features.enabled("cloud_nodes"):
    from app import cloud_node_enrolled

log = logging.getLogger("app.routes.node_bus")

router = APIRouter()

# Close codes (4xxx = application range), mirroring console.py's convention.
_WS_UNAUTHORIZED = 4401
_WS_BAD_HELLO = 4400
# Closed because this process is handing off (`app/main.py::_lifespan` sweeps every node socket with it):
# redial now, skipping the exponential reconnect ladder. There is deliberately no "not the leader" code —
# any container may hold a node socket, and a command landing on another one is forwarded to the holder
# (`app/node_bus.py`). This line is the single home of the number; the node-side copy (like
# `BUS_SCHEMA_VERSION`) points back here.
_WS_HANDOFF = 4504

# A node must complete its `hello` within this window, so a socket that connects and says nothing cannot
# pin a slot indefinitely.
HELLO_TIMEOUT_SECONDS = 10.0


@router.websocket("/api/node-bus")
@infra(Tenancy.ORG_SCOPED)
async def node_bus_ws(websocket: WebSocket) -> None:
    # Any container may hold a node socket; it stays until that container goes away, then redials. Which
    # container holds it is recorded (`record_ws_connected` -> `app/node_presence.py`), so a command landing
    # on another container is forwarded to the holder (`app/node_bus.py`) rather than answered 503.
    await websocket.accept()

    wire_id = await _handshake(websocket)
    if wire_id is None:
        return

    # Keyed by the machine key, not the wire spelling: every lookup names the node by NAME while
    # `node.env` may present the uuid, and a dict applies no shim. Resolved once here (a DB probe per
    # heartbeat would cost a round trip for an answer that cannot change while the socket is open).
    node_id, name = await asyncio.to_thread(_identity, wire_id)

    conn = registry.register(node_id, websocket)
    # The `try` opens on the registration: the `finally` is the only release, and cancellation during
    # the presence calls would otherwise skip `registry.unregister`. `record_ws_connected` RAISES when the
    # placement row cannot be written (a held socket no row names is a split brain):
    # the `except` below then closes the socket so the node redials rather than sits on a dead session.
    # Awaited, not threaded: it keeps dict writes on the loop and hops only the DB work to a worker; a
    # bare `SessionLocal()` here would block the loop on a saturated pool, starving `GET /api/health`.
    try:
        # Placement first: until the row names this process, a peer routes commands for this node nowhere.
        await record_ws_connected(node_id, conn.socket_id)
        await record_ws_presence(node_id)
        log.info("node-bus: %s connected%s", name, "" if node_id == wire_id else f" (presented {wire_id!r})")
        # After the presence commit, off the frame loop: NodeVault enrolment and its wake-ups for this node
        # (`app/durable/nodevault.py::on_node_online`). Never fatal, never awaited by the socket.
        _spawn_node_online(node_id)
        await _read_forever(websocket, node_id, name, conn)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # `exc_info` because `str(exc)` alone is empty for a bare `TimeoutError`.
        log.warning("node-bus: %s session error: %s", name, exc, exc_info=True)
        # Explicit close (1011): a returned handler leaves the close to the server, which a test client
        # never sends — and the node reads a stated code as "redial now" sooner than a silent drop.
        await _close(websocket, 1011, "session error")
    finally:
        registry.unregister(node_id, conn)
        # Logged before the await, which may never return: under cancellation `record_ws_disconnect`'s
        # thread hop re-raises `CancelledError`. The synchronous registry work above always lands.
        log.info("node-bus: %s disconnected", name)
        await record_ws_disconnect(node_id, conn.socket_id)


# NodeVault's node-online pass: at most one per node per window, on a small pool of its own, so a node that
# redials in a loop can neither pin the shared `to_thread` pool nor wake its org's workflows on every dial. Short,
# because a delivery that found the node offline inside the window waits for its re-check instead of the wake.
NODE_ONLINE_DEBOUNCE_SECONDS = 5.0
_node_online_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="nodevault-online")
_node_online_last: dict[str, float] = {}
_owed_removals_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="account-removals")
# Strong references to the in-flight futures: the loop keeps only weak ones.
_node_online_tasks: set[asyncio.Future] = set()


def _spawn_node_online(node_id: str) -> None:
    from app.durable import nodevault as durable_nodevault

    mono = time.monotonic()
    if mono - _node_online_last.get(node_id, float("-inf")) < NODE_ONLINE_DEBOUNCE_SECONDS:
        return
    _node_online_last[node_id] = mono
    future = asyncio.get_running_loop().run_in_executor(
        _node_online_pool, durable_nodevault.on_node_online, node_id,
    )
    _node_online_tasks.add(future)
    future.add_done_callback(_node_online_tasks.discard)
    # The account removals it still owes (`app/account_removal.py`), on a pool of their own: a node's round may take
    # minutes, and must not hold up NodeVault's.
    owed = asyncio.get_running_loop().run_in_executor(_owed_removals_pool, account_removal.settle, node_id)
    _node_online_tasks.add(owed)
    owed.add_done_callback(_node_online_tasks.discard)
    _spawn_after_enrolled(node_id)


def _spawn_after_enrolled(node_id: str) -> None:
    """A Your Cloud node's first-connect hook (`app/cloud_node_enrolled.py::run`), on the node-online pool."""
    if not edition_features.enabled("cloud_nodes"):
        return  # The community edition has no Your Cloud node.
    future = asyncio.get_running_loop().run_in_executor(_node_online_pool, cloud_node_enrolled.run, node_id)
    _node_online_tasks.add(future)
    future.add_done_callback(_node_online_tasks.discard)


def _identity(wire_id: str) -> tuple[str, str]:
    """`(machine key, name)` for a dialling node — `node_identity.bus_identity` in its own session.

    The machine key keys the socket and presence; the name is for logs and for
    `console_direct.record_used`, whose tokens are minted with the name. The session uses this handler's
    own maker (the WebSocket scope has no request-scoped one).

    Never fatal: an unreachable database must not refuse a dial, so the fallback addresses the node by the
    spelling it presented."""
    # On the fleet-wide plane (`seam.detached`, as `jaah_discovery._sync_roster`): this runs on a
    # `to_thread` worker with no org bound, and under the enforcing read seam the lookup would return zero
    # rows silently, degrading every name to the uuid.
    try:
        with seam.detached(), seam.operator_scope(), SessionLocal() as session:
            return bus_identity(session, wire_id)
    except Exception as exc:  # noqa: BLE001 — see the docstring: a DB fault must not refuse presence
        log.warning("node-bus: could not resolve %r to a machine key (%s) — keying the socket by the "
                    "value the node presented; commands addressed by name may 503 until it redials",
                    wire_id, exc)
        return wire_id, wire_id


async def _handshake(websocket: WebSocket) -> str | None:
    """Validate the first frame as a token-bearing `hello` and return the node id, or close and return
    None. See `app/node_bus.py`'s residual note."""
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        await _close(websocket, _WS_BAD_HELLO, "no hello")
        return None

    try:
        frame = json.loads(raw)
    except ValueError:
        await _close(websocket, _WS_BAD_HELLO, "malformed hello")
        return None

    if not isinstance(frame, dict) or frame.get("t") != FRAME_HELLO:
        await _close(websocket, _WS_BAD_HELLO, "expected hello")
        return None
    caller = await resolve_caller_async(frame.get("token"))
    if caller is None:
        log.warning("node-bus: rejected hello from %r — invalid token", frame.get("node_id"))
        await _close(websocket, _WS_UNAUTHORIZED, "invalid token")
        return None

    node_id = frame.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        await _close(websocket, _WS_BAD_HELLO, "missing node_id")
        return None

    # The credential outranks the claim: a per-node token names its holder, so a mismatched `node_id` is
    # refused. A caller with no derived identity (`cp_self`, local-dev bypass) is taken at its word — no
    # real host's credential lands there. Case-folded, because a hand-provisioned `node.env` may carry an
    # uppercase uuid that authenticates; a raw comparison would fail closed on it.
    if caller.is_derived and node_id.strip().lower() != str(caller.node_id).lower():
        log.warning(
            "node-bus: rejected hello — the credential belongs to node %s but the frame claimed %r; "
            "a node may only dial in as itself",
            caller.node_id,
            node_id,
        )
        await _close(websocket, _WS_UNAUTHORIZED, "node_id does not match the credential")
        return None
    return node_id


async def _read_forever(
    websocket: WebSocket, node_id: str, name: str, conn: node_bus.NodeConnection,
) -> None:
    """Pump this socket's frames. `node_id` is the machine key; `name` is for log lines and the
    name-keyed report — see `_identity`."""
    while True:
        raw = await websocket.receive_text()
        try:
            frame = json.loads(raw)
        except ValueError:
            log.warning("node-bus: %s sent a malformed frame", name)
            continue
        if not isinstance(frame, dict):
            continue

        kind = frame.get("t")
        if kind == FRAME_REPLY:
            # The CP-side parser tolerates the extra `t`/`id` routing keys, so the frame is the reply body.
            conn.resolve(frame.get("id"), json.dumps(frame).encode())
        elif kind == FRAME_HEARTBEAT:
            # Hot path: nothing transitions on a heartbeat, so no database or thread pool — bar a cloud hook
            # that failed on this node's dial, retried from an in-process set (`app/cloud_node_enrolled.py`).
            await record_ws_presence(node_id)
            await _ack_heartbeat(node_id, conn)
            if edition_features.enabled("cloud_nodes") and cloud_node_enrolled.due(node_id):
                _spawn_after_enrolled(node_id)
        elif kind == FRAME_HELLO:
            # A re-hello on a live socket is harmless; treat it as presence.
            await record_ws_presence(node_id)
            await _ack_heartbeat(node_id, conn)
        elif kind == FRAME_CONSOLE_DIRECT_USED:
            # A direct console opened/closed on this node — a report, not a request, and the CP's only
            # view of shell access (`app/console_direct.py`). `node_id` is passed, never read from the
            # frame, so a node can only report about itself.
            console_direct.record_used(name, frame)
        else:
            log.debug("node-bus: %s sent unknown frame type %r", name, kind)


async def _ack_heartbeat(node_id: str, conn: node_bus.NodeConnection) -> None:
    """Answer one presence frame with the CP's own verdict on this socket's registration.

    A successful write only proves the socket takes bytes, so the node learns here (on the same
    transport) whether it has gone unregistered and should re-dial. Never raised: the node treats a
    missing ack as "not told", never as `false`."""
    try:
        await conn.notify({"t": FRAME_HB_ACK, "registered": registry.holds(node_id, conn)})
    except Exception as exc:  # noqa: BLE001 — see the docstring: an unacked heartbeat is not an incident
        log.debug("node-bus: could not ack %s's heartbeat: %s", node_id, exc)


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass
