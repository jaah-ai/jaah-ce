"""Which worktrees exist on which node, and how many — and the two node sets every placement reader asks.

Where a project may run is `app/placement_groups.py`'s one rule (`allocated_node_ids`: its node groups, else All).
This module serves it as two sets, deliberately different:
- `placement_node_ids` — where a NEW task of the project may start: the allocated nodes only;
- `hosting_node_ids` — where a node may still act for the project (secrets, memory, clone, scans): the allocated
  nodes plus every node holding a HELD row of the project not being destroyed, so a task still running on a node its
  project left keeps what it needs until it finishes (D16), while no new task is placed there.

Every worktree is `dynamic`: made on demand for a dispatch (`add_dynamic`) and destroyed once freed
(`lease_drain`'s free sweep, which retires ANY free row whatever its `kind`, so a `static` row an older release
re-declares is retired too). Rows share one unbounded index space per (node, project), lowest free index first;
the node picks each worktree's ports itself. A row being destroyed (`retiring_at`) still occupies its index until
the node acknowledges the destroy and the row goes.

A project with no repository (`Project.has_repo` false) gets a dynamic **folder** slot per task, a row whose own
`has_repo` is false. So does a task in a project WITH a repository whose starter chose to run it without the
repository (`task_wants_folder`). A slot's kind is set at declare and never changes. A node that advertises
`FOLDER_SLOT` reads each slot's own kind and holds both kinds per (node, project) pair; on any other node a pair
holds one kind (`pair_has_repo`), and the old kind's rows drain before a row of the new kind is made.

`converge` makes rows match the rules: it retires FREE rows of a pair whose node the project may no longer start on,
and every git worktree of a project that lost its repository. It never ends a held row's run for a placement
change. Safe to call repeatedly; the caller commits.

Two writers of `worktrees` columns live here, each a declared statement family
(`app/slot_columns.py::ALLOCATION`); the declare INSERT names the `DECLARATION` actor like every declare.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Iterator, Optional
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.orm import Session

from app import (
    actors,
    edition,
    edition_features,
    fleet_verdicts,
    forges,
    merge_lane_state,
    placement_groups,
    runner_terminations,
    slot_state,
    slot_title,
    task_lifecycle,
)
from app.clock import utcnow
from app.events import emit_console_event
from app.entity_shape import SYSTEM_ACTOR_ID
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import WORKTREE_KIND_DYNAMIC, SlotIntent, Worktree
from app.node_clone import slot_path
from app.user_activity import record_task_note

log = logging.getLogger(__name__)

# The task-note action `requeue_after_destroy` writes (`task_note:slot_destroyed`).
NOTE_SLOT_DESTROYED = "slot_destroyed"


# ── the two node sets ────────────────────────────────────────────────────────────────────────────

# `session.info` keys of an armed memo: `{project id: frozenset of node ids}`. Two keys, never shared: the
# placement set must never be answered from the hosting memo.
_HOSTING_MEMO = "slot_allocation.hosting_memo"
_PLACEMENT_MEMO = "slot_allocation.placement_memo"


@contextmanager
def _memo(session: Session, key: str, project_ids: Iterable[int], fill) -> Iterator[None]:
    armed_here = key not in session.info
    if armed_here:
        session.info[key] = {}
    memo = session.info[key]
    try:
        missing = [p for p in project_ids if int(p) not in memo]
        if missing:
            for project_id, nodes in fill(session, missing).items():
                memo[project_id] = frozenset(nodes)
        yield
    finally:
        if armed_here:
            session.info.pop(key, None)


def _served(session: Session, key: str, project_id: int, read) -> set[UUID]:
    memo = session.info.get(key)
    if memo is not None and int(project_id) in memo:
        return set(memo[int(project_id)])
    found = read(session, [int(project_id)])[int(project_id)]
    if memo is not None:
        memo[int(project_id)] = frozenset(found)
    return set(found)


@contextmanager
def hosting_memo(session: Session, project_ids: Iterable[int] = ()) -> Iterator[None]:
    """Answer `hosting_node_ids` on `session` from one read per project for the block, filled up front for
    `project_ids` in one batched read. For a lease-drain pass. Only for a block whose hosting cannot move under it,
    or need not be seen moving: its answers are as old as the block. Nested, the outer memo serves."""
    with _memo(session, _HOSTING_MEMO, project_ids, hosting_node_ids_by_project):
        yield


@contextmanager
def placement_memo(
    session: Session, project_ids: Iterable[int] = (), *, known: Optional[dict[int, Iterable[Any]]] = None,
) -> Iterator[None]:
    """`hosting_memo`'s twin for `placement_node_ids`, under its own key. `known` seeds it with answers the caller
    already read this pass (node ids as uuids or their strings), so a node's plan does not read them again."""
    def fill(session: Session, ids: list[int]) -> dict[int, set[UUID]]:
        given = known or {}
        out = {int(p): {UUID(str(n)) for n in given[int(p)]} for p in ids if int(p) in given}
        out.update(placement_node_ids_by_project(session, [p for p in ids if int(p) not in out]))
        return out

    with _memo(session, _PLACEMENT_MEMO, project_ids, fill):
        yield


