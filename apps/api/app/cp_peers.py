"""The control plane's peer registry — what makes N API processes one control plane.

A PEER is one running API process (one ECS task, one local `./dc` api container). Each registers itself in
`cp_peers` with the private address the others can reach it on and heartbeats that row; a node's WebSocket
belongs to exactly one peer, recorded in `node_bus_sockets` (`app/node_presence.py`). A command landing on
a peer that does not hold the node's socket is forwarded over HTTP to the holder (`forward_request` /
`forward_notify`), so the fleet no longer needs the CP pinned to one task.

THE CREDENTIAL IS THE CP'S OWN TOKEN, NOT A NEW SECRET. A peer presents `node_credentials.cp_self_token()`
as `X-Agent-Token` to another peer's `/api/agent/cp-peer/*` routes — the `cp_self` arm
`routes/agent/auth.py::_check_token` already accepts. It is an HMAC over `SESSION_SECRET`, which every task
shares (Secrets Manager in deployment; `./dc`'s generated `.env.local` locally), so every peer derives the same
value and none of it is stored. `app/node_credentials.py` stays the single home of that arm.

Address resolution (`self_address`), first match wins: `CP_PEER_ADDRESS` (a full URL, for a stack that
knows its own name); else the ECS task metadata v4 endpoint (`ECS_CONTAINER_METADATA_URI_V4` + `/task`, the
first container's first awsvpc IPv4, port `CP_PEER_PORT` default 8000); else `http://127.0.0.1:<port>`,
which is right for the one-process local stack and wrong for anything else.

Liveness is the heartbeat: a peer whose `heartbeat_at` is older than `PEER_STALE_SECONDS` is not routed
to, and one older than twice that is deleted by the next peer whose tick sweeps (every
`SWEEP_EVERY_TICKS`-th; its socket and ticket rows cascade). Every tick also sweeps `console_tickets`
(`app/console_relay/tickets.py::sweep`: prune and revoke), and every sweeping tick hour-old `rate_limit_hits` and
`login_failures`, so those tables need no sweeper of their own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.control_plane import CpPeer, RateLimitHit
from app.models.identity import LoginFailure

log = logging.getLogger("app.cp_peers")

# A peer not heard from for this long is not routed to; twice this and its row is deleted.
PEER_STALE_SECONDS = 45
HEARTBEAT_INTERVAL_SECONDS = 15
# The dead-peer prune and the two retention DELETEs run on every this-many-th tick (the first tick included),
# not every tick (to save idle CPU). None of them is latency-bound: a dead peer is already unrouted after
# `PEER_STALE_SECONDS`, so its row lingering one cadence (60 s) past `2 * PEER_STALE_SECONDS` routes nothing to
# it, and the retention windows are an hour against readers that filter by time anyway.
SWEEP_EVERY_TICKS = 4
# `rate_limit_hits` older than this are pruned on every sweeping tick (the widest window a limiter reads is an hour).
RATE_LIMIT_HIT_RETENTION_SECONDS = 3600
# `login_failures` older than this likewise (the lockout reads back at most `WINDOW + LOCK_FOR`, 30 minutes).
LOGIN_FAILURE_RETENTION_SECONDS = 3600

# The ECS metadata fetch: local to the task, so a slow answer is a broken endpoint, not a slow network.
_METADATA_TIMEOUT_SECONDS = 2.0
_DEFAULT_PORT = 8000

# Per-process identity, minted on first use; `_self_address` caches the resolved address, `_self_task_id`
# the ECS task id.
_self_id: Optional[uuid.UUID] = None
_self_task_id: Optional[str] = None
_self_address: Optional[str] = None
_started_at: Optional[datetime] = None
# The forwarding client, built on first use so importing this module opens nothing.
#: A client a test installed; None in production, where each loop carries its own (`_client`).
_http: Optional[httpx.AsyncClient] = None
#: The attribute a loop's own client hangs on. On the loop, not in a module map: a client references its
#: loop through its transports, so a weak-keyed map would never let either go (a throwaway loop per
#: `dispatch_and_audit_sync` call would leak its sockets for the task's life).
_LOOP_CLIENT_ATTR = "_cp_peers_http"
_no_token_warned = False

# The forwarding routes another peer serves (`routes/agent/`, the cp-peer group).
_FORWARD_REQUEST_PATH = "/api/agent/cp-peer/node-bus/request"
_FORWARD_NOTIFY_PATH = "/api/agent/cp-peer/node-bus/notify"


# ── Identity ────────────────────────────────────────────────────────────────────────────────────────


def self_id() -> uuid.UUID:
    """This process's peer id, minted once per process."""
    global _self_id
    if _self_id is None:
        _self_id = uuid.uuid4()
    return _self_id


