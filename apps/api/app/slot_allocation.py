"""Which worktrees exist on which node, and how many — the single home of the assignment model.

An **assignment** places a project on a node: an explicit `node_projects` row, or the node's group carrying
the project (`node_group_projects`, resolved here at read time and never materialised per node). A pair with
no assignment holds no worktree at all; every project is pool-capable, nothing enables it. Each assignment
holds a **static count**: the explicit row's `static_slots` override, else 0.

Two kinds of numbered worktree share one index space per (node, project), `1..index_ceiling` — the org's
port grid decides the ceiling (`app/port_grid.py::index_ceiling`, `pool_bound` here) — lowest free index
first; slot 0 (the main clone) is never counted. A `static` slot is held at the count and survives its
release; a `dynamic` one is made on demand and destroyed once freed. A row being destroyed (`retiring_at`)
still occupies its index until the node acknowledges the destroy and the row goes.

A project with no repository (`Project.has_repo` false) holds no static slot: each task gets a dynamic
**folder** slot, a row whose own `has_repo` is false. A slot's kind is copied from its project at declare and never
changes, so connecting a project's first repository or removing its last reaches only the next slot; and since a node reads one kind per
(node, project) pair (`pair_has_repo`), the old kind's rows drain before a row of the new kind is made.

`converge` is the one place that makes rows match the rules. It never runs a destroy: it retires (orders the
node to destroy, `retire`), demotes a held surplus static to `dynamic` (D12: the task keeps running and the
slot goes when freed), and declares missing statics. Safe to call repeatedly; the caller commits. Nothing
else re-derives these rules — dispatch, the routes and the console read them from here.

Two writers of `worktrees` columns live here, each a declared statement family
(`app/slot_columns.py::ALLOCATION`); the declare INSERT names the `DECLARATION` actor like every declare.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import (
    actors,
    agent_session_lifecycle,
    agent_sessions,
    edition,
    fleet_verdicts,
    forges,
    merge_lane_state,
    port_grid,
    runner_terminations,
    slot_state,
    slot_teardown,
    slot_title,
    task_lifecycle,
)
from app.events import emit_console_event
from app.entity_shape import SYSTEM_ACTOR_ID
from app.models.node_groups import NodeGroup, NodeGroupCapacity, NodeGroupProject, NodeProject
from app.models.nodes import Node
from app.models.orgs import Org
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import WORKTREE_KIND_DYNAMIC, WORKTREE_KIND_STATIC, SlotIntent, Worktree
from app.node_clone import resolve_clone
from app.user_activity import record_task_note

log = logging.getLogger(__name__)

# The main clone's number: unleasable, never a pool index, never counted.
MAIN_CLONE_NUMBER = 0

# The task-note action `requeue_after_destroy` writes (`task_note:slot_destroyed`).
NOTE_SLOT_DESTROYED = "slot_destroyed"


# ── reading the assignments ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Assignment:
    """One effective (node, project) assignment: the explicit row and the group line merged.

    `override` is the explicit row's `static_slots` (None = none set); `group_*` name the node's group
    when it carries the project; `explicit` says a `node_projects` row exists. `effective_static` is
    the override, else 0 — the count the pair holds before the index ceiling (`static_target` applies that).
    """

    node_id: UUID
    project_id: int
    override: Optional[int]
    group_id: Optional[UUID]
    group_name: Optional[str]
    effective_static: int
    explicit: bool


def effective_assignments(
    session: Session, *, node_id: Optional[UUID] = None, project_id: Optional[int] = None,
    project_ids: Optional[Iterable[int]] = None,
) -> list[Assignment]:
    """Every effective assignment, optionally narrowed to one node and/or one project (or a set of projects,
    `project_ids`), ordered by (node, project). The union of explicit rows and group lines; a group line is
    inherited whether or not its group is enabled, since disabling a group means "stop scaling", not "unplace
    its projects"."""
    explicit_q = select(NodeProject.node_id, NodeProject.project_id, NodeProject.static_slots)
    group_q = (
        select(Node.id, NodeGroupProject.project_id, NodeGroup.id, NodeGroup.name)
        .join(NodeGroupCapacity, NodeGroupCapacity.id == Node.node_group_capacity_id)
        .join(NodeGroupProject, NodeGroupProject.group_id == NodeGroupCapacity.group_id)
        .join(NodeGroup, NodeGroup.id == NodeGroupCapacity.group_id)
    )
    if node_id is not None:
        explicit_q = explicit_q.where(NodeProject.node_id == node_id)
        group_q = group_q.where(Node.id == node_id)
    if project_id is not None:
        explicit_q = explicit_q.where(NodeProject.project_id == project_id)
        group_q = group_q.where(NodeGroupProject.project_id == project_id)
    if project_ids is not None:
        wanted = sorted({int(p) for p in project_ids})
        explicit_q = explicit_q.where(NodeProject.project_id.in_(wanted))
        group_q = group_q.where(NodeGroupProject.project_id.in_(wanted))

    explicit = {(n, p): s for n, p, s in session.execute(explicit_q).all()}
    groups = {(n, p): (gid, gname) for n, p, gid, gname in session.execute(group_q).all()}
    keys = sorted(set(explicit) | set(groups), key=lambda k: (str(k[0]), k[1]))

    out: list[Assignment] = []
    for key in keys:
        override = explicit.get(key)
        group_id, group_name = groups.get(key, (None, None))
        out.append(Assignment(
            node_id=key[0], project_id=key[1], override=override, group_id=group_id, group_name=group_name,
            effective_static=override if override is not None else 0, explicit=key in explicit,
        ))
    return out


