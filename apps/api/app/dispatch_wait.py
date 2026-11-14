"""Why the dispatcher declined a `ready` task, kept long enough for the console to say so.

The lease-drain (`app/lease_drain.py::_dispatch_node`) declines a ready task for reasons no org setting
explains: its node is at the runner limit, no account it may use is free, or no worktree is free. Those
declines used to reach only the pass journal (`cron_runs.detail`), so the console showed a bare `ready` for
as long as the wait lasted. Each pass now stamps the sentence on the task (`tasks.dispatch_wait_reason`,
`dispatch_wait_at`) and `app/org_limits.py::wait_reasons` serves it as the task's `wait_reason` while it is
fresh — a stamp older than `FRESH_SECONDS` is a pass that stopped deciding about the task, not a reason.

The stamp is machinery: written with Core, never through the ORM flush, and with `updated_at` held, so a
waiting task does not float to the top of "recently updated" every ten seconds.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Sequence
from uuid import UUID

from sqlalchemy import Integer, column, exists, or_, select, update, values
from sqlalchemy.orm import Session

from app import edition
from app.clock import utcnow
from app.models.tasks import Task
from app.task_lifecycle import READY

#: How long a stamp is a reason. Several drain intervals (10s) plus a slow pass, so a live wait never
#: flickers off between passes.
FRESH_SECONDS = 120
#: An unchanged reason is re-stamped at most this often, bounding the writes a long wait costs.
RESTAMP_SECONDS = 30

#: The sentences, one per decline kind. They lead with `Waiting:` like the org's own reasons, which the
#: console strips for its chip and keeps for its banner.
NO_FREE_WORKTREE = "Waiting: no free worktree"
#: The community edition's project whose repository its node is cloning for the first time (`org_limits._first_clones`).
#: Ends in a period: it clears on its own, so the console appends no "room frees up".
FIRST_CLONE = "Waiting: first clone of this project's repository. Starts once it is done."
#: Every account the task may use is busy or at its quota: a capacity wait, so no period ("room frees up").
NO_ACCOUNT = "Waiting: no coding agent API/subscription available"
#: Every account the task may use has a dead sign-in (`routes/fleet.py::_no_account_why`'s `auth_expired`). Ends
#: in a period: only a re-login ends it, so the console appends no "room frees up".
LOGIN_EXPIRED = "Waiting: every AI account's login expired. Log one in again in Accounts."
#: Every node the task may use is running a merge-lane gate, which takes no new run until it ends
#: (`merge_lane.gating_node_ids`).
LANE_GATING = "Waiting: its node is running the merge lane's gate"
#: An SDK agent task with no node of its project that can run agents (`agent_resolution.sdk_capable_nodes`):
#: a capacity wait, not an unrunnable agent, so no attempt is spent (architecture §3.4). Ends in a period: a node
#: that can run them is a person's to add, so the console appends no "room frees up".
NO_SDK_NODE = "Waiting: no node in this org can run SDK agents yet. Add or upgrade an Ubuntu node."
#: The project's nodes exist but none is live for placement: offline, draining or not reporting slots
#: (`pool_state.live_nodes_for_project`). A disabled node is never in the set (`no_node` says so).
NODES_OFFLINE = "Waiting: this project's nodes are offline or draining"
#: Live nodes run the project, but none of them is open to this task (its starter's node grants).
NO_ALLOWED_NODE = ("Waiting: none of this project's online nodes is open to this task's starter. "
                   "Grant the starter access to a node in Nodes.")
#: A Jaah-account task offered by a node outside its org (a pinned hold, or a node with no row): the Jaah session is
#: bound to the org's own nodes (`routes/fleet.py::build_dispatch_plan`'s Jaah arm).
JAAH_FOREIGN_NODE = ("Waiting: the Jaah account runs only on this organization's own nodes. "
                     "Add a node to this organization.")
#: An SDK agent task still holding a slot (from an earlier built-in attempt) on a node that cannot run agents:
#: the hold pins it there, so Reset frees it for a node that can.
HELD_ON_NODE_WITHOUT_SDK = "Waiting: its worktree is on a node that cannot run SDK agents. Reset the task to move it."


def no_node(project_name: str, group_names: Sequence[str], *, any_candidate: bool) -> str:
    """The project may start work on no node (`slot_allocation.placement_node_ids` empty). `group_names` are the
    node groups in effect (empty for All, Easy Mode, and groups kept while the plan switch is off); `any_candidate`
    says nodes exist there but every one is disabled (`placement_groups.candidate_node_ids`). Names the project and
    its groups, never a node (served at `tasks:view`). Each ends in a period: a person ends the wait."""
    groups = ", ".join(group_names)
    if group_names and any_candidate:
        return (f"Waiting: every node in {project_name}'s node groups ({groups}) is disabled. "
                f"Enable one, or change {project_name}'s Node groups.")
    if group_names:
        return f"Waiting: no node in {project_name}'s node groups ({groups}). Change them in {project_name}'s Node groups."
    if any_candidate:
        return f"Waiting: {project_name}'s nodes are disabled. Enable one on the Nodes page."
    return f"Waiting: {project_name} has no node to run on. Add one on the Nodes page."


def no_node_for(session: Session, project_id: int) -> str:
    """`no_node` for one project, its name, groups in effect and candidates read here."""
    from app import placement_groups  # deferred: placement_groups reads the plan terms
    from app.models.projects import Project

    name = session.scalar(select(Project.name).where(Project.id == int(project_id))) or "This project"
    return no_node(name, placement_groups.project_groups(session, project_id).names_in_effect,
                   any_candidate=bool(placement_groups.candidate_node_ids(session, project_id)))


def held_on_offline_node(node: str) -> str:
    """The task's own worktree is on a node that is not live (`pool_state.held_on_dead_nodes`). A held task
    runs only there, so it waits for the node; Reset frees the slot and discards the tree's unpushed work
    (`routes/tasks/transitions.py::_reset_row`). Ends in a period: the sentence carries its own remedy, so the console
    appends none; its chip shows the first sentence alone (`orgWaitLabel`)."""
    if edition.IS_CE:
        # Its one node: there is nowhere else to run, so no Reset is offered.
        return "Waiting: this machine's node is offline. Starts once it is back."
    return f"Waiting: node {node} is offline. Reset and Start to run elsewhere; discards unpushed work."


def no_sdk_node() -> str:
    """`NO_SDK_NODE`, or the community edition's: its one node, no org to search."""
    if edition.IS_CE:
        return "Waiting: this machine's node cannot run SDK agents yet. It needs Ubuntu and a current Jaah release."
    return NO_SDK_NODE