def address_from_task_metadata(doc: dict, port: int) -> Optional[str]:
    """`http://<ip>:<port>` from an ECS task metadata v4 `/task` document, or `None` when it names no
    IPv4. Pure, so the parse is testable without the endpoint."""
    for container in doc.get("Containers") or ():
        for network in (container or {}).get("Networks") or ():
            addresses = (network or {}).get("IPv4Addresses") or ()
            if addresses and addresses[0]:
                return f"http://{addresses[0]}:{port}"
    return None


def _fetch_task_metadata() -> Optional[dict]:
    """The ECS task metadata v4 document, or `None` off ECS or on any failure (logged).

    One attempt; `self_address` retries, because the endpoint can be slow to answer in a task's first
    seconds under launch pressure."""
    base = os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "").strip()
    if not base:
        return None
    try:
        with urllib.request.urlopen(f"{base}/task", timeout=_METADATA_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — any failure means "no metadata"; the caller retries, then raises
        log.warning("cp-peers: could not read ECS task metadata from %s (%s)", base, exc)
        return None


# How many times, and how far apart, a task asks the metadata endpoint for its own address before it
# refuses to start: ~10s covers the endpoint's warm-up; a task that still cannot learn its address must
# fail its deploy, never register loopback.
_METADATA_ATTEMPTS = 5
_METADATA_RETRY_SECONDS = 2.0


def self_address() -> str:
    """This peer's `http://<ip>:<port>`, resolved once and then cached (the module docstring has the order).

    ON ECS THERE IS NO FALLBACK: with `ECS_CONTAINER_METADATA_URI_V4` set, a task that cannot learn its own
    IP raises rather than registering `127.0.0.1` — every peer forwarding to that address would reach
    ITSELF and answer 503 for the task's whole life, silently. Raising fails the lifespan, the health
    check and so the deploy, which is the loud version. Loopback is only the answer off ECS with nothing
    configured (a one-process local stack, where nothing is ever forwarded)."""
    global _self_address
    if _self_address is not None:
        return _self_address
    configured = os.environ.get("CP_PEER_ADDRESS", "").strip()
    if configured:
        _self_address = configured.rstrip("/")
        return _self_address
    port = int(os.environ.get("CP_PEER_PORT", "") or _DEFAULT_PORT)
    if not os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "").strip():
        _self_address = f"http://127.0.0.1:{port}"
        return _self_address
    resolved = _from_task_metadata(lambda doc: address_from_task_metadata(doc, port), "IPv4")
    if resolved is None:
        raise RuntimeError(
            "cp-peers: this task could not learn its own address from the ECS task metadata endpoint after "
            f"{_METADATA_ATTEMPTS} attempts — refusing to register a loopback address other peers would "
            "forward to. Set CP_PEER_ADDRESS to override."
        )
    _self_address = resolved
    return _self_address


def _from_task_metadata(extract: Callable[[dict[str, Any]], Optional[str]], what: str) -> Optional[str]:
    """`extract` of the ECS task metadata document, asked up to `_METADATA_ATTEMPTS` times; `None` when no
    attempt yielded it."""
    for attempt in range(1, _METADATA_ATTEMPTS + 1):
        doc = _fetch_task_metadata()
        resolved = extract(doc) if doc else None
        if resolved is not None:
            return resolved
        if doc is not None:
            log.warning("cp-peers: ECS task metadata named no %s (attempt %d/%d)", what, attempt, _METADATA_ATTEMPTS)
        if attempt < _METADATA_ATTEMPTS:
            time.sleep(_METADATA_RETRY_SECONDS)
    return None


def task_id_from_task_metadata(doc: dict[str, Any]) -> Optional[str]:
    """`<task id>` out of the metadata document's `TaskARN` (`arn:...:task/<cluster>/<task id>`), or `None`."""
    arn = doc.get("TaskARN")
    tail = arn.rsplit("/", 1)[-1] if isinstance(arn, str) else ""
    return tail or None


