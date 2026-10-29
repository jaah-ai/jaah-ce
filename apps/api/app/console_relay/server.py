"""The relay's routes and byte pump (package docstring has the whole flow).

Three doors, each accept-then-close on refusal (a close code needs an accepted connection):
  * `BROWSER_PATH` — the browser's leg, authorized by its single-use ticket alone (the API checked the
    viewer when it minted it).
  * `NODE_PATH` — the node's dial-back: two factors, a fleet `NODE_TOKEN` and the node ticket, bound to
    the dialling node's id.
  * `OWNER_LEG_PATH` — another relay task handing over a leg it claimed for a session this task owns;
    the bearer must be the control plane's own `cp_self` token, never a node's.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional
from urllib.parse import quote

from fastapi import APIRouter, WebSocket
from websockets.asyncio.client import connect as _ws_connect_following_redirects
from websockets.exceptions import ConnectionClosed

from app import cp_peers
from app.authz.catalog import Tenancy
from app.authz.declarations import always, infra
from app.console_relay import tickets
from app.node_bus import resolve_caller_async

router = APIRouter(tags=["console"])
log = logging.getLogger("app.console_relay")

# Browser close codes (`apps/web/src/components/ConsolePane.tsx` keys its reconnect on them).
BROWSER_CLOSE_UNAUTHORIZED = 4401   # the viewer's login session was revoked — terminal
BROWSER_CLOSE_RELAY_FAILED = 4503   # the transport broke, the node-side session lives on — reconnect
BROWSER_CLOSE_IDLE = 4408           # a HIPAA org's console with no keystroke for 15 minutes — terminal
BROWSER_CLOSE_IDLE_REASON = "closed after 15 minutes of inactivity"
BROWSER_CLOSE_NETWORK = 4403        # the viewer's address left the org's IP allowlist — terminal
BROWSER_CLOSE_NETWORK_REASON = "your network is no longer allowed"
# Why `touch_forever` ended a session (`_Pair.end`).
END_REVOKED = "revoked"
END_IDLE = "idle"
END_NETWORK = "network"
END_UNVERIFIED = "unverified"
# A refused node or owner-leg dial: one code for unknown, expired, replayed and wrong-node alike.
CLOSE_UNKNOWN_TICKET = 4404

# Log-line bound on the dialling node's self-asserted id. Truncated only for the log.
_LOGGED_NODE_ID_CHARS = 128
# The owner is on the same private network; a dial that takes longer is a dead task.
_OWNER_DIAL_TIMEOUT = 5.0
# Close codes a peer may SEND (RFC 6455 §7.4) — 1005/1006 are only ever received. Mirrors
# `websockets.frames.Close.check()`; anything else is forwarded as 1000.
_SENDABLE_CLOSE_CODES = frozenset({1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014})
# The largest frame the proxy's client leg accepts: uvicorn's server-side `--ws-max-size` default
# (16 MiB), so a proxied paste fits where a direct one does (`websockets`' client default is 1 MiB).
PROXY_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class _OwnerLegConnect(_ws_connect_following_redirects):
    """`websockets.connect` that refuses a redirect: it would re-send the `cp_self` token wherever
    `Location` points, and the owner is a fixed `ip:port` that never redirects."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


def _logged_node_id(node_id: object) -> str:
    text = node_id if isinstance(node_id, str) else ""
    return text[:_LOGGED_NODE_ID_CHARS] or "<none>"


def _forwardable_close(code: object, reason: object) -> tuple[int, str]:
    """A close as received on one leg, in a form the other leg may send: an unsendable code becomes 1000,
    the reason is cut to RFC 6455's 123 BYTES."""
    sendable = code if isinstance(code, int) and (
        code in _SENDABLE_CLOSE_CODES or 3000 <= code < 5000
    ) else 1000
    text = reason if isinstance(reason, str) else ""
    return sendable, text.encode("utf-8")[:123].decode("utf-8", "ignore")


async def _refuse(websocket: WebSocket, code: int, reason: str) -> None:
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


