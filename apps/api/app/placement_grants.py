"""What a task may be placed on — its node and account sets — plus the structural refusal when those
sets make placement impossible. The one home for that translation: `app/routes/fleet.py` and
`app/lease_drain.py` both call `allowed_nodes`/`allowed_accounts`.

Two conjuncts in fixed order: the org floor (the task's tenant, `placement_org`), then the narrowing —
nodes hosting the task's PROJECT, accounts its LAUNCHER may dispatch on (`authz.visible_account_clause`);
the intersection means no binding or superadmin crosses tenants, and floor-first names the right
operator action. `slot_state.lease`/`claim_for_user`'s atomic UPDATE enforces the floor again, closing
the point-in-time window; only this module can explain a refusal.

The refusal is structural only (D3/D7): row presence, never liveness; `accounts.enabled`, never quota or
auth state. Offline nodes, busy slots and quota/auth-blocked accounts leave the task waiting.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features, slot_allocation, slot_state, task_ancestry, tenant_defaults
from app.authz.principal import principal_for_user
from app.authz.visibility import AllowedObjects, visible_account_clause
from app.models.credentials import Account
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.node_identity import node_id_or_none
from app.operator_settings import reads as settings_reads

log = logging.getLogger("app.placement-grants")

#: The fixed set of refusal causes; each names a different operator action (R11).
#: One node cause, not two: with nodes derived from the project there is no "permitted nodes do not host
#: it" case distinct from "no in-org node hosts it" — both ask for the same assignment (`_node_refusal`).
CAUSE_NO_PROJECT_NODE = "no_project_node"
CAUSE_NO_USABLE_ACCOUNT = "no_usable_account"
CAUSE_NO_ENABLED_GRANTED_ACCOUNT = "no_enabled_granted_account"
#: The floor the others narrow: task and pool in different tenants, or no resolvable org. Separate from
#: `no_project_node` because the action differs in kind (attribute the task, or assign the project to a
#: node in this org).
CAUSE_ORG_MISMATCH = "org_mismatch"
#: A node-target task's pin (`Task.pinned_node_id`, `app/node_targets.py`) names a node that is gone: deleted,
#: or not in the task's org. Nothing else may take the task, so this is terminal, not a wait.
CAUSE_PINNED_NODE_GONE = "pinned_node_gone"
#: The pinned node exists in the task's org but is disabled. Separate from GONE: the action is to enable it.
CAUSE_PINNED_NODE_DISABLED = "pinned_node_disabled"

CAUSES = (
    CAUSE_ORG_MISMATCH,
    CAUSE_PINNED_NODE_GONE,
    CAUSE_PINNED_NODE_DISABLED,
    CAUSE_NO_PROJECT_NODE,
    CAUSE_NO_USABLE_ACCOUNT,
    CAUSE_NO_ENABLED_GRANTED_ACCOUNT,
)

#: Object type → class whose `org_id` is its floor. Local rather than `authz.OBJECT_SPEC_BY_NAME`:
#: tenancy is not an authz-kernel question.
_FLOOR_ENTITY = {"node": Node, "account": Account}

# A task write key delegates the creator's reach: fan-out stamps children with the epic creator's
# `created_by_id`, so a `tasks:edit` holder starting someone else's task runs it on the creator's
# accounts without holding those bindings. Accepted (the same key can rewrite the prompt).

# Names enumerated before "and N more", so `task_notify`'s `REASON_MAX` (500) cut keeps the actionable
# half.
_MAX_NAMES_IN_SENTENCE = 5


@dataclass(frozen=True)
class PlacementRefusal:
    """One structural refusal: the machine key, the sentence a human reads, and the operator detail.

    `end_attempt(reason=...)` takes `sentence`, the Activity note's `extra` takes `cause`, and `detail`
    is logged and stored nowhere.

    The sentence names no node and no account: it is served at object-scoped `tasks:view`, a wider
    audience than the launcher (cf. `routes/tasks/activity.py::_strip_ungranted_account_slugs`; `NODE_SPEC` has no
    `unscoped_read_key`). `detail` carries the enumeration to the log. Account-set cardinality is
    disclosed, accepted."""

    cause: str
    sentence: str
    detail: str


def allowed_nodes(
    session: Session, task_id: Optional[int], *, memo: Optional[dict] = None
) -> AllowedObjects:
    """The nodes this task may be placed on — `AllowedObjects` over `Node.id` (uuids): the org floor ∩
    the nodes hosting its project (`slot_allocation.hosting_node_ids`: an assignment, where a dynamic
    worktree is made when no static one is free). The launcher does not narrow nodes. A co-located node on another org's Multi-Node Host is
    dropped unless the task's org holds the operator's exception (`app/tenant_defaults.py`)."""
    return _allowed(session, task_id, "node", memo)


def allowed_accounts(
    session: Session, task_id: Optional[int], *, memo: Optional[dict] = None
) -> AllowedObjects:
    """The accounts this task's launcher may dispatch on — `AllowedObjects` over `Account.id` (uuids):
    the org floor ∩ `authz.visible_account_clause` (own personal account, an account-scope binding, or
    org-wide `accounts:view`)."""
    return _allowed(session, task_id, "account", memo)


def placement_org(session: Session, task_id: Optional[int]) -> Optional[UUID]:
    """Which org places this task — the single home for that question and the value every taking
    statement is bound with. `None` means unresolvable: a refusal, never a wildcard.

    `tasks.org_id` is stored so the lease floor is single-table (`app/tenancy/manifest.py`) and is NOT NULL
    (`app/models/base.py::OrgScoped`), so `None` is reached only for a `task_id` whose row is gone.

    It equals the project's org: `tasks.project_id` carries a same-org key (`app/tenancy/keys.py`)."""
    task = session.get(Task, task_id) if task_id is not None else None
    return task.org_id if task is not None else None


