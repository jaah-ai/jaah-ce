"""The console relay's ASGI app — its own ECS service, run from the api image (package docstring).

    ./entrypoint.sh app.console_relay.main:app --host 0.0.0.0 --port 8000

ONE Granian worker (`apps/api/entrypoint.sh` refuses more): a session's two legs meet in this process's memory
(`server.rendezvous`), and the task's address (`cp_peers.self_address`) must name exactly one process for a proxied
leg to find it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator

from fastapi import FastAPI

from app import cp_peers, loop_debug, worker_guard
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app import db_async
from app.console_relay import server, tickets

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # The serving process's own checks, as the API's lifespan runs them (`app/main.py::_lifespan`).
    worker_guard.assert_gil_off()
    worker_guard.assert_threads_start_clean()
    loop_debug.configure()
    # This task's address, resolved before serving — off the loop, and raising here on ECS fails the
    # start (and so the deploy) loudly rather than every leg later (`cp_peers.self_address`).
    await asyncio.to_thread(cp_peers.self_address)
    # The relay's async pool (`app/db_async.py`) belongs to this loop: its node-leg token checks draw on it, sized by
    # the relay's own `DB_POOL_SIZE`, never an unpooled engine.
    await db_async.start()
    toucher = asyncio.ensure_future(server.touch_forever())
    try:
        yield
    finally:
        toucher.cancel()
        await asyncio.gather(toucher, return_exceptions=True)
        await db_async.stop()


app = FastAPI(title="console-relay", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
app.include_router(server.router)


@app.get(tickets.HEALTH_PATH)
@always(Tenancy.BOOTSTRAP)
async def health() -> dict[str, bool]:
    """The target group's health check. Liveness only: the database is checked by every claim."""
    return {"ok": True}