def placement_node_ids(session: Session, project_id: int) -> set[UUID]:
    """Where a NEW task of `project_id` may start: `placement_groups.allocated_node_ids`. Served from an armed
    `placement_memo`; always a fresh set the caller may change."""
    return _served(session, _PLACEMENT_MEMO, project_id, placement_node_ids_by_project)


def placement_node_ids_by_project(session: Session, project_ids: Iterable[int]) -> dict[int, set[UUID]]:
    """`placement_node_ids` for many projects in one batched read; every id is a key."""
    return placement_groups.allocated_node_ids_by_project(session, project_ids)


def hosting_node_ids(session: Session, project_id: int) -> set[UUID]:
    """Every node that may act for `project_id`: an allocated node, or one holding a held row of the project not
    being destroyed (a task still running where its project no longer starts work). Served from an armed `hosting_memo`;
    always a fresh set the caller may change."""
    return _served(session, _HOSTING_MEMO, project_id, hosting_node_ids_by_project)


def hosting_node_ids_by_project(session: Session, project_ids: Iterable[int]) -> dict[int, set[UUID]]:
    """`hosting_node_ids` for many projects in one Worktree read and one placement read: every id in
    `project_ids` is a key, an unhosted one mapping to an empty set."""
    wanted = sorted({int(p) for p in project_ids})
    placed = session.info.get(_PLACEMENT_MEMO)
    if placed is not None and all(p in placed for p in wanted):
        out = {p: set(placed[p]) for p in wanted}  # the pass's one placement read, not a second one
    else:
        out = placement_groups.allocated_node_ids_by_project(session, wanted)
    if not wanted:
        return out
    # Only HELD rows (a task running there), on a node of the project's own org: a row elsewhere (none is ever made,
    # `add_dynamic` places by the org's own nodes) must never open the project's secrets or memory to another org's
    # node, and a free row needs no reach (it is destroyed, never leased again).
    held: dict[int, set[UUID]] = {}
    for node_id, project_id in session.execute(
        select(Worktree.node_id, Worktree.project_id)
        .join(Project, Project.id == Worktree.project_id)
        .join(Node, Node.id == Worktree.node_id)
        .where(Worktree.project_id.in_(wanted), Worktree.retiring_at.is_(None), Worktree.state != slot_state.FREE,
               Node.org_id == Project.org_id)
        .distinct()
    ).all():
        held.setdefault(project_id, set()).add(node_id)
    if held and edition_features.enabled("multi_node_hosts"):
        from app import tenant_defaults  # a co-located node on another org's host never acts for the project

        barred = tenant_defaults.foreign_hosted_among(session, set().union(*held.values()))
        held = {p: nodes - barred for p, nodes in held.items()}
    for project_id, nodes in held.items():
        out[project_id] |= nodes
    return out


def lock_pair(session: Session, node_id: UUID, project_id: int) -> None:
    """Serialise every writer of one (node, project) pair's rows — `converge`, `add_dynamic`, a group write's
    converge — for the rest of the transaction, so a lease racing a group change cannot land on a node after the
    change returns. Whoever takes it second re-reads after it. A route takes it BEFORE its own insert or row lock.
    Re-entrant within a transaction."""
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("slot_allocation"), func.hashtext(f"{node_id}:{project_id}")))
    )


# ── the slot's kind: git worktree or folder ───────────────────────────────────────────────

#: The `Node.launch_kinds` entry a node reports when it reads a roster slot's own `has_repo`
#: (`apps/jaahd/reconcile/tick_wire.py::FOLDER_SLOT`), so one (node, project) pair may hold git worktrees and
#: folder slots at once. A node without it reads one kind per pair.
FOLDER_SLOT = "folder_slot"


