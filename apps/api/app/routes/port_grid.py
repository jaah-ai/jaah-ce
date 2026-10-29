"""The port grid's CRUD — `/api/port-grid`, the Settings section behind it.

Per-service range starts are rows (`port_grid_vars`, one set per org) edited here by a superadmin —
the operator for the operator plane's rows, a tenant superadmin for their own org's. The four grid
scalars are ordinary operator knobs written only through `PUT /api/settings`. Resolver, seed and
reasoning live in `app/port_grid.py`.

Validation is a floor plus advice (operator decision). Refused: a name that is not an env identifier
(a newline or `=` injects a `.env` assignment) and a start outside 1..65535. Softer invariants are
warnings returned beside the saved value, never a block; the pool-port check reports fallout.
Delete is unconditional: it demotes the var to tier 2 (node-side auto-assigned), a working state.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import node_bus, port_grid
from app.operator_settings import cache as settings_cache
# Aliased as in `app/routes/accounts.py`: `_var_out` takes the resolved map under the plain name.
from app.actors import actor_names as resolve_actor_names, actor_names_for, stamp_actor
# `authz.org_admin_writer` on the org's own var CRUD (the `projects` field — see `get_port_grid`);
# `require_operator` on reband, which reaches every connected node — see `app/auth.py::require_operator`.
from app.auth import require_operator
from app.authz.catalog import Tenancy
from app.authz.declarations import feature
from app.authz.principal import org_admin_writer
from app.db import get_session
from app.entity_shape import EntityOut, StrictModel, entity_fields, flush_or_409, row_by_uuid_or_404
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.worktrees import PortGridVar
from app.routes.commands import dispatch_and_audit

log = logging.getLogger("port-grid")

#: An org's own grid vars: its administrator (an operator on the operator plane).
_require_grid_admin = org_admin_writer("Port grid settings")

router = APIRouter(prefix="/api/port-grid", tags=["port-grid"])

_VAR_NAME_RE = re.compile(port_grid.VAR_NAME_PATTERN)

# The existing `run` verb with an op the node knows (`apps/jaahd/worktree_ops.py`), running
# the pool-port check with `--fix --restart` — one implementation of rebanding.
REBAND_OP = "ports_reband"

# `--restart` gives each stack's `./dc up -d` 300s, so the default 180s command timeout could expire
# mid-sweep, leaving `.env`s rewritten but containers on old ports. Timeouts are still reported per node.
REBAND_TIMEOUT_SECONDS = 900.0


def _validated_var_name(value: Optional[str]) -> Optional[str]:
    """The env-identifier grammar, enforced here because this string becomes a `.env` write key on every
    node (node-side twin: `worktree_mechanics.ports._PORT_VAR_NAME_RX`, since names also arrive via
    `projects.port_vars`). A newline would append e.g. `COMPOSE_PROJECT_NAME=shared`, collapsing every slot
    onto shared volumes.
    """
    if value is None:
        return None
    name = value.strip()
    if not _VAR_NAME_RE.match(name):
        raise ValueError(
            f"port grid var name {value!r} is not an environment identifier "
            f"(must match {port_grid.VAR_NAME_PATTERN}); it becomes a .env assignment key on every node"
        )
    # And it must read as a port var, so a row cannot shadow an unrelated key (`port_grid.VAR_NAME_MUST_CONTAIN`).
    if port_grid.VAR_NAME_MUST_CONTAIN not in name.upper():
        raise ValueError(
            f"port grid var name {name!r} does not contain "
            f"{port_grid.VAR_NAME_MUST_CONTAIN!r}; this table writes a PORT into every slot's .env, so a "
            f"name that is not a port var would shadow an unrelated key (NODE_TOKEN, ADMIN_PASSWORD)"
        )
    return name


def _validated_range_start(value: object) -> Optional[int]:
    """A host port, or nothing. 1..65535 is the whole floor; softer bounds are warnings.

    `mode="before"` is load-bearing: lax coercion would turn `true` into 1 and `"8000"` into 8000, so only
    the raw value lets the wrong JSON type be refused.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"range_start must be a whole number, not {type(value).__name__} ({value!r})"
        )
    if not 1 <= value <= 65535:
        raise ValueError(f"range_start {value} is outside the host port space (1..65535)")
    return value


