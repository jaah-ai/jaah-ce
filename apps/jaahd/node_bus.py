"""Node bus over WebSocket — the NODE half (CP half: `apps/api/app/node_bus.py`).

The daemon DIALS the CP and holds one WebSocket carrying: `cmd` (CP->node, reply; fed to
`commands.CommandBridge` via the transport-blind `_WsMsg` shim), `hello` (on connect; close is
deregister), `heartbeat` (driven by the idle loop — see `send_heartbeat`), `hb_ack` (the CP's verdict
on whether this socket is routed; `false` re-dials — the only self-heal for open-but-unregistered),
`console_open` (control step only; PTY bytes ride a dedicated socket, `console.py`), the
`console_direct_*` family, `vnode.converge`, and `send_intent`.

`send_intent` (no reply) carries one entry in `GET /api/agent/pool-tick`'s `intents` shape, parsed by
the same `reconcile.tick_wire._slot_intent_from_entry`. A latency device only: the `slot_intents` row stays the
authority and a dropped frame leaves it pending for the next pool-tick drain.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import env_helpers  # `environ_node_token`: read at call time, never pinned at import
import environment

log = logging.getLogger("jaahd.node_bus")

BUS_SCHEMA_VERSION = 1

FRAME_CMD = "cmd"
FRAME_REPLY = "reply"
FRAME_HELLO = "hello"
FRAME_HEARTBEAT = "heartbeat"
FRAME_HB_ACK = "hb_ack"
FRAME_CONSOLE_OPEN = "console_open"
# Direct console (`console_direct.py`); names pinned by the CP's `app/node_bus.py`. `grant` (reply
# EXPECTED: the CP withholds the token from the browser until the ack). `revoke` (no reply) is keyed on
# the admin SESSION, not the token — the CP keeps no token store across replicas. `used` (node->CP)
# is the audit record direct mode would otherwise lose.
FRAME_CONSOLE_DIRECT_GRANT = "console_direct_grant"
FRAME_CONSOLE_DIRECT_REVOKE = "console_direct_revoke"
FRAME_CONSOLE_DIRECT_USED = "console_direct_used"
# Slot-intent push (CP -> node, no reply). Pinned by the CP's `app/node_send_intent.py`, not
# `app/node_bus.py`; `tests/test_node_bus.py` holds the two spellings together.
FRAME_SEND_INTENT = "send_intent"
# CP -> host, NO reply: "your desired vnode list changed — converge now" (the CP's vnode
# `FRAME_CONVERGE`). A nudge only; the host's 60s poll of `desired` is the durable path.
FRAME_VNODE_CONVERGE = "vnode.converge"
# CP -> node, NO reply: "this machine was deleted with uninstall". Twin of the CP's
# `app/node_send_intent.py::FRAME_UNINSTALL` — change both or neither. Only drops a marker
# (`uninstall_request.py`); the root unit acts only once the CP refuses this node's token.
FRAME_UNINSTALL = "uninstall"

WS_PATH = "/api/node-bus"

# Heartbeat cadence is set by the daemon's idle loop (default 5s), not here — see `send_heartbeat`. Presence
# itself is the live socket (`apps/api/app/jaah_discovery.py::status_and_last_seen`), not a heartbeat TTL.

# WebSocket-level keepalive on the node's own dialled session. A peer that stops answering pings is dropped
# after roughly PING_INTERVAL + PING_TIMEOUT, which bounds how long a dead CP can look alive to this node.
PING_INTERVAL_SECONDS = 20.0
PING_TIMEOUT_SECONDS = 20.0
_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 30.0

# Handoff closes — "this replica is standing down, another is coming up": 4504 (the CP's own close,
# single-homed at `apps/api/app/routes/node_bus.py::_WS_HANDOFF`), 1012 SERVICE_RESTART (uvicorn), 1001
# GOING_AWAY. Only a deliberate handoff earns a near-zero retry; 1011, 1006 and transport failures stay
# on the exponential ladder, as does a 403 (the public listener refuses `/api/node-bus` permanently).
_HANDOFF_CLOSE_CODE = 4504
_HANDOFF_CLOSE_CODES = frozenset({_HANDOFF_CLOSE_CODE, 1012, 1001})
_HANDOFF_RETRY_SECONDS = 0.5
# Jitter so the fleet does not redial the incoming replica in one burst; redial lands in [0.5s, 1.0s).
_HANDOFF_RETRY_JITTER_SECONDS = 0.5


def _is_handoff_close(exc: BaseException) -> bool:
    """Whether this session ended because the control plane deliberately handed off.

    Decided on the close frames alone; no `websockets` import needed. In the vendored `websockets` 17.3,
    `ConnectionClosed.rcvd`/`.sent` are `frames.Close | None`, and `.code` is a DEPRECATED property that
    warns (and returns 1006 with no `rcvd`), so it is never read. Any other exception has no frames and is
    not a handoff.
    """
    return any(
        getattr(frame, "code", None) in _HANDOFF_CLOSE_CODES
        for frame in (getattr(exc, "rcvd", None), getattr(exc, "sent", None))
    )


# Bounds the opening handshake so a black-holed CP address cannot wedge the connect attempt forever (the
# retry loop must keep cycling).
CONNECT_TIMEOUT_SECONDS = 10.0


def resolve_cp_ws_url() -> str:
    """The control-plane WebSocket URL this node dials.

    Precedence: explicit `JAAH_CP_WS_URL`; else `JAAH_CP_BASE_URL` mapped to ws(s) + `WS_PATH`
    (`control_plane_base_explicit`, which answers `""` rather than guessing loopback); else the
    node-local API base — right for single-host dev, wrong for a fleet node, so `run_forever` logs the
    resolved target per connect attempt.
    """
    explicit = environment.control_plane_ws_url_explicit()
    if explicit:
        return explicit

    base = environment.control_plane_base_explicit()
    if not base:
        import main as jaahd_main  # late import: avoids a cycle at module import time

        base = jaahd_main.resolve_api_base_url()

    parts = urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, WS_PATH, "", ""))


def _expected_token() -> str:
    """Read at call time, never pinned at import (see `commands.py::_expected_token`)."""
    return env_helpers.environ_node_token()


class _WsMsg:
    """The message shape `commands._serve_one` uses (`.data`, `await .respond(bytes)`), keeping the
    executor transport-blind."""

    __slots__ = ("data", "subject", "_send", "_corr")

    def __init__(self, data: bytes, corr: object, send: Callable[[dict], Awaitable[None]]) -> None:
        self.data = data
        # Some log lines read `msg.subject`; keep a synthetic one so they stay meaningful.
        self.subject = "node-bus/ws"
        self._corr = corr
        self._send = send

    async def respond(self, payload: bytes) -> None:
        """Wrap the executor's reply bytes in a correlated `reply` frame, merging the routing keys into
        its JSON object."""
        try:
            body = json.loads(payload or b"{}")
            if not isinstance(body, dict):
                body = {"status": "failed", "result": {"reason": "non-object reply"}}
        except ValueError:
            body = {"status": "failed", "result": {"reason": "unserializable reply"}}
        await self._send({**body, "t": FRAME_REPLY, "id": self._corr, "v": BUS_SCHEMA_VERSION})


class NodeBusClient:
    """Outbound WebSocket to the CP, with reconnect. One instance per daemon."""

    def __init__(
        self,
        node_id: str,
        *,
        deliver_command: Callable[[Any], None],
        cp_ws_url: Optional[str] = None,
        open_console: Optional[Callable[[dict], Awaitable[dict]]] = None,
        on_connected: Optional[Callable[[], Awaitable[None]]] = None,
        grant_direct_console: Optional[Callable[[dict], dict]] = None,
        revoke_direct_console: Optional[Callable[[dict], int]] = None,
        on_send_intent: Optional[Callable[[dict], None]] = None,
        on_vnode_converge: Optional[Callable[[], None]] = None,
        on_uninstall: Optional[Callable[[], None]] = None,
    ) -> None:
        self._node_id = node_id
        # Fired once per (re)connect, right after `hello` (see `_session`).
        self._on_connected = on_connected
        # None when Console is disabled; `console_open` then gets an explicit error reply.
        self._open_console = open_console
        # `console_direct.DirectGrantStore.grant` / `.revoke`, or None. SYNCHRONOUS: dispatching them to a
        # task would let a revoke overtake the grant it revokes.
        self._grant_direct_console = grant_direct_console
        self._revoke_direct_console = revoke_direct_console
        # `main.NodeStatePublisher.push_slot_intent`, or None. Synchronous, but it must SPAWN its pass (a
        # pool-tick fetch, maybe a kill) and return — awaiting it would block the read loop.
        self._on_send_intent = on_send_intent
        # `vnode_converge.VnodeConverger.nudge`: spawns its single-flight pass. None ignores the frame.
        self._on_vnode_converge = on_vnode_converge
        # `uninstall_request.request_uninstall`: writes one empty marker and returns. None ignores the frame.
        self._on_uninstall = on_uninstall
        # `commands.CommandBridge.deliver` — spawns a task per command, so the read loop never blocks.
        self._deliver_command = deliver_command
        self._cp_ws_url = cp_ws_url
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        # Whether the CP accepted the current dial (it sent a frame) — read by `run_forever` to reset the ladder.
        self._established = False

    @property
    def is_connected(self) -> bool:
        """Whether OUR END believes a session is up — not a claim about the CP's registry."""
        return self._connected.is_set()

    def request_reconnect(self) -> None:
        """Drop the current session so `run_forever` re-dials (and re-sends `hello`, re-registering us).

        For a socket open and writable while the CP no longer has it registered. Sync and non-blocking
        (called from the heartbeat path): it schedules the close and lets `_session` unwind. Idempotent."""
        ws = self._ws
        if ws is None:
            return
        self._connected.clear()
        # `close()` is a coroutine; we are sync here on purpose, so schedule it and return.
        asyncio.ensure_future(_close_quietly(ws))

    async def run_forever(self, stop: asyncio.Event) -> None:
        backoff = _RECONNECT_BACKOFF_INITIAL
        while not stop.is_set():
            url = self._cp_ws_url or resolve_cp_ws_url()
            self._established = False
            try:
                await self._session(url, stop)
                backoff = _RECONNECT_BACKOFF_INITIAL
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_handoff_close(exc):
                    # Near-zero, jittered, and the ladder is RESET: a handoff proves the session worked, so an
                    # outage's climb must not carry into the next deploy.
                    delay = _HANDOFF_RETRY_SECONDS + random.random() * _HANDOFF_RETRY_JITTER_SECONDS
                    log.info(
                        "node-bus: %s closed the session (handing off) — redialling in %.2fs", url, delay,
                    )
                    await _sleep_or_stop(stop, delay)
                    backoff = _RECONNECT_BACKOFF_INITIAL
                else:
                    # A session the CP accepted proved the route works, so an earlier outage's climb must not
                    # carry into this drop (a deploy's ALB cut, 1006, would otherwise wait up to 30s).
                    if self._established:
                        backoff = _RECONNECT_BACKOFF_INITIAL
                    # Jittered into [backoff/2, backoff): a fleet dropped together must not redial in lockstep.
                    delay = backoff * (0.5 + random.random() * 0.5)
                    log.warning(
                        "node-bus: session to %s ended (%s) — reconnecting in %.1fs", url, exc, delay
                    )
                    await _sleep_or_stop(stop, delay)
                    backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
            finally:
                self._connected.clear()
                self._ws = None

    async def _session(self, url: str, stop: asyncio.Event) -> None:
        # Lazy: only a dialling daemon needs `websockets`, a pinned dependency of the node's venv
        # (infra/jaah/requirements.txt); tests import this module directly.
        from websockets.asyncio.client import connect

        log.info("node-bus: dialling %s as %s", url, self._node_id)
        # Explicit ping settings: a library default change must not lengthen how long a half-open session
        # looks alive. `compression=None`: no permessage-deflate — its stateful context serializes every
        # frame, and the CP refuses it anyway (`--ws-per-message-deflate false`).
        async with connect(
            url,
            open_timeout=CONNECT_TIMEOUT_SECONDS,
            ping_interval=PING_INTERVAL_SECONDS,
            ping_timeout=PING_TIMEOUT_SECONDS,
            compression=None,
        ) as ws:
            self._ws = ws
            await self._send({
                "t": FRAME_HELLO,
                "v": BUS_SCHEMA_VERSION,
                "token": _expected_token(),
                "node_id": self._node_id,
            })
            self._connected.set()
            log.info("node-bus: connected to %s", url)
            self._fire_on_connected()

            # NO HEARTBEAT TIMER HERE, deliberately — see `send_heartbeat`.
            await self._read_forever(ws, stop)

    def _fire_on_connected(self) -> None:
        """Run the connected-callback, in a way that cannot cost us the session.

        Every (re)connect: registration is a property of THIS connection, and an offline-through-a-deploy
        node is the one that needs the package converge. Off the read loop: it ends in a blocking 30s
        urllib GET. Wrapped so a failure is logged by name rather than as an anonymous "Task exception
        was never retrieved"."""
        callback = self._on_connected
        if callback is None:
            return

        async def _guarded() -> None:
            try:
                await callback()
            except Exception as exc:  # noqa: BLE001 — an optional callback may never cost us the bus
                log.warning("node-bus: the on-connected callback failed: %s", exc)

        asyncio.ensure_future(_guarded())

    async def _read_forever(self, ws: Any, stop: asyncio.Event) -> None:
        while not stop.is_set():
            raw = await ws.recv()
            # The CP sends nothing before it accepts the hello — a refused one (4400/4401, 1011 on a failed
            # presence write) is closed frameless — so a received frame is the proof `run_forever` resets on.
            self._established = True
            try:
                frame = json.loads(raw)
            except ValueError:
                log.warning("node-bus: CP sent a malformed frame")
                continue
            if not isinstance(frame, dict):
                continue

            kind = frame.get("t")
            corr = frame.get("id")
            if kind == FRAME_CMD:
                # The frame IS the command envelope plus routing keys; `_serve_one` reads only
                # token/command_type/args and ignores the rest.
                self._deliver_command(_WsMsg(json.dumps(frame).encode(), corr, self._send))
            elif kind == FRAME_HB_ACK:
                self._note_hb_ack(frame)
            elif kind == FRAME_CONSOLE_OPEN:
                # Off the read loop: opening a console can take tens of seconds.
                asyncio.ensure_future(self._serve_console_open(frame, corr))
            elif kind == FRAME_CONSOLE_DIRECT_GRANT:
                # Grant APPLIED INLINE, only the reply deferred: the revoke below is inline, so a deferred
                # grant could install AFTER a later revoke and leave a revoked token redeemable for 24h.
                # Reachable: the vendored `SimpleQueue.get()` returns buffered frames without suspending.
                body = self._apply_direct_grant(frame)
                asyncio.ensure_future(self._reply_direct_grant(body, corr))
            elif kind == FRAME_CONSOLE_DIRECT_REVOKE:
                # Inline: a revoke must never be reordered behind anything.
                self._serve_direct_revoke(frame)
            elif kind == FRAME_SEND_INTENT:
                # Inline; NO reply frame — the CP fires and forgets, and the row stays pending for the drain.
                self._serve_send_intent(frame)
            elif kind == FRAME_VNODE_CONVERGE:
                # Inline and reply-less like `send_intent`: the callback spawns the pass and returns.
                self._serve_vnode_converge()
            elif kind == FRAME_UNINSTALL:
                # Inline and reply-less: the CP deleted the node row before sending, so nothing awaits an answer.
                self._serve_uninstall()
            else:
                log.debug("node-bus: ignoring frame type %r", kind)

    def _note_hb_ack(self, frame: dict) -> None:
        """Act on the CP's registration verdict for this socket.

        `registered: false` (no CP route down this connection) ends the session so `run_forever` re-dials
        and re-registers. Anything but exactly `False` (absent, None, an older CP) is "not told" and never
        drops a working socket. Sync: runs on the read loop."""
        registered = frame.get("registered")
        if registered is not False:
            return
        log.warning(
            "node-bus: the control plane does not have this socket registered for %s — commands would find "
            "no responder; re-dialling", self._node_id,
        )
        self.request_reconnect()

    async def _serve_console_open(self, frame: dict, corr: object) -> None:
        """Answer one `console_open`. Always replies, so the CP need not wait out its timeout."""
        if self._open_console is None:
            body: dict = {"ok": False, "error": "console is disabled on this node"}
        else:
            try:
                body = await self._open_console(frame)
            except Exception as exc:
                # Never log the frame: it carries the frame proof and the live rendezvous ticket.
                log.exception("node-bus: console_open handler failed")
                body = {"ok": False, "error": f"console open failed: {exc}", "retryable": True}
        try:
            await self._send({**body, "t": FRAME_REPLY, "id": corr, "v": BUS_SCHEMA_VERSION})
        except Exception as exc:
            log.warning("node-bus: could not reply to console_open: %s", exc)

    def _apply_direct_grant(self, frame: dict) -> dict:
        """Record one `console_direct_grant` and return the reply body. Must stay SYNCHRONOUS (ordering
        against a revoke; see the dispatch site)."""
        if self._grant_direct_console is None:
            return {"ok": False, "error": "direct console is disabled on this node"}
        try:
            return self._grant_direct_console(frame)
        except Exception as exc:
            # Never log the frame: it carries the frame proof and the direct credential being granted.
            log.exception("node-bus: console_direct_grant handler failed")
            return {"ok": False, "error": f"direct grant failed: {exc}"}

    async def _reply_direct_grant(self, body: dict, corr: object) -> None:
        """Send the ack for a grant already applied. Always replies: the CP withholds the token until it."""
        try:
            await self._send({**body, "t": FRAME_REPLY, "id": corr, "v": BUS_SCHEMA_VERSION})
        except Exception as exc:
            log.warning("node-bus: could not reply to console_direct_grant: %s", exc)

    def _serve_direct_revoke(self, frame: dict) -> None:
        """Apply one `console_direct_revoke`. No reply, and never raises (it would drop the socket)."""
        if self._revoke_direct_console is None:
            return
        try:
            self._revoke_direct_console(frame)
        except Exception:
            log.exception("node-bus: console_direct_revoke handler failed")

    def _serve_vnode_converge(self) -> None:
        """Hand a `vnode.converge` nudge to the converge loop. Never raises: we are on the read loop."""
        callback = self._on_vnode_converge
        if callback is None:
            log.debug("node-bus: vnode.converge ignored — no converge loop is wired on this client")
            return
        try:
            callback()
        except Exception:
            log.exception("node-bus: vnode.converge handler failed")

    def _serve_uninstall(self) -> None:
        """Hand an `uninstall` frame to the uninstall request. Never raises: we are on the read loop."""
        callback = self._on_uninstall
        if callback is None:
            log.debug("node-bus: uninstall ignored — no uninstall request is wired on this client")
            return
        try:
            callback()
        except Exception:
            log.exception("node-bus: uninstall handler failed")

    def _serve_send_intent(self, frame: dict) -> None:
        """Act on one `send_intent`: hand the carried intent to the node's intent sink, and return.

        `frame["intent"]` passes through uninspected: the sink uses the pool tick's parser. An absent
        callback is only a debug line (the row stays pending). Never raises: an exception on the read loop
        would drop the whole bus session for a missed latency hint."""
        callback = self._on_send_intent
        if callback is None:
            log.debug("node-bus: send_intent ignored — no intent sink is wired on this client")
            return
        try:
            callback(frame.get("intent"))
        except Exception:  # noqa: BLE001 — a latency hint may never cost us the bus (see the docstring)
            log.exception("node-bus: send_intent handler failed")

    async def send_direct_used(self, token: str, event: str) -> None:
        """Report that a direct grant was opened or closed. Best-effort: logged and swallowed; the local
        audit log carries the same record."""
        try:
            await self._send({
                "t": FRAME_CONSOLE_DIRECT_USED, "v": BUS_SCHEMA_VERSION,
                "token": token, "event": event,
            })
        except Exception as exc:
            log.warning("node-bus: could not report direct console %s: %s", event, exc)

    async def send_heartbeat(self) -> None:
        """Send one presence `heartbeat` frame. Raises if the socket is not up, so the caller can log and
        retry on its next tick.

        Driven by the daemon's idle loop, never a timer of its own: a WEDGED loop must stop heartbeating
        so the CP flips the node OFFLINE truthfully.
        """
        await self._send({"t": FRAME_HEARTBEAT, "v": BUS_SCHEMA_VERSION})

    async def _send(self, frame: dict) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("node-bus: not connected")
        # Serialize writes: concurrent producers would interleave frames.
        async with self._send_lock:
            await ws.send(json.dumps(frame))


async def _close_quietly(ws) -> None:
    """Close one socket, swallowing everything; a socket already gone has ended the session anyway."""
    try:
        await ws.close()
    except Exception:  # noqa: BLE001 — a socket that cannot close is a socket that is already finished
        pass


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
