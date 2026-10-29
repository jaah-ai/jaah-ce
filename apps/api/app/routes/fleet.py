"""The fleet dispatch DECISION — the plan builder and the account pick.

apps/api decides, jaahd executes: `app/lease_drain.py` calls `build_dispatch_plan` in-process on every
drain pass; `routes/console.py` calls `_best_account` for a console's account.
Reads come from the DB only — `app/pool_state.py` (occupancy), `app/slot_state.py` (holders), pure deciders
in `app/fleet_decide.py`. No local `git`/`gh`, no working tree, no GitHub: dispatch decides on DB state
alone and a task settles on the runner's own report.

Serves no route: its only callers are the in-process ones above.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app import (
    account_auth,
    account_fold,
    acp_runtime,
    agent_providers,
    agent_resolution,
    budgets,
    dispatch_wait,
    edition,
    edition_features,
    fleet_decide,
    fleet_verdicts,
    forges,
    git_access,
    launch_cpu,
    launch_disk,
    launch_failures,
    launch_memory,
    ce_caps,
    merge_lane_state,
    node_platform,
    org_limits,
    placement_grants,
    pool_state,
    provider_auth_policy,
    jaah_scheduler,
    slot_allocation,
    slot_state,
)
from app.operator_settings import reads as settings_reads
from app.account_runners import fleet_assigned_runner_counts
from app.authz.visibility import AllowedObjects
from app.models.credentials import Account
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import WORKTREE_KIND_DYNAMIC, Worktree
from app.node_identity import node_id_or_none, node_matches, node_org, node_ref
from app.node_clone import resolve_auth_dir
from app.routes.tasks.dispatch import dispatchable_tasks

log = logging.getLogger("fleet-decide")


def _integration_branch_or_422(project_row: Project) -> str:
    """The project's integration branch — `projects.integration_branch`, the ONLY source, never probed.

    A blank value is a 422, never `develop`: the column has a server_default, so blank means an operator
    cleared it — fail closed, a fixable configuration error. Still an `HTTPException` with no route: the
    drain's per-node worker catches and journals whatever the plan raises, and 422 is its vocabulary."""
    base = (project_row.integration_branch or "").strip()
    if not base:
        raise HTTPException(
            status_code=422,
            detail=(f"project {project_row.name!r} has no integration_branch set — the branch runners "
                    "rebase onto and PR into has no default and is never derived; set it in the console "
                    "project settings"),
        )
    return base


# ══ dispatch-plan ══════════════════════════════════════════════════════════════════════════════════════

class DispatchDecision(BaseModel):
    task_id: int
    # dispatch | skip-inflight | skip-no-granted-account | skip-node-missing-runner-cli
    #   | skip-dynamic-refused (its reason in `why`) | skip-jaah-held | skip-acp-adapter-missing (`jaah_routing`)
    #   | skip-no-account-of-bound-provider:<provider>   (the ONE code carrying a payload)
    decision: str
    lease_target: Optional[int] = None
    # True when `lease_target` names no row yet: no static slot was free, so the take declares a `dynamic`
    # worktree at that index and leases it (`slot_allocation.add_dynamic`, R6).
    create_dynamic: bool = False
    # Why a `skip-dynamic-refused` task waits (`slot_allocation.dynamic_headroom`'s reason); a
    # `skip-acp-adapter-missing`'s sentence.
    why: Optional[str] = None
    # True when `lease_target` is this task's own prior slot (`select_slot` rule 2, a `task_id` match): the
    # executor must not reset the tree. Derived from the pick, never requested, so it cannot be forged.
    resume: bool = False
    # The credential account for THIS task when placement narrows the pool below the repo's pick
    # (`app/placement_grants.py`). None ⇒ unnarrowed; the executor falls back to `RepoDispatchPlan.account`.
    # On the Jaah account, `bedrock:<jaah session uuid>` (`app/jaah_routing/`).
    account: Optional[str] = None
    # K2, the Jaah-account block (`jaah_routing.plan_block`); only ever set beside a `bedrock:` account.
    bedrock: Optional[dict] = None
    # The SDK agent this task runs (`agent_resolution.Sdk`), as a uuid string: no account and no
    # prompt — the executor issues `fleet_verdicts.agent_launch` (`lease_drain._dispatch_agent`). None otherwise.
    agent_id: Optional[str] = None


class RepoDispatchPlan(BaseModel):
    # The display name; `project_id` is the identity.
    project: str
    project_id: Optional[int] = None
    # `owner/repo` for a GitHub project and `""` for every other forge (D-A13: the field an old reader keys
    # on names nothing it could clone). `repo_path` is every forge's identity; `clone_url` what a node
    # clones (`app/forges.py::RepoRef`).
    owner_repo: Optional[str] = None
    clone_url: Optional[str] = None
    forge: Optional[str] = None
    repo_path: Optional[str] = None
    # The four `projects` columns dispatch is decided from (`base` = `integration_branch`); they ride the
    # plan rather than being re-read by the executor, so one pass sees one value.
    base: Optional[str] = None
    branch_prefix: str = ""
    gate_quick_cmd: str = ""
    gate_full_cmd: str = ""
    # This node's `task`-held slots across projects at pass entry (`fleet_decide.count_active`).
    active: int = 0
    ready: int = 0
    account: Optional[str] = None
    dispatches: list[DispatchDecision] = []
    skipped_reason: str = ""
    # `Project.has_repo`. False: a folder project, planned with no forge, base or gate — the git
    # fields above are filled only when the project still holds a resolvable repository (its old git slots
    # may still be finishing), and otherwise stay None. The slot taken, not this, decides each dispatch.
    has_repo: bool = True


@dataclass
class DispatchPassFacts:
    """Node-independent facts of one drain pass, read once and shared by every node's plan.

    `waiting`: the ids of projects with any dispatchable Task when `fleet_allocation` ran (see
    `build_dispatch_plan`). `live_by_project` / `allowed_by_task`: memos of `pool_state.live_nodes_for_project`
    (only for a pass without `hosting_by_project`) and the task's placement-grant node uuids (strings, C10),
    filled by whichever node asks first. Plain data, never ORM rows, so the
    node workers (one session each) can share them; a racing double fill computes the same value. Every
    input moves within a pass anyway, which is why the allocation table is shared the same way."""

    waiting: frozenset[int]
    live_by_project: dict[int, list[str]] = field(default_factory=dict)
    allowed_by_task: dict[int, Optional[frozenset[str]]] = field(default_factory=dict)
    # `_account_candidates`' fleet-wide reads: each org's allowed (provider, auth kind) pairs, and the DB
    # clock that judges every quota verdict.
    allowed_pairs_by_org: dict = field(default_factory=dict)
    db_now: Optional[datetime] = None
    # `fleet_assigned_runner_counts` read once before any node leases, so this pass's own leases are counted
    # by `account_runners` alone: a per-node read taken after another node committed would count them twice.
    # None (a direct plan) reads per node.
    runner_counts: Optional[dict[UUID, int]] = None
    # `{task id: node uuids}` its launches kept failing on (`launch_failures.excluded_node_ids`), read once.
    failed_on: Optional[dict[int, frozenset[str]]] = None
    # `{account id: dispatches}` every node's plan of this pass has decided, under `account_lock`: the nodes
    # plan concurrently, and a per-node tally let two of them spend one account's last place under its cap.
    account_runners: dict[UUID, int] = field(default_factory=dict)
    account_lock: threading.Lock = field(default_factory=threading.Lock)
    # `{(node id, task id): account id}`: which place each still-booked decision holds, so one that does not go
    # out (no slot, a disk or memory wait, a contained raise) gives exactly its own place back (`give_back`).
    account_booked: dict[tuple[str, int], UUID] = field(default_factory=dict)
    # The rest is filled by `read_node_independent` (the drain); None (a direct plan) reads per node.
    # `{waiting project id: hosting node uuid strings}` (`slot_allocation.hosting_node_ids_by_project`): the
    # project half of `pool_state.live_nodes_for_project`. Its node half is each plan's own read of its own row
    # (`pool_state.allocation_roster_clause`), so a node fresh since this was filled is judged fresh.
    hosting_by_project: Optional[dict[int, frozenset[str]]] = None
    # `pool_state.max_observation_age_secs`, a fleet-tier knob: one bound for every node's pool and roster read.
    observation_age_secs: Optional[float] = None
    # Every account row's dispatch columns (`account_rows`), plain rows: `_account_candidates`' enabled set and
    # the drain's provider map and shared slugs (`lease_drain._account_providers`, `_shared_store_facts`).
    accounts: Optional[list] = None
    # `{org id: port_grid.Grid}`, filled by whichever node of the org asks first (`lease_drain._dispatch_node`).
    grid_by_org: dict = field(default_factory=dict)

    def read_node_independent(self, session: Session) -> None:
        """Fill the pass's node-independent reads that every node's plan used to repeat. Each is
        only as old as the pass, which is the bound `waiting` and the allocation already hold every node to."""
        # All three read before any is set: a raise leaves every field None, so each node reads its own.
        hosting = {
            project_id: frozenset(str(n) for n in nodes)
            for project_id, nodes in slot_allocation.hosting_node_ids_by_project(session, self.waiting).items()
        }
        observation_age_secs = pool_state.max_observation_age_secs()
        accounts = account_rows(session)
        self.hosting_by_project, self.observation_age_secs, self.accounts = hosting, observation_age_secs, accounts

    def book(self, node_id: str, task_id: int, account_id: UUID, seen: int) -> bool:
        """Book one place on `account_id` for this decision, if the ledger still reads `seen` for it: the pick
        was judged against that count outside the lock, and a node that booked since makes it stale."""
        with self.account_lock:
            if self.account_runners.get(account_id, 0) != seen:
                return False
            self.account_runners[account_id] = seen + 1
            self.account_booked[(node_id, task_id)] = account_id
            return True

    def give_back(self, node_id: str, task_id: int) -> None:
        """Return the place this decision booked, if it holds one. Idempotent."""
        with self.account_lock:
            account_id = self.account_booked.pop((node_id, task_id), None)
            if account_id is not None:
                self.account_runners[account_id] = self.account_runners.get(account_id, 1) - 1

    def give_back_undispatched(self, node_id: str, dispatched: set[int]) -> None:
        """Give back every place `node_id`'s plan booked for a task not in `dispatched` (the drain's decline)."""
        with self.account_lock:
            keys = [key for key in self.account_booked if key[0] == node_id and key[1] not in dispatched]
        for key in keys:
            self.give_back(*key)

    def snapshot(self) -> dict[UUID, int]:
        with self.account_lock:
            return dict(self.account_runners)


@dataclass
class _RepoPass:
    """One project's read-only context from `build_dispatch_plan`'s phase 1, carried into the
    fleet-wide phase-2 spending walk. `candidates` is `[(task_id, priority)]` in `priority DESC, id`
    order; phase 2 mutates `plan.dispatches` and `assigned`."""

    plan: RepoDispatchPlan
    project: dict
    name: str
    roster: dict
    assigned: set[int]
    candidates: list[tuple[int, Optional[int]]]
    bound_provider_by_task: dict[int, Optional[str]]
    # Each candidate's effective agent (`agent_resolution.resolve_agent`); `bound_provider_by_task` is its
    # built-in narrowing input.
    choice_by_task: dict[int, "agent_resolution.Choice"] = field(default_factory=dict)
    # The project row's id, for the dynamic-worktree question (`_dynamic_index`).
    project_id: Optional[int] = None
    # Slot numbers never picked, each still occupying its index: a row being destroyed
    # (`slot_allocation.retire`), and a free dynamic one, which the next sweep destroys.
    unpickable: set[int] = field(default_factory=set)


def fleet_priority_order(passes: list[_RepoPass]) -> list[tuple[int, _RepoPass]]:
    """Every pass's candidates merged into one list in `priority DESC, id` order (NULL priority as 0, like
    `routes/tasks/policy.py::_PRIORITY_ORDER`), each paired with its pass. Pure, so the rule that decides which
    project's task gets capacity first is tested in isolation (`test_fleet_routes.py`); ids are unique, so
    the merge is deterministic."""
    merged = [
        (task_id, priority if priority is not None else 0, repo_pass)
        for repo_pass in passes
        for task_id, priority in repo_pass.candidates
    ]
    merged.sort(key=lambda entry: (-entry[1], entry[0]))
    return [(task_id, repo_pass) for task_id, _priority, repo_pass in merged]


class DispatchPlanIn(BaseModel):
    # Which node is asking, by its uuid: the plan is per-node (the pool read it caps against, the candidate
    # partition), and the fleet maps are uuid-keyed (C10). Empty ⇒ the plan refuses, since an unscoped
    # `active` reads as an idle fleet.
    node_id: str = ""
    # Restrict to one project (its id), else every project.
    project_id: Optional[int] = None


class DispatchPlanOut(BaseModel):
    repos: list[RepoDispatchPlan] = []
    reason: str = ""
    # False ⇒ DO NOT DISPATCH from this plan: without a fresh COMPLETE enumeration an empty read looks like
    # an idle fleet and the plan would over-dispatch. Single home: `app/pool_state.py`.
    pool_state_ok: bool = False
    pool_state_detail: str = ""