def self_task_id() -> Optional[str]:
    """This process's ECS task id, resolved once and then cached; `None` off ECS. One api process per task
    (`app/worker_guard.py`), so it names this process to ECS — what `app/durable/runtime.py` runs its DBOS
    executor as, so a stopped task's in-flight workflows are told apart from a live one's.

    ON ECS IT RAISES rather than answer `None`, as `self_address` does: an executor id the sweep cannot find among
    the service's tasks reads as a stopped task's, and its live workflows would be run twice."""
    global _self_task_id
    if _self_task_id is not None or not os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "").strip():
        return _self_task_id
    resolved = _from_task_metadata(task_id_from_task_metadata, "TaskARN")
    if resolved is None:
        raise RuntimeError(
            "cp-peers: this task could not learn its own task id from the ECS task metadata endpoint after "
            f"{_METADATA_ATTEMPTS} attempts — refusing to run durable workflows under an id no sweep can find"
        )
    _self_task_id = resolved
    return _self_task_id


def self_token() -> str:
    """The credential a peer presents to another peer: the CP's own `cp_self` token.

    Derived, never configured: `node_credentials.cp_self_token()` is an HMAC over `SESSION_SECRET`, so
    every task sharing that secret derives the same value. When no real secret is configured the key is
    per-process random (`node_credentials.cp_self_key_is_ephemeral`), every peer derives a different
    token and none can authenticate to another — `start` warns about that; on a one-process stack it
    costs nothing, since nothing is ever forwarded.
    """
    # Lazy: `app.node_credentials` imports `app.models` and the dev-bypass posture; keep this module light.
    from app import node_credentials

    return node_credentials.cp_self_token()


def secret_is_ephemeral() -> bool:
    """Would another peer derive a different `self_token` than this one? True on the unset-secret case."""
    from app import node_credentials

    return node_credentials.cp_self_key_is_ephemeral()


# ── The registry ────────────────────────────────────────────────────────────────────────────────────


def register(session: Session) -> datetime:
    """Upsert this peer's row: `started_at` stamped once per process, `heartbeat_at` = the database's
    `now()` — every call is a heartbeat. Does not commit.

    NEVER the process start time on the heartbeat column: `node_presence.record_connected` re-registers
    on every node connect, and a heartbeat rewound to a start time older than the prune window let the
    next tick of ANY peer delete this one as dead — cascading away the socket row that connect had just
    written (measured: the row vanished within one heartbeat interval of the dial)."""
    global _started_at
    if _started_at is None:
        _started_at = datetime.now(timezone.utc)
    stmt = pg_insert(CpPeer).values(
        id=self_id(), address=self_address(), started_at=_started_at, heartbeat_at=func.now(),
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[CpPeer.id],
            set_={"address": stmt.excluded.address, "heartbeat_at": func.now()},
        )
    )
    return _started_at


def heartbeat_tick(session: Session, local_sockets: Optional[list[tuple[str, uuid.UUID]]] = None) -> int:
    """One heartbeat in ONE transaction: `register` then `housekeeping` (each documented below). The
    lifespan actor (`_tick_committed`) commits the two separately; this combined form is for a caller
    that owns its transaction (tests). Does not commit. Returns the number of peers pruned."""
    register(session)
    return housekeeping(session, local_sockets)


