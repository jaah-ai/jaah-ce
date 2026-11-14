"""Which nodes a project may run on: the one placement rule (node groups).

Three arms, in order:
1. **Easy Mode** (`Project.easy_mode`): the node of the org's Easy Mode comp copy, read WITHOUT
   `easy_mode.shared_node`'s usability filter (`easy_mode.comp_node_id`), so the shared node moving needs no write.
2. **Custom groups**: the org's `placement_groups_allowed` switch is ON and the project has
   `project_placement_groups` rows → the union of those groups' member nodes.
3. **All**: otherwise every node of the project's org. All is computed, never stored: a node registered later is
   in it with no write, and a project with no rows is on it.

Every arm then keeps only enabled nodes and drops Multi-Node Hosts and nodes co-located on another org's host without
the operator's exception (`tenant_defaults.foreign_hosted_among`); arms 2 and 3 also drop every node an Easy Mode
comp copy of the org points at (read unfiltered, so a momentarily unusable shared node never slips into All).
Liveness (online, draining) is NOT judged here: that is the dispatch pass's, so "no node in the groups" and "nodes
offline" stay two different waits (`dispatch_wait`).

Switch OFF ignores the rows and keeps them: turning it back ON restores them with no write. An edition without the
`placement_groups` feature (the community edition) always takes the All arm.

`slot_allocation.placement_node_ids` (where a new task may start) and `slot_allocation.hosting_node_ids` (where a
node may still act for a project it is running) both start from `allocated_node_ids`.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Literal, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features, plan_terms
from app.models.nodes import Node
from app.models.placement_groups import PlacementGroup, PlacementGroupNode, ProjectPlacementGroup
from app.models.projects import Project
from app.models.worktrees import VnodeHost

if TYPE_CHECKING:
    from app.authz.principal import Principal

#: The project page's Node groups field anchor (`ProjectNodeGroupsField.tsx` renders it as the field's `id`).
NODE_GROUPS_ANCHOR = "node-groups"

#: The name of the built-in group every project without rows runs on.
ALL_NAME = "All"


@dataclass(frozen=True)
class GroupRef:
    id: UUID
    name: str
    slug: str


@dataclass(frozen=True)
class GroupsView:
    """A project's node groups as the UI/API reads them: `mode` "all" (no rows) or "groups"; `groups` its rows;
    `active` false when rows exist but the org's switch is off (kept, inactive) or in the community edition."""

    mode: Literal["all", "groups"]
    groups: tuple[GroupRef, ...] = field(default_factory=tuple)
    active: bool = True

    @property
    def names_in_effect(self) -> list[str]:
        """The group names placement follows right now: empty for All and for kept-but-inactive rows."""
        return [g.name for g in self.groups] if self.mode == "groups" and self.active else []


def groups_enabled(session: Session, org_id: Optional[UUID]) -> bool:
    """Whether custom groups count for `org_id`: never in an edition without the feature, else the plan switch."""
    if org_id is None or not edition_features.enabled("placement_groups"):
        return False
    return plan_terms.switch_on(session, org_id, plan_terms.PLACEMENT_GROUPS)


# ── node facts of an org ─────────────────────────────────────────────────────────────────────────


@dataclass
class _OrgNodes:
    """One org's node facts, read once: every node and whether it is enabled, the hosts, the Easy Mode nodes."""

    enabled: dict[UUID, bool]
    #: Multi-Node Hosts, and co-located nodes on another org's host no work may use (`tenant_defaults`).
    hosts: frozenset[UUID]
    easy_mode: frozenset[UUID]
    easy_mode_comp_node: Optional[UUID]
    groups_on: bool


def _orgs_nodes(
    session: Session, org_ids: Iterable[UUID], *, need_switch: Iterable[UUID] = (), need_comp: Iterable[UUID] = (),
) -> dict[UUID, _OrgNodes]:
    """The node facts of many orgs in a fixed number of reads, whatever their count: their nodes, hosts and Easy Mode
    copy nodes in one read each. The plan switch is read only for `need_switch` (orgs with group rows) and the Easy
    Mode comp node only for `need_comp` (orgs with an Easy Mode project), both rare."""
    orgs = sorted(set(org_ids), key=str)
    enabled: dict[UUID, dict[UUID, bool]] = {org: {} for org in orgs}
    if not orgs:
        return {}
    for nid, org_id, on in session.execute(
        select(Node.id, Node.org_id, Node.enabled).where(Node.org_id.in_(orgs))
    ).all():
        enabled[org_id][nid] = bool(on)
    every = {nid for nodes in enabled.values() for nid in nodes}
    barred: frozenset[UUID] = frozenset()
    if every and edition_features.enabled("multi_node_hosts"):
        from app import tenant_defaults

        barred = frozenset(session.scalars(select(VnodeHost.node_id).where(VnodeHost.node_id.in_(sorted(every, key=str)))).all())
        # A node co-located on another org's host without the operator's exception runs no work, so it never joins
        # All or a group: neither placement nor the hosting set (secrets, memory) may reach it.
        barred |= tenant_defaults.foreign_hosted_among(session, every)
    # Easy Mode ships with the Customers feature (cloud only): elsewhere no node is an Easy Mode node.
    copies: dict[UUID, set[UUID]] = {}
    comps: dict[UUID, Optional[UUID]] = {}
    if edition_features.enabled("customers"):
        from app import easy_mode

        copies = easy_mode.copy_node_ids_by_org(session, orgs)
        comps = {org: easy_mode.comp_node_id(session, org) for org in set(need_comp) & set(orgs)}
    switch = set(need_switch)
    return {org: _OrgNodes(
        enabled=enabled[org],
        hosts=barred & frozenset(enabled[org]),
        easy_mode=frozenset(copies.get(org, ())),
        easy_mode_comp_node=comps.get(org),
        groups_on=org in switch and groups_enabled(session, org),
    ) for org in orgs}


