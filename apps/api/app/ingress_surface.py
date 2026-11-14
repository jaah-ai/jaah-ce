"""Which surface a request may reach, decided on its TCP peer: the API's own replacement for nginx's per-listener fences.

WHY THE PEER. Granian binds one port and the API runs one worker (`apps/api/entrypoint.sh`), so nginx's split by
listener port has no successor here, and a header marker fails open the moment a request lacks it. The peer address
is a property of the connection: the internal ALB, the API tasks and the console relay sit in the private subnets,
the public ALBs (console, live, hooks) in the public ones. A peer inside `JAAH_INGRESS_INTERNAL_CIDRS`, or loopback,
is INTERNAL and passes untouched (the tailnet's machine surface). ANY OTHER PEER IS PUBLIC, so a subnet nobody listed
fails closed. The peer is the one `ProxyHeadersMiddleware` saw before it rewrote the client
(`scope["state"]["ingress_peer"]`, `app/proxy_headers.py`); this layer sits directly inside it (`app/main.py`).

A PUBLIC REQUEST, in this order:
1. a raw path (before `?`) carrying `..`, `%2e`, `%2f` or `%5c` is 400 `malformed path`: nothing the SPA or a webhook
   sends has one, and each is how a prefix check is evaded;
2. a machine path (`MACHINE_PATHS`), judged on the server-decoded, `//`-collapsed, dot-resolved, lowercased path,
   is 403;
3. two `Host` headers are 400 `malformed host`; the Host then NARROWS it: docs (`DOCS_API_PREFIXES`), live (`LIVE_RULES`) or hooks (`HOOKS_ALLOWED_PATHS`). Host
   never widens anything — an unknown Host (an ALB health check by IP) gets the console rules, steps 1-2 alone.
The Host narrowing is defence in depth behind the hooks and live ALBs' own host-header rules: a public peer's Host is
the caller's to write. A websocket refusal is a close before accept (an HTTP 403 to the client, whatever the rule).
Any request carrying `X-Forwarded-For` to `/api/agent/cp-peer/…` is 403 whatever its peer: the peer surface is dialled
task to task on `ip:8000`, with no proxy in between.

INERT WHEN UNSET. With `JAAH_INGRESS_INTERNAL_CIDRS` unset (the community edition, the local stack) every request
passes: nginx's fences still apply there. The ECS task definition sets it (the Terraform ECS module's
`api_ingress_environment`), and on ECS it is REQUIRED: `assert_configured_on_ecs`, called by `create_app`, refuses
to start a process on ECS (`ECS_CONTAINER_METADATA_URI_V4` set) without it, because there the public ALBs dial
the API directly and an inert guard would open the machine surface to the internet.

Stdlib only at import, so the hooks-ingress Terraform selftest reads `HOOKS_ALLOWED_PATHS` from here without the
API's dependencies.
"""
from __future__ import annotations

import ipaddress
import json
import os
import posixpath
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping, Optional

if TYPE_CHECKING:  # pragma: no cover
    from starlette.types import ASGIApp, Receive, Scope, Send

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

LOOPBACK_NETWORKS: tuple[IPNetwork, ...] = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))

PEER_STATE_KEY = "ingress_peer"
"""Where `ProxyHeadersMiddleware` keeps the original TCP peer's host, under `scope["state"]`."""

MALFORMED_RAW_PATH = re.compile(r"(\.\.|%2e|%2f|%5c)", re.I)
"""nginx's `$refuse_public_raw_normalized_split`, on the raw path only: a query string may carry `%2F`."""

MACHINE_PATHS: frozenset[str] = frozenset({"/api/agent", "/api/node-bus", "/api/console-stream"})
"""The machine surface: each path and everything under it. `Access.INFRA`'s paths in `app/authz/catalog.py` but the
one deliberately internet-reachable `/api/node-bootstrap` (`app/node_bootstrap.py::BOOTSTRAP_PATH`); the API test
suite asserts the two agree."""

CP_PEER_PREFIX = "/api/agent/cp-peer/"

DOCS_API_PREFIXES: tuple[str, ...] = ("/api/public/jarvis/",)
"""All a docs host may reach under `/api`: the Jarvis embed's own API."""

LIVE_READ_PREFIX = "/api/public/live/"
LIVE_BEACON = "/api/public/live/beacon"
LIVE_RELAY = "/api/read-relay"
LIVE_READ_METHODS = frozenset({"GET", "HEAD"})
LIVE_RULES: tuple[tuple[str, str, frozenset[str]], ...] = (
    # (match, path, methods): `=` is the exact path, `^` the path and everything under it. First match wins.
    ("=", LIVE_BEACON, frozenset({"POST"})),
    ("^", LIVE_READ_PREFIX, LIVE_READ_METHODS),
    # A websocket upgrade is a GET; the route is a websocket alone, so nothing else is ever a call.
    ("=", LIVE_RELAY, frozenset({"GET"})),
)
"""EXACTLY what a live host reaches (the read-only Live Cast surface, its beacon and the read relay's socket)."""

