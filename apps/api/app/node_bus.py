"""Node bus over WebSocket — the CP half. Nodes dial in, never the reverse, and hold
one socket carrying `cmd`→`reply` (correlated by `id`), `hello` (registration), close (deregistration)
and `heartbeat`→`hb_ack` (presence). Console PTY bytes use their own socket, to the console relay (`app/console_relay/`), so
heartbeats and commands cannot be starved; only `console_open` rides here.

A `cmd` frame is the JSON command envelope plus `t`/`id`; `routes/node_bus.py::_handshake` checks the
`hello` node id against the per-node credential.

PLACEMENT ACROSS N CONTROL-PLANE TASKS. The registry is per-process for SOCKETS only: a `NodeConnection`
lives in the process that accepted the dial, and no socket ever moves. WHICH process holds a node is the
shared `node_bus_sockets` row (`app/node_presence.py`), written by the socket's own handler. A caller
addresses a node by id through `route`/`request`/`get`; when the socket is local it is used directly,
and when another peer holds it the call is forwarded to that peer over `/api/agent/cp-peer/*`
(`app/routes/cp_peer.py`, client side `app/cp_peers.py`) as a `RemoteNodeConnection`, so N tasks
answer correctly and a deploy overlap no longer 503s wrong-task commands. The peer routes use `local`,
never `route`, so a forward cannot re-forward. The shared placement is attached by the lifespan
(`attach_store`); with none attached the registry is exactly local (unit tests, and
the node daemon's console-direct test, which imports this module stdlib-only — every
app import here is lazy).

The loop affinity is the same across the board: reply futures live on the loop that owns the socket, so
a threadpool caller hops back with `run_on_serving_loop`, and a remote target reports the process's
captured `serving_loop()` so the hop still works.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from app import node_credentials

log = logging.getLogger("app.node_bus")

# Matches `apps/jaahd/commands.py::_BUS_SCHEMA_VERSION`.
BUS_SCHEMA_VERSION = 1

# `cmd` is CP -> node; `reply` answers it; `hello`/`heartbeat` are node -> CP; `hb_ack` is the CP's
# uncorrelated answer to a `heartbeat`.
FRAME_CMD = "cmd"
FRAME_REPLY = "reply"
FRAME_HELLO = "hello"
FRAME_HEARTBEAT = "heartbeat"
# `hb_ack` (CP -> node, no reply) carries `registered`: whether the socket the heartbeat arrived on is the
# one this registry routes to. Per-connection, so a displaced socket learns it routes nowhere, and it
# needs no second transport.
FRAME_HB_ACK = "hb_ack"
# `console_open` (CP -> node, reply expected): attach/create one console PTY and dial back a dedicated
# socket to the console relay redeeming the frame's ticket (`app/console_relay/`).
FRAME_CONSOLE_OPEN = "console_open"
# Direct-console control (`app/console_direct.py`). Unknown types are dropped by old daemons, so the
# grant is request/reply: a silent drop and a hung node look alike, and the CP issues on neither.
# `console_direct_grant` (CP -> node, reply): accept one bearer for one slot until an expiry.
# `console_direct_revoke` (CP -> node, no reply): drop grants for a `session_id` (and slot, if given).
# `console_direct_used` (node -> CP, no reply): a direct console opened/closed.
FRAME_CONSOLE_DIRECT_GRANT = "console_direct_grant"
FRAME_CONSOLE_DIRECT_REVOKE = "console_direct_revoke"
FRAME_CONSOLE_DIRECT_USED = "console_direct_used"

# In-flight reply cap per connection, well above the node's default `node_max_concurrent_commands` (64), whose
# ceiling it is (`operator_settings`).
MAX_PENDING_PER_NODE = 256


class NodeNotConnected(Exception):
    """No live WebSocket for this node; mapped to HTTP 503 by `app/routes/commands.py`. Distinct from a
    timeout: the CP knows instantly nothing is listening."""

    def __init__(self, node_id: str) -> None:
        super().__init__(f"node '{node_id}' is not connected")
        self.node_id = node_id


class HolderRefused(NodeNotConnected):
    """`NodeNotConnected` from a FORWARD that never reached the node: the recorded holder answered 503 (no
    socket there) or could not be connected to at all. The one form a `RemoteNodeConnection` may re-route
    on — a failure after the request was sent (a read error, a timeout) is NOT this, since the node may
    already be executing it and a retry elsewhere could deliver a command twice (`app/cp_peers.py`)."""


class NodeRequestTimeout(Exception):
    """The node is connected but produced no reply within the deadline; mapped to HTTP 504."""

    def __init__(self, node_id: str, timeout: float) -> None:
        super().__init__(f"node '{node_id}' did not reply within {timeout}s")
        self.node_id = node_id
        self.timeout = timeout


class Reply:
    """Reply wrapper exposing `data`, the only attribute `_dispatch`'s `json.loads(reply.data)` reads."""

    __slots__ = ("data",)

    def __init__(self, data: bytes) -> None:
        self.data = data


