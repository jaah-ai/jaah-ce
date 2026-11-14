"""THE REQUEST RELAY: a console request re-sent over a fresh connection when its own connection went silent.

Some networks (an ISP's DPI box, a corporate TLS-inspecting firewall) silently kill a browser's HTTP/2 connection
to the console after a handful of requests; the browser keeps sending on it and only opens a new one ~20 s later,
and no page script can force that sooner. A WebSocket, though, is always a NEW TCP connection here (the load
balancer offers no WebSocket-over-HTTP/2), so the console races a request unanswered after a second over this socket
(`apps/web/src/net/relay.ts`), and the first answer wins. The path stays `/api/read-relay` for both directions
(reads and writes), so nginx, the WAF and the load balancer need nothing new.

Each relayed request is REPLAYED IN-PROCESS against this same app, as an ASGI call built here from the socket's
handshake (its cookie, client address and forwarding headers) plus the frame's method and body, so every middleware
and authorization check judges it exactly as it would have judged the direct request. That includes the
idempotency middleware: a relayed write carries the SAME `Idempotency-Key` as its direct copy, and that middleware,
not this module, applies the key at most once. No idempotency logic lives here; the relay only refuses a write
that carries no well-formed key, so every relayed write reaches the app under the key its direct copy carries.

The socket itself grants nothing. A handshake must come from the console's own page (`origin_matches`). With a
session it is a CONSOLE socket; without one (no cookie, or one that no longer reads as a session) it is an
ANONYMOUS socket for the cookieless Live Cast viewer, which carries only `GET /api/public/live/*` (anything else:
`refused_path`) and replays no cookie and no identity header. What every socket refuses, so it can never reach
further than the browser's own `/api/`: a method outside `_METHODS`; a machine-surface path the public listener
refuses (`apps/web/nginx.conf.template`'s `$refuse_machine_surface_raw`), a long-lived stream, the sign-in surface
(`/api/auth/me` is relayed as a GET only), a write to the public unauthenticated surface (`/api/public/`), and
any path whose raw form could normalize somewhere else
(`$refuse_public_raw_normalized_split`); any header beyond the console's own few (`_MESSAGE_HEADERS`) — the identity
headers come from the handshake only.

What it bounds, since the public WAF sees only the handshake (not a relayed write's body): a write body is at most
`MAX_WRITE_BODY_BYTES` decoded (a larger upload goes direct only); a response is abandoned the moment it starts as a
stream or a download (`_STREAM_TYPES`, an attachment) or grows past `MAX_BODY_BYTES`, and every request, read or
write, ends at `READ_TIMEOUT_SECONDS`; a socket takes at most `MAX_READS_PER_SOCKET` frames. A console session has no
socket, in-flight or rate cap of its own (R4): each request it carries meets its own route's limits, as it would direct.
Per anonymous client address (`websocket.client.host`: `app/proxy_headers.py`'s trusted-hop reading of `X-Forwarded-For`, never
the client-supplied leftmost hop, which a caller could vary to get a fresh bucket per frame; an IPv6 client: per /64):
one shared budget (`_anon_limiter`, a `SharedRateLimiter` counted in the database, so one budget across every CP
task) of `ANON_BURST` per `ANON_WINDOW_SECONDS` — the burst of `ANON_BURST` and the sustained `ANON_READS_PER_SECOND`
— that every handshake and every read draws on (so reopening sockets buys no fresh frames); and, in this process, at
most `MAX_SOCKETS_PER_IP` open sockets and `MAX_IN_FLIGHT_PER_IP` requests being replayed, resource bounds kept per
process by decision (a count of open sockets or pending replays is not a sliding window; the abuse throttle is the
shared budget).

Wire, JSON text frames, at most `MAX_FRAME_CHARS` on a console socket and `ANON_MAX_FRAME_CHARS` on an anonymous one.
In: `{"id", "method"?, "path", "headers", "body"?}` — `method` defaults to `GET`; `body`, base64, is carried for a
non-GET only; `headers` may name `idempotency-key` (required on a non-GET, `IDEMPOTENCY_KEY` shape) and
`content-type`. Or `{"cancel": id}`: the direct copy of that read won, so its replay is cancelled and answers nothing
(a write is never cancelled — its key is the idempotency middleware's to settle); it is not a request and does not
count toward `MAX_READS_PER_SOCKET`; it is at most `MAX_CANCEL_FRAME_CHARS`, and a socket takes at most
`MAX_READS_PER_SOCKET` of them (one per read it could have sent) before it is closed, so cancels buy no free frames. Out: `{"id", "ack": true}` the moment a request is taken (the browser's proof this socket is alive),
then `{"id", "status", "headers", "body"}`, the body base64 and the headers only `_ANSWER_HEADERS`, or `{"id",
"error"}` when the relay will not answer it (the browser then keeps waiting on the direct request): `bad_path`,
`refused_path`, `bad_method`, `no_key`, `bad_body`, `too_large`, `rate_limited`, `not_relayable`, `redirect`,
`timeout`, `failed`. A redirect is not followed and comes back as an error."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import functools
import ipaddress
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.auth import COOKIE_NAME, read_session
from app.authz.catalog import Tenancy
from app.authz.declarations import always, session_only
from app.login_oauth import console_origins
from app.ratelimit import SharedRateLimiter

log = logging.getLogger(__name__)

router = APIRouter()

RELAY_PATH = "/api/read-relay"
#: A socket that has carried nothing this long is closed; the console opens another on its next race.
IDLE_SECONDS = 120.0
#: One relayed request's own bound, read or write: past it the browser has long since given up on it.
READ_TIMEOUT_SECONDS = 30.0
#: Frames one socket ever takes (`net/relay.ts::MAX_READS_PER_SOCKET`, where the console moves to a new socket).
MAX_READS_PER_SOCKET = 12
#: A `{"cancel": id}` frame's ceiling: an id and its envelope, nothing a padded frame could make the loop parse.
MAX_CANCEL_FRAME_CHARS = 64
#: A console socket's frame: room for a `MAX_WRITE_BODY_BYTES` body in base64 (4/3) plus its envelope.
MAX_FRAME_CHARS = 1_572_864
MAX_PATH_CHARS = 4_096
MAX_BODY_BYTES = 4 * 1024 * 1024
#: A relayed write's body, decoded; a larger upload goes direct only.
MAX_WRITE_BODY_BYTES = 1024 * 1024
#: The anonymous (Live Cast viewer) socket: reads of this prefix only, bounded per client address.
ANON_PATH_PREFIX = "/api/public/live/"
ANON_MAX_FRAME_CHARS = 16_384
ANON_BURST = 30
ANON_READS_PER_SECOND = 5.0
#: The shared window that holds both: at most `ANON_BURST` in any `ANON_BURST / ANON_READS_PER_SECOND` seconds.
ANON_WINDOW_SECONDS = ANON_BURST / ANON_READS_PER_SECOND
#: Per process, per client address: a file-descriptor bound (module docstring), not the abuse throttle.
MAX_SOCKETS_PER_IP = 4
MAX_IN_FLIGHT_PER_IP = 24
#: The key shape the idempotency middleware accepts (`Idempotency-Key`); the relay checks only that one is there.
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{22,64}")

#: Close code for a handshake the relay cannot replay as anyone: no client address (the 4401 the authorization guard
#: uses for a refused socket).
CLOSE_NO_SESSION = 4401
CLOSE_BAD_ORIGIN = 4403
#: Policy violation: a socket past its read quota, or a frame the console never sends.
CLOSE_POLICY = 1008

#: The headers the console itself sets on a request (`apps/web/src/lib/activeOrg.ts`, `lib/actAs.ts`, `lib/activity.ts`,
#: `authz/guard.py::ORG_SCOPE_HEADER`); everything else a message names is dropped.
_MESSAGE_HEADERS = frozenset(
    {"accept", "content-type", "idempotency-key", "x-jaah-org", "x-jaah-act-as", "x-jaah-org-scope", "x-activity"}
)
#: An anonymous socket's message may name only these: it has no session for an org or act-as header to narrow.
_ANON_MESSAGE_HEADERS = frozenset({"accept"})
#: The handshake's headers every replay carries: who and where the browser is, as nginx and the ALB named it.
_HANDSHAKE_HEADERS = ("host", "cookie", "user-agent", "accept-language", "x-forwarded-for", "x-real-ip")
#: An anonymous socket's: the same, never its cookie.
_ANON_HANDSHAKE_HEADERS = tuple(name for name in _HANDSHAKE_HEADERS if name != "cookie")
#: What a frame may ask for; anything else is `bad_method`.
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
#: Paths the relay never replays: the machine surface (refused on the public listener), the long-lived streams,
#: the sign-in surface (its own per-IP limits live at the WAF), and itself. Prefix match, case-insensitive.
_REFUSED_PREFIXES = (
    "/api/agent",
    "/api/node-bus",
    "/api/console-stream",
    "/api/console-relay",
    "/api/stream",
    "/api/auth",
    RELAY_PATH,
)
#: The one sign-in route the console reads on every load; relayed as a GET only (the auth surface's writes stay
#: behind the edge).
_ALLOWED_EXACT = frozenset({"/api/auth/me"})
#: A path whose raw form could normalize elsewhere: a dot segment, any percent-escape, a backslash, a fragment, an
#: empty segment.
_UNSAFE_PATH = re.compile(r"(\.\.|%|\\|#|//)")
#: Writes the relay never carries: the public, unauthenticated surface (the console never writes to it, and its
#: bodies belong to the WAF).
_WRITE_REFUSED_PREFIX = "/api/public/"
#: Response headers a `fetch` caller reads; `set-cookie` never (a relayed request cannot set a cookie).
#: `server-timing` too: the console tells a slow server from a dead connection by it (`net/relay.ts`), and its
#: slow-read reports say how long the app held a relayed read (`app/server_timing.py`).
_ANSWER_HEADERS = frozenset({
    "content-type", "content-disposition", "x-next-cursor", "etag", "cache-control", "idempotent-replayed", "location",
    "server-timing",
})
#: Content types that stream rather than answer: abandoned at their first byte.
_STREAM_TYPES = frozenset(
    {"text/event-stream", "application/x-ndjson", "application/gzip", "application/octet-stream", "application/zip"}
)


def refusal(path: Any, method: str = "GET") -> str | None:
    """Why the relay will not replay `method path`, or None when it will."""
    if not isinstance(path, str) or not path.startswith("/api/") or len(path) > MAX_PATH_CHARS:
        return "bad_path"
    bare = path.split("?", 1)[0].lower()
    if _UNSAFE_PATH.search(bare):
        return "bad_path"
    if bare in _ALLOWED_EXACT and method == "GET":
        return None
    if any(bare == prefix or bare.startswith(prefix + "/") for prefix in _REFUSED_PREFIXES):
        return "refused_path"
    if method != "GET" and bare.startswith(_WRITE_REFUSED_PREFIX):
        return "refused_path"
    return None


def address_key(host: str) -> tuple[str, str]:
    """An anonymous socket's budget key: its client address, or an IPv6 client's /64 (one subscriber's whole range,
    which it could otherwise rotate through for a fresh bucket each time). A tuple, never equal to a session id."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ("ip", host)
    if address.version == 6:
        return ("ip", str(ipaddress.ip_network(f"{address}/64", strict=False)))
    return ("ip", str(address))