class PortGridVarOut(EntityOut):
    range_start: int
    # Projects declaring this var, by name. Presentation only; lets the delete confirmation say what it demotes.
    projects: list[str] = []


class PortGridOut(BaseModel):
    """The whole page in one response: the rows, the live scalars, and the advice.

    Scalars are echoed here so derived windows are never computed from a stale second fetch.
    """

    vars: list[PortGridVarOut] = []
    block_size: int
    stride: int
    window: int
    auto_port_floor: int
    auto_port_ceiling: int
    # Returned on every read and write, so a warning from an earlier save does not vanish.
    warnings: list[str] = []


class PortGridVarCreateIn(StrictModel):
    name: str
    range_start: int
    description: str = ""

    _check_name = field_validator("name")(_validated_var_name)
    _check_range_start = field_validator("range_start", mode="before")(_validated_range_start)


class PortGridVarUpdateIn(StrictModel):
    """A partial update; only the fields present are touched. Renames are legitimate: the row should follow a
    service's new `.env` spelling.
    """

    name: Optional[str] = None
    range_start: Optional[int] = None
    description: Optional[str] = None

    _check_name = field_validator("name")(_validated_var_name)
    _check_range_start = field_validator("range_start", mode="before")(_validated_range_start)


class RebandNodeResult(BaseModel):
    node_id: str
    status: str
    detail: str = ""


class RebandOut(BaseModel):
    dispatched: list[RebandNodeResult] = []



def _declaring_projects(session: Session, org: Optional[UUID]) -> dict[str, list[str]]:
    """`{VAR: [project, …]}` for every var some project in this org declares in `port_vars`, in one query.

    The org predicate is explicit rather than left to the seam: these routes are `Tenancy.CALLER_PLANE`,
    so the operator's read is fleet-wide and the page must still show one org.
    """
    return port_grid.usage_by_var(
        (row.name, row.port_vars or [])
        for row in session.scalars(
            select(Project).where(Project.org_id == org).order_by(Project.name)
        )
    )


def _var_out(
    row: PortGridVar, *, usage: dict[str, list[str]], actor_names: dict[UUID, str]
) -> PortGridVarOut:
    return PortGridVarOut(
        **entity_fields(row, actor_names),
        range_start=int(row.range_start),
        projects=usage.get(row.name, []),
    )


def _page(session: Session, org: Optional[UUID]) -> PortGridOut:
    """The whole read model for one org, rebuilt after every write so the caller sees its effect on warnings.

    The org is passed in by every route (`user.org_id`), never read from ambient scope, for
    `_declaring_projects`' reason.
    """
    rows = list(
        session.scalars(
            select(PortGridVar)
            .where(PortGridVar.org_id == org)
            .order_by(PortGridVar.range_start, PortGridVar.name)
        )
    )
    usage = _declaring_projects(session, org)
    actor_names = actor_names_for(session, rows)
    snapshot = port_grid.grid(session, org)
    return PortGridOut(
        vars=[_var_out(row, usage=usage, actor_names=actor_names) for row in rows],
        block_size=snapshot.block_size,
        stride=snapshot.stride,
        window=snapshot.window,
        auto_port_floor=snapshot.auto_port_floor,
        auto_port_ceiling=snapshot.auto_port_ceiling,
        warnings=port_grid.warnings_for(snapshot),
    )


def _one(session: Session, var_id: str, org: Optional[UUID]) -> PortGridVar:
    row = row_by_uuid_or_404(session, PortGridVar, var_id, detail="no such port grid var")
    if row.org_id != org:
        # 404, not 403: existence outside the caller's boundary is itself a cross-tenant fact.
        raise HTTPException(status_code=404, detail="no such port grid var")
    return row


