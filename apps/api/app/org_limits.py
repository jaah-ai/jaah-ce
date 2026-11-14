"""Per-org caps: what each of the five counts, what governs it, and the refusals built on them.

THE SINGLE HOME of each cap's usage definition. Which value governs a cap is `app/plan_terms.py`'s: the org's
own `orgs.limit_<kind>` (`override`) when non-NULL, else its plan's `billing_plans.limit_<kind>` (`plan`) — save
runner slots on a per-agent plan, which are the agents the org bought (`purchase`), lifted while its trial
is live (`trial`, `plan_terms.trial_agents_of`). In both, NULL on the plan and 0 anywhere mean unlimited. Lowering a cap
only refuses new growth: nothing running is touched. Agents is the one cap on what runs at once: the Active worktrees
cap is gone, so a retained MicroVM of a failed task counts nowhere.

Usage, per kind:
* `nodes` — the org's `nodes` rows, Multi-Node Host rows excluded (a host runs no work), plus every node-
  group line's positive `desired - current` (capacity the control plane will still launch).
* `projects` — `projects` rows.
* `users` — `admin_users` in the org, the system actor excluded.
* `runner_slots` (Agents, terms §7) — the live sessions the org runs at once (`_agents`): distinct tasks HOLDING a
  slot (a `worktrees` row in state `task`, or a task's live MicroVM) whose runner is live — an advisor chat among
  them while it runs, and a `ready` one launching in its slot — plus every console session's live MicroVM. A task
  that keeps its slot with no runner (paused, stopped on an unanswered question, settled) counts nowhere.

A staging org (`app/staging_org.py`) and its parent share ONE dispatch cap — the parent's terms
(`plan_terms.terms_org`): `runner_slots` counts over both (`staging_org.family_ids`), from either side, and the
dispatch budget spends from the parent's ledger. The other four caps count each org's own rows.

Every read filters `org_id` explicitly, so a caller's session may be bound to that org or to the
operator plane. A session bound to one org sees only that org's half of a staging family's usage, so the dispatch
pass (operator plane), `require_headroom` and the Subscription page count both on a fleet session
(`staging_org.fleet_session`). Nothing here writes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Literal, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, not_, or_, select, union
from sqlalchemy.orm import Session, aliased

from app import budgets, dispatch_wait, edition, edition_features, fleet_decide, plan_terms, pool_state, staging_org, task_lifecycle
from app.clock import utcnow
from app.models.identity import AdminUser
from app.models.node_groups import GhaRunnerPool, NodeGroupCapacity
from app.models.nodes import Microvm, Node, NodeOrder
from app.models.orgs import ORG_PENDING, Org
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import VnodeHost, Worktree

log = logging.getLogger("org-limits")

#: The five caps, in the order every surface lists them (`app/models/orgs.py::ORG_LIMIT_COLUMNS` pairs with it, less
#: Agents, whose override is Max agents).
LIMIT_KINDS: tuple[str, ...] = ("runner_slots", "nodes", "projects", "users", "github_runners")

#: Display label and the noun the refusal counts in, (singular, plural), per kind.
_LABELS: dict[str, str] = {
    "runner_slots": "Agents",
    "nodes": "Nodes",
    "projects": "Projects",
    "users": "Users",
    "github_runners": "GitHub Runners",
}
_NOUNS: dict[str, tuple[str, str]] = {
    "runner_slots": ("agent", "agents"),
    "nodes": ("node", "nodes"),
    "projects": ("project", "projects"),
    "users": ("user", "users"),
    "github_runners": ("GitHub Runner", "GitHub Runners"),
}

Source = Literal["override", "plan", "purchase", "trial"]

#: The MicroVM states that hold a task's place (`app/microvm_placement.py::LIVE`).
_HOLDING_VM_STATES = ("provisioning", "running")

#: The caps a staging org shares with its parent (`app/staging_org.py`).
_SHARED_KINDS = ("runner_slots",)

#: The caps an interactive session spends, as a dispatched task does: one Agent.
SESSION_KINDS = ("runner_slots",)

#: The caps that bound what an org runs at once: what a dispatch spends (`DispatchBudget`) and a ready task waits on.
_DISPATCH_KINDS = SESSION_KINDS

#: What an operator-facing and tenant-facing surface says for a pending org's refusal and queued task.
AWAITING_APPROVAL = "Available after approval"
#: A ready task of a tenant org with no node to run it on yet (`_awaits_node`): the setup guide's words, as
#: `routes/org_setup.py` and the console's `lib/welcomeWizard.ts::NODE_WAITING_NOTE` say them.
AWAITING_NODE = "Waiting for your node"




def column(kind: str) -> str:
    """The `orgs` column holding one org's own cap for `kind`."""
    return f"limit_{kind}"


@dataclass(frozen=True)
class Limit:
    """One cap as it governs one org. `limit` None = unlimited; `source` says whether the org's own column
    (`override`), its plan (`plan`), the agents it bought (`purchase`) or its live trial (`trial`) decided it;
    `plan_limit` is what the plan alone says."""

    kind: str
    label: str
    limit: Optional[int]
    source: Source
    usage: int
    plan_limit: Optional[int] = None

    @property
    def remaining(self) -> Optional[int]:
        """Headroom left, never negative; None when unlimited."""
        return None if self.limit is None else max(0, self.limit - self.usage)


# ── Usage ────────────────────────────────────────────────────────────────────────────────────────────


def _count(session: Session, stmt) -> int:
    return int(session.scalar(stmt) or 0)


def parked_hold_clause(dispatched_at, task=Task):
    """A `ready` task's hold dispatched BEFORE the task last went back to `ready`: a paused task Started (or a balance
    resume), an answered question-stopped one, a re-queued attempt. Nothing runs in it until it is relaunched in place,
    so it is not an Agent and must find room under the cap first (`resume_has_room`). A hold leased or re-armed since
    that transition is a launch in flight and counts. `dispatched_at` is the hold's `worktrees.dispatched_at` (set on
    every lease and re-arm, never by an ack); `tasks.github_synced_at` is the transition watermark, compared with the
    skew `fleet_decide.launch_never_claimed` allows the same pair. `task` is the `Task` entity or an alias of it."""
    return and_(
        task.state == task_lifecycle.READY,
        dispatched_at.is_not(None),
        task.github_synced_at > dispatched_at + timedelta(seconds=fleet_decide.WATERMARK_SKEW_SECONDS),
    )


def runner_started(task=Task):
    """`task` (the `Task` entity or an alias of it) has a runner that started and is still live: `active`, or
    `awaiting_response` whose runner was not stopped on the unanswered question."""
    return or_(
        task.state == task_lifecycle.ACTIVE,
        and_(task.state == task_lifecycle.AWAITING_RESPONSE, task.question_runner_stopped_at.is_(None)),
    )


def _launching(dispatched_at, task=Task):
    """`task` is `ready` and its hold is a launch in flight, not a parked restart (`parked_hold_clause`).
    `dispatched_at` None = a MicroVM hold, whose VM is its launch (a paused or stopped task's VM is destroyed, so its
    restart is a new placement)."""
    ready = task.state == task_lifecycle.READY
    return ready if dispatched_at is None else and_(ready, not_(parked_hold_clause(dispatched_at, task)))


def _live_tasks(dispatched_at, *, launching: bool = True):
    """A holding task with a live runner: `active`; `awaiting_response` whose runner was not stopped on the unanswered
    question (`question_runner_stopped_at`, `routes/tasks/awaiting.py::stop_runner_on_unanswered_question`); or
    `ready` launching (`_launching`, so the next pass cannot over-admit past a placement still starting).
    `launching=False` leaves the `ready` launches out: only runners that started."""
    started = runner_started()
    if not launching:
        return started
    return or_(started, _launching(dispatched_at))


def live_runner(task=Task):
    """`task` (the `Task` entity or an alias of it) has a live runner or one launching: `runner_started`, or `ready`
    holding a slot (a `worktrees` row in state `task`, or a holding MicroVM) whose launch is in flight (`_launching`).
    A parked hold waits for its relaunch and does not count. The row-level form of `_live_tasks`, for a count that is
    not joined to the hold: the epic throttle's (`routes/tasks/dispatch.py::_parent_epic_throttled`,
    `EpicThrottleBudget`, `epic_has_room`)."""
    wt = aliased(Worktree)
    vm = aliased(Microvm)
    holds_launch = or_(
        select(wt.id).where(wt.task_id == task.id, wt.state == "task", _launching(wt.dispatched_at, task)).exists(),
        select(vm.id).where(
            vm.task_id == task.id, vm.kind == "task", vm.state.in_(_HOLDING_VM_STATES), _launching(None, task),
        ).exists(),
    )
    return or_(runner_started(task), holds_launch)


def _agents(session: Session, org_ids: list[UUID], *, launching: bool = True) -> int:
    """The orgs' Agents: the tasks holding a slot (a `worktrees` row in state `task`, or a task's MicroVM from
    admission until it is retained or ends, D21) with a live runner (`_live_tasks`, `launching` passed on), plus the
    interactive sessions — live console MicroVMs. A paused, question-stopped, parked or settled task may keep its slot
    but runs nothing, so it counts nowhere: not as running on the agents meter, not against the cap."""
    live = union(
        select(Worktree.task_id).join(Task, Task.id == Worktree.task_id).where(
            Worktree.org_id.in_(org_ids), Worktree.state == "task",
            _live_tasks(Worktree.dispatched_at, launching=launching),
        ),
        select(Microvm.task_id).join(Task, Task.id == Microvm.task_id).where(
            Microvm.org_id.in_(org_ids), Microvm.kind == "task", Microvm.state.in_(_HOLDING_VM_STATES),
            _live_tasks(None, launching=launching),
        ),
    ).subquery()
    tasks = _count(session, select(func.count()).select_from(live))
    consoles = _count(
        session,
        select(func.count()).select_from(Microvm).where(
            Microvm.org_id.in_(org_ids), Microvm.kind == "console", Microvm.state.not_in(("destroyed", "failed")),
        ),
    )
    return tasks + consoles


def parked_task_ids(session: Session, task_ids: Iterable[int]) -> set[int]:
    """Those of `task_ids` whose hold is parked (`parked_hold_clause`)."""
    wanted = sorted({int(t) for t in task_ids})
    if not wanted:
        return set()
    return set(session.scalars(select(Worktree.task_id).join(Task, Task.id == Worktree.task_id).where(
        Worktree.task_id.in_(wanted), Worktree.state == "task", parked_hold_clause(Worktree.dispatched_at),
    )).all())


def agents_capped(session: Session, org_id: UUID) -> bool:
    """Whether `org_id`'s Agents cap bounds anything: read without the org row lock or a count, so a lease of an
    unlimited org asks no further (`lease_drain._cap_refuses_new_lease`)."""
    return _raw_limit(session, _org(session, org_id), "runner_slots") is not None


def resume_has_room(session: Session, org_id: UUID) -> bool:
    """Whether a parked hold of `org_id`'s may relaunch now: its Agents cap has room for one more (`require_headroom`,
    which locks the org row to the caller's commit, so concurrent resumes count one after the other)."""
    try:
        require_headroom(session, org_id, "runner_slots")
    except HTTPException:
        return False
    return True


def live_agents(session: Session, org_id: UUID) -> int:
    """The Agents of `org_id`'s staging family (both orgs: one shared cap): the cap's usage and the meter's running
    count."""
    return _agents(session, staging_org.family_ids(session, org_id))


def started_agents(session: Session, org_id: UUID) -> int:
    """`live_agents` less the launches still in flight (a `ready` hold): the runners that started, which the agents
    meter's peak counts (`DispatchBudget.demand`) — a launch that never starts never raises it."""
    return _agents(session, staging_org.family_ids(session, org_id), launching=False)


def started_agents_by_ledger(session: Session) -> dict[UUID, int]:
    """`{ledger org: started Agents}` (`started_agents`) for every ledger with agents bought (`orgs.agents_bought`, a
    per-agent plan's) and a started runner: a live staging org's folded into its parent's (`staging_org.billing_org_id`).
    Four reads for the whole fleet; one when no org has agents bought."""
    ledgers = set(session.scalars(select(Org.id).where(Org.agents_bought.is_not(None), Org.parent_org_id.is_(None))))
    if not ledgers:
        return {}
    ledger_of = dict(session.execute(select(Org.id, func.coalesce(Org.parent_org_id, Org.id)).where(
        Org.deleted_at.is_(None), or_(Org.id.in_(sorted(ledgers)), Org.parent_org_id.in_(sorted(ledgers))),
    )).all())
    org_ids = sorted(ledger_of)
    holds = union(
        select(Worktree.org_id, Worktree.task_id).join(Task, Task.id == Worktree.task_id).where(
            Worktree.org_id.in_(org_ids), Worktree.state == "task",
            _live_tasks(Worktree.dispatched_at, launching=False),
        ),
        select(Microvm.org_id, Microvm.task_id).join(Task, Task.id == Microvm.task_id).where(
            Microvm.org_id.in_(org_ids), Microvm.kind == "task", Microvm.state.in_(_HOLDING_VM_STATES),
            _live_tasks(None, launching=False),
        ),
    ).subquery()
    out: dict[UUID, int] = {}
    for stmt in (
        select(holds.c.org_id, func.count()).group_by(holds.c.org_id),
        select(Microvm.org_id, func.count()).where(
            Microvm.org_id.in_(org_ids), Microvm.kind == "console", Microvm.state.not_in(("destroyed", "failed")),
        ).group_by(Microvm.org_id),
    ):
        for org_id, n in session.execute(stmt):
            ledger = ledger_of[org_id]
            out[ledger] = out.get(ledger, 0) + int(n)
    return out


def own_runner_slots(session: Session, org_id: UUID) -> int:
    """`org_id`'s own Agents, its staging family's other org left out: a staging org's share."""
    return _agents(session, [org_id])


def _usage(session: Session, org_id: UUID, kind: str, *, slots: Optional[int] = None) -> int:
    """`kind`'s usage. `slots` is an already-counted `runner_slots`."""
    if kind == "runner_slots":
        return live_agents(session, org_id) if slots is None else slots
    if kind == "nodes":
        rows = _count(
            session,
            select(func.count()).select_from(Node).where(
                Node.org_id == org_id, Node.id.not_in(select(VnodeHost.node_id))
            ),
        )
        current = (
            select(func.count())
            .select_from(Node)
            .where(Node.node_group_capacity_id == NodeGroupCapacity.id)
            .correlate(NodeGroupCapacity)
            .scalar_subquery()
        )
        pending = _count(
            session,
            select(func.coalesce(func.sum(func.greatest(NodeGroupCapacity.desired - current, 0)), 0)).where(
                NodeGroupCapacity.org_id == org_id
            ),
        )
        return rows + pending
    if kind == "github_runners":
        return _count(
            session,
            select(func.coalesce(func.sum(GhaRunnerPool.desired_count), 0)).where(
                GhaRunnerPool.org_id == org_id, GhaRunnerPool.desired_state != "absent"
            ),
        )
    if kind == "projects":
        return _count(session, select(func.count()).select_from(Project).where(Project.org_id == org_id))
    if kind == "users":
        return _count(
            session,
            select(func.count()).select_from(AdminUser).where(
                AdminUser.org_id == org_id, AdminUser.is_system.is_(False)
            ),
        )
    raise ValueError(f"unknown limit kind {kind!r}; one of {LIMIT_KINDS}")


# ── The caps ─────────────────────────────────────────────────────────────────────────────────────────


def _org(session: Session, org_id: UUID) -> Org:
    org = session.get(Org, org_id)
    if org is None:
        raise ValueError(f"no org {org_id}")
    return org


class _Entitlement:
    """`org`'s five cap terms (`app/plan_terms.py::caps`), read at most once per call however many caps ask."""

    def __init__(self, session: Session, org: Org) -> None:
        self._session, self._org = session, org
        self._caps: Optional[dict[str, plan_terms.Term[Optional[int]]]] = None

    def term(self, kind: str) -> "plan_terms.Term[Optional[int]]":
        if edition.IS_CE and kind == "runner_slots":
            # The community edition's literal, which no override, plan or purchase moves (`app/ce_caps.py`).
            return plan_terms.Term(value=edition.CE_MAX_AGENTS, source="plan", plan_value=edition.CE_MAX_AGENTS)
        if self._caps is None:
            self._caps = plan_terms.caps(self._session, self._org)
        return self._caps[kind]


def _raw_limit(session: Session, org: Org, kind: str, entitlement: Optional[_Entitlement] = None) -> Optional[int]:
    """`kind`'s effective cap for `org` without counting anything; None = unlimited."""
    return (entitlement or _Entitlement(session, org)).term(kind).value


def _limit(
    session: Session, org: Org, kind: str, *, slots: Optional[int] = None,
    entitlement: Optional[_Entitlement] = None,
) -> Limit:
    term = (entitlement or _Entitlement(session, org)).term(kind)
    return Limit(
        kind=kind,
        label=_LABELS[kind],
        limit=term.value,
        source=term.source,
        usage=_usage(session, org.id, kind, slots=slots),
        plan_limit=term.plan_value,
    )


def effective_limits(session: Session, org_id: UUID) -> list[Limit]:
    """All five caps as they govern `org_id`, in `LIMIT_KINDS` order, each with its current usage."""
    org = _org(session, org_id)
    slots = live_agents(session, org_id)
    entitlement = _Entitlement(session, org)
    return [_limit(session, org, kind, slots=slots, entitlement=entitlement) for kind in LIMIT_KINDS]


def effective_limit(session: Session, org_id: UUID, kind: str) -> Limit:
    """One cap as it governs `org_id`."""
    if kind not in LIMIT_KINDS:
        raise ValueError(f"unknown limit kind {kind!r}; one of {LIMIT_KINDS}")
    return _limit(session, _org(session, org_id), kind)


def limit_reached_detail(limit: Limit) -> str:
    """The one sentence every refusal says when `limit` is full: agents the org bought are raised in Billing."""
    singular, plural = _NOUNS[limit.kind]
    noun = singular if limit.limit == 1 else plural
    if edition.IS_CE:
        return f"Limit reached: {limit.limit} {noun} on this machine."
    if limit.source == "purchase":
        return f"Limit reached: {limit.limit} {noun} bought. Add agents in Billing."
    if limit.source == "trial":
        return f"Trial limit reached: {limit.limit} {noun}."
    return f"Limit reached: {limit.limit} {noun} (your plan's limit). Contact us to raise it."


def lock_org(session: Session, org_id: UUID) -> None:
    """Lock `org_id`'s row to the caller's commit: the serialization every cap check counts under. A door
    that must read its own row's current value after the lock (the GitHub runner pool) takes it first."""
    session.scalar(select(Org.id).where(Org.id == org_id).with_for_update())