# ── The rendezvous ──────────────────────────────────────────────────────────────────────────────────


class _Pair:
    """One session's two legs as this task holds them. The second leg to attach runs the pump; the first
    parks on `done`."""

    __slots__ = ("key", "read_only", "legs", "both", "done", "ended", "end_reason", "typed")

    def __init__(self, key: str, read_only: bool) -> None:
        self.key = key
        self.read_only = read_only
        self.legs: dict[str, WebSocket] = {}
        self.both = asyncio.Event()
        self.done = asyncio.Event()
        # Set by `touch_forever` (`end`): `END_REVOKED` when the API's sweep revoked the viewer (4401, final),
        # `END_IDLE` when it closed a HIPAA org's console for inactivity (4408, final), `END_NETWORK` when the
        # viewer's address left the org's IP allowlist (4403, final),
        # `END_UNVERIFIED` when the row vanished or went unread too long — the session can no longer be
        # vouched for, so the browser is closed for a reconnect through the API (4503).
        self.ended = asyncio.Event()
        self.end_reason: str | None = None
        # A keystroke arrived since `touch_forever` last reported one (`_is_keystroke`).
        self.typed = False

    def end(self, reason: str) -> None:
        """End the session for `reason`; the first reason stands."""
        if self.end_reason is None:
            self.end_reason = reason
        self.ended.set()


class Rendezvous:
    """The sessions this relay task owns, keyed by the session key (`tickets.Claim.key`). Process-wide."""

    def __init__(self) -> None:
        self._pairs: dict[str, _Pair] = {}

    def keys(self) -> list[str]:
        return list(self._pairs)

    def pair(self, key: str) -> Optional[_Pair]:
        return self._pairs.get(key)

    async def attach(self, key: str, leg: str, websocket: WebSocket, *, read_only: bool, wait: float) -> None:
        """Hold `websocket` as `leg` of session `key` until the session ends. The leg that completes the
        pair runs the pump; a leg whose partner never arrives within `wait` seconds is closed — the
        browser with 4503 (the pane re-opens), the node with a plain detach."""
        pair = self._pairs.get(key)
        if pair is None:
            pair = self._pairs[key] = _Pair(key, read_only)
        if leg in pair.legs:
            # Unreachable through `tickets.claim` (single-use per leg); refused rather than displacing.
            await websocket.close(code=CLOSE_UNKNOWN_TICKET, reason="leg already attached")
            return
        pair.legs[leg] = websocket
        if len(pair.legs) == len(tickets.LEGS):
            pair.both.set()
            try:
                await _pump(pair)
            finally:
                pair.done.set()
                self._pairs.pop(key, None)
                await asyncio.to_thread(tickets.finish, key)
            return
        try:
            await asyncio.wait_for(pair.both.wait(), timeout=wait)
        except asyncio.CancelledError:
            # A parked leg cancelled before its partner came (a shutdown): leave no half pair behind.
            if self._pairs.get(key) is pair and not pair.both.is_set():
                self._pairs.pop(key, None)
            raise
        except asyncio.TimeoutError:
            if self._pairs.get(key) is pair and not pair.both.is_set():
                self._pairs.pop(key, None)
                log.info("console-relay: the %s leg waited %.0fs for its partner and was closed", leg, wait)
                code = BROWSER_CLOSE_RELAY_FAILED if leg == tickets.LEG_BROWSER else 1000
                try:
                    await websocket.close(code=code, reason="the other end did not arrive")
                except Exception:  # noqa: BLE001 — already gone
                    pass
                return
        await pair.done.wait()

    def _reset_for_tests(self) -> None:
        self._pairs.clear()


rendezvous = Rendezvous()


# The pane's client frame: a 1-byte type, a 2-byte length, the payload (`ConsolePane.tsx`). Input and the two
# paste types are a person typing; resize is not, and neither is the pane's zero-length keepalive input.
_FRAME_HEADER_BYTES = 3
_KEYSTROKE_FRAME_TYPES = frozenset({0, 2, 3})