def held_on_node_without_sdk() -> str:
    """`HELD_ON_NODE_WITHOUT_SDK`, or the community edition's, which offers no Reset: no other node can take it."""
    return no_sdk_node() if edition.IS_CE else HELD_ON_NODE_WITHOUT_SDK


#: How many holders `no_free_worktree` names before it counts the rest.
_HOLDERS_NAMED = 5


def no_free_worktree(stopped_holders: Sequence[int] = ()) -> str:
    """No worktree is free for the task. When stopped (`paused`) tasks hold the node's slots for this project — a
    stopped task keeps its tree for a resume (`task_lifecycle._ACTIVE_TASK_STATES`) — they are named, with the
    remedy, so the queue does not wait silently behind them. The chip shows the first sentence."""
    if not stopped_holders:
        return NO_FREE_WORKTREE
    ids = ", ".join(f"#{task_id}" for task_id in stopped_holders[:_HOLDERS_NAMED])
    more = len(stopped_holders) - _HOLDERS_NAMED
    if more > 0:
        ids += f" and {more} more"
    noun = "task" if len(stopped_holders) == 1 else "tasks"
    return (f"Waiting: a free slot (held by stopped {noun} {ids}). "
            f"Start, Reset or delete a stopped task to free its slot.")


def dynamic_refused(why: Optional[str]) -> str:
    """No worktree may be made for the task on its node yet (`slot_allocation.dynamic_headroom`'s reason), said as
    that reason rather than as a full pool: worktrees are made on demand and no count caps them."""
    return f"Waiting: {why}" if why else NO_FREE_WORKTREE