# Last pool-state outcome REPORTED: a transition logs once at WARNING, repeats do not. Process-local, never
# reset — a fresh worker re-announces, which can duplicate a line but never swallow a change.
_POOL_STATE_LAST_OUTCOME: object = None


def _log_pool_state_outcome(outcome: object, message: str, *args: object) -> None:
    """Log a pool-state outcome at WARNING on TRANSITION, DEBUG when unchanged.

    `outcome` is the dedup key — the semantic state, never the formatted string.

    CONCURRENCY: unlocked read-modify-write of a module global from threadpool workers. Tolerated because
    the race is one-directional — a transition may log twice, a changed outcome can never compare equal."""
    global _POOL_STATE_LAST_OUTCOME
    if outcome == _POOL_STATE_LAST_OUTCOME:
        log.debug(message, *args)
        return
    _POOL_STATE_LAST_OUTCOME = outcome
    log.warning(message, *args)


def _pool_read(
    session: Session, node_id: str, *, max_age_secs: Optional[float] = None,
) -> tuple[list[dict], bool, str]:
    """`(pool_records, pool_state_ok, pool_state_detail)` — the whole pool read, fail-closed, in one place.

    Every failure arm returns `([], False, <why>)`; the `False` is load-bearing since `count_active([]) == 0`
    reads as an idle fleet. A DB error is rolled back first so the aborted transaction cannot 500 the plan."""
    if not node_id:
        detail = ("the dispatch-plan request named no node: the pool read is per-node and cannot be "
                  "evaluated, so `active` carries no information and nothing is dispatched (fail closed)")
        _log_pool_state_outcome("no-node-id", "pool state NOT evaluated: %s", detail)
        return [], False, detail
    try:
        records = pool_state.pool_records(session, node_id)
        ok, detail = pool_state.pool_state(session, node_id, max_age_secs=max_age_secs)
    except Exception as exc:                      # noqa: BLE001 — see the docstring: a refusal, not a 500
        session.rollback()
        detail = (f"pool read failed for node {node_id} ({type(exc).__name__}: {exc}) — `active` cannot be "
                  "trusted, so nothing is dispatched from this plan (fail closed)")
        # Always WARNING, never memoised: a failed read is not an observed state and must not suppress the
        # next real verdict transition.
        log.warning("pool state NOT evaluated: %s", detail)
        return [], False, detail
    _log_pool_state_outcome(
        ("verdict", ok),
        "pool state %s for node %s (%d pool records): %s",
        "OK — dispatch permitted" if ok else "NOT OK — dispatch refused", node_id, len(records), detail,
    )
    return records, ok, detail


def _slot_bound_to_task(roster: dict, task_id: object) -> Optional[int]:
    """The slot number in `roster` whose `task_id` IS `task_id`, or None — the DB fact `select_slot` rule 2
    binds on; feeds `_account_for_task`'s `pinned` and keeps the task's own held slot out of the excludes.
    Never a branch comparison."""
    for number, record in roster.items():
        if record.get("task_id") is not None and str(record["task_id"]) == str(task_id):
            return number
    return None


class _AllowedNodeNames:
    """Per-pass memo: task id → the node uuid strings this task may be placed on (the fleet maps' key, C10).

    The `None` arm is unreachable (`placement_grants` floors every task at its org) and kept only as
    `pool_state.owns_assignment`'s parameter contract. An EMPTY set means "nothing may place it", never
    "unconfined".

    The one sanctioned site where node uuids (`AllowedObjects.ids`) become the strings `assign_nodes` and
    `live_nodes_for_project` hold: an unconverted `UUID` set fails every membership test and starves every
    restricted task. `grant_memo` is keyed on the task's project, `_names_by_ids` on the resolved id set."""

    def __init__(self, session: Session, grant_memo: dict,
                 shared: Optional[dict[int, Optional[frozenset[str]]]] = None,
                 failed_on: Optional[dict[int, frozenset[str]]] = None,
                 held_back: frozenset[str] = frozenset()) -> None:
        self._session = session
        self._grant_memo = grant_memo
        self._names_by_ids: dict[frozenset, frozenset[str]] = {}
        # `DispatchPassFacts.allowed_by_task`: the same answer for every node of a pass.
        self._shared = shared
        # `{task id: node uuids}` its launches kept failing on (`app/launch_failures.py`): the pass's one
        # read when given, else read lazily once for a direct caller.
        self._failed_on = failed_on
        # Live nodes this pass offers no new run (`fleet_allocation`'s `paused`): no other node for the yield.
        self._held_back = held_back
        self._live_by_project: dict[Optional[int], list[str]] = {}

    def __call__(self, task_id: int) -> Optional[frozenset[str]]:
        if self._shared is not None and task_id in self._shared:
            return self._shared[task_id]
        allowed = placement_grants.allowed_nodes(self._session, task_id, memo=self._grant_memo)
        if allowed.unrestricted:
            names = None
        else:
            names = self._names_by_ids.get(allowed.ids)
            if names is None:
                names = frozenset(str(node_id) for node_id in allowed.ids)
                self._names_by_ids[allowed.ids] = names
        excluded = self._failed_on_names()
        failed_on = excluded.get(task_id)
        if failed_on and names is not None:
            # Left out, not pinned elsewhere: an empty remainder makes the task wait, never fail. The `None`
            # arm (unconfined) is unreachable, so it is left alone rather than enumerated here. A node left out
            # only for the task's own failed launches yields when no other live node open to new runs remains:
            # never strand a task on them (`launch_failures.ExcludedNodes`).
            yielding = getattr(excluded, "launch_failed", {}).get(task_id, frozenset())
            rest = names - failed_on
            if yielding and not (rest.intersection(self._live_for(task_id)) - self._held_back):
                rest = names - (failed_on - yielding)
            names = rest
        if self._shared is not None:
            self._shared[task_id] = names
        return names

    def _live_for(self, task_id: int) -> list[str]:
        """The live node uuids of the task's project (`pool_state.live_nodes_for_project`), once per project."""
        project_id = self._session.scalar(select(Task.project_id).where(Task.id == task_id))
        if project_id not in self._live_by_project:
            self._live_by_project[project_id] = (
                pool_state.live_nodes_for_project(self._session, project_id) if project_id is not None else [])
        return self._live_by_project[project_id]

    def _failed_on_names(self) -> dict[int, frozenset[str]]:
        if self._failed_on is None:
            self._failed_on = launch_failures.excluded_node_ids(self._session)
        return self._failed_on


class _AccountNodes:
    """Per-pass memo: narrows a model task's allowed nodes to the live ones where it has a usable account.

    Without it placement is account-blind: a task lands on a node whose own plan must decline it (one that
    has not attested an account yet, say) and waits there while accounts are free elsewhere. The test is
    the node plan's own pick, `_best_account` — grants, vendor, Auto-assign, runner CLI, attestation,
    quota and runner cap — so placement and dispatch agree on everything but this pass's own dispatches,
    which only the node plan counts (`in_pass_runners`). With no such node the set is left unchanged: the
    task waits where it would have, and the node plan names the wait."""

    def __init__(self, session: Session, grant_memo: dict) -> None:
        self._session = session
        self._grant_memo = grant_memo
        self._jaah_memo: dict = {}
        self._org_by_node: dict[str, Optional[UUID]] = {}
        self._usable: dict[tuple, bool] = {}

    def __call__(self, task: Task, provider: Optional[str], allowed: Optional[frozenset[str]],
                 live: list[str]) -> Optional[frozenset[str]]:
        # A Jaah-routed task runs on no account of the org's own.
        if _jaah_task_hold(self._session, task, self._jaah_memo)[0] is not None:
            return allowed
        accounts = placement_grants.allowed_accounts(self._session, task.id, memo=self._grant_memo)
        if accounts.unrestricted:
            # Unreachable (the org floor); `_account_for_task` refuses it, so placement need not narrow.
            return allowed
        # A pinned account is the only one the task may run on: place it where that account is.
        accounts = _account_pin_narrowed(accounts, task.bound_account_id)
        provider = provider or None
        usable = frozenset(
            node for node in live
            if (allowed is None or node in allowed) and self._has_account(node, accounts, provider)
        )
        return usable or allowed

    def _has_account(self, node: str, accounts: AllowedObjects, provider: Optional[str]) -> bool:
        key = (node, accounts.ids, provider)
        if key not in self._usable:
            if node not in self._org_by_node:
                self._org_by_node[node] = node_org(self._session, node)
            self._usable[key] = _best_account(
                self._session, node, node_org_id=self._org_by_node[node], allowed=accounts, provider=provider,
            ) is not None
        return self._usable[key]


@contextmanager
def _candidates_pinned(session: Session):
    """Arm the `_account_candidates` pin for a block; disarm it after only if this block armed it, since
    `fleet_allocation` also runs inside a dispatch plan's own pass."""
    armed_here = _CANDIDATE_PIN not in session.info
    _pin_account_candidates(session)
    try:
        yield
    finally:
        if armed_here:
            _unpin_account_candidates(session)


def owns_candidate(
    node_id: str, live: list[str], allowed_ids: Optional[frozenset[str]], task_id: int,
    hold_node: Optional[str], assignment: dict[int, str], *, node_draining: bool = False,
) -> bool:
    """Does this node get to offer `task_id` this pass? The least-busy allocation, with a pin that says
    yes and a drain that says no above it.

    The pin (any task-held worktree) keeps uncommitted work in one tree; confinement is not consulted
    for a pinned task. The drain arm must be here too: `owns_assignment` hands a node absent from `live`
    everything it is granted, so a draining node would claim the whole queue and
    `lease_drain._disable_drained_nodes` would never fire.

    `node_id`, `live` and `hold_node` (`slot_state.task_hold`'s `node_id`, or None) are node uuid strings. `node_draining` defaults False, the
    dangerous default; a new call site needs a plan-level case like
    `tests/test_node_drain.py::test_the_dispatch_plan_ITSELF_offers_a_draining_node_nothing`.
    `assignment`: `pool_state.assign_nodes`' table, built once per project per pass."""
    if hold_node is not None:
        return hold_node == node_id
    if node_draining:
        return False
    return pool_state.owns_assignment(node_id, live, allowed_ids, task_id, assignment)


def _slots_the_org_cannot_take(
    session: Session, records: list[dict], project_id: int, task_id: int, memo: dict,
) -> list[str]:
    """The slot numbers on this node, in this project, that `task_id`'s org could never lease.

    Picker and taker must read one population: `slot_state.lease` binds `worktrees.org_id`, while
    `fleet_decide.select_slot` ranks a tenancy-blind roster; a disagreement is a silent permanent stall.

    Not redundant with the node floor: that confines NODES on `nodes.org_id`; this asks about the SLOT,
    which may carry another real org (`app/tenancy/keys.py` for the key argument).

    Excludes rather than a filtered roster, so one roster per node per pass serves every candidate. A
    `None` org excludes EVERY slot: a task with no resolvable tenant may take nothing, and
    `placement_grants.unplaceable_reason` turns that into a sentence."""
    key = ("_org_of_task", task_id)
    if key not in memo:
        memo[key] = placement_grants.placement_org(session, task_id)
    org = memo[key]
    # `is not None` on BOTH sides: `None == None` would read two unknowns as a match.
    return [
        str(record["_number"])
        for record in records
        if record.get("_project_id") == project_id
        and int(record["_number"]) > 0
        and not (org is not None and record.get("_org_id") is not None
                 and record["_org_id"] == org)
    ]


# No free static slot, and no dynamic worktree may be made on this node (R6, D1, D10): the task waits `ready`.
SKIP_DYNAMIC_REFUSED = "skip-dynamic-refused"
# A task the Jaah account holds (`app/jaah_routing/routing.py::task_hold`, `app/lease_drain.py`).
SKIP_JAAH_HELD = "skip-jaah-held"
# A task on a node that does not attest the ACP adapter its run needs yet: a Jaah-account lane's
# (`_acp_adapter_missing`) or an account's (`_acp_engine_refusal`). `why` carries the wait sentence.
SKIP_ACP_ADAPTER_MISSING = "skip-acp-adapter-missing"


def _jaah_task_hold(session: Session, task: Any, memo: dict) -> tuple[Any, Optional[str]]:
    """`jaah_routing.task_hold`: `(None, None)` off the Jaah account, and always in an edition without it."""
    if not edition_features.enabled("jaah_ai_credit"):
        return None, None
    from app.jaah_routing import routing as jaah_routing

    return jaah_routing.task_hold(session, task, memo)


