"""Assignments from either side — which projects a node runs, which nodes a project runs on.

One relationship, two doors: `GET/PUT /api/nodes/{node_id}/projects` and `GET/PUT /api/projects/{project_id}/
nodes` read and write the same `node_projects` rows, so a save on one side shows on the other at once. A PUT is
REPLACE-SET over the explicit rows on its side: an entry it omits is unassigned, except a pair the node's group
carries (`via_group`, `removable: false`), which only the group line places. On such a pair an explicit row
exists only to hold a static count, so `static_slots: null` there drops the row rather than writing one.

The assignment's `static_slots` is the ONE static-slot surface: null means none (0), a number is refused above
the org's plan (`app/static_slots_policy.py`).

The rules are not here: who holds how many worktrees is `app/slot_allocation.py`'s, and every PUT ends in its
`converge`, whose counts and warnings the response carries as `outcome`. Writing needs `require_assignment_writer`
(D13, `routes/projects.py`); reading needs the side's own view key, and lists only what the caller can see.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app import edition, edition_features, pool_state, slot_allocation, slot_state, static_slots_policy, task_lifecycle
from app.auth import require_user
from app.authz.catalog import Tenancy
from app.authz.declarations import feature
from app.authz.object_access import can_access
from app.authz.principal import Principal, current_principal
from app.authz.visibility import ObjectAccess, visible_node_clause, visible_project_clause
from app.db import get_session
from app.entity_shape import SYSTEM_ACTOR_ID, StrictModel, UuidRefOut
from app.events import emit_console_event
from app.models.identity import AdminUser
from app.models.node_groups import NodeProject
from app.models.nodes import Node
from app.models.projects import Project
from app.models.worktrees import Worktree
from app.node_identity import node_ref
from app.project_identity import project_or_404
from app.routes.projects import MAX_STATIC_SLOTS, require_assignment_writer
from app.tenancy.ids import is_tenant_org

router = APIRouter(tags=["assignments"])


class ProjectAssignmentIn(StrictModel):
    """One project a node should run; `static_slots` null means no static slot (dynamic worktrees only)."""

    project_id: int
    static_slots: Optional[int] = Field(default=None, ge=0, le=MAX_STATIC_SLOTS)


class NodeAssignmentIn(StrictModel):
    """One node a project should run on; `static_slots` as on `ProjectAssignmentIn`."""

    node_id: UUID
    static_slots: Optional[int] = Field(default=None, ge=0, le=MAX_STATIC_SLOTS)


class NodeProjectsIn(StrictModel):
    assignments: list[ProjectAssignmentIn]


class ProjectNodesIn(StrictModel):
    assignments: list[NodeAssignmentIn]


class GroupRef(UuidRefOut):
    pass


class LiveRunOut(BaseModel):
    """One run on the pair that removing it kills: a task (`task_id`, `title`), a task the caller may not see
    (`hidden`), or a console session (all empty)."""

    task_id: Optional[int]
    title: Optional[str]
    # A task the caller may not see: counted, never named.
    hidden: bool = False
    vm: bool


class AssignmentOut(BaseModel):
    """One assignment with what the badge needs: `static_slots` is the override (null = none),
    `effective_static` what the pair holds (the override, else 0)."""

    node_id: UUID
    node_name: str
    # The node is marked MicroVM (`Node.microvm`): the project's Slots › Nodes mark.
    node_microvm: bool = False
    project_id: int
    project_name: str
    static_slots: Optional[int]
    effective_static: int
    # The pair's slots a task or a person holds, not already retiring: what unassigning would destroy.
    held: int
    # Every run on the pair that removing it kills — held slots and live MicroVMs — named for the confirmation.
    live: list[LiveRunOut] = Field(default_factory=list)
    # The node's group carries this project; only the group line unassigns it.
    via_group: Optional[GroupRef]
    removable: bool


class CandidateOut(BaseModel):
    """An unassigned candidate on the other side: a project id, or a node uuid."""

    id: Union[int, UUID]
    name: str
    # A node candidate's `Node.microvm`; None for a project.
    microvm: Optional[bool] = None


class OutcomeOut(BaseModel):
    """What the save's converge did (`slot_allocation.ConvergeOutcome`)."""

    created: int
    destroyed: int
    demoted: int
    tasks_requeued: list[int]
    warnings: list[str]