def hosting_node_ids(session: Session, project_id: int) -> set[UUID]:
    """Every node `project_id` may run on: an effective assignment, or a numbered row not being destroyed. The
    row arm only covers the window before the next `converge`, which retires every row of an unassigned pair;
    a project with no free static slot still runs on its assigned nodes, on a dynamic worktree."""
    rows = set(session.scalars(
        select(Worktree.node_id).where(
            Worktree.project_id == int(project_id), Worktree.number >= 1, Worktree.retiring_at.is_(None)
        ).distinct()
    ).all())
    return rows | {a.node_id for a in effective_assignments(session, project_id=int(project_id))}


def hosting_node_ids_by_project(session: Session, project_ids: Iterable[int]) -> dict[int, set[UUID]]:
    """`hosting_node_ids` for many projects in one Worktree read and one assignment read: every id in
    `project_ids` is a key, an unhosted one mapping to an empty set. The lease drain's pass roster
    (`routes/fleet.py::DispatchPassFacts.hosting_by_project`), which had re-read it per project per node."""
    wanted = sorted({int(p) for p in project_ids})
    out: dict[int, set[UUID]] = {p: set() for p in wanted}
    if not wanted:
        return out
    for node_id, project_id in session.execute(
        select(Worktree.node_id, Worktree.project_id).where(
            Worktree.project_id.in_(wanted), Worktree.number >= 1, Worktree.retiring_at.is_(None)
        ).distinct()
    ).all():
        out[project_id].add(node_id)
    for a in effective_assignments(session, project_ids=wanted):
        out[a.project_id].add(a.node_id)
    return out


def _assignment(session: Session, node_id: UUID, project_id: int) -> Optional[Assignment]:
    found = effective_assignments(session, node_id=node_id, project_id=project_id)
    return found[0] if found else None


def lock_pair(session: Session, node_id: UUID, project_id: int) -> None:
    """Serialise every writer of one (node, project) pair's assignment and rows — `converge`, and a per-slot
    Declare or Delete through `pin_to_declared` — for the rest of the transaction, so neither acts on a count
    the other is changing. Whoever takes it second re-reads both after it. A route takes it BEFORE its own
    insert or row lock: taken after, a converge holding it and waiting on that row would deadlock with it.
    Re-entrant within a transaction."""
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("slot_allocation"), func.hashtext(f"{node_id}:{project_id}")))
    )


def is_assigned(session: Session, node_id: UUID, project_id: int) -> bool:
    """Whether `project` may hold worktrees on `node` at all."""
    return _assignment(session, node_id, project_id) is not None


def pool_bound(session: Session, org_id: Optional[UUID]) -> int:
    """The index ceiling every (node, project) pair of `org_id` shares: `1..ceiling` are the numbered
    indexes one port block holds (`port_grid.index_ceiling`). Read once per node where it matters: the
    grid is a settings read plus one table read."""
    return port_grid.index_ceiling(port_grid.grid(session, org_id))


def _node_bound(session: Session, node_id: UUID) -> int:
    node = session.get(Node, node_id)
    return pool_bound(session, node.org_id) if node is not None else 0


def static_target(session: Session, node_id: UUID, project_id: int) -> int:
    """How many static slots the pair holds: the assignment's count clamped to the index ceiling, 0 when the
    pair is unassigned or the project has no repository (its slots are made per task)."""
    assignment = _assignment(session, node_id, project_id)
    project = session.get(Project, project_id)
    if assignment is None or project is None or not project.has_repo:
        return 0
    return min(assignment.effective_static, _node_bound(session, node_id))


# ── the slot's kind: git worktree or folder ───────────────────────────────────────────────


def task_has_repo(session: Session, task: Task) -> bool:
    """Whether `task` runs in a git worktree: the kind of the slot it was last leased into (`tasks.has_repo`,
    written by the lease), else — never dispatched — whether its project has a repository. The ONE reader for the
    composer, settle and the task API, so a held or finished task never follows a later flip of the project."""
    if task.has_repo is not None:
        return task.has_repo
    # The identity map holds rows WEAKLY: dropped after the read, the row is collected and every task of a
    # page re-SELECTs its project (8 reads for one epic's GET, ~5% of the api's CPU). So the row
    # is pinned, for the CURRENT TRANSACTION only: sessions here do not expire on commit, so a pin outliving
    # it would serve a later transaction values a fresh read would not. Released BEFORE the read, so a new
    # transaction re-reads exactly as it did before the pin existed.
    txn = session.get_transaction()
    pinned = session.info.get("_pinned_projects")
    if pinned is None or pinned[0] is not txn:
        pinned = session.info["_pinned_projects"] = (txn, {})
    project = session.get(Project, task.project_id)
    if project is None:
        return True
    pinned[1][project.id] = project
    return project.has_repo


def snapshot_task_kind(session: Session, worktree_id: UUID) -> None:
    """Copy a slot's kind onto the task it now holds (`tasks.has_repo`). Called by `slot_state.lease` and
    `slot_state.redispatch` inside the take's own transaction, so a task keeps the kind its node built after
    the project's repository flips. A system fact, not an edit: written only when it differs, and
    `updated_at`/`updated_by_id` are left as they were. Not committed."""
    session.execute(
        update(Task)
        .where(
            Worktree.id == worktree_id,
            Task.id == Worktree.task_id,
            Task.has_repo.is_distinct_from(Worktree.has_repo),
        )
        .values(has_repo=Worktree.has_repo, updated_at=Task.updated_at, updated_by_id=Task.updated_by_id)
        .execution_options(synchronize_session=False)
    )
    # The sessions here keep loaded rows across commits, so a `Task` already in this one is told to re-read it.
    for obj in list(session.identity_map.values()):
        if isinstance(obj, Task):
            session.expire(obj, ["has_repo"])