# The API's main loop, captured by `app/main.py`'s lifespan (`capture_serving_loop`). The single home
# of the process loop for every threadpool caller that must schedule a socket write onto it
# (`app/node_send_intent.py`, `run_on_serving_loop` for a remote target). `None` outside a running app or
# after shutdown, which every caller reads as "not connected"/"drop", never as an error.
_serving_loop: Optional[asyncio.AbstractEventLoop] = None


def capture_serving_loop() -> None:
    """Pin the running loop as the one node sockets are served on. Called from the lifespan, since a
    FastAPI worker thread has no loop to ask."""
    global _serving_loop
    _serving_loop = asyncio.get_running_loop()


def release_serving_loop() -> None:
    """Forget the loop on shutdown so a late scheduling drops instead of hitting a closing loop.
    Idempotent."""
    global _serving_loop
    _serving_loop = None


def serving_loop() -> Optional[asyncio.AbstractEventLoop]:
    """The captured process loop, or `None` when there is none (tests, shutdown)."""
    return _serving_loop


def resolve_caller(provided: object) -> node_credentials.AgentCaller | None:
    """Authenticate one node's presented bearer and say which node it is, or `None`.

    The rule is `app/node_credentials.py::resolve`'s; this adds a session for the WebSocket handlers,
    which sit outside FastAPI's dependency graph.

    Fails closed on a database fault, unlike `app/routes/node_bus.py::_identity`: presence may survive an
    outage, but an uncheckable credential must not become an unauthenticated command surface.
    """
    try:
        # Imported lazily: `app.db` builds the engine at import, and
        # the node daemon's console-direct test imports this module stdlib-only.
        from app import node_credentials
        from app.db import IdentitySessionLocal

        with IdentitySessionLocal() as session:
            return node_credentials.resolve(session, provided)
    except Exception as exc:  # noqa: BLE001 — see the docstring: an unprovable caller is refused
        log.warning("node-auth: could not resolve a presented bearer (%s) — refusing the dial", exc)
        return None


async def resolve_caller_async(provided: object) -> node_credentials.AgentCaller | None:
    """`resolve_caller`, off the event loop — the form the WebSocket doors must use (`SessionLocal` is
    synchronous).

    `run_in_threadpool`, not `asyncio.to_thread`: the latter uses a different pool, so neither capacity
    limiter would see the real concurrency. Imported lazily to keep the jaahd contract test's import
    graph stdlib-only.
    """
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(resolve_caller, provided)


def token_ok(provided: object) -> bool:
    """`resolve_caller` reduced to yes/no, for doors that do not care which node dialled (the
    console-stream leg is paired by a single-use ticket).
    """
    return resolve_caller(provided) is not None