def low_disk(free_gb: Optional[float], floor_gb: float) -> str:
    """The node's free disk is below the launch floor (`runner_launch_disk_floor_gb`). `None` leaves the free figure
    out (a node on another org's host: it is the host's). Ends in a period: freeing disk is a person's act."""
    if free_gb is None:
        return f"Waiting: node disk low (needs {floor_gb:g} GB free). Free disk on the node."
    return f"Waiting: node disk low ({free_gb:.1f} GB free, needs {floor_gb:g} GB). Free disk on the node."


def disk_paused(used_pct: Optional[float], resume_pct: float) -> str:
    """The disk gate paused the node (`app/node_disk_events.py`); it resumes on its own under `resume_pct`."""
    usage = f"{used_pct:.0f}% full" if used_pct is not None else "full"
    return f"Waiting: node disk {usage}; resumes under {resume_pct:g}%"


def memory_pressure(
    used_pct: Optional[float], critical_pct: float, hold_minutes: float, *,
    free_gb: Optional[float] = None, total_gb: Optional[float] = None,
) -> str:
    """The node's memory reached `critical_pct` within the hold window (`app/launch_gates.py`); it reopens
    once a whole `hold_minutes` passes under that line. `used_pct` is the window's peak; `free_gb` of `total_gb`
    is the machine's memory free now. `None` figures are left out (a node on another org's host: they are the
    host's)."""
    if used_pct is None:
        usage = "full"
    elif free_gb is None or total_gb is None:
        usage = f"{used_pct:.0f}% used"
    else:
        usage = f"peaked at {used_pct:.0f}%, {free_gb:.1f} of {total_gb:.1f} GB free now"
    return f"Waiting: node memory {usage}; resumes after {hold_minutes:g} min under {critical_pct:g}%"


def cpu_saturated(low_pct: Optional[float], saturated_pct: float) -> str:
    """Every recent minute of the node's CPU read at or above `saturated_pct` (`app/launch_gates.py`); it reopens
    as soon as one minute reads under it."""
    usage = f"{low_pct:.0f}%+ busy" if low_pct is not None else "saturated"
    return f"Waiting: node CPU {usage}; resumes under {saturated_pct:g}%"


def node_stalled(low_pct: Optional[float]) -> str:
    """Every recent minute, the node's tasks stalled on memory or disk at least `low_pct` of the time (PSI,
    `app/launch_gates.py`); it reopens as soon as one minute eases. `None` leaves the figure out (another org's
    host)."""
    stall = f" ({low_pct:.0f}%+ stalled)" if low_pct is not None else ""
    return f"Waiting: node short of memory or disk{stall}; resumes when it eases"


def node_task_limit(node: str, cap: int) -> str:
    """The node runs as many tasks as its `max_running_tasks` allows (`app/launch_gates.py`)."""
    noun = "task" if cap == 1 else "tasks"
    return f"Waiting: node {node} runs its limit of {cap} {noun}; starts when one finishes"


def installing_runner_tools(node: str) -> str:
    """The node has not attested a runner tool this task's launch needs yet (a fresh node installs them in the
    background, `jaah-runner-cli-vn@`); the wait ends on the observation that attests it."""
    return f"Waiting: installing runner tools on {node}"


def node_failed_launches(node: str) -> str:
    """No node is left for this task: its runs could not start on these — a lane's node fault, skipped for
    `launch_failures.NODE_FAULT_WINDOW` and then retried. A task's own failed launches (`launch_failures.PER_NODE`)
    yield when no other live node open to new runs remains. No time is promised."""
    return f"Waiting: launches did not start on {node}; will retry"