def _who_places(
    session: Session, task_id: Optional[int], memo: Optional[dict]
) -> tuple[Optional[UUID], Optional[UUID]]:
    """`(org, launcher)` for one task, memoised per task per pass.

    Keyed on the task (unlike the project- and launcher-keyed set memo) because `build_dispatch_plan` resolves every
    waiting task once per node. Namespaced key so it cannot collide with the other memo entries."""
    if memo is None:
        return placement_org(session, task_id), task_ancestry.launcher_user_id(session, task_id)
    key = ("_who_places", task_id)
    if key not in memo:
        memo[key] = (
            placement_org(session, task_id),
            task_ancestry.launcher_user_id(session, task_id),
        )
    return memo[key]


def org_floor(session: Session, object_type: str, org_id: UUID) -> frozenset:
    """Every object of this type that belongs to `org_id` — the floor `_allowed` then narrows.

    Public for callers needing the floor alone: `routes/fleet.py::_account_for_task`'s pinned arm
    (waives the launcher's reach for a leased task, A2, never the tenant), and the console credential pick
    (`routes/console.py::_console_account`, both transports) via `routes/fleet.py::_best_account`,
    keyed on the node's org so a tenant's node never runs another org's credential regardless of the read
    seam's mode or `operator_scope()`.

    Strict equality: a NULL-org row is in no floor (fail-closed, still reachable for accounts). A `None`
    org raises, as `slot_state._require_org` does: `org_id == None` compiles to `IS NULL`, a wildcard over
    unstamped rows."""
    if org_id is None:
        raise ValueError(
            "placement_grants.org_floor() requires an org; None would compile to `org_id IS NULL` and "
            "answer every UNSTAMPED row instead of none. Resolve the org (placement_org) or decline."
        )
    try:
        entity = _FLOOR_ENTITY[object_type]
    except KeyError:
        # A named verdict listing the known types, like every other refusal here.
        raise ValueError(
            f"placement_grants.org_floor() has no floor for object type {object_type!r}; "
            f"it resolves {sorted(_FLOOR_ENTITY)} only"
        ) from None
    statement = select(entity.id).where(entity.org_id == org_id)
    if entity is Node:
        # A node an operator just moved here takes no work until it has erased the old org's account dirs.
        statement = statement.where(Node.org_transfer_cleanup_pending.is_(False))
    return frozenset(session.scalars(statement).all())


