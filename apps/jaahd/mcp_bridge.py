"""mcp_bridge.py — one linked remote MCP server, served to a runner's engine as a local stdio MCP server.

A project links an MCP connection (a credential holding the remote server's URL and its bearer token or OAuth
grant); the control plane names it in the dispatch decision (`mcp_connections`), and the adapter adds one stdio
entry per connection (`agents.base.remote_mcp_servers`) that starts THIS file:

    <python> -E -s mcp_bridge.py --credential <roster path> --url <https url> --transport streamable_http|sse

The token never sits in a config file, the environment, the worktree or the transcript: every request batch
fetches it through the task's credential socket (`credential.get`, task-scoped, so the grant rule applies; the
control plane refreshes an OAuth token on read) and holds it in memory for that request only. A 401 re-fetches
it once. Nothing of a header or a body is ever logged: diagnostics name the connection and an HTTP status.

STDOUT IS THE TRANSPORT — only JSON-RPC lines. Stdlib only (it runs on the node's interpreter beside the
control server). Redirects are refused, so the token is never replayed to another host, and the legacy SSE
transport's POST endpoint must be on the server's own origin.

Server-initiated messages arrive on a POST's event stream (streamable HTTP) or on the SSE stream (legacy). The
optional standalone GET stream of streamable HTTP is not opened: no request of a runner depends on it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

#: The remote transports a connection may name (`agents.base.McpConnection` checks against this).
TRANSPORTS = ("streamable_http", "sse")
#: The engine sees a connection as `mcp_<name>`.
SERVER_PREFIX = "mcp_"
#: The interpreter the packaged node ships (`mcp/jaah/trampoline.py`'s choice), else the daemon's own.
JAAH_PYTHON = "/usr/lib/jaah-python/bin/python3"
#: A MicroVM guest's interpreter: its distro's own (the guest has no `jaah-python`; `microvm_seed`'s D3), which runs
#: this file from the shared `/usr/lib/jaahd` toolchain root at the same path as on the host.
GUEST_PYTHON = "/usr/bin/python3"
#: A remote tool call may run long; a wedged server must still end eventually.
REQUEST_TIMEOUT_S = 600.0
#: How long the engine's EOF waits, in all, for requests still in flight.
SHUTDOWN_WAIT_S = 10.0
#: How long a legacy SSE server has to announce its POST endpoint.
ENDPOINT_WAIT_S = 30.0
_CREDENTIAL_TIMEOUT_S = 35.0
_PROTOCOL_HEADER = "MCP-Protocol-Version"
_SESSION_HEADER = "Mcp-Session-Id"
_INTERNAL_ERROR = -32603
#: One remote message (a JSON body, an SSE line, an SSE event) may be this large; a bigger one is refused, so a
#: hostile server cannot grow the bridge without bound.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
#: A server-chosen header value echoed back (`Mcp-Session-Id`, the protocol version): visible ASCII only.
_HEADER_VALUE = re.compile(r"^[\x21-\x7e]{1,256}$")
#: Fields a credential may carry its bearer in, after the live OAuth `access_token` the broker mints.
_TOKEN_FIELDS = ("bearer_token", "access_token", "api_key", "token")


def server_entry(name: str, credential_path: str, url: str, transport: str, *, guest: bool = False) -> dict:
    """The ACP stdio `McpServer` entry that starts this bridge for one connection: `mcp_<name>`, no env (the
    bridge reaches the credential socket the way the control server does). `guest`: the entry runs in a MicroVM
    guest, on `GUEST_PYTHON` — never a host interpreter the guest does not hold."""
    import os  # noqa: PLC0415

    if guest:
        python = GUEST_PYTHON
    else:
        python = JAAH_PYTHON if os.access(JAAH_PYTHON, os.X_OK) else sys.executable
    return {"name": f"{SERVER_PREFIX}{name}", "command": python,
            # `--flag=value`: a value starting with `-` can never be read as another option.
            "args": ["-E", "-s", str(Path(__file__).resolve()), f"--credential={credential_path}", f"--url={url}",
                     f"--transport={transport}", f"--name={name}"],
            "env": []}


def check_url(url: str) -> str:
    """`url` when it is one a connection may name: https with a host, and no userinfo, query or fragment — it
    rides the bridge's argv and the launch file, so a secret must never be part of it. Else ValueError."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("an MCP connection's url must be https with a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("an MCP connection's url must carry no userinfo, query or fragment; a secret belongs in "
                         "the credential")
    return url