class AssignmentsOut(BaseModel):
    assignments: list[AssignmentOut]
    available: list[CandidateOut]
    # Still onboarding (D15): a new node or project is assigned everything automatically.
    onboarding_active: bool


class AssignmentsSavedOut(AssignmentsOut):
    outcome: OutcomeOut


# ── auto-assign, attributed ───────────────────────────────────────────────────────────────────────


def auto_assign_as(
    session: Session, actor: Optional[AdminUser], *, node_id: Optional[UUID] = None,
    project_id: Optional[int] = None,
) -> int:
    """`slot_allocation.auto_assign` for a TENANT org, with the rows it made attributed to a person: a pool
    clone authenticates as its assignment's creator (`slot_allocation._declarer`), and the system actor can
    clone no private repo. The person is `actor`, else — an actor-less door: the trial node, a group's
    scale-out — the project's own creator. The node-row and project-row doors call this, never the bare
    function. An operator-plane org has no company-setup card to finish, so its latch would never close:
    it is never auto-assigned."""
    if node_id is not None:
        org_id = session.scalar(select(Node.org_id).where(Node.id == node_id))
    else:
        org_id = session.scalar(select(Project.org_id).where(Project.id == project_id))
    # The community edition's one org is org zero, and its one node runs every project (`app/ce_bootstrap.py`).
    if org_id is None or not (is_tenant_org(org_id) or edition.IS_CE):
        return 0
    created = slot_allocation.auto_assign(session, node_id=node_id, project_id=project_id)
    if not created:
        return 0
    scope = NodeProject.node_id == node_id if node_id is not None else NodeProject.project_id == project_id
    creators = dict(session.execute(
        select(Project.id, Project.created_by_id).where(Project.org_id == org_id)
    ).tuples().all())
    for row in session.scalars(select(NodeProject).where(scope, NodeProject.created_by_id == SYSTEM_ACTOR_ID)):
        person = actor.id if actor is not None else creators.get(row.project_id)
        if person is not None and person != SYSTEM_ACTOR_ID:
            row.created_by_id = person
            row.updated_by_id = person
    session.flush()
    return created


# ── validating the submission ─────────────────────────────────────────────────────────────────────


def _field_error(index: int, field: str, message: str) -> HTTPException:
    """A 422 in FastAPI's own field-error shape, so the form marks the row it came from."""
    return HTTPException(
        status_code=422,
        detail=[{"loc": ["body", "assignments", index, field], "msg": message, "type": "value_error"}],
    )


def _refuse_static_above_plan(
    session: Session, org_id: Optional[UUID], wanted: dict[tuple[UUID, int], Optional[int]],
) -> None:
    """A field error on the first override above the org's plan (`app/static_slots_policy.py`); an override
    already saved at its value is kept as is."""
    limit = static_slots_policy.cap(session, org_id)
    if limit is None or not wanted:
        return
    current = {
        (n, p): s for n, p, s in session.execute(
            select(NodeProject.node_id, NodeProject.project_id, NodeProject.static_slots).where(
                tuple_(NodeProject.node_id, NodeProject.project_id).in_(list(wanted))
            )
        ).all()
    }
    for index, (pair, static_slots) in enumerate(wanted.items()):
        why = static_slots_policy.refusal(limit, static_slots, current.get(pair))
        if why is not None:
            raise _field_error(index, "static_slots", why)