class NodeConnection:
    """One node's live socket plus its pending-reply table.

    `_send_lock` serializes writes: `WebSocket.send_text` is not safe for concurrent callers, and commands
    are dispatched concurrently.
    """

    def __init__(self, node_id: str, websocket: Any) -> None:
        self.node_id = node_id
        self._ws = websocket
        # This connection's identity in the shared placement row (`app/node_presence.py`): the teardown
        # deletes only the row this id wrote, so a redial onto the same peer is never evicted by the old
        # socket's late close.
        self.socket_id = uuid.uuid4()
        self._pending: dict[int, asyncio.Future] = {}
        self._corr = itertools.count(1)
        self._send_lock = asyncio.Lock()
        self._closed = False
        # The loop that owns this socket. Reply futures are created and resolved on it, so a sync
        # (threadpool) caller must hand its coroutine back here with `run_coroutine_threadsafe`. A
        # throwaway loop would only observe replies at its own timeout and drive `send_text` from a
        # foreign loop. `None` only when constructed outside a loop (unit tests).
        try:
            self.serving_loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self.serving_loop = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def request(self, envelope: dict[str, Any], *, timeout: float, frame_type: str = FRAME_CMD) -> Reply:
        """Send one request frame and await its correlated reply. Reads happen only in `handle_frame`
        on the read loop; reading here would race it.
        """
        if self._closed:
            raise NodeNotConnected(self.node_id)
        if len(self._pending) >= MAX_PENDING_PER_NODE:
            # Refuse rather than grow unbounded; a node this far behind is not serving commands.
            log.warning(
                "node-bus: %s has %d requests in flight (cap %d) — refusing another",
                self.node_id, len(self._pending), MAX_PENDING_PER_NODE,
            )
            raise NodeNotConnected(self.node_id)

        corr = next(self._corr)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[corr] = future
        try:
            await self._send({**envelope, "t": frame_type, "id": corr, "v": BUS_SCHEMA_VERSION})
            data = await asyncio.wait_for(future, timeout)
            return Reply(data)
        except asyncio.TimeoutError as exc:
            raise NodeRequestTimeout(self.node_id, timeout) from exc
        finally:
            self._pending.pop(corr, None)

    async def notify(self, frame: dict[str, Any]) -> None:
        """Send one frame that expects no reply (e.g. `hb_ack`): no correlation id, so it cannot occupy
        a `MAX_PENDING_PER_NODE` slot. Raises `NodeNotConnected` on a dead socket."""
        await self._send({**frame, "v": BUS_SCHEMA_VERSION})

    async def _send(self, frame: dict[str, Any]) -> None:
        try:
            async with self._send_lock:
                await self._ws.send_text(json.dumps(frame))
        except Exception as exc:
            # Present a dead socket as "not connected" (503) rather than a 500.
            raise NodeNotConnected(self.node_id) from exc

    def resolve(self, corr: object, data: bytes) -> None:
        """Complete the waiter for one correlation id. Unknown or duplicate ids are logged and dropped:
        a late reply after its timeout is expected."""
        if not isinstance(corr, int):
            return
        future = self._pending.pop(corr, None)
        if future is None:
            log.debug("node-bus: %s replied to unknown/expired id %s", self.node_id, corr)
            return
        if not future.done():
            future.set_result(data)

    def fail_all(self) -> None:
        """Fail every in-flight request when the socket dies, so callers get an immediate 503."""
        self._closed = True
        for corr, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(NodeNotConnected(self.node_id))
            self._pending.pop(corr, None)

    async def close(self, code: int, reason: str) -> None:
        """Close this socket with an application close code the node will read.

        Does not take `_send_lock`: a close ends the stream, and waiting on a write stuck against a dead
        peer would stall the whole `close_all` sweep."""
        self._closed = True
        await self._ws.close(code=code, reason=reason)