def _kind_payload(slot_has_repo: bool, project_has_repo: bool) -> dict[str, bool]:
    """An intent's `has_repo` key: spelled whenever the slot or its project has no repository, and absent for a
    repo project's git worktree — which the node reads as true — so those intents stay byte for byte what they
    were."""
    return {} if slot_has_repo and project_has_repo else {"has_repo": bool(slot_has_repo)}


def kind_of_pair(project_has_repo: bool, numbered_kinds: Iterable[bool]) -> bool:
    """The one kind a (node, project) pair's slots are treated as: the kind of its numbered rows while any row
    of the OTHER kind is still there (a held slot finishing, a retiring one awaiting its destroy), else the
    project's. The node reads one `has_repo` per roster entry, so the planner makes no row of the project's new
    kind until the old kind has drained (`mixed_kind_wait`). Slot 0 is never counted: it is the clone, kept
    when the last repository is removed."""
    other = [kind for kind in numbered_kinds if kind != project_has_repo]
    return other[0] if other else project_has_repo


def pair_has_repo(session: Session, node_id: UUID, project: Project) -> bool:
    """`kind_of_pair` for the pair's rows in the database."""
    return kind_of_pair(project.has_repo, session.scalars(
        select(Worktree.has_repo).where(
            Worktree.node_id == node_id, Worktree.project_id == project.id, Worktree.number >= 1,
        )
    ).all())


def mixed_kind_wait(session: Session, node_id: UUID, project: Project) -> Optional[str]:
    """Why no slot of the project's kind may be made on this node yet, or None."""
    if pair_has_repo(session, node_id, project) == project.has_repo:
        return None
    old = "git worktrees" if project.has_repo is False else "folder slots"
    return f"this project's {old} on this node are still in use or being destroyed — a new slot is made once they are gone"


def _occupied(session: Session, node_id: UUID, project_id: int) -> set[int]:
    return set(session.scalars(
        select(Worktree.number).where(
            Worktree.node_id == node_id, Worktree.project_id == project_id, Worktree.number >= 1
        )
    ).all())


def dynamic_headroom(
    session: Session, node_id: UUID, project_id: int, *, occupied: Optional[Iterable[int]] = None
) -> tuple[Optional[int], Optional[str]]:
    """The index a new dynamic worktree would take, or why none can be made: `(index, None)` or
    `(None, reason)`. Bounded by the org's index ceiling alone (`pool_bound`). `occupied` lets a caller
    that already holds the pair's numbers skip the read."""
    project = session.get(Project, project_id)
    bound = _node_bound(session, node_id) if project is not None else 0
    if bound < 1:
        return None, "the port block holds no worktree index — widen the port grid's block size"
    mixed = mixed_kind_wait(session, node_id, project)
    if mixed is not None:
        return None, mixed
    taken = set(occupied) if occupied is not None else _occupied(session, node_id, project_id)
    for number in range(1, bound + 1):
        if number not in taken:
            return number, None
    return None, f"every index 1..{bound} of the port block is in use"


def dynamic_room(session: Session, node_id: UUID, project_id: int) -> int:
    """How many dynamic worktrees the pair could still take right now: free indexes in `1..index_ceiling` —
    the count `dynamic_headroom` answers one index of. 0 for an unassigned pair."""
    if not is_assigned(session, node_id, project_id):
        return 0
    project = session.get(Project, project_id)
    bound = _node_bound(session, node_id) if project is not None else 0
    if project is not None and mixed_kind_wait(session, node_id, project) is not None:
        return 0
    return max(0, bound - len({n for n in _occupied(session, node_id, project_id) if n <= bound}))


def _is_host(session: Session, node_id: Optional[UUID]) -> bool:
    """`vnodes.is_host`; the community edition has no Multi-Node Hosts."""
    if edition.IS_CE:
        return False
    from app import vnodes  # deferred: vnodes pulls the route layer's HTTPException

    return vnodes.is_host(session, node_id)


def add_dynamic(session: Session, node: Node, project: Project, number: int) -> Worktree:
    """Declare a `dynamic` worktree at `number` for a dispatch about to lease it (R6), under the pair lock.
    Raises `ValueError` naming why when the index is taken or no index may be made (`dynamic_headroom`), and
    lets the unique-key `IntegrityError` of a concurrent insert propagate. Flushes, never commits: the caller
    leases the row in the same transaction."""
    lock_pair(session, node.id, project.id)
    bound = pool_bound(session, node.org_id)
    if not 1 <= number <= bound:
        raise ValueError(f"index {number} is outside the port block's 1..{bound}")
    if _is_host(session, node.id):
        raise ValueError(f"node '{node.name}' is a host and runs no worktrees")
    # A folder slot clones nothing, so only a repo project needs a usable repo URL.
    if project.has_repo and forges.repo_ref_or_none(project) is None:
        raise ValueError(f"project '{project.name}' has no usable repo URL, so no worktree can be cloned")
    if number in _occupied(session, node.id, project.id):
        raise ValueError(f"index {number} taken concurrently")
    index, why = dynamic_headroom(session, node.id, project.id)
    if index is None:
        raise ValueError(why)
    clone = resolve_clone(node.jaah_home, project.id)
    if not clone:
        raise ValueError(f"node '{node.name}' has no Jaah home yet, so no worktree path can be named")
    assignment = _assignment(session, node.id, project.id)
    if assignment is None:
        raise ValueError(f"project '{project.name}' is not assigned to node '{node.name}'")
    declarer = _declarer(session, assignment, _locked_rows(session, node.id, project.id), None)
    _add_row(session, node, project, number, clone, WORKTREE_KIND_DYNAMIC, declarer, has_repo=project.has_repo)
    session.flush()
    return session.scalars(select(Worktree).where(
        Worktree.node_id == node.id, Worktree.project_id == project.id, Worktree.number == number
    )).one()