def _allowed(
    session: Session, task_id: Optional[int], object_type: str, memo: Optional[dict]
) -> AllowedObjects:
    """The org floor, narrowed — in that order: nodes by the task's project, accounts by its launcher.

    The intersection means the narrowing only narrows: a cross-org binding or an unrestricted launcher
    (superadmin, system actor) cannot widen placement past the tenant. Never returns `unrestricted`;
    an unresolvable org gets the empty set.

    `memo` is a caller-owned per-pass dict keyed `(object_type, org, project, effective pin)` for nodes and
    `(object_type, org, launcher)` for accounts (an epic's children share one entry). The pin is in the node
    key because it narrows: two Nodes-project tasks pinned to different nodes must not share an answer. Per
    pass so a binding edit, enrolment or org move takes effect on the next pass."""
    org, launcher = _who_places(session, task_id, memo)
    if org is None:
        # Deny: no tenant, no resources. `unplaceable_reason` turns this into `org_mismatch`.
        return AllowedObjects(unrestricted=False, ids=frozenset())
    task = session.get(Task, task_id) if object_type == "node" else None
    if object_type == "node" and task is None:
        return AllowedObjects(unrestricted=False, ids=frozenset())
    pin = effective_pin(session, task, memo) if task is not None else None
    key = (
        (object_type, org, task.project_id, pin) if task is not None
        else (object_type, org, launcher)
    )
    if memo is not None and key in memo:
        return memo[key]
    floor_key = ("_org_floor", object_type, org)
    if memo is None or floor_key not in memo:
        floor = org_floor(session, object_type, org)
        if memo is not None:
            memo[floor_key] = floor
    else:
        floor = memo[floor_key]
    if task is not None:
        ids = _placeable_project_nodes(session, task.project_id, floor)
        if pin is not None:
            # A pinned task (`effective_pin`): only its own node, whatever else hosts the project.
            ids = ids & {pin}
    elif launcher is None:
        # D5's creator-less case (or the system actor, which `launcher_user_id` skips): nobody to
        # narrow by, so only the floor applies (confining would be unfixable) — never a personal account.
        ids = floor if object_type == "node" else shareable_account_floor(session, floor)
    else:
        ids = floor & _launcher_account_ids(session, launcher, floor)
    allowed = AllowedObjects(unrestricted=False, ids=ids)
    if memo is not None:
        memo[key] = allowed
    return allowed


def node_pin_enabled(org_id: Optional[UUID]) -> bool:
    """Does `org_id` let its members pin a task to a node (`task_node_pin_enabled`)? Off, a stored pin
    places nothing (`effective_pin`). No org is off: an unscoped read of an org-tier knob raises."""
    return org_id is not None and settings_reads.get_bool("task_node_pin_enabled", org=org_id)


def effective_pin(session: Session, task: Task, memo: Optional[dict] = None) -> Optional[UUID]:
    """The pin placement obeys: a node task's (`app/node_targets.py`, its project's `system_kind`) always, any
    other only while its org has the node picker on (`node_pin_enabled`) — off, the stored pin is kept for when it
    is turned back on, and the task places on any of its project's nodes. `memo`: the per-pass dict, keyed per task."""
    if task.pinned_node_id is None:
        return None
    key = ("_effective_pin", task.id, task.pinned_node_id)
    if memo is not None and key in memo:
        return memo[key]
    if task.project_id is not None and session.scalar(
        select(Project.system_kind).where(Project.id == task.project_id)
    ) is not None:
        pin = task.pinned_node_id
    else:
        pin = task.pinned_node_id if node_pin_enabled(task.org_id) else None
    if memo is not None:
        memo[key] = pin
    return pin