def node_mixes_kinds(session: Session, node_id: UUID) -> bool:
    """Whether the node advertises `FOLDER_SLOT`, so a pair there may hold both kinds."""
    kinds = session.scalar(select(Node.launch_kinds).where(Node.id == node_id))
    return FOLDER_SLOT in (kinds or ())


def folder_slot_refusal(session: Session, project_id: int) -> str:
    """Why no node of `project_id` can run one of its tasks without the repository, or "" when one can: a folder
    slot beside the project's git worktrees needs a node that mixes the kinds (`node_mixes_kinds`)."""
    placed = placement_node_ids(session, project_id)
    if placed and any(FOLDER_SLOT in (kinds or ()) for kinds in session.scalars(
            select(Node.launch_kinds).where(Node.id.in_(sorted(placed, key=str))))):
        return ""
    return ("None of this project's nodes can run a task without the repository: update them "
            "(a MicroVM node never can).")


def runs_without_repo(session: Session, task: Task) -> bool:
    """Whether `task`'s starter chose to run it without its project's repository: its own choice
    (`tasks.run_without_repo`), or the choice its epic's Start made for the whole group (one hop, as a child's
    starter is the epic's: `task_ancestry.starter_user_id`)."""
    if task.run_without_repo:
        return True
    return task.parent_task_id is not None and bool(
        session.scalar(select(Task.run_without_repo).where(Task.id == task.parent_task_id)))


def task_wants_folder(session: Session, task: Task, project: Project) -> bool:
    """Whether `task` runs in a folder slot although its project has a repository (`runs_without_repo`)."""
    return bool(project.has_repo) and runs_without_repo(session, task)


def task_has_repo(session: Session, task: Task) -> bool:
    """Whether `task` runs in a git worktree: the kind of the slot it was last leased into (`tasks.has_repo`,
    written by the lease), else — never dispatched — whether its project has a repository. The ONE reader for the
    composer, settle and the task API, so a held or finished task never follows a later flip of the project."""
    if task.has_repo is not None:
        return task.has_repo
    pins = _project_pins(session)
    project = session.get(Project, task.project_id)
    if project is None:
        return True
    pins[project.id] = project
    return project.has_repo


def pin_projects(session: Session, tasks: Iterable[Task]) -> None:
    """Load, in ONE read, the projects `task_has_repo` would read one by one for `tasks` (those with no
    `has_repo` snapshot), and pin them as it does — a page of tasks over N projects reads them once, not N times."""
    pins = _project_pins(session)
    wanted = {t.project_id for t in tasks if t.has_repo is None and t.project_id is not None} - pins.keys()
    if wanted:
        pins.update((p.id, p) for p in session.scalars(select(Project).where(Project.id.in_(sorted(wanted)))))


def _project_pins(session: Session) -> dict[int, Project]:
    """This transaction's pinned projects, by id.

    The identity map holds rows WEAKLY: dropped after the read, the row is collected and every task of a
    page re-SELECTs its project (8 reads for one epic's GET, ~5% of the api's CPU). So the row
    is pinned, for the CURRENT TRANSACTION only: sessions here do not expire on commit, so a pin outliving
    it would serve a later transaction values a fresh read would not. Released BEFORE the read, so a new
    transaction re-reads exactly as it did before the pin existed."""
    txn = session.get_transaction()
    pinned = session.info.get("_pinned_projects")
    if pinned is None or pinned[0] is not txn:
        pinned = session.info["_pinned_projects"] = (txn, {})
    return pinned[1]


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
    project's. A node that does not advertise `FOLDER_SLOT` reads one `has_repo` per roster entry, so the planner
    makes no row of the project's new kind there until the old kind has drained (`mixed_kind_wait`)."""
    other = [kind for kind in numbered_kinds if kind != project_has_repo]
    return other[0] if other else project_has_repo


def pair_has_repo(session: Session, node_id: UUID, project: Project) -> bool:
    """`kind_of_pair` for the pair's rows in the database."""
    return kind_of_pair(project.has_repo, session.scalars(
        select(Worktree.has_repo).where(
            Worktree.node_id == node_id, Worktree.project_id == project.id,
        )
    ).all())


