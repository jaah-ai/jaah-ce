"""The browser's own address when the console is served through a CloudFront edge (Jaah's Terraform, edge.tf).

Through the edge, the ALB's appended `X-Forwarded-For` hop, which is what `app/proxy_headers.py` and `request_caller` read as the
client, is a CloudFront address shared by every viewer behind that point of presence. Every per-address control
would then key on CloudFront: rate limits, sign-in lockout, org IP allowlists, audit addresses and the console
relay's allowlist check. CloudFront sends the viewer's real address in `CloudFront-Viewer-Address`
(`<ip>:<port>`), but a caller reaching the ALB directly can send that header too. So it is believed ONLY on a
request that also carries the edge's origin secret (`EDGE_ORIGIN_HEADER`, value `EDGE_ORIGIN_SECRET`), which
CloudFront adds on its leg to the origin and a viewer never sees. Without the secret configured (dev, local, CE)
nothing here changes anything.

`EdgeViewerMiddleware` applies it to the API: the client and the right-most `X-Forwarded-For` hop become the
viewer, so `request.client.host` and `request_caller.client_headers` agree. The console relay, which has no such
middleware, calls `viewer_address` itself (`console_relay/server.py::client_address`). The secret header is
stripped either way, so no log or handler downstream ever holds it.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
from typing import Iterable, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

EDGE_ORIGIN_HEADER = "x-jaah-edge"
VIEWER_HEADER = "cloudfront-viewer-address"
_EDGE_ORIGIN_HEADER_BYTES = EDGE_ORIGIN_HEADER.encode()
_FORWARDED_FOR_BYTES = b"x-forwarded-for"


def _secret() -> str:
    return os.environ.get("EDGE_ORIGIN_SECRET", "")


def _parse_viewer(value: str) -> Optional[str]:
    """`198.51.100.10:46532` or `2001:db8::1:46532` (CloudFront appends the port after the last colon)."""
    host = value.strip().rsplit(":", 1)[0].strip("[]")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def viewer_address(headers: Iterable[tuple[str, str]]) -> Optional[str]:
    """The viewer's address when `headers` (lowercase name, value) prove they came through the edge, else None:
    exactly one line of each header (CloudFront sends one; a second line is not CloudFront's) and the secret
    matching."""
    secret = _secret()
    if not secret:
        return None
    pairs = list(headers)
    presented = [value for name, value in pairs if name == EDGE_ORIGIN_HEADER]
    viewers = [value for name, value in pairs if name == VIEWER_HEADER]
    if len(presented) != 1 or len(viewers) != 1:
        return None
    if not hmac.compare_digest(presented[0].encode(), secret.encode()):
        return None
    return _parse_viewer(viewers[0])


class EdgeViewerMiddleware:
    """Pure ASGI: rewrites the client of an edge-proven request to the viewer, and strips the origin secret."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        raw = scope.get("headers") or []
        if not any(name == _EDGE_ORIGIN_HEADER_BYTES for name, _ in raw):
            await self.app(scope, receive, send)
            return
        viewer = viewer_address((n.decode("latin-1"), v.decode("latin-1")) for n, v in raw)
        headers = [(n, v) for n, v in raw if n != _EDGE_ORIGIN_HEADER_BYTES]
        scope = dict(scope)
        if viewer is not None:
            port = scope["client"][1] if scope.get("client") else 0
            scope["client"] = (viewer, port)
            # The viewer becomes the right-most hop, which is the one `request_caller` reads as the client.
            forwarded = b", ".join(v for n, v in headers if n == _FORWARDED_FOR_BYTES)
            headers = [(n, v) for n, v in headers if n != _FORWARDED_FOR_BYTES]
            headers.append((_FORWARDED_FOR_BYTES, (forwarded + b", " if forwarded else b"") + viewer.encode()))
        scope["headers"] = headers
        await self.app(scope, receive, send)