HOOKS_ALLOWED_PATHS: frozenset[str] = frozenset({
    "/api/public/triggers/*", "/api/public/email/unsubscribe/*", "/api/public/telegram/updates",
    "/api/public/ci/hosted-app-deploy", "/api/public/ci/hosted-app-deploy/started",
    "/api/public/ci/hosted-app-deploy/finished", "/api/public/trackers/*",
})
"""EXACTLY what the hooks edge forwards (trigger webhooks, one-click unsubscribe, the Telegram webhook, an Easy Mode
app's deploy exchange and its reports, which a GitHub-hosted runner calls from outside dev's admin list, and issue
trackers' event webhooks): `/*` is
everything under the slashed prefix, else the exact path. A new path is a deliberate edit of this set, the terraform
local and nginx's fence together (the hooks-ingress Terraform selftest pins all three)."""

MACHINE_DETAIL = (
    "This is the public browser entrypoint. The agent/node API is not served here - dial the control plane's "
    "internal hostname over the tailnet."
)
MALFORMED_DETAIL = "malformed path"
MALFORMED_HOST_DETAIL = "malformed host"
CP_PEER_DETAIL = "peer surface is task-to-task only"
NOT_FOUND_DETAIL = "Not Found"
METHOD_DETAIL = "Method Not Allowed"


@dataclass(frozen=True)
class IngressConfig:
    internal: tuple[IPNetwork, ...]
    hooks_host: str = ""
    live_host: str = ""
    docs_host: str = ""

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> Optional["IngressConfig"]:
        """The guard's configuration, or None (inert) when `JAAH_INGRESS_INTERNAL_CIDRS` is unset or empty."""
        env: Mapping[str, str] = os.environ if environ is None else environ
        cidrs = parse_networks(env.get("JAAH_INGRESS_INTERNAL_CIDRS", ""))
        if not cidrs:
            return None
        return cls(
            internal=cidrs,
            hooks_host=env.get("JAAH_HOOKS_HOSTNAME", "").strip().lower(),
            live_host=env.get("JAAH_LIVE_HOSTNAME", "").strip().lower(),
            docs_host=env.get("JAAH_DOCS_HOSTNAME", "").strip().lower(),
        )


ECS_MARKER_ENV = "ECS_CONTAINER_METADATA_URI_V4"
"""Set by the ECS agent in every task, never by a test run, the local stack or the community edition."""


class IngressNotConfigured(RuntimeError):
    """The API was started on ECS without `JAAH_INGRESS_INTERNAL_CIDRS`."""


def assert_configured_on_ecs(environ: Optional[Mapping[str, str]] = None) -> None:
    """Refuse to start on ECS with the guard inert (module docstring, INERT WHEN UNSET)."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    if not env.get(ECS_MARKER_ENV, "").strip():
        return
    if IngressConfig.from_env(env) is None:
        raise IngressNotConfigured(
            "JAAH_INGRESS_INTERNAL_CIDRS is unset on ECS: the public load balancers reach this API directly, so it "
            "refuses to start without the ingress guard. Set it to the private subnet CIDRs (the ECS task definition "
            "does)."
        )
    # Without the proxy trust every public caller keys on its load balancer's address: one shared rate-limit
    # bucket, a sign-in lockout of everyone (`app/proxy_headers.py`).
    if not parse_networks(env.get("JAAH_TRUSTED_PROXY_CIDRS", "")):
        raise IngressNotConfigured(
            "JAAH_TRUSTED_PROXY_CIDRS is unset on ECS: every caller would share its load balancer's address. Set it "
            "to the VPC CIDR (the ECS task definition does)."
        )


def parse_networks(value: str) -> tuple[IPNetwork, ...]:
    """Comma-separated CIDRs (or bare addresses). A malformed entry raises: a typo must not silently open a subnet."""
    return tuple(ipaddress.ip_network(part.strip(), strict=False) for part in value.split(",") if part.strip())


def in_networks(host: Optional[str], networks: Iterable[IPNetwork]) -> bool:
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


def normalized_path(path: str) -> str:
    """`scope["path"]` — decoded ONCE by the server, exactly what the router matches — with `//` collapsed and dot
    segments resolved, lowercased: what a prefix rule compares. Never decoded a second time: `%252e` reaches the router
    as the literal text `%2e`, and a guard that read it as `.` would judge a path the router never serves."""
    decoded = path.replace("\\", "/")
    collapsed = re.sub(r"/{2,}", "/", decoded if decoded.startswith("/") else "/" + decoded)
    resolved = posixpath.normpath(collapsed)
    if resolved.startswith("//"):
        resolved = "/" + resolved.lstrip("/")
    if collapsed.endswith("/") and resolved != "/":
        resolved += "/"
    return resolved.lower()


def under(path: str, prefix: str) -> bool:
    """`path` is `prefix` or below it (`prefix` without a trailing slash)."""
    return path == prefix or path.startswith(prefix + "/")


def is_machine_path(path: str) -> bool:
    return any(under(path, machine) for machine in MACHINE_PATHS)


def hooks_allows(path: str) -> bool:
    for pattern in HOOKS_ALLOWED_PATHS:
        if pattern.endswith("/*"):
            if path.startswith(pattern[:-1]):
                return True
        elif path == pattern:
            return True
    return False


def live_verdict(path: str, method: str) -> Optional[tuple[int, str]]:
    """None when a live host may serve `method path`, else the refusal (404 off the surface, 405 a wrong method)."""
    if path != LIVE_BEACON and under(path.rstrip("/"), LIVE_BEACON):
        return 404, NOT_FOUND_DETAIL  # `beacon/`, `beacon/x`: never the read prefix's
    for match, rule_path, methods in LIVE_RULES:
        hit = path == rule_path if match == "=" else path.startswith(rule_path)
        if hit:
            return None if method in methods else (405, METHOD_DETAIL)
    return 404, NOT_FOUND_DETAIL


def live_methods(path: str) -> frozenset[str]:
    """The methods `LIVE_RULES` allows on `path` (the 405's `Allow`)."""
    for match, rule_path, methods in LIVE_RULES:
        if (path == rule_path if match == "=" else path.startswith(rule_path)):
            return methods
    return frozenset()


def request_host(headers: Iterable[tuple[bytes, bytes]]) -> Optional[str]:
    """The `Host` header lowercased, its port and a trailing dot stripped (`[::1]:80` → `[::1]`); "" when absent, None
    when there is more than one (which a guard cannot pick from)."""
    hosts = [value for name, value in headers if name.lower() == b"host"]
    if len(hosts) > 1:
        return None
    if not hosts:
        return ""
    host = hosts[0].decode("latin-1").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return (host.rsplit(":", 1)[0] if host.count(":") == 1 else host).rstrip(".")


def _raw_path(scope: "Scope") -> str:
    raw = scope.get("raw_path")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).split(b"?", 1)[0].decode("latin-1")
    return str(scope.get("path") or "")


