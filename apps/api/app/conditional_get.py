"""Answer an unchanged node roster with a body-less 304: the fingerprint (ETag) half of conditional GET.

Nodes poll a few rosters on a timer (`apps/jaahd/control_plane_api.py::CONDITIONAL_PATHS`, the twin of
`CONDITIONAL_PATHS` here — change both or neither). The handler still runs in full — authorization, scoping and
the read are untouched — and only its 200 body is replaced: hashed, stamped as `ETag`, and dropped for a 304 when
the request's `If-None-Match` already names that hash. So a 304 tells the caller only "what you already hold is
still the answer", never anything it could not read. Pure ASGI rather than `BaseHTTPMiddleware`, whose per-request
task and stream wrapping cost more than these small bodies do.
"""
from __future__ import annotations

import hashlib
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

# Exact paths: `/api/agent/accounts/<slug>/api-key` must never be fingerprinted or cached.
CONDITIONAL_PATHS = frozenset({"/api/agent/pool-tick", "/api/agent/accounts", "/api/agent/worktrees"})

# Dropped from a 304, which carries no body to describe.
_BODY_HEADERS = frozenset({b"content-length", b"content-type", b"content-encoding", b"etag"})


def fingerprint(body: bytes) -> bytes:
    """The strong ETag for `body`: a quoted, truncated SHA-256 (collision-safe at this scale, short on the wire)."""
    return b'"' + hashlib.sha256(body).hexdigest()[:32].encode() + b'"'


def _requested_tags(scope: Scope) -> set[bytes]:
    for name, value in scope.get("headers") or ():
        if name.lower() == b"if-none-match":
            # A weak tag (`W/"…"`, a proxy that recompressed) names the same body here: compare its opaque part.
            return {tag.strip().removeprefix(b"W/") for tag in value.split(b",") if tag.strip()}
    return set()


class ConditionalGetMiddleware:
    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "GET" or scope.get("path") not in CONDITIONAL_PATHS:
            await self.app(scope, receive, send)
            return
        requested = _requested_tags(scope)
        start: Message | None = None
        chunks: list[bytes] = []
        passthrough = False

        async def capture(message: Message) -> None:
            nonlocal start, passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                if message.get("status") != 200:
                    # Errors and redirects go out untouched.
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            etag = fingerprint(body)
            headers = [(k, v) for k, v in start.get("headers", ()) if k.lower() != b"etag"]
            if etag in requested:
                headers = [(k, v) for k, v in headers if k.lower() not in _BODY_HEADERS]
                await send({"type": "http.response.start", "status": 304, "headers": [*headers, (b"etag", etag)]})
                await send({"type": "http.response.body", "body": b""})
                return
            await send({**start, "headers": [*headers, (b"etag", etag)]})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, capture)