def _node_or_404(session: Session, principal: Principal | None, node_id: str, *, mode: ObjectAccess) -> Node:
    """The node this request is about, or a 404 naming it (`routes/gha_runners.py` imports it: its node routes share
    the message).

    The path parameter is the node uuid (a non-uuid resolves to nothing, the same 404); the object grant is the
    probe, refused with the unknown-node body so the refusal is no existence oracle. `mode` is the caller's."""
    unknown = HTTPException(status_code=404, detail=f"unknown node '{node_id}'")
    row = session.scalar(select(Node).where(Node.id == node_ref(node_id)))
    if row is None or not can_access(session, principal, "node", row.id, mode=mode):
        raise unknown
    return row


def _refuse_work_on_host(session: Session, node_id: Optional[UUID]) -> None:
    """`vnodes.refuse_work_on_host`; the community edition has no Multi-Node Hosts."""
    if not edition_features.enabled("multi_node_hosts"):
        return
    from app import vnodes

    vnodes.refuse_work_on_host(session, node_id)


# ── reading ───────────────────────────────────────────────────────────────────────────────────────


def _read(
    session: Session, principal: Principal | None, *, node: Optional[Node] = None,
    project: Optional[Project] = None,
) -> AssignmentsOut:
    """One side's assignments and the other side's unassigned candidates, both narrowed to what the caller
    can see and to the subject's org."""
    org_id = node.org_id if node is not None else project.org_id
    host = False
    if edition_features.enabled("multi_node_hosts"):  # The community edition has no Multi-Node Hosts.
        from app import vnodes

        host = node is not None and vnodes.is_host(session, node.id)
    found = slot_allocation.effective_assignments(
        session, node_id=node.id if node is not None else None, project_id=project.id if project is not None else None,
    )
    if node is not None:
        others = {p.id: p for p in session.scalars(
            select(Project).where(Project.org_id == org_id, visible_project_clause(principal)).order_by(Project.name)
        )}
        nodes = {node.id: node}
        projects = others
    else:
        others = {n.id: n for n in session.scalars(
            select(Node).where(Node.org_id == org_id, visible_node_clause(principal), pool_state.not_a_vnode_host())
            .order_by(Node.name)
        )}
        nodes = others
        projects = {project.id: project}

    held = {
        (n, p): count for n, p, count in session.execute(
            select(Worktree.node_id, Worktree.project_id, func.count())
            .where(
                Worktree.state != slot_state.FREE,
                Worktree.retiring_at.is_(None),
                Worktree.node_id == node.id if node is not None else Worktree.project_id == project.id,
            )
            .group_by(Worktree.node_id, Worktree.project_id)
        ).tuples()
    }
    live = _live_runs(session, principal, node_id=node.id if node is not None else None,
                      project_id=project.id if project is not None else None)
    out: list[AssignmentOut] = []
    assigned: set[Union[int, UUID]] = set()
    for item in found:
        pair_node, pair_project = nodes.get(item.node_id), projects.get(item.project_id)
        if pair_node is None or pair_project is None:
            continue  # the other side is one the caller cannot see
        assigned.add(item.project_id if node is not None else item.node_id)
        out.append(AssignmentOut(
            node_id=pair_node.id, node_name=pair_node.name, node_microvm=bool(pair_node.microvm),
            project_id=pair_project.id,
            project_name=pair_project.name, static_slots=item.override,
            effective_static=item.effective_static, held=held.get((item.node_id, item.project_id), 0),
            live=live.get((item.node_id, item.project_id), []),
            via_group=GroupRef(id=item.group_id, name=item.group_name) if item.group_id is not None else None,
            removable=item.group_id is None,
        ))
    out.sort(key=lambda a: (a.project_name, a.node_name) if node is not None else (a.node_name, a.project_name))
    return AssignmentsOut(
        assignments=out,
        # A Multi-Node Host runs no work, so nothing is offered to it.
        available=[] if host else [
            CandidateOut(id=key, name=row.name, microvm=bool(row.microvm) if node is None else None)
            for key, row in others.items() if key not in assigned
        ],
        onboarding_active=slot_allocation.onboarding_active(session, org_id),
    )


