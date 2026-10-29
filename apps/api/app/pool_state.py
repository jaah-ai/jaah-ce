"""The pool-state invariant, derived from SQL: may this node's occupancy be trusted, and what it is.

Hazard: `count_active([]) == 0` and an empty in-flight set look like an idle fleet, so an empty read
would let the plan dispatch onto slots it cannot see (stale rows fail safe, into under-dispatch).

The discriminator is freshness, not row presence: a just-enrolled project normally has zero rows, and
a fresh complete observation reporting none is a positive statement. The stamp is
`nodes.worktrees_updated_at`, advanced only on a complete enumeration
(`app/fleet_observe.py::node_stamp_statement`), never `observed_at` (liveness). Required tests: zero
rows on an observing node → ok; incomplete observation → not ok.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from app import node_host_metrics, plan_terms, slot_allocation
from app.operator_settings import reads as settings_reads
from app.models.nodes import Node
from app.models.orgs import ORG_ACTIVE, Org
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import VnodeHost, Worktree
from app.node_identity import is_uuid_shaped, node_matches, node_ref

log = logging.getLogger("pool-state")

# The default (`fleet_max_observation_age_secs`, `app/operator_settings/`) is four observation
# passes of the fleet's slowest node (a 30-slot node completes an enumeration every ~33s), not four
# idle ticks; a smaller bound made that node drop out of `live_nodes`/`pool_state` intermittently.
# Widening costs: a dead node's occupancy is trusted that much longer (bounded by its slot count;
# liveness is a separate 30s presence clock). Do not widen it to hide a clock bug. No env var is set
# in either deploy, so the catalogue default is what runs.


def max_observation_age_secs() -> float:
    """The `fleet_max_observation_age_secs` operator override, else `FLEET_MAX_OBSERVATION_AGE_SECS`,
    else the catalogue default: how stale a node's last observation may be before its reported
    occupancy stops being trusted."""
    return settings_reads.get_float("fleet_max_observation_age_secs")


def pool_records(session, node_id: str) -> list[dict[str, Any]]:
    """One node's pool slots as `fleet_decide` record dicts. `node_id` is the node uuid.

    Beside `pool_state` because `active` (counted off these) and `pool_state_ok` are one answer;
    callers must pass the same `node_id` to both. Node-scoped: an unscoped read would count another
    node's occupancy as this node's.

    `holder` is `Worktree.state` read directly, except the number-0 main clone reports "main" (never
    leased; a display label, dropped downstream by `roster_from_records`). `task_id` is the DB fact
    `fleet_decide.select_slot` binds a dispatch to its prior slot by. `session` is normalized to bool.
    `_project_id` is the identity the in-memory `fleet_decide` matchers compare on; `_project` is the
    display name.

    Raises whatever the DB raises; `[]` means "no pool rows" (a legitimate state), never a failure
    sentinel. Callers must keep an exception away from `fleet_decide.select_slot`."""
    rows = session.execute(
        select(
            Worktree.project_id,
            Project.name.label("project"),
            Worktree.number,
            Worktree.branch,
            Worktree.session,
            Worktree.state,
            Worktree.task_id,
            Worktree.org_id,
            Worktree.retiring_at,
            Worktree.kind,
            Worktree.has_repo,
            Worktree.stack_up,
            Task.state.label("task_state"),
        )
        .join(Project, Project.id == Worktree.project_id)
        .outerjoin(Task, Task.id == Worktree.task_id)
        # `node_ref` resolves a non-uuid or unknown uuid to NULL, which matches nothing: a fail-closed empty read.
        .where(Worktree.node_id == node_ref(node_id))
        .order_by(Project.name, Worktree.number)
    ).all()
    return [
        {
            # `_project_id`/`_number` are the keys `roster_from_records`/`held_slots_from_records` read;
            # renaming them would silently empty every roster. The inner join drops nothing (NOT NULL FK).
            "_project_id": row.project_id,
            "_project": row.project,
            "_number": row.number,
            "branch": row.branch,
            # "main" display override for the number-0 row (see the docstring).
            "holder": "main" if row.number == 0 else row.state,
            "task_id": row.task_id,
            # The slot's tenant, so the picker sees the column `slot_state.lease` binds on; otherwise
            # it would hand over a slot the lease can never take and decline forever. NOT NULL now, but
            # the consumer's None arm (leasable by nobody, fail-closed) stays (`routes/fleet.py`).
            "_org_id": row.org_id,
            # Being destroyed (`slot_allocation.retire`): its index stays occupied, but nothing may take it.
            "_retiring": row.retiring_at is not None,
            # `static` or `dynamic`: a free dynamic row is destroyed, never picked again.
            "_kind": row.kind,
            # Git worktree or folder (`Worktree.has_repo`): a free slot of the other kind than its project's is
            # never picked (`routes/fleet.py::build_dispatch_plan`).
            "_has_repo": row.has_repo,
            "session": row.session is True,
            # The compose stack observed up: the merge lane prefers such a slot (`fleet_decide.select_slot`).
            "stack_up": row.stack_up is True,
            # The holding task's state: a parked task with no live runner holds its tree but no runner place
            # (`fleet_decide.is_active_record`).
            "_task_state": row.task_state,
        }
        for row in rows
    ]


def pool_state(
    session, node_id: str, *, max_age_secs: Optional[float] = None
) -> tuple[bool, str]:
    """`(pool_state_ok, pool_state_detail)` for one node's pool. `node_id` is the node uuid.

    Ok iff the node has a complete slot enumeration inside the age bound (`worktrees_updated_at`, see
    the module docstring). The two failure texts stay distinct: "no complete enumeration" also covers a
    node posting `worktrees_complete=false` every pass (a node-side sensing fault, not a cold start).
    Dispatch is refused until the first complete observation: fail-closed, as the spec accepts.
    """
    age = max_observation_age_secs() if max_age_secs is None else max_age_secs
    # Filtered on the uuid; the detail strings name the node by its display name, which an operator can
    # act on (a uuid alone would name nothing to them), falling back to the uuid for an unknown row.
    row = session.execute(
        select(Node.name, Node.worktrees_updated_at, func.now().label("now")).where(node_matches(node_id))
    ).first()
    label = row.name if row is not None and row.name else node_id
    if row is None or row.worktrees_updated_at is None:
        return False, (
            f"node {label} has posted no complete slot enumeration: an empty read is indistinguishable "
            "from an idle fleet, so its occupancy is not trusted (dispatching on it would over-dispatch)"
        )
    stale_by = (row.now - row.worktrees_updated_at).total_seconds()
    if stale_by > age:
        return False, (
            f"node {label}'s last complete slot enumeration is {stale_by:.0f}s old (bound {age:.0f}s): "
            "its reported occupancy is too stale to dispatch against"
        )
    return True, f"node {label} enumerated its slots {stale_by:.0f}s ago"


def _node_org_in_service():
    """The conjunct that keeps a disabled org's nodes out of the dispatch roster.

    Step one of the org lifecycle: disabling stops dispatch and lets in-flight work settle;
    `app/tenancy/org_lifecycle.py` is step two. The roster suffices because placement's org floor is strict
    (`app/placement_grants.py`).

    Not in `placement_grants._placeable_slot_clause`: that feeds the feasibility sweep, whose refusal is
    terminal `system_failed`, so it would destroy or strand queued tasks.
    Work is not started; nothing queued is touched.

    NOT EXISTS rather than a join, so a NULL or missing org stays in the roster: fail-open on the
    tenancy axis by design; this conjunct is only the kill switch.

    An org whose plan has ended is out of service too (`app/plan_terms.py::ended_clause`, any plan with a
    duration — D-7), until the operator extends it or moves it to another plan: its queued tasks simply wait. An org that is not `active` (pending approval, or rejected) is out of service too: the backstop
    behind `app/org_gate.py::require_org_active`: no node of a pending org is dispatched to, even through
    a door that forgot the gate, and its tasks have no other node to land on while placement's org floor
    stays strict (above).

    A staging org (`app/staging_org.py`) is out of service while its parent is disabled, deleted or not active. Its
    own plan end is not read: its terms are its parent's, and the dispatch budget holds it once the parent's plan
    ends or loses the staging switch (`staging_org.dispatch_hold`).
    """
    parent = aliased(Org)
    return ~select(Org.id).where(
        Org.id == Node.org_id,
        or_(
            Org.enabled.is_(False),
            Org.deleted_at.is_not(None),
            Org.lifecycle != ORG_ACTIVE,
            and_(Org.parent_org_id.is_(None), plan_terms.ended_clause()),
            select(parent.id).where(
                parent.id == Org.parent_org_id,
                or_(parent.enabled.is_(False), parent.deleted_at.is_not(None), parent.lifecycle != ORG_ACTIVE),
            ).exists(),
        ),
    ).exists()


def live_nodes_for_project(
    session, project_id: int, *, max_age_secs: Optional[float] = None
) -> list[str]:
    """The sorted node uuids (as strings) that host `project_id` (`slot_allocation.hosting_node_ids`: its assignments, so a
    project with no free static slot still runs there, on a dynamic worktree), sit in an org that is in
    service, and have posted a complete slot enumeration inside the freshness bound — the nodes a
    dispatch plan may hand work to right now.

    Membership is freshness, not hosting alone: a dead node keeps its assignments, so partitioning over
    them would strand work; a dead node drops out within one bound. Sorted, since a stable order is the
    disjointness guarantee `assign_nodes` relies on.

    Returns `str(nodes.id)`, never names: names repeat across orgs, so a name-keyed roster would merge
    two orgs' nodes. `assign_nodes`/`owns_assignment` test membership with `in`, so every consumer must
    hold the same uuid strings (a mixed set silently makes every node take everything).
    """
    age = max_observation_age_secs() if max_age_secs is None else max_age_secs
    # Keyed on the node id, not the name, so a console rename cannot strand the shard.
    hosting = slot_allocation.hosting_node_ids(session, int(project_id))
    if not hosting:
        return []
    rows = session.execute(
        select(Node.id).where(Node.id.in_(hosting), *allocation_roster_clause(age))
    ).all()
    return sorted(str(r.id) for r in rows)


def allocation_roster_clause(age: float) -> tuple:
    """The node half of `live_nodes_for_project`: enabled, not draining, org in service, slot enumeration
    inside `age` seconds. Shared with the dispatch plan, which reads it as one column of the asking node's
    own row (`routes/fleet.py::build_dispatch_plan`) and pairs it with the pass's hosting map."""
    return (
        Node.enabled.is_(True),
        # Not winding down: this is the allocation roster, so excluding draining nodes is "no new
        # tasks". Deliberately absent from `live_nodes` and `placement_grants._placeable_slot_clause`
        # (see `app/models/nodes.py::Node.draining`).
        Node.draining.is_(False),
        # Org in service (see `_node_org_in_service`).
        _node_org_in_service(),
        Node.worktrees_updated_at.is_not(None),
        func.extract("epoch", func.now() - Node.worktrees_updated_at) <= age,
    )