def project_node_ids(session: Session, project_id: int, org_id: UUID) -> frozenset:
    """The nodes a task of `project_id` in `org_id` may be placed on, before any pin: what a node pin
    (`routes/tasks/create.py::_pinned_node_or_refuse`) must name. Same set `_allowed` narrows a task's pin within."""
    return _placeable_project_nodes(session, project_id, org_floor(session, "node", org_id))


def _placeable_project_nodes(session: Session, project_id: int, floor: frozenset) -> frozenset:
    """`floor` narrowed to the nodes hosting `project_id`, less the dedicated-node exclusion
    (`app/tenant_defaults.py`): never a co-located node on another org's host without the operator's exception,
    whichever path leases, claims or builds on it. Hosting only: `Node.enabled` and liveness are the refusal's and
    the dispatcher's questions."""
    if not floor:
        return frozenset()
    ids = floor & frozenset(slot_allocation.hosting_node_ids(session, project_id))
    return ids - tenant_defaults.foreign_hosted_among(session, ids)


def shareable_account_floor(
    session: Session, floor: frozenset, owner_id: Optional[UUID] = None
) -> frozenset:
    """`floor` minus every PERSONAL account (`owner_id IS NOT NULL`) except `owner_id`'s own — the account
    floor for a pick no launcher's reach narrows: the pinned relaunch (`routes/fleet.py::
    _account_for_task`, `owner_id` = the launcher), a launcher-less task and a console (`None`). Personal
    stays personal (G3): a colleague's account never lands on a runner or a terminal it did not bring."""
    if not floor:
        return frozenset()
    shareable = Account.owner_id.is_(None)
    if owner_id is not None:
        shareable = shareable | (Account.owner_id == owner_id)
    return frozenset(
        session.scalars(select(Account.id).where(Account.id.in_(floor), shareable)).all()
    )


def launcher_of(session: Session, task_id: Optional[int]) -> Optional[UUID]:
    """The user whose reach narrows `task_id`'s account pick (`task_ancestry.launcher_user_id`)."""
    return task_ancestry.launcher_user_id(session, task_id)


def _launcher_account_ids(session: Session, launcher: UUID, floor: frozenset) -> frozenset:
    """The accounts `launcher` may dispatch on, within `floor` (`authz.visible_account_clause`: its own,
    account bindings, and the org's unowned accounts for an org-wide holder — a superadmin included, so
    nobody's task runs on a colleague's personal account). A launcher row gone is deny."""
    user = session.get(AdminUser, launcher)
    if user is None:
        return frozenset()
    principal = principal_for_user(session, user)
    return frozenset(
        session.scalars(select(Account.id).where(visible_account_clause(principal))).all()
    )


def node_may_claim(session: Session, task_id: Optional[int], node_id: Optional[str]) -> bool:
    """The claim floor: may a runner on `node_id` claim `task_id`? Fail-closed.

    Uuid-to-uuid (`node_id_or_none` resolves either node.env spelling). The org floor arrives via
    `allowed_nodes`; there is no unrestricted arm."""
    allowed = allowed_nodes(session, task_id)
    if not node_id:
        # Fails closed, contrary to `control_plane_api.this_node_id()`'s no-op-on-misconfiguration
        # intent: an access decision must refuse an absent node.
        log.warning(
            "claim floor: a claim for task %s arrived with NO node_id and is REFUSED. Since the org "
            "floor there is no unrestricted task left to exempt, so a daemon whose NODE_ID is unset "
            "now serves NOTHING — which looks exactly like an empty queue from every angle an "
            "operator can see. Check /etc/jaah/node.env on the host that made this call",
            task_id,
        )
        return False
    resolved = node_id_or_none(session, node_id)
    if resolved is None:
        log.warning(
            "claim floor: node %r resolves to no `nodes` row, so task %s is REFUSED (fail-closed: a "
            "placement decision cannot be made against a node that does not exist)",
            node_id, task_id,
        )
        return False
    if allowed.contains(resolved):
        return True
    # The one exemption: a task already held on this node (A2). `dispatch_plan.owns_candidate` and
    # `unplaceable_reason` exempt held tasks too; refusing here would strand the row `ready` under a
    # working runner forever, since only the task's terminal/delete/reset edge frees a slot.
    # Not a door: only the confined `dispatch_plan` mints holds. It also waives the org floor, unlike
    # `routes/fleet.py::_account_for_task`: the hold's own `slot_state.lease` already compared the org;
    # `routes/fleet.py::_slots_the_org_cannot_take` keeps the next placement off legacy slots.
    hold = slot_state.task_hold(session, task_id)
    held_by = node_id_or_none(session, hold["node_id"]) if hold else None
    if held_by is not None and held_by == resolved:
        log.info(
            "claim floor: node %s is not permitted task %s any more, but it holds that task's %s "
            "lease — the CP placed it there before the permitted set changed, so the started attempt is "
            "left alone (A2) and only the NEXT placement is confined",
            node_id, task_id, hold["state"],
        )
        return True
    return False