def _live_runs(
    session: Session, principal: Principal | None, *, node_id: Optional[UUID], project_id: Optional[int],
) -> dict[tuple[UUID, int], list[LiveRunOut]]:
    """Per pair, every run removing it kills: a held slot not already retiring (the converge retires it), and a
    live MicroVM (`_end_vms`). One side is fixed, as in `_read`. A task the caller may not see is counted with
    neither id nor title."""
    from app.authz.visibility import scoped_task_select
    from app.models.nodes import Microvm
    from app.models.tasks import Task

    visible = {row for row in session.scalars(
        scoped_task_select(principal).with_only_columns(Task.id).where(Task.id.in_(
            select(Worktree.task_id).where(Worktree.node_id == node_id if node_id is not None
                                           else Worktree.project_id == project_id)
            .union(select(Microvm.task_id).where(Microvm.node_id == node_id if node_id is not None
                                                 else Microvm.project_id == project_id))
        ))
    )}
    out: dict[tuple[UUID, int], list[LiveRunOut]] = {}
    slots = (
        select(Worktree.node_id, Worktree.project_id, Worktree.task_id, Task.title)
        .outerjoin(Task, Task.id == Worktree.task_id)
        .where(Worktree.state != slot_state.FREE, Worktree.retiring_at.is_(None),
               Worktree.node_id == node_id if node_id is not None else Worktree.project_id == project_id)
    )
    queries = [(False, slots)]
    if edition_features.enabled("microvm"):  # The community edition has no MicroVMs.
        from app.microvm_placement import LIVE

        queries.append((True, (
            select(Microvm.node_id, Microvm.project_id, Microvm.task_id, Task.title)
            .outerjoin(Task, Task.id == Microvm.task_id)
            .where(Microvm.state.in_(LIVE),
                   Microvm.node_id == node_id if node_id is not None else Microvm.project_id == project_id)
        )))
    for is_vm, query in queries:
        for pair_node, pair_project, task_id, title in session.execute(query):
            seen = task_id in visible
            out.setdefault((pair_node, pair_project), []).append(LiveRunOut(
                task_id=task_id if seen else None, title=title if seen else None,
                hidden=task_id is not None and not seen, vm=is_vm,
            ))
    for runs in out.values():
        runs.sort(key=lambda run: (run.task_id is None, run.task_id or 0))
    return out


def _end_vms(session: Session, pairs: set[tuple[UUID, int]]) -> list:
    """Removing a project from a MicroVM node ends its live VMs there, as the converge's retire ends a held slot's
    run: each VM is ordered destroyed and its task goes back to the queue with no attempt spent
    (`slot_allocation.requeue_after_destroy`, the slot's own requeue). Does not commit; the caller sends what it
    returns after its commit."""
    if not edition_features.enabled("microvm"):
        return []  # The community edition has no MicroVMs.
    from app import microvm_lifecycle, microvm_rail
    from app.microvm_placement import LIVE
    from app.models.nodes import Microvm
    from app.models.tasks import Task

    issued: list = []
    if not pairs:
        return issued
    reason = "its project was removed from this node"
    for vm in session.scalars(
        select(Microvm).where(tuple_(Microvm.node_id, Microvm.project_id).in_(pairs), Microvm.state.in_(LIVE))
        .order_by(Microvm.created_at)
    ).all():
        task_state = None
        if vm.kind == microvm_rail.KIND_TASK and vm.task_id is not None:
            # Task before VM, the order `destroy_for_task` takes them.
            task_state = session.scalar(select(Task.state).where(Task.id == vm.task_id).with_for_update())
        # Re-read under its own lock: the sweep or a node fact may have ended it since the read above.
        vm = session.scalars(select(Microvm).where(Microvm.id == vm.id).with_for_update()).first()
        if vm is None or vm.state not in LIVE:
            continue
        if task_state in task_lifecycle.SLOT_DESTROYED.sources:
            slot_allocation.requeue_after_destroy(session, int(vm.task_id), reason)
        one = microvm_lifecycle.request_destroy(session, vm, retain=False, reason=f"VM {vm.id}: {reason}")
        if one is not None:
            issued.append(one)
    return issued