def _open_jaah_runner(session: Session, task_row: Any, task_id: int, node_uuid: Optional[UUID], route: Any
                      ) -> tuple[str, dict]:
    """The Jaah run's session row, opened as the plan decides (a node cannot invent one): its account reference
    and the plan's `bedrock` block. Only a route `_jaah_task_hold` gave reaches here."""
    if not edition_features.enabled("jaah_ai_credit"):
        raise RuntimeError("a Jaah route in an edition without the Jaah account")
    from app.jaah_routing import routing as jaah_routing, sessions as jaah_sessions

    row = jaah_sessions.open_runner(
        session, org_id=task_row.org_id, task_id=task_id, project_id=task_row.project_id,
        user_id=placement_grants.launcher_of(session, task_id), node_id=node_uuid,
    )
    block = jaah_routing.plan_block(route, row.id, jaah_routing.task_family(session, task_row))
    return jaah_routing.account_ref(row.id), block


# An SDK agent task this node may not run: the node reports no `agent` launch kind, or its OS cannot
# (`agent_resolution.sdk_capable_nodes`). Only reachable for a held task, which bypasses the allocation.
SKIP_NODE_CANNOT_RUN_AGENTS = "skip-node-cannot-run-agents"
# A task whose agent cannot run (`agent_resolution.Refused`): the drain's prelude spends its attempt
# (`lease_drain._fail_unrunnable_agents`); a plan seeing one in between issues nothing.
SKIP_AGENT_UNRUNNABLE = "skip-agent-unrunnable"


def _dynamic_index(
    session: Session, node_uuid: Optional[UUID], project_id: int, occupied: set[int],
) -> tuple[Optional[int], Optional[str]]:
    """`(index, None)` for a new dynamic worktree on this node, `(None, why)` when none may be made, or
    `(None, None)` when the project is not assigned here, so no dynamic worktree is this node's to make.
    `occupied` is every index the plan knows is taken — the roster (retiring rows included) plus this
    pass's own picks."""
    if node_uuid is None or not slot_allocation.is_assigned(session, node_uuid, project_id):
        return None, None
    return slot_allocation.dynamic_headroom(session, node_uuid, project_id, occupied=occupied)


def _vm_capacity(session: Session, project_id: int, node_ids: list[str]) -> dict[str, Any]:
    """`microvm_placement.capacity`: `{}` where MicroVM is not shipped (the community edition has no MicroVMs)."""
    if not edition_features.enabled("microvm"):
        return {}
    from app import microvm_placement

    return microvm_placement.capacity(session, project_id, node_ids)


def _room_by_node(session: Session, project_id: int, node_ids: list[str]) -> dict[str, int]:
    """`assign_nodes`' room for one project, per node uuid string: its free static slots plus the dynamic
    worktrees it could still make (`slot_allocation.dynamic_room`); a MicroVM node's is the VMs it admits
    (`microvm_placement.capacity`)."""
    room = slot_state.free_slots_by_node(session, project_id)
    for node in node_ids:
        room[node] = room.get(node, 0) + slot_allocation.dynamic_room(session, UUID(node), project_id)
    for node, cap in _vm_capacity(session, project_id, node_ids).items():
        room[node] = cap.room
    return room


def _assign(
    session: Session, project_id: int, task_ids: list[int], project_live: list[str], *,
    allowed_by_task: dict[int, Optional[frozenset[str]]], wait_reasons_out: Optional[dict[int, str]] = None,
    **kwargs: Any,
) -> dict[int, str]:
    """`pool_state.assign_nodes` for one project, with MicroVM capacity a HARD gate (D21).

    `assign_nodes` treats room as a preference (with no room anywhere it still allocates), so a MicroVM node
    with no room, or whose KVM is not usable, is left out of `live` for each task — re-decided per task, so the
    pass's own placements spend the room. A task left with no node at all because of that gets the nodes'
    sentence in `wait_reasons_out`. With no MicroVM node in `project_live` this is `assign_nodes` unchanged.
    """
    room = _room_by_node(session, project_id, project_live)
    gate = _vm_capacity(session, project_id, project_live)
    if not gate:
        return pool_state.assign_nodes(task_ids, project_live, allowed_by_task=allowed_by_task,
                                       free_slots=room, **kwargs)
    room = dict(room)
    table: dict[int, str] = {}
    for task_id in task_ids:
        allowed = allowed_by_task.get(task_id, frozenset())
        open_live = [n for n in project_live if n not in gate or room.get(n, 0) > 0]
        picked = pool_state.assign_nodes([task_id], open_live, allowed_by_task=allowed_by_task,
                                         free_slots=room, **kwargs)
        if task_id in picked:
            node = picked[task_id]
            if room.get(node, 0) > 0:
                room[node] -= 1
            table.update(picked)
            continue
        closed = [n for n in project_live if n in gate and n not in open_live
                  and (allowed is None or n in allowed)]
        if closed and wait_reasons_out is not None:
            wait_reasons_out[task_id] = "; ".join(gate[n].why for n in closed)
    return table


def _tally_key(in_pass_runners: dict[str, int]) -> tuple[tuple[str, int], ...]:
    """`in_pass_runners` as a hashable, order-independent memo key (sorted: insertion order is not a
    different question)."""
    return tuple(sorted(in_pass_runners.items()))


def _pick(
    memo: dict, session: Session, node_id: str, allowed: AllowedObjects, bound_provider: Optional[str],
    in_pass_runners: dict[str, int], *, node_org_id: Optional[UUID],
) -> tuple[Optional[str], bool, Optional[str]]:
    """One task's account answer, shared by both of `_account_for_task`'s arms so pinned and unpinned can
    only differ about the launcher's reach.

    `or None`, not `is not None`: account-reference composers fail closed to `""`, which `lease_drain`'s
    `decision.account or repo.account` would read as unnarrowed.

    The second pick is a diagnosis (would the task have had an account unbound?). The tally is part of
    the key, so misses are O(tasks): anything added to `_best_account` runs once per dispatch."""
    # The tally is in the key, else the first task's answer would put every later task on one account.
    tally = _tally_key(in_pass_runners)
    key = (allowed.ids, bound_provider, tally)
    if key not in memo:
        memo[key] = _best_account(session, node_id, allowed=allowed, provider=bound_provider,
                                  in_pass_runners=in_pass_runners, node_org_id=node_org_id)
    narrowed = memo[key] or None
    if narrowed is not None:
        return narrowed, True, None
    if not bound_provider:
        return None, False, None
    unbound_key = (allowed.ids, None, tally)
    if unbound_key not in memo:
        memo[unbound_key] = _best_account(session, node_id, allowed=allowed, provider=None,
                                          in_pass_runners=in_pass_runners, node_org_id=node_org_id)
    # Vendor wait only when the binding is what emptied the list; otherwise the ordinary reach/quota wait.
    return None, False, (bound_provider if memo[unbound_key] else None)


def _account_for_task(
    session: Session, node_id: str, task_id: int, memo: dict, grant_memo: dict, *, pinned: bool,
    node_org_id: Optional[UUID],
    bound_provider: Optional[str] = None,
    in_pass_runners: Optional[dict[str, int]] = None,
    avoid_account_id: Optional[UUID] = None,
    account_pin: Optional[UUID] = None,
) -> tuple[Optional[str], bool, Optional[str]]:
    """`(the account to annotate, is this task placeable on an account at all, the vendor it is WAITING
    for)` for ONE task. `account_pin` is the task's `bound_account_id`, which the caller holds.

    `avoid_account_id` is the account whose launch just failed to announce
    (`fleet_decide.launch_never_claimed`); it steers the PINNED arm only — rotate when another account in
    the org floor qualifies, else the same account (the relaunch loop is bounded in `app/lease_drain.py`).

    The third member is only set alongside `False`, and only when the BINDING emptied the list: a vendor
    wait is one an operator ends by enabling an account of that vendor, distinct from the reach/quota wait.
    The caller turns it into the decision code, `app/lease_drain.py` into the sentence.

    `_best_account` is stateless (ties go to discovery order), so per-task narrowing cannot perturb
    rotation. `None` means "no account is placeable", never "take the fleet-wide pick".

    `pinned` mirrors `owns_candidate`: a leased task is placed as its hold says, not its launcher's current reach.
    The pin waives that reach, never the org floor: falling through to `RepoDispatchPlan.account` could pick
    another tenant's credential, which `routes/agent/task_secrets.py::get_account_api_key` (no org conjunct) serves."""
    # `None` means "no pass is counting"; normalized once so neither `_pick` arm has to.
    in_pass = in_pass_runners if in_pass_runners is not None else {}
    if pinned:
        org = placement_grants.placement_org(session, task_id)
        if org is None:
            # No tenant, no credential — `False` rather than the unconfined pick, so `unplaceable_reason`
            # emits `org_mismatch` on the same pass.
            return None, False, None
        # A pin waives the launcher's CURRENT reach, never personal-stays-personal: the launcher's own
        # personal accounts and the org's, nobody else's.
        floor = placement_grants.shareable_account_floor(
            session, placement_grants.org_floor(session, "account", org),
            placement_grants.launcher_of(session, task_id),
        )
        # The pin narrows by vendor too. Steer away from the account that just failed to announce when
        # the floor has another; fall back to the full floor so a one-account org still relaunches.
        if avoid_account_id is not None and avoid_account_id in floor:
            narrowed = frozenset(floor) - {avoid_account_id}
            account, has_account, waiting_for = _pick(
                memo, session, node_id,
                _account_pin_narrowed(AllowedObjects(unrestricted=False, ids=narrowed), account_pin),
                bound_provider, in_pass, node_org_id=node_org_id)
            if has_account:
                return account, has_account, waiting_for
        allowed = _account_pin_narrowed(AllowedObjects(unrestricted=False, ids=floor), account_pin)
        return _pick(memo, session, node_id, allowed, bound_provider, in_pass, node_org_id=node_org_id)
    allowed = placement_grants.allowed_accounts(session, task_id, memo=grant_memo)
    if allowed.unrestricted:
        # Unreachable since the org floor; answers `False` so a revived `unrestricted` is a visible skip,
        # not a silent cross-tenant pick.
        log.warning(
            "dispatch-plan: placement answered UNRESTRICTED for task %s, which the org floor is supposed "
            "to make impossible. Refusing rather than falling back to the repo's fleet-wide account pick",
            task_id,
        )
        return None, False, None
    return _pick(memo, session, node_id, _account_pin_narrowed(allowed, account_pin), bound_provider, in_pass,
                 node_org_id=node_org_id)


def _stamp_lane_gating(
    session: Session, project_id: int, unplaced: list[int], gating: set[str], blocked: frozenset[str],
    allowed_node_names: Any, waits_out: dict[int, str],
) -> None:
    """Name the wait of each unplaced task whose every usable node is gating a lane batch, so the pause
    is visible rather than a bare `ready`. Unplaced for any other reason, nothing is written. `blocked` is the
    nodes `launch_disk`/`launch_memory` hold back, which are not usable either way."""
    usable = [n for n in pool_state.live_nodes_for_project(session, project_id) if n not in blocked]
    for task_id in unplaced:
        allowed = allowed_node_names(task_id)
        mine = [n for n in usable if allowed is None or n in allowed]
        if mine and all(n in gating for n in mine):
            waits_out.setdefault(task_id, dispatch_wait.LANE_GATING)


def _blocked_reason(session: Session, node_id: str, memo: dict[str, Optional[str]]) -> Optional[str]:
    """The sentence `launch_disk`/`launch_memory`/`launch_cpu` give for a node they hold back, in the drain's
    order (`lease_drain._dispatch_node`), or None; once per node per pass (`memo`). No `task_id`: an unplaced task
    holds no slot, so it has none of its own to discount."""
    if node_id not in memo:
        memo[node_id] = (launch_disk.wait_reason(session, node_id) or launch_memory.wait_reason(session, node_id)
                         or launch_cpu.wait_reason(session, node_id))
    return memo[node_id]


def _stamp_unplaced(
    session: Session, project_id: int, unplaced: list[int], blocked: frozenset[str], allowed_node_names: Any,
    failed_on: Optional[dict[int, frozenset[str]]], waits_out: dict[int, str], memo: dict[str, Optional[str]], *,
    candidates: Optional[list[str]] = None,
) -> None:
    """Name the wait of each task the allocation left unplaced with no sentence yet: no node runs the
    project, its nodes are not live, none is open to the task, or every one it may use is held back on disk, memory
    or CPU (`blocked`). A task whose launches failed everywhere is `lease_drain._stamp_failed_launch_waits`'.
    `candidates` narrows the project's live nodes to those the tasks can run on at all (an SDK agent's);
    `memo` is `_blocked_reason`'s, one per pass."""
    unplaced = [t for t in unplaced if t not in waits_out]
    if not unplaced:
        return
    live = pool_state.live_nodes_for_project(session, project_id)
    if candidates is not None:
        live = [n for n in live if n in candidates]
    if not live:
        why = dispatch_wait.NODES_OFFLINE if slot_allocation.hosting_node_ids(session, project_id) else (
            dispatch_wait.NO_NODE)
        waits_out.update({task_id: why for task_id in unplaced})
        return
    for task_id in unplaced:
        allowed = allowed_node_names(task_id)
        mine = [n for n in live if allowed is None or n in allowed]
        if not mine:
            if not (failed_on or {}).get(task_id):
                waits_out[task_id] = dispatch_wait.NO_ALLOWED_NODE
            continue
        if all(n in blocked for n in mine):
            reason = next((r for r in (_blocked_reason(session, n, memo) for n in mine) if r), None)
            if reason is not None:
                waits_out[task_id] = reason