def node_left(node: str) -> str:
    """No node is left for this task: it was started fresh elsewhere, off these
    (`launch_failures.left_node_ids`), and no other node of its project is online. A plain Start offers them again."""
    if edition.IS_CE:
        return "Waiting: set aside on this machine's node. Start runs it there again."
    return f"Waiting: started elsewhere than {node}, and no other node is online. Start it again to retry there."


def no_account_logged_in(node: str) -> str:
    """Every account the task may use is unattested on its node (`account_unobserved`,
    `node_reports_no_credentials`): the org has accounts, just none logged in there. Ends in a period: the
    sentence carries its own remedy."""
    if edition.IS_CE:
        return "Waiting: no account is logged in on this machine. Log one in to run tasks."
    return f"Waiting: no account is logged in on {node}. Log one in there, or add a node that has one."


def no_account_granted(starter: Optional[str]) -> str:
    """The task's starter may use none of the org's accounts usable here (`_no_account_why`'s `not_granted`). Ends
    in a period: a grant is a person's act, so the console appends no "room frees up"."""
    return f"Waiting: no AI account is open to {starter or 'its starter'}. Grant one in Accounts."


def node_missing_cli(node: str) -> str:
    """The node attests no runner CLI the task's accounts need (`nodes.runner_clis`): a host fault, fixed on the node.
    Ends in a period: the sentence carries its own remedy."""
    return f"Waiting: runner CLI missing on {node}. Re-run the node install."


def no_account_of(provider_label: str) -> str:
    """The task is bound to a vendor and no account of it is free."""
    return f"Waiting: no {provider_label} API/subscription available"


#: The node outcomes that say a slot order was not enacted, plus the CP's `parked` for one refused to the end
#: (`fleet_verdicts.OUTCOME_*`, spelled here so this light module does not import that one at load).
_REFUSED_ORDER_OUTCOMES = ("refused_stale", "refused_precondition", "failed", "parked", "clone_refused")
#: A refused clone retires its order on the one ack, which clears the refusal tally: a reason at once.
_RETIRED_REFUSAL_OUTCOMES = ("clone_refused",)
#: A refusal is the node declining to try, re-served with backoff (`fleet_verdicts.refusal_backoff_secs`): a fresh
#: node task's folder slot is refused "not in this pass's roster" several times until the node's next pass sees it.
#: So a refusal is a reason only once the CP parks it (`REFUSAL_PARK_AFTER`, ~75s of backoff) or the order outlives
#: that budget — a node refusing for changing reasons restarts the count (`fleet_verdicts.refusal_key`) and never
#: parks. A `failed` build is a reason at once.
_TRANSIENT_ORDER_OUTCOMES = ("refused_stale", "refused_precondition")
_TRANSIENT_GRACE = timedelta(seconds=90)
#: The node's words are cut to this many characters: the sentence is a chip and a banner, not a log.
_REFUSAL_ECHO_CAP = 240


#: An epic's reason when its subtasks all wait (`routes/tasks/task_wire.py::_epic_wait_reasons`): the subtask's id
#: leads it, so a reader that may not see that subtask (Live Cast) can drop it (`epic_wait_subtask`).
_EPIC_WAIT = re.compile(r"^Waiting: subtask #(\d+): ")


def epic_wait(subtask_id: int, reason: str) -> str:
    """An epic's reason, held by subtask `subtask_id`'s `reason` ("Waiting: subtask #12: <reason>")."""
    return f"Waiting: subtask #{subtask_id}: {reason.removeprefix('Waiting:').strip()}"


def epic_wait_subtask(reason: Optional[str]) -> Optional[int]:
    """The subtask an `epic_wait` reason names, or None for any other reason."""
    match = _EPIC_WAIT.match(reason or "")
    return int(match.group(1)) if match else None


def slot_refusal(reason: Optional[str]) -> str:
    """The task's slot could not be built: its newest `lease_slot` order was refused by the node. The node's own
    words follow, redacted (`fleet_verdicts.redact_node_detail`) because `wait_reason` is served at `tasks:view`.
    Ends in a period: Reset is its remedy (a fresh worktree), so the console appends none."""
    said = _node_words(reason).rstrip(".")
    told = f"The node said: {said}." if said else "The node gave no reason."
    return f"Waiting: its worktree could not be prepared. {told} Reset the task to retry on a fresh worktree."