def original_peer(scope: "Scope") -> Optional[str]:
    """The TCP peer before any proxy-header rewrite (`app/proxy_headers.py` keeps it), else the scope's client."""
    state = scope.get("state")
    if isinstance(state, dict) and PEER_STATE_KEY in state:
        return state[PEER_STATE_KEY]
    client = scope.get("client")
    return client[0] if client else None


def verdict(scope: "Scope", config: IngressConfig) -> Optional[tuple[int, str]]:
    """None to pass the request on, else (status, detail) — the module docstring's rules, in its order."""
    headers = scope.get("headers") or []
    path = normalized_path(str(scope.get("path") or ""))
    if under(path, CP_PEER_PREFIX.rstrip("/")) and any(name.lower() == b"x-forwarded-for" for name, _ in headers):
        return 403, CP_PEER_DETAIL
    peer = original_peer(scope)
    if peer and (in_networks(peer, LOOPBACK_NETWORKS) or in_networks(peer, config.internal)):
        return None
    if MALFORMED_RAW_PATH.search(_raw_path(scope)):
        return 400, MALFORMED_DETAIL
    if is_machine_path(path):
        return 403, MACHINE_DETAIL
    host = request_host(headers)
    if host is None:
        return 400, MALFORMED_HOST_DETAIL
    method = "GET" if scope["type"] == "websocket" else str(scope.get("method") or "GET").upper()
    if config.docs_host and host == config.docs_host:
        if under(path, "/api") and not path.startswith(DOCS_API_PREFIXES):
            return 404, NOT_FOUND_DETAIL
    elif config.live_host and host == config.live_host:
        return live_verdict(path, method)
    elif config.hooks_host and host == config.hooks_host:
        if not hooks_allows(path):
            return 404, NOT_FOUND_DETAIL
    return None


async def _refuse(scope: "Scope", send: "Send", status: int, detail: str) -> None:
    if scope["type"] == "websocket":
        # Before `accept`, a close is the server's refusal of the handshake (an HTTP 403 to the client).
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"detail": detail}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if status == 405:
        path = normalized_path(str(scope.get("path") or ""))
        headers.append((b"allow", ", ".join(sorted(live_methods(path))).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class IngressSurfaceMiddleware:
    """Pure ASGI: refuses a request its peer and Host may not reach before any other layer or handler runs."""

    def __init__(self, app: "ASGIApp", config: Optional[IngressConfig] = None, *, from_env: bool = True) -> None:
        self.app = app
        self.ingress_config = config if config is not None or not from_env else IngressConfig.from_env()

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if self.ingress_config is not None and scope["type"] in ("http", "websocket"):
            refusal = verdict(scope, self.ingress_config)
            if refusal is not None:
                await _refuse(scope, send, *refusal)
                return
        await self.app(scope, receive, send)