def unplaceable_reason(
    session: Session, task: Task, *, memo: Optional[dict] = None
) -> Optional[PlacementRefusal]:
    """The structural-impossibility verdict: three independent set tests, org first, then nodes, then
    accounts, so an org with no slots gets the org sentence rather than the node one.

    Returns None for placeable tasks and two exclusions: a task with no project, and a task holding a
    slot-state hold. A node↔account pair test is rejected: `_account_candidates` has no
    `node_id` predicate, so it would refuse tasks `_best_account` would place."""
    project = session.get(Project, task.project_id) if task.project_id is not None else None
    if project is None:
        return None
    # An SDK agent runs no model, so no account test applies to it; a
    # refused agent is the unrunnable-agent sweep's (`lease_drain._fail_unrunnable_agents`), never an account
    # sentence. `resolve_agent` returns `Builtin` for every task that names no agent, so built-ins are unchanged.
    from app import agent_resolution

    needs_account = isinstance(
        agent_resolution.resolve_agent(session, task, project), agent_resolution.Builtin
    ) and not _on_jaah_account(session, task.org_id, memo)
    refusal = (_pinned_refusal(session, task, project, memo)
               or _org_refusal(session, task, project, memo, needs_account=needs_account)
               or _node_refusal(session, task, project, memo)
               or (_account_refusal(session, task, project, memo) if needs_account else None))
    if refusal is None:
        return None
    if slot_state.task_hold(session, task.id) is not None:
        # A hold cannot be relocated and refusing would destroy uncommitted work (the slot survives
        # attempt ends). Asked last: only refusals pay for this query.
        return None
    return refusal


def _on_jaah_account(session: Session, org_id: Optional[UUID], memo: Optional[dict]) -> bool:
    """`jaah_routing.on_jaah_account`, once per org per pass (`memo`): an org on the Jaah account runs on no account
    of its own, so having none is no refusal. Never, in an edition without it."""
    if not edition_features.enabled("jaah_ai_credit"):
        return False
    from app.jaah_routing import routing as jaah_routing

    key = ("_on_jaah_account", org_id)
    if memo is not None and key in memo:
        return memo[key]
    found = jaah_routing.on_jaah_account(session, org_id)
    if memo is not None:
        memo[key] = found
    return found


def _pinned_refusal(
    session: Session, task: Task, project: Project, memo: Optional[dict]
) -> Optional[PlacementRefusal]:
    """Set test −1, a pinned task's own: does its pinned node still exist in its org, and is it enabled?

    First, so a pin whose node is gone or disabled is told so rather than to "assign the project", which would
    not help: only that node may take the task. An offline node is not refused (D3/D7): the task waits for it.
    An unresolvable org is `_org_refusal`'s. Unpinned tasks never reach a query here, nor a pin placement ignores
    (`effective_pin`)."""
    pin = effective_pin(session, task, memo)
    if pin is None:
        return None
    org, _launcher = _who_places(session, task.id, memo)
    if org is None:
        return None
    node = session.execute(
        select(Node.org_id, Node.enabled).where(Node.id == pin)
    ).first()
    if node is None or node.org_id != org:
        return PlacementRefusal(
            CAUSE_PINNED_NODE_GONE,
            _pinned_node_gone_sentence(project),
            f"task {task.id} places in org {org} and is pinned to node {pin}, which "
            + ("no longer exists" if node is None else f"belongs to org {node.org_id}"),
        )
    if not node.enabled:
        return PlacementRefusal(
            CAUSE_PINNED_NODE_DISABLED,
            _pinned_node_disabled_sentence(project),
            f"task {task.id} is pinned to node {pin}, which is disabled",
        )
    return None


