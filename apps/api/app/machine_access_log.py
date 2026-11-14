"""One access line per request to the token-only surface, in nginx's stock `main` format, for the X-Agent-Token alarm.

WHY THIS EXISTS. The alarm on node-token failures (the ECS module's `detection.tf`) counts 401s on the machine
surface from nginx's access lines in the API's log group; the API itself runs Granian `--no-access-log`
(`apps/api/entrypoint.sh`). Where the load balancers dial the API directly (ECS, with `JAAH_INGRESS_INTERNAL_CIDRS`
set), no nginx sees those requests, so this layer writes the line nginx would have:

    $remote_addr - - [$time_local] "$request" $status $body_bytes_sent "-" "$http_user_agent" "$http_x_forwarded_for"

`$remote_addr` is the TCP peer (the load balancer, as nginx saw it), the forwarded chain is the last field.
THE QUERY STRING IS DROPPED from `$request`: a node-bootstrap `?token=` or a console `?ticket=` must not reach
the log group. A WebSocket is logged once its handshake is decided: `101` when accepted, `403` when closed first.

ONLY THE TOKEN-ONLY SURFACE: `ingress_surface.MACHINE_PATHS` and `/api/node-bootstrap`, judged on the normalised
path. Elsewhere the ALB's own access logs record every request. OFF where nginx still fronts the API (the
community edition, the local stack): inert unless the ingress guard is configured, so no line is doubled.

Directly inside the ingress guard (`app/main.py`): a public request the guard refuses is the load balancer's to log,
as the public listener's deny rule always was (nginx never saw those either).
"""
from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from app.ingress_surface import IngressConfig, is_machine_path, normalized_path, original_peer, under

if TYPE_CHECKING:  # pragma: no cover
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

BOOTSTRAP_PATH = "/api/node-bootstrap"
"""`app/node_bootstrap.py::BOOTSTRAP_PATH`: the token door that is reachable from the internet."""


def logged(path: str) -> bool:
    """The token-only surface this layer writes a line for (`path` as the router sees it)."""
    normal = normalized_path(path)
    return is_machine_path(normal) or under(normal.rstrip("/"), BOOTSTRAP_PATH)


def _header(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str:
    values = [value.decode("latin-1") for key, value in headers if key.lower() == name]
    return ", ".join(values) if values else "-"


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def access_line(scope: "Scope", status: int, sent: int, now: Optional[float] = None) -> str:
    """nginx's `main` line for one request (module docstring)."""
    headers = scope.get("headers") or []
    method = "GET" if scope["type"] == "websocket" else str(scope.get("method") or "GET")
    version = str(scope.get("http_version") or "1.1")
    stamp = time.strftime("%d/%b/%Y:%H:%M:%S +0000", time.gmtime(now))
    request = f"{method} {scope.get('path') or '/'} HTTP/{version}"
    return (
        f"{original_peer(scope) or '-'} - - [{stamp}] \"{_quoted(request)}\" {status} {sent} \"-\" "
        f"\"{_quoted(_header(headers, b'user-agent'))}\" \"{_quoted(_header(headers, b'x-forwarded-for'))}\""
    )


def _write(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class MachineAccessLogMiddleware:
    """Pure ASGI: writes `access_line` once per request to the token-only surface (module docstring)."""

    def __init__(self, app: "ASGIApp", *, enabled: Optional[bool] = None, write: Callable[[str], None] = _write) -> None:
        self.app = app
        self.enabled = IngressConfig.from_env() is not None if enabled is None else enabled
        self.write = write

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if not self.enabled or scope["type"] not in ("http", "websocket") or not logged(str(scope.get("path") or "")):
            await self.app(scope, receive, send)
            return
        state = {"status": 0, "sent": 0, "written": False}

        def emit() -> None:
            if not state["written"]:
                state["written"] = True
                self.write(access_line(scope, int(state["status"]) or 500, int(state["sent"])))

        async def observe(message: "Message") -> None:
            kind = message["type"]
            if kind == "http.response.start":
                state["status"] = message["status"]
            elif kind == "http.response.body":
                state["sent"] = int(state["sent"]) + len(message.get("body", b""))
            elif kind == "websocket.accept":
                state["status"] = 101
                emit()
            elif kind == "websocket.close" and not state["status"]:
                state["status"] = 403
                emit()
            elif kind == "websocket.http.response.start":
                state["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            emit()
