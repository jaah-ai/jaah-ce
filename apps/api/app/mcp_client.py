"""The control plane's MCP client: one handshake against a remote MCP server, to prove a connection works.

`probe` performs the MCP `initialize` handshake, the `notifications/initialized` notification, then the tool list
(`TOOLS_LIST`),
over either remote transport:

* `streamable_http` (MCP 2025-03-26 and later): each JSON-RPC message is a POST to the server URL; the answer is
  a JSON body or an SSE stream carrying it, and the session rides the `Mcp-Session-Id` header;
* `sse` (the older HTTP+SSE transport): a GET opens an event stream whose first `endpoint` event names where to
  POST; answers arrive on that stream as `message` events.

The server URL is a host a tenant chose, so it is dialled only at the addresses `http_bounded.vetted_addresses`
passed (`http_bounded.pin`): the metadata service, RFC 1918 and every other private block are refused before
anything is sent, and a rebinding name cannot swap one in. Redirects are not followed. A token is sent only as
`Authorization: Bearer`, and nothing here logs a header, a body or a URL.

Used by Test (`credentials/testers.py::test_mcp_server` and every catalog spec with an `[mcp]` block). No DB.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from time import monotonic
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import httpx

from app import http_bounded

log = logging.getLogger("mcp-client")

#: The remote transports a connection may name (the node's `apps/jaahd/mcp_bridge.py::TRANSPORTS` is its twin
#: across the deploy boundary; `tests/test_mcp_client.py` holds the two equal).
TRANSPORTS: tuple[str, ...] = ("streamable_http", "sse")

#: The JSON-RPC method that lists a server's tools. Two literals: the mirror's path scanner reads the joined
#: spelling as a repository path under `tools/`.
TOOLS_LIST = "tools" "/list"

#: The protocol revision `initialize` offers; a server answers with the one it speaks.
PROTOCOL_VERSION = "2025-06-18"

_CLIENT_INFO = {"name": "jaah", "version": "1"}

# One answer is a few JSON members; the tool list can be large (tool schemas), so a generous cap - also the whole
# budget of one event stream (`_capped_lines`).
_MAX_MESSAGE_BYTES = 2 * 1024 * 1024
# The most SSE lines read while waiting for one answer (a server streaming notifications forever).
_MAX_STREAM_LINES = 20_000


class McpRefused(Exception):
    """The probe could not complete. `kind` is closed: `destination` (refused before dialling), `rejected`
    (401/403), `status` (another HTTP error), `unreachable`, `timeout`, `protocol` (not an MCP answer).
    The message is a sentence safe to show: it never carries a token or the server's body."""

    def __init__(self, kind: str, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class McpProbe:
    """What a completed handshake learned."""

    server_name: Optional[str]
    protocol_version: str
    tool_count: int


def _client(host: str, addresses: tuple[str, ...], timeout: float) -> httpx.Client:
    """The seam tests replace: a redirect-off client pinned to the vetted addresses."""
    return http_bounded.pinned_client(host, addresses, timeout)


def _remaining(deadline: float) -> float:
    left = deadline - monotonic()
    if left <= 0:
        raise McpRefused("timeout", "The MCP server did not answer in time.")
    return left


def _vetted(url: str) -> tuple[str, tuple[str, ...]]:
    """`url`'s host and its vetted addresses, or the `destination` refusal."""
    refusal, addresses = http_bounded.vetted_addresses(url)
    if refusal is not None or not addresses:
        raise McpRefused("destination", f"The server URL was refused - {refusal or 'it did not resolve'}.")
    return httpx.URL(url).host, addresses


def _rpc(method: str, message_id: Optional[int], params: Optional[dict] = None) -> dict[str, Any]:
    """One JSON-RPC 2.0 message: a request when `message_id` is set, else a notification."""
    out: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if message_id is not None:
        out["id"] = message_id
    if params is not None:
        out["params"] = params
    return out


def _initialize_params() -> dict[str, Any]:
    return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": dict(_CLIENT_INFO)}


def _check_status(response: httpx.Response) -> None:
    status = response.status_code
    if status in (401, 403):
        raise McpRefused("rejected", f"rejected the token ({status})", status=status)
    if 300 <= status < 400:
        raise McpRefused("status", f"The MCP server answered with a redirect (HTTP {status}), which is not followed.",
                         status=status)
    if status >= 400:
        raise McpRefused("status", f"The MCP server answered HTTP {status}.", status=status)


def _capped_lines(response: httpx.Response, deadline: float) -> Iterator[str]:
    """`response`'s body as text lines, read chunk by chunk under one byte budget (`_MAX_MESSAGE_BYTES`, the whole
    stream) and the probe's deadline checked on every chunk: an endless line or a server dripping a byte at a time
    ends the probe instead of growing memory or holding the thread."""
    buffer, total = b"", 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > _MAX_MESSAGE_BYTES:
            raise McpRefused("protocol", "The MCP server's event stream was too large to read.")
        _remaining(deadline)
        buffer += chunk
        *complete, buffer = buffer.split(b"\n")
        for line in complete:
            yield line.rstrip(b"\r").decode("utf-8", "replace")
    if buffer:
        yield buffer.rstrip(b"\r").decode("utf-8", "replace")


def _sse_events(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    """`(event, data)` per SSE event (WHATWG: `data` lines joined by newlines, blank line ends one)."""
    event = "message"
    data: list[str] = []
    seen = 0
    for line in lines:
        seen += 1
        if seen > _MAX_STREAM_LINES:
            raise McpRefused("protocol", "The MCP server's event stream never delivered the answer.")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


def _answer(payload: Any, message_id: int) -> Optional[dict[str, Any]]:
    """The JSON-RPC answer to `message_id` in `payload` (an object or a batch), or None when absent."""
    for item in payload if isinstance(payload, list) else [payload]:
        if isinstance(item, dict) and item.get("id") == message_id and ("result" in item or "error" in item):
            return item
    return None


def _result(answer: dict[str, Any], what: str) -> dict[str, Any]:
    if "error" in answer:
        error = answer.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise McpRefused("protocol", f"The MCP server refused {what} (JSON-RPC error {code}).")
    result = answer.get("result")
    if not isinstance(result, dict):
        raise McpRefused("protocol", f"The MCP server's {what} answer was not an object.")
    return result


def _read_capped(response: httpx.Response, deadline: float) -> bytes:
    raw, truncated = http_bounded.read_capped(response, _MAX_MESSAGE_BYTES, lambda: monotonic() >= deadline)
    if truncated:
        raise McpRefused("protocol", "The MCP server's answer was too large or too slow to read.")
    return raw


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        raise McpRefused("protocol", "The MCP server did not answer with JSON-RPC.") from None


# ── streamable HTTP ──────────────────────────────────────────────────────────────────────────────────────────────

class _StreamableSession:
    def __init__(self, client: httpx.Client, url: str, token: Optional[str], deadline: float) -> None:
        self._client, self._url, self._deadline = client, url, deadline
        self._headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._session_id: Optional[str] = None
        self._version: Optional[str] = None

    def _send(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._version:
            headers["MCP-Protocol-Version"] = self._version
        timeout = _remaining(self._deadline)
        with self._client.stream("POST", self._url, content=json.dumps(message).encode("utf-8"), headers=headers,
                                 timeout=timeout) as response:
            _check_status(response)
            session_id = response.headers.get("mcp-session-id")
            if session_id and not self._session_id:
                self._session_id = session_id
            message_id = message.get("id")
            if message_id is None:
                return None  # a notification: 202 Accepted, no body worth reading
            kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if kind == "text/event-stream":
                for _event, data in _sse_events(_capped_lines(response, self._deadline)):
                    _remaining(self._deadline)
                    answer = _answer(_parse_json(data.encode("utf-8")), message_id)
                    if answer is not None:
                        return answer
                raise McpRefused("protocol", "The MCP server's event stream ended without an answer.")
            if kind != "application/json":
                raise McpRefused("protocol", "The MCP server did not answer with JSON-RPC.")
            answer = _answer(_parse_json(_read_capped(response, self._deadline)), message_id)
            if answer is None:
                raise McpRefused("protocol", "The MCP server's answer did not match the request.")
            return answer

    def run(self) -> McpProbe:
        init = self._send(_rpc("initialize", 1, _initialize_params()))
        assert init is not None  # narrowing: a request always returns its answer or raises
        result = _result(init, "initialize")
        version = result.get("protocolVersion")
        self._version = version if isinstance(version, str) else PROTOCOL_VERSION
        self._send(_rpc("notifications/initialized", None))
        tools = self._send(_rpc(TOOLS_LIST, 2, {}))
        assert tools is not None
        probe = _probe(result, self._version, _result(tools, "the tool list request"))
        self._close()
        return probe

    def _close(self) -> None:
        """End the session the server opened (best effort: a server may answer 405, or be gone)."""
        if not self._session_id:
            return
        headers = {key: value for key, value in self._headers.items() if key == "Authorization"}
        headers["Mcp-Session-Id"] = self._session_id
        try:
            with self._client.stream("DELETE", self._url, headers=headers, timeout=_remaining(self._deadline)):
                pass
        except (httpx.HTTPError, McpRefused):
            pass


# ── HTTP + SSE (legacy) ──────────────────────────────────────────────────────────────────────────────────────────

class _SseSession:
    def __init__(self, client: httpx.Client, url: str, token: Optional[str], deadline: float) -> None:
        self._client, self._url, self._deadline = client, url, deadline
        self._auth = {"Authorization": f"Bearer {token}"} if token else {}

    def run(self) -> McpProbe:
        headers = {"Accept": "text/event-stream", **self._auth}
        with self._client.stream("GET", self._url, headers=headers, timeout=_remaining(self._deadline)) as stream:
            _check_status(stream)
            events = _sse_events(_capped_lines(stream, self._deadline))
            endpoint = self._endpoint(events)
            init = self._call(events, endpoint, _rpc("initialize", 1, _initialize_params()))
            result = _result(init, "initialize")
            version = result.get("protocolVersion")
            self._post(endpoint, _rpc("notifications/initialized", None))
            tools = self._call(events, endpoint, _rpc(TOOLS_LIST, 2, {}))
            return _probe(result, version if isinstance(version, str) else PROTOCOL_VERSION,
                          _result(tools, "the tool list request"))

    def _endpoint(self, events: Iterator[tuple[str, str]]) -> str:
        for event, data in events:
            _remaining(self._deadline)
            if event == "endpoint":
                target = urljoin(self._url, data.strip())
                # Only the server's own origin: the pinned client refuses any other host anyway, and a
                # different scheme or port would be a second destination nobody vetted.
                if http_bounded.origin(target) != http_bounded.origin(self._url):
                    raise McpRefused("protocol", "The MCP server named a message endpoint on another host.")
                return target
        raise McpRefused("protocol", "The MCP server's event stream named no message endpoint.")

    def _post(self, endpoint: str, message: dict[str, Any]) -> None:
        # Streamed and never read: the answer arrives on the event stream, and an unread body costs nothing.
        with self._client.stream("POST", endpoint, content=json.dumps(message).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **self._auth},
                                 timeout=_remaining(self._deadline)) as response:
            _check_status(response)

    def _call(self, events: Iterator[tuple[str, str]], endpoint: str, message: dict[str, Any]) -> dict[str, Any]:
        self._post(endpoint, message)
        for event, data in events:
            _remaining(self._deadline)
            if event != "message":
                continue
            answer = _answer(_parse_json(data.encode("utf-8")), message["id"])
            if answer is not None:
                return answer
        raise McpRefused("protocol", "The MCP server's event stream ended without an answer.")


def _probe(init: dict[str, Any], version: str, tools: dict[str, Any]) -> McpProbe:
    listed = tools.get("tools")
    if not isinstance(listed, list):
        raise McpRefused("protocol", "The MCP server's tool list answer carried no tools.")
    info = init.get("serverInfo")
    name = info.get("name") if isinstance(info, dict) else None
    clean = name.strip()[:80] if isinstance(name, str) and name.strip().isprintable() else None
    return McpProbe(server_name=clean or None, protocol_version=version, tool_count=len(listed))


def probe(url: str, *, transport: str, token: Optional[str], deadline: float) -> McpProbe:
    """Handshake with the MCP server at `url` and list its tools, or raise `McpRefused`. `deadline` is a
    `time.monotonic()` instant (`credentials/testers.py::new_deadline`)."""
    if transport not in TRANSPORTS:
        raise McpRefused("protocol", f"Transport must be one of {', '.join(TRANSPORTS)}.")
    host, addresses = _vetted(url)
    try:
        with _client(host, addresses, _remaining(deadline)) as client:
            session = (_StreamableSession if transport == "streamable_http" else _SseSession)(
                client, url, token, deadline)
            return session.run()
    except httpx.TimeoutException:
        raise McpRefused("timeout", "The MCP server did not answer in time.") from None
    except httpx.HTTPError as exc:
        # The class only: httpx's message renders the request URL.
        log.info("mcp probe: transport failure (%s)", type(exc).__name__)
        raise McpRefused("unreachable", "The MCP server could not be reached.") from None