def _org_refusal(
    session: Session, task: Task, project: Project, memo: Optional[dict] = None, *, needs_account: bool = True,
) -> Optional[PlacementRefusal]:
    """Set test 0, the floor: is this task in an org, and does that org run this project?

    One cause (`org_mismatch`) for three arms: no resolvable org, no slot in the org, no enabled account
    in the org. The account arm lives here so a tenant with no account is not told to ask for one to be
    shared when none exists; the refusal is terminal. Each arm excludes the fleet-wide-empty case
    (transient: a fresh project, missing pool mount at boot), which waits instead."""
    org, _launcher = _who_places(session, task.id, memo)
    if org is None:
        return PlacementRefusal(
            CAUSE_ORG_MISMATCH,
            _no_org_sentence(project),
            f"task {task.id} carries no org_id, no ancestor carries one, and its creator chain names "
            "no user with one — so there is no tenant whose pool it could be placed in",
        )
    # Both resource arms are asked: a slot-only org must fall through to the account arm. The second
    # conjunct is the fleet-wide exclusion.
    if (not _placeable_slot_exists(session, project.id, org_id=org)
            and _some_org_hosts(session, project.id)):
        return PlacementRefusal(
            CAUSE_ORG_MISMATCH,
            _no_org_slot_sentence(project),
            f"task {task.id} places in org {org}; no enabled node in that org hosts {project.name}, "
            f"though {project.name} is assigned to nodes elsewhere in the pool",
        )
    if needs_account and not _enabled_account_exists(session, org_id=org):
        if not _enabled_account_exists(session, org_id=None, owned_only=True):
            # No enabled account anywhere in the fleet: transient, wait.
            return None
        return PlacementRefusal(
            CAUSE_ORG_MISMATCH,
            _no_org_account_sentence(project),
            f"task {task.id} places in org {org}; no enabled `accounts` row belongs to that org, "
            "though enabled accounts exist elsewhere in the fleet",
        )
    return None


def _enabled_account_exists(
    session: Session, *, org_id: Optional[UUID], owned_only: bool = False
) -> bool:
    """Does an enabled account exist — in this org, or (with `org_id=None`) anywhere in the fleet?

    `Account.enabled` only, the same population `_account_refusal` probes (D3/D7). `owned_only` is for
    the exclusion arm: a NULL-org account is nobody's, so it must not prove another tenant is provisioned
    and make the refusal terminal."""
    stmt = select(Account.id).where(Account.enabled.is_(True)).limit(1)
    if org_id is not None:
        stmt = stmt.where(Account.org_id == org_id)
    elif owned_only:
        stmt = stmt.where(Account.org_id.is_not(None))
    return session.scalar(stmt) is not None


def _some_org_hosts(session: Session, project_id: int) -> bool:
    """Does an enabled node of any org at all host this project?

    The node twin of `owned_only`: a NULL-org node is nobody's and must not make a refusal terminal.
    `_node_names_with_worktree` stays unfiltered; it only feeds sentences."""
    return session.scalar(
        select(Node.id).where(*_placeable_slot_clause(session, project_id), Node.org_id.is_not(None)).limit(1)
    ) is not None


