"""CPU offload (seam S2): run CPU-heavy work off the serving loop, on threads that run in parallel.

The control plane serves every request on one event loop (`app/db_async.py`), so a handler that spends real CPU
— building a response over a large row set, crypto, an archive, markdown, JSON over megabytes — stalls everyone
while it runs. `run_cpu` moves that work to a module thread pool (`cpu_count() * 2` threads) and awaits it.

`fn` must not touch the database: its connection lives on the loop. `app/db_async.py::async_engine` raises off the
loop thread, so the async pool is out of its reach; a `BridgedSession()` is not fenced, so a CPU function must not
open one either. The call runs in a copy of the caller's context, as `asyncio.to_thread` does. The threads run Python
in parallel only on a free-threaded interpreter; under a GIL they still keep the loop responsive.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

#: Twice the CPUs: a CPU-bound call that waits briefly (a page fault, a lock) leaves its core to another.
CPU_THREADS = (os.cpu_count() or 1) * 2

_executor: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    """The CPU pool, built on first use and again after a `shutdown` (a test's second app lifespan)."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=CPU_THREADS, thread_name_prefix="cpu")
    return _executor


async def run_cpu(fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """`fn(*args, **kwargs)` on the CPU pool, awaited. `fn` must not touch the database (the module docstring)."""
    context = contextvars.copy_context()
    call = functools.partial(context.run, fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(_pool(), call)


def shutdown() -> None:
    """Lifespan shutdown (`app/main.py`): stop taking work and drop what has not started; a running call finishes."""
    global _executor
    pool, _executor = _executor, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