def origin_matches(websocket: WebSocket) -> bool:
    """Whether the handshake comes from the console's own page: an allowlisted console origin
    (`login_oauth.console_origins`), or this very host on this socket's own scheme. A browser always sends `Origin` on
    a WebSocket; the session cookie is SameSite=lax, which still lets a same-site sibling host open one. The allowlist
    covers the local dev proxy, which rewrites `Host`."""
    origin = (websocket.headers.get("origin") or "").rstrip("/").lower()
    if not origin:
        return False
    if origin in (allowed.lower() for allowed in console_origins()):
        return True
    host = websocket.headers.get("host")
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    return host is not None and origin == f"{scheme}://{host.lower()}"


def _message_headers(raw: Any, allowed: frozenset[str] = _MESSAGE_HEADERS) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(name).lower(): str(value)
        for name, value in raw.items()
        if isinstance(name, str) and name.lower() in allowed and isinstance(value, (str, int))
    }


def has_key(headers: dict[str, str]) -> bool:
    """Whether a write names a well-formed key; its judgement (fresh, replayed, reused) is the idempotency middleware's."""
    return IDEMPOTENCY_KEY.fullmatch(headers.get("idempotency-key", "")) is not None


def body_refusal(body: Any) -> tuple[str | None, bytes]:
    """Why the relay will not carry this base64 write `body`, or None and the decoded body."""
    if body is None:
        return None, b""
    if not isinstance(body, str):
        return "bad_body", b""
    # Refused before decoding: base64 is 4 characters per 3 bytes.
    if len(body) > (MAX_WRITE_BODY_BYTES + 2) // 3 * 4:
        return "too_large", b""
    try:
        decoded = base64.b64decode(body, validate=True)
    except ValueError:
        return "bad_body", b""
    if len(decoded) > MAX_WRITE_BODY_BYTES:
        return "too_large", b""
    return None, decoded