def fleet_allocation(
    session: Session, *, waiting_out: Optional[set[int]] = None,
    failed_on: Optional[dict[int, frozenset[str]]] = None,
    allowed_out: Optional[dict[int, Optional[frozenset[str]]]] = None,
    wait_reasons_out: Optional[dict[int, str]] = None,
    demand_out: Optional[dict[UUID, int]] = None,
) -> dict[int, str]:
    """`{task_id: node uuid string}` for every waiting task in the fleet — the pass's whole allocation, built once.

    Once, outside the project loop: `app/lease_drain.py` plans nodes concurrently on moving inputs, and
    `slot_state.lease` keys on the slot, not the task, so only one shared table keeps nodes from both
    leasing one task; and the spread ledger (`booked`) must span the fleet. Every project, never
    `body.project_id`. Pinned tasks are not in the walk. Sorted by name, then id, for reproducibility.

    `waiting_out` collects the id of every project with any dispatchable Task, pinned ones included: the
    one read of that fact per pass, which `build_dispatch_plan` reuses to skip the rest per node.
    `failed_on` is the pass's `launch_failures.excluded_node_ids`; `allowed_out`, when given, is filled with each
    task's allowed node uuids (that exclusion applied) for the drain to share with every node's plan.
    `wait_reasons_out`, when given, is filled with `{task id: sentence}` for each task left unplaced for a reason the
    pass can name — every usable node a MicroVM node with no room or no usable KVM (`_assign`), no node able to
    run agents (`dispatch_wait.NO_SDK_NODE`), its node gating a lane batch (`dispatch_wait.LANE_GATING`), and every
    other unplaced task's cause (`_stamp_unplaced`: no node, nodes not live, none open to it, or each held back on
    disk, memory or CPU) — and the drain stamps it as the task's wait reason.
    `demand_out`, when given, is filled with each live per-agent trial's agents wanted this pass
    (`org_limits.DispatchBudget.demand`), which the drain keeps as the trial's peak (`app/trial_agents.py`).

    A node `app/launch_disk.py` blocks (disk-paused, or below the launch disk floor) or `app/launch_memory.py`
    blocks (memory pressure, or at its runner ceiling) is offered no task here: it would only hold one waiting on
    disk or memory while another node has room. Nor is a node gating a lane batch (`merge_lane.gating_node_ids`)
    offered a model task until that gate ends. A task already holding a slot there is not in this walk. A lane run is placed on the most idle cores (`pool_state.assign_nodes`'
    `free_cores`)."""
    # Keyed by id: a name repeats across orgs (`uq_projects_org_name`).
    row_by_id = {p.id: p for p in session.scalars(select(Project)).all()}
    projects = [{"id": p.id, "name": p.name, "repo_url": p.repo_url} for p in row_by_id.values()]
    enrolled = sorted(fleet_decide.enrolled_projects(projects), key=lambda p: (p["name"], p["id"]))
    if not enrolled:
        if edition.IS_CE:
            ce_caps.record_held_back({})
        return {}
    # Fleet-wide reads once per pass, over `live_nodes` (a superset of every project's set).
    live = pool_state.live_nodes(session)
    node_busyness = pool_state.busyness_by_node(session, live)
    node_occupancy = slot_state.occupied_slots_by_node(session)
    node_cores = pool_state.cores_by_node(session, live)
    # Read once per pass so an operator edit never applies halfway through one table.
    band_pct = pool_state.cpu_tie_band_pct()
    # The shared booking ledger — mutated by every call below (reason 2 above).
    booked: dict = {}
    grant_memo: dict = {}
    # Disk, memory and CPU hold a node back the same way (`launch_disk`, `launch_memory`, `launch_cpu`).
    low_resources = (
        launch_disk.ids_blocked(session, live) | launch_memory.ids_blocked(session, live)
        | launch_cpu.ids_blocked(session, live)
    )
    # A node gating a lane batch takes no NEW model run until the gate ends: the runners waiting
    # on that merge outrank the next one's build. Lane runs themselves are still placed there.
    gating = merge_lane_state.gating_node_ids(session)
    paused = low_resources | gating
    allowed_node_names = _AllowedNodeNames(session, grant_memo, shared=allowed_out, failed_on=failed_on,
                                           held_back=frozenset(paused))
    node_free_cores = pool_state.free_cores_by_node(session, live)
    table: dict[int, str] = {}
    # Each org's runner-slot / active-worktree headroom, spent across the whole pass (`org_limits`): a task
    # over it stays `ready` and waits, its reason derived at read (`org_limits.wait_reason`). Spent in
    # fleet-wide `priority DESC, id` order — the dispatch plan's own (`fleet_priority_order`) — so a capped
    # org's first-named project never starves a higher-priority task in another.
    budget = org_limits.DispatchBudget(session)
    # The monthly budgets over 100%, each org's read once for the pass (`app/budgets.py`).
    money_budgets = budgets.Ledger(session)
    account_nodes = _AccountNodes(session, grant_memo)
    # `_stamp_unplaced`'s per-node sentences, read once each for the pass.
    blocked_why: dict[str, Optional[str]] = {}
    walks = []
    for project in enrolled:
        row = row_by_id[project["id"]]
        waiting = dispatchable_tasks(row.id, session)
        if waiting and waiting_out is not None:
            waiting_out.add(row.id)
        # A lane run is placed like any task, but spends no runner budget and is never Jaah-held (it runs no
        # model).
        lanes = [t for t in waiting
                 if merge_lane_state.is_lane_persona(t.persona) and slot_state.task_hold(session, t.id) is None]
        if lanes:
            project_live = pool_state.live_nodes_for_project(session, row.id)
            table.update(_assign(
                session, row.id, [t.id for t in lanes], project_live,
                allowed_by_task={t.id: allowed_node_names(t.id) for t in lanes},
                wait_reasons_out=wait_reasons_out,
                busyness=node_busyness, occupancy=node_occupancy, cores=node_cores, band_pct=band_pct,
                booked=booked, free_cores=node_free_cores, run_cores=launch_failures.LANE_MIN_CORES,
            ))
            if wait_reasons_out is not None:
                _stamp_unplaced(session, row.id, [t.id for t in lanes if t.id not in table],
                                frozenset(), allowed_node_names, failed_on, wait_reasons_out, blocked_why)
        # An SDK agent run is placed like a lane run — no runner budget, never Jaah-held — but only on a node
        # that can run agents; with none, it waits naming why (architecture §3.4). A refused one is the
        # prelude's (`lease_drain._fail_unrunnable_agents`) and is placed nowhere.
        choices = {t.id: agent_resolution.resolve_agent(session, t, row) for t in waiting
                   if not merge_lane_state.is_lane_persona(t.persona)}
        # A run whose git credential its forge reported revoked would only be refused its token (410): it stays
        # `ready`, waiting on one reconnect (`git_access.revoked_holds`, its reason derived at read).
        git_held = git_access.revoked_holds(session, [t for t in waiting if t.id in choices])
        agent_runs = [t for t in waiting if isinstance(choices.get(t.id), agent_resolution.Sdk)
                      and slot_state.task_hold(session, t.id) is None and money_budgets.hold_reason(t) is None
                      and t.id not in git_held]
        if agent_runs:
            project_live = pool_state.live_nodes_for_project(session, row.id)
            # A manifest 2 agent needs a node that runs it under `jaah-acp-host` too, and a registry install one
            # whose host installs it: each class is placed in its own pass.
            for placement in agent_resolution.PLACEMENT_CLASSES:
                runs = [t for t in agent_runs if agent_resolution.placement_class(choices[t.id].agent) == placement]
                if not runs:
                    continue
                capable = agent_resolution.sdk_capable_nodes(session, project_live, placement=placement)
                able = [n for n in project_live if n in capable and n not in paused]
                if able:
                    table.update(_assign(
                        session, row.id, [t.id for t in runs], able,
                        allowed_by_task={t.id: allowed_node_names(t.id) for t in runs},
                        wait_reasons_out=wait_reasons_out,
                        busyness=node_busyness, occupancy=node_occupancy, cores=node_cores, band_pct=band_pct,
                        booked=booked,
                    ))
                elif not capable and wait_reasons_out is not None:
                    # Only when no node can run this pass's agents at all: capable nodes that are merely full or
                    # low on disk or memory keep their own wait, so the sentence never misnames the cause.
                    for t in runs:
                        wait_reasons_out[t.id] = dispatch_wait.no_sdk_node()
                if capable and wait_reasons_out is not None:
                    _stamp_unplaced(session, row.id, [t.id for t in runs if t.id not in table], low_resources,
                                    allowed_node_names, failed_on, wait_reasons_out, blocked_why,
                                    candidates=sorted(capable))
        models = [t for t in waiting if not merge_lane_state.is_lane_persona(t.persona)
                  and isinstance(choices.get(t.id), agent_resolution.Builtin)]
        # A task the Jaah account holds back (`app/jaah_routing/`) will not dispatch, so it spends no budget; nor
        # will one a monthly budget holds (`app/budgets.py`, its reason derived at read like the caps').
        jaah_held = org_limits.jaah_holds(session, models)
        unheld = [t for t in models if slot_state.task_hold(session, t.id) is None and t.id not in jaah_held
                  and money_budgets.hold_reason(t) is None and t.id not in git_held]
        if unheld:
            walks.append((row, waiting, unheld, choices))
    ordered = sorted(
        (t for _, _, unheld, _ in walks for t in unheld),
        key=lambda t: (-(t.priority if t.priority is not None else 0), t.id),
    )
    # Rounds: admit what the budget allows, place it, refund what found no node, and offer the refund to
    # the next tasks in order — so an unplaceable head task never holds the ones behind it back. Each round
    # offers only tasks never offered before, so it ends.
    offered: set[int] = set()
    # `_account_candidates` once per node for the whole allocation (`_AccountNodes`).
    with _candidates_pinned(session):
        while True:
            admitted = set()
            for t in ordered:
                if t.id in offered:
                    continue
                if not budget.take(t.org_id, task_id=t.id):
                    continue
                offered.add(t.id)
                admitted.add(t.id)
            if not admitted:
                break
            for row, waiting, unheld, choices in walks:
                ids = [t.id for t in unheld if t.id in admitted]
                if not ids:
                    continue
                project_live = [n for n in pool_state.live_nodes_for_project(session, row.id)
                                if n not in paused]
                allowed_by_task = {t.id: allowed_node_names(t.id) for t in waiting}
                for t in unheld:
                    if t.id in admitted:
                        allowed_by_task[t.id] = account_nodes(t, choices[t.id].provider, allowed_by_task[t.id],
                                                              project_live)
                # Room is per project (slots are declared per project per node); the reads above are host
                # facts.
                table.update(_assign(
                    session, row.id, ids, project_live,
                    allowed_by_task=allowed_by_task,
                    wait_reasons_out=wait_reasons_out,
                    busyness=node_busyness,
                    occupancy=node_occupancy,
                    cores=node_cores,
                    band_pct=band_pct,
                    booked=booked,
                ))
                if gating and wait_reasons_out is not None:
                    _stamp_lane_gating(session, row.id, [i for i in ids if i not in table], gating, low_resources,
                                       allowed_node_names, wait_reasons_out)
            unplaced = [t for t in ordered if t.id in admitted and t.id not in table]
            if not unplaced:
                break
            for t in unplaced:
                budget.refund(t.org_id)
    if wait_reasons_out is not None:
        for row, _, unheld, _ in walks:
            _stamp_unplaced(session, row.id, [t.id for t in unheld if t.id in offered and t.id not in table],
                            low_resources, allowed_node_names, failed_on, wait_reasons_out, blocked_why)
    if edition.IS_CE:
        ce_caps.record_held_back(budget.held_back())
    if demand_out is not None:
        demand_out.update(budget.demand([(t.org_id, t.id) for t in ordered if t.id in offered and t.id in table]))
    return table