def bearer_token(credential: object) -> str:
    """The bearer a `credential.get` answer carries: the live OAuth `access_token`, else the first secret field
    of `_TOKEN_FIELDS` in its config. `""` when there is none (a server that needs no auth)."""
    if not isinstance(credential, dict):
        return ""
    live = credential.get("access_token")
    if isinstance(live, str) and live:
        return live
    config = credential.get("config")
    if isinstance(config, dict):
        for field in _TOKEN_FIELDS:
            value = config.get(field)
            if isinstance(value, str) and value:
                return value
    return ""


def fetch_token(credential_path: str) -> str:
    """The connection's bearer, fetched now through the task's credential socket. Raises `BridgeError`."""
    import agent_socket  # noqa: PLC0415 — the daemon module beside this file

    try:
        answer = agent_socket.call("credential.get", {"name": credential_path}, timeout=_CREDENTIAL_TIMEOUT_S)
    except agent_socket.AgentSocketRefused as exc:
        raise BridgeError(f"the credential {credential_path!r} was refused: {exc.reason}") from None
    except agent_socket.AgentSocketUnavailable:
        raise BridgeError("the node daemon's credential socket did not answer") from None
    return bearer_token(answer)


class BridgeError(RuntimeError):
    """A request could not be forwarded. The message names no header, body or token."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is refused, never followed: urllib would replay the `Authorization` header to its target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


def default_opener() -> Callable[..., Any]:
    return urllib.request.build_opener(_NoRedirect()).open


def _bounded_lines(response) -> Iterator[bytes]:
    """`response`'s lines, each at most `MAX_MESSAGE_BYTES`; a longer one raises `BridgeError`."""
    while True:
        line = response.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_MESSAGE_BYTES:
            raise BridgeError("remote sent a line over the size limit")
        yield line


def sse_events(lines: Iterator[bytes]) -> Iterator[tuple[str, str]]:
    """`(event, data)` per Server-Sent Event in a byte-line stream (the WHATWG field rules this needs). An event
    over `MAX_MESSAGE_BYTES` raises `BridgeError`."""
    event: str = ""
    data: list[str] = []
    size = 0
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                yield event or "message", "\n".join(data)
            event, data, size = "", [], 0
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            size += len(value)
            if size > MAX_MESSAGE_BYTES:
                raise BridgeError("remote sent an event over the size limit")
            data.append(value)
    if data:
        yield event or "message", "\n".join(data)


def _items(message: object) -> list:
    return message if isinstance(message, list) else [message]


def _request_ids(message: object) -> list:
    """The ids of the requests in one JSON-RPC message or batch (notifications and responses carry none)."""
    return [m["id"] for m in _items(message) if isinstance(m, dict) and "method" in m and "id" in m]


def _response_ids(message: object) -> list:
    return [m["id"] for m in _items(message) if isinstance(m, dict) and "method" not in m and "id" in m]


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def _sends_in_order(message: object) -> bool:
    """A notification, a response, or `initialize`: forwarded before the next stdin line is read, so the remote
    sees `initialize` → `notifications/initialized` → the first request in the order the engine sent them."""
    items = [m for m in _items(message) if isinstance(m, dict)]
    return not _request_ids(message) or any(m.get("method") == "initialize" for m in items)