class RemoteNodeConnection:
    """A node whose socket another control-plane peer holds: the same `request`/`notify` surface as
    `NodeConnection`, carried out by forwarding to that peer (`app/cp_peers.py`).

    `address` is the holder's peer address once known; `None` is the LAZY form `NodeBusRegistry.get`
    hands out, which resolves the holder on first use through `registry.route` (and so may end up on
    a local socket that arrived meanwhile). Every peer error surfaces as `NodeNotConnected` or
    `NodeRequestTimeout`, the two answers every caller already maps.

    `serving_loop` is the process loop: a remote target has no socket loop of its own, and
    `run_on_serving_loop` needs one to hand a threadpool caller's coroutine to.

    ONE RE-ROUTE on a holder that REFUSED (`HolderRefused`: 503, or unreachable): the placement row can
    name a peer the node has just left (it redialled onto a third peer, whose row landed after this call
    read the old one). A single fresh `route` then finds the new holder — or the socket that arrived here
    meanwhile — and the call is retried there once. Once PER CALL, not per hop (`_may_reroute` is cleared
    on the connection the re-route hands out), and only somewhere ELSE: a second refusal is the node
    being gone, not a stale row. A failure after the request was sent never re-routes.
    """

    def __init__(self, node_id: str, address: Optional[str], *, registry: "NodeBusRegistry") -> None:
        self.node_id = node_id
        self.address = address
        self._registry = registry
        self._may_reroute = True

    @property
    def serving_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return serving_loop()

    async def _elsewhere(self) -> "Optional[NodeConnection | RemoteNodeConnection]":
        """The connection a fresh route gives, when this call may still re-route and the route is not the
        peer that just refused — else None. The connection handed out may not re-route again."""
        if not self._may_reroute:
            return None
        try:
            conn = await self._registry.route(self.node_id)
        except NodeNotConnected:
            return None
        if isinstance(conn, RemoteNodeConnection):
            if conn.address == self.address:
                return None
            conn._may_reroute = False
        return conn

    async def request(self, envelope: dict[str, Any], *, timeout: float, frame_type: str = FRAME_CMD) -> Reply:
        if self.address is None:
            conn = await self._registry.route(self.node_id)
            return await conn.request(envelope, timeout=timeout, frame_type=frame_type)
        from app import cp_peers

        try:
            data = await cp_peers.forward_request(
                self.address, self.node_id, envelope, timeout=timeout, frame_type=frame_type,
            )
        except HolderRefused:
            conn = await self._elsewhere()
            if conn is None:
                raise
            log.info("node-bus: %s moved off %s — re-routing the request once", self.node_id, self.address)
            return await conn.request(envelope, timeout=timeout, frame_type=frame_type)
        return Reply(data)

    async def notify(self, frame: dict[str, Any]) -> None:
        if self.address is None:
            conn = await self._registry.route(self.node_id)
            await conn.notify(frame)
            return
        from app import cp_peers

        try:
            await cp_peers.forward_notify(self.address, self.node_id, frame)
        except HolderRefused:
            conn = await self._elsewhere()
            if conn is None:
                raise
            log.info("node-bus: %s moved off %s — re-routing the notify once", self.node_id, self.address)
            await conn.notify(frame)


class SharedSocketStore:
    """The default placement store: `app/node_presence.py` read through this process's own sessions.

    The surface `NodeBusRegistry.attach_store` takes (a test may attach a fake with the same four
    methods). Sync methods open a `SessionLocal` here and so must be called off the event loop; the
    `_async` twins are `node_presence`'s own thread-hopping ones. All imports are lazy — see the module
    docstring.
    """

    def holder_of(self, node_id: str):
        from app import node_presence
        from app.db import SessionLocal

        with SessionLocal() as session:
            return node_presence.holder_of(session, node_id)

    async def holder_of_async(self, node_id: str):
        from app import node_presence

        return await node_presence.holder_of_async(node_id)

    def connected_node_ids(self) -> list[str]:
        from app import node_presence
        from app.db import SessionLocal

        with SessionLocal() as session:
            return node_presence.connected_node_ids(session)

    async def connected_node_ids_async(self) -> list[str]:
        from app import node_presence

        return await node_presence.connected_node_ids_async()