def clone_failed(session: Session, task, reason: Optional[str], node_id: Optional[UUID] = None,
                 refused_at: Optional[datetime] = None) -> str:
    """A clone refused on the node — the slot's own, or the ones holding every slot of its project there
    (`held_clone_refusal`). A task's clone authenticates as its starter, so the sentence is the checkout gate's,
    naming them and the fix (`checkout_gate.clone_refusal_sentence`), or the refusing `node_id` when that login
    has cloned elsewhere since `refused_at`; `slot_refusal` when that says nothing."""
    from app.routes.tasks import checkout_gate  # noqa: PLC0415 — the refusal vocabulary's home

    said = checkout_gate.clone_refusal_sentence(session, task, reason, node_id=node_id, refused_at=refused_at)
    return f"Waiting: {said}" if said else slot_refusal(reason)


def _node_words(reason: Optional[str]) -> str:
    from app import fleet_verdicts  # noqa: PLC0415 — a heavy module; only these sentences need it

    return " ".join((fleet_verdicts.redact_node_detail(reason) or "").split())[:_REFUSAL_ECHO_CAP]


def _refused(row, settled_before: datetime) -> bool:
    """Whether a `lease_slot` order row is a refusal worth saying (`_TRANSIENT_ORDER_OUTCOMES`' grace)."""
    if row.outcome in _RETIRED_REFUSAL_OUTCOMES:
        return True
    return (row.outcome in _REFUSED_ORDER_OUTCOMES and (row.refusals or 0) >= 1
            and (row.outcome not in _TRANSIENT_ORDER_OUTCOMES or row.created_at < settled_before))


def _refusal_sentence(session: Session, row, task) -> str:
    """`clone_failed` for a clone refusal of `task` (its row), else `slot_refusal`."""
    from app.routes.tasks import checkout_gate  # noqa: PLC0415 — the refusal vocabulary's home

    if task is not None and checkout_gate.is_clone_refusal(row.reason):
        return clone_failed(session, task, row.reason, row.node_id, row.created_at)
    return slot_refusal(row.reason)


def slot_refusals(session: Session, tasks) -> dict[int, str]:
    """`{task id: slot_refusal}` for those `ready` tasks of `tasks` that hold a slot whose newest `lease_slot`
    order the node failed, or refused past the park budget; a refused clone says `clone_failed`. A task holding no
    slot is past that order (a Reset released it), so it says nothing. One read: Postgres `DISTINCT ON` keeps each
    task's newest order."""
    from app import fleet_verdicts, slot_state  # noqa: PLC0415 — heavy modules; only this read needs them
    from app.models.worktrees import SlotIntent, Worktree  # noqa: PLC0415

    ids = [str(t.id) for t in tasks if t.state == READY]
    if not ids:
        return {}
    task_key = SlotIntent.payload["task_id"].astext
    rows = session.execute(
        select(task_key.label("task_id"), SlotIntent.outcome, SlotIntent.refusals, SlotIntent.reason,
               SlotIntent.created_at, SlotIntent.node_id)
        .where(SlotIntent.verb == fleet_verdicts.VERB_LEASE_SLOT, task_key.in_(ids),
               exists().where(Worktree.task_id == SlotIntent.payload["task_id"].astext.cast(Integer),
                              Worktree.state == slot_state.TASK))
        .distinct(task_key)
        .order_by(task_key, SlotIntent.created_at.desc(), SlotIntent.id.desc())
    ).all()
    settled_before = utcnow() - _TRANSIENT_GRACE
    by_id = {t.id: t for t in tasks}
    return {int(row.task_id): _refusal_sentence(session, row, by_id.get(int(row.task_id)))
            for row in rows if _refused(row, settled_before)}


