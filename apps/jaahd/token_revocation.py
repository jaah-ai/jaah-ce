"""The confirmed-revocation back-off and exit for a node the operator deleted — every platform.

Deleting a node in the console revokes its `NODE_TOKEN`, but nothing stops a daemon that is
still running: a self-hosted Ubuntu box, a deleted Multi-Node Host, a Mac or a Windows box would dial every
few seconds and 401 forever — enough to trip prod's X-Agent-Token failure-rate alarm on its own. So, during an unbroken streak of 401s:
  * after `BACKOFF_AFTER_SECS` the daemon BACKS OFF: `admit` lets ONE control-plane call through per
    `PROBE_INTERVAL_SECS` and refuses the rest locally (`_agent_urlopen` raises them as transport failures);
  * after the window — `WINDOW_SECS` off Linux, `LINUX_WINDOW_SECS` on Linux — it logs one line and exits 0,
    a clean exit no service definition restarts (`jaahd.service` is `Restart=on-failure`). Linux
    waits longer because a CP-side fault that 401s every node would stop the whole fleet until restarted
    by hand; an hour leaves room to fix it first.

What it takes, so a rotation, a re-login or a blip never trips it:
  * every attempt re-reads the token from `node.env` (`control_plane_api._agent_urlopen`'s 401 arm), so a
    rotated or re-issued token is retried at once — and a 401 under a DIFFERENT token restarts the clock;
  * any answer that is not a 401 (a success, or any other status) clears the streak and the back-off;
  * a single 401 never exits: the window must have elapsed between the streak's first and latest 401.
A transport failure (no answer at all) neither counts nor clears: the CP never said the token was bad.
The clock is `time.monotonic`, which does not advance while a laptop sleeps, so a lid closed over a
revocation does not count as 401 time."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

import host_platform

log = logging.getLogger("jaahd.token_revocation")

WINDOW_SECS = 600.0
LINUX_WINDOW_SECS = 3600.0
BACKOFF_AFTER_SECS = 60.0
PROBE_INTERVAL_SECS = 60.0


def _exit_clean(code: int) -> None:
    """End the whole process from whichever thread saw the 401: `sys.exit` would only end that thread."""
    logging.shutdown()
    os._exit(code)


class RevocationWatch:
    """The streak of consecutive 401s, keyed on the token each was answered for. Thread-safe."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 exit_fn: Callable[[int], None] = _exit_clean, window_secs: float | None = None) -> None:
        self._clock = clock
        self._exit = exit_fn
        if window_secs is None:
            window_secs = LINUX_WINDOW_SECS if host_platform.is_linux() else WINDOW_SECS
        self._window = float(window_secs)
        self._lock = threading.Lock()
        self._since: float | None = None
        self._token: str | None = None
        self._last_probe: float | None = None

    def admit(self) -> bool:
        """Whether a control-plane call may go out now. False only while backing off a 401 streak older than
        `BACKOFF_AFTER_SECS` — and then one caller per `PROBE_INTERVAL_SECS` is still admitted, as the probe
        that finds a re-issued token or a recovered CP."""
        with self._lock:
            if self._since is None:
                return True
            now = self._clock()
            if now - self._since < BACKOFF_AFTER_SECS:
                return True
            if self._last_probe is not None and now - self._last_probe < PROBE_INTERVAL_SECS:
                return False
            self._last_probe = now
            return True

    def observe_answer(self) -> None:
        """The CP answered with something other than 401: the token is not revoked."""
        with self._lock:
            self._since = None
            self._token = None
            self._last_probe = None  # defensive: inert while PROBE_INTERVAL_SECS <= BACKOFF_AFTER_SECS

    def observe_401(self, token: str | None) -> None:
        """The CP answered 401 to `token` (the value re-read from `node.env` for this attempt)."""
        with self._lock:
            now = self._clock()
            if self._since is None or token != self._token:
                self._since, self._token, self._last_probe = now, token, None
                return
            elapsed = now - self._since
        if elapsed >= self._window:
            log.error("NODE_TOKEN refused (HTTP 401) on every control-plane call for %.0fs — this node was "
                      "deleted in the console; exiting", elapsed)
            self._exit(0)


WATCH = RevocationWatch()