def build_dispatch_plan(
    body: DispatchPlanIn, session: Session, *, allocation: Optional[dict[int, str]] = None,
    pass_facts: Optional[DispatchPassFacts] = None,
) -> DispatchPlanOut:
    """The decision half of the scheduled drain: per repo (or the one named), cap/active/
    dedup, per-candidate eligibility and a lease target, from DB state only. jaahd executes and decides
    nothing. An unusable pool read looks like an idle fleet, so `pool_state_ok` reports it and the
    executor refuses the plan.

    `pass_facts.waiting` comes from the same `fleet_allocation` build as `allocation`: a project outside it
    had no dispatchable Task when the pass began, so its candidate reads are skipped (they were ~10% of the
    api's CPU, re-read per node). Nothing is lost: a task readied since is absent from the
    allocation too, so no live node could have owned it this pass — except a pinned task, whose hold node
    bypasses the allocation — and it dispatches on a later pass. `None` (a direct call with its own table)
    skips nothing. `pass_facts` also carries the pass's shared memos (`DispatchPassFacts`)."""
    project_rows = session.scalars(select(Project)).all()
    # The phase-1 loop reads its rows from this one read, not one SELECT per project per node.
    # Keyed by id: a name repeats across orgs.
    row_by_id = {p.id: p for p in project_rows}
    projects = [{"id": p.id, "name": p.name, "repo_url": p.repo_url} for p in project_rows]
    if body.project_id is not None:
        projects = [p for p in projects if p["id"] == body.project_id]
    enrolled = fleet_decide.enrolled_projects(projects)
    if not enrolled:
        return DispatchPlanOut(
            reason="no projects — nothing to do",
            pool_state_detail="no projects — pool state not evaluated",
        )

    # One freshness bound for this plan's pool and roster reads: the pass's, else this call's own.
    observation_age = (pass_facts.observation_age_secs
                       if pass_facts is not None and pass_facts.observation_age_secs is not None
                       else pool_state.max_observation_age_secs())
    # The asking node's row, read once for org, draining, and whether it may be in any project's allocation
    # roster now (`pool_state.allocation_roster_clause`, the node half of `live_nodes_for_project`).
    node_row = session.execute(
        select(Node.org_id, Node.draining, Node.name, Node.runner_clis, Node.senses_acp_adapters, Node.os,
               Node.arch, Node.microvm,
               and_(*pool_state.allocation_roster_clause(observation_age)).label("in_roster"))
        .where(node_matches(body.node_id))
    ).first()
    node_org_id = node_row.org_id if node_row is not None else None
    # False for a node with no row: `pool_state_ok` and the claim floor stop an unregistered node.
    node_draining = bool(node_row.draining) if node_row is not None else False
    node_in_roster = node_row is not None and node_row.in_roster is True
    # A Jaah-account run's ACP lane needs the node's ACP adapter, which a fresh node is still installing.
    jaah_acp_wait = _acp_adapter_missing(node_row)

    pool_records, pool_ok, pool_detail = _pool_read(session, body.node_id, max_age_secs=observation_age)
    active = fleet_decide.count_active(pool_records)
    if node_org_id is None:
        # A node naming no tenant is refused a plan (200 + `False`, not a 5xx): its org-tier reads cannot be
        # made (`operator_settings.reads._refuse_unscoped_read` would raise), and the plan otherwise looks
        # ordinary, so `pool_state_ok = False` is the entire fence. Separate sentences for "no row" and "no org".
        org_detail = (
            (f"node {body.node_id!r} matches no row in `nodes`, so there is no org to plan its dispatches in")
            if node_row is None else
            (f"node {body.node_id!r} names no org, so a lease into it would be refused as org_mismatch")
        ) + " — nothing is dispatched from this plan (fail closed)"
        # Only when the pool read was fine; otherwise it already carries a more specific refusal.
        if pool_ok:
            pool_detail = org_detail
        _log_pool_state_outcome("no-node-org", "pool state NOT evaluated: %s", org_detail)
        pool_ok = False
    # Two dedup sources unioned: the roster's `session` flag lags a dispatch by a pass. `leased_task_ids`
    # covers every held task; the subtractions below let `_take_for_dispatch` reach `resume`.
    in_flight = fleet_decide.inflight_task_ids(pool_records) | slot_state.leased_task_ids(session)
    # This node's non-live holds are dispatchable back into (`resume`), judged off the CP's own
    # `acked_at`/`dispatched_at` (`fleet_decide.dispatch_is_redispatchable`): a fresh lease has no
    # sighting yet. `leased_account_id` feeds `avoid_account_by_task`.
    _redispatch_candidates = session.execute(
        select(Worktree.task_id, Worktree.acked_at, Worktree.dispatched_at, Worktree.leased_account_id,
               Task.github_synced_at, func.now().label("now"),
               fleet_verdicts.own_unanswered_lease_since().label("lease_unanswered_since"),
               fleet_verdicts.predecessor_gone().label("predecessor_gone"))
        .join(Task, Task.id == Worktree.task_id)
        .where(
            Worktree.node_id == node_ref(body.node_id),
            Worktree.state == slot_state.TASK,
            Worktree.task_id.isnot(None),
            Worktree.session.isnot(True),
        )
    ).all()
    redispatchable = [
        row for row in _redispatch_candidates
        if fleet_decide.dispatch_is_redispatchable(
            acked_at=row.acked_at, dispatched_at=row.dispatched_at, now=row.now,
            lease_unanswered_since=row.lease_unanswered_since, predecessor_gone=row.predecessor_gone,
        )
    ]
    in_flight -= {str(row.task_id) for row in redispatchable}
    # The previous hold's account for launches acked and never claimed; read by the pinned arm.
    avoid_account_by_task: dict[str, UUID] = {
        str(row.task_id): row.leased_account_id
        for row in redispatchable
        if row.leased_account_id is not None and fleet_decide.launch_never_claimed(
            acked_at=row.acked_at, dispatched_at=row.dispatched_at, github_synced_at=row.github_synced_at,
        )
    }
    # Node-scoped: the fleet-wide `auth_rejected` fold would let one host veto an account everywhere.
    # Candidates pinned first so re-asking `_best_account` per grant set costs no repeated reads.
    _pin_account_candidates(session)
    if pass_facts is not None:
        session.info[_PASS_FACTS] = pass_facts
    account = _best_account(session, body.node_id, node_org_id=node_org_id)

    out = DispatchPlanOut(pool_state_ok=pool_ok, pool_state_detail=pool_detail)
    # The shard key: `live_nodes_for_project`, the allocation and holds are keyed by the node uuid string (C10).
    shard_id = body.node_id
    # Per-pass memos collapse picks; `_CANDIDATE_PIN` collapses the reads beneath.
    grant_memo: dict = {}
    allowed_node_names = _AllowedNodeNames(
        session, grant_memo, shared=pass_facts.allowed_by_task if pass_facts is not None else None,
        failed_on=pass_facts.failed_on if pass_facts is not None else None)
    # Keyed on `(grant set, bound vendor, this pass's own assignments)`.
    account_by_grant: dict[
        tuple[frozenset, Optional[str], tuple[tuple[str, int], ...]], Optional[str]
    ] = {}
    # `{account reference: dispatches}` decided this pass (`jaah_scheduler.account_ref` spelling), this node's
    # view of the pass ledger, re-read at each pick (phase 2); without it a node planning four tasks puts all
    # four on one account.
    in_pass_runners: dict[str, int] = {}
    # `app/lease_drain.py` always supplies the allocation (see `fleet_allocation`); this is the fallback
    # for a direct call.
    if allocation is None:
        waiting_projects: set[int] = set()
        allocation = fleet_allocation(session, waiting_out=waiting_projects)
        pass_facts = DispatchPassFacts(waiting=frozenset(waiting_projects))
    # Each waiting project's candidates' holds (`slot_state.task_holds`), one read per project, not one per
    # waiting task. Per plan, not per pass: another node's lease this pass is a hold this plan must see.
    # Each org's Jaah route (`jaah_routing.org_route`), read once per org per pass; and the asking node's uuid,
    # which a Jaah session is bound to.
    jaah_memo: dict = {}
    node_uuid = node_id_or_none(session, body.node_id)
    # Phase 1's task rows, so phase 2 reads none again.
    task_by_id: dict[int, Task] = {}

    # ── PHASE 1 of 2: per-project context. ───────────────────────────────────────────────────────────
    # Nothing here takes a slot. Taking happens after, over all projects' candidates merged into one
    # fleet-wide `priority DESC, id` order, so a high-priority task in a late project is not starved.
    passes: list[_RepoPass] = []
    for project in enrolled:
        name = project["name"]
        # `enrolled` is built from these same rows, so every id resolves.
        row = row_by_id[project["id"]]
        plan = RepoDispatchPlan(project=name, project_id=row.id)
        ref = forges.repo_ref_or_none(row)
        plan.has_repo = row.has_repo
        if plan.has_repo and ref is None:
            plan.skipped_reason = (
                "repo_url names no supported forge repository — set the project's Repo URL (and, for a "
                "self-managed GitLab, its forge) on the project page"
            )
            out.repos.append(plan)
            continue
        plan.active = active
        plan.account = account
        # A folder project reads no forge, base or gate — none of them is required, none refuses. Filled only
        # while it still holds a usable repository, for a git slot of its own still finishing (`has_repo`).
        if row.has_repo or (ref is not None and (row.integration_branch or "").strip()):
            plan.owner_repo = ref.path if ref.forge == "github" else ""
            plan.clone_url = ref.clone_url()
            plan.forge = ref.forge
            plan.repo_path = ref.path
            # The four project columns. An unset base refuses (422).
            base = _integration_branch_or_422(row)
            branch_prefix = row.branch_prefix or ""
            plan.base = base
            plan.branch_prefix = branch_prefix
            # Stripped but not refused (a 422 would abort the whole pass): a whitespace-only gate would
            # otherwise pass `lease_drain`'s emptiness check.
            plan.gate_quick_cmd = (row.gate_quick_cmd or "").strip()
            plan.gate_full_cmd = (row.gate_full_cmd or "").strip()

        if pass_facts is not None and row.id not in pass_facts.waiting:
            # Nothing was dispatchable here at pass start (see the docstring): the repo reports, offers none.
            out.repos.append(plan)
            continue
        # Partitioned across live nodes (fresh enumerations) so no two nodes get one unit; the claim stays
        # the real lock. A waiting task may migrate; a held one never does.
        # Read here for one fact: is THIS node in it (`owns_assignment` takes everything for a node that is
        # not). With the pass's hosting map (the drain) that is "hosts this project" and "its own row, read at
        # this plan's start, is in the roster" — no node is judged by another's freshness, and the list it is
        # handed names only itself, which is all `owns_assignment` reads of it. Otherwise the pass's
        # shared list serves only when it already names this node, else this node reads its own: a node fresh
        # since the list was filled must not be judged by it.
        hosting = pass_facts.hosting_by_project if pass_facts is not None else None
        live_nodes: Optional[list[str]]
        if hosting is not None:
            hosts = hosting.get(row.id, frozenset())
            if node_in_roster and shard_id not in hosts:
                # Assigned to this project since the pass read its map (a console assignment lands mid-pass):
                # the per-node path's own hosting read would see it and judge this node live, not hand it the
                # whole queue as a non-member (`owns_assignment`), so read this one project's hosting fresh.
                hosts = frozenset(str(n) for n in slot_allocation.hosting_node_ids(session, row.id))
            live_nodes = [shard_id] if node_in_roster and shard_id in hosts else []
        else:
            live_nodes = pass_facts.live_by_project.get(row.id) if pass_facts is not None else None
            if live_nodes is None or shard_id not in live_nodes:
                live_nodes = pool_state.live_nodes_for_project(session, row.id, max_age_secs=observation_age)
                if pass_facts is not None and shard_id in live_nodes:
                    pass_facts.live_by_project.setdefault(row.id, live_nodes)
        # Materialised once so the allocation walk and the filter see the same rows.
        waiting = dispatchable_tasks(row.id, session)
        task_by_id.update({t.id: t for t in waiting})
        # Shared by table and predicate, which must agree.
        allowed_by_task = {t.id: allowed_node_names(t.id) for t in waiting}
        holds = slot_state.task_holds(session, [t.id for t in waiting])
        hold_node_by_task = {t.id: (holds.get(t.id) or {}).get("node_id") or None for t in waiting}
        # Each task's effective agent (`agent_resolution`); a built-in's provider is the narrowing input —
        # `bound_provider`, else the project's default built-in. NULL (never dispatched) narrows nothing.
        choice_by_task = {t.id: agent_resolution.resolve_agent(session, t, row) for t in waiting}
        bound_provider_by_task = {
            t.id: (choice_by_task[t.id].provider or None)
            if isinstance(choice_by_task[t.id], agent_resolution.Builtin) else None
            for t in waiting
        }
        # Confinement filters as the list is built, before the resume branch, so a resume cannot land on
        # a revoked node.
        candidates = [
            {"task": t.id}
            for t in waiting
            if owns_candidate(
                shard_id, live_nodes, allowed_by_task[t.id], t.id,
                hold_node_by_task[t.id], allocation, node_draining=node_draining,
            )
        ]
        plan.ready = len(candidates)

        roster = fleet_decide.roster_from_records(pool_records, row.id)
        # The busy set from both observed (roster) and decided (`worktrees.state`) sources, since a fresh
        # dispatch is observed by nobody yet. Node-scoped (A10).
        assigned: set[int] = {
            int(number)
            for number in slot_state.held_slots(session, row.id, node_id=body.node_id)
            if str(number).isdigit()
        } | {
            int(number)
            for number in fleet_decide.held_slots_from_records(pool_records, row.id)
            if str(number).isdigit()
        }
        # Confinement-filtered ids in `waiting` order, each with its priority (phase 2's merge key).
        candidate_ids = {c["task"] for c in candidates}
        passes.append(_RepoPass(
            plan=plan, project=project, name=name, project_id=row.id, roster=roster, assigned=assigned,
            unpickable={
                number for number, record in roster.items()
                if record.get("_retiring")
                or (record.get("_kind") == WORKTREE_KIND_DYNAMIC and record.get("holder") == slot_state.FREE)
                # A free slot of the other kind (git worktree vs folder) is never leased; a held one bound to
                # its task is still resumed into, whatever the project's repositories now are.
                or (record.get("holder") == slot_state.FREE
                    and record.get("_has_repo", True) != row.has_repo)
            },
            candidates=[(t.id, t.priority) for t in waiting if t.id in candidate_ids],
            bound_provider_by_task=bound_provider_by_task,
            choice_by_task=choice_by_task,
        ))
        out.repos.append(plan)

    # The pass's account ledger (`DispatchPassFacts.account_runners`), keyed by account id because a reference
    # (`jaah_scheduler.account_ref`) is a per-node path; a direct call keeps its own. Each pick reads it as this
    # node's `in_pass_runners` OUTSIDE its lock (the pick reads the DB), then books only if the picked account's
    # count has not moved since (`DispatchPassFacts.book`), else picks again: no two nodes spend one account's
    # last place, and no node's DB reads wait on another's.
    ledger = pass_facts if pass_facts is not None else DispatchPassFacts(waiting=frozenset())
    id_by_ref = ({jaah_scheduler.account_ref(c): c["account_id"] for c in _account_candidates(session, body.node_id)}
                 if body.node_id else {})
    ref_by_id = {account_id: ref for ref, account_id in id_by_ref.items()}

    # ── PHASE 2 of 2: take slots, highest priority first, across every project. ────────────────────────
    for task_id, repo_pass in fleet_priority_order(passes):
        plan = repo_pass.plan
        project = repo_pass.project
        roster = repo_pass.roster
        assigned = repo_pass.assigned
        bound_provider_by_task = repo_pass.bound_provider_by_task

        # The slot this task already owns (`task_id` on the roster row, `select_slot` rule 2): used for the
        # account pin and to keep it out of the exclude set.
        own_slot = _slot_bound_to_task(roster, task_id)
        if str(task_id) in in_flight:
            plan.dispatches.append(DispatchDecision(
                task_id=task_id, decision="skip-inflight"))
            continue
        task_row = task_by_id.get(task_id)
        # An SDK agent run: no account, no Jaah route (§4.3).
        choice = repo_pass.choice_by_task.get(task_id)
        if isinstance(choice, agent_resolution.Refused):
            plan.dispatches.append(DispatchDecision(task_id=task_id, decision=SKIP_AGENT_UNRUNNABLE,
                                                    why=choice.reason))
            continue
        agent_id = str(choice.agent.id) if isinstance(choice, agent_resolution.Sdk) else None
        if agent_id is not None and shard_id not in agent_resolution.sdk_capable_nodes(
                session, [shard_id], placement=agent_resolution.placement_class(choice.agent)):
            plan.dispatches.append(DispatchDecision(task_id=task_id, decision=SKIP_NODE_CANNOT_RUN_AGENTS))
            continue
        no_model = agent_id is not None
        # The Jaah arm runs first (`app/jaah_routing/`): an org on the Jaah account never consults its own
        # accounts, has no runner cap and no quota gate, and a held task waits, unbilled. `None` for every other
        # org, whose path below is unchanged.
        jaah_route, jaah_hold = _jaah_task_hold(session, task_row, jaah_memo) if (
            task_row is not None and not no_model) else (None, None)
        if jaah_route is not None and (jaah_hold is not None or node_uuid is None
                                       or task_row.org_id != node_org_id):
            # A hold's own sentence is derived at read (`org_limits.wait_reasons`); a foreign node needs one here.
            plan.dispatches.append(DispatchDecision(
                task_id=task_id, decision=SKIP_JAAH_HELD,
                why=None if jaah_hold is not None else dispatch_wait.JAAH_FOREIGN_NODE))
            continue
        if jaah_route is not None and jaah_acp_wait is not None:
            # Waits, spending no attempt: launched now, the ACP host would die at spawn.
            plan.dispatches.append(DispatchDecision(
                task_id=task_id, decision=SKIP_ACP_ADAPTER_MISSING, why=jaah_acp_wait))
            continue
        # Account confinement: a task whose granted accounts are all blocked waits (D3/D7) and never falls
        # through to the unconfined `repo.account`. Decided before the slot, so a wait costs nothing.
        contended = False
        if jaah_route is not None or no_model:
            task_account, has_account, waiting_for = None, True, None
        else:
            for _attempt in range(_BOOK_ATTEMPTS):
                counts = ledger.snapshot()
                in_pass_runners = {ref_by_id[i]: n for i, n in counts.items() if i in ref_by_id}
                task_account, has_account, waiting_for = _account_for_task(
                    session, body.node_id, task_id, account_by_grant, grant_memo,
                    pinned=own_slot is not None, node_org_id=node_org_id,
                    bound_provider=bound_provider_by_task.get(task_id),
                    in_pass_runners=in_pass_runners,
                    avoid_account_id=avoid_account_by_task.get(str(task_id)),
                    account_pin=task_row.bound_account_id if task_row is not None else None)
                # A reference outside this node's candidates names no account to count.
                account_id = id_by_ref.get(task_account or "")
                if not has_account or account_id is None or ledger.book(
                        str(body.node_id), task_id, account_id, counts.get(account_id, 0)):
                    break
            else:
                # Another node booked under every attempt: wait for the next pass rather than risk the cap.
                task_account, has_account, waiting_for = None, False, None
                contended = True
        if not has_account:
            # A task bound to a vendor with no dispatchable account waits (never another vendor) and names
            # the vendor (R4.2/R4.3); capped, since `tasks.bound_provider` is open `Text`. A node that can
            # launch NO account's CLI is a host fault, not an account wait, and says so.
            if not contended and _task_awaits_engine(
                    session, body.node_id, task_id, grant_memo, bound_provider_by_task.get(task_id),
                    account_pin=task_row.bound_account_id if task_row is not None else None):
                # Waits, spending no attempt, as the Jaah arm above: launched now, the engine would not spawn.
                plan.dispatches.append(DispatchDecision(
                    task_id=task_id, decision=SKIP_ACP_ADAPTER_MISSING,
                    why=dispatch_wait.installing_runner_tools(node_row.name)))
                continue
            if _node_launches_no_cli(session, body.node_id):
                decision = SKIP_NODE_MISSING_RUNNER_CLI
            elif waiting_for:
                decision = f"{SKIP_NO_ACCOUNT_OF_BOUND_PROVIDER}:{waiting_for[:_PROVIDER_ECHO_CAP]}"
            else:
                decision = "skip-no-granted-account"
            why = _CONTENDED_WHY if contended else None
            if decision == "skip-no-granted-account" and own_slot is None and not contended:
                try:
                    why = _no_account_why(session, body.node_id, task_id, grant_memo,
                                          bound_provider_by_task.get(task_id), in_pass_runners,
                                          account_pin=task_row.bound_account_id if task_row else None)
                except Exception:  # noqa: BLE001 — a diagnostic must never cost the node its plan
                    log.exception("dispatch-plan: could not explain the account decline of task %s", task_id)
            plan.dispatches.append(DispatchDecision(task_id=task_id, decision=decision, why=why))
            continue
        # From here a booked place is given back on every path that dispatches nothing, a raise included: held,
        # it would push the rest of the pass's nodes off an account no runner is on. The drain gives back what
        # it declines in turn (`lease_drain._dispatch_node`).
        try:
            # `own_slot` is in `assigned` and must be un-excluded for this candidate so rule 2 can resume into
            # it. Slots this task's org cannot lease are excluded too, `own_slot` included: sparing a
            # cross-tenant held slot would stall the resume forever (knowingly trades A2 for progress).
            # An unpickable slot is excluded too, `own_slot` included: a retiring one's task waits for the
            # destroy's requeue.
            slot = fleet_decide.select_slot(
                roster,
                exclude=([str(i) for i in assigned if i != own_slot]
                         + _slots_the_org_cannot_take(
                             session, pool_records, repo_pass.project_id, task_id, grant_memo)
                         + [str(i) for i in repo_pass.unpickable]),
                task_id=task_id,
            )
            # A resume is what the pick means: rule 2 returned the slot this task's `task_id` already names.
            resume = slot is not None and slot == own_slot
            # No free static slot and no slot of its own: on an assigned node, a dynamic worktree at the lowest free
            # index (R6), or a wait naming why none may be made. Elsewhere the empty pick stands ("no free slot").
            create_dynamic = False
            if slot is None and own_slot is None:
                slot, why = _dynamic_index(session, node_uuid, int(repo_pass.project_id), set(roster) | assigned)
                if why is not None:
                    ledger.give_back(str(body.node_id), task_id)
                    plan.dispatches.append(DispatchDecision(task_id=task_id, decision=SKIP_DYNAMIC_REFUSED, why=why))
                    continue
                create_dynamic = slot is not None
            bedrock_block = None
            if jaah_route is not None and slot is not None:
                # The session row is the CP's, written as it decides (a node cannot invent one); the drain ends it
                # if this decision does not go out.
                task_account, bedrock_block = _open_jaah_runner(session, task_row, task_id, node_uuid, jaah_route)
            plan.dispatches.append(DispatchDecision(
                task_id=task_id, decision="dispatch", lease_target=slot, create_dynamic=create_dynamic,
                resume=resume, account=task_account, bedrock=bedrock_block, agent_id=agent_id))
            if slot is not None:
                assigned.add(slot)
            else:
                # The drain declines a decision with no target ("no free slot"), so its place goes back now.
                ledger.give_back(str(body.node_id), task_id)
        except BaseException:
            ledger.give_back(str(body.node_id), task_id)
            raise

    # The pin's lifetime is this plan, not the session (see `_CANDIDATE_PIN`). The early return runs
    # before arming, and on an exception the caller's `with SessionLocal()` discards `session.info`.
    _unpin_account_candidates(session)
    session.info.pop(_PASS_FACTS, None)
    # Return `out` itself; rebuilding it would drop every field but `repos`.
    return out


