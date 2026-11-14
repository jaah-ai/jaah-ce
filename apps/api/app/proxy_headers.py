"""The client's real address behind a proxy: `X-Forwarded-For` / `X-Forwarded-Proto`, trusted from known proxies only.

WHY THIS EXISTS. Every request reaches the API through the nginx sidecar on 127.0.0.1, so the ASGI peer is always
nginx. Granian, the server, does not rewrite `scope["client"]` from proxy headers, and every per-client decision
keys on `request.client.host` — `app/routes/public.py::check_rate_limit` first among them. Without this layer every
caller would share ONE bucket: a public-login lockout of everyone.

THE RULE (as the ASGI ecosystem's proxy-header middlewares apply it). Only when the direct peer is a trusted proxy
(`trusted_networks()`: loopback — nginx's hop — plus `JAAH_TRUSTED_PROXY_CIDRS`, the VPC CIDR the ECS task definition
sets once the ALBs dial the API directly) are the headers read; then the client is the RIGHTMOST `X-Forwarded-For`
entry that is not itself a trusted proxy — the CloudFront edge or the hooks sender the ALB appended, never a
client-forged prefix — and the scheme is `X-Forwarded-Proto`'s. A peer trusted by CIDR (an ALB dialling the API
directly) is the ONE proxy hop, so only loopback entries are skipped behind it: its appended entry is the caller even
when that caller is itself inside the VPC, which would otherwise be skipped and hand the client to its forged prefix.
Only nginx's loopback hop, which forwards the ALB's own address, skips the configured CIDRs too. Every header line is joined first, so a second line cannot hide the ALB's entry.
A peer that is not trusted (a caller reaching port 8000 directly) keeps its own address, whatever it sends.
`EdgeViewerMiddleware` (`app/edge_viewer.py`) then replaces an edge-proven client with the viewer.

THE ORIGINAL PEER IS KEPT. Before any rewrite the TCP peer's host is stored at `scope["state"]["ingress_peer"]`, which
the ingress-surface guard (`app/ingress_surface.py`, directly inside this layer) decides on.

Outermost in `create_app` (`app/main.py`), so every layer and route sees the same address.
"""
from __future__ import annotations

import os
from typing import Iterable, Mapping, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from app.ingress_surface import LOOPBACK_NETWORKS, PEER_STATE_KEY, IPNetwork, in_networks, parse_networks

TRUSTED_PROXY_CIDRS_ENV = "JAAH_TRUSTED_PROXY_CIDRS"


def trusted_networks(environ: Optional[Mapping[str, str]] = None) -> tuple[IPNetwork, ...]:
    """Loopback (nginx in the same task; the console relay has none: `app/console_relay/server.py::client_address`)
    plus `JAAH_TRUSTED_PROXY_CIDRS`. Unset — the community edition, the local stack — is loopback only."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    return LOOPBACK_NETWORKS + parse_networks(env.get(TRUSTED_PROXY_CIDRS_ENV, ""))


_HTTPS_FOR = {"http": "https", "ws": "wss"}


def _joined(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str | None:
    values = [value.decode("latin-1") for key, value in headers if key.lower() == name]
    return ", ".join(values) if values else None


Trusted = Iterable[IPNetwork | str]
"""Networks, or a literal peer name (a test client's `testclient`)."""


def _is_trusted(host: str, trusted: tuple[IPNetwork | str, ...]) -> bool:
    return host in trusted or in_networks(host, (t for t in trusted if not isinstance(t, str)))


def client_from_forwarded_for(header: str, trusted: Trusted) -> str | None:
    """The rightmost entry of `header` that is not a trusted proxy, or None when there is none."""
    proxies = tuple(trusted)
    for entry in reversed(header.split(",")):
        host = entry.strip()
        if host and not _is_trusted(host, proxies):
            return host
    return None


class ProxyHeadersMiddleware:
    """Rewrite `scope["client"]` and `scope["scheme"]` from the proxy headers of a trusted peer (module docstring)."""

    def __init__(self, app: ASGIApp, trusted: Optional[Trusted] = None) -> None:
        self.app = app
        self.trusted = tuple(trusted) if trusted is not None else trusted_networks()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            scope = {**scope, "state": {**(scope.get("state") or {}), PEER_STATE_KEY: client[0] if client else None}}
            if client and _is_trusted(client[0], self.trusted):
                headers = scope.get("headers") or []
                proto = _joined(headers, b"x-forwarded-proto")
                if proto:
                    # The last hop's scheme; a websocket keeps its own family (https -> wss).
                    value = proto.rsplit(",", 1)[-1].strip().lower()
                    if value in ("http", "https", "ws", "wss"):
                        secure = value in ("https", "wss")
                        base = "ws" if scope["type"] == "websocket" else "http"
                        scope = {**scope, "scheme": _HTTPS_FOR[base] if secure else base}
                forwarded_for = _joined(headers, b"x-forwarded-for")
                if forwarded_for:
                    behind = self.trusted if in_networks(client[0], LOOPBACK_NETWORKS) else LOOPBACK_NETWORKS
                    host = client_from_forwarded_for(forwarded_for, behind)
                    if host:
                        scope = {**scope, "client": (host, 0)}
        await self.app(scope, receive, send)