def live_nodes(session, *, max_age_secs: Optional[float] = None) -> list[str]:
    """Every enabled node in an in-service org whose last complete slot enumeration is inside the
    freshness bound, sorted.

    The unrestricted form of `live_nodes_for_project`, for the CP drain (`app/lease_drain.py`), which
    iterates nodes. Same bound and stamp. Node uuid strings, which the drain feeds to `pool_records`/`pool_state`."""
    age = max_observation_age_secs() if max_age_secs is None else max_age_secs
    rows = session.execute(select(Node.id).where(*live_node_clause(age))).all()
    return sorted(str(r.id) for r in rows)


def live_node_clause(age: float) -> tuple:
    """`live_nodes`' where-clause: enabled, org in service, slot enumeration inside `age` seconds.

    Shared with `live_node_exists` and the company-setup status (`routes/org_setup.py`), so "a node is
    live" means what dispatch means by it rather than `nodes.last_seen`."""
    return (
        Node.enabled.is_(True),
        # `Node.draining` deliberately absent: a draining node still gets asked for a plan, so
        # `routes/fleet.py::owns_candidate` can keep re-dispatching its pinned tasks there
        # (`app/models/nodes.py::Node.draining`).
        _node_org_in_service(),
        Node.worktrees_updated_at.is_not(None),
        func.extract("epoch", func.now() - Node.worktrees_updated_at) <= age,
    )