def mixed_kind_wait(
    session: Session, node_id: UUID, project: Project, has_repo: Optional[bool] = None
) -> Optional[str]:
    """Why no slot of the wanted kind (`has_repo`, default the project's) may be made on this node yet, or None.
    A repo project on a node that mixes kinds never waits; a folder slot of a repo project waits on any other node."""
    if project.has_repo:
        if node_mixes_kinds(session, node_id):
            return None
        if has_repo is False:
            node = session.execute(select(Node.name, Node.microvm).where(Node.id == node_id)).first()
            if node is not None and node.microvm:
                return f"node {node.name} runs tasks in MicroVMs, which can't run a task without the repository"
            name = node.name if node is not None else node_id
            return f"node {name} needs a Jaah update to run a task without the repository"
    if pair_has_repo(session, node_id, project) == project.has_repo:
        return None
    old = "git worktrees" if project.has_repo is False else "folder slots"
    return f"this project's {old} on this node are still in use or being destroyed — a new slot is made once they are gone"


def _occupied(session: Session, node_id: UUID, project_id: int) -> set[int]:
    return set(session.scalars(
        select(Worktree.number).where(
            Worktree.node_id == node_id, Worktree.project_id == project_id
        )
    ).all())


#: `dynamic_room` of a pair that may make worktrees: no index ceiling binds, so the room is any count above what
#: one dispatch pass can place. A number, not infinity, because the room is summed and served as `headroom`.
UNBOUNDED_ROOM = 1000


def _free_indexes(taken: set[int], count: int) -> list[int]:
    """The `count` lowest indexes from 1 up that `taken` does not hold."""
    free: list[int] = []
    number = 1
    while len(free) < count:
        if number not in taken:
            free.append(number)
        number += 1
    return free


def dynamic_headroom(
    session: Session, node_id: UUID, project_id: int, *, occupied: Optional[Iterable[int]] = None,
    has_repo: Optional[bool] = None,
) -> tuple[Optional[int], Optional[str]]:
    """The index a new dynamic worktree of kind `has_repo` (default the project's) would take, or why none can be
    made: `(index, None)` or `(None, reason)`. The lowest free index; no ceiling binds. `occupied` lets a caller
    that already holds the pair's numbers skip the read."""
    project = session.get(Project, project_id)
    if project is None:
        return None, "the project is gone"
    mixed = mixed_kind_wait(session, node_id, project, has_repo)
    if mixed is not None:
        return None, mixed
    taken = set(occupied) if occupied is not None else _occupied(session, node_id, project_id)
    return _free_indexes(taken, 1)[0], None


def dynamic_room(session: Session, node_id: UUID, project_id: int, *, has_repo: Optional[bool] = None) -> int:
    """How many dynamic worktrees of kind `has_repo` (default the project's) the pair could still take right now:
    `UNBOUNDED_ROOM` when the project may start work on the node and `dynamic_headroom` would answer an index,
    else 0."""
    if node_id not in placement_node_ids(session, project_id):
        return 0
    project = session.get(Project, project_id)
    if project is None or mixed_kind_wait(session, node_id, project, has_repo) is not None:
        return 0
    return UNBOUNDED_ROOM


def dynamic_room_by_pair(
    session: Session, pairs: Iterable[tuple[int, UUID]],
) -> dict[tuple[int, UUID], int]:
    """`dynamic_room` (default kind) for many (project, node) pairs: the placement set read once per project, each
    node's `launch_kinds` once, each pair's slot kinds once. For the dispatch pass's room roster."""
    wanted = sorted(set(pairs), key=lambda k: (k[0], str(k[1])))
    out: dict[tuple[int, UUID], int] = dict.fromkeys(wanted, 0)
    if not wanted:
        return out
    project_ids = sorted({p for p, _n in wanted})
    node_ids = sorted({n for _p, n in wanted}, key=str)
    placed = placement_node_ids_by_project(session, project_ids)
    projects = {p.id: p for p in session.scalars(select(Project).where(Project.id.in_(project_ids)))}
    mixes = {nid: FOLDER_SLOT in (kinds or ()) for nid, kinds in session.execute(
        select(Node.id, Node.launch_kinds).where(Node.id.in_(node_ids))
    ).all()}
    kinds: dict[tuple[int, UUID], list[bool]] = {}
    for project_id, node_id, has_repo in session.execute(
        select(Worktree.project_id, Worktree.node_id, Worktree.has_repo).where(
            Worktree.project_id.in_(project_ids), Worktree.node_id.in_(node_ids)
        )
    ).all():
        kinds.setdefault((project_id, node_id), []).append(has_repo)
    for project_id, node_id in wanted:
        project = projects.get(project_id)
        if project is None or node_id not in placed.get(project_id, ()):
            continue
        if not (project.has_repo and mixes.get(node_id)) and \
                kind_of_pair(project.has_repo, kinds.get((project_id, node_id), ())) != project.has_repo:
            continue  # `mixed_kind_wait`'s drain of the old kind
        out[(project_id, node_id)] = UNBOUNDED_ROOM
    return out