def _members(session: Session, project_ids: list[int]) -> dict[int, set[UUID]]:
    """`{project id: member node ids}` for every project in `project_ids` that has group rows (a project whose groups
    have no members maps to an empty set; a project with no rows is absent)."""
    out: dict[int, set[UUID]] = {}
    if not project_ids:
        return out
    rows = session.execute(
        select(ProjectPlacementGroup.project_id, PlacementGroupNode.node_id)
        .outerjoin(PlacementGroupNode, PlacementGroupNode.group_id == ProjectPlacementGroup.group_id)
        .where(ProjectPlacementGroup.project_id.in_(project_ids))
    ).all()
    for project_id, node_id in rows:
        members = out.setdefault(project_id, set())
        if node_id is not None:
            members.add(node_id)
    return out


def _resolve(
    facts: _OrgNodes, *, easy: bool, members: Optional[set[UUID]], enabled_only: bool,
) -> set[UUID]:
    if easy:
        found = {facts.easy_mode_comp_node} if facts.easy_mode_comp_node in facts.enabled else set()
    else:
        found = set(members) if facts.groups_on and members is not None else set(facts.enabled)
        found &= set(facts.enabled)  # a member moved to another org is not this org's
        found -= facts.easy_mode
    found -= facts.hosts
    if enabled_only:
        found = {nid for nid in found if facts.enabled.get(nid)}
    return found


def _by_project(session: Session, project_ids: Iterable[int], *, enabled_only: bool) -> dict[int, set[UUID]]:
    wanted = sorted({int(p) for p in project_ids})
    out: dict[int, set[UUID]] = {p: set() for p in wanted}
    if not wanted:
        return out
    projects = session.execute(
        select(Project.id, Project.org_id, Project.easy_mode).where(Project.id.in_(wanted))
    ).all()
    by_org: dict[UUID, list[tuple[int, bool]]] = defaultdict(list)
    for pid, org_id, easy in projects:
        if org_id is not None:
            by_org[org_id].append((pid, bool(easy)))
    members = _members(session, [pid for pid, _org, _easy in projects])
    facts = _orgs_nodes(
        session, by_org,
        need_switch=[org for org, rows in by_org.items() if any(pid in members for pid, _easy in rows)],
        need_comp=[org for org, rows in by_org.items() if any(easy for _pid, easy in rows)],
    )
    for org_id, rows in by_org.items():
        for pid, easy in rows:
            out[pid] = _resolve(facts[org_id], easy=easy, members=members.get(pid), enabled_only=enabled_only)
    return out


# ── the public rule ──────────────────────────────────────────────────────────────────────────────


def allocated_node_ids(session: Session, project_id: int) -> set[UUID]:
    """The nodes `project_id` may run on (module docstring): enabled, not a host, per its arm. A fresh set."""
    return allocated_node_ids_by_project(session, [project_id])[int(project_id)]


def allocated_node_ids_by_project(session: Session, project_ids: Iterable[int]) -> dict[int, set[UUID]]:
    """`allocated_node_ids` for many projects: the org's nodes read once per org, the group rows once for all.
    Every id is a key; an unknown project maps to an empty set."""
    return _by_project(session, project_ids, enabled_only=True)


def is_allocated_node(session: Session, node_id: UUID, project_id: int) -> bool:
    """Whether `project_id` may start work on `node_id`."""
    return node_id in allocated_node_ids(session, project_id)


def candidate_node_ids(session: Session, project_id: int) -> set[UUID]:
    """`allocated_node_ids` WITHOUT the enabled filter. Only for wait text: it tells "the nodes are all disabled"
    from "there is no node at all"; it never feeds placement or access."""
    return _by_project(session, [project_id], enabled_only=False)[int(project_id)]