#: Every handshake and read of an anonymous socket, per client address, across every CP task.
_anon_limiter = SharedRateLimiter(ANON_BURST, ANON_WINDOW_SECONDS, bucket="read-relay-anon")
#: Open anonymous sockets and their requests being replayed, per client address, in this process.
_open_sockets: dict[Any, int] = {}
_in_flight: dict[Any, int] = {}


async def _anon_take(key: tuple[str, str]) -> bool:
    """One draw on the address's shared budget. Off the loop: the limiter is a short DB transaction."""
    return await run_in_threadpool(_anon_limiter.allow, ":".join(key))


def _count(counts: dict[Any, int], key: Any, step: int) -> None:
    left = counts.get(key, 0) + step
    if left > 0:
        counts[key] = left
    else:
        counts.pop(key, None)


def _forget(reads: dict[int, asyncio.Task[None]], read_id: int, _done: object) -> None:
    """A finished GET is no longer one a `cancel` frame can stop."""
    reads.pop(read_id, None)


class _Abandon(Exception):
    """Raised from the replay's `send` to stop a response the relay will not carry."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Replayer:
    """Replays one socket's requests against this app as the socket's own browser."""

    def __init__(self, websocket: WebSocket, client: tuple[str, int], *, anonymous: bool = False) -> None:
        self._headers = [
            (name.encode("latin-1"), websocket.headers[name].encode("latin-1"))
            for name in (_ANON_HANDSHAKE_HEADERS if anonymous else _HANDSHAKE_HEADERS)
            if name in websocket.headers
        ]
        self._client = client
        self._scheme = "https" if websocket.url.scheme == "wss" else "http"
        self._server = (websocket.url.hostname or "localhost", websocket.url.port or (443 if self._scheme == "https" else 80))

    async def answer(self, path: str, headers: dict[str, str], method: str = "GET", body: bytes = b"") -> dict[str, Any]:
        # A fresh context: the replay starts as an inbound request does, carrying nothing of the socket's.
        task = asyncio.get_running_loop().create_task(
            self._call(path, headers, method, body), context=contextvars.Context()
        )
        try:
            return await asyncio.wait_for(task, READ_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return {"error": "timeout"}

    def _scope(self, path: str, headers: dict[str, str], method: str = "GET", body: bytes = b"") -> dict[str, Any]:
        parts = urlsplit(path)
        message = [(name.encode("latin-1"), value.encode("latin-1")) for name, value in headers.items()]
        if method != "GET":
            message.append((b"content-length", str(len(body)).encode("ascii")))
        return {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": self._scheme, "path": parts.path, "raw_path": parts.path.encode("ascii"),
            "query_string": parts.query.encode("latin-1"), "root_path": "",
            # Handshake headers last and authoritative: a message can name none of them (`_MESSAGE_HEADERS`).
            "headers": message + self._headers, "client": self._client, "server": self._server,
        }

    async def _call(self, path: str, headers: dict[str, str], method: str, request_body: bytes) -> dict[str, Any]:
        from app.main import app  # lazily: app.main imports every route, this module among them

        start: dict[str, Any] = {}
        body = bytearray()
        sent_request = False
        abandoned: list[str] = []

        def abandon(reason: str) -> None:
            abandoned.append(reason)
            raise _Abandon(reason)

        async def receive() -> dict[str, Any]:
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            await asyncio.Event().wait()  # no disconnect until the call is abandoned (its task cancelled)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if abandoned:
                raise _Abandon(abandoned[0])  # a middleware answering the abandonment is not heard either
            if message["type"] == "http.response.start":
                start.update(message)
                found = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message.get("headers", [])}
                if 300 <= message["status"] < 400:
                    abandon("redirect")
                media = found.get("content-type", "").split(";", 1)[0].strip().lower()
                if media in _STREAM_TYPES or found.get("content-disposition", "").lower().startswith("attachment"):
                    abandon("not_relayable")
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                if len(body) > MAX_BODY_BYTES:
                    abandon("too_large")

        try:
            await app(self._scope(path, headers, method, request_body), receive, send)
        except Exception:  # noqa: BLE001 — an abandonment, however a middleware re-raised or wrapped it, or the route's own
            if not abandoned:
                raise
        if abandoned:
            return {"error": abandoned[0]}
        if "status" not in start:
            return {"error": "failed"}
        answer_headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in start.get("headers", [])
            if k.decode("latin-1").lower() in _ANSWER_HEADERS
        }
        return {"status": start["status"], "headers": answer_headers, "body": base64.b64encode(bytes(body)).decode("ascii")}


@router.websocket(RELAY_PATH)
@always(Tenancy.ORG_SCOPED)
@session_only
async def read_relay(websocket: WebSocket) -> None:
    """The console's request relay (module docstring). `ALWAYS`: the socket itself authorizes nothing; every request
    it carries is authorized by its own route, replayed as the browser. With no session it is the anonymous Live Cast
    socket, keyed by client address."""
    if not origin_matches(websocket):
        await websocket.close(code=CLOSE_BAD_ORIGIN)
        return
    # No client address, no replay: a stand-in address would be judged as some other peer (fail closed).
    if websocket.client is None:
        await websocket.close(code=CLOSE_NO_SESSION)
        return
    claims = read_session(websocket.cookies.get(COOKIE_NAME))
    if claims is not None:
        await _relay(websocket, claims.admin_id)
        return
    key = address_key(websocket.client.host)
    if _open_sockets.get(key, 0) >= MAX_SOCKETS_PER_IP:
        await websocket.close(code=CLOSE_POLICY)
        return
    # Counted before the shared draw awaits, so concurrent handshakes cannot all pass the socket bound.
    _count(_open_sockets, key, 1)
    try:
        if not await _anon_take(key):
            await websocket.close(code=CLOSE_POLICY)
            return
        await _relay(websocket, key, anonymous=True)
    finally:
        _count(_open_sockets, key, -1)


async def _frame_refusal(
    message: dict[str, Any], key: Any, anonymous: bool
) -> tuple[str | None, str, dict[str, str], bytes]:
    """Why the relay will not take this frame, or None; and the request it carries (method, headers, body)."""
    method = message.get("method", "GET")
    path = message.get("path")
    if anonymous:
        headers = _message_headers(message.get("headers"), _ANON_MESSAGE_HEADERS)
        if method != "GET" or not isinstance(path, str) or not path.startswith(ANON_PATH_PREFIX):
            return "refused_path", "GET", headers, b""
    else:
        headers = _message_headers(message.get("headers"))
    if not isinstance(method, str) or method.upper() not in _METHODS:
        return "bad_method", "GET", headers, b""
    method = method.upper()
    why = refusal(path, method)
    if why is not None:
        return why, method, headers, b""
    if method != "GET" and not has_key(headers):
        return "no_key", method, headers, b""
    # The anonymous bounds before anything else is spent on the frame.
    if anonymous and (_in_flight.get(key, 0) >= MAX_IN_FLIGHT_PER_IP or not await _anon_take(key)):
        return "rate_limited", method, headers, b""
    if method == "GET":
        return None, method, headers, b""
    why, body = body_refusal(message.get("body"))
    return why, method, headers, body


async def _relay(websocket: WebSocket, key: Any, *, anonymous: bool = False) -> None:
    """One accepted socket's life: take requests, replay each, answer each."""
    if websocket.client is None:  # `read_relay` already closed such a socket; restated for the type
        return
    await websocket.accept()
    replayer = _Replayer(websocket, (websocket.client.host, websocket.client.port), anonymous=anonymous)
    max_frame = ANON_MAX_FRAME_CHARS if anonymous else MAX_FRAME_CHARS
    who = "anon" if anonymous else key
    send_lock = asyncio.Lock()
    pending: set[asyncio.Task[None]] = set()
    #: The GETs being replayed, by frame id: what a `cancel` frame may stop.
    reads: dict[int, asyncio.Task[None]] = {}
    taken = 0
    cancels = 0

    async def send(frame: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(frame))

    async def serve(read_id: Any, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        try:
            frame = await replayer.answer(path, headers, method, body)
        except Exception as exc:  # noqa: BLE001 — a failed replay is that request's error, never the socket's
            # The type only: an exception's text could carry the request's headers.
            log.warning("read relay: replay failed (%s)", type(exc).__name__)
            frame = {"error": "failed"}
        # Answered: past here a cancel has nothing to stop, and must not interrupt the answer's send.
        if isinstance(read_id, int):
            reads.pop(read_id, None)
        # The access log nginx keeps for the direct request; never the query, which can carry a token.
        log.info("read relay: admin=%s %s %s -> %s (%d bytes)", who, method, path.split("?", 1)[0],
                 frame.get("status", frame.get("error")), len(frame.get("body", "")) * 3 // 4)
        try:
            await send({"id": read_id, **frame})
        except Exception:  # noqa: BLE001 — the browser went away (any transport's spelling of it); nothing to answer
            pass

    try:
        while True:
            try:
                received = await asyncio.wait_for(websocket.receive(), IDLE_SECONDS)
            except asyncio.TimeoutError:
                break
            raw = received.get("text")
            # A disconnect, or a binary frame the console never sends.
            if received.get("type") != "websocket.receive" or not isinstance(raw, str) or len(raw) > max_frame:
                break
            try:
                message = json.loads(raw)
            except ValueError:
                break
            if not isinstance(message, dict):
                break
            if "cancel" in message:
                cancels += 1
                if len(raw) > MAX_CANCEL_FRAME_CHARS or cancels > MAX_READS_PER_SOCKET:
                    await websocket.close(code=CLOSE_POLICY)
                    break
                running = reads.get(message["cancel"]) if isinstance(message["cancel"], int) else None
                if running is not None:
                    running.cancel()
                continue
            taken += 1
            if taken > MAX_READS_PER_SOCKET:
                await websocket.close(code=CLOSE_POLICY)
                break
            read_id = message.get("id")
            why, method, headers, body = await _frame_refusal(message, key, anonymous)
            path = message.get("path")
            if why is not None or not isinstance(path, str):
                await send({"id": read_id, "error": why or "bad_path"})
                continue
            if anonymous:
                _count(_in_flight, key, 1)
            await send({"id": read_id, "ack": True})
            task = asyncio.create_task(serve(read_id, method, path, headers, body))
            pending.add(task)
            task.add_done_callback(pending.discard)
            if method == "GET" and isinstance(read_id, int):
                reads[read_id] = task
                task.add_done_callback(functools.partial(_forget, reads, read_id))
            if anonymous:
                # On the task, not in it: a task cancelled before it ever ran still gives its slot back.
                task.add_done_callback(lambda _done: _count(_in_flight, key, -1))
    except WebSocketDisconnect:
        pass
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed
