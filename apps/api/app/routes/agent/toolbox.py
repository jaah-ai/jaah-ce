"""The toolbox routes' one shape. A toolbox is a tool on a node (the outcome scan, the duel review, the deploy probe,
`jaah-audit`) that speaks to the control plane through the daemon's TASK-scoped verbs, never the model: a GET for its
plan and a POST for what it found. Every such pair is the same door and the same refusals,
so each lives here as data (`ToolboxCall`) and `toolbox_router` builds the routes.

The task is the one `X-Agent-Task-Id` declares — jaahd stamps it from the calling peer, so it is the runner's own —
and it must not be held on another node (`routes/agent/auth.py::_refuse_a_node_not_holding`). With a `persona` it
must be an open task of that persona (`open_declared_task`) and the rules call receives the `Task`; without one
(`declared_task_id`) it receives the task id. Everything else is the same 404 `not_found`, so a runner can neither
read nor report another project's run. A refusal the rules raise (`refusals`) is rolled back and answered with its
status; `HTTPException` passes through. Every call commits what the rules wrote.

`@infra(Tenancy.ORG_SCOPED)` on each, no router prefix: the authz policy suite reads the node wall on these off the
live route registry; the public-UI split test's source mirror does not see them.
"""
from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from app import node_credentials
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db_async import DbDep
# The one door onto `/api/agent/*` and the node wall every task-scoped route shares.
from app.routes.agent.auth import AgentDoor, declared_task_id, open_declared_task

#: `(exception, status, detail)`: the rules' refusal, the HTTP status it earns, and its detail from the exception
#: (`str(exc)` when None).
Refusal = tuple[type[BaseException], int, Optional[Callable[[Any], Any]]]


@dataclass(frozen=True)
class ToolboxCall:
    """One route of a toolbox: `run(session, task_or_id)` for a GET, `run(session, task_or_id, body)` for a POST.
    `with_caller` adds `caller=` (the node's `AgentCaller`) to that call. `after` (POST only) runs on the loop after
    the commit with `run`'s result and returns the response — for work that must follow the commit off the session
    (`events.flush`). `caps` (GET only) passes the toolbox's announced capabilities, the `caps` query value
    (comma-separated), as `caps=` (a frozenset)."""

    method: str
    path: str
    name: str
    doc: str
    run: Callable[..., Any]
    refusals: tuple[Refusal, ...] = ()
    with_caller: bool = False
    after: Optional[Callable[[Any], Awaitable[Any]]] = None
    caps: bool = False


def toolbox_router(*, tag: str, not_found: str, persona: Optional[str], calls: tuple[ToolboxCall, ...]) -> APIRouter:
    """The router carrying `calls`, each gated as every toolbox route is (see the module docstring). Each handler
    reports the CALLING module as its home (read off the frame, never self-declared), which is what the authz
    policy suite's `_INFRA_MODULES` keys the agent surface on."""
    module = sys._getframe(1).f_globals["__name__"]
    router = APIRouter(tags=[tag])
    for call in calls:
        # The surface the public listener denies; the split test's source mirror does not see a factory route.
        if not call.path.startswith("/api/agent/"):
            raise ValueError(f"toolbox route {call.path} is off the /api/agent/ surface")
        handler = _handler(call, not_found=not_found, persona=persona)
        handler.__module__ = module
        router.add_api_route(call.path, handler, methods=[call.method], name=call.name)
    return router


def _handler(call: ToolboxCall, *, not_found: str, persona: Optional[str]) -> Callable[..., Awaitable[Any]]:
    refused = tuple(exc for exc, _, _ in call.refusals)

    def _sync(session: Session, body: Any, agent: node_credentials.AgentCaller, x_agent_task_id: str,
              caps: Optional[str] = None) -> Any:
        subject: Any = (
            open_declared_task(session, agent, x_agent_task_id, persona=persona, not_found=not_found)
            if persona is not None
            else declared_task_id(session, agent, x_agent_task_id, not_found=not_found)
        )
        args = (session, subject) if body is None else (session, subject, body)
        kwargs: dict[str, Any] = {"caller": agent} if call.with_caller else {}
        if call.caps:
            kwargs["caps"] = frozenset(c.strip() for c in (caps or "").split(",") if c.strip())
        try:
            out = call.run(*args, **kwargs)
        except refused as exc:
            session.rollback()
            status, detail = next((s, d) for e, s, d in call.refusals if isinstance(exc, e))
            raise HTTPException(status_code=status, detail=str(exc) if detail is None else detail(exc)) from None
        session.commit()
        return out

    if call.method == "GET" and call.caps:
        async def handler(
            db: DbDep,
            door: AgentDoor,
            caps: Optional[str] = Query(default=None, max_length=200),
        ) -> dict[str, Any]:
            return await db.run_sync(_sync, None, *door, caps)
    elif call.method == "GET":
        async def handler(  # type: ignore[misc]
            db: DbDep,
            door: AgentDoor,
        ) -> dict[str, Any]:
            return await db.run_sync(_sync, None, *door)
    else:
        async def handler(  # type: ignore[misc]
            body: dict[str, Any],
            db: DbDep,
            door: AgentDoor,
        ) -> dict[str, Any]:
            out = await db.run_sync(_sync, body, *door)
            return await call.after(out) if call.after is not None else out

    handler.__name__ = handler.__qualname__ = call.name
    handler.__doc__ = call.doc
    return infra(Tenancy.ORG_SCOPED)(handler)