# `/task-target` is deleted (D10): the CP names no branch for any path, so it had nothing to compute.


def _max_runners_per_account() -> int:
    """The `max_runners_per_account` operator override, else `jaah_scheduler`'s code default.

    Resolved here and passed in (`jaah_scheduler` does no I/O); one function so the gate and any
    refusal text use the same number. Operator-tier, so no `org`: a per-tenant cap on a shared
    credential account would not bind."""
    return settings_reads.get_int("max_runners_per_account")


# How often a pick is retried after another node booked the picked account between pick and book; past it the
# task waits for the next pass. Each retry follows a real booking elsewhere, so a pass's nodes exhaust it only
# under sustained contention on one account.
_BOOK_ATTEMPTS = 8
_CONTENDED_WHY = "other nodes booked its account's last places while this one picked; retried next pass"


# ── The per-pass pin: one `_account_candidates` evaluation per dispatch-plan request ───────────────────

# `_account_candidates` is a pure function of `(session, node_id)` and was evaluated 1 + k times per pass
# (once per distinct grant set); its cheap reads still drained the DB pool under burst. A pin, not a
# parameter: `test_per_node_account_pick.py` holds `_best_account`/`_account_for_task`'s narrow signatures
# shut. Keyed on the session; armed by `build_dispatch_plan` and by `fleet_allocation` (`_candidates_pinned`,
# which disarms only what it armed), each disarming on its way out, since the drain session outlives both. Safe to share: nothing downstream mutates the list or its dicts.
_CANDIDATE_PIN = "fleet.dispatch_plan.account_candidates"
# The pass's `DispatchPassFacts`, armed beside the pin for `_account_candidates`' fleet-wide reads.
_PASS_FACTS = "fleet.dispatch_plan.pass_facts"


def _pin_account_candidates(session: Session) -> None:
    """Arm the pin for the caller's pass. Idempotent; see `_CANDIDATE_PIN`. Pair with
    `_unpin_account_candidates`: the session may outlive the pass."""
    session.info.setdefault(_CANDIDATE_PIN, {})


def _unpin_account_candidates(session: Session) -> None:
    """Disarm the pin. Idempotent, so it is safe in a `finally` that may not have armed anything."""
    session.info.pop(_CANDIDATE_PIN, None)


# ── Which vendor's CLI an account needs, and whether the asking node has it (PRD R8.3) ─────────────────

#: The provider catalogue indexed by `accounts.provider` value, derived from `agent_providers.PROVIDERS`
#: so it cannot drift.
_PROVIDER_BY_VALUE: dict[str, agent_providers.AgentProvider] = {
    p.value: p for p in agent_providers.PROVIDERS
}


