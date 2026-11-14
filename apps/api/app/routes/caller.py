"""The signed-in caller's own-org session, for `@always(Tenancy.ORG_SCOPED)` handlers whose guard binds no org
(`routes/inbox.py`, `routes/client_events.py`)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import act_as
from app.authz.principal import Principal
from app.models.identity import AdminUser
from app.tenancy import seam


@contextmanager
def caller_session(principal: Principal | None, session: Session) -> Iterator[tuple[Session, AdminUser]]:
    """`session` — the route's, inside `run_sync` — bound to the caller's own org, and the caller's user row — 401
    without a signed-in user (the agent principal included)."""
    if principal is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    with seam.bound_to(principal.org_id):
        user = act_as.own_row(session, principal)
        if user is None or user.is_system:
            raise HTTPException(status_code=401, detail="unauthorized")
        yield session, user
