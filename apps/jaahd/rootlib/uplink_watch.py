"""Re-pin every egress fence the moment the node's uplink changes — sensed from the kernel, never polled.

`hostnet.node_uplink()` names the ONE wire egress may leave by, and `vnodehelper/nft.py` pins it by
interface name, read when a table is installed. Without this module a node whose default route moves — a
cable unplugged and Wi-Fi taking over, a backup WAN, a USB adapter — keeps every vnode fenced to a wire
that carries nothing, until the next boot. This watcher subscribes to the
kernel's IPv4 route events over netlink and, when the sensed uplink differs from the one last pinned,
hands the new name to a callback that reinstalls the fences. The vnode helper runs one.

Events, not a timer: a cadence either wakes needlessly or leaves egress dead for most of its period. No
parsing: ANY route event re-senses through the one function that already decides the uplink. `NoUplink`
is not a change: nothing can leave anyway, and the stale pin keeps the fence closed.
Stdlib only: `socket.AF_NETLINK` with the `RTMGRP_IPV4_ROUTE` group needs no capability.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import socket
from typing import Awaitable, Callable

from . import hostnet

log = logging.getLogger("jaah.rootlib.uplink")

# linux/rtnetlink.h — the IPv4 route multicast group of NETLINK_ROUTE. Receiving it is unprivileged.
RTMGRP_IPV4_ROUTE = 0x40
# A burst of route events (a NIC bouncing) is let settle, then the uplink is re-sensed once for all of it.
SETTLE_SECONDS = 0.5
# While a re-pin has FAILED, the watcher retries on this cadence even with no route event — a transient
# nft/xtables failure on a stable route must not leave egress on a dead wire until something else moves.
RETRY_SECONDS = 5.0
# The netlink socket is restarted after a fault; this is the pause before it, so a broken kernel path does
# not spin the helper.
REOPEN_SECONDS = 2.0
# Wide enough that a dockerd restart creating many networks does not overflow it while a re-pin runs.
RCVBUF_BYTES = 1 << 20


def open_route_events() -> socket.socket:
    """A non-blocking netlink socket subscribed to IPv4 route changes. The payload is never read for
    content: a readable socket IS the event."""
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_BYTES)
    sock.bind((0, RTMGRP_IPV4_ROUTE))
    sock.setblocking(False)
    return sock


def drain(sock: socket.socket) -> None:
    """Empty the queue: the kernel sends one message per route touched, and only that some did matters.
    `ENOBUFS` — the queue overflowed while the watcher was busy — is itself proof that events happened,
    never a fault."""
    while True:
        try:
            sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            if exc.errno == errno.ENOBUFS:
                return
            raise


class _NetlinkEvents:
    """The default event source: one netlink socket, reopened after a fault."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._readable = asyncio.Event()

    def _open(self) -> socket.socket:
        if self._sock is None:
            self._sock = open_route_events()
            asyncio.get_running_loop().add_reader(self._sock.fileno(), self._readable.set)
        return self._sock

    def close(self) -> None:
        if self._sock is not None:
            asyncio.get_running_loop().remove_reader(self._sock.fileno())
            self._sock.close()
            self._sock = None
            self._readable.clear()

    async def wait(self) -> None:
        while True:
            try:
                sock = self._open()
                await self._readable.wait()
                self._readable.clear()
                drain(sock)
                return
            except OSError as exc:
                log.error("route-event socket failed (%s) — reopening in %.0fs", exc, REOPEN_SECONDS)
                self.close()
                await asyncio.sleep(REOPEN_SECONDS)


def _current_uplink(sense: Callable[[], str]) -> str | None:
    try:
        return sense()
    except hostnet.NoUplink as exc:
        log.warning("no uplink right now (%s) — fences keep their last pin", exc)
        return None


async def watch(on_change: Callable[[str], None], *,
                sense: Callable[[], str] = hostnet.node_uplink,
                wait_for_event: Callable[[], Awaitable[None]] | None = None,
                settle: float = SETTLE_SECONDS, retry: float = RETRY_SECONDS) -> None:
    """Run forever: re-pin to the sensed uplink at start, then on every route change that moved it.

    The start re-pin is what catches a move that happened while the helper was down (an upgrade restart);
    it is idempotent and costs what the boot rebuild costs. `on_change` runs off the loop thread; one that
    raises is logged, the pin is NOT recorded as moved, and it is retried after `retry` seconds or on the
    next event, whichever comes first. `wait_for_event` is the seam tests use in place of netlink.
    """
    loop = asyncio.get_running_loop()
    events = None
    if wait_for_event is None:
        events = _NetlinkEvents()
        wait_for_event = events.wait

    pinned: str | None = None
    sensed = _current_uplink(sense)
    log.info("watching the default route (uplink now: %s)", sensed or "-")
    try:
        while True:
            if sensed is not None and sensed != pinned:
                log.info("pinning egress to %s (was %s)", sensed, pinned or "-")
                try:
                    await loop.run_in_executor(None, on_change, sensed)
                    pinned = sensed
                except Exception:  # noqa: BLE001 — the fence stays as it was; retried below
                    log.exception("re-pinning egress to %s failed — retrying in %.0fs", sensed, retry)
            if sensed is not None and sensed != pinned:
                try:
                    await asyncio.wait_for(wait_for_event(), timeout=retry)
                except asyncio.TimeoutError:
                    pass
            else:
                await wait_for_event()
            await asyncio.sleep(settle)
            sensed = _current_uplink(sense)
    finally:
        if events is not None:
            events.close()


def start(on_change: Callable[[str], None], **kw) -> asyncio.Task:
    """The watcher as a background task of the running loop; a crash is logged, never the server's."""
    async def _guarded() -> None:
        try:
            await watch(on_change, **kw)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("uplink watcher stopped — egress will not follow a route change until restart")
    return asyncio.get_running_loop().create_task(_guarded())