def held_clone_refusal(session: Session, node_id: UUID, project_id: int) -> Optional[str]:
    """`clone_failed` when a task holding one of this node's slots for `project_id` had its newest `lease_slot`
    order refused as a clone refusal — the slots a "no free worktree" wait queues behind are taken by launches the
    node refuses, so that refusal, not the queue, is the reason (`lease_drain._dispatch_node`). None otherwise."""
    from app import fleet_verdicts, slot_state  # noqa: PLC0415 — heavy modules; only this read needs them
    from app.models.worktrees import SlotIntent, Worktree  # noqa: PLC0415
    from app.routes.tasks import checkout_gate  # noqa: PLC0415 — the refusal vocabulary's home

    holders = [str(task_id) for task_id in session.scalars(
        select(Worktree.task_id).where(Worktree.node_id == node_id, Worktree.project_id == project_id,
                                       Worktree.state == slot_state.TASK, Worktree.task_id.is_not(None))
    )]
    if not holders:
        return None
    # Keyed by the holders' task ids, so the read probes `ix_slot_intents_lease_slot_task`, never the pair's whole
    # lease history.
    task_key = SlotIntent.payload["task_id"].astext
    rows = session.execute(
        select(task_key.label("task_id"), SlotIntent.outcome, SlotIntent.refusals, SlotIntent.reason,
               SlotIntent.created_at)
        .where(SlotIntent.verb == fleet_verdicts.VERB_LEASE_SLOT, task_key.in_(holders),
               SlotIntent.node_id == node_id, SlotIntent.project_id == project_id)
        .distinct(task_key)
        .order_by(task_key, SlotIntent.created_at.desc(), SlotIntent.id.desc())
    ).all()
    settled_before = utcnow() - _TRANSIENT_GRACE
    for row in rows:
        if not (_refused(row, settled_before) and checkout_gate.is_clone_refusal(row.reason)):
            continue
        holder = session.scalars(select(Task).where(Task.id == int(row.task_id))).first()
        said = (checkout_gate.clone_refusal_sentence(session, holder, row.reason, node_id=node_id,
                                                         refused_at=row.created_at)
                if holder is not None else "")
        if said:
            # Another task's refusal: named, so the reader looks there rather than at their own task.
            return f"Waiting: task #{holder.id} holds this project's worktrees and could not clone. {said}"
    return None


def record(session: Session, waits: dict[int, str]) -> None:
    """Stamp each `{task id: sentence}` on its task while it is still `ready`, then commit. Skips a row
    whose reason is unchanged and was stamped within `RESTAMP_SECONDS`.

    One `UPDATE … FROM (VALUES …)` for every row, not one per task: the drain stamps every waiting task each pass."""
    if not waits:
        return
    restamp_before = utcnow() - timedelta(seconds=RESTAMP_SECONDS)
    wanted = values(
        column("task_id", Task.id.type), column("reason", Task.dispatch_wait_reason.type), name="waits",
    ).data([(int(task_id), reason) for task_id, reason in waits.items()])
    session.execute(
        update(Task)
        .where(
            Task.id == wanted.c.task_id,
            Task.state == READY,
            or_(
                Task.dispatch_wait_reason.is_distinct_from(wanted.c.reason),
                Task.dispatch_wait_at.is_(None),
                Task.dispatch_wait_at < restamp_before,
            ),
        )
        # `updated_at` held: the column's `onupdate` would otherwise stamp it (see the module docstring).
        .values(dispatch_wait_reason=wanted.c.reason, dispatch_wait_at=utcnow(),
                updated_at=Task.updated_at)
        .execution_options(synchronize_session=False)
    )
    session.commit()


def fresh_reason(task, *, now: Optional[datetime] = None) -> Optional[str]:
    """The task's stamped reason while it is `ready` and the stamp is fresh, else None."""
    reason, at = getattr(task, "dispatch_wait_reason", None), getattr(task, "dispatch_wait_at", None)
    if task.state != READY or not reason or at is None:
        return None
    if (now or utcnow()) - at > timedelta(seconds=FRESH_SECONDS):
        return None
    return reason