def require_headroom(session: Session, org_id: UUID, kind: str, adding: int = 1, *, lock: bool = True) -> None:
    """409 when adding `adding` more of `kind` would take `org_id` past its cap; nothing when it fits or
    the cap is unlimited. Called at every creation door BEFORE the write.

    Locks the org row first (held to the caller's commit), so two concurrent creates in one org count
    one after the other instead of both fitting the last free place. `lock=False` is an early answer before slow
    outside work (a repo made on GitHub) that must not hold the org's lock; the locked check still runs before the
    write."""
    if lock:
        lock_org(session, org_id)
    limit = effective_limit(session, org_id, kind)
    if kind in _SHARED_KINDS:
        family = staging_org.family_ids(session, org_id)
        if len(family) > 1:
            # A staging family shares this cap: one queue (the billing org's row) and a count over both orgs, which
            # this org-bound session cannot see.
            if lock:
                lock_org(session, family[0])
            with staging_org.fleet_session() as fleet:
                limit = effective_limit(fleet, org_id, kind)
    if limit.limit is not None and limit.usage + adding > limit.limit:
        if edition.IS_CE and kind == "runner_slots":
            from app import ce_caps

            raise ce_caps.refusal("agents")
        raise HTTPException(status_code=409, detail=limit_reached_detail(limit))


def refuse_growth_past_cap(session: Session, org_id: UUID, kind: str, *, before: int) -> None:
    """409 when a write ALREADY FLUSHED in this transaction grew `kind`'s usage from `before` past the cap.

    For a door whose usage change is only known once written — a node group's `desired`, where a line's
    still-launching capacity is `desired - current`. A write that grows nothing, or shrinks usage while it
    is still over a lowered cap, is never refused: lowering never takes anything away. The caller's
    transaction is not committed on a refusal (the route raises before its commit)."""
    limit = effective_limit(session, org_id, kind)
    if limit.limit is not None and limit.usage > before and limit.usage > limit.limit:
        raise HTTPException(status_code=409, detail=limit_reached_detail(limit))


def usage(session: Session, org_id: UUID, kind: str) -> int:
    """`kind`'s current usage for `org_id`, as this module counts it."""
    if kind not in LIMIT_KINDS:
        raise ValueError(f"unknown limit kind {kind!r}; one of {LIMIT_KINDS}")
    return _usage(session, org_id, kind)


def _headroom(session: Session, org_id: UUID, kinds: tuple[str, ...]) -> Optional[int]:
    """The smallest remaining headroom over `kinds`, never negative; None when all are unlimited (then
    nothing is counted)."""
    org = _org(session, org_id)
    entitlement = _Entitlement(session, org)
    if all(_raw_limit(session, org, kind, entitlement) is None for kind in kinds):
        return None
    # A parked hold (`parked_hold_clause`) reserves nothing: it competes for a free Agent like a queued task
    # (`resume_has_room` admits its relaunch). Reserving one deadlocked an org whose parked hold could not relaunch
    # yet (an unmet prerequisite, its epic at its throttle) while it held the last free Agent from new work.
    slots = live_agents(session, org_id)
    left = [_limit(session, org, kind, slots=slots, entitlement=entitlement).remaining for kind in kinds]
    bounded = [n for n in left if n is not None]
    return min(bounded) if bounded else None


