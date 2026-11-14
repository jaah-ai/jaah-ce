"""Console write idempotency: a write carrying an `Idempotency-Key` is applied at most once.

The console may send one write twice — direct and over the WebSocket relay when the direct connection looks dead,
or as a retry — always under ONE key it mints per user action. This middleware makes the server apply that key at
most once and answer every copy with the first copy's answer.

WHAT IT COVERS: `POST|PUT|PATCH|DELETE` on `/api/*` from a console session (the cookie session
`SessionAuthMiddleware` resolved: `state.admin_id` with `state.console_session`), carrying the header. Machine
callers (node and agent tokens, API keys, `/api/agent/*`, `/api/node-bus*`) and every request without the header
pass untouched. A key not of `KEY_PATTERN`'s shape is refused `400 idempotency_key_invalid`.

THE ANSWERS, scope `(admin_id, key)`, fingerprint `method`, path with query, and a keyed digest of the body and the
`FINGERPRINT_HEADERS` (an act-as target changes what a write means):

* first sight — a `running` claim is COMMITTED before the route runs (claim-before-run: storing only after the
  route would double-apply a second copy that arrives while the first is still running). The route runs; its
  status, `STORED_HEADERS` and body (up to `MAX_BYTES`) are stored the moment its last body chunk is sent (not
  after its background tasks), and the row turns `done`.
* same fingerprint, `done` — the stored answer is replayed with `Idempotent-Replayed: true`, after the session is
  re-checked live (a signed-out cookie replays nothing); the route never runs. A stored answer whose body was over
  `MAX_BYTES` (status only) answers `409 idempotency_replay_unavailable`, as does one claimed while its user already
  held `MAX_ROWS_PER_ADMIN` live keys: past that budget the write still runs, its answer is kept as status
  only, so one user cannot grow the table with large answers for a whole `RETENTION`.
* same fingerprint, `running` — the copy waits, re-reading every `POLL_SECONDS` for up to `WAIT_SECONDS`, and
  replays on `done`; past that, `409 idempotency_in_progress`. How many copies may wait is not capped.
* `running` with a heartbeat older than `STALE_SECONDS` — the claiming process died mid-route:
  `409 idempotency_outcome_unknown`. Never re-run.
* different fingerprint — `422 idempotency_key_reused`; nothing runs.
* a row older than `RETENTION` is ignored as absent (that key is a first sighting again) and pruned by the
  `request-idempotency-prune` cron (`prune`).

Every completed status is stored, 4xx and 5xx included. An unhandled route exception before the answer completed
is stored as `500 {"detail": "Internal Server Error"}` and re-raised. A route CANCELLED mid-run (a `BaseException`,
e.g. the relay's `wait_for` timing out) before it answered stores nothing: the heartbeat stops, and the claim goes
stale into `idempotency_outcome_unknown` — never re-applied.

AT REST: a stored answer is sealed (`app/credential_crypto.py::seal`, AAD bound to the row and its claim), since a
write may answer a secret exactly once (a node token, an API key, recovery codes); a replay takes status, headers
and body from the envelope only. The fingerprint is an HMAC under a key derived from the session secret, never a
bare hash of a password-bearing body.

ACCEPTED: a replay skips the route's authorization by design. A user demoted within the window can still replay,
under the same key and fingerprint, an answer they already received; nothing is applied again.

A request body over `MAX_BYTES` gets no idempotency: a `content-length` above it passes untouched, unbuffered; a
chunked one is buffered up to it, then replayed to the app followed by the rest of the stream, with no claim.

PLACEMENT (`app/main.py`, pinned): directly inside `StaleTabGuardMiddleware`, so an org-switched or
unauthenticated request is refused before a key is claimed; outside `SessionRefreshMiddleware` and
`UserActivityMiddleware`, so a replay re-mints no cookie and records no second activity row; outside the route's
authz dependency, so a replay serves exactly what the authorized first copy got, and only to the same `admin_id`.
Pure ASGI, not `BaseHTTPMiddleware`, which breaks body and context binding (`app/user_activity.py`).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

HEADER = b"idempotency-key"
REPLAYED_HEADER = "Idempotent-Replayed"
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{22,64}")
METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Machine planes under `/api` that never carry the console's key semantics.
MACHINE_PREFIXES = ("/api/agent/", "/api/node-bus")
#: The response headers a replay carries; every other header is per-response. `cache-control` so a secret's
#: `no-store` rides its replay too.
STORED_HEADERS = frozenset({"content-type", "location", "content-disposition", "cache-control"})
#: Request headers that change what a write means, folded into the fingerprint (`app/act_as.py`).
FINGERPRINT_HEADERS = (b"x-jaah-act-as", b"x-jaah-org-scope")

#: The request body and stored answer cap.
MAX_BYTES = 1024 * 1024
#: How long a key is honoured; past it the row is ignored and pruned.
RETENTION = timedelta(hours=24)
#: Live keys per user whose answers are stored in full; past it a claimed write's answer is stored as status only.
MAX_ROWS_PER_ADMIN = 2000
#: A waiting copy's re-read interval and bound. The copy waits on the loop (`asyncio.sleep`) and re-reads on the
#: async pool: it holds no thread and no connection between reads.
POLL_SECONDS = 0.25
WAIT_SECONDS = 25.0
#: The claiming request refreshes `heartbeat_at` this often while the route runs; a claim whose heartbeat is
#: older than `STALE_SECONDS` belongs to a dead process.
HEARTBEAT_SECONDS = 10.0
STALE_SECONDS = 30.0

KEY_INVALID = "idempotency_key_invalid"
KEY_REUSED = "idempotency_key_reused"
IN_PROGRESS = "idempotency_in_progress"
OUTCOME_UNKNOWN = "idempotency_outcome_unknown"
REPLAY_UNAVAILABLE = "idempotency_replay_unavailable"

_INTERNAL_ERROR_BODY = b'{"detail":"Internal Server Error"}'
_SEAL_AAD = b"request-idempotency|v1|"

# The claim: a fresh row, or a row past `RETENTION` taken over in place. The row lock `ON CONFLICT DO UPDATE` takes
# makes two expired-key copies serialize, and only the first sees the old `created_at`. Its `created_at` is the
# claim's token: the heartbeat and the finish touch only the claim they made.
_SQL_CLAIM = text(
    "INSERT INTO request_idempotency (admin_id, key, method, path, body_sha256, state, headers, created_at, "
    "heartbeat_at) VALUES (:admin_id, :key, :method, :path, :sha, 'running', '{}'::jsonb, now(), now()) "
    "ON CONFLICT (admin_id, key) DO UPDATE SET method = EXCLUDED.method, path = EXCLUDED.path, "
    "body_sha256 = EXCLUDED.body_sha256, state = 'running', status = NULL, headers = '{}'::jsonb, body = NULL, "
    "created_at = now(), heartbeat_at = now(), completed_at = NULL "
    "WHERE request_idempotency.created_at < now() - make_interval(secs => :retention) "
    "RETURNING created_at"
)
# The user's live keys, this claim included, counted only as far as the budget decides (`MAX_ROWS_PER_ADMIN`).
_SQL_LIVE_ROWS = text(
    "SELECT count(*) FROM (SELECT 1 FROM request_idempotency WHERE admin_id = :admin_id "
    "AND created_at >= now() - make_interval(secs => :retention) LIMIT :limit) AS live"
)
_SQL_READ = text(
    "SELECT method, path, body_sha256, state, status, headers, body, created_at, "
    "heartbeat_at < now() - make_interval(secs => :stale) AS stale FROM request_idempotency "
    "WHERE admin_id = :admin_id AND key = :key AND created_at >= now() - make_interval(secs => :retention)"
)
_CLAIMED = "admin_id = :admin_id AND key = :key AND created_at = :claimed_at AND state = 'running'"
_SQL_BEAT = text(f"UPDATE request_idempotency SET heartbeat_at = now() WHERE {_CLAIMED}")
_SQL_FINISH = text(
    "UPDATE request_idempotency SET state = 'done', status = :status, headers = :headers, body = :body, "
    f"completed_at = now() WHERE {_CLAIMED}"
).bindparams(bindparam("headers", type_=JSONB))
_SQL_PRUNE = text("DELETE FROM request_idempotency WHERE created_at < now() - make_interval(secs => :retention)")

# The answers being stored (`_store`), referenced until each is done. Touched only on the event loop.
_finishing: set[asyncio.Task] = set()


@dataclass(frozen=True)
class _Claim:
    admin_id: UUID
    key: str
    created_at: datetime
    #: Over the user's `MAX_ROWS_PER_ADMIN` budget: the answer is stored as status only.
    capped: bool = False


@dataclass(frozen=True)
class _Row:
    method: str
    path: str
    body_sha256: str
    state: str
    status: Optional[int]
    headers: dict[str, str]
    body: Optional[bytes]
    stale: bool


@dataclass
class _Answer:
    """What the route answered, as far as it got. `chunks` None: the body is not kept (over the cap)."""

    status: Optional[int] = None
    headers: dict[str, str] = field(default_factory=dict)
    chunks: Optional[list[bytes]] = field(default_factory=list)
    size: int = 0
    complete: bool = False
    stored: bool = False


def _session():  # type: ignore[no-untyped-def]
    """A short session on the async pool (`app/db_async.py`); every claim, read, beat and finish is one."""
    from app import db_async  # lazy: the module stays importable without an engine

    return db_async.async_session()


def _aad(admin_id: UUID, key: str, created_at: datetime) -> bytes:
    """Bound to the row AND the claim: an envelope moved to another row, or kept from an expired claim of the same
    key, does not open."""
    return _SEAL_AAD + f"{admin_id}|{key}|{created_at.isoformat()}".encode()


def _seal(claim: _Claim, status: int, headers: dict[str, str], body: bytes) -> bytes:
    """The whole answer in one envelope: the replay takes its status and headers from it too, never from the
    unauthenticated columns."""
    from app.credential_crypto import seal

    envelope = seal({"s": status, "h": headers, "b": base64.b64encode(body).decode()},
                    aad=_aad(claim.admin_id, claim.key, claim.created_at))
    return json.dumps(envelope, separators=(",", ":")).encode()


def _open(admin_id: UUID, key: str, created_at: datetime, stored: bytes) -> Optional[tuple[int, dict[str, str], bytes]]:
    """`(status, headers, body)` of a stored answer, or None when it does not open (its key was rotated out of the
    ring, or it was moved)."""
    from app.credential_crypto import unseal

    try:
        opened = unseal(json.loads(stored), aad=_aad(admin_id, key, created_at), label="request_idempotency.body")
        return int(opened["s"]), dict(opened["h"]), base64.b64decode(opened["b"])
    except (ValueError, KeyError, TypeError):
        log.warning("idempotency: a stored answer of admin %s did not open", admin_id)
        return None


def _fingerprint(scope: Scope, body: bytes) -> str:
    """HMAC-SHA256 of the meaning-bearing headers and the body, under a key derived from the session secret."""
    from app.auth import resolve_session_secret

    secret = hmac.new(resolve_session_secret().encode(), b"request-idempotency/v1", hashlib.sha256).digest()
    digest = hmac.new(secret, digestmod=hashlib.sha256)
    for name in FINGERPRINT_HEADERS:
        digest.update(name + b"=" + (_header(scope, name) or b"") + b"\n")
    digest.update(body)
    return digest.hexdigest()


async def _claim(admin_id: UUID, key: str, method: str, path: str, sha: str) -> Optional[_Claim]:
    """Insert (or take over an expired) `running` claim, committed; the claim when this request owns the key, marked
    `capped` when the user's live keys are past `MAX_ROWS_PER_ADMIN`."""
    retention = RETENTION.total_seconds()
    async with _session() as session, session.begin():
        created_at = (await session.execute(_SQL_CLAIM, {
            "admin_id": admin_id, "key": key, "method": method, "path": path, "sha": sha, "retention": retention,
        })).scalar()
        if created_at is None:
            return None
        live = (await session.execute(_SQL_LIVE_ROWS, {
            "admin_id": admin_id, "retention": retention, "limit": MAX_ROWS_PER_ADMIN + 1,
        })).scalar_one()
    capped = live > MAX_ROWS_PER_ADMIN
    if live == MAX_ROWS_PER_ADMIN + 1:  # logged as the budget is crossed, not on every write past it
        log.warning("idempotency: admin %s is past %d live keys; this answer is stored as status only",
                    admin_id, MAX_ROWS_PER_ADMIN)
    return _Claim(admin_id, key, created_at, capped)