def housekeeping(
    session: Session, local_sockets: Optional[list[tuple[str, uuid.UUID]]] = None, *, sweep: bool = True,
) -> int:
    """The tick's second half: re-assert and retract this peer's socket rows and sweep the console relay's
    ticket rows (pruned and revoked) on every tick; with `sweep`, also prune dead peers (their
    `node_bus_sockets` cascade), old rate-limit hits and old login failures. Does not commit. Returns the
    number of peers pruned (0 without `sweep`).

    Kept on every tick because each is latency-bound, not housekeeping: the socket writes below promise
    "within one interval" to routing (`node_presence.RETRACT_GRACE_SECONDS` is sized on it), and the ticket
    sweep is how a logged-out viewer's open console is revoked. At idle the ticket sweep is two statements.

    `local_sockets` is the `(node_id, socket_id)` snapshot of what this process holds, taken ON THE LOOP
    by `_heartbeat_forever` before the hop (`node_bus.registry.local_sockets`); None takes one here.
    Two writes keep the shared rows honest for the life of a socket, not just at its ends:
      - RE-ASSERT (`node_presence.reassert_connected`, never over a newer holder on a live peer): a peer
        pruned as dead during a DB outage had its rows cascaded away, and nothing else re-creates them —
        `hb_ack` answers `registered` from the in-process dict, so the node never redials and every node
        on the slower peer stays dark for the life of its healthy socket.
      - RETRACT (`node_presence.retract_lost`): a row this peer wrote for a socket it no longer holds
        (the close-time delete failed, or a connect hop committed after the disconnect hop) would read
        ONLINE for this peer's whole life; it goes within one interval instead."""
    from app import node_bus, node_presence  # lazy: node_bus must stay importable stdlib-only
    from app.console_relay import tickets as console_relay_tickets  # lazy: it imports this module's peers

    if local_sockets is None:
        local_sockets = node_bus.registry.local_sockets()
    for node_id, socket_id in local_sockets:
        node_presence.reassert_connected(session, node_id, socket_id)
    retracted = node_presence.retract_lost(session, [socket_id for _, socket_id in local_sockets])
    console_relay_tickets.sweep(session)
    if retracted:
        log.info("cp-peers: retracted %d socket row(s) this process no longer holds", retracted)
    if not sweep:
        return 0
    pruned = session.execute(
        delete(CpPeer).where(
            CpPeer.id != self_id(),
            CpPeer.heartbeat_at < func.now() - timedelta(seconds=2 * PEER_STALE_SECONDS),
        )
    ).rowcount
    session.execute(
        delete(RateLimitHit).where(
            RateLimitHit.hit_at < func.now() - timedelta(seconds=RATE_LIMIT_HIT_RETENTION_SECONDS)
        )
    )
    session.execute(
        delete(LoginFailure).where(
            LoginFailure.failed_at < func.now() - timedelta(seconds=LOGIN_FAILURE_RETENTION_SECONDS)
        )
    )
    if pruned:
        log.info("cp-peers: pruned %d dead peer(s)", pruned)
    return pruned


def _register_committed() -> datetime:
    from app.db import SessionLocal

    with SessionLocal() as session:
        started = register(session)
        session.commit()
        return started


# Ticks taken by `_tick_committed`; only the one heartbeat thread touches it, so it needs no lock.
_ticks = 0


def _tick_committed(local_sockets: list[tuple[str, uuid.UUID]]) -> int:
    """The heartbeat proper, then the housekeeping, as TWO transactions; the housekeeping sweeps on every
    `SWEEP_EVERY_TICKS`-th call (the first included), counted whether or not the call fails, so a failing
    sweep does not turn every following tick into a retry of it. The heartbeat is what keeps this
    peer alive to the others (six missed ticks and it is pruned, sockets and tickets cascading away), so
    it must never be rolled back by a lock wait, a deadlock or a fault in the N upserts and four DELETEs
    beside it — and it must not hold this peer's row lock across them either, since every node connect
    (`node_presence.record_connected`) re-registers and would queue behind it."""
    from app.db import SessionLocal

    global _ticks
    sweep = _ticks % SWEEP_EVERY_TICKS == 0
    _ticks += 1
    with SessionLocal() as session:
        register(session)
        session.commit()
        pruned = housekeeping(session, local_sockets, sweep=sweep)
        session.commit()
        return pruned


# The heartbeat's own thread. NOT the default `to_thread` executor: that one is shared with every
# presence hop, every `route` holder read and the answer lock, and a converge fan-out could hold all of
# its workers for longer than the prune window — a live peer pruned for being busy. One dedicated worker
# cannot be starved by them, and one is enough (ticks are serial).
_heartbeat_executor: Optional[ThreadPoolExecutor] = None


def _heartbeat_thread() -> ThreadPoolExecutor:
    global _heartbeat_executor
    if _heartbeat_executor is None:
        _heartbeat_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cp-peer-heartbeat")
    return _heartbeat_executor


def _deregister_committed() -> None:
    from app.db import SessionLocal

    with SessionLocal() as session:
        session.execute(delete(CpPeer).where(CpPeer.id == self_id()))
        session.commit()