def add_user_folder(
    session: Session, project: Project, *, user_id: Any, org_id: UUID, node: Optional[Node] = None,
) -> Worktree:
    """Declare a dynamic FOLDER slot for one person's console session and hold it `user` for them. A
    project with no repository holds no static slot, so a console makes its own exactly as a dispatch does
    (`add_dynamic`, under the index ceiling and `mixed_kind_wait`), claimed through
    `slot_state.claim_for_user` (the org floor) instead of leased to a task. Its `/exit` frees it
    (`routes/agent/slot_intents.py::user_session_ended`) and the free→destroy sweep (`lease_drain._destroy_free_dynamic`)
    deletes the folder. `node` None takes the first of the project's assigned, enabled nodes in `org_id` with
    room, by name. The claim commits the declared row with its hold, so nothing may follow it expecting
    one transaction. Raises `ValueError` naming why no slot could be made."""
    if project.has_repo:
        raise ValueError(f"project '{project.name}' has a repository — a console opens one of its declared slots")
    if node is not None:
        candidates = [node]
    else:
        node_ids = {a.node_id for a in effective_assignments(session, project_id=project.id)}
        candidates = sorted(
            (n for n in (session.get(Node, node_id) for node_id in node_ids) if n is not None),
            key=lambda n: n.name,
        )
    reasons: list[str] = []
    for candidate in candidates:
        if candidate.org_id != org_id:
            reasons.append(f"node '{candidate.name}' is not in your organization")
            continue
        if not candidate.enabled:
            reasons.append(f"node '{candidate.name}' is disabled")
            continue
        if candidate.microvm:
            # A MicroVM node holds no slot: its console sessions run in their own VM (`routes/console.py`).
            reasons.append(f"node '{candidate.name}' runs sessions in MicroVMs")
            continue
        if not is_assigned(session, candidate.id, project.id):
            reasons.append(f"project '{project.name}' is not assigned to node '{candidate.name}'")
            continue
        index, why = dynamic_headroom(session, candidate.id, project.id)
        if index is None:
            reasons.append(f"node '{candidate.name}': {why}")
            continue
        try:
            row = add_dynamic(session, candidate, project, index)
        except (ValueError, IntegrityError) as exc:
            session.rollback()
            reasons.append(f"node '{candidate.name}': {exc if isinstance(exc, ValueError) else 'index taken concurrently'}")
            continue
        worktree_id = row.id
        if slot_state.claim_for_user(
            session, worktree_id=worktree_id, user_id=user_id, org_id=org_id, reason="console session", folder=True,
        ) is None:
            # Unreachable for a row this transaction declared in the caller's own org; the sweep retires it.
            raise ValueError(f"the new folder slot on node '{candidate.name}' could not be claimed")
        log.info("slot-allocation: node %s — declared folder slot %s#%s for a console session",
                 candidate.name, project.name, index)
        return session.get(Worktree, worktree_id)
    raise ValueError("; ".join(reasons) or f"project '{project.name}' is not assigned to any node")


# ── the onboarding latch (D15) ───────────────────────────────────────────────────────────────────


def onboarding_active(session: Session, org_id: Optional[UUID]) -> bool:
    """Whether the org is still in onboarding: its latch (`orgs.onboarding_completed_at`) is unset."""
    if org_id is None:
        return False
    org = session.get(Org, org_id)
    return org is not None and org.onboarding_completed_at is None


def _assigns(session: Session, org_id: Optional[UUID]) -> bool:
    """Whether `auto_assign` pairs new nodes and projects in `org_id`: during onboarding, and always in the local
    edition, whose one node runs every project (`app/ce_bootstrap.py`)."""
    return edition.IS_CE or onboarding_active(session, org_id)


def auto_assign(session: Session, *, node_id: Optional[UUID] = None, project_id: Optional[int] = None) -> int:
    """During onboarding only (`_assigns`): assign a new node every project of its org, or a new project every enabled
    node of its org (never a Multi-Node Host, which runs no work). No static override: the pair is dynamic.
    Returns the assignments created; 0 once the latch is set. Does not converge or commit."""
    if (node_id is None) == (project_id is None):
        raise ValueError("auto_assign takes exactly one of node_id or project_id")
    if node_id is not None:
        node = session.get(Node, node_id)
        if node is None or not _assigns(session, node.org_id) or _is_host(session, node.id):
            return 0
        pairs = [(node.id, pid, node.org_id) for pid in session.scalars(
            select(Project.id).where(Project.org_id == node.org_id).order_by(Project.id)
        ).all()]
    else:
        project = session.get(Project, project_id)
        if project is None or not _assigns(session, project.org_id):
            return 0
        pairs = [
            (nid, project.id, project.org_id)
            for nid in session.scalars(
                select(Node.id).where(Node.org_id == project.org_id, Node.enabled.is_(True)).order_by(Node.name)
            ).all()
            if not _is_host(session, nid)
        ]
    if not pairs:
        return 0
    inserted = session.execute(
        pg_insert(NodeProject)
        .values([{"node_id": n, "project_id": p, "org_id": o} for n, p, o in pairs])
        .on_conflict_do_nothing(constraint="uq_node_projects_node_project")
        .returning(NodeProject.id)
    ).all()
    return len(inserted)