def not_a_vnode_host():
    """The conjunct that drops Multi-Node Hosts, which take no work (`app/vnodes.py::refuse_work_on_host`)."""
    return ~select(VnodeHost.node_id).where(VnodeHost.node_id == Node.id).exists()


def live_node_exists(session, org_id) -> bool:
    """Does `org_id` own a node `live_nodes` would list that is not a vnode host? One `EXISTS`."""
    clause = select(Node.id).where(
        Node.org_id == org_id, not_a_vnode_host(), *live_node_clause(max_observation_age_secs())
    )
    return bool(session.scalar(select(clause.exists())))


def held_on_dead_nodes(session, task_ids: Iterable[int]) -> dict[int, str]:
    """`{task id: node name}` for those of `task_ids` holding a slot on a node `live_nodes` would not list.

    The drain plans only live nodes and offers a held task only to its hold node (`routes/fleet.py::
    owns_candidate`), so such a task waits until the node returns — or until Reset frees the slot. Nothing
    in the drain says so (no pass runs for that node), so the readers ask here. One statement."""
    ids = list(task_ids)
    if not ids:
        return {}
    live = select(Node.id).where(*live_node_clause(max_observation_age_secs()))
    rows = session.execute(
        select(Worktree.task_id, Node.name)
        .join(Node, Node.id == Worktree.node_id)
        # `slot_state.TASK` spelled out (as `org_limits.wait_reasons` does): keeps `slot_state`'s import graph out.
        .where(Worktree.task_id.in_(ids), Worktree.state == "task", Node.id.not_in(live))
    ).all()
    return {task_id: name for task_id, name in rows}


