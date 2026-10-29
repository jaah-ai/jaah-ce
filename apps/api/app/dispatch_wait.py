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

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import Integer, exists, or_, select, update
from sqlalchemy.orm import Session

from app import edition
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
NO_ACCOUNT = "Waiting: no coding agent API/subscription available"
NODE_MISSING_CLI = "Waiting: runner CLI missing on node"
#: Every node the task may use is running a merge-lane gate, which takes no new run until it ends
#: (`merge_lane.gating_node_ids`).
LANE_GATING = "Waiting: its node is running the merge lane's gate"
#: An SDK agent task with no node of its project that can run agents (`agent_resolution.sdk_capable_nodes`):
#: a capacity wait, not an unrunnable agent, so no attempt is spent (architecture §3.4).
NO_SDK_NODE = (
    "Waiting: No node in this org can run SDK agents yet (needs an Ubuntu node on a current Jaah release)"
)
#: The project is assigned to no node (`slot_allocation.hosting_node_ids` empty): nothing can run it.
NO_NODE = "Waiting: no node runs this project. Add one in the project's Slots tab."
#: The project's nodes exist but none is live for placement: offline, disabled, draining or not reporting slots
#: (`pool_state.live_nodes_for_project`).
NODES_OFFLINE = "Waiting: this project's nodes are offline, disabled or draining"
#: Live nodes run the project, but none of them is open to this task (its launcher's node grants).
NO_ALLOWED_NODE = "Waiting: none of this project's online nodes is open to this task's launcher"
#: A Jaah-account task offered by a node outside its org (a pinned hold, or a node with no row): the Jaah session is
#: bound to the org's own nodes (`routes/fleet.py::build_dispatch_plan`'s Jaah arm).
JAAH_FOREIGN_NODE = "Waiting: the Jaah account runs only on this organization's own nodes"
#: An SDK agent task still holding a slot (from an earlier built-in attempt) on a node that cannot run agents:
#: the hold pins it there, so Reset frees it for a node that can.
HELD_ON_NODE_WITHOUT_SDK = (
    "Waiting: its worktree is on a node that cannot run SDK agents — Reset the task to move it"
)


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
    return "Waiting: this machine's node cannot run SDK agents yet" if edition.IS_CE else NO_SDK_NODE


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


def low_disk(free_gb: Optional[float], floor_gb: float) -> str:
    """The node's free disk is below the launch floor (`runner_launch_disk_floor_gb`). `None` leaves the free figure
    out (a node on another org's host: it is the host's)."""
    if free_gb is None:
        return f"Waiting: node disk low (needs {floor_gb:g} GB free)"
    return f"Waiting: node disk low ({free_gb:.1f} GB free, needs {floor_gb:g} GB)"


def disk_paused(used_pct: Optional[float], resume_pct: float) -> str:
    """The disk gate paused the node (`app/node_disk_events.py`); it resumes on its own under `resume_pct`."""
    usage = f"{used_pct:.0f}% full" if used_pct is not None else "full"
    return f"Waiting: node disk {usage}; resumes under {resume_pct:g}%"


def memory_pressure(used_pct: Optional[float], critical_pct: float, hold_minutes: float) -> str:
    """The node's memory reached `critical_pct` within the hold window (`app/launch_memory.py`); it reopens
    once a whole `hold_minutes` passes under that line."""
    usage = f"{used_pct:.0f}% used" if used_pct is not None else "full"
    return f"Waiting: node memory {usage}; resumes after {hold_minutes:g} min under {critical_pct:g}%"


def cpu_saturated(low_pct: Optional[float], saturated_pct: float) -> str:
    """Every recent minute of the node's CPU read at or above `saturated_pct` (`app/launch_cpu.py`); it reopens
    as soon as one minute reads under it."""
    usage = f"{low_pct:.0f}%+ busy" if low_pct is not None else "saturated"
    return f"Waiting: node CPU {usage}; resumes under {saturated_pct:g}%"


def memory_ceiling(
    runners: Optional[int], ceiling: Optional[int], reservation_gb: float, *, shared: bool = False,
) -> str:
    """The node's machine runs as many runners as its memory holds (`runner_memory_reservation_gb` each).
    `shared`: the node is co-located, so the machine is its HOST and the count spans every node on it.
    `None` figures leave the count out (a node on another org's host: the figures are the host's)."""
    if edition.IS_CE:
        # One machine and its owner: the knob that moves the ceiling is theirs, so the sentence names it.
        held = f"holds {ceiling} runners" if ceiling is not None else "is full of runners"
        return (f"Waiting: this machine's memory {held} at {reservation_gb:g} GB each. "
                "To run more, lower Settings › Runners › Runner memory.")
    where = "host" if shared else "node"
    if runners is None or ceiling is None:
        return f"Waiting: {where} memory full of runners ({reservation_gb:g} GB each)"
    return f"Waiting: {where} memory full of runners ({runners}/{ceiling} at {reservation_gb:g} GB each)"


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
    return f"Waiting: started elsewhere than {node}, and no other node is online"


