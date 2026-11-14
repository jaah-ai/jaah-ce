"""A sync session of its own for the org routes' shared services that still take one (`routes/orgs.py`).

`org_lifecycle.wind_down`, `offboard.execute` and `billing_org_plan.retry_bundle` are awaited with a sync `Session`
they hand to worker threads, and an approved draft's sign-up claim commits on a second session after the approval's
(`signup_draft.submit_by_operator`). A route module may not open `BridgedSession` (the async-route check), so the
org routes open it here. The route's own `DbDep` session is released before either is called: a coroutine never
holds two connections.

`awaited_on_own_session` opens its session on the loop thread, where no bridge exists (`app/db.py::current_bind`),
so it binds `offline_engine` and those services' inline statements block the loop while they run, as their sync
route session did. Rare operator flows (an org's wind-down, offboarding, a bundle retry); the cure is those services
taking an `AsyncSession`."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, TypeVar

from sqlalchemy.orm import Session

from app.db import BridgedSession

T = TypeVar("T")


async def awaited_on_own_session(fn: Callable[..., Awaitable[T]], /, *args: Any, **kwargs: Any) -> T:
    """`await fn(session, *args, **kwargs)` on a sync session of its own, closed after: `fn` commits what it keeps."""
    with BridgedSession() as session:
        return await fn(session, *args, **kwargs)


def on_own_session(fn: Callable[[Session], T]) -> T:
    """`fn(session)` on a sync session of its own, closed after — for a worker thread's second session (the draft
    approval's claim). `fn` commits what it keeps."""
    with BridgedSession() as session:
        return fn(session)