class NodeBusRegistry:
    """`node_id -> NodeConnection` for every node dialled into THIS process, plus — with a store attached —
    the route to every node dialled into a peer (module docstring)."""

    def __init__(self) -> None:
        self._conns: dict[str, NodeConnection] = {}
        self._store: Any = None

    def attach_store(self, store: Any) -> None:
        """Consult `store` (a `SharedSocketStore`-shaped object) for nodes this process does not hold."""
        self._store = store

    def detach_store(self) -> None:
        self._store = None

    def _is_self(self, peer_id: object) -> bool:
        from app import cp_peers

        return peer_id == cp_peers.self_id()

    def register(self, node_id: str, websocket: Any) -> NodeConnection:
        """Adopt a node's socket, displacing any previous one for the same id: after a partition the CP
        may still hold a half-open original, so the newest wins and the stale one is failed out.
        """
        previous = self._conns.get(node_id)
        if previous is not None:
            log.info("node-bus: %s reconnected — displacing the previous connection", node_id)
            previous.fail_all()
        conn = NodeConnection(node_id, websocket)
        self._conns[node_id] = conn
        return conn

    def unregister(self, node_id: str, conn: NodeConnection) -> None:
        """Drop `conn` only if it is still the live entry, so a displaced connection's teardown cannot
        evict its replacement."""
        conn.fail_all()
        if self._conns.get(node_id) is conn:
            del self._conns[node_id]

    def drop(self, node_id: str) -> bool:
        """Evict whatever connection this node has, by id. Returns True if there was one.

        For a node that vanished without a FIN, whose entry would otherwise stay connected until keepalive
        reaps it (every command a 504 meanwhile). In-process only: the shared placement row is the
        socket handler's to delete on close (`routes/node_bus.py`), and the heartbeat retracts a row for
        a socket this process no longer holds (`node_presence.retract_lost`). Does not close the socket:
        a live peer learns `registered: false` from its next `hb_ack` and re-dials."""
        conn = self._conns.pop(node_id, None)
        if conn is None:
            return False
        conn.fail_all()
        return True

    async def close_all(self, code: int, reason: str) -> None:
        """Close every registered socket with a stated close code, and empty the registry.

        The announce half of a CP handoff (`app/main.py` lifespan): a readable close code lets the node
        redial immediately instead of backing off (`apps/jaahd/node_bus.py`).

        Snapshot first (unregistering mutates `_conns`), and unregister before closing so a concurrent
        command gets an immediate 503. Per-socket failures are swallowed: under uvicorn many sockets are
        already closed by now (`app/main.py::_lifespan`). Idempotent.
        """
        conns = list(self._conns.items())
        if conns:
            # Makes a handoff's announcement measurable from CP logs.
            log.info("node-bus: closing %d node socket(s) with code %d (%s)", len(conns), code, reason)
        for node_id, conn in conns:
            self.unregister(node_id, conn)
            try:
                await conn.close(code, reason)
            except Exception as exc:  # noqa: BLE001 — one dead socket must not end the sweep; see above
                log.debug("node-bus: could not close %s's socket: %s", node_id, exc)

    def is_connected(self, node_id: str) -> bool:
        """Local socket, or — with a store — a live peer recorded as the holder. Synchronous: the store
        read is a DB round trip, so call it from the threadpool, never on the loop (`is_connected_async`
        is the loop's form). A store fault reads as not connected."""
        if node_id in self._conns:
            return True
        if self._store is None:
            return False
        try:
            return self._store.holder_of(node_id) is not None
        except Exception as exc:  # noqa: BLE001 — a placement read that fails is "not connected", not a 500
            log.warning("node-bus: could not read %s's placement (%s) — answering not connected", node_id, exc)
            return False

    def holds(self, node_id: str, conn: NodeConnection) -> bool:
        """Whether `conn` is the connection commands for `node_id` would go down: after a displacement
        both sockets are open, so a heartbeat ack must answer per connection, not per node id."""
        return self._conns.get(node_id) is conn

    def connected_nodes(self) -> list[str]:
        """Every node with a socket on this process or (with a store) on a live peer. Synchronous — a DB
        read with a store, so threadpool only; `connected_nodes_async` is the loop's form. A store fault
        degrades to the local set."""
        if self._store is None:
            return sorted(self._conns)
        try:
            remote = self._store.connected_node_ids()
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.warning("node-bus: could not list peer-held sockets (%s) — listing local ones only", exc)
            remote = []
        return sorted(set(self._conns) | set(remote))

    async def is_connected_async(self, node_id: str) -> bool:
        """`is_connected` for a caller on the event loop (`app/console_direct.py`)."""
        if node_id in self._conns:
            return True
        if self._store is None:
            return False
        try:
            return (await self._store.holder_of_async(node_id)) is not None
        except Exception as exc:  # noqa: BLE001 — see `is_connected`
            log.warning("node-bus: could not read %s's placement (%s) — answering not connected", node_id, exc)
            return False

    def local_sockets(self) -> list[tuple[str, uuid.UUID]]:
        """`(node_id, socket_id)` for every socket this process holds — what the peer heartbeat re-asserts
        in the shared placement table (`app/cp_peers.py::heartbeat_tick`). Sync and DB-free.

        `list(dict.items())` first — one C-level copy under the GIL — so the comprehension never walks the
        loop-owned dict while a register/unregister on the loop resizes it (the heartbeat calls this off
        the loop is how it would otherwise raise "dictionary changed size during iteration" mid-redial)."""
        return [(node_id, conn.socket_id) for node_id, conn in list(self._conns.items())]

    async def connected_nodes_async(self) -> list[str]:
        """`connected_nodes` for a caller on the event loop (`routes/port_grid.py`)."""
        if self._store is None:
            return sorted(self._conns)
        try:
            remote = await self._store.connected_node_ids_async()
        except Exception as exc:  # noqa: BLE001 — see `connected_nodes`
            log.warning("node-bus: could not list peer-held sockets (%s) — listing local ones only", exc)
            remote = []
        return sorted(set(self._conns) | set(remote))

    def local(self, node_id: str) -> Optional[NodeConnection]:
        """This process's own socket for `node_id`, or None — never a forward. The peer routes
        (`routes/cp_peer.py`) use this so a forwarded call cannot be forwarded again."""
        return self._conns.get(node_id)

    def get(self, node_id: str) -> "Optional[NodeConnection | RemoteNodeConnection]":
        """With a store: ALWAYS a lazy `RemoteNodeConnection` that resolves the holder on first use through
        `route` — even when this process holds a socket, because a local socket can be a half-open one
        the node has already left for another peer (a partition, then a redial elsewhere), and `notify`
        into its TCP buffer "succeeds" for the ~40s until keepalive reaps it — which is how a direct-
        console revoke would be counted delivered and never arrive (found by review). `route` reads the
        recorded holder and comes back to the local socket when it is ours. Without a store: the local
        socket or None. Synchronous and DB-free either way, so a threadpool caller (`run_on_serving_loop`)
        can take it; a caller that reads None as "not connected" must also catch `NodeNotConnected` from
        the lazy connection's `notify`/`request`."""
        if self._store is None:
            return self._conns.get(node_id)
        return RemoteNodeConnection(node_id, None, registry=self)

    async def route(self, node_id: str) -> "NodeConnection | RemoteNodeConnection":
        """The connection commands for `node_id` go down, wherever its socket is. Raises
        `NodeNotConnected` when no live peer holds one.

        With a store: the recorded holder decides. Ourselves → the local socket (a row naming us with no
        local socket is stale: ours died and its `finally` is about to delete it). Another live peer → a
        forward, even over a local socket, since the newest registration wins across peers as it does
        within one (`register`). No holder → the local socket if there is one (the handler registers
        before it records) or not connected. A store fault → local-or-raise, logged."""
        local = self._conns.get(node_id)
        if self._store is None:
            if local is None:
                raise NodeNotConnected(node_id)
            return local
        try:
            holder = await self._store.holder_of_async(node_id)
        except Exception as exc:  # noqa: BLE001 — a placement read that fails degrades to local-only
            log.warning("node-bus: could not read %s's placement (%s) — routing locally only", node_id, exc)
            holder = None
        if holder is None or self._is_self(holder[0]):
            if local is None:
                raise NodeNotConnected(node_id)
            return local
        return RemoteNodeConnection(node_id, holder[1], registry=self)

    async def request(
        self, node_id: str, envelope: dict[str, Any], *, timeout: float, frame_type: str = FRAME_CMD,
    ) -> Reply:
        conn = await self.route(node_id)
        return await conn.request(envelope, timeout=timeout, frame_type=frame_type)

    def _reset_for_tests(self) -> None:
        self._store = None
        for node_id, conn in list(self._conns.items()):
            conn.fail_all()
            del self._conns[node_id]


# The process-wide registry: this process's sockets, plus the route to every peer's (module docstring).
registry = NodeBusRegistry()


def run_on_serving_loop(bus_target: str, coro, *, timeout: float):
    """Run `coro` on the loop that owns `bus_target`'s websocket, and block this thread for its answer.

    Single home of the idiom (`routes/commands.py`'s `account_converge` fan-out). `asyncio.run(coro)` from a loopless thread is broken: the reply future lives on the throwaway
    loop, a cross-loop `set_result` does not wake it, so replies are seen only at timeout, and `send_text`
    runs from a foreign loop.

    No serving loop raises `NodeNotConnected` (the coroutine is closed first). `timeout` is an outer bound
    and must be looser than the coroutine's own; it only stops a dead loop parking a thread forever.

    A peer-held target (`RemoteNodeConnection`) reports the process loop, so the coroutine runs there and
    its own `registry.request` forwards; the routing happens on the loop, never on this thread."""
    conn = registry.get(bus_target)
    loop = getattr(conn, "serving_loop", None) if conn is not None else None
    if loop is None:
        coro.close()
        raise NodeNotConnected(bus_target)
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)