def _is_keystroke(data: bytes) -> bool:
    return len(data) > _FRAME_HEADER_BYTES and data[0] in _KEYSTROKE_FRAME_TYPES


async def _pump(pair: _Pair) -> None:
    """Bidirectional byte pump: browser keystrokes → the node's PTY socket, PTY output → the browser.

    `read_only` discards every browser frame (resize included — tmux sizes a shared session to its
    smallest client) while still reading them, the only detector of the browser leaving. The browser's
    close says why: 1000 when the node ended the session (4000), 4401 when the viewer was revoked, 4408
    when a HIPAA org's console idled out, 4403 when the viewer's address left the org's IP allowlist, 4503
    when only the transport broke. Closing the node leg is this
    viewer's detach, never a kill."""
    browser, node = pair.legs[tickets.LEG_BROWSER], pair.legs[tickets.LEG_NODE]
    relay_failed = False
    revoked = False
    idled = False
    blocked = False

    async def _watch() -> None:
        nonlocal revoked, idled, blocked, relay_failed
        await pair.ended.wait()
        if pair.end_reason == END_IDLE:
            idled = True
        elif pair.end_reason == END_NETWORK:
            blocked = True
        elif pair.end_reason == END_REVOKED:
            revoked = True
        else:
            relay_failed = True

    async def _browser_to_node() -> None:
        nonlocal relay_failed
        while True:
            try:
                message = await browser.receive()
            except (RuntimeError, OSError):
                return   # the browser is already gone
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                text = message.get("text")
                data = text.encode() if isinstance(text, str) else None
            if not data:
                continue
            # Before the read-only discard: a watcher typing is still a person at the console.
            if _is_keystroke(data):
                pair.typed = True
            if pair.read_only:
                continue
            try:
                await node.send_bytes(data)
            except Exception as exc:  # noqa: BLE001 — the node leg died; reconnect
                log.warning("console-relay: stdin to node failed, closing for reconnect: %s", exc)
                relay_failed = True
                return

    async def _node_to_browser() -> None:
        nonlocal relay_failed
        while True:
            try:
                message = await node.receive()
            except Exception as exc:  # noqa: BLE001 — the node leg died; reconnect
                log.warning("console-relay: node stream read failed: %s", exc)
                relay_failed = True
                return
            if message.get("type") == "websocket.disconnect":
                # Anything but the explicit terminal reason leaves the browser free to retry.
                if message.get("code") != tickets.NODE_CLOSE_SESSION_ENDED:
                    relay_failed = True
                return
            data = message.get("bytes")
            if data is None:
                text = message.get("text")
                data = text.encode() if isinstance(text, str) else None
            if not data:
                continue
            try:
                await browser.send_bytes(data)
            except Exception:  # noqa: BLE001 — the browser is gone; nobody left to reconnect
                return

    tasks = [asyncio.ensure_future(_browser_to_node()), asyncio.ensure_future(_node_to_browser()),
             asyncio.ensure_future(_watch())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.warning("console-relay: pump died unexpectedly: %s", task.exception())
                relay_failed = True
    except asyncio.CancelledError:
        # Our own shutdown leaves the node-side session alive, so it must not look terminal.
        relay_failed = True
        raise
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        try:
            await node.close()
        except Exception:  # noqa: BLE001 — already gone
            pass
        code, reason = (
            (BROWSER_CLOSE_IDLE, BROWSER_CLOSE_IDLE_REASON) if idled
            else (BROWSER_CLOSE_NETWORK, BROWSER_CLOSE_NETWORK_REASON) if blocked
            else (BROWSER_CLOSE_UNAUTHORIZED, "") if revoked
            else (BROWSER_CLOSE_RELAY_FAILED, "") if relay_failed
            else (1000, "")
        )
        try:
            await browser.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 — already gone
            pass


async def touch_forever() -> None:
    """Every `SEEN_INTERVAL_SECONDS`, mark this task's live sessions seen and learn which are revoked.
    One query for all of them, run by the relay app's lifespan (`main.py`).

    FAILS CLOSED: a revocation is only ever learned here, so once the rows have gone unread for
    `STALE_AFTER_SECONDS` every live session is closed for a reconnect — the pane re-opens through the
    API, which re-checks the viewer. A shorter DB fault ends nothing."""
    unread_since: float | None = None
    while True:
        await asyncio.sleep(tickets.SEEN_INTERVAL_SECONDS)
        live = [key for key in rendezvous.keys() if (pair := rendezvous.pair(key)) and pair.both.is_set()]
        if not live:
            unread_since = None
            continue
        typed = [key for key in live if (pair := rendezvous.pair(key)) and pair.typed]
        _set_typed(typed, False)
        try:
            touched = await asyncio.to_thread(tickets.touch, live, typed)
        except Exception as exc:  # noqa: BLE001 — retried next tick, until the fail-closed bound below
            log.warning("console-relay: touching live sessions failed (%s)", exc)
            _set_typed(typed, True)
            now = asyncio.get_running_loop().time()
            unread_since = unread_since if unread_since is not None else now
            if now - unread_since >= tickets.STALE_AFTER_SECONDS:
                log.warning("console-relay: sessions unverifiable for %.0fs — closing them for a reconnect",
                            now - unread_since)
                _end(live, END_UNVERIFIED)
            continue
        unread_since = None
        _end(touched.idled, END_IDLE)
        _end(touched.blocked, END_NETWORK)
        _end(touched.revoked, END_REVOKED)
        _end(touched.vanished, END_UNVERIFIED)


def _set_typed(keys: Iterable[str], typed: bool) -> None:
    for key in keys:
        if (pair := rendezvous.pair(key)) is not None:
            pair.typed = typed


def _end(keys: Iterable[str], reason: str) -> None:
    """End each of this task's sessions in `keys` for `reason`."""
    for key in keys:
        if (pair := rendezvous.pair(key)) is not None:
            log.info("console-relay: a session is %s — closing its browser leg", reason)
            pair.end(reason)


# ── The doors ───────────────────────────────────────────────────────────────────────────────────────


async def _route(websocket: WebSocket, leg: str, claim: tickets.Claim) -> None:
    """Attach a claimed leg here, or hand it to the task that owns its session."""
    if claim.owner_address == cp_peers.self_address():
        await rendezvous.attach(
            claim.key, leg, websocket, read_only=claim.read_only, wait=claim.seconds_left,
        )
        return
    await _proxy_to_owner(websocket, claim, leg)


def client_address(websocket: WebSocket) -> str | None:
    """The browser's address as the API saw it at the mint (`request.client.host`, `app/ip_allowlist.py`). The
    relay has no nginx sidecar, so an ALB is its direct peer: the RIGHTMOST `X-Forwarded-For` entry is the one
    the ALB appended — the address the API's uvicorn reads through nginx — and a client-forged prefix is never
    read. Every header line is joined first, as uvicorn's `ProxyHeadersMiddleware` joins them for the API, so a
    second line cannot make the two disagree. With no header (the local stack's dev proxy, which forwards none
    to either) it is the peer itself."""
    lines = websocket.headers.getlist("x-forwarded-for")
    if lines:
        return ", ".join(lines).rsplit(",", 1)[-1].strip() or None
    return websocket.client.host if websocket.client else None


@router.websocket(tickets.BROWSER_PATH)
# Authorized per subject in the handler: the single-use browser ticket the API minted for this viewer.
@always(Tenancy.ORG_SCOPED)
async def browser_leg(websocket: WebSocket) -> None:
    """The browser's leg. Every refusal is 4503: the pane re-opens through the API, which mints afresh."""
    try:
        claim = await asyncio.to_thread(
            tickets.claim, tickets.LEG_BROWSER, websocket.query_params.get("ticket"),
            self_address=cp_peers.self_address(), client_ip=client_address(websocket),
        )
    except Exception as exc:  # noqa: BLE001 — a DB fault: the pane re-opens, as for any broken transport
        log.warning("console-relay: claiming a browser leg failed (%s)", exc)
        await _refuse(websocket, BROWSER_CLOSE_RELAY_FAILED, "console relay unavailable")
        return
    if claim is None:
        log.warning(
            "console-relay: refused a browser leg — unknown, expired, revoked or replayed ticket, or another address"
        )
        await _refuse(websocket, BROWSER_CLOSE_RELAY_FAILED, "console ticket is not valid")
        return
    await websocket.accept()
    await _route(websocket, tickets.LEG_BROWSER, claim)


@router.websocket(tickets.NODE_PATH)
@infra(Tenancy.ORG_SCOPED)
async def node_leg(websocket: WebSocket) -> None:
    """The node's dial-back for one console session: its own `NODE_TOKEN` (a header only — a query string
    lands in access logs) plus its node ticket, which is bound to the node it was minted for. The binding
    is checked against the node the TOKEN names, never the self-asserted `NODE_ID_HEADER` (logged only),
    so one node's credential cannot redeem another node's ticket. The control plane's own arms name no
    node and are refused here."""
    caller = await resolve_caller_async(websocket.headers.get("x-agent-token"))
    node_id = str(caller.node_id) if caller is not None and caller.node_id is not None else None
    if node_id is None:
        log.warning("console-relay: rejected a node dial without a node's own agent token")
        await _refuse(websocket, 4401, "unauthorized")
        return
    try:
        claim = await asyncio.to_thread(
            tickets.claim, tickets.LEG_NODE, websocket.query_params.get("ticket"),
            self_address=cp_peers.self_address(), node_id=node_id,
        )
    except Exception as exc:  # noqa: BLE001 — a DB fault refuses like an unknown ticket; the browser re-opens
        log.warning("console-relay: claiming a node leg failed (%s)", exc)
        await _refuse(websocket, CLOSE_UNKNOWN_TICKET, "unknown ticket")
        return
    if claim is None:
        log.warning(
            "console-relay: refused a dial from node %s — unknown, expired, replayed, or not this "
            "ticket's node", _logged_node_id(node_id),
        )
        await _refuse(websocket, CLOSE_UNKNOWN_TICKET, "unknown ticket")
        return
    await websocket.accept()
    log.info("console-relay: node %s attached a PTY", _logged_node_id(node_id))
    await _route(websocket, tickets.LEG_NODE, claim)


@router.websocket(tickets.OWNER_LEG_PATH)
@infra(Tenancy.OPERATOR)
async def owner_leg(websocket: WebSocket) -> None:
    """A leg another relay task claimed for a session THIS task owns. The leg was already claimed (and so
    validated) there; this door only checks the bearer is the control plane itself, not a node."""
    caller = await resolve_caller_async(websocket.headers.get("x-agent-token"))
    if caller is None or caller.is_derived:
        log.warning("console-relay: rejected an owner-leg dial without the control plane's own token")
        await _refuse(websocket, 4401, "unauthorized")
        return
    key = websocket.query_params.get("key") or ""
    leg = websocket.query_params.get("leg")
    if leg not in tickets.LEGS or not key:
        await _refuse(websocket, CLOSE_UNKNOWN_TICKET, "unknown ticket")
        return
    try:
        wait = max(float(websocket.query_params.get("wait") or 0), 0.0)
    except ValueError:
        wait = 0.0
    await websocket.accept()
    await rendezvous.attach(
        key, leg, websocket, read_only=websocket.query_params.get("read_only") == "1", wait=wait,
    )


async def _proxy_to_owner(websocket: WebSocket, claim: tickets.Claim, leg: str) -> None:
    """Pump a claimed leg to the relay task that owns its session and back, transparently — bytes, text and
    close codes both ways. An owner that cannot be reached closes the leg like a broken transport."""
    failed_code = BROWSER_CLOSE_RELAY_FAILED if leg == tickets.LEG_BROWSER else CLOSE_UNKNOWN_TICKET
    if cp_peers.secret_is_ephemeral():
        log.warning("console-relay: no shared SESSION_SECRET — cannot hand a leg to its owner")
        await websocket.close(code=failed_code, reason="relay owner unreachable")
        return
    query = (
        f"key={quote(claim.key, safe='')}&leg={leg}&read_only={'1' if claim.read_only else '0'}"
        f"&wait={claim.seconds_left:.3f}"
    )
    url = f"{cp_peers.peer_ws_url(claim.owner_address, tickets.OWNER_LEG_PATH)}?{query}"
    try:
        client = await asyncio.wait_for(
            _OwnerLegConnect(
                url,
                additional_headers={"X-Agent-Token": cp_peers.self_token()},
                max_size=PROXY_MAX_MESSAGE_BYTES,
                # No permessage-deflate on any console hop (package docstring).
                compression=None,
            ),
            timeout=_OWNER_DIAL_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — every dial failure closes alike; the reason is logged
        log.warning("console-relay: could not reach the session's owner at %s (%s)", claim.owner_address, exc)
        await websocket.close(code=failed_code, reason="relay owner unreachable")
        return
    log.info("console-relay: proxying a %s leg to its owner %s", leg, claim.owner_address)
    # An owner that dies without a close frame must not read as "session ended" (1000) to a browser, which
    # would stop reconnecting: it is a broken transport, 4503. A node leg reads 1000 as a plain detach.
    await _pump_between(websocket, client, owner_lost=(failed_code if leg == tickets.LEG_BROWSER else 1000))


async def _pump_between(websocket: Any, client: Any, *, owner_lost: int = 1000) -> None:
    """The two pumps of `_proxy_to_owner`, over a connected `client` (duck-typed: `send`, `recv`, `close`,
    `recv` raising `ConnectionClosed`). Each side's close reaches the other with its code; an owner gone
    without one closes the leg with `owner_lost`."""
    leg_to_owner = None
    owner_to_leg = None

    async def _pump_leg_to_owner() -> Optional[tuple[int, str]]:
        while True:
            try:
                message = await websocket.receive()
            except (RuntimeError, OSError):
                return None
            if message.get("type") == "websocket.disconnect":
                return _forwardable_close(message.get("code"), message.get("reason"))
            data = message.get("bytes")
            payload = data if data is not None else message.get("text")
            if payload is None:
                continue
            try:
                await client.send(payload)
            except Exception as exc:  # noqa: BLE001 — the owner leg died; the other pump reports why
                log.warning("console-relay: proxy send to owner failed: %s", exc)
                return None

    async def _pump_owner_to_leg() -> Optional[tuple[int, str]]:
        while True:
            try:
                frame = await client.recv()
            except ConnectionClosed as exc:
                rcvd = exc.rcvd
                return _forwardable_close(rcvd.code, rcvd.reason) if rcvd is not None else None
            except Exception as exc:  # noqa: BLE001 — any other read fault ends the proxy
                log.warning("console-relay: proxy read from owner failed: %s", exc)
                return None
            try:
                if isinstance(frame, (bytes, bytearray, memoryview)):
                    await websocket.send_bytes(bytes(frame))
                else:
                    await websocket.send_text(frame)
            except Exception:  # noqa: BLE001 — the leg's socket is gone
                return None

    owner_close: Optional[tuple[int, str]] = None
    leg_close: Optional[tuple[int, str]] = None
    try:
        leg_to_owner = asyncio.ensure_future(_pump_leg_to_owner())
        owner_to_leg = asyncio.ensure_future(_pump_owner_to_leg())
        done, _ = await asyncio.wait({leg_to_owner, owner_to_leg}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled() or task.exception() is not None:
                if not task.cancelled():
                    log.warning("console-relay: proxy pump died unexpectedly: %s", task.exception())
                continue
            if task is owner_to_leg:
                owner_close = task.result()
            else:
                leg_close = task.result()
    finally:
        pending = [task for task in (leg_to_owner, owner_to_leg) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        code, reason = owner_close or (owner_lost, "")
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 — the leg may already be gone
            pass
        code, reason = leg_close or (1000, "")
        try:
            await client.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 — the owner may already be gone
            pass