async def _read(admin_id: UUID, key: str) -> Optional[_Row]:
    async with _session() as session:
        row = (await session.execute(_SQL_READ, {
            "admin_id": admin_id, "key": key, "stale": STALE_SECONDS, "retention": RETENTION.total_seconds(),
        })).one_or_none()
    if row is None:
        return None
    status, headers, body = row.status, dict(row.headers or {}), None
    if row.body is not None:
        opened = _open(admin_id, key, row.created_at, bytes(row.body))
        if opened is not None:
            status, headers, body = opened
    return _Row(row.method, row.path, row.body_sha256, row.state, status, headers, body, bool(row.stale))


def _claimed(claim: _Claim) -> dict[str, Any]:
    return {"admin_id": claim.admin_id, "key": claim.key, "claimed_at": claim.created_at}


async def _beat(claim: _Claim) -> None:
    async with _session() as session, session.begin():
        await session.execute(_SQL_BEAT, _claimed(claim))


async def _finish(claim: _Claim, status: int, headers: dict[str, str], body: Optional[bytes]) -> None:
    sealed = None if body is None else _seal(claim, status, headers, body)
    async with _session() as session, session.begin():
        await session.execute(_SQL_FINISH, {**_claimed(claim), "status": status, "headers": headers, "body": sealed})