def live_static_count(session: Session, node_id: UUID, project_id: int) -> int:
    """The pair's numbered static rows not being destroyed — what a Declare adds to and the static count holds."""
    return session.scalar(
        select(func.count()).select_from(Worktree).where(
            Worktree.node_id == node_id, Worktree.project_id == project_id, Worktree.number >= 1,
            Worktree.kind == WORKTREE_KIND_STATIC, Worktree.retiring_at.is_(None),
        )
    ) or 0


def pin_to_declared(session: Session, node_id: UUID, project_id: int, *, actor_id: Optional[UUID] = None) -> None:
    """Keep the pair's assignment in step with a per-slot Declare or Delete, so `converge` neither retires a
    hand-declared slot nor re-declares a hand-deleted one (A10). After a Declare: an explicit assignment
    holding at least the pair's live static rows. After a Delete: an explicit assignment follows the rows
    down, and goes once no row of the pair is left (removing slot 0 is retiring the project from the node); a
    group-only pair is left to its group, which re-declares as it always has. Flushes, never commits."""
    session.flush()
    lock_pair(session, node_id, project_id)
    live = live_static_count(session, node_id, project_id)
    any_row = session.scalar(
        select(func.count()).select_from(Worktree)
        .where(Worktree.node_id == node_id, Worktree.project_id == project_id)
    ) or 0
    assignment = _assignment(session, node_id, project_id)
    row = session.scalar(select(NodeProject).where(
        NodeProject.node_id == node_id, NodeProject.project_id == project_id
    ).with_for_update())
    if not any_row:
        if row is not None:
            session.delete(row)
            session.flush()
        return
    if assignment is not None and assignment.effective_static == live:
        return
    if row is None:
        if assignment is not None and assignment.effective_static > live:
            return  # group-only, and short: the group re-declares, as it always has
        org_id = session.scalar(select(Node.org_id).where(Node.id == node_id))
        actor = {"created_by_id": actor_id, "updated_by_id": actor_id} if actor_id is not None else {}
        session.add(NodeProject(node_id=node_id, project_id=project_id, org_id=org_id, static_slots=live, **actor))
    else:
        row.static_slots = live
        row.updated_at = datetime.now(timezone.utc)
        if actor_id is not None:
            row.updated_by_id = actor_id
    session.flush()


# ── the two ALLOCATION statements (`app/slot_columns.py`) ───────────────────────────────────────────


def _retire_statement(row_id: UUID, at: datetime):
    """Order one row destroyed. Guarded on `retiring_at IS NULL`, so a second retire is a no-op."""
    return (
        update(Worktree)
        .where(Worktree.id == row_id, Worktree.retiring_at.is_(None))
        .values(retiring_at=at)
    )


def _kind_statement(row_id: UUID, kind: str):
    """Change one row's kind — the D12 demote of a held static surplus."""
    return update(Worktree).where(Worktree.id == row_id).values(kind=kind)


# ── destroying ───────────────────────────────────────────────────────────────────────────────────


def retire(session: Session, worktree: Worktree, *, reason: str) -> None:
    """Order `worktree` destroyed: stamp `retiring_at` and mint one `destroy_slot` intent at its node. A hold
    is left intact — the node's destroy ends the runner and its ack releases and requeues
    (`requeue_after_destroy`). A row already retiring mints nothing. Not committed and not pushed: the node's
    pool tick serves a pending intent, so a frame would only save latency."""
    if worktree.retiring_at is not None:
        return
    at = datetime.now(timezone.utc)
    if session.execute(_retire_statement(worktree.id, at)).rowcount == 0:
        return
    _mint_destroy(session, worktree, reason)
    session.refresh(worktree)


def remint_destroy(session: Session, worktree: Worktree, *, reason: str) -> None:
    """Order an already-retiring `worktree` destroyed again, for a destroy the node has not acked: one fresh
    `destroy_slot` intent, superseding any still pending. The caller paces it and never re-mints over a
    recorded refusal (`destroy_error`). Not committed."""
    if worktree.retiring_at is None or worktree.destroy_error is not None:
        return
    _mint_destroy(session, worktree, reason)


def retry_destroy(session: Session, worktree: Worktree, *, reason: str) -> bool:
    """An operator's Retry on a stuck or refused destroy: clear `destroy_error` and order the destroy again.
    False when the row is not retiring (nothing was ordered, so nothing to retry). Not committed."""
    if worktree.retiring_at is None:
        return False
    session.execute(update(Worktree).where(Worktree.id == worktree.id).values(destroy_error=None))
    session.refresh(worktree)
    remint_destroy(session, worktree, reason=reason)
    return True


