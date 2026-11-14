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
dead session still reaches its handler's 401. Both reads are identity reads on the async pool (`app/db_async.py`).
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app import auth, db_async
from app.entity_shape import optional_uuid
from app.models.identity import AdminUser
from app.org_names import org_name
from app.tenancy import seam

ORG_HEADER = "x-jaah-org"
STREAM_PATH = "/api/stream"
STREAM_ORG_PARAM = "org"
ORG_SWITCHED = "org_switched"


#: `_row_org`'s cache bound; past it the cache starts over (an id is never reused, so a stale entry cannot lie).
_ROW_ORG_CACHE_MAX = 4096
_row_orgs: dict[UUID, Optional[UUID]] = {}


async def _row_org(admin_id: UUID) -> Optional[UUID]:
    """The org of membership row `admin_id` (immutable for the row's life), or None for no such row. Never
    invalidated: an id is never reused, and a cached None only sends the request on to the live re-read."""
    if admin_id in _row_orgs:
        return _row_orgs[admin_id]
    org_id = await db_async.run_in_session(_row_org_in, admin_id, identity=True)
    if len(_row_orgs) >= _ROW_ORG_CACHE_MAX:
        _row_orgs.clear()
    _row_orgs[admin_id] = org_id
    return org_id


def _row_org_in(session: Session, admin_id: UUID) -> Optional[UUID]:
    # Identity escape: this read resolves who the cookie names, before any org is bound (`auth._session_user`).
    with seam.identity_scope():
        return session.scalar(select(AdminUser.org_id).where(AdminUser.id == admin_id))


async def _live_active_org(claims: auth.SessionClaims) -> Optional[dict]:
    """`{"id", "name"}` of the cookie's active org when its row still holds the session, else None."""
    return await db_async.run_in_session(_live_active_org_in, claims, identity=True)


def _live_active_org_in(session: Session, claims: auth.SessionClaims) -> Optional[dict]:
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
        if claims is None or _same_org(claimed, await _row_org(claims.admin_id)):
            return await call_next(request)
        active = await _live_active_org(claims)
        if active is None or _same_org(claimed, UUID(active["id"])):
            return await call_next(request)
        return JSONResponse({"detail": ORG_SWITCHED, "active_org": active}, status_code=409)