# ── writing ───────────────────────────────────────────────────────────────────────────────────────


def _replace(
    session: Session, actor: AdminUser, wanted: dict[tuple[UUID, int], Optional[int]], *,
    node_id: Optional[UUID] = None, project_id: Optional[int] = None,
) -> list:
    """Make the explicit rows on one side match `wanted` (module docstring). Every pair the following converge
    will take — assigned, holding a row, or wanted — is locked first, in converge's own order, so this write
    and a concurrent sweep cannot deadlock; the assignments are re-read under the locks (`lock_pair`)."""
    held = select(Worktree.node_id, Worktree.project_id).distinct().where(
        Worktree.node_id == node_id if node_id is not None else Worktree.project_id == project_id
    )
    before = slot_allocation.effective_assignments(session, node_id=node_id, project_id=project_id)
    scope_pairs = {(a.node_id, a.project_id) for a in before} | set(session.execute(held).tuples().all())
    for pair in sorted(scope_pairs | set(wanted), key=lambda k: (str(k[0]), k[1])):
        slot_allocation.lock_pair(session, *pair)
    current = slot_allocation.effective_assignments(session, node_id=node_id, project_id=project_id)
    group_carried = {(a.node_id, a.project_id) for a in current if a.group_id is not None}
    scope = NodeProject.node_id == node_id if node_id is not None else NodeProject.project_id == project_id
    rows = {(r.node_id, r.project_id): r for r in session.scalars(select(NodeProject).where(scope).with_for_update())}

    removed: set[tuple[UUID, int]] = set()
    for pair, row in rows.items():
        if pair not in wanted or (pair in group_carried and wanted[pair] is None):
            session.delete(row)
            if pair not in group_carried:
                removed.add(pair)
    for pair, static_slots in wanted.items():
        row = rows.get(pair)
        if pair in group_carried and static_slots is None:
            continue
        if row is None:
            org_id = session.scalar(select(Node.org_id).where(Node.id == pair[0]))
            session.add(NodeProject(
                node_id=pair[0], project_id=pair[1], org_id=org_id, static_slots=static_slots,
                created_by_id=actor.id, updated_by_id=actor.id,
            ))
        elif row.static_slots != static_slots:
            row.static_slots = static_slots
            # No `updated_at` trigger on this table: its writers stamp it (`slot_allocation.pin_to_declared`).
            row.updated_at = datetime.now(timezone.utc)
            row.updated_by_id = actor.id
    session.flush()
    return _end_vms(session, removed)


def _saved(
    session: Session, principal: Principal | None, actor: AdminUser, *, node: Optional[Node] = None,
    project: Optional[Project] = None, issued: Sequence = (),
) -> AssignmentsSavedOut:
    """Converge the side just written, commit, and answer with the fresh read plus what the converge did."""
    outcome = slot_allocation.converge(
        session, node_id=node.id if node is not None else None,
        project_id=project.id if project is not None else None, actor=actor,
    )
    org_id = node.org_id if node is not None else project.org_id
    emit_console_event(session, entity="worktree", id=0, action="upserted", org_id=org_id)
    session.commit()
    for batch in issued:
        batch.send()
    read = _read(session, principal, node=node, project=project)
    return AssignmentsSavedOut(
        **read.model_dump(),
        outcome=OutcomeOut(
            created=outcome.created, destroyed=outcome.destroyed, demoted=outcome.demoted,
            tasks_requeued=list(outcome.tasks_requeued), warnings=list(outcome.warnings),
        ),
    )