def cancel_destroy(session: Session, worktree: Worktree) -> int:
    """Retire `worktree`'s still-pending `destroy_slot` orders `superseded`, for a row dropped without the node
    (the row-only DELETE, the console's Forget): served later, one would erase a tree the CP no longer tracks.
    Returns how many went. Not committed."""
    return session.execute(
        update(SlotIntent)
        .where(
            SlotIntent.verb == fleet_verdicts.VERB_DESTROY_SLOT,
            SlotIntent.node_id == worktree.node_id,
            SlotIntent.project_id == worktree.project_id,
            SlotIntent.number == worktree.number,
            fleet_verdicts.still_pending(),
        )
        .values(outcome=fleet_verdicts.OUTCOME_SUPERSEDED, reason="the worktree was forgotten without its node")
        .execution_options(synchronize_session=False)
    ).rowcount


# The newest destroy order's outcomes that carry the node's own words in `slot_intents.reason`: a `failed` ack
# stays pending with its detail, a refusal is re-served, a park is the budget spent on them.
_DESTROY_NODE_ERROR_OUTCOMES = (
    fleet_verdicts.OUTCOME_FAILED,
    fleet_verdicts.OUTCOME_REFUSED_STALE,
    fleet_verdicts.OUTCOME_REFUSED_PRECONDITION,
    fleet_verdicts.OUTCOME_PARKED,
)


def destroy_node_error(session: Session, worktree: Worktree) -> Optional[str]:
    """Why the node has not destroyed a retiring `worktree`, redacted for display; None when it has said
    nothing (`destroy_node_errors` for one slot)."""
    return destroy_node_errors(session, [worktree]).get(worktree.id)


def destroy_node_errors(session: Session, worktrees: Iterable[Worktree]) -> dict[Any, str]:
    """`{worktree id: the node's redacted words}` for those retiring `worktrees` it has said why about: a recorded
    refusal (`destroy_error`) first, else the newest `destroy_slot` intent's reason when that order failed, was
    refused or parked. One read for the whole page (Postgres `DISTINCT ON` keeps each slot's newest order)."""
    retiring = [wt for wt in worktrees if wt.retiring_at is not None]
    details: dict[Any, Optional[str]] = {wt.id: wt.destroy_error for wt in retiring if wt.destroy_error}
    unsaid = {(wt.node_id, wt.project_id, wt.number): wt.id for wt in retiring if wt.id not in details}
    if unsaid:
        slot = (SlotIntent.node_id, SlotIntent.project_id, SlotIntent.number)
        rows = session.execute(
            select(*slot, SlotIntent.outcome, SlotIntent.reason)
            .where(SlotIntent.verb == fleet_verdicts.VERB_DESTROY_SLOT, tuple_(*slot).in_(list(unsaid)))
            .distinct(*slot)
            .order_by(*slot, SlotIntent.created_at.desc(), SlotIntent.id.desc())
        ).all()
        for row in rows:
            if row.outcome in _DESTROY_NODE_ERROR_OUTCOMES:
                details[unsaid[(row.node_id, row.project_id, row.number)]] = row.reason
    redacted = {wt_id: fleet_verdicts.redact_node_detail(detail) for wt_id, detail in details.items()}
    return {wt_id: detail for wt_id, detail in redacted.items() if detail}


def _mint_destroy(session: Session, worktree: Worktree, reason: str) -> None:
    project = session.get(Project, worktree.project_id)
    fleet_verdicts.issue_intents(
        session,
        str(worktree.node_id),
        [fleet_verdicts.build_slot_intent(
            fleet_verdicts.VERB_DESTROY_SLOT,
            worktree.project_id,
            project.name if project is not None else "",
            worktree.number,
            reason=reason,
            payload={
                "task_id": worktree.task_id,
                "purge_volumes": True,
                "compose_project": f"p{worktree.project_id}-{worktree.number}",
                # The SLOT's kind, never the project's: a git worktree whose project lost its repository is still
                # scanned and bundled before it goes (the node refuses a kind its roster disagrees with).
                **_kind_payload(worktree.has_repo, project.has_repo if project is not None else True),
            },
            # The CP's order stands whatever the tree's state: nothing for the node to re-verify.
            precondition_fields=(),
        )],
        commit=False,
    )


def requeue_after_destroy(session: Session, task_id: int, reason: str) -> None:
    """Put a task whose slot was destroyed under it back in the queue: `{active, awaiting_response} → ready`
    (`task_lifecycle.SLOT_DESTROYED`; a `paused` task stays paused), no attempt spent (the killed run's is refunded), its open question rounds dropped (the
    session that asked them is gone) and a `task_note:slot_destroyed` recorded. Raises `ValueError` from any
    other state. Does not commit."""
    edge = task_lifecycle.SLOT_DESTROYED
    moved = session.scalar(
        update(Task)
        .where(Task.id == task_id, Task.state.in_(edge.sources))
        .values(
            state=edge.target,
            # The killed run was charged its attempt at `ready → active` and the relaunch is charged again:
            # refund it, so after the relaunch the count is where it was (D9), as `BALANCE_RESUME` does.
            dispatch_attempts=func.greatest(Task.dispatch_attempts - 1, 0),
            **actors.task_actor_values(session),
        )
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    if moved is None:
        raise ValueError(
            f"task {task_id} cannot be requeued after its slot was destroyed: it is not in "
            f"{sorted(edge.sources)}"
        )
    merge_lane_state.lane_task_left_active(session, task_id)
    runner_terminations.drop_undelivered_questions(session, task_id)
    record_task_note(session, task_id, NOTE_SLOT_DESTROYED, "", {"cause": edge.name, "reason": reason})
    emit_console_event(session, entity="task", id=task_id, action=edge.target)


# ── converging ───────────────────────────────────────────────────────────────────────────────────


@dataclass
class ConvergeOutcome:
    """What one converge did. `tasks_requeued` stays empty here: a held destroy is requeued by the node's
    ack (`requeue_after_destroy`), after its runner is dead."""

    created: int = 0
    destroyed: int = 0
    demoted: int = 0
    tasks_requeued: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)


