"""Run-telemetry read endpoints, served from the `run_telemetry` table.

* No eviction: the table is unbounded; `limit` bounds only the response.
* Ordering is newest-first-seen, not newest-published: `POST /api/agent/telemetry` upserts on
  `uq_run_telemetry_run_id` and `ON CONFLICT DO UPDATE` leaves `created_at` alone, so a run keeps the
  position of its first record. There is no `updated_at` to order by.

An empty list is a valid response before any run has posted telemetry."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.authz.visibility import visible_node_ref_clause
from app.db import get_session
from app.entity_shape import iso_or_none
from app.models.agent_sessions import RunTelemetry
from app.models.identity import AdminUser
from app.node_identity import names_by_id

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

# Default page size.
DEFAULT_LIMIT = 100


class RunTelemetryOut(BaseModel):
    """One run's telemetry row. `created_at` is the ordering signal (see the module docstring)."""

    run_id: str
    node_id: Optional[str]
    org_repo: Optional[str]
    task_id: Optional[str]
    payload: dict[str, Any]
    created_at: Optional[str]


def _out(row: RunTelemetry, node_name: Optional[str]) -> RunTelemetryOut:
    """`node_name` is passed in, not read off `row`: `run_telemetry.node_id` is a uuid FK, and resolving the
    name per row would be an N+1 on `list_runs`."""
    return RunTelemetryOut(
        run_id=row.run_id,
        node_id=node_name,
        org_repo=row.org_repo,
        task_id=row.task_id,
        payload=row.payload or {},
        created_at=iso_or_none(row.created_at),
    )


@router.get("/runs", response_model=list[RunTelemetryOut])
@feature("system", "view")
def list_runs(
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=1000),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
):
    """List recent run telemetry, narrowed to the nodes this caller was granted; `system:view` still required.

    The grant filter goes in the `WHERE`, not around `limit`: post-filtering the page would return fewer
    than `limit` rows while later granted rows existed, a shortfall a client cannot tell from the end."""
    rows = session.scalars(
        select(RunTelemetry)
        .where(visible_node_ref_clause(principal, RunTelemetry.node_id))
        .order_by(RunTelemetry.created_at.desc(), RunTelemetry.id.desc()).limit(limit)
    ).all()
    names = names_by_id(session, [row.node_id for row in rows])
    return [_out(row, names.get(row.node_id)) for row in rows]