class Bridge:
    """The stdio side and the remote side of one connection. `opener` and `token` are injectable for tests."""

    def __init__(self, *, name: str, credential_path: str, url: str, transport: str,
                 opener: Callable[..., Any] | None = None, token: Callable[[str], str] | None = None,
                 out: Any = None, err: Any = None) -> None:
        if transport not in TRANSPORTS:
            raise ValueError(f"unknown transport {transport!r}")
        self.name, self.credential_path, self.url, self.transport = name, credential_path, check_url(url), transport
        self._open = opener or default_opener()
        self._token = token or fetch_token
        self._out = out or sys.stdout
        self._err = err or sys.stderr
        self._write_lock = threading.Lock()
        self._session_id = ""
        self._protocol = ""
        self._endpoint = ""
        self._endpoint_ready = threading.Event()
        self._sse_failed = ""
        # Legacy SSE: the request ids POSTed whose answer has not come back on the stream yet.
        self._pending: set = set()
        self._pending_lock = threading.Lock()
        self._warned_no_bearer = False

    # ── output ──

    def log(self, message: str) -> None:
        """A diagnostic to stderr — never a header, a body or a token."""
        print(f"mcp_bridge {self.name}: {message}", file=self._err, flush=True)

    def emit(self, message: object) -> None:
        with self._write_lock:
            self._out.write(json.dumps(message, separators=(",", ":")) + "\n")
            self._out.flush()

    def fail_ids(self, ids, reason: str) -> None:
        """Answer each request id with a JSON-RPC error, so the engine never waits on a lost call."""
        for request_id in ids:
            self.emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": _INTERNAL_ERROR,
                                                                     "message": f"mcp_{self.name}: {reason}"}})

    def fail(self, message: object, reason: str) -> None:
        self.fail_ids(_request_ids(message), reason)

    # ── remote side ──

    def _headers(self, token: str, *, accept: str, post: bool) -> dict:
        headers = {"Accept": accept}
        if post:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif not self._warned_no_bearer:
            self._warned_no_bearer = True
            self.log("the credential carries no bearer; sending the request without one")
        if self._session_id:
            headers[_SESSION_HEADER] = self._session_id
        if self._protocol:
            headers[_PROTOCOL_HEADER] = self._protocol
        return headers

    def _send(self, url: str, body: bytes | None, *, accept: str, method: str = "",
              timeout: float | None = REQUEST_TIMEOUT_S):
        """Open one request with a freshly fetched token; on a 401 fetch it again and retry once."""
        verb = method or ("POST" if body is not None else "GET")
        for attempt in (1, 2):
            token = self._token(self.credential_path)
            request = urllib.request.Request(url, data=body, method=verb,
                                             headers=self._headers(token, accept=accept, post=body is not None))
            try:
                return self._open(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code == 401 and attempt == 1:
                    self.log("remote answered 401; fetching the token again")
                    continue
                if exc.code == 404 and self._session_id:
                    # The server ended the session (MCP 2025-06-18 §Session Management): the engine must start a
                    # new one, and nothing of the old one is sent again.
                    self._session_id, self._protocol = "", ""
                    raise BridgeError("the remote session expired; reconnect the server") from None
                raise BridgeError(f"remote answered HTTP {exc.code}") from None
            except (urllib.error.URLError, OSError) as exc:
                raise BridgeError(f"remote unreachable ({type(exc).__name__})") from None
        raise BridgeError("remote answered 401 twice")  # unreachable: the second 401 raises above

    def _note(self, message: object) -> None:
        """Remember the negotiated protocol version off an `initialize` result (sent on every later request)."""
        for item in _items(message):
            if isinstance(item, dict) and isinstance(item.get("result"), dict):
                version = item["result"].get("protocolVersion")
                if (isinstance(version, str) and _HEADER_VALUE.match(version)
                        and "serverInfo" in item["result"]):
                    self._protocol = version

    def _relay(self, message: object) -> list:
        """Emit one remote message; returns the response ids it carried."""
        self._note(message)
        self.emit(message)
        return _response_ids(message)

    def _relay_stream(self, response) -> set:
        answered: set = set()
        for event, data in sse_events(_bounded_lines(response)):
            if event != "message":
                continue
            try:
                message = json.loads(data)
            except ValueError:
                self.log("remote sent an event that is not JSON; dropped")
                continue
            answered.update(self._relay(message))
        return answered

    def forward_streamable(self, message: object) -> None:
        wanted = _request_ids(message)
        body = json.dumps(message, separators=(",", ":")).encode()
        answered: set = set()
        try:
            with self._send(self.url, body, accept="application/json, text/event-stream") as response:
                session = response.headers.get(_SESSION_HEADER)
                if session and _HEADER_VALUE.match(session):
                    self._session_id = session
                kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not wanted:
                    return
                if kind == "text/event-stream":
                    answered = self._relay_stream(response)
                else:
                    raw = response.read(MAX_MESSAGE_BYTES + 1)
                    if len(raw) > MAX_MESSAGE_BYTES:
                        raise BridgeError("remote answer over the size limit")
                    if raw.strip():
                        try:
                            answered = set(self._relay(json.loads(raw)))
                        except ValueError:
                            raise BridgeError("remote answered with a body that is not JSON") from None
        except BridgeError as exc:
            self.log(str(exc))
            self.fail_ids([i for i in wanted if i not in answered], str(exc))
            return
        missing = [i for i in wanted if i not in answered]
        if missing:
            self.log("remote closed the answer without a response to every request")
            self.fail_ids(missing, "remote closed the answer without responding")

    def _sse_reader(self) -> None:
        """The legacy transport's GET stream: `endpoint` once, then every server message. When it ends, every
        unanswered request is failed and every later one fails fast."""
        try:
            # No read timeout: the stream is idle between messages by design.
            with self._send(self.url, None, accept="text/event-stream", timeout=None) as response:
                for event, data in sse_events(_bounded_lines(response)):
                    if event == "endpoint":
                        endpoint = urllib.parse.urljoin(self.url, data.strip())
                        if not _same_origin(endpoint, self.url):
                            raise BridgeError("remote announced an endpoint on another origin; refused")
                        self._endpoint = endpoint
                        self._endpoint_ready.set()
                    elif event == "message":
                        try:
                            message = json.loads(data)
                        except ValueError:
                            self.log("remote sent an event that is not JSON; dropped")
                            continue
                        with self._pending_lock:
                            self._pending.difference_update(_response_ids(message))
                        self.emit(message)
            reason = "the remote event stream ended"
        except BridgeError as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 — a dead stream must never leave a request waiting
            reason = f"the remote event stream failed ({type(exc).__name__})"
        self._sse_failed, self._endpoint = reason, ""
        self.log(reason)
        self._endpoint_ready.set()
        with self._pending_lock:
            lost, self._pending = list(self._pending), set()
        self.fail_ids(lost, reason)

    def start(self) -> None:
        if self.transport == "sse":
            threading.Thread(target=self._sse_reader, name=f"mcp_{self.name}-sse", daemon=True).start()

    def forward_sse(self, message: object) -> None:
        wanted = _request_ids(message)
        self._endpoint_ready.wait(ENDPOINT_WAIT_S)
        endpoint = self._endpoint
        if not endpoint:
            self.fail(message, self._sse_failed or "remote announced no endpoint")
            return
        with self._pending_lock:
            self._pending.update(wanted)
        body = json.dumps(message, separators=(",", ":")).encode()
        try:
            with self._send(endpoint, body, accept="application/json"):
                pass  # The answer arrives on the event stream.
        except BridgeError as exc:
            self.log(str(exc))
            with self._pending_lock:
                self._pending.difference_update(wanted)
            self.fail_ids(wanted, str(exc))

    def forward(self, message: object) -> None:
        (self.forward_sse if self.transport == "sse" else self.forward_streamable)(message)

    def close(self) -> None:
        """End a streamable-HTTP session (best effort; a server may refuse with 405)."""
        if self.transport != "streamable_http" or not self._session_id:
            return
        try:
            with self._send(self.url, None, accept="application/json", method="DELETE", timeout=10.0):
                pass
        except BridgeError:
            pass

    # ── stdio side ──

    def serve(self, stdin: Any = None) -> int:
        """Relay stdin's JSON-RPC lines until EOF. A request runs in its own thread, so a long call never blocks a
        ping; a notification, a response and `initialize` are sent in order first (`_sends_in_order`)."""
        source = stdin or sys.stdin
        self.start()
        workers: list[threading.Thread] = []
        while True:
            line = source.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "malformed JSON"}})
                continue
            if _sends_in_order(message):
                self._guarded(message)
                continue
            worker = threading.Thread(target=self._guarded, args=(message,), daemon=True)
            worker.start()
            workers = [w for w in workers if w.is_alive()] + [worker]
        deadline = time.monotonic() + SHUTDOWN_WAIT_S
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        self.close()
        return 0

    def _guarded(self, message: object) -> None:
        try:
            self.forward(message)
        except Exception as exc:  # noqa: BLE001 — one bad request must not end the session
            self.log(f"forwarding raised {type(exc).__name__}")
            self.fail(message, f"bridge fault ({type(exc).__name__})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_bridge", description=__doc__.splitlines()[0])
    parser.add_argument("--credential", required=True, help="the credential's roster path")
    parser.add_argument("--url", required=True, help="the remote MCP server (https)")
    parser.add_argument("--transport", required=True, choices=TRANSPORTS)
    parser.add_argument("--name", default="", help="the connection's name, for diagnostics")
    args = parser.parse_args(argv)
    name = args.name or args.credential.rsplit("/", 1)[-1]
    try:
        bridge = Bridge(name=name, credential_path=args.credential, url=args.url, transport=args.transport)
    except ValueError as exc:
        print(f"mcp_bridge: {exc}", file=sys.stderr)
        return 2
    return bridge.serve()


if __name__ == "__main__":
    raise SystemExit(main())