def _flush_or_conflict(session: Session, name: str, org: Optional[UUID]) -> None:
    """Flush under `flush_or_409`, the 409 worded by whose row collided. `name` is unique within an org: it is the
    `.env` key every consumer keys on.

    The fleet-wide `uq_port_grid_vars_name` still stands (`app/models/worktrees.py::PortGridVar`), so a collision
    with another org's row says only "unavailable" rather than confirming another tenant's var; a collision with
    the caller's own row is reported plainly. The caller's row is looked up BEFORE the flush, under
    `no_autoflush` (the lookup would otherwise flush the pending row itself): `flush_or_409` takes its detail up
    front, and a rollback would have expired the instance anyway.

    Still open until that constraint is dropped: the 409 itself reveals the name is taken somewhere, and a
    tenant can squat a name. No wording fixes that.

    Every `IntegrityError` here is attributed to the name (no `constraints`); of the columns written, only
    `name` can violate a unique.
    """
    with session.no_autoflush:
        mine = session.scalar(
            select(PortGridVar.id).where(PortGridVar.org_id == org, PortGridVar.name == name)
        )
    flush_or_409(
        session,
        f"a port grid var named {name!r} already exists"
        if mine is not None
        else f"the port grid var name {name!r} is not available",
    )


@router.get("", response_model=PortGridOut)
@feature("system", "view", tenancy=Tenancy.CALLER_PLANE)
def get_port_grid(
    user: AdminUser = Depends(_require_grid_admin), session: Session = Depends(get_session)
) -> PortGridOut:
    """An administrator, not merely `system:view`, because of the `projects` field rather than the grid.

    The grid numbers are not sensitive (every node receives them), but `_declaring_projects` names every
    pool-enabled project, which a `system:view` holder without `projects:view` must not read. The console
    route is superadmin-gated anyway.
    """
    return _page(session, user.org_id)


@router.post("/vars", response_model=PortGridVarOut, status_code=201)
@feature("system", "create", tenancy=Tenancy.CALLER_PLANE)
def create_port_grid_var(
    body: PortGridVarCreateIn,
    user: AdminUser = Depends(_require_grid_admin),
    session: Session = Depends(get_session),
) -> PortGridVarOut:
    """Promote a var to tier 1. Declaring projects band onto the grid at their next lease; until then they keep
    their tier-2 port, so no fleet coordination is needed.
    """
    # `org_id` set explicitly: the operator's `CALLER_PLANE` scope is fleet-wide, so the write seam stamps
    # nothing, and the row would be invisible to the page that created it.
    row = PortGridVar(
        name=body.name, range_start=body.range_start, description=body.description or "",
        org_id=user.org_id,
    )
    stamp_actor(row, user, created=True)
    session.add(row)
    _flush_or_conflict(session, body.name, user.org_id)
    emit_console_event(session, entity="port_grid_var", id=row.id, action="upserted", org_id=row.org_id)
    session.commit()
    session.refresh(row)
    return _var_out(row, usage=_declaring_projects(session, user.org_id), actor_names=resolve_actor_names(
        session, [row.created_by_id, row.updated_by_id]
    ))