def all_node_ids(session: Session, org_id: UUID) -> set[UUID]:
    """The All arm for `org_id`: every enabled node that is not a host and not an Easy Mode shared node.
    `len()` of it is All's node count (`GroupAll.node_count`)."""
    facts = _orgs_nodes(session, [org_id])[org_id]
    return _resolve(facts, easy=False, members=None, enabled_only=True)


def project_groups(session: Session, project_id: int) -> GroupsView:
    """The project's node groups for the UI, the API and the wait text."""
    org_id = session.scalar(select(Project.org_id).where(Project.id == int(project_id)))
    refs = tuple(GroupRef(gid, name, slug) for gid, name, slug in session.execute(
        select(PlacementGroup.id, PlacementGroup.name, PlacementGroup.slug)
        .join(ProjectPlacementGroup, ProjectPlacementGroup.group_id == PlacementGroup.id)
        .where(ProjectPlacementGroup.project_id == int(project_id))
        .order_by(PlacementGroup.name)
    ).all())
    if not refs:
        return GroupsView(mode="all")
    return GroupsView(mode="groups", groups=refs, active=groups_enabled(session, org_id))


# ── writes (the console routes in `routes/placement_groups.py` and the agent `project_update`) ─────


#: The refusal for a group write while the org's plan leaves Node groups out (AC2); delete and "back to All" stay open.
NOT_ON_PLAN = plan_terms.not_on_plan(plan_terms.PLACEMENT_GROUPS)
#: The community edition has no node groups: every project runs on All.
NOT_IN_EDITION = "Node groups aren't part of this edition. Every project runs on all nodes."
#: A project write naming groups for an Easy Mode project, which runs only on its shared node (O1).
EASY_MODE_TAKES_NO_GROUPS = "An Easy Mode project runs on its shared node, so it can't use node groups."


def require_groups_on(session: Session, principal: Optional[object]) -> None:
    """403 unless this edition has node groups and the caller's org plan has them on (the operator passes the plan
    check, `plan_terms.require_tenant_switch`)."""
    from fastapi import HTTPException

    if not edition_features.enabled("placement_groups"):
        raise HTTPException(status_code=403, detail=NOT_IN_EDITION)
    plan_terms.require_tenant_switch(session, principal, plan_terms.PLACEMENT_GROUPS)


def member_refusal(session: Session, org_id: UUID, node_ids: Iterable[UUID]) -> Optional[str]:
    """Why one of `node_ids` may not join a group of `org_id`, as one sentence, or None. A member is a node of the
    org that is neither a Multi-Node Host (nor a node barred from work on another org's host) nor an Easy Mode
    shared node; a disabled node may join (it is kept and shown disabled)."""
    wanted = set(node_ids)
    if not wanted:
        return None
    facts = _orgs_nodes(session, [org_id])[org_id]
    if wanted - set(facts.enabled):
        return "That node isn't one of this organization's nodes."
    if wanted & facts.hosts:
        return "A Multi-Node Host runs no work, so it can't join a group."
    if wanted & facts.easy_mode:
        return "The Easy Mode shared node can't join a group."
    return None


def projects_using(session: Session, group_ids: Iterable[UUID]) -> list[int]:
    """The ids of every project allocated to any of `group_ids`, sorted."""
    ids = sorted(set(group_ids), key=str)
    if not ids:
        return []
    return sorted(set(session.scalars(
        select(ProjectPlacementGroup.project_id).where(ProjectPlacementGroup.group_id.in_(ids))
    ).all()))


