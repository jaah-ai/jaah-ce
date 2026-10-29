"""Your Cloud node lifecycle: `POST /api/nodes/{node_id}/cloud/stop` and `/start`, `PUT …/cloud/instance-type` and
`…/cloud/disk`. Delete stays `DELETE /api/nodes/{node_id}` (`delete.py`).

Each door is an org admin's (`@org_admin`, then `_administers_own_hosts` on the node's own org), refused 403 while the
org's plan has the node's cloud off (`clouds.require_provider`), and answers 202 with the node's `cloud` block. The
work is `app/cloud_lifecycle.py`'s, the type change `app/durable/cloud_change.py`'s; nothing here knows the cloud.
A Jaah Hosted machine keeps its own lifecycle (Billing) and an operator-allocated one is the operator's: both refused,
as is a dynamic node group's member (409: its group decides its machines). Delete stays open to a group's member.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features, node_allocation
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import org_admin
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.entity_shape import StrictModel
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.routes.nodes.delete import HOSTED_MACHINE, _writable_node_uuid
from app.routes.nodes.node_wire import CloudOut, _cloud_node_errors, _cloud_out
from app.tenancy.ids import is_tenant_org

if edition_features.enabled("cloud_nodes"):
    from app import cloud_lifecycle, clouds
    from app.cloud_node_launch import InstanceGone

router = APIRouter(tags=["nodes"])
log = logging.getLogger("cockpit-nodes")

#: A node with no Your Cloud instance (self-hosted, or a launch still placing it).
NO_CLOUD_INSTANCE = "This node has no cloud machine."
#: An operator-allocated machine, which its tenant uses but does not manage.
ALLOCATED_MACHINE = "This machine is managed by Jaah."
#: The cloud says the instance no longer exists (deleted outside Jaah).
MACHINE_GONE = "The machine no longer exists in your cloud. Delete the node."


class StopIn(StrictModel):
    #: Interrupt the tasks running on the node (each back to `ready`, its attempt spent) instead of refusing.
    force: bool = False


class InstanceTypeIn(StrictModel):
    instance_type: str = Field(min_length=1)
    #: As Stop's: the change stops the machine, so tasks running on it are refused (409) unless forced.
    force: bool = False


class DiskIn(StrictModel):
    size_gb: int = Field(gt=0, le=65536)
    #: As Stop's, where the cloud stops the machine to grow its disk (`CloudProvider.grow_stops_machine`).
    force: bool = False


def _cloud_node(session: Session, node_id: str, user: AdminUser, principal: Optional[Principal]) -> Node:
    """The node to act on, locked; 403/404/409 for every node these doors refuse. The plan switch is asked last,
    of the node's own org, before any cloud call."""
    node_uuid = _writable_node_uuid(session, node_id, user, principal)
    row = session.scalar(select(Node).where(Node.id == node_uuid))
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown node '{node_id}'")
    if node_allocation.jaah_cloud([row.id]):
        raise HTTPException(status_code=409, detail=HOSTED_MACHINE)
    if row.operator_allocated and is_tenant_org(user.org_id):
        raise HTTPException(status_code=403, detail=ALLOCATED_MACHINE)
    if not edition_features.enabled("cloud_nodes"):
        raise HTTPException(status_code=409, detail=NO_CLOUD_INSTANCE)  # The community edition has no cloud nodes.
    try:
        ref = clouds.instance_ref_of(row.infra)
    except ValueError:
        ref = None
    if ref is None:
        raise HTTPException(status_code=409, detail=NO_CLOUD_INSTANCE)
    group = cloud_lifecycle.group_name(session, row)
    if group is not None:
        raise HTTPException(status_code=409, detail=cloud_lifecycle.IN_GROUP.format(name=group))
    clouds.require_provider(session, row.org_id, ref.provider)
    if cloud_lifecycle.change_running(row.infra):
        raise HTTPException(status_code=409, detail=cloud_lifecycle.CHANGE_RUNNING)
    return row


def _tasks_running(exc: cloud_lifecycle.TasksRunning) -> JSONResponse:
    """The 409 a door answers while tasks run on the machine: the sentence, and the tasks a Force would interrupt."""
    return JSONResponse(status_code=409, content={
        "detail": str(exc), "tasks": [{"id": task.id, "title": task.title} for task in exc.tasks],
    })