def _node_refusal(
    session: Session, task: Task, project: Project, memo: Optional[dict]
) -> Optional[PlacementRefusal]:
    """Set test 1: is this project assigned to an enabled node in the task's org?

    Runs after `_org_refusal`. The probe carries the org conjunct as well as the node set, so both read the
    population `_placeable_slot_clause` defines.

    One cause. `allowed_nodes` is already "in-org nodes hosting the project", so an empty set and a
    non-empty set of disabled nodes ask the operator for the same thing — assign the project to an
    enabled in-org node."""
    # No `unrestricted` arm: `_allowed` cannot answer it.
    org, _launcher = _who_places(session, task.id, memo)
    allowed = allowed_nodes(session, task.id, memo=memo)
    if allowed.ids and _placeable_slot_exists(session, project.id, node_ids=allowed.ids, org_id=org):
        return None
    anywhere = _node_names_with_worktree(session, project.id)
    if not anywhere:
        # The project runs nowhere, possibly transiently (a fresh project;
        # `fleet_observe.gone_slot_keys` deletes slots reported gone). Wait.
        return None
    return PlacementRefusal(
        CAUSE_NO_PROJECT_NODE,
        _no_project_node_sentence(project),
        f"task {task.id} places in org {org}; {project.name} is hosted on {_join(anywhere)}, none of "
        "them an enabled node of that org",
    )


def _account_refusal(
    session: Session, task: Task, project: Project, memo: Optional[dict]
) -> Optional[PlacementRefusal]:
    """Set test 2: does any account the launcher may dispatch on exist and read `enabled`? Org-floored,
    so another tenant's credential is never reachable."""
    allowed = allowed_accounts(session, task.id, memo=memo)
    if not allowed.ids:
        return PlacementRefusal(
            CAUSE_NO_USABLE_ACCOUNT, _no_usable_account_sentence(project),
            "this launcher owns no account, holds no account-scope binding and no org-wide accounts:view",
        )
    if session.scalar(
        select(Account.id)
        .where(Account.id.in_(allowed.ids), Account.enabled.is_(True))
        .limit(1)
    ) is not None:
        return None
    # After the probe, so placeable tasks never pay for this string.
    slugs = sorted(
        session.scalars(select(Account.slug).where(Account.id.in_(allowed.ids))).all()
    )
    return PlacementRefusal(
        CAUSE_NO_ENABLED_GRANTED_ACCOUNT, _no_enabled_account_sentence(project, len(slugs)),
        f"usable {_join(slugs) or '<no account row still exists>'}, none of them enabled",
    )


def _placeable_slot_clause(session: Session, project_id: int):
    """What "the project can run here" means, in one place, so refusal and detail read the same population:
    a `nodes` predicate. Decided facts only, never liveness (D3/D7):
    * the node hosts the project (`slot_allocation.hosting_node_ids`) — an assignment is enough, since a
      dispatch makes a dynamic worktree where no static one is free;
    * `Node.enabled` — matches `live_nodes_for_project`, else a disabled-only node waits invisibly.

    `Node.draining` is deliberately not a conjunct, although `live_nodes_for_project` excludes it: the
    verdict is terminal (`lease_drain._fail_unplaceable_tasks`, `final=True`), and a reversible drain must
    not terminally fail queued tasks. The wait ends when the drain disables the node; then the task gets
    the accurate refusal, or waits on "project runs nowhere" if that node held its only slots."""
    return (
        Node.id.in_(slot_allocation.hosting_node_ids(session, project_id)),
        Node.enabled.is_(True),
    )


def _placeable_slot_exists(
    session: Session, project_id: int, *, node_ids=None, org_id: Optional[UUID] = None
) -> bool:
    """Does an enabled node hosting this project exist among these nodes, in this org, or both at once?

    One function so every narrowing reads the same population (`_placeable_slot_clause`). The narrowings
    conjoin. Neither given is a caller bug."""
    if node_ids is None and org_id is None:
        raise ValueError("_placeable_slot_exists takes at least one of `node_ids` or `org_id`")
    # `Node.org_id`: a slot's own `worktrees.org_id`, which `slot_state.lease` binds, is the node's by
    # revision 0181's composite key, and a dynamic worktree is declared with the node's org.
    # `is not None` on both: an empty `node_ids` must produce `IN ()`, and a `None` org must never
    # compile to `IS NULL`.
    narrowings = []
    if node_ids is not None:
        narrowings.append(Node.id.in_(node_ids))
    if org_id is not None:
        narrowings.append(Node.org_id == org_id)
    return session.scalar(
        select(Node.id).where(*_placeable_slot_clause(session, project_id), *narrowings).limit(1)
    ) is not None