async def _heartbeat_forever() -> None:
    """The lifespan actor: tick every `HEARTBEAT_INTERVAL_SECONDS`; a failed tick logs and the next one
    retries, so a transient DB fault never ends the heartbeat (and so never gets this peer pruned).
    The socket snapshot is taken here, on the loop that owns the registry, and handed to the thread."""
    from app import node_bus  # lazy: see `housekeeping`

    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            local_sockets = node_bus.registry.local_sockets()
            await loop.run_in_executor(_heartbeat_thread(), _tick_committed, local_sockets)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.warning("cp-peers: heartbeat tick failed (%s) — retrying next interval", exc)


async def start(app: Any) -> None:
    """Register this peer and spawn the heartbeat. `app.state.cp_peer_task` holds the task,
    `app.state.cp_peer_started_at` the row's `started_at`."""
    if secret_is_ephemeral():
        # INFO, not a warning: only a process that skipped the boot check gets here (the published dev
        # constant counts as unset); a `./dc` stack and a deployed task always have a real secret.
        log.info(
            "cp-peers: SESSION_SECRET is unset (or the published dev constant), so this peer's token is "
            "per-process random and no other peer can authenticate to it — forwarding between control-"
            "plane tasks would fail. Harmless on a one-process stack."
        )
    app.state.cp_peer_started_at = await asyncio.to_thread(_register_committed)
    log.info("cp-peers: registered peer %s at %s", self_id(), self_address())
    app.state.cp_peer_task = asyncio.create_task(_heartbeat_forever())