@contextmanager
def _errors() -> Iterator[None]:
    """`_cloud_node_errors`, and 409 for a machine the cloud no longer has."""
    try:
        with _cloud_node_errors():
            yield
    except InstanceGone as exc:
        raise HTTPException(status_code=409, detail=MACHINE_GONE) from exc


def _accepted(session: Session, row: Node) -> CloudOut:
    session.refresh(row)
    out = _cloud_out(row)
    assert out is not None  # `_cloud_node` refused a node with none
    return out


@router.post("/api/nodes/{node_id}/cloud/stop", response_model=CloudOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def stop_cloud_node(
    node_id: str,
    body: Optional[StopIn] = Body(default=None),
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CloudOut | JSONResponse:
    """Take the node out of dispatch, then stop its machine (the disk kept). 409 on a machine not running, and
    naming the running tasks unless `force`, which sends each back to `ready` (`cloud_lifecycle.stop`)."""
    row = _cloud_node(session, node_id, user, principal)
    if cloud_lifecycle.state_of(row.infra) != "running":
        raise HTTPException(status_code=409, detail=cloud_lifecycle.NOT_RUNNING)
    try:
        with _errors():
            cloud_lifecycle.stop(session, row, force=bool(body and body.force))
    except cloud_lifecycle.TasksRunning as exc:
        session.rollback()
        return _tasks_running(exc)
    return _accepted(session, row)


@router.post("/api/nodes/{node_id}/cloud/start", response_model=CloudOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def start_cloud_node(
    node_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CloudOut:
    """Start the node's machine; it takes work again once it connects. A machine still stopping answers `starting`:
    it starts once stopped (`cloud_lifecycle.start`). 409 on one running or already starting."""
    row = _cloud_node(session, node_id, user, principal)
    state = cloud_lifecycle.state_of(row.infra)
    if state in ("running", "starting"):
        raise HTTPException(status_code=409, detail=(
            cloud_lifecycle.ALREADY_RUNNING if state == "running" else cloud_lifecycle.ALREADY_STARTING
        ))
    with _errors():
        cloud_lifecycle.start(session, row)
    return _accepted(session, row)


@router.put("/api/nodes/{node_id}/cloud/instance-type", response_model=CloudOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def change_cloud_node_type(
    body: InstanceTypeIn,
    node_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CloudOut | JSONResponse:
    """Record the change, take the node out of dispatch and enqueue `app/durable/cloud_change.py` (stop → set →
    start) in one transaction; the 202 shows it running. Tasks running on the machine: 409 as Stop's, unless
    `force`, which sends them back to `ready`."""
    if edition_features.enabled("cloud_nodes"):  # `_cloud_node` refuses every node in the community edition.
        from app.durable import cloud_change

    row = _cloud_node(session, node_id, user, principal)
    tasks = cloud_lifecycle.running_tasks(session, row.id)
    if tasks and not body.force:
        return _tasks_running(cloud_lifecycle.TasksRunning(tasks))
    with _errors():
        change_id = cloud_lifecycle.begin_change(session, row, body.instance_type.strip())
    cloud_change.enqueue_in_transaction(session, row.id, change_id)
    session.commit()
    cloud_lifecycle.end_tasks(session, tasks, reason=f"its node {row.name} is changing machine type")
    return _accepted(session, row)


@router.put("/api/nodes/{node_id}/cloud/disk", response_model=CloudOut, status_code=202)
@org_admin(Tenancy.CALLER_PLANE)
def grow_cloud_node_disk(
    body: DiskIn,
    node_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CloudOut | JSONResponse:
    """Grow the boot disk (422 for a size not larger); the node grows its root filesystem into it on its next
    check-in (`cloud_lifecycle.grow_disk`). A cloud that stops the machine to grow it: 409 as Stop's while tasks
    run, unless `force`."""
    row = _cloud_node(session, node_id, user, principal)
    try:
        with _errors():
            cloud_lifecycle.grow_disk(session, row, body.size_gb, force=body.force)
    except cloud_lifecycle.TasksRunning as exc:
        session.rollback()
        return _tasks_running(exc)
    return _accepted(session, row)