# ── the four routes ───────────────────────────────────────────────────────────────────────────────


@router.get("/api/nodes/{node_id}/projects", response_model=AssignmentsOut)
@feature("nodes", "view", tenancy=Tenancy.CALLER_PLANE)
def get_node_projects(
    node_id: str,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AssignmentsOut:
    """The projects this node runs, and the org's other projects it could."""
    node = _node_or_404(session, principal, node_id, mode=ObjectAccess.READ)
    return _read(session, principal, node=node)


@router.put("/api/nodes/{node_id}/projects", response_model=AssignmentsSavedOut)
# The read's plane: an operator writes any org's node from its page, as every other node-page write does.
@feature("projects", "create", tenancy=Tenancy.CALLER_PLANE)
def put_node_projects(
    node_id: str,
    body: NodeProjectsIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AssignmentsSavedOut:
    """Replace the projects this node runs (module docstring), then converge the node."""
    require_assignment_writer(user, principal)
    node = _node_or_404(session, principal, node_id, mode=ObjectAccess.WRITE)
    wanted: dict[tuple[UUID, int], Optional[int]] = {}
    ids = {item.project_id for item in body.assignments}
    in_org = set(session.scalars(
        select(Project.id).where(Project.id.in_(ids), Project.org_id == node.org_id)
    ).all()) if ids else set()
    for index, item in enumerate(body.assignments):
        if item.project_id not in in_org:
            raise _field_error(index, "project_id", f"no such project {item.project_id}")
        if (node.id, item.project_id) in wanted:
            raise _field_error(index, "project_id", f"project {item.project_id} is listed twice")
        wanted[(node.id, item.project_id)] = item.static_slots
    if wanted:
        # A Multi-Node Host runs no work: refused in its own words (`app/vnodes.py`).
        _refuse_work_on_host(session, node.id)
    _refuse_static_above_plan(session, node.org_id, wanted)
    issued = _replace(session, user, wanted, node_id=node.id)
    return _saved(session, principal, user, node=node, issued=issued)


@router.get("/api/projects/{project_id}/nodes", response_model=AssignmentsOut)
@feature("projects", "view", object_type="project", object_params=("project_id",))
def get_project_nodes(
    project_id: int,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AssignmentsOut:
    """The nodes this project runs on, and the org's other nodes it could."""
    return _read(session, principal, project=project_or_404(session, project_id))


@router.put("/api/projects/{project_id}/nodes", response_model=AssignmentsSavedOut)
@feature("projects", "delete", object_type="project", object_params=("project_id",))
def put_project_nodes(
    project_id: int,
    body: ProjectNodesIn,
    user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> AssignmentsSavedOut:
    """Replace the nodes this project runs on (module docstring), then converge the project."""
    require_assignment_writer(user, principal)
    project = project_or_404(session, project_id)
    wanted: dict[tuple[UUID, int], Optional[int]] = {}
    ids = {item.node_id for item in body.assignments}
    in_org = set(session.scalars(
        select(Node.id).where(Node.id.in_(ids), Node.org_id == project.org_id)
    ).all()) if ids else set()
    for index, item in enumerate(body.assignments):
        if item.node_id not in in_org:
            raise _field_error(index, "node_id", f"no such node {item.node_id}")
        if (item.node_id, project.id) in wanted:
            raise _field_error(index, "node_id", f"node {item.node_id} is listed twice")
        wanted[(item.node_id, project.id)] = item.static_slots
        try:
            # A Multi-Node Host runs no work: refused in its own words (`app/vnodes.py`), on the row named.
            _refuse_work_on_host(session, item.node_id)
        except HTTPException as exc:
            raise _field_error(index, "node_id", str(exc.detail)) from None
    _refuse_static_above_plan(session, project.org_id, wanted)
    issued = _replace(session, user, wanted, project_id=project.id)
    return _saved(session, principal, user, project=project, issued=issued)
