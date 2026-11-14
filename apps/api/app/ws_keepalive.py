"""The control plane's own WebSocket keepalive: notice a half-open peer on a fixed schedule.

WHY THE APP DOES IT. Granian, the server (`apps/api/entrypoint.sh`), answers a peer's protocol pings but sends none
of its own, so a peer that vanished without a close (a node's network dropped, a laptop lid shut) would look
connected until the kernel gave up on the TCP connection, minutes to hours later. A node-bus socket that reads as
live is a node that reads as online, so the window has to stay the one both ends agree on.

THE SCHEDULE: `PING_INTERVAL_SECONDS` / `PING_TIMEOUT_SECONDS`, the node's own numbers (`apps/jaahd/node_bus.py`).
Any inbound frame proves the peer is alive (`Keepalive.seen`). After `PING_INTERVAL_SECONDS` of inbound silence the
watcher sends the socket's own ping frame, if it has one; if nothing at all arrives within `PING_TIMEOUT_SECONDS`
after that, the peer is dead and `run_with_keepalive` ends the session. A busy socket is never pinged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional, TypeVar

log = logging.getLogger("app.ws_keepalive")

PING_INTERVAL_SECONDS = 20.0
PING_TIMEOUT_SECONDS = 20.0

T = TypeVar("T")


class PeerSilent(Exception):
    """The peer sent nothing within the keepalive window: the socket is half-open."""


class Keepalive:
    """One socket's inbound-silence watch (module docstring). `ping` sends the socket's ping frame — a frame the
    peer answers — or is None where the peer's own periodic frames are the only proof of life."""

    def __init__(
        self,
        ping: Optional[Callable[[], Awaitable[None]]] = None,
        *,
        interval: Optional[float] = None,
        timeout: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ping = ping
        # Read at construction, not bound as defaults: the module's numbers are the one home of the schedule.
        self.interval = PING_INTERVAL_SECONDS if interval is None else interval
        self.timeout = PING_TIMEOUT_SECONDS if timeout is None else timeout
        self._clock = clock
        self._last = clock()

    def seen(self) -> None:
        """Record an inbound frame: the peer is alive now."""
        self._last = self._clock()

    async def watch(self) -> None:
        """Return when the peer has gone silent past the window (it never returns for a live peer)."""
        while True:
            idle = self._clock() - self._last
            if idle < self.interval:
                await asyncio.sleep(self.interval - idle)
                continue
            pinged_at = self._clock()
            if self._ping is not None:
                # BOUNDED: on a half-open peer whose send buffer is full, or behind a send that holds the socket's
                # lock, the send itself never returns — exactly the socket this exists to catch.
                try:
                    await asyncio.wait_for(self._ping(), self.timeout)
                except Exception as exc:  # noqa: BLE001 — a ping that cannot be sent in time is a dead socket too
                    log.debug("keepalive: ping send failed: %r", exc)
                    return
            await asyncio.sleep(max(0.0, self.timeout - (self._clock() - pinged_at)))
            if self._last < pinged_at:
                return


async def run_with_keepalive(reader: Awaitable[T], keepalive: Keepalive) -> T:
    """Run `reader` (the socket's receive loop, which calls `keepalive.seen()` per frame) until it returns, raises,
    or the peer goes silent — then raise `PeerSilent`, with the reader cancelled. The caller closes the socket."""
    read_task = asyncio.ensure_future(reader)
    watch_task = asyncio.ensure_future(keepalive.watch())
    try:
        await asyncio.wait({read_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # On cancellation of the caller too: neither child outlives the session.
        for task in (read_task, watch_task):
            if not task.done():
                task.cancel()
    if read_task.done() and not read_task.cancelled():
        return read_task.result()
    try:
        await read_task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — the reader died with the socket; silence is the verdict reported
        pass
    raise PeerSilent(f"no frame for {keepalive.interval + keepalive.timeout:.0f}s")