def prune(session: Session) -> int:
    """Delete every key older than `RETENTION`; returns how many went. The `request-idempotency-prune` cron's job."""
    deleted = session.execute(_SQL_PRUNE, {"retention": RETENTION.total_seconds()}).rowcount or 0
    session.commit()
    return deleted


async def _session_live(scope: Scope) -> bool:
    """Whether the request's cookie session still holds (not signed out, disabled or idled out): a replay is served
    only to a live session, as the route's own principal check would have demanded."""
    from app import auth
    from app.stale_tab_guard import _live_active_org

    cookie = HTTPConnection(scope).cookies.get(auth.COOKIE_NAME)
    claims = auth.read_session(cookie)
    return claims is not None and await _live_active_org(claims) is not None


def _console_admin(scope: Scope) -> Optional[UUID]:
    """The admin of a console COOKIE session `SessionAuthMiddleware` resolved, else None (API key, node, public)."""
    state = scope.get("state") or {}
    admin_id = state.get("admin_id")
    if admin_id is None or state.get("console_session") is None:
        return None
    return admin_id if isinstance(admin_id, UUID) else UUID(str(admin_id))


def _header(scope: Scope, name: bytes) -> Optional[bytes]:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value
    return None


def _error(status: int, detail: str) -> Response:
    return JSONResponse({"detail": detail}, status_code=status)


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in METHODS
            or not scope["path"].startswith("/api/")
            or scope["path"].startswith(MACHINE_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        raw_key = _header(scope, HEADER)
        admin_id = _console_admin(scope)
        if raw_key is None or admin_id is None:
            await self.app(scope, receive, send)
            return
        key = raw_key.decode("latin-1")
        if not KEY_PATTERN.fullmatch(key):
            await _error(400, KEY_INVALID)(scope, receive, send)
            return
        length = _header(scope, b"content-length")
        if length is not None and length.isdigit() and int(length) > MAX_BYTES:
            await self.app(scope, receive, send)
            return

        buffered, body = await _buffer(receive)
        if body is None:
            # Over the cap (chunked) or the client left mid-body: no claim, the app sees the stream unchanged.
            await self.app(scope, _replaying(buffered, receive), send)
            return

        query = scope.get("query_string") or b""
        path = scope["path"] + ("?" + query.decode("latin-1") if query else "")
        method = scope["method"]
        sha = _fingerprint(scope, body)
        inner_receive = _replaying([{"type": "http.request", "body": body, "more_body": False}], receive)

        row: Optional[_Row] = None
        for _ in range(3):
            claim = await _claim(admin_id, key, method, path, sha)
            if claim is not None:
                await self._run(scope, inner_receive, send, claim)
                return
            row = await _read(admin_id, key)
            if row is not None:
                break
            # Pruned or expired between the claim and the read: claim again.
        await self._answer_copy(scope, receive, send, admin_id, key, row, (method, path, sha))

    async def _answer_copy(
        self, scope: Scope, receive: Receive, send: Send, admin_id: UUID, key: str, row: Optional[_Row],
        fingerprint: tuple[str, str, str],
    ) -> None:
        """A key already claimed: replay, wait, or refuse — never run the route."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAIT_SECONDS
        while True:
            response: Optional[Response] = None
            if row is None:
                response = _error(409, IN_PROGRESS)
            elif (row.method, row.path, row.body_sha256) != fingerprint:
                response = _error(422, KEY_REUSED)
            elif row.state == "done":
                response = await self._replay(scope, admin_id, row)
            elif row.stale:
                response = _error(409, OUTCOME_UNKNOWN)
            elif loop.time() >= deadline:
                response = _error(409, IN_PROGRESS)
            if response is not None:
                await response(scope, receive, send)
                return
            await asyncio.sleep(POLL_SECONDS)
            row = await _read(admin_id, key)

    async def _replay(self, scope: Scope, admin_id: UUID, row: _Row) -> Response:
        if not await _session_live(scope):
            return _error(401, "unauthorized")
        if row.body is None or row.status is None:
            return _error(409, REPLAY_UNAVAILABLE)
        log.info("idempotency: replayed %s %s (%s) to admin %s", row.method, row.path, row.status, admin_id)
        return Response(content=row.body, status_code=row.status, headers={**row.headers, REPLAYED_HEADER: "true"})

    async def _run(self, scope: Scope, receive: Receive, send: Send, claim: _Claim) -> None:
        """Run the route under this request's claim and store what it answered."""
        # Past the user's row budget the body is never kept, so it is not buffered either.
        answer = _Answer(chunks=None) if claim.capped else _Answer()

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                answer.status = message["status"]
                answer.headers = {
                    name.decode("latin-1").lower(): value.decode("latin-1")
                    for name, value in message.get("headers") or ()
                    if name.decode("latin-1").lower() in STORED_HEADERS
                }
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                answer.size += len(chunk)
                if answer.chunks is not None:
                    if answer.size > MAX_BYTES:
                        answer.chunks = None
                    else:
                        answer.chunks.append(chunk)
                answer.complete = not message.get("more_body", False)
            await send(message)
            if answer.complete and not answer.stored:
                # Stored now, not when the app returns: a route's background tasks run after its last chunk, and a
                # waiting copy must not wait them out.
                await _store(claim, answer)

        heartbeat = asyncio.create_task(_heartbeat(claim))
        try:
            await self.app(scope, receive, capture)
        except Exception:
            if not answer.stored:
                await _store(claim, _internal_error())
            raise
        except BaseException:
            # Cancelled before it answered: store nothing. The heartbeat stops, so the claim goes stale into
            # `idempotency_outcome_unknown` and is never re-applied.
            raise
        finally:
            # Awaited, not just cancelled: a beat mid-write must have landed or been abandoned before the route's
            # cancellation completes, or a late `heartbeat_at` bump would outlive the route it claimed to track.
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        if not answer.stored:
            # The app returned without completing an answer (a misbehaving route): stored as the server's 500.
            await _store(claim, _internal_error())


def _internal_error() -> _Answer:
    return _Answer(status=500, headers={"content-type": "application/json"}, chunks=[_INTERNAL_ERROR_BODY],
                   complete=True)


async def _store(claim: _Claim, answer: _Answer) -> None:
    answer.stored = True
    if answer.status is None:
        return
    body = b"".join(answer.chunks) if answer.chunks is not None else None
    try:
        # Its own task: `_store` runs inside the route's `send`, whose task may still hold the route's connection
        # (`app/db_async.py`: a task never opens a second); shielded, so a client gone mid-store still records it.
        finishing = asyncio.create_task(_finish(claim, answer.status, answer.headers, body))
        _finishing.add(finishing)  # held until done: a shielded task its awaiter abandoned is otherwise collectable
        finishing.add_done_callback(_finishing.discard)
        await asyncio.shield(finishing)
    except Exception:  # noqa: BLE001 — the answer is already sent; an unfinished claim goes stale, never re-runs
        log.exception("idempotency: could not store the answer for a key of admin %s", claim.admin_id)


async def _heartbeat(claim: _Claim) -> None:
    """Refresh the claim's `heartbeat_at` every `HEARTBEAT_SECONDS` until cancelled, so a long route is never
    mistaken for a dead process."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            await _beat(claim)
        except Exception:  # noqa: BLE001 — a missed beat only risks a later outcome_unknown, never a re-run
            log.warning("idempotency: heartbeat failed for admin %s", claim.admin_id, exc_info=True)


async def _buffer(receive: Receive) -> tuple[list[Message], Optional[bytes]]:
    """Read the request body up to `MAX_BYTES`. Returns the messages read and the whole body, or None for the body
    when it is over the cap or the client disconnected (the messages then replay to the app as they came)."""
    messages: list[Message] = []
    parts: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            return messages, None
        chunk = message.get("body", b"")
        size += len(chunk)
        parts.append(chunk)
        if size > MAX_BYTES:
            return messages, None
        if not message.get("more_body", False):
            return messages, b"".join(parts)


def _replaying(messages: list[Message], receive: Receive) -> Receive:
    """A `receive` that yields `messages` first, then delegates to the original (a later disconnect)."""
    pending: list[Any] = list(messages)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay
