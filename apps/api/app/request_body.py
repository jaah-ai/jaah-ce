"""The bounded inbound body read: a request body buffered only up to a cap, refused by the declared length
first and then by what actually arrives, so a chunked or lying `Content-Length` never makes the single API
process buffer more than the cap. Every route that BUFFERS a body under a limit reads through here (a body
streamed to disk at its signed length, `routes/ce_storage.py`, is not one); the outbound twin is
`app/http_bounded.py::read_capped`. The refusal's words stay the route's own.
"""
from __future__ import annotations

from fastapi import Request


async def read_capped(request: Request, limit: int) -> bytes | None:
    """The raw body, or None once it passes `limit` bytes: not a byte is read past a declared length over the
    cap, and a body that arrives longer is abandoned one chunk past it, never parsed as a prefix."""
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            return None
        body += chunk
    return bytes(body)