def _uuids(node_ids: Iterable[str]) -> dict[str, UUID]:
    """`{node uuid string: UUID}` for the uuid-shaped entries of `node_ids`; anything else is absent."""
    return {node_id: UUID(node_id) for node_id in node_ids if is_uuid_shaped(node_id)}


def _fold_by_node(session, node_ids: Iterable[str], fold: Callable[[Any], Any]) -> dict[str, Any]:
    """`{node uuid: fold(latest metrics row or None)}` for the non-empty entries of `node_ids` — the one join
    the three `*_by_node` readers share; each fold is `node_host_metrics`' own. A non-uuid entry folds `None`.
    Total over `node_ids`."""
    wanted = [node_id for node_id in node_ids if node_id]
    if not wanted:
        return {}
    ids = _uuids(wanted)
    latest = node_host_metrics.latest_map(session, ids.values())
    return {node_id: fold(latest.get(ids[node_id]) if node_id in ids else None) for node_id in wanted}


def busyness_by_node(session, node_ids: Iterable[str]) -> dict[str, node_host_metrics.Busyness]:
    """`{node uuid: busyness}` for `node_ids` — the load ranking `assign_nodes` orders on. The fold is
    `node_host_metrics.busyness`'s: a node with no row (or a non-uuid entry) comes back `BUSIEST`."""
    return _fold_by_node(session, node_ids, node_host_metrics.busyness)


def free_cores_by_node(session, node_ids: Iterable[str]) -> dict[str, Optional[float]]:
    """`{node uuid: idle cores}` for `node_ids` — what a merge-lane run is placed on (`assign_nodes`'
    `free_cores`). The fold (and `None` for no fresh reading) is `node_host_metrics.free_cores`'s."""
    return _fold_by_node(session, node_ids, node_host_metrics.free_cores)


def cores_by_node(session, node_ids: Iterable[str]) -> dict[str, int]:
    """`{node uuid: usable core count}` for `node_ids` — the power `assign_nodes` divides runner counts by.
    The fold (including "unknown is 1") is `node_host_metrics.cores`'s: an unheard-from node comes back `1`,
    pessimistic in both uses."""
    return _fold_by_node(session, node_ids, node_host_metrics.cores)


# Tie band width, single-homed on `placement_cpu_tie_band_pct` (`app/operator_settings/`); why a tie
# is a band is `node_host_metrics.band`'s. Read here and passed down: `assign_nodes` stays pure.


def cpu_tie_band_pct() -> float:
    """The `placement_cpu_tie_band_pct` operator override, else the catalogue default: the band within
    which two nodes count as equally loaded and runner-per-core density decides."""
    return settings_reads.get_float("placement_cpu_tie_band_pct")