class DispatchBudget:
    """One dispatch pass's per-org admission ledger: each org's Agents headroom (`_headroom`), read once per org per
    pass, then spent one placement at a time — so a pass never places more of an org's tasks than its
    Agents cap leaves room for, however many projects or nodes it walks.

    Used by the placer (`routes/fleet.py::fleet_allocation`). A task the
    pass then fails to place is `refund`ed. Only NEW placements are budgeted: what already runs is usage,
    never touched, so a lowered cap only holds new work back. A restart relaunching in the slot it kept is admitted
    at its relaunch (`resume_has_room`) and reserves nothing here, so a new placement re-checks the cap at its lease
    (`lease_drain._cap_refuses_new_lease`) and whichever of the two leases first takes the free Agent.

    A staging org spends from its parent's ledger (`billing_org_id`: one agent pool for the two), and places
    nothing while `staging_org.dispatch_hold` holds it (its parent lost the switch or its plan ended).

    It also tallies a per-agent plan's demand (`demand`, the peak `app/trial_agents.py` keeps), trial or paid: the
    tasks the caps refused (`take`'s `task_id`) and each such ledger's Agents at pass start. The pass's own placements
    are not demand: a placement counts once its runner starts (`trial_agents.record_peaks` samples every org's)."""

    def __init__(self, session: Session) -> None:
        self._session = session
        # One ledger per cap per org (`_KINDS`): what `runner_slots` still allows. A NEW placement spends one.
        self._left: dict[tuple[UUID, str], Optional[int]] = {}
        # Each task org's ledger key (its billing org), and None for one `staging_org.dispatch_hold` holds.
        self._ledger: dict[UUID, Optional[UUID]] = {}
        # Per ledger on a per-agent plan: its started Agents at pass start (read once, `_demand_start`); None = not
        # per-agent.
        self._start_slots: dict[UUID, Optional[int]] = {}
        # Per ledger: the tasks a cap refused, and the tasks admitted (a refused task the refund later lets in is
        # demand already counted as placed or as waiting on a node, not on the cap).
        self._capped: dict[UUID, set[int]] = {}
        self._admitted: set[int] = set()

    def _ledger_of(self, org_id: UUID) -> Optional[UUID]:
        if org_id not in self._ledger:
            org = self._session.get(Org, org_id)
            if org is None:
                self._ledger[org_id] = org_id  # `_remaining` holds it and says why
            elif staging_org.dispatch_hold(self._session, org) is not None:
                self._ledger[org_id] = None
            else:
                self._ledger[org_id] = staging_org.billing_org_id(org)
        return self._ledger[org_id]

    def _remaining(self, org_id: UUID, kind: str) -> Optional[int]:
        key = (org_id, kind)
        if key not in self._left:
            try:
                self._left[key] = _headroom(self._session, org_id, (kind,))
            except ValueError:
                # A task naming no `orgs` row: its tasks are held, never the whole pass.
                log.error("org_limits: org %s has no orgs row; holding its tasks this pass", org_id)
                self._left[key] = 0
        return self._left[key]

    _KINDS = _DISPATCH_KINDS

    def _demand_start(self, ledger: UUID) -> Optional[int]:
        """`ledger`'s started Agents (`started_agents`) at pass start when it is on a per-agent plan (agents bought, trial or paid), else None.
        Read off the org's `agents_bought` mirror first, so only a per-agent org pays for its plan row and count."""
        if ledger not in self._start_slots:
            org = self._session.get(Org, ledger)
            per_agent = (
                org is not None and org.agents_bought is not None
                and plan_terms.per_agent(org, plan_terms.plan_row(self._session, org.plan_key))
            )
            self._start_slots[ledger] = started_agents(self._session, ledger) if per_agent else None
        return self._start_slots[ledger]

    def take(self, org_id: Optional[UUID], task_id: Optional[int] = None) -> bool:
        """Spend one placement of `org_id`'s; False, spending nothing, when its caps leave no room — `task_id`, when
        given, then counts in its ledger's demand (`demand`)."""
        if org_id is None:
            return True
        ledger = self._ledger_of(org_id)
        if ledger is None:
            return False
        lefts = {kind: self._remaining(ledger, kind) for kind in self._KINDS}
        if task_id is not None:
            self._demand_start(ledger)
        if any(left is not None and left <= 0 for left in lefts.values()):
            if task_id is not None:
                self._capped.setdefault(ledger, set()).add(task_id)
            return False
        for kind, left in lefts.items():
            if left is not None:
                self._left[(ledger, kind)] = left - 1
        if task_id is not None:
            self._admitted.add(task_id)
        return True

    def demand(self) -> dict[UUID, int]:
        """`{ledger org: agents wanted}` for every per-agent ledger this pass took from: its started Agents at pass
        start plus the tasks a cap refused and no later round admitted. A task admitted this pass, placed or held for a
        node or a worktree, is not counted, nor a launch still in flight: it counts once its runner starts (sampled
        every pass, `trial_agents.record_peaks`); never started, it never counts."""
        out = {ledger: start for ledger, start in self._start_slots.items() if start is not None}
        for ledger, refused in self._capped.items():
            if ledger in out:
                out[ledger] += len(refused - self._admitted)
        return out

    def held_back(self) -> dict[UUID, int]:
        """`{ledger org: tasks}` the caps held back this pass and no later round admitted: the queue at the cap."""
        return {ledger: len(refused - self._admitted) for ledger, refused in self._capped.items()}

    def refund(self, org_id: Optional[UUID]) -> None:
        """Give back a placement `take` spent but the pass did not make."""
        ledger = self._ledger.get(org_id) if org_id is not None else None
        if ledger is None:
            return
        for kind in self._KINDS:
            left = self._left.get((ledger, kind))
            if left is not None:
                self._left[(ledger, kind)] = left + 1


def jaah_holds(session: Session, tasks) -> dict[int, str]:
    """`{task id: wait reason}` for those of `tasks` the Jaah account holds back (`app/jaah_routing/`), each hold
    in its readable sentence (`routing.HELD_SENTENCES`); none in an edition without it."""
    if not edition_features.enabled("jaah_ai_credit"):
        return {}
    from app.jaah_routing import routing as jaah_routing

    return {task_id: jaah_routing.HELD_SENTENCES.get(hold, hold)
            for task_id, hold in jaah_routing.hold_reasons(session, tasks).items()}


def orgs_with_a_coder(session: Session, org_ids: set[UUID]) -> set[UUID]:
    """Those of `org_ids` a coding agent can run for: an enabled `accounts` row of their own, or ON the Jaah account.
    Two reads for a page of orgs. The `no_account` dispatch block (`routes/projects.py`) reads it."""
    from app.models.credentials import Account

    if not org_ids:
        return set()
    with_account = set(session.scalars(
        select(Account.org_id).where(Account.org_id.in_(org_ids), Account.enabled.is_(True)).distinct()
    ).all())
    if not edition_features.enabled("jaah_ai_credit"):
        return with_account
    from app.jaah_routing import routing as jaah_routing

    return with_account | jaah_routing.on_jaah_account_many(session, set(org_ids) - with_account)