def _locked_rows(session: Session, node_id: UUID, project_id: int) -> list[Worktree]:
    """The pair's rows, `FOR UPDATE` so a lease cannot take a free row this pass is about to retire."""
    return list(session.scalars(
        select(Worktree)
        .where(Worktree.node_id == node_id, Worktree.project_id == project_id)
        .order_by(Worktree.number)
        .with_for_update()
    ).all())


def _is_held(row: Worktree) -> bool:
    return row.state != slot_state.FREE


def _end_ledger(session: Session, row: Worktree) -> None:
    """Close a held slot's session ledger `abandoned`: Audit > Sessions must stop reading it as live."""
    snap = slot_teardown.snapshot(row, session)
    agent_session_lifecycle.note_end(
        session, agent_session_id=snap.session_id or "", end_reason=agent_sessions.END_REASON_ABANDONED,
    )


def _declarer(
    session: Session, assignment: Assignment, rows: list[Worktree], actor: Any
) -> UUID:
    """Whom a new slot names as its declarer — the person its pool clone authenticates as: the
    acting operator, else the assignment's creator, else the group's, else the pair's earliest declarer."""
    actor_id = getattr(actor, "id", None)
    if actor_id is not None:
        return actor_id
    if assignment.explicit:
        creator = session.scalar(select(NodeProject.created_by_id).where(
            NodeProject.node_id == assignment.node_id, NodeProject.project_id == assignment.project_id
        ))
        if creator is not None and creator != SYSTEM_ACTOR_ID:
            return creator
    if assignment.group_id is not None:
        creator = session.scalar(select(NodeGroup.created_by_id).where(NodeGroup.id == assignment.group_id))
        if creator is not None and creator != SYSTEM_ACTOR_ID:
            return creator
    for row in rows:
        if row.created_by_id is not None and row.created_by_id != SYSTEM_ACTOR_ID:
            return row.created_by_id
    return SYSTEM_ACTOR_ID


def _add_row(session: Session, node: Node, project: Project, number: int, clone: str, kind: str,
             declarer: UUID, *, has_repo: bool = True) -> None:
    """`has_repo` is the slot's kind for life (`Worktree.has_repo`); keyword-only, and true only where a
    caller declares a git worktree."""
    from app.routes.pool_slots import slot_path  # deferred: service -> routes, at call time only

    session.add(Worktree(
        node_id=node.id,
        # The node's org, written explicitly: an unbound cron pass gets no seam stamp, and 0181's composite
        # key makes this the only legal value.
        org_id=node.org_id,
        project_id=project.id,
        number=number,
        path=slot_path(clone, number),
        name=slot_title.default_name(project.name, number),
        kind=kind,
        has_repo=has_repo,
        created_by_id=declarer,
        updated_by_id=declarer,
    ))


def _converge_unassigned(session: Session, rows: list[Worktree], outcome: ConvergeOutcome) -> None:
    """Unassigned: every numbered row is retired, a held one with its hold intact; slot 0 goes through the
    Delete path's row deletion once no numbered row is left."""
    numbered = [row for row in rows if row.number != MAIN_CLONE_NUMBER]
    for row in numbered:
        if row.retiring_at is not None:
            continue
        if _is_held(row):
            _end_ledger(session, row)
        retire(session, row, reason="its project was unassigned from this node")
        outcome.destroyed += 1
    main = next((row for row in rows if row.number == MAIN_CLONE_NUMBER), None)
    if main is not None and not numbered:
        from app.routes.pool_slots import _delete_slot_rows  # deferred: service -> routes

        project = session.get(Project, main.project_id)
        _delete_slot_rows(
            session, str(main.node_id), main.project_id, project.name if project else "", main.number, main.id,
        )