def _node_names_with_worktree(session: Session, project_id: int) -> list[str]:
    """Every enabled node name hosting this project, sorted; empty means the fleet runs it nowhere.

    Hosting, not freshness: a silent node keeps its assignment and a task placeable only there waits
    (R-K). An empty answer is never refused."""
    rows = session.execute(select(Node.name).where(*_placeable_slot_clause(session, project_id))).all()
    return sorted(r.name for r in rows)


def _join(names: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`, and `a, b, c, d, e and 7 more` past `_MAX_NAMES_IN_SENTENCE`."""
    shown = list(names[:_MAX_NAMES_IN_SENTENCE])
    if len(names) > _MAX_NAMES_IN_SENTENCE:
        shown.append(f"{len(names) - _MAX_NAMES_IN_SENTENCE} more")
    if not shown:
        return ""
    if len(shown) == 1:
        return shown[0]
    return f"{', '.join(shown[:-1])} and {shown[-1]}"


def _no_org_sentence(project: Project) -> str:
    """R11 — "this task belongs to no organization". Names no org (there is none) and must not imply the
    reader's."""
    return (
        f"This task cannot be placed: it is not attributed to an organization, so there is no pool "
        f"{project.name} could start it in. Ask an operator to re-file it from the console so it is "
        f"attributed, or to set its organization."
    )


def _no_org_slot_sentence(project: Project) -> str:
    """R11 — "your organization runs this project nowhere". Never names the other tenant that does: the
    sentence reaches a wider `tasks:view` audience, and that fact would disclose another tenant."""
    return (
        f"This task cannot be placed: {project.name} is not assigned to any node in your organization. "
        f"Ask an operator to assign the project to a node."
    )


def _no_org_account_sentence(project: Project) -> str:
    """R11 — "your organization holds no credential account". The action is to create one there, not to
    share one. Names no slug and no other org."""
    return (
        f"This task cannot be placed: running it in {project.name} needs a credential account and your "
        f"organization has none enabled. Ask an operator to add one to your organization."
    )


def _no_project_node_sentence(project: Project) -> str:
    """R11 — "this project is assigned to no enabled node in your organization"."""
    return (
        f"This task cannot be placed: {project.name} is not assigned to an enabled node in your "
        f"organization. Ask an operator to assign the project to a node."
    )


def _pinned_node_gone_sentence(project: Project) -> str:
    """R11 — "the one node this task may run on is gone". Names no node (see `PlacementRefusal`)."""
    return (
        f"This task cannot be placed in {project.name}: it may run only on the node it was filed for, and that "
        f"node no longer exists in your organization. File it again for a node that does."
    )


def _pinned_node_disabled_sentence(project: Project) -> str:
    """R11 — "the one node this task may run on is disabled"."""
    return (
        f"This task cannot be placed in {project.name}: it may run only on the node it was filed for, and that "
        f"node is disabled. Enable the node, or file the task again for another."
    )


def _no_usable_account_sentence(project: Project) -> str:
    """R11 — "you may dispatch on no credential account"."""
    return (
        f"This task cannot be placed: running it in {project.name} needs a credential account you can "
        f"use. Add your own account, or ask an admin to share one."
    )


def _no_enabled_account_sentence(project: Project, usable: int) -> str:
    """R11 — "the accounts you can use are disabled".

    Takes the count, not slugs (see `PlacementRefusal`). Never 0: an empty set already returned
    `CAUSE_NO_USABLE_ACCOUNT`."""
    if usable == 1:
        return (
            f"This task cannot be placed in {project.name}: the one credential account you can use is "
            f"disabled. Enable it, or ask an admin to share another."
        )
    return (
        f"This task cannot be placed in {project.name}: every credential account you can use is "
        f"disabled. Enable one, or ask an admin to share another."
    )