def is_host(session: Session, node_id: Optional[UUID]) -> bool:
    """`vnodes.is_host`; the community edition has no Multi-Node Hosts."""
    if edition.IS_CE:
        return False
    from app import vnodes  # deferred: vnodes pulls the route layer's HTTPException

    return vnodes.is_host(session, node_id)


def add_dynamic(
    session: Session, node: Node, project: Project, number: int, *, task_id: int,
    has_repo: Optional[bool] = None,
) -> Worktree:
    """Declare a `dynamic` worktree at `number` for the dispatch of task `task_id` about to lease it (R6), under the
    pair lock. Its kind is `has_repo`, default the project's (a folder slot of a repo project: `task_wants_folder`).
    The project's placement is re-checked INSIDE the lock, so a lease racing a group change cannot land on a node
    the change took away. Its declarer — whom its pool clone authenticates as — is the task's git person
    (`_declarer`). Raises `ValueError` naming why when the index is taken or no index may be made
    (`dynamic_headroom`), and lets the unique-key `IntegrityError` of a concurrent insert propagate. Flushes, never
    commits: the caller leases the row in the same transaction."""
    lock_pair(session, node.id, project.id)
    if number < 1:
        raise ValueError(f"index {number} is below 1")
    if is_host(session, node.id):
        raise ValueError(f"node '{node.name}' is a host and runs no worktrees")
    if not placement_groups.is_allocated_node(session, node.id, project.id):
        raise ValueError(f"project '{project.name}' does not run on node '{node.name}'")
    kind = project.has_repo if has_repo is None else has_repo
    # A folder slot clones nothing, so only a git worktree needs a usable repo URL.
    if kind and forges.repo_ref_or_none(project) is None:
        raise ValueError(f"project '{project.name}' has no usable repo URL, so no worktree can be cloned")
    if number in _occupied(session, node.id, project.id):
        raise ValueError(f"index {number} taken concurrently")
    index, why = dynamic_headroom(session, node.id, project.id, has_repo=kind)
    if index is None:
        raise ValueError(why)
    if not slot_path(node.jaah_home, project.id, number):
        raise ValueError(f"node '{node.name}' has no Jaah home yet, so no worktree path can be named")
    _add_row(session, node, project, number, WORKTREE_KIND_DYNAMIC, _declarer(session, task_id), has_repo=kind)
    session.flush()
    return session.scalars(select(Worktree).where(
        Worktree.node_id == node.id, Worktree.project_id == project.id, Worktree.number == number
    )).one()


# ── the two ALLOCATION statements (`app/slot_columns.py`) ───────────────────────────────────────────


def _retire_statement(row_id: UUID, at: datetime):
    """Order one row destroyed. Guarded on `retiring_at IS NULL`, so a second retire is a no-op."""
    return (
        update(Worktree)
        .where(Worktree.id == row_id, Worktree.retiring_at.is_(None))
        .values(retiring_at=at)
    )


# ── destroying ───────────────────────────────────────────────────────────────────────────────────