def _converge_assigned(
    session: Session, node: Node, project: Project, assignment: Assignment, rows: list[Worktree],
    actor: Any, outcome: ConvergeOutcome, *, bound: int,
) -> None:
    if not project.has_repo:
        # Its last repository was removed: every git worktree goes now, free or task-held. A task is cut off — the
        # node's destroy ends its runner and the ack requeues it (`requeue_after_destroy`), to start again in a
        # folder slot once the pair has drained (`kind_of_pair`). A `user` hold is a person's console, never cut
        # off: it goes when freed, as a dynamic row does.
        for row in rows:
            if (row.number != MAIN_CLONE_NUMBER and row.has_repo and row.retiring_at is None
                    and row.state != slot_state.USER):
                retire(session, row, reason="its project no longer has a repository")
                outcome.destroyed += 1
    wanted = assignment.effective_static if project.has_repo else 0
    retire_reason = "the static count for this node was lowered"
    if wanted > bound:
        outcome.warn(f"project '{project.name}' asks for {wanted} static worktrees on node '{node.name}' but "
                     f"the port block holds {bound} indexes, so only {bound} can be declared — widen the "
                     "port grid's block size to grow further")
    target = min(wanted, bound)

    live_static = [row for row in rows if row.number != MAIN_CLONE_NUMBER
                   and row.kind == WORKTREE_KIND_STATIC and row.retiring_at is None]
    if len(live_static) > target:
        excess = len(live_static) - target
        free = sorted((r for r in live_static if not _is_held(r)), key=lambda r: -r.number)
        for row in free[:excess]:
            retire(session, row, reason=retire_reason)
            outcome.destroyed += 1
        held = sorted((r for r in live_static if _is_held(r)), key=lambda r: -r.number)
        for row in held[:max(0, excess - len(free))]:
            session.execute(_kind_statement(row.id, WORKTREE_KIND_DYNAMIC))
            outcome.demoted += 1

    if not project.has_repo:
        # Nothing to clone: no main clone is declared and none is warned about. An existing slot 0 is kept.
        session.flush()
        return
    if any(row.number != MAIN_CLONE_NUMBER and not row.has_repo for row in rows):
        # A repository connected while folder slots remain: they go when freed, and the statics come after
        # (`kind_of_pair`).
        session.flush()
        return
    has_main = any(row.number == MAIN_CLONE_NUMBER for row in rows)
    need = target - len(live_static)
    if (has_main and need <= 0) or bound < 1:
        # Nothing missing — or a pool that can hold no worktree, whose main clone would serve nothing.
        return
    clone = resolve_clone(node.jaah_home, project.id)
    if not clone:
        # No Jaah home yet — normal for a node launched a minute ago; a later pass declares.
        return
    if _is_host(session, node.id):
        outcome.warn(f"node '{node.name}' is a host and runs no worktrees — unassign "
                     f"'{project.name}' from it")
        return
    if forges.repo_ref_or_none(project) is None:
        outcome.warn(f"project '{project.name}' has no usable repo URL, so its worktrees cannot be cloned on "
                     f"node '{node.name}' — set the project's Repo URL on its card, or unassign it")
        return
    declarer = _declarer(session, assignment, rows, actor)
    if not has_main:
        _add_row(session, node, project, MAIN_CLONE_NUMBER, clone, WORKTREE_KIND_STATIC, declarer)
    if need <= 0:
        session.flush()
        return
    occupied = {row.number for row in rows if row.number != MAIN_CLONE_NUMBER}
    free_indexes = [n for n in range(1, bound + 1) if n not in occupied]
    take = min(need, len(free_indexes))
    for number in free_indexes[:take]:
        _add_row(session, node, project, number, clone, WORKTREE_KIND_STATIC, declarer)
    session.flush()
    outcome.created += take
    if take < need:
        outcome.warn(f"project '{project.name}' holds {len(live_static) + take} of {target} static worktrees "
                     f"on node '{node.name}': every index 1..{bound} of the port block is in use")


def nodes_in_scope(session: Session) -> list[UUID]:
    """Every node holding an assignment (explicit or through its group) or a worktree row: what the sweep
    converges, one node per transaction."""
    explicit = select(NodeProject.node_id)
    grouped = (select(Node.id).join(NodeGroupCapacity, NodeGroupCapacity.id == Node.node_group_capacity_id)
               .join(NodeGroupProject, NodeGroupProject.group_id == NodeGroupCapacity.group_id))
    holding = select(Worktree.node_id)
    return sorted(set(session.scalars(explicit.union(grouped, holding)).all()), key=str)


def converge(
    session: Session, *, node_id: Optional[UUID] = None, project_id: Optional[int] = None, actor: Any = None,
) -> ConvergeOutcome:
    """Make the rows of every (node, project) pair in scope match the rules (module docstring). Scope is every
    pair that is assigned OR holds a row. `actor` is the operator whose save caused this, named as a new slot's
    declarer. Flushes, never commits."""
    outcome = ConvergeOutcome()
    assignments = {(a.node_id, a.project_id)
                   for a in effective_assignments(session, node_id=node_id, project_id=project_id)}
    held_q = select(Worktree.node_id, Worktree.project_id).distinct()
    if node_id is not None:
        held_q = held_q.where(Worktree.node_id == node_id)
    if project_id is not None:
        held_q = held_q.where(Worktree.project_id == project_id)
    pairs = sorted(set(assignments) | set(session.execute(held_q).tuples().all()),
                   key=lambda k: (str(k[0]), k[1]))

    # The index ceiling is the org's, read once per org per pass.
    bounds: dict[Optional[UUID], int] = {}
    for pair_node, pair_project in pairs:
        node = session.get(Node, pair_node)
        project = session.get(Project, pair_project)
        if node is None or project is None:  # pragma: no cover - FKs make this unreachable
            continue
        if node.org_id not in bounds:
            bounds[node.org_id] = pool_bound(session, node.org_id)
        # Locked, then re-read: the snapshot above only chose the pairs, and a Declare or Delete that
        # committed since must be what this pass acts on.
        lock_pair(session, node.id, project.id)
        rows = _locked_rows(session, node.id, project.id)
        assignment = _assignment(session, node.id, project.id)
        if assignment is None:
            _converge_unassigned(session, rows, outcome)
        else:
            _converge_assigned(session, node, project, assignment, rows, actor, outcome, bound=bounds[node.org_id])
    session.flush()
    if outcome.created or outcome.destroyed or outcome.demoted:
        log.info("slot allocation: created %d, destroyed %d, demoted %d (node %s, project %s)",
                 outcome.created, outcome.destroyed, outcome.demoted, node_id, project_id)
    return outcome