def wait_reason(session: Session, task) -> Optional[str]:
    """Why a ready task of this org is not dispatching, or None: the Jaah account holding it
    (`app/jaah_routing/routing.py::HELD_REASONS`) first, then a budget it is over (`app/budgets.py`), then a
    pending org, then an org with no node yet (`_awaits_node`), then a full Agents cap, then a slot the node
    would not build (`dispatch_wait.slot_refusals`), then the
    dispatcher's own last decline (`app/dispatch_wait.py`). A git credential its
    forge revoked (`git_access.revoked_holds`) ranks with the budget, after it."""
    from app import git_access

    return (jaah_holds(session, [task]).get(task.id) or budgets.hold_reasons(session, [task]).get(task.id)
            or git_access.revoked_holds(session, [task]).get(task.id)
            or org_wait_reason(session, task.org_id) or dispatch_wait.slot_refusals(session, [task]).get(task.id)
            or dispatch_wait.fresh_reason(task))


def wait_reasons(session: Session, tasks) -> dict[int, str]:
    """`{task id: reason}` for those of `tasks` their org holds back: one reason read per distinct org
    (it depends on the org alone), and a task that already HOLDS a slot is left out — it is the usage,
    not a task waiting on it — unless that slot is on an offline node, which is its reason
    (`pool_state.held_on_dead_nodes`), or the dispatcher stamped why it declined the hold (`dispatch_wait`).
    Absent = no reason."""
    from app import git_access

    tasks = [t for t in tasks if t.org_id is not None]
    # Read first and answered alone: the drain never plans an offline node, so this hold is the whole story.
    stranded = {
        task_id: dispatch_wait.held_on_offline_node(node)
        for task_id, node in pool_state.held_on_dead_nodes(session, [t.id for t in tasks]).items()
    }
    tasks = [t for t in tasks if t.id not in stranded]
    org_ids = {t.org_id for t in tasks}
    # The page's orgs in one read, held here so `org_wait_reason`'s `session.get` is an identity-map hit (the map
    # holds rows weakly), and the node wait read for all of them at once.
    orgs = {org.id: org for org in session.scalars(select(Org).where(Org.id.in_(sorted(org_ids))))} if org_ids else {}
    # Only where `org_wait_reason` asks it: never for a pending org.
    awaiting = _awaits_node_many(session, [org.id for org in orgs.values() if org.lifecycle != ORG_PENDING])
    by_org = {
        org_id: org_wait_reason(session, org_id, awaiting=awaiting) if org_id in orgs else None for org_id in org_ids
    }
    # One reason per task: the Jaah account's hold, where it holds one, outranks a budget's, which outranks the
    # org's, which outranks the dispatcher's own last decline (no account, no worktree).
    held = jaah_holds(session, tasks)
    over_budget = budgets.hold_reasons(session, [t for t in tasks if t.id not in held])
    revoked = git_access.revoked_holds(session, [t for t in tasks if t.id not in held and t.id not in over_budget])
    now = utcnow()
    # A slot the node would not build is the task's own story, ahead of the dispatcher's generic decline.
    refused = dispatch_wait.slot_refusals(session, tasks)
    reason = {
        t.id: held.get(t.id) or over_budget.get(t.id) or revoked.get(t.id) or by_org[t.org_id]
        or refused.get(t.id) or dispatch_wait.fresh_reason(t, now=now)
        for t in tasks
    }
    waiting = [t for t in tasks if reason[t.id] is not None]
    first_clone = _first_clones(session, tasks, reason) if edition.IS_CE else {}
    if not waiting:
        return {**first_clone, **stranded}
    ids = [t.id for t in waiting]
    # A parked hold (`parked_hold_clause`) is not the usage: it waits on the cap like any queued task.
    holding = set(session.scalars(
        select(Worktree.task_id).where(Worktree.task_id.in_(ids), Worktree.state == "task")
    ).all()) - parked_task_ids(session, ids)
    # A holder still says why its own node declined it (the Jaah account's foreign node, a node that cannot run
    # its agent, the node's memory): the dispatcher's stamp is about the hold, not about a cap it is counted in.
    stamped = {t.id: refused.get(t.id) or dispatch_wait.fresh_reason(t, now=now) for t in waiting if t.id in holding}
    held_waits = {task_id: why for task_id, why in stamped.items() if why}
    return {**{t.id: reason[t.id] for t in waiting if t.id not in holding}, **held_waits, **first_clone, **stranded}


