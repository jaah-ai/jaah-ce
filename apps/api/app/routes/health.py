"""Composite component-health endpoint for the console header badge.

`GET /api/health/components` returns a per-component snapshot — API DB reachability, whether that DB
connection is TLS (`db_tls`), the browser SSE LISTEN relay, and the local jaahd node's presence — so the console's "Live" badge is a real composite
rather than an SSE-only signal. It also carries the operator-alert relay's two counters
(`app/alert_relay.py`), the alert path's only positive liveness signal — see `ComponentHealthOut`.

Every sub-check is defensive: any failure degrades that one component (false, or "unknown" for
jaahd) and never raises, so the endpoint never 500s and the badge always renders.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import alert_relay, db_async, node_presence, jaah_discovery
from app.auth import require_user
from app.authz.declarations import feature
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.node_identity import resolve_node_id
from app.node_identity import cp_node_id
from app.tenancy.ids import is_tenant_org

router = APIRouter(tags=["health"])


class JaahdHealth(BaseModel):
    status: str  # "online" | "offline" | "unknown"
    last_seen: Optional[str]


class ComponentHealthOut(BaseModel):
    api: bool
    # Whether the serving DB connection is encrypted (`pg_stat_ssl`) — the behavioural proof that the RDS
    # path's `verify-full` (app/db_credentials.py) is in force. False on the local stack, which has no TLS.
    # `None` for a tenant: a control-plane fact, withheld like the relay counters below.
    db_tls: Optional[bool]
    sse: bool
    jaahd: JaahdHealth
    # The alert path's only positive signal: every alert layer is best-effort, so no exception ever
    # surfaces a broken relay. Zero relayed and zero failed over a period the fleet alerted in means the
    # path is dead. Process-local (reset by a restart) — read as a rate. `None` for a tenant (and
    # `jaahd` reads "unknown"): these are control-plane facts, and `system:view` is grantable to org
    # admins. The test is operator-plane membership (`OPERATOR_PLANE_ORG_IDS`), not the superadmin flag.
    alerts_relayed_total: Optional[int]
    alerts_relay_failed_total: Optional[int]


async def _api_ok() -> bool:
    """DB reachability: a bare `SELECT 1` through the same async pool the data routes use."""
    try:
        async with db_async.async_session() as session:
            await session.execute(text("SELECT 1"))
            return True
    except Exception:
        return False


async def _db_tls() -> bool:
    """Whether the connection the data routes use is TLS, read from the server's own `pg_stat_ssl`."""
    try:
        async with db_async.async_session() as session:
            return bool(
                (
                    await session.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"))
                ).scalar()
            )
    except Exception:
        return False


def _sse_ok(request: Request) -> bool:
    """The browser stream/LISTEN relay is alive iff its single background task is still running."""
    try:
        task = getattr(request.app.state, "console_events_task", None)
        return task is not None and not task.done()
    except Exception:
        return False


async def _jaahd_health() -> JaahdHealth:
    """The local node daemon's presence: `jaah_discovery.status_and_last_seen` over its socket row
    (`app/node_presence.py`) and its roster row; "unknown" (not-asserted, not offline) when the node id is
    unset or the lookup fails.

    `NODE_ID` is resolved to its uuid before comparing (`node_identity.cp_node_id`, which also resolves a
    deployed CP's name inside the operator org): socket rows are keyed by the machine key, so a
    raw `==` misses an online node whose spellings differ. An unresolvable value is compared raw, the way
    `node_identity.bus_key` falls back."""
    try:
        raw = resolve_node_id()
        if not raw:
            return JaahdHealth(status="unknown", last_seen=None)
        async with db_async.async_session() as session:
            entry, persisted = await session.run_sync(_jaahd_rows, raw)
        status, last_seen = jaah_discovery.status_and_last_seen(entry, persisted)
        return JaahdHealth(status=status, last_seen=last_seen)
    except Exception:
        return JaahdHealth(status="unknown", last_seen=None)


def _jaahd_rows(session: Session, raw: str):
    """`_jaahd_health`'s reads, over its async session (`run_sync`): the node's socket row and `nodes.last_seen`."""
    node_uuid = cp_node_id(session)
    key = str(node_uuid) if node_uuid is not None else raw
    entry = next(
        (e for e in node_presence.snapshot(session) if e.get("node_id") == key), None
    )
    persisted = (
        session.scalar(select(Node.last_seen).where(Node.id == node_uuid))
        if node_uuid is not None else None
    )
    return entry, persisted


@router.get("/api/health/components", response_model=ComponentHealthOut)
@feature("system", "view")
async def health_components(
    request: Request, user: AdminUser = Depends(require_user)
) -> ComponentHealthOut:
    if is_tenant_org(user.org_id):
        # A tenant's own halves only — see `ComponentHealthOut`. Plane membership, not
        # `is_operator_plane`: non-superadmin operator staff keep the daemon's presence.
        return ComponentHealthOut(
            api=await _api_ok(),
            db_tls=None,
            sse=_sse_ok(request),
            jaahd=JaahdHealth(status="unknown", last_seen=None),
            alerts_relayed_total=None,
            alerts_relay_failed_total=None,
        )
    relay_counters = alert_relay.counters()
    return ComponentHealthOut(
        api=await _api_ok(),
        db_tls=await _db_tls(),
        sse=_sse_ok(request),
        jaahd=await _jaahd_health(),
        alerts_relayed_total=relay_counters["alerts_relayed_total"],
        alerts_relay_failed_total=relay_counters["alerts_relay_failed_total"],
    )
