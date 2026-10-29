"""The stale-tab guard (R4/AC8): no request is served as an org the browser session has switched away from.

Every tab of a browser shares one session cookie, so an org switch in one tab (`POST /api/auth/switch`) moves
them all. A tab still showing the old org says which org it loaded with — the `X-Jaah-Org` header on every
`/api` request, or `?org=` on `/api/stream` (an `EventSource` cannot set headers) — and when that is not the
cookie's active org the request is refused `409 {"detail": "org_switched", "active_org": {"id", "name"}}`
BEFORE any handler runs: never served as either org. No header, no check (node, agent and CLI callers). Every
`/api` path is guarded, sign-out and the switch itself included: a stale tab reloads before it acts.

Placed inside `SessionAuthMiddleware` and outside `SessionRefreshMiddleware` (`app/main.py`), so the refusal
is never re-minted: the refresh middleware is never reached by it.

The cookie row's org is cached per row: a membership row's `org_id` never changes (a person in another org is
another row), so a matching header costs no query. A mismatch is re-read and checked live before refusing, so a
dead session still reaches its handler's 401.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from app import auth
from app.db import IdentitySessionLocal
from app.entity_shape import optional_uuid
from app.models.identity import AdminUser
from app.org_names import org_name
from app.tenancy import seam

ORG_HEADER = "x-jaah-org"
STREAM_PATH = "/api/stream"
STREAM_ORG_PARAM = "org"
ORG_SWITCHED = "org_switched"


@lru_cache(maxsize=4096)
def _row_org(admin_id: UUID) -> Optional[UUID]:
    """The org of membership row `admin_id` (immutable for the row's life), or None for no such row. Never
    invalidated: an id is never reused, and a cached None only sends the request on to the live re-read."""
    # Identity escape: this read resolves who the cookie names, before any org is bound (`auth._session_user`).
    with seam.identity_scope(), IdentitySessionLocal() as session:
        return session.scalar(select(AdminUser.org_id).where(AdminUser.id == admin_id))


def _live_active_org(claims: auth.SessionClaims) -> Optional[dict]:
    """`{"id", "name"}` of the cookie's active org when its row still holds the session, else None."""
    with IdentitySessionLocal() as session:
        with seam.identity_scope():
            row = session.get(AdminUser, claims.admin_id)
            if not auth.session_row_is_live(row, claims.epoch, claims.iepoch):
                return None
        # Outside the escape, which declares exactly what it reads (`orgs` is not tenant-owned).
        if auth.session_idled_out(None, session, row, claims.issued_at):
            return None
        with seam.identity_scope():
            return {"id": str(row.org_id), "name": org_name(session, row.org_id)}


def _same_org(claimed: str, org_id: Optional[UUID]) -> bool:
    return org_id is not None and optional_uuid(claimed.strip()) == org_id


class StaleTabGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        claimed = request.headers.get(ORG_HEADER)
        if claimed is None and path == STREAM_PATH:
            claimed = request.query_params.get(STREAM_ORG_PARAM)
        if not claimed or not path.startswith("/api"):
            return await call_next(request)
        claims = auth.read_session(request.cookies.get(auth.COOKIE_NAME))
        if claims is None or _same_org(claimed, await run_in_threadpool(_row_org, claims.admin_id)):
            return await call_next(request)
        active = await run_in_threadpool(_live_active_org, claims)
        if active is None or _same_org(claimed, UUID(active["id"])):
            return await call_next(request)
        return JSONResponse({"detail": ORG_SWITCHED, "active_org": active}, status_code=409)