def no_account_logged_in(node: str) -> str:
    """Every account the task may use is unattested on its node (`account_unobserved`,
    `node_reports_no_credentials`): the org has accounts, just none logged in there. Ends in a period: the
    sentence carries its own remedy."""
    if edition.IS_CE:
        return "Waiting: no account is logged in on this machine. Log one in to run tasks."
    return f"Waiting: no account is logged in on {node}. Log one in there, or add a node that has one."


def no_account_of(provider_label: str) -> str:
    """The task is bound to a vendor and no account of it is free."""
    return f"Waiting: no {provider_label} API/subscription available"


#: The node outcomes that say a slot order was not enacted, plus the CP's `parked` for one refused to the end
#: (`fleet_verdicts.OUTCOME_*`, spelled here so this light module does not import that one at load).
_REFUSED_ORDER_OUTCOMES = ("refused_stale", "refused_precondition", "failed", "parked")
#: A refusal is the node declining to try, re-served with backoff (`fleet_verdicts.refusal_backoff_secs`): a fresh
#: node task's folder slot is refused "not in this pass's roster" several times until the node's next pass sees it.
#: So a refusal is a reason only once the CP parks it (`REFUSAL_PARK_AFTER`, ~75s of backoff) or the order outlives
#: that budget — a node refusing for changing reasons restarts the count (`fleet_verdicts.refusal_key`) and never
#: parks. A `failed` build is a reason at once.
_TRANSIENT_ORDER_OUTCOMES = ("refused_stale", "refused_precondition")
_TRANSIENT_GRACE = timedelta(seconds=90)
#: The node's words are cut to this many characters: the sentence is a chip and a banner, not a log.
_REFUSAL_ECHO_CAP = 240


def slot_refusal(reason: Optional[str]) -> str:
    """The task's slot could not be built: its newest `lease_slot` order was refused by the node. The node's own
    words follow, redacted (`fleet_verdicts.redact_node_detail`) because `wait_reason` is served at `tasks:view`."""
    from app import fleet_verdicts  # noqa: PLC0415 — a heavy module; only this sentence needs it

    said = " ".join((fleet_verdicts.redact_node_detail(reason) or "").split())[:_REFUSAL_ECHO_CAP]
    return f"Waiting: its slot could not be built. The node said: {said}" if said else (
        "Waiting: its slot could not be built; the node gave no reason")


def slot_refusals(session: Session, tasks) -> dict[int, str]:
    """`{task id: slot_refusal}` for those `ready` tasks of `tasks` that hold a slot whose newest `lease_slot`
    order the node failed, or refused past the park budget. A task holding no slot is past that order (a Reset
    released it), so it says nothing. One read: Postgres `DISTINCT ON` keeps each task's newest order."""
    from app import fleet_verdicts, slot_state  # noqa: PLC0415 — heavy modules; only this read needs them
    from app.models.worktrees import SlotIntent, Worktree  # noqa: PLC0415

    ids = [str(t.id) for t in tasks if t.state == READY]
    if not ids:
        return {}
    task_key = SlotIntent.payload["task_id"].astext
    rows = session.execute(
        select(task_key.label("task_id"), SlotIntent.outcome, SlotIntent.refusals, SlotIntent.reason,
               SlotIntent.created_at)
        .where(SlotIntent.verb == fleet_verdicts.VERB_LEASE_SLOT, task_key.in_(ids),
               exists().where(Worktree.task_id == SlotIntent.payload["task_id"].astext.cast(Integer),
                              Worktree.state == slot_state.TASK))
        .distinct(task_key)
        .order_by(task_key, SlotIntent.created_at.desc(), SlotIntent.id.desc())
    ).all()
    settled_before = datetime.now(timezone.utc) - _TRANSIENT_GRACE
    return {
        int(row.task_id): slot_refusal(row.reason) for row in rows
        if row.outcome in _REFUSED_ORDER_OUTCOMES and (row.refusals or 0) >= 1
        and (row.outcome not in _TRANSIENT_ORDER_OUTCOMES or row.created_at < settled_before)
    }


def record(session: Session, waits: dict[int, str]) -> None:
    """Stamp each `{task id: sentence}` on its task while it is still `ready`, then commit. Skips a row
    whose reason is unchanged and was stamped within `RESTAMP_SECONDS`."""
    if not waits:
        return
    restamp_before = datetime.now(timezone.utc) - timedelta(seconds=RESTAMP_SECONDS)
    for task_id, reason in waits.items():
        session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.state == READY,
                or_(
                    Task.dispatch_wait_reason.is_distinct_from(reason),
                    Task.dispatch_wait_at.is_(None),
                    Task.dispatch_wait_at < restamp_before,
                ),
            )
            # `updated_at` held: the column's `onupdate` would otherwise stamp it (see the module docstring).
            .values(dispatch_wait_reason=reason, dispatch_wait_at=datetime.now(timezone.utc),
                    updated_at=Task.updated_at)
            .execution_options(synchronize_session=False)
        )
    session.commit()


def fresh_reason(task, *, now: Optional[datetime] = None) -> Optional[str]:
    """The task's stamped reason while it is `ready` and the stamp is fresh, else None."""
    reason, at = getattr(task, "dispatch_wait_reason", None), getattr(task, "dispatch_wait_at", None)
    if task.state != READY or not reason or at is None:
        return None
    if (now or datetime.now(timezone.utc)) - at > timedelta(seconds=FRESH_SECONDS):
        return None
    return reason