@router.put("/vars/{var_id}", response_model=PortGridVarOut)
@feature("system", "edit", tenancy=Tenancy.CALLER_PLANE)
def update_port_grid_var(
    var_id: str,
    body: PortGridVarUpdateIn,
    user: AdminUser = Depends(_require_grid_admin),
    session: Session = Depends(get_session),
) -> PortGridVarOut:
    row = _one(session, var_id, user.org_id)
    fields = body.model_dump(exclude_unset=True)
    for field, value in fields.items():
        if value is None and field != "description":
            # An explicit null asks to clear, and neither `name` nor `range_start` has a meaningful empty value
            # (deleting the row expresses that). Refuse.
            raise HTTPException(
                status_code=422, detail=f"{field} cannot be cleared; delete the var to demote it to tier 2"
            )
        setattr(row, field, "" if value is None else value)
    if fields:
        stamp_actor(row, user)
    _flush_or_conflict(session, str(fields.get("name") or row.name), user.org_id)
    emit_console_event(session, entity="port_grid_var", id=row.id, action="upserted", org_id=row.org_id)
    session.commit()
    session.refresh(row)
    return _var_out(row, usage=_declaring_projects(session, user.org_id), actor_names=resolve_actor_names(
        session, [row.created_by_id, row.updated_by_id]
    ))


@router.delete("/vars/{var_id}", status_code=204)
@feature("system", "delete", tenancy=Tenancy.CALLER_PLANE)
def delete_port_grid_var(
    var_id: str,
    user: AdminUser = Depends(_require_grid_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Demote a var to tier 2. No occupancy guard: the node auto-assigns a flat port on the next lease, and
    re-creating the row bands it back. Logged with declaring projects for traceability.
    """
    row = _one(session, var_id, user.org_id)
    declared_by = _declaring_projects(session, user.org_id).get(row.name, [])
    # Actor in the log: a delete destroys the row's own `updated_by_id`, so this is the only record.
    log.info(
        "port-grid: %s deleted %s (start %s) — demoted to tier 2; declared today by %s",
        user.email, row.name, row.range_start, ", ".join(declared_by) or "no project",
    )
    session.delete(row)
    emit_console_event(session, entity="port_grid_var", id=row.id, action="deleted", org_id=row.org_id)
    session.commit()
    return Response(status_code=204)


@router.post("/reband", response_model=RebandOut)
@feature("system", "edit")
async def reband_now(
    user: AdminUser = Depends(require_operator),
    session: Session = Depends(get_session),
) -> RebandOut:
    """Push the recomputed band onto every slot of every pool-enabled project on every connected node now, and
    restart the stacks that were live.

    The impatient path, never a repair: every slot rebands at its next create/lease anyway. It costs a
    restart of each warm stack whose ports moved, so it is an explicit action.

    Disconnected nodes are reported, not silently skipped. Each node's outcome is returned individually; a
    failure is recorded and the loop continues.
    """
    # Before dispatch: the node calls back into `GET /api/agent/port-grid`, and the knob cache TTL would
    # otherwise serve the replaced scalar.
    settings_cache.invalidate_cache()
    results: list[RebandNodeResult] = []
    # The async form: with peers the connected set is a DB read, which must not run on the loop.
    for node_id in await node_bus.registry.connected_nodes_async():
        try:
            reply, _audit_id = await dispatch_and_audit(
                session, node_id, "run", {"op": REBAND_OP}, user.email, timeout=REBAND_TIMEOUT_SECONDS,
            )
        except HTTPException as exc:
            # `dispatch_and_audit` already audited; the detail is the node-facing reason.
            results.append(
                RebandNodeResult(node_id=node_id, status="error", detail=str(exc.detail))
            )
            continue
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        # Exit code, not the bus status word: the script exits 1 when it found (and fixed) drift, which the node
        # reports as `failed`. So 0 and 1 are success; anything else, including no exit code, is an error.
        exit_code = result.get("exit_code")
        ran = isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code in (0, 1)
        detail = str(result.get("stderr") or result.get("reason") or "").strip()[-400:]
        results.append(
            RebandNodeResult(
                node_id=node_id,
                status="ok" if ran else "error",
                detail=detail if ran else (detail or f"node reply: {reply.get('status')!r}"),
            )
        )
    if not results:
        log.warning(
            "port-grid: reband requested by %s but no node has a live bus connection — nothing was "
            "dispatched; every slot still rebands at its next lease",
            user.email,
        )
    return RebandOut(dispatched=results)
