"""Newline-delimited JSON-RPC 2.0 over a pair of asyncio streams — the ACP transport.

ACP is bidirectional: the agent answers our requests AND sends its own (`session/request_permission`,
`elicitation/create`). An incoming request may block for minutes (an elicitation waits on a human), so each
one is handled in its own task; the read loop never awaits a request handler, or every response behind it —
including the one that request is itself waiting for — would stall.

Notifications are awaited INLINE, in arrival order: `session/update` chunks must be normalised in the order
they were sent, and a handler that wants concurrency can spawn its own task.

Writes are serialised under one lock (a frame is one line; two interleaved writes would corrupt both).
EOF fails every pending request with ConnectionClosed — the engine is gone, nothing will answer.
At most MAX_CONCURRENT_REQUESTS incoming request handlers run at once; one more is answered BUSY at once
(an agent cannot pile up unbounded tasks). A malformed frame — a bad error `code`, a shape the dispatch does
not expect — is logged and skipped; only EOF or an unreadable stream ends the read loop.
`on_frame(direction, obj)` sees every decoded frame for the recorder/replay harness.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("jaahd.acp.jsonrpc")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
BUSY = -32000
MAX_CONCURRENT_REQUESTS = 32


class JsonRpcError(Exception):
    """An error response, received from the peer or raised by a handler to answer one."""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data

    def to_wire(self) -> dict:
        err: dict = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


class ConnectionClosed(Exception):
    """The stream ended (or was never usable): no response will ever arrive."""


RequestHandler = Callable[[str, dict, Any], Awaitable[object]]
NotificationHandler = Callable[[str, dict], "Awaitable[None] | None"]
FrameHook = Callable[[str, dict], None]


class Connection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: Any,
        *,
        on_request: RequestHandler | None = None,
        on_notification: NotificationHandler | None = None,
        on_frame: FrameHook | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_request = on_request
        self._on_notification = on_notification
        self._on_frame = on_frame
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: set[asyncio.Task] = set()
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def request(self, method: str, params: dict) -> object:
        if self._closed:
            raise ConnectionClosed(f"connection closed before {method}")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await fut
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict) -> None:
        if self._closed:
            raise ConnectionClosed(f"connection closed before {method}")
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def run(self) -> None:
        try:
            while True:
                try:
                    line = await self._reader.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:  # one frame over the reader limit
                    log.warning("acp frame over the reader limit; closing: %s", exc)
                    break
                except (ConnectionError, OSError) as exc:
                    log.info("acp stream read failed: %s", exc)
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    log.warning("acp: unparseable frame (%d bytes)", len(line))
                    await self._reply_error(None, JsonRpcError(PARSE_ERROR, "Parse error"))
                    continue
                if not isinstance(obj, dict):
                    await self._reply_error(None, JsonRpcError(INVALID_REQUEST, "Invalid Request"))
                    continue
                self._frame("in", obj)
                try:
                    await self._dispatch(obj)
                except Exception:  # a malformed frame is skipped, never the end of the session
                    log.exception("acp: frame skipped")
        finally:
            self._close()

    async def _dispatch(self, obj: dict) -> None:
        method = obj.get("method")
        params = obj.get("params")
        if not isinstance(params, dict):
            params = {}
        if isinstance(method, str):
            if "id" in obj:
                if len(self._handlers) >= MAX_CONCURRENT_REQUESTS:
                    log.warning("acp: %s refused busy (%d requests in flight)", method, len(self._handlers))
                    await self._reply_error(obj["id"], JsonRpcError(BUSY, "busy"))
                    return
                task = asyncio.create_task(self._handle_request(method, params, obj["id"]))
                self._handlers.add(task)
                task.add_done_callback(self._handlers.discard)
                return
            if self._on_notification is None:
                return
            try:
                res = self._on_notification(method, params)
                if inspect.isawaitable(res):
                    await res
            except Exception:  # a broken handler must not end the session
                log.exception("acp notification handler failed: %s", method)
            return
        rid = obj.get("id")
        fut = self._pending.get(rid) if isinstance(rid, int) else None
        if fut is None:
            log.info("acp: response for unknown id %r ignored", rid)
            return
        if fut.done():
            return
        err = obj.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if not isinstance(code, int) or isinstance(code, bool):
                log.info("acp: error response with non-integer code %r", type(code).__name__)
                code = INTERNAL_ERROR
            fut.set_exception(JsonRpcError(code, str(err.get("message", "")), err.get("data")))
        else:
            fut.set_result(obj.get("result"))

    async def _handle_request(self, method: str, params: dict, rid: Any) -> None:
        try:
            if self._on_request is None:
                raise JsonRpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
            result = await self._on_request(method, params, rid)
        except JsonRpcError as exc:
            await self._reply_error(rid, exc)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("acp request handler failed: %s", method)
            await self._reply_error(rid, JsonRpcError(INTERNAL_ERROR, f"Internal error: {type(exc).__name__}"))
            return
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "result": result})
        except ConnectionClosed:
            log.info("acp: response to %s dropped, connection closed", method)

    async def _reply_error(self, rid: Any, exc: JsonRpcError) -> None:
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "error": exc.to_wire()})
        except ConnectionClosed:
            log.info("acp: error response dropped, connection closed")

    async def _send(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._lock:
            if self._closed:
                raise ConnectionClosed("connection closed")
            self._frame("out", obj)
            try:
                self._writer.write(data)
                await self._writer.drain()
            except (ConnectionError, OSError, RuntimeError) as exc:
                self._close()
                raise ConnectionClosed(str(exc)) from exc

    def _frame(self, direction: str, obj: dict) -> None:
        if self._on_frame is None:
            return
        try:
            self._on_frame(direction, obj)
        except Exception:
            log.exception("acp on_frame hook failed")

    def _close(self) -> None:
        self._closed = True
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionClosed("connection closed"))
        self._pending.clear()
        for task in list(self._handlers):
            task.cancel()