def _first_clones(session: Session, tasks, reason: dict[int, Optional[str]]) -> dict[int, str]:
    """The community edition's tasks of a project whose first clone is under way: a task holds one of its worktrees,
    and its node has reported none of them yet — which no worktree count says (`dispatch_wait.FIRST_CLONE`). A clone
    that fails is the holder's slot refusal instead (`dispatch_wait.slot_refusals`). Only where no other reason, or a
    bare worktree wait, speaks for the task."""
    bare = dispatch_wait.NO_FREE_WORKTREE
    candidates = [t for t in tasks if (reason[t.id] or bare).startswith(bare)]
    projects = {t.project_id for t in candidates if t.project_id is not None}
    if not projects:
        return {}
    rows = session.execute(
        select(Worktree.project_id, Worktree.state, Worktree.observed_at).where(Worktree.project_id.in_(projects))
    ).all()
    held = {row.project_id for row in rows if row.state == "task"}
    built = {row.project_id for row in rows if row.observed_at is not None}
    return {t.id: dispatch_wait.FIRST_CLONE for t in candidates if t.project_id in held - built}


def org_wait_reason(session: Session, org_id: UUID, *, awaiting: Optional[set[UUID]] = None) -> Optional[str]:
    """`wait_reason` for any ready task of `org_id`; None for an org with no row (nothing to say). `awaiting`:
    `_awaits_node_many` already read for a set of orgs including this one."""
    org = session.get(Org, org_id)
    if org is None:
        return None
    held = staging_org.dispatch_hold(session, org)
    if held is not None:
        return held
    if org.lifecycle != ORG_PENDING and (
        org.id in awaiting if awaiting is not None else _awaits_node(session, org.id)
    ):
        return AWAITING_NODE
    entitlement = _Entitlement(session, org)
    if org.lifecycle != ORG_PENDING and all(
        _raw_limit(session, org, kind, entitlement) is None for kind in _DISPATCH_KINDS
    ):
        return None  # unlimited: nothing to count
    if org.lifecycle == ORG_PENDING:
        return AWAITING_APPROVAL
    slots = live_agents(session, org.id)
    for kind in _DISPATCH_KINDS:
        limit = _limit(session, org, kind, slots=slots, entitlement=entitlement)
        if limit.limit is not None and limit.usage >= limit.limit:
            if edition.IS_CE:
                # One machine, one user: no organization or plan to name (`edition.CE_MAX_AGENTS`).
                return f"Waiting: runner limit ({limit.usage}/{limit.limit})"
            if limit.source == "purchase":
                # A per-agent plan: the cap is the agents bought, so the next step is buying more.
                return f"Waiting: agent limit ({limit.usage}/{limit.limit}). Add agents in Billing."
            return f"Waiting: organization runner limit ({limit.usage}/{limit.limit})"
    return None


def _awaits_node(session: Session, org_id: UUID) -> bool:
    """A tenant org with no live node that is either new (it owns no node and no node hosts any of its projects) or
    still having one brought up (an Add-node order or the plan's own node, the setup guide's "Preparing your
    node…"). Read live per org: the dispatcher's `dispatch_wait.no_node`/`NODES_OFFLINE` stamps are per project
    and only while a pass names them, and say "add one" or "offline" where the tenant's node is on its way."""
    return org_id in _awaits_node_many(session, {org_id})


def _awaits_node_many(session: Session, org_ids: Iterable[UUID]) -> set[UUID]:
    """Those of `org_ids` `_awaits_node` holds, each read once for the whole set (a board page's orgs), not per org."""
    from app import slot_allocation
    from app.tenancy.ids import is_tenant_org

    pending = {org_id for org_id in org_ids if is_tenant_org(org_id)}
    if not pending:
        return set()
    pending -= pool_state.orgs_with_live_node(session, pending)
    if not pending:
        return set()
    owning = set(session.scalars(
        select(Node.org_id).where(Node.org_id.in_(pending), pool_state.not_a_vnode_host()).distinct()
    ))
    unowned = pending - owning
    projects = session.execute(
        select(Project.org_id, Project.id).where(Project.org_id.in_(unowned))
    ).all() if unowned else []
    placed = slot_allocation.placement_node_ids_by_project(session, [project_id for _, project_id in projects])
    # An org that owns no node and no node may start any of its projects is new: it waits, whatever is on its way.
    hosted = {org_id for org_id, project_id in projects if placed[project_id]}
    new = pending - owning - hosted
    return new | _node_coming_many(session, pending - new)


def _node_coming_many(session: Session, org_ids: set[UUID]) -> set[UUID]:
    """Those of `org_ids` with a node on its way — an Add-node order the system is bringing up, or the plan's own
    node still provisioning: the orders in one read, the plan node per org left over."""
    # The community edition has no node orders or plan nodes.
    if edition.IS_CE:
        return set()
    if not org_ids:
        return set()
    from app import node_orders, plan_bundles

    ordering = set(session.scalars(
        select(NodeOrder.org_id).where(
            NodeOrder.org_id.in_(org_ids), NodeOrder.stage.in_(node_orders.COMING_STAGES),
        ).distinct()
    ))
    return ordering | {
        org_id for org_id in org_ids - ordering
        if plan_bundles.first_node(session, org_id, tenant=True)[0] == "provisioning"
    }