def _node_cli_refusal(provider: str, attested: Optional[list[str]]) -> Optional[str]:
    """Why this account cannot be launched on the asking node, or None when it can (PRD R8.3).

    The reason names the binary (interpolated from the catalogue), so an operator installs that runner CLI on
    that host instead of dispatching into `command not found`.

    `attested` is `nodes.runner_clis` (`app/models/nodes.py::Node.runner_clis`): NULL means never attested and
    asserts nothing, so the account stays eligible (else every pre-upgrade node would be refused); `[]`
    is a real "has none" and refuses. `apps/jaahd/runner_clis.py` never claims a CLI it cannot see.

    A provider this CP's catalogue does not know is refused outright (`accounts.provider` is open), since
    there is no binary or adapter for it."""
    record = _PROVIDER_BY_VALUE.get(provider)
    if record is None:
        return (
            f"provider {provider!r} is not in this control plane's agent-CLI catalogue "
            f"(app/agent_providers.py), so there is no runner CLI to launch it with"
        )
    if attested is None:
        # Never attested: silence, not an empty answer.
        return None
    if record.binary in attested:
        return None
    return (
        f"the node does not have the {record.binary!r} runner CLI that provider {record.value!r} "
        f"needs on the runner's PATH"
    )


def _acp_engine_refusal(provider: str, api_key_slug: str, node_row) -> Optional[str]:
    """Why this account's run would die at spawn on the asking node, or None: the run resolves ACP there
    (`acp_runtime.dispatch_runtime`, as `lease_drain` resolves it) and the node's attested `runner_clis` lacks
    the provider's engine adapter (`acp_binary`). A fresh node installs its adapters in the background
    (`jaah-runner-cli-vn@`), and a Linux node does not downgrade a missing one (`apps/jaahd/acp_runtime.py`).

    Not judged, as `_acp_adapter_missing`: a nodeless caller, a node that never attested (NULL), a daemon that does
    not sense adapters (`Node.senses_acp_adapters`), and a MicroVM node, whose guest is not the host it attests. Nor a
    node off Linux, which runs such a run legacy itself. A blank provider is the default, as `lease_drain` launches it."""
    if (node_row is None or node_row.runner_clis is None or not node_row.senses_acp_adapters
            or node_row.microvm or (node_row.os or node_platform.OS_UBUNTU) != node_platform.OS_UBUNTU):
        return None
    provider = provider or agent_providers.DEFAULT_PROVIDER
    record = _PROVIDER_BY_VALUE.get(provider)
    if record is None or not record.acp_binary or record.acp_binary in node_row.runner_clis:
        return None
    if acp_runtime.dispatch_runtime(provider, api_key_account=api_key_slug,
                                    node_os=node_platform.session_platform(node_row)[0]) != acp_runtime.RUNTIME_ACP:
        return None
    return (f"the node has not installed the {record.acp_binary!r} engine that {record.value!r} runs on yet "
            f"(it installs runner tools in the background)")


def _cli_eligible(candidates: list[dict]) -> list[dict]:
    """`candidates` minus the ones the asking node has no runner CLI for — the eligibility half of R8.3,
    applied by `_best_account` only.

    `_account_candidates` keeps refused candidates with their `node_missing_cli` reason, so a full-list
    reader sees why. Separate from `jaah_scheduler.exclusion_reason`: this is a host fact, and the
    scheduler takes no node.

    Logs at ERROR when it empties a non-empty list: that is a node outage (`lease_drain` would otherwise
    decline with a credential message naming no binary), and this is the one place every pick passes."""
    eligible = [c for c in candidates if not c.get("node_missing_cli")]
    # A node still installing an ACP engine is a wait (`_node_installing_engine`), not the outage this alarms on.
    if candidates and not eligible and not all(c.get("node_installing_engine") for c in candidates):
        log.error(
            "runner-CLI refusal emptied the candidate set: NO account is launchable on this node, so "
            "dispatch to it stops until its `runner_clis` attestation or its installed CLIs are "
            "repaired. Per candidate: %s",
            "; ".join(
                f"{jaah_scheduler.account_ref(c)}: {c.get('node_missing_cli')}" for c in candidates
            ),
        )
    return eligible


def account_rows(session: Session) -> list:
    """Every account row's dispatch columns, ordered by slug, as plain rows (never ORM objects, so one pass's
    node workers can share them — `DispatchPassFacts.accounts`). One column list for the shared and the fresh
    read, so neither can serve a field the other lacks."""
    return session.execute(
        select(Account.id, Account.slug, Account.org_id, Account.enabled, Account.shared, Account.provider,
               Account.auth_kind, Account.auto_assign, Account.max_runners, Account.quota_status,
               Account.quota_status_until)
        .order_by(Account.slug)
    ).all()


def _account_candidates(session: Session, node_id: Optional[str] = None) -> list[dict]:
    """The enabled account set as `gate_and_tier` candidates: id, per-node `config_dir`, the fold's quota
    reading, and the fleet-wide runner count per account.

    `account_id` is the identity (grants, runner counts); `config_dir` is where the credentials live on
    one node, derived (0109). Conflating them collapsed nodeless reads onto one account.

    Quota comes only from the fold (`account_fold.quota_readings*`), never the `accounts` quota columns.
    This reader fails open (blank quota → `cold_start` round-robin), so the named absences below stop
    that. `quota_status`/`quota_status_until` remain the runner-break verdict store
    (`account_fold.resolved_quota_status`).

    Provider keys (PRD R5.1, R8.3), from `app/agent_providers.py`: `provider`, `has_quota_model` (the
    scheduler does no lookups; narrowed per account by the node's `quota_model` word), `api_key_lane`
    (`jaah_scheduler.is_spillover`) and `node_missing_cli` (`_cli_eligible`'s filter). Account keys:
    `auto_assign` (`_auto_assign_narrowed`) and `max_runners` (`jaah_scheduler.runner_cap`)."""
    # The per-pass pin, keyed by node (see `_CANDIDATE_PIN`); a plain miss for other callers.
    pinned = session.info.get(_CANDIDATE_PIN)
    if pinned is not None and node_id in pinned:
        return pinned[node_id]
    facts = session.info.get(_PASS_FACTS)
    # The enabled set, `accounts.enabled IS TRUE`; the pass's rows when the drain read them.
    rows = [row for row in (facts.accounts if facts is not None and facts.accounts is not None
                            else account_rows(session))
            if row.enabled is True]
    runner_counts = (facts.runner_counts if facts is not None and facts.runner_counts is not None
                     else fleet_assigned_runner_counts(session))
    # `config_dir` is per node and acted on (the `lease_slot` intent's directory). Both per-node facts
    # come from one statement so the pin's savings are not undone by a seventh probe.
    node_row = (
        session.execute(
            select(Node.id, Node.name, Node.jaah_home, Node.runner_clis, Node.senses_acp_adapters, Node.os,
                   Node.arch, Node.microvm)
            .where(node_matches(node_id))
        ).first()
        if node_id else None
    )
    # A Multi-Node Host runs no work: every candidate is excluded (`jaah_scheduler.EXCLUSION_REASONS`).
    vnode_host = False
    if edition_features.enabled("multi_node_hosts"):  # The community edition has no Multi-Node Hosts.
        from app import vnodes

        vnode_host = node_row is not None and vnodes.is_host(session, node_row.id)
    jaah_home = node_row.jaah_home if node_row is not None else None
    # NULL (never attested) and `[]` (none) differ (`_node_cli_refusal`); a nodeless caller gets NULL.
    attested_clis = node_row.runner_clis if node_row is not None else None
    # Node-scoped when a node is named: account health is per (account, node), and the fleet-wide
    # `bool_or(rejected)` would let one host veto another. No production caller is nodeless.
    readings = (account_fold.quota_readings_for_node(session, node_id) if node_id
                else account_fold.quota_readings(session))
    # The DB's clock, once: `quota_status_until` is DB `now() + interval`, and one instant judges all rows.
    # An unreadable clock leaves a verdict applied (`account_fold.standing_verdict`).
    db_now = facts.db_now if facts is not None else None
    if db_now is None:
        db_now = account_fold.db_clock(session)
        if facts is not None and db_now is not None:
            facts.db_now = db_now
    # Per org: its provider grid's cells (`app/provider_auth_policy.py`); the pass's
    # memo when there is one, so one read per org serves every node.
    allowed_memo: dict = facts.allowed_pairs_by_org if facts is not None else {}
    candidates = []
    for row in rows:
        # `None` (no node attested it recently: nothing to dispatch against) differs from `{}` (attested,
        # silent on quota: `cold_start`).
        reading = readings.get(row.slug)
        observed = reading if reading is not None else {}
        # One verdict from observation and TTL-gated column (`account_fold.resolved_quota_status`).
        quota_status = account_fold.resolved_quota_status(
            observed.get("quota_status"),
            stored=row.quota_status,
            until=row.quota_status_until,
            db_now=db_now,
        )
        # `resolve_auth_dir` or `""` (no node, or no home), which fails closed against `available_dirs`.
        # Not asked for an API-key account: the CP names no dir for it; the node derives its token-less
        # home itself (`apps/jaahd/fleet_dispatch.py::api_key_account_dir`).
        config_dir = (
            "" if account_auth.is_api_key(row.auth_kind)
            else resolve_auth_dir(jaah_home, row.slug)
        )
        # Catalogue record for `provider`, or None for an unknown value (open vocabulary).
        provider_record = _PROVIDER_BY_VALUE.get(row.provider)
        # Why the node cannot run this account at all: the platform's refusal first (a provider the session's
        # OS cannot run is refused whatever is installed; a MicroVM node's session runs in its Linux guest,
        # `node_platform.session_platform`; a nodeless caller is not narrowed), then the CLI it lacks.
        cli_refusal = (node_platform.session_reason(node_row, node_platform.runner_cli(row.provider))
                       or _node_cli_refusal(row.provider, attested_clis))
        # Else the engine this account's run would spawn here, not installed yet: a wait, not a host fault.
        engine_refusal = None if cli_refusal else _acp_engine_refusal(
            row.provider, row.slug if account_auth.is_api_key(row.auth_kind) else "", node_row)
        candidates.append({
            # The identity every fairness structure keys on.
            "account_id": row.id,
            "config_dir": config_dir,
            # Auth kind: `jaah_scheduler` partitions on it with `api_key_lane` (a spillover API key after
            # the rest, a peer one beside OAuth), exempts API-key rows from quota exclusions and weights them
            # constant. Present on every row.
            "auth_kind": row.auth_kind,
            # Name identity, from which the scheduler composes the `apikey:` reference with no I/O.
            "slug": row.slug,
            # No `account_ref` key: it is derived by `jaah_scheduler.account_ref`, its single home.
            "enabled": row.enabled,
            # The vendor, plus the one capability the pure scheduler needs, resolved here.
            "provider": row.provider,
            # Whether there is a quota to conserve (PRD R5.1). True for an unknown provider, the
            # restrictive direction. False too for an account its node says has NO quota to read
            # (`account_fold.QUOTA_MODEL_KEY`): structural, so it must not be excluded for lacking a reading.
            "has_quota_model": (
                (provider_record.has_quota_model if provider_record is not None else True)
                and observed.get(account_fold.QUOTA_MODEL_KEY) != account_fold.QUOTA_MODEL_NONE
            ),
            # Why this account cannot run on the asking node, or None: a reason, since `_cli_eligible`'s
            # alarm shows it to the operator.
            "node_missing_cli": cli_refusal or engine_refusal,
            # True when that refusal is only the ACP engine still to be installed (`_task_awaits_engine`).
            "node_installing_engine": engine_refusal is not None,
            # Which lane an API-key account rides: "spillover" behind the OAuth set, or "peer" beside it.
            # An unknown provider reads as spillover, the old position (fail-safe).
            "api_key_lane": provider_record.api_key_lane if provider_record is not None else "spillover",
            # May the scheduler pick it for a task that did not ask for its provider (`_auto_assign_narrowed`).
            "auto_assign": row.auto_assign,
            # This account's own runner cap, only for a provider declaring one; None = the fleet's cap.
            "max_runners": (
                row.max_runners
                if provider_record is not None and provider_record.per_account_runner_cap else None
            ),
            "five_hour_percent": observed.get("five_hour_percent"),
            "weekly_percent": observed.get("weekly_percent"),
            "over_quota_spend_percent": observed.get("over_quota_spend_percent"),
            # `gate_and_tier`'s `enabled` filter reads four named absences, computed here so the scheduler
            # stays pure. They are different absences and are not collapsed.

            # (1) A standing verdict: membership in `account_fold.BLOCKING_QUOTA_STATUSES` (historical key
            # name).
            "auth_rejected": quota_status in account_fold.BLOCKING_QUOTA_STATUSES,
            # (1b) The org's provider grid no longer has this account's (provider, auth kind) cell.
            "auth_pair_disallowed": not provider_auth_policy.is_allowed(
                session, row.org_id, row.provider, row.auth_kind, memo=allowed_memo
            ),
            # (2) A reading too old to trust, guarded by `speaks_about_quota` (a node-status-only reading
            # is not a quota fact); without the guard `cold_start` is unreachable.
            "quota_stale": (
                account_fold.speaks_about_quota(observed)
                and account_fold.quota_is_stale(observed.get("quota_updated_at"), now=db_now)
            ),
            # (3) Nobody attests this account: absent must deny, so a total blackout degrades to
            # `"empty"` rather than round-robin.
            "account_unobserved": reading is None,
            # (4) The holding node reports no usable credential: attested but quota-silent, so without
            # this `cold_start` would dispatch a "Not logged in" runner.
            "node_reports_no_credentials": (
                observed.get(account_fold.NODE_STATUS_KEY) == account_fold.NODE_STATUS_NO_CREDENTIALS
            ),
            # Keyed on the account (`worktrees.account_id`, 0109).
            "runners": runner_counts.get(row.id, 0),
            "vnode_host": vnode_host,
            # Not gate inputs: context for a rejection message.
            "quota_updated_at": observed.get("quota_updated_at"),
            "quota_status": quota_status,
        })
    if pinned is not None:
        pinned[node_id] = candidates
    return candidates


