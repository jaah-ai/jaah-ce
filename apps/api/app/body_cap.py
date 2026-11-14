"""The request-body ceiling nginx used to enforce, kept where the load balancers dial the API directly.

WHY THIS EXISTS. nginx's console listeners set no `client_max_body_size`, so its 1 MiB default bounded every
`/api` body, and its hooks listener allowed 2 MiB (`apps/web/nginx.conf.template`). Granian sets no limit, and
FastAPI reads a JSON body whole before validating it: without this, a few unauthenticated multi-GB POSTs to a
public route would exhaust a one-worker task. So the API refuses an oversized body itself, with a 413:

* `DEFAULT_LIMIT` (1 MiB, nginx's default) for every path but these;
* `HOOKS_LIMIT` (2 MiB, the hooks listener's) for the hooks allow-list (`ingress_surface.HOOKS_ALLOWED_PATHS`);
* nothing for `TRIGGERS_PREFIX`, whose route picks each delivery's cap before reading (`app/trigger_intake.py`,
  up to 25 MiB for a signed record-only GitHub delivery) and streams the rest;
* nothing for the peer surface (`ingress_surface.CP_PEER_PREFIX`), dialled task to task on `ip:8000` and never
  through nginx, so it never had this ceiling (the guard refuses it to any request through a load balancer).

A declared `Content-Length` over the limit is refused before a byte is read; a chunked body is counted as it
arrives and cut at the limit. Off where nginx still fronts the API (the community edition, the local stack):
inert unless the ingress guard is configured (`app/ingress_surface.py`), as nginx's own cap applies there.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

from app.ingress_surface import CP_PEER_PREFIX, HOOKS_ALLOWED_PATHS, IngressConfig, hooks_allows, normalized_path

if TYPE_CHECKING:  # pragma: no cover
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_LIMIT = 1024 * 1024
HOOKS_LIMIT = 2 * 1024 * 1024
TRIGGERS_PREFIX = "/api/public/triggers/"
TOO_LARGE_DETAIL = "Request body too large"

assert TRIGGERS_PREFIX + "*" in HOOKS_ALLOWED_PATHS


def limit_for(path: str) -> Optional[int]:
    """The body ceiling for `path` (as the router sees it), or None where the route caps its own body."""
    normal = normalized_path(path)
    if normal.startswith((TRIGGERS_PREFIX, CP_PEER_PREFIX)):
        return None
    return HOOKS_LIMIT if hooks_allows(normal) else DEFAULT_LIMIT


class BodyTooLarge(Exception):
    """Raised from the wrapped `receive` once a streamed body passes its ceiling."""


async def _refuse(send: "Send") -> None:
    body = json.dumps({"detail": TOO_LARGE_DETAIL}).encode()
    await send({"type": "http.response.start", "status": 413, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close"),
    ]})
    await send({"type": "http.response.body", "body": body})


class BodyCapMiddleware:
    """Pure ASGI: a 413 for a body over `limit_for(path)` (module docstring)."""

    def __init__(self, app: "ASGIApp", *, enabled: Optional[bool] = None) -> None:
        self.app = app
        self.enabled = IngressConfig.from_env() is not None if enabled is None else enabled

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        limit = limit_for(str(scope.get("path") or "")) if self.enabled and scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = [value for name, value in scope.get("headers") or [] if name.lower() == b"content-length"]
        if declared and (not declared[0].strip().isdigit() or int(declared[0]) > limit):
            await _refuse(send)
            return
        seen = 0
        started = False

        async def capped() -> "Message":
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge()
            return message

        async def tracked(message: "Message") -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, capped, tracked)
        except BodyTooLarge:
            if not started:
                await _refuse(send)