def move_preview(
    session: Session, before: dict[int, set[UUID]], after: dict[int, set[UUID]], principal: "Optional[Principal]" = None,
) -> list[dict]:
    """The nodes a change takes away from a project while that project's tasks are using them: `MovePreview.nodes`
    (`[{id, name, tasks_in_use, projects: [{id, name}], hidden_projects}]`, by node name). Empty when nothing in use is
    left. Only what `principal` may see is named: another project counts in `hidden_projects`, another node is listed
    with no id and no name."""
    from app import slot_state
    from app.authz.object_access import can_access
    from app.authz.visibility import ObjectAccess
    from app.models.worktrees import Worktree

    lost = {(node, pid) for pid, nodes in before.items() for node in nodes - after.get(pid, set())}
    if not lost:
        return []
    rows = session.execute(
        select(Worktree.node_id, Worktree.project_id, Worktree.id)
        .where(Worktree.state != slot_state.FREE, Worktree.retiring_at.is_(None),
               Worktree.project_id.in_(sorted({pid for _n, pid in lost})))
    ).all()
    in_use: dict[UUID, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for node, pid, _row in rows:
        if (node, pid) in lost:
            in_use[node][pid] += 1
    if not in_use:
        return []
    names = dict(session.execute(select(Node.id, Node.name).where(Node.id.in_(sorted(in_use, key=str)))).tuples().all())
    projects = dict(session.execute(
        select(Project.id, Project.name).where(Project.id.in_(sorted({p for per in in_use.values() for p in per})))
    ).tuples().all())
    def sees(kind: str, key: "int | UUID") -> bool:
        return principal is None or can_access(session, principal, kind, key, mode=ObjectAccess.READ)

    out = []
    for node, per in in_use.items():
        shown = [pid for pid in sorted(per, key=lambda p: projects.get(p, "")) if sees("project", pid)]
        named = sees("node", node)
        out.append({
            "id": str(node) if named else None, "name": names.get(node, "") if named else "",
            "tasks_in_use": sum(per.values()),
            "projects": [{"id": pid, "name": projects.get(pid, "")} for pid in shown],
            "hidden_projects": len(per) - len(shown),
        })
    return sorted(out, key=lambda row: (row["name"], row["id"] or ""))


def apply_and_converge(
    session: Session, project_ids: Iterable[int], write: Callable[[], None], *, confirm: bool,
    principal: "Optional[Principal]" = None,
) -> None:
    """Run `write` (a group or allocation change), then converge every project in `project_ids` in the same
    transaction (`slot_allocation.converge`: free rows on nodes a project left retire, held rows never do, PRD D16).
    Without `confirm`, a change that takes a node away from a project while its tasks use it is undone and refused
    409 with the `MovePreview` (`{message, nodes}`); running tasks always finish, so confirming only says so."""
    from fastapi import HTTPException

    from app import slot_allocation

    touched = sorted({int(p) for p in project_ids})
    before = allocated_node_ids_by_project(session, touched)
    write()
    session.flush()
    after = allocated_node_ids_by_project(session, touched)
    preview = move_preview(session, before, after, principal)
    if preview and not confirm:
        session.rollback()
        raise HTTPException(status_code=409, detail={
            "message": "Tasks are still running on nodes this change takes away. They will finish there. Confirm to save.",
            "nodes": preview,
        })
    if len(touched) == 1:
        slot_allocation.converge(session, project_id=touched[0])
    elif touched:
        # One pass for the org: its placement read is batched, where a call per project re-reads the org each time.
        # A write here never spans orgs, and converging an untouched project's pairs retires nothing it may keep.
        for org_id in sorted(set(session.scalars(select(Project.org_id).where(Project.id.in_(touched))).all()), key=str):
            slot_allocation.converge(session, org_id=org_id)


def group_ids_by_name(session: Session, org_id: UUID, names: Iterable[str]) -> list[UUID]:
    """The ids of `org_id`'s groups named `names` (case-insensitive), in order; a 422 naming the first unknown one."""
    from fastapi import HTTPException

    by_name = {name.lower(): gid for gid, name in session.execute(
        select(PlacementGroup.id, PlacementGroup.name).where(PlacementGroup.org_id == org_id)
    ).all()}
    out: list[UUID] = []
    for name in names:
        gid = by_name.get((name or "").strip().lower())
        if gid is None:
            raise HTTPException(status_code=422, detail=f"There's no node group named “{name}”.")
        if gid not in out:
            out.append(gid)
    return out


def set_project_groups(
    session: Session, project: Project, group_ids: list[UUID], principal: "Optional[Principal]", *, confirm: bool,
) -> None:
    """Replace `project`'s groups with `group_ids` (`[]` = All: every row removed, none written, D11/D12), checked
    and converged. The caller has already checked who may write (`require_assignment_writer`). A non-empty list needs
    the plan switch (403 `NOT_ON_PLAN`); back to All is open with the switch off (D7). Each id must be a group of the
    project's org (422)."""
    from fastapi import HTTPException

    if group_ids:
        require_groups_on(session, principal)
        if project.easy_mode:
            raise HTTPException(status_code=422, detail=EASY_MODE_TAKES_NO_GROUPS)
        # Locked, so a delete racing this write waits for it (and then cascades the rows it adds).
        known = set(session.scalars(select(PlacementGroup.id).where(
            PlacementGroup.id.in_(group_ids), PlacementGroup.org_id == project.org_id).with_for_update()).all())
        if set(group_ids) - known:
            raise HTTPException(status_code=422, detail="That group isn't one of this organization's node groups.")

    def write() -> None:
        current = {row.group_id: row for row in session.scalars(
            select(ProjectPlacementGroup).where(ProjectPlacementGroup.project_id == project.id).with_for_update())}
        for gid, row in current.items():
            if gid not in group_ids:
                session.delete(row)
        for gid in group_ids:
            if gid not in current:
                session.add(ProjectPlacementGroup(org_id=project.org_id, project_id=project.id, group_id=gid))

    apply_and_converge(session, [project.id], write, confirm=confirm, principal=principal)
