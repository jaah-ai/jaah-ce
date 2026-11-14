"""Slow-callback logging on the serving event loop: a sync call that blocks the loop names itself in the log.

The API serves every request from ONE asyncio loop (`apps/api/entrypoint.sh`), so a handler that does blocking I/O
or heavy CPU on it stalls everyone. Each loop callback is timed, and one that holds the loop longer than
`SLOW_CALLBACK_SECONDS` is logged (`app.loop_debug`, WARNING) with what ran: the task and its coroutine, or the
callable — never its arguments, which can be a credential handed between threads.

NOT asyncio's debug mode, which would also name slow callbacks: it captures a stack trace for every handle and
future the loop creates, which on the dev control plane more than doubled the latency of a 24-request burst. Timing
the callback is two clock reads. On only where `API_ASYNCIO_DEBUG` says so: the dev control plane (its terraform's
`cp_api_asyncio_debug`). Called first in both lifespans.
"""
from __future__ import annotations

import asyncio
import asyncio.events
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("app.loop_debug")

ENV_VAR = "API_ASYNCIO_DEBUG"
SLOW_CALLBACK_SECONDS = 0.1

_ORIGINAL_RUN = asyncio.events.Handle._run


def enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def _await_chain(coro: Any) -> list[str]:
    """The coroutines a task is suspended through, outermost first, by qualified name (never their arguments)."""
    names: list[str] = []
    for _ in range(64):   # bounded: a chain is never this deep, and a cycle must not hang the log line
        code = getattr(coro, "cr_code", None)
        if code is None:
            break
        names.append(code.co_qualname)
        coro = getattr(coro, "cr_await", None)
    return names


def _describe(handle: asyncio.Handle) -> str:
    """What a handle ran, named without its arguments: a task step names the task and the chain of coroutines it is
    suspended through once the slow step returned — which runs through the handler that blocked, since it awaits next
    (read AFTER the step, so a task that finished in that step names only its outer coroutine) — and anything else its
    callable. Plus the thread,
    so a helper thread's own loop is told apart from the serving loop."""
    callback: Any = getattr(handle, "_callback", None)
    callback = getattr(callback, "func", callback)   # a functools.partial names what it wraps
    owner = getattr(callback, "__self__", None)
    thread = threading.current_thread().name
    if isinstance(owner, asyncio.Task):
        chain = _await_chain(owner.get_coro()) or [type(owner.get_coro()).__name__]
        return f"task {owner.get_name()!r} in {' > '.join(chain)} [thread {thread}]"
    return f"callback {getattr(callback, '__qualname__', type(callback).__name__)} [thread {thread}]"


def _timed_run(self: asyncio.Handle) -> None:
    started = time.perf_counter()
    try:
        _ORIGINAL_RUN(self)
    finally:
        held = time.perf_counter() - started
        if held >= SLOW_CALLBACK_SECONDS:
            log.warning("slow loop callback: %s held the event loop for %.0f ms", _describe(self), held * 1000)


def configure() -> bool:
    """Time every loop callback when `API_ASYNCIO_DEBUG` is set. Idempotent; returns whether it is on."""
    if not enabled():
        return False
    if asyncio.events.Handle._run is not _timed_run:
        asyncio.events.Handle._run = _timed_run  # type: ignore[method-assign]
        log.info("slow loop callback logging on (> %.0f ms)", SLOW_CALLBACK_SECONDS * 1000)
    return True


def _reset_for_tests() -> None:
    asyncio.events.Handle._run = _ORIGINAL_RUN  # type: ignore[method-assign]