async def stop(app: Any) -> None:
    """Cancel the heartbeat and delete this peer's row (best effort: the next tick elsewhere prunes it
    anyway, and its socket rows cascade with it)."""
    task = getattr(app.state, "cp_peer_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        app.state.cp_peer_task = None
    # The tick thread first, and WAITED for: a tick already running on it would re-insert this peer's
    # row after the delete below, showing a gone peer as live for a stale window (as after a crash).
    global _heartbeat_executor
    if _heartbeat_executor is not None:
        executor, _heartbeat_executor = _heartbeat_executor, None
        await asyncio.to_thread(executor.shutdown, True)
    try:
        await asyncio.to_thread(_deregister_committed)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("cp-peers: could not delete this peer's row on stop (%s)", exc)
    await aclose_running_loop_client()


# ── Forwarding ──────────────────────────────────────────────────────────────────────────────────────


def peer_ws_url(address: str, path: str) -> str:
    """A peer's WebSocket URL for `path`: `http` -> `ws`, `https` -> `wss`."""
    if address.startswith("https://"):
        base = "wss://" + address[len("https://"):]
    elif address.startswith("http://"):
        base = "ws://" + address[len("http://"):]
    else:
        base = address
    return base.rstrip("/") + "/" + path.lstrip("/")


def _client() -> httpx.AsyncClient:
    """The forwarding client of the RUNNING loop.

    An `httpx.AsyncClient` is bound to the loop it first sends on: httpcore's pooled connections hold that
    loop's streams, and reused from another loop after the first one closed every send raised
    `RuntimeError: Event loop is closed` (main, 2026-10-14). A forward does run off the serving
    loop: `routes/commands.py::dispatch_and_audit_sync` dispatches on a throwaway `asyncio.run` loop. So
    there is one client per loop, carried by the loop: the serving loop's is closed in `stop`, a throwaway
    loop's by `aclose_running_loop_client` before that loop ends (`dispatch_and_audit_sync`). A client a
    test installed (`_http`) is served to every loop."""
    if _http is not None:
        return _http
    loop = asyncio.get_running_loop()
    client = getattr(loop, _LOOP_CLIENT_ATTR, None)
    if client is None:
        client = httpx.AsyncClient(follow_redirects=False)
        setattr(loop, _LOOP_CLIENT_ATTR, client)
    return client


async def aclose_running_loop_client() -> None:
    """Close and forget the running loop's own client (never a test-installed one). A throwaway loop calls
    this before it ends, so its keep-alive sockets go with it instead of outliving it."""
    loop = asyncio.get_running_loop()
    client = getattr(loop, _LOOP_CLIENT_ATTR, None)
    if client is None:
        return
    delattr(loop, _LOOP_CLIENT_ATTR)
    try:
        await client.aclose()
    except Exception as exc:  # noqa: BLE001 — a client that will not close cleanly is not worth a failed call
        log.debug("cp-peers: forwarding client did not close cleanly (%s)", exc)


def _headers(node_id: str) -> dict[str, str]:
    """The auth header, or `NodeNotConnected`: without a shared secret no peer can accept the call."""
    global _no_token_warned
    from app import node_bus

    if secret_is_ephemeral():
        if not _no_token_warned:
            _no_token_warned = True
            log.warning("cp-peers: no shared SESSION_SECRET — cannot forward to another peer")
        raise node_bus.NodeNotConnected(node_id)
    return {"X-Agent-Token": self_token()}


async def _post(address: str, path: str, node_id: str, body: dict, *, timeout: float) -> httpx.Response:
    """One POST to a peer; every transport failure is `NodeNotConnected`, an HTTP-level timeout past the
    node's own deadline is `NodeRequestTimeout`. `node_bus` is imported lazily so it stays stdlib-only
    for the jaahd contract test."""
    from app import node_bus

    headers = _headers(node_id)
    try:
        return await _client().post(f"{address}{path}", json=body, headers=headers, timeout=timeout)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Nothing was sent: the holder may be re-routed to (`node_bus.HolderRefused`).
        raise node_bus.HolderRefused(node_id) from exc
    except httpx.TimeoutException as exc:
        raise node_bus.NodeRequestTimeout(node_id, timeout) from exc
    except httpx.HTTPError as exc:
        # The request MAY have reached the holder (a read error, a reset mid-response): plain
        # `NodeNotConnected`, so no caller retries a command the node may already be running.
        log.warning("cp-peers: forward to %s failed (%s)", address, exc)
        raise node_bus.NodeNotConnected(node_id) from exc


async def forward_request(
    address: str, node_id: str, envelope: dict, *, timeout: float, frame_type: str,
) -> bytes:
    """Ask the peer at `address` to send `envelope` down its socket for `node_id` and return the reply
    bytes. 503 -> `NodeNotConnected` (the holder lost the socket), 504 -> `NodeRequestTimeout`; any other
    non-200 is logged and read as not connected. The HTTP timeout is the node's deadline plus 5s, so the
    holder's own 504 arrives first."""
    from app import node_bus

    body = {"node_id": node_id, "envelope": envelope, "timeout": timeout, "frame_type": frame_type}
    resp = await _post(address, _FORWARD_REQUEST_PATH, node_id, body, timeout=timeout + 5)
    if resp.status_code == 200:
        return str(resp.json()["data"]).encode("utf-8")
    if resp.status_code == 504:
        raise node_bus.NodeRequestTimeout(node_id, timeout)
    if resp.status_code == 503:
        raise node_bus.HolderRefused(node_id)   # no socket there: the row was stale, re-routable
    log.warning(
        "cp-peers: peer %s answered %d to a forwarded request for %s: %s",
        address, resp.status_code, node_id, resp.text[:200],
    )
    raise node_bus.NodeNotConnected(node_id)


async def forward_notify(address: str, node_id: str, frame: dict) -> None:
    """Ask the peer at `address` to send one reply-less `frame` down its socket for `node_id`. 204 is
    success; 503, a connection error or any other status is `NodeNotConnected`."""
    from app import node_bus

    resp = await _post(
        address, _FORWARD_NOTIFY_PATH, node_id, {"node_id": node_id, "frame": frame}, timeout=10.0,
    )
    if resp.status_code == 204:
        return
    if resp.status_code == 503:
        raise node_bus.HolderRefused(node_id)
    log.warning(
        "cp-peers: peer %s answered %d to a forwarded notify for %s: %s",
        address, resp.status_code, node_id, resp.text[:200],
    )
    raise node_bus.NodeNotConnected(node_id)


def _reset_for_tests() -> None:
    """Forget the per-process identity, address cache and client, so a test can re-resolve."""
    global _self_id, _self_address, _self_task_id, _started_at, _http, _no_token_warned
    _self_id = None
    _self_address = None
    _self_task_id = None
    _started_at = None
    _http = None
    _no_token_warned = False