def retire(session: Session, worktree: Worktree, *, reason: str) -> None:
    """Order `worktree` destroyed: stamp `retiring_at` and mint one `destroy_slot` intent at its node. A hold
    is left intact — the node's destroy ends the runner and its ack releases and requeues
    (`requeue_after_destroy`). A row already retiring mints nothing. Not committed and not pushed: the node's
    pool tick serves a pending intent, so a frame would only save latency."""
    if worktree.retiring_at is not None:
        return
    at = utcnow()
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
    ack (`requeue_after_destroy`), after its runner is dead. `created` and `demoted` stay 0 since static worktrees
    went; kept so the response shape holds."""

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


def _declarer(session: Session, task_id: Optional[int]) -> UUID:
    """Whom a new dynamic slot names as its declarer — the person its pool clone authenticates as: the task's git
    person, resolved exactly as its push is (`git_access.git_person_for_task`: its starter, else an
    automation-filed task's project Run jobs as), else the system actor, whose clone is refused with a readable
    sentence (`git_access.resolve_for_worktree_clone`). Never a person who did not start the work."""
    from app import git_access  # deferred: git_access reads this module

    task = session.get(Task, task_id) if task_id is not None else None
    person = git_access.git_person_for_task(session, task) if task is not None else None
    return person if person is not None else SYSTEM_ACTOR_ID


def _add_row(session: Session, node: Node, project: Project, number: int, kind: str,
             declarer: UUID, *, has_repo: bool = True) -> None:
    """`has_repo` is the slot's kind for life (`Worktree.has_repo`); keyword-only, and true only where a
    caller declares a git worktree."""
    session.add(Worktree(
        node_id=node.id,
        # The node's org, written explicitly: an unbound cron pass gets no seam stamp, and 0181's composite
        # key makes this the only legal value.
        org_id=node.org_id,
        project_id=project.id,
        number=number,
        path=slot_path(node.jaah_home, project.id, number),
        name=slot_title.default_name(project.name, number),
        kind=kind,
        has_repo=has_repo,
        created_by_id=declarer,
        updated_by_id=declarer,
    ))


def _converge_pair(
    session: Session, project: Project, rows: list[Worktree], placed: bool, outcome: ConvergeOutcome,
) -> None:
    if not project.has_repo:
        # Its last repository was removed: every git worktree goes now, free or task-held. A task is cut off — the
        # node's destroy ends its runner and the ack requeues it (`requeue_after_destroy`), to start again in a
        # folder slot once the pair has drained (`kind_of_pair`). Not a placement change: kept as it was.
        for row in rows:
            if row.has_repo and row.retiring_at is None:
                retire(session, row, reason="its project no longer has a repository")
                outcome.destroyed += 1
    if placed:
        return
    # The project no longer starts work on this node: its FREE rows go; a held row keeps its task, which finishes
    # (D16), and the free sweep retires the row once freed. Never a ledger ended, never a held row retired.
    for row in rows:
        if row.retiring_at is None and not _is_held(row):
            retire(session, row, reason="its project no longer runs on this node")
            outcome.destroyed += 1


def nodes_in_scope(session: Session) -> list[UUID]:
    """Every node holding a worktree row: what the sweep converges, one node per transaction."""
    return sorted(set(session.scalars(select(Worktree.node_id).distinct()).all()), key=str)


def converge(
    session: Session, *, node_id: Optional[UUID] = None, project_id: Optional[int] = None,
    org_id: Optional[UUID] = None, actor: Any = None,
) -> ConvergeOutcome:
    """Make the rows of every (node, project) pair in scope match the rules (module docstring). Scope is every pair
    holding a row, narrowed to a node, a project and/or an org (`org_id`: every pair of the org, after a plan or
    group change). `actor` is accepted for callers' symmetry and unused: nothing is declared ahead of a task.
    Flushes, never commits."""
    del actor
    outcome = ConvergeOutcome()
    held_q = select(Worktree.node_id, Worktree.project_id).distinct()
    if node_id is not None:
        held_q = held_q.where(Worktree.node_id == node_id)
    if project_id is not None:
        held_q = held_q.where(Worktree.project_id == project_id)
    if org_id is not None:
        held_q = held_q.where(Worktree.org_id == org_id)
    pairs = sorted(set(session.execute(held_q).tuples().all()), key=lambda k: (str(k[0]), k[1]))
    placed = placement_node_ids_by_project(session, {p for _n, p in pairs})

    for pair_node, pair_project in pairs:
        project = session.get(Project, pair_project)
        if project is None:  # pragma: no cover - FKs make this unreachable
            continue
        # Locked, then the rows re-read: the snapshot above only chose the pairs, and a lease that committed since
        # must be what this pass acts on. A lease racing the other way is refused by `add_dynamic`'s own in-lock
        # placement check; and a free row retired here on a stale "not placed" is one the free sweep retires anyway.
        lock_pair(session, pair_node, project.id)
        rows = _locked_rows(session, pair_node, project.id)
        _converge_pair(session, project, rows, pair_node in placed.get(project.id, ()), outcome)
    session.flush()
    if outcome.destroyed:
        log.info("slot allocation: destroyed %d (node %s, project %s, org %s)",
                 outcome.destroyed, node_id, project_id, org_id)
    return outcome