def _narrowed(candidates: list[dict], allowed: Optional[AllowedObjects]) -> list[dict]:
    """`candidates` restricted to the accounts `allowed` names: the one narrowing, keyed on `account_id`
    (what a role binding scopes to), never the per-node `config_dir`."""
    if allowed is None or allowed.unrestricted:
        return candidates
    return [c for c in candidates if allowed.contains(c["account_id"])]


#: Decision code (with `:<provider>` appended) for a task waiting on its own bound vendor. Distinct from
#: `skip-no-granted-account` because only an account of that vendor ends it. `app/lease_drain.py` owns
#: the sentence.
SKIP_NO_ACCOUNT_OF_BOUND_PROVIDER = "skip-no-account-of-bound-provider"

#: Decision code for a task the asking node cannot launch on ANY account: every enabled account is refused
#: there for a runner CLI the node does not attest (`_node_cli_refusal`). A host fault, distinct from
#: `skip-no-granted-account`, which reads as an account wait. `app/lease_drain.py` owns the sentence.
SKIP_NODE_MISSING_RUNNER_CLI = "skip-node-missing-runner-cli"


def _acp_adapter_missing(node_row) -> Optional[str]:
    """The wait a Jaah-account task has on this node while the operator's switch puts its lane on ACP and the node's
    attested `runner_clis` lacks the adapter (`agent_providers.JAAH_ACCOUNT_PROVIDER.acp_binary`): a fresh node
    installs its runner tools in the background, and a run routed before the adapter lands dies at spawn. None when
    it may launch. An unattested node (`runner_clis` NULL) is not judged here; `pool_state_ok` and the claim floor
    stop it. Nor is a node whose daemon does not sense adapters (`Node.senses_acp_adapters`): its list never names
    one, so the gate would hold its tasks until it updates; it keeps the behaviour from before the gate."""
    if node_row is None or node_row.runner_clis is None or not node_row.senses_acp_adapters:
        return None
    # The lane's runtime as the dispatch resolves it ON THIS NODE: a node whose OS has no ACP runtime runs legacy and
    # never needs the adapter (`acp_runtime.dispatch_runtime`).
    provider = agent_providers.JAAH_ACCOUNT_PROVIDER
    if acp_runtime.dispatch_runtime(provider.value, api_key_account="", bedrock=True,
                                    node_os=node_platform.session_platform(node_row)[0]) != acp_runtime.RUNTIME_ACP:
        return None
    if provider.acp_binary in node_row.runner_clis:
        return None
    return dispatch_wait.installing_runner_tools(node_row.name)


def _node_launches_no_cli(session: Session, node_id: Optional[str]) -> bool:
    """Does the runner-CLI refusal empty this node's WHOLE candidate set — `_cli_eligible`'s ERROR case?

    Node-wide over every enabled account, not the task's grants: it errs toward an ordinary account wait
    (a node holding one launchable account anywhere is not called a host fault), never toward blaming the
    host for a wait an account would end. Reads the per-pass pin; does not repeat `_cli_eligible`'s log."""
    if not node_id:
        return False
    candidates = _account_candidates(session, node_id)
    return bool(candidates) and all(c.get("node_missing_cli") for c in candidates)


def _task_awaits_engine(
    session: Session, node_id: Optional[str], task_id: int, grant_memo: dict, bound_provider: Optional[str],
    account_pin: Optional[UUID] = None,
) -> bool:
    """Was an account this task could use here refused only because its ACP engine is not installed yet
    (`_acp_engine_refusal`)? Then the task waits on that install, in the words of
    `dispatch_wait.installing_runner_tools`, instead of being told the node's CLI is broken or its accounts are.
    The task's own narrowing, as `_no_account_why`'s, so another task's account never names this task's wait.
    Reads the per-pass pin."""
    if not node_id:
        return False
    allowed = _account_pin_narrowed(
        placement_grants.allowed_accounts(session, task_id, memo=grant_memo), account_pin)
    usable = _auto_assign_narrowed(
        _provider_narrowed(_narrowed(_account_candidates(session, node_id), allowed), bound_provider),
        bound_provider)
    return any(c.get("node_installing_engine") for c in usable)


#: Cap on a provider value in a decision code or human-read line (open-vocabulary `Text`), matching
#: `_compose_provider` and `lease_drain._resolve_account`.
_PROVIDER_ECHO_CAP = 64


def _provider_narrowed(candidates: list[dict], bound_provider: Optional[str]) -> list[dict]:
    """`candidates` restricted to the vendor this task is bound to (PRD R4.1/R4.2), `_narrowed`'s sibling.

    Narrows the list only, never the gate or tier (E8). An unbound task (NULL until first dispatch) is
    untouched (R4.4). An emptied list is a wait, never another vendor (R4.2). Compares the raw
    `accounts.provider` string, so an unplaceable binding waits (fail-closed)."""
    if not bound_provider:
        return candidates
    return [c for c in candidates if c.get("provider") == bound_provider]


def _account_pin_narrowed(allowed: AllowedObjects, account_id: Optional[UUID]) -> AllowedObjects:
    """`allowed` restricted to the one account a task is pinned to (`tasks.bound_account_id`),
    `_provider_narrowed`'s sibling on the grant side. Unpinned is untouched. A pin outside `allowed` empties
    it: a wait like any other no-account wait, never another account."""
    if account_id is None:
        return allowed
    if allowed.unrestricted:
        return AllowedObjects(unrestricted=False, ids=frozenset({account_id}))
    return AllowedObjects(unrestricted=False, ids=allowed.ids & {account_id})


def _auto_assign_narrowed(candidates: list[dict], provider: Optional[str]) -> list[dict]:
    """`candidates` minus the accounts whose Auto-assign is off, when the request named no provider.

    The single home of the Auto-assign rule: an account with it off runs only work that asked for its
    provider (a task's `bound_provider`, or a console's provider choice), and every account of the named
    provider is kept regardless. A task is unpinned until its first claim stamps `bound_provider`
    (`routes/tasks/dispatch.py::_bound_provider_stamp`), so a retry or resume stays on that provider even after
    Auto-assign is turned off — intended. A candidate omitting the key is assignable."""
    if provider:
        return candidates
    return [c for c in candidates if c.get("auto_assign", True)]


def _best_account(
    session: Session, node_id: str, *, node_org_id: Optional[UUID],
    allowed: Optional[AllowedObjects] = None,
    oauth_only: bool = False, provider: Optional[str] = None,
    in_pass_runners: Optional[dict[str, int]] = None,
    apply_runner_cap: bool = True,
) -> Optional[str]:
    """The current best account pick (stateless), or None when none qualifies: the account a dispatch
    launches against (`app/lease_drain.py`) and the one a `clauded` console opens on.

    `node_id` is required: `config_dir` and account health are per node, and the result is acted on.
    `spend_gate=False` (time >> cost). Thresholds are read here (`jaah_scheduler` does no I/O)
    for `node_org_id`, which the caller passes (hoisted once per pass); `None` is the org-less read.

    `allowed` and `provider` narrow the candidate list only, before the gate (E8): task callers pass
    `placement_grants.allowed_accounts` (launcher reach ∩ org floor); `console.py` passes the node's org floor (`placement_grants.org_floor`).

    Returns the wire reference (`jaah_scheduler.account_ref`): the `config_dir` for OAuth,
    `apikey:<slug>` otherwise. A console consumes either (`routes/console.py::_console_account`);
    `oauth_only` is for a caller that must use the result as a path.

    `in_pass_runners` adds this pass's own dispatches to each candidate's count (on a copy; the pinned
    list is shared), so the cap and fewest-runners tier see decisions the frozen snapshot cannot. Not
    transactional: concurrent node drains can still race, corrected next pass.

    `apply_runner_cap=False` for console/commands: the cap counts fleet runners, and a human session is
    not one (`app/account_runners.py`), so capping it would block it by a limit it never counts toward.
    Separate from `oauth_only` because the questions differ."""
    if not node_id:
        # `dispatch-plan` accepts `node_id=""`, so this is reachable: no node, no account.
        return None
    candidates = _account_candidates(session, node_id)
    if in_pass_runners:
            # A copy per candidate, keyed by the same `account_ref` the pass increments under.
        candidates = [
            {**c, "runners": jaah_scheduler.runner_count(c)
                + in_pass_runners.get(jaah_scheduler.account_ref(c), 0)}
            for c in candidates
        ]
    # Before the gate (E8); `_cli_eligible`'s own alarm is the operator's signal here.
    candidates = _cli_eligible(candidates)
    if oauth_only:
        # Before the gate, like `allowed`: with no API-key candidates the spillover arm has nobody.
        candidates = [c for c in candidates if not account_auth.is_api_key(c.get("auth_kind"))]
    # Both quota gates are org-tier, read for the asking node's org, never the ambient (operator) scope,
    # which would trip `_refuse_unscoped_read` once any org overrides a knob. `None` is the org-less read
    # and raises in that case (unlike the budget read, which is skipped); see
    # `tests/test_operator_settings.py`.
    status, enabled, tier = jaah_scheduler.gate_and_tier(
        # The per-task narrowings (grant, vendor, Auto-assign), before the gate (E8).
        _auto_assign_narrowed(_provider_narrowed(_narrowed(candidates, allowed), provider), provider),
        spend_gate=False,
        high_pct=settings_reads.get_int("quota_high_pct", org=node_org_id),
        weekly_high_pct=settings_reads.get_int("quota_weekly_high_pct", org=node_org_id),
        # `None` applies no cap (see `apply_runner_cap`) and skips the knob read.
        max_runners_per_account=_max_runners_per_account() if apply_runner_cap else None,
    )
    if status == "tier":
        return jaah_scheduler.account_ref(tier[0])
    if status == "cold_start":
        return jaah_scheduler.account_ref(enabled[0])
    return None


def _no_account_why(
    session: Session, node_id: str, task_id: int, grant_memo: dict, bound_provider: Optional[str],
    in_pass_runners: Optional[dict[str, int]], account_pin: Optional[UUID] = None,
) -> str:
    """Why `_best_account` found no account for this unpinned task on this node, tallied per reason
    (`jaah_scheduler.exclusion_reason`), so a `skip-no-granted-account` names its cause rather than five
    possible ones. Read only on a decline; the narrowing mirrors `_best_account`'s."""
    candidates = _account_candidates(session, node_id)
    if in_pass_runners:
        candidates = [
            {**c, "runners": jaah_scheduler.runner_count(c)
                + in_pass_runners.get(jaah_scheduler.account_ref(c), 0)}
            for c in candidates
        ]
    candidates = _cli_eligible(candidates)
    allowed = _account_pin_narrowed(
        placement_grants.allowed_accounts(session, task_id, memo=grant_memo), account_pin)
    granted = _narrowed(candidates, allowed)
    if not granted:
        # Counted within the task's org, never fleet-wide: another org's accounts are not this task's business.
        org = placement_grants.placement_org(session, task_id)
        in_org = _narrowed(candidates, AllowedObjects(
            unrestricted=False, ids=frozenset(placement_grants.org_floor(session, "account", org)))) if org else []
        return f"the launcher is granted none of the org's {len(in_org)} account(s) usable here"
    of_provider = _provider_narrowed(granted, bound_provider)
    if not of_provider:
        return f"{len(granted)} granted, none of its bound vendor {bound_provider[:_PROVIDER_ECHO_CAP]!r}"
    narrowed = _auto_assign_narrowed(of_provider, bound_provider)
    if not narrowed:
        return f"{len(granted)} granted, all with Auto-assign off"
    cap = _max_runners_per_account()
    tally: dict[str, int] = {}
    for c in narrowed:
        reason = jaah_scheduler.exclusion_reason(c, max_runners_per_account=cap) or "over a quota gate"
        # One key for every account at its runner cap: the scheduler's sentence carries per-account numbers.
        if reason.startswith("it is already carrying"):
            reason = "at its runner limit"
        tally[reason] = tally.get(reason, 0) + 1
    return "; ".join(f"{n}x {reason}" for reason, n in sorted(tally.items(), key=lambda kv: -kv[1]))