def assign_nodes(
    task_ids: Iterable[int],
    live: list[str],
    *,
    allowed_by_task: dict[int, Optional[frozenset[str]]],
    busyness: dict[str, node_host_metrics.Busyness],
    occupancy: dict[str, int],
    cores: dict[str, int],
    free_slots: dict[str, int],
    band_pct: float,
    booked: Optional[dict[str, int]] = None,
    free_cores: Optional[dict[str, Optional[float]]] = None,
    run_cores: float = 0.0,
) -> dict[int, str]:
    """`{task_id: the node uuid that owns it}` — which node a waiting task is allocated to.

    Every node key/member is the node uuid string (`str(nodes.id)`; `allowed=None` means unrestricted):
    names repeat across orgs. Ranked over eligible nodes only.
    Key: (1) measured load banded by `band_pct` (CPU leads, operator decision); (2) runners per core
    incl. this walk's `booked` placements (the in-pass spread); (3) the raw reading; (4) the uuid.
    Band the measurement, never the booked ledger. Free slots gate preference; with no room anywhere
    it still allocates (the owner journals a decline). No busyness ceiling (operator decision).

    Inputs move continuously, so compute the table once per pass and share it
    (`routes/fleet.py::fleet_allocation`), or two callers can double dispatch. `task_ids` in dispatch
    order (`priority DESC, id`). Pass one `booked` across all queues (mutated in place).

    `free_cores` (`free_cores_by_node`) switches to the MERGE-LANE key: the most idle cores
    first, a node with no fresh reading behind every node with one, then the keys above. A lane run is one
    full gate, not one more light runner, so it wants raw idle capacity, not the least-loaded percentage.
    Each placement debits its node `run_cores` IN `free_cores` (mutated in place, like `booked`; pass one
    map across every lane queue of a pass), so two lanes of one pass spread."""
    if booked is None:
        booked = {}
    # The measurement, never written to (keys 1 and 3); `booked` is what the walk bumps.
    measured = {node: busyness.get(node, node_host_metrics.BUSIEST) for node in live}
    for node in live:
        # Default 0 occupancy (normally the true answer), unlike `cores`' 1 (which would divide by 0).
        booked.setdefault(node, int(occupancy.get(node, 0)))
    room = {node: int(free_slots.get(node, 0)) for node in live}
    # The lane key's idle cores, debited as the pass places; `None` (no fresh reading) stays `None`.
    idle = free_cores if free_cores is not None else {}
    for node in live:
        idle.setdefault(node, None)
    assignments: dict[int, str] = {}
    for task_id in task_ids:
        if task_id not in allowed_by_task:
            # The one lookup that must not default: `.get()` would read a missing confinement as
            # unrestricted (fail-open). Refuse to place instead.
            log.warning(
                "assign_nodes: task %s carries no confinement entry — refusing to place it rather than "
                "treating an unresolved confinement as unrestricted", task_id,
            )
            continue
        allowed = allowed_by_task[task_id]
        eligible = [n for n in live if allowed is None or n in allowed]
        if not eligible:
            # No live node may run this task; `unplaceable_reason` asks that fleet-wide.
            continue
        with_room = [n for n in eligible if room[n] > 0]
        pick = min(with_room or eligible, key=lambda n: (
            *(() if free_cores is None else (idle[n] is None, -(idle[n] or 0.0))),  # 0. lane: idle cores
            node_host_metrics.band(measured[n], band_pct),                # 1. CPU leads, in bands
            node_host_metrics.runners_per_core(booked[n], cores.get(n)),  # 2. density breaks the tie
            measured[n],                                                  # 3. the reading inside the band
            n,                                                            # 4. determinism, never dict order
        ))
        if idle[pick] is not None:
            idle[pick] -= run_cores
        if room[pick] > 0:
            room[pick] -= 1
        # The whole spread, in the no-room arm too.
        booked[pick] = booked[pick] + 1
        assignments[task_id] = pick
    return assignments


def owns_assignment(
    node_id: str,
    live: list[str],
    allowed_ids: Optional[frozenset[str]],
    task_id: int,
    assignment: dict[int, str],
) -> bool:
    """Does `node_id` (uuid string) own `task_id` under `assignment` (`assign_nodes`' table)?

    Confinement is absolute and tested first. A node not in `live` takes everything: before its first
    complete enumeration it is in nobody's ranking, and `pool_state_ok` (fail-closed) is what actually
    stops it dispatching."""
    if allowed_ids is not None and node_id not in allowed_ids:
        return False                      # may not run this task, whatever the ranking says
    if node_id not in live:
        return True
    return assignment.get(task_id) == node_id
