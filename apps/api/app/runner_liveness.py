"""Runner liveness: is the runner carrying a Task still there, and did its whole node just lose every
runner at once. The single home for both, since they read the same row with the same predicate;
`task_pipeline_job` re-exports the names.

The node-wide question exists because a node substrate fault (the shared fleet tmux server dying)
kills every runner at once, and charging each reclaim a dispatch attempt turns one node fault into N
tasks' spent retry budgets. Nothing on the node reports its tmux server dying, so the signal is
shape: several slots that ought to have a runner and all do not, as most of the node's judged slots.

Fails closed: anything unproven is UNKNOWN, never DEAD, and an unproven peer is not counted. A bug
costs latency or a withheld refund, never a live runner.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app import edition_features, slot_state
from app.db import SessionLocal
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, READY

log = logging.getLogger("cockpit-scheduler")

# How fresh a node's observation must be to count as the node talking to us now: ~12 missed ~5s passes,
# well short of the caller's UNKNOWN grace (`task_pipeline_job.HOLDER_LIVE_WINDOW_SECONDS`). Only a
# report this fresh turns "no session" into confirmed DEAD.
OBSERVATION_FRESH_SECONDS = 60.0
# How recently a node must have spoken (`nodes.last_seen`) to count as still talking. Wider than the observation
# bound: a starved daemon's heartbeat itself arrives late. A talking node that stopped observing a slot is starved,
# not gone (`task_pipeline_job._runner_held`), and its VMs are still sighted (`microvm_lifecycle`).
NODE_STILL_TALKING_SECONDS = 300.0
# How long an `active` task may go with no sign of its runner — no fresh observation reporting a session, no
# node sighting (`worktrees.last_seen_live`, a VM's `microvms.last_sighted_at`) and no transcript write
# (`worktrees.transcript_written_at`) — before the stall detector records a `stalled` break and reclaims it
# (`task_pipeline.reclaim_silent_active_tasks`). Counted from the latest of those and the claim.
SILENCE_THRESHOLD = timedelta(minutes=20)
# How long after a launch ACK a never-sighted runner is off-limits to confirmed DEAD. The node's
# `session_*` columns lag by an observation and its agents probe is cached, so it may truthfully
# report no session just after the ack; without this, runners seconds old were reclaimed. 90s, not the
# earlier 180s: DEAD inside the give-up bound also needs the node's cwd scan to find nothing rooted in the
# tree (the stale-agents-cache guard below), and each dead launch held its task for the whole settle.
LAUNCH_SETTLE_SECONDS = 90.0
# How long a never-sighted launch whose tree is NOT proven empty stays off-limits to DEAD. Past it the
# node has had ~180 probes of its fast lane to sight the runner, so "not sighted" is evidence again; it
# bounds the strict reclaimers (`task_pipeline_job._runner_confirmed_dead`), which never act on UNKNOWN.
LAUNCH_UNSIGHTED_GIVE_UP_SECONDS = 900.0
# How long a MicroVM may stay `provisioning` (clone, base-image wait, boot) before a guest that never
# reported a session is a stuck one. Past the node's own longest provision wait (`microvm_images.
# wait_for_base`, ~55 minutes on a fresh node), after which the node reports `failed` itself; this only
# bounds a node that never does.
VM_PROVISION_GIVE_UP_SECONDS = 3600.0
# How recent a MicroVM node's published report must be for a `pending` guest in it to count. A marked node
# republishes at least once a minute while its free RAM moves.
GUEST_REPORT_FRESH_SECONDS = 300.0

# How many of a node's task-held slots must be confirmed gone at once to read as the node's loss. Two:
# one is the ordinary case, two in one window means their shared substrate stopped, and a two-runner
# node can produce no more.
NODE_WIDE_LOSS_MIN = 2
# …and the share of the node's judged slots those losses must be. A substrate death is total (the
# tmux server is shared), so a half share admits every genuine case while refusing "two of twelve"
# churn. The denominator is judged slots only: UNKNOWN is not evidence, and a peer relaunched seconds
# ago counted as a survivor would hide a cascade in progress. A false negative only costs a refund.
NODE_WIDE_LOSS_SHARE = 0.5

# One read, every slot on the node carrying `:task_id` (found by subquery; no slot matches nothing,
# which `runner_verdict` reads as DEAD unless a MicroVM runs the task). Joined to `nodes` for "is the node talking to us";
# `task_id IS NOT NULL` keeps only slots carrying work. LEFT-joined to `tasks` for
# `_peer_expects_a_runner`, since a stopped task keeps its slot and would read DEAD forever as a phantom
# peer loss. Only the peer count is filtered: an inner join would drop a slot whose task row is missing
# and change the caller's own verdict. NULL `task_state` reads as "no runner expected".
_RUNNER_STATE_SQL = text(
    """
    SELECT w.task_id, w.acked_at, w.launch_session_id, w.session_id, w.session_status,
           w.pid, w.pid_start_time, w.session_pid, w.session_proc_started_at,
           (w.probes->>'agents_ok') AS agents_ok,
           (w.probes->>'session_cwd') AS session_cwd,
           t.state                                           AS task_state,
           (t.question_runner_stopped_at IS NOT NULL)        AS runner_stopped_on_question,
           now() - t.github_synced_at                        AS since_task_moved,
           now() - coalesce(w.last_seen_live, w.state_since) AS since_seen_live,
           now() - w.observed_at                             AS since_observed,
           now() - n.last_seen                               AS since_node_spoke,
           now() - w.acked_at                                AS since_acked,
           w.observed_at - w.acked_at                        AS observed_since_ack,
           w.last_seen_live - w.acked_at                     AS seen_since_ack
      FROM worktrees w
      JOIN nodes n ON n.id = w.node_id
      LEFT JOIN tasks t ON t.id = w.task_id
     WHERE w.node_id = (
               SELECT w2.node_id FROM worktrees w2
                WHERE w2.task_id = :task_id AND w2.state <> :free
                LIMIT 1
           )
       AND w.state <> :free
       AND w.task_id IS NOT NULL
    """
)

# A MicroVM task has no slot row: its VM is its runner (`microvm_placement.admit`), so while that VM is
# provisioning or running there is a runner to wait for. A lost or refused VM is marked `failed`
# (`microvm_lifecycle`), which ends the attempt itself; a VM that runs on with no agent inside is read off
# the node's `microvm.guest_sessions` report (`_vm_verdict`). Only while its node reports: the node's silence
# is read as the slot path reads it, unknown for as long as the node has been quiet.
_LIVE_VM_SQL = text(
    """
    SELECT m.id::text                                  AS vm_id,
           m.state                                     AS vm_state,
           now() - n.last_seen                         AS since_node_spoke,
           now() - n.config_reported_at                AS since_reported,
           now() - m.created_at                        AS since_admitted,
           now() - m.updated_at                        AS since_vm_moved,
           n.config_snapshot -> 'microvm' -> 'guest_sessions' AS guest_sessions
      FROM microvms m
      JOIN nodes n ON n.id = m.node_id
     WHERE m.task_id = :task_id AND m.state IN :live
     LIMIT 1
    """
).bindparams(bindparam("live", expanding=True))

ALIVE = "alive"  # the runner we launched is still sensed — never reclaim
DEAD = "dead"  # a live node's fresh probe says it is gone — reclaim now
UNKNOWN = "unknown"  # nobody can tell — wait out the caller's silence grace, then reclaim


def _secs(delta) -> Optional[float]:
    """A `timedelta` from the query as seconds, or None when the column was NULL."""
    return None if delta is None else delta.total_seconds()


class RunnerState(NamedTuple):
    """What is known about a task's runner, for how long it has been unknown, and how many runners its
    node has lost at the same instant.

    `verdict` is what the evidence says; `unknown_for_secs` is how long we have been without evidence.
    Acting on silence is the caller's policy, never an observation. `dead_peers`/`judged_peers` are
    observations too (this slot included in both); whether they mean "the node broke" is
    `NODE_WIDE_LOSS_MIN`/`NODE_WIDE_LOSS_SHARE`'s and the callers' call.
    """

    verdict: str
    unknown_for_secs: Optional[float]
    dead_peers: int = 0
    judged_peers: int = 0
    # Seconds since this task's node last spoke to us (`nodes.last_seen`, its heartbeat), None when unread.
    # An observation, not a verdict: a node that heartbeats but stopped observing is starved, not gone, so
    # the caller may wait longer before reclaiming on silence (`task_pipeline_job._runner_held`).
    node_spoke_secs: Optional[float] = None
    # Seconds since the node last observed this task's slot (`worktrees.observed_at`), None when unread. With
    # `node_spoke_secs` it tells a starved node (heartbeats, stopped observing) from a merely unproven launch.
    observed_secs: Optional[float] = None


def _verdict_from_row(row: Any) -> tuple[str, Optional[float]]:
    """`(verdict, unknown_for_secs)` for one slot row from `_RUNNER_STATE_SQL`. Pure: no I/O, no policy.

    Shared by the per-task answer and the peer count so they cannot drift. Three values, not a boolean:
    a live node's fresh "no session" is evidence (reclaim now), while a silent node is not (its daemon
    can wedge while runners keep working; redispatching would put two writers on one tree).

    DEAD requires all four:
      1. the node and this slot's observation are within `OBSERVATION_FRESH_SECONDS`;
      2. `agents_ok` is true (the probe ran);
      3. the dispatch was acked and the launch settled: the node's observation postdates the ack, and
         either `last_seen_live` postdates it (the node sighted this launch) or, for a never-sighted
         launch, `LAUNCH_SETTLE_SECONDS` has passed AND the node's cwd scan found nothing rooted in the
         tree (`probes.session_cwd` false) or `LAUNCH_UNSIGHTED_GIVE_UP_SECONDS` has passed;
      4. what is sensed does not match what we launched.
    """
    # ALIVE, cheapest first. The launch identity pair is the strong test; `pid` alone can be recycled.
    launched_pid_matches = (
        row.pid is not None
        and row.session_pid == row.pid
        and row.pid_start_time is not None
        and row.session_proc_started_at == row.pid_start_time
    )
    launched_session_matches = (
        row.launch_session_id is not None
        and row.session_id == row.launch_session_id
        and bool(row.session_status)
    )
    if launched_pid_matches or launched_session_matches:
        return ALIVE, None

    since_observed = _secs(row.since_observed)
    since_node_spoke = _secs(row.since_node_spoke)
    node_is_talking = (
        since_node_spoke is not None and since_node_spoke <= OBSERVATION_FRESH_SECONDS
        and since_observed is not None and since_observed <= OBSERVATION_FRESH_SECONDS
    )
    probe_ran = row.agents_ok == "true"
    # The node must have looked since we launched, long enough ago to have seen it (the agents probe is
    # cached). An observation stamped before the ack cannot have seen the session.
    since_acked = _secs(row.since_acked)
    observed_since_ack = _secs(row.observed_since_ack)
    seen_since_ack = _secs(row.seen_since_ack)
    # Did the node ever sight this launch? `last_seen_live` advances only on an observed live session, so
    # later than the ack means yes, and a subsequent absence is evidence at any age.
    launch_was_sighted = seen_since_ack is not None and seen_since_ack > 0
    # Fallback for a launch never sighted: ambiguous while young, a failed startup once old — but only if
    # nothing runs in the tree. A process rooted there is a runner the node's agents list has not caught
    # (a stale agents cache once read live runners DEAD at exactly this settle), so it stays UNKNOWN.
    launch_never_appeared = (
        not launch_was_sighted
        and since_acked is not None and since_acked > LAUNCH_SETTLE_SECONDS
        and (getattr(row, "session_cwd", None) == "false" or since_acked > LAUNCH_UNSIGHTED_GIVE_UP_SECONDS)
    )
    launch_has_settled = (
        (launch_was_sighted or launch_never_appeared)
        and observed_since_ack is not None and observed_since_ack > 0
    )
    if node_is_talking and probe_ran and row.acked_at is not None and launch_has_settled:
        return DEAD, None

    # Nothing is proven. How long it has been unproven is the caller's policy; the verdict stays a fact.
    return UNKNOWN, _secs(row.since_seen_live)


def _peer_expects_a_runner(row: Any) -> bool:
    """Should there be a runner in this slot right now? Only such a slot can have lost one.

    A slot keeps its task and launch identity through every non-terminal end, so without this a stopped
    task reads DEAD forever and makes every churning peer look like a node-fault victim. Admitted:

      * `active` / `awaiting_response`: the CP believes a runner is working or parked.
      * `ready` with a fresh transition: a row a reclaim just requeued. Reclaims arrive about a second
        apart, so admitting these keeps a cascade whole. `github_synced_at` is what `end_attempt` stamps
        on that edge; bounded by `OBSERVATION_FRESH_SECONDS` so a row stuck `ready` stops counting.

    Everything else is excluded; `paused` matters most (a Stop kills the runner and keeps the slot). A
    CP-parked wedged-dispatch `awaiting_response` row never acked, so it reads UNKNOWN and counts nowhere.
    """
    if row.task_state in (AWAITING_RESPONSE, READY) and getattr(row, "runner_stopped_on_question", False):
        # A park whose runner the CP stopped on purpose (`tasks.stop_runner_on_unanswered_question`), or the
        # `ready` row its answer resumed: no runner is expected, as for `paused`.
        return False
    if row.task_state in (ACTIVE, AWAITING_RESPONSE):
        return True
    if row.task_state == READY:
        moved = _secs(row.since_task_moved)
        return moved is not None and moved <= OBSERVATION_FRESH_SECONDS
    return False


def _read_node_slots(task_id: str, session: Optional[Session]) -> Optional[list[Any]]:
    """Every task-held slot row on the node carrying TASK_ID, or None when the read itself failed.

    `[]` (no slot carries it) and None (could not ask) differ: `runner_verdict` reads them as DEAD and
    UNKNOWN. `session` is an open session for a caller that needs its own uncommitted writes visible (the
    reclaim routes); None opens a fresh one (the sweep).

    A shared session is rolled back on failure: a DB error aborts the transaction, and returning UNKNOWN
    on a poisoned session would make the route's next UPDATE raise `PendingRollbackError`. The routes ask
    before they write, so only the failed read is discarded. Everything, `int(task_id)` included, is
    inside the `try`.
    """
    try:
        params = {"task_id": int(task_id), "free": slot_state.FREE}
        if session is not None:
            return list(session.execute(_RUNNER_STATE_SQL, params).all())
        with SessionLocal() as own:
            return list(own.execute(_RUNNER_STATE_SQL, params).all())
    except Exception as exc:  # noqa: BLE001 — fail CLOSED
        log.warning("runner-liveness: runner-state read failed for task %s (%s) — UNKNOWN", task_id, exc)
        if session is not None:
            try:
                session.rollback()
            except Exception as rb:  # noqa: BLE001 — nothing left to salvage; the caller still gets UNKNOWN
                log.warning("runner-liveness: could not roll back the caller's session for task %s: %s",
                            task_id, rb)
        return None


def _live_vm_state(task_id: str, session: Optional[Session]) -> Optional[RunnerState]:
    """TASK_ID's runner as its MicroVM answers it (`_LIVE_VM_SQL`): by its guest session (`_vm_verdict`)
    while the VM's node reports, UNKNOWN aged by the node's silence once it does not, None when no VM runs
    the task. A failed read is
    UNKNOWN and rolls back a shared session, as `_read_node_slots` does."""
    if not edition_features.enabled("microvm"):
        return None  # The community edition has no MicroVMs.
    from app import microvm_placement

    params = {"task_id": int(task_id), "live": list(microvm_placement.LIVE)}
    try:
        if session is not None:
            row = session.execute(_LIVE_VM_SQL, params).first()
        else:
            with SessionLocal() as own:
                row = own.execute(_LIVE_VM_SQL, params).first()
    except Exception as exc:  # noqa: BLE001 — fail CLOSED
        log.warning("runner-liveness: MicroVM read failed for task %s (%s) — UNKNOWN", task_id, exc)
        if session is not None:
            try:
                session.rollback()
            except Exception as rb:  # noqa: BLE001 — the caller still gets UNKNOWN
                log.warning("runner-liveness: could not roll back the caller's session for task %s: %s",
                            task_id, rb)
        return RunnerState(UNKNOWN, None, 0, 0)
    if row is None:
        return None
    quiet_for = _secs(row.since_node_spoke)
    if quiet_for is None or quiet_for > OBSERVATION_FRESH_SECONDS:
        return RunnerState(UNKNOWN, quiet_for, 0, 0)
    verdict = _vm_verdict(row.vm_id, row.vm_state == microvm_placement.RUNNING, row.guest_sessions,
                          since_admitted=_secs(row.since_admitted), since_vm_moved=_secs(row.since_vm_moved),
                          since_reported=_secs(row.since_reported))
    return RunnerState(verdict, None, 0, 0)


def _vm_verdict(vm_id: str, vm_running: bool, guest_sessions: Any, *, since_admitted: Optional[float],
                since_vm_moved: Optional[float], since_reported: Optional[float]) -> str:
    """ALIVE or DEAD for a live VM on a node that is talking, by its node's `guest_sessions` report.
    The static path's rules, read off the guest instead of a slot:

      ended    the agent session inside is gone (its pid, or a heartbeat silent past the static silence grace):
               DEAD now, as a sensed-gone runner;
      alive    ALIVE however long it has run — and so are `stale` (unproven) and `unknown` (launched before
               the report existed);
      pending  (or no entry) DEAD once the VM has been `running` for `LAUNCH_UNSIGHTED_GIVE_UP_SECONDS`, the
               bound a never-sighted static launch gets, or `provisioning` for `VM_PROVISION_GIVE_UP_SECONDS`
               since admission. Only on a snapshot the node published within `GUEST_REPORT_FRESH_SECONDS`:
               a `pending` the node never managed to replace is not evidence.

    A node that sends no `guest_sessions` at all predates the report, so nothing about the guest is known and
    the VM stays ALIVE, as it always was."""
    if not isinstance(guest_sessions, dict):
        return ALIVE
    seen = guest_sessions.get(vm_id)
    if seen == "ended":
        return DEAD
    if seen not in (None, "pending"):
        return ALIVE
    if since_reported is None or since_reported > GUEST_REPORT_FRESH_SECONDS:
        return ALIVE
    if vm_running:
        waited, bound = since_vm_moved, LAUNCH_UNSIGHTED_GIVE_UP_SECONDS
    else:
        waited, bound = since_admitted, VM_PROVISION_GIVE_UP_SECONDS
    return DEAD if waited is not None and waited > bound else ALIVE


def runner_verdict(task_id: str, session: Optional[Session] = None) -> RunnerState:
    """ALIVE / DEAD / UNKNOWN for the runner carrying TASK_ID, plus how many of its node's task-held
    slots read DEAD on the same read (`dead_peers`, this slot included).

    The predicate is `_verdict_from_row`'s. Fails closed: any error or unproven state is UNKNOWN, never
    DEAD; UNKNOWN still reclaims on the caller's timer. The one exception: no slot carries this task and
    no MicroVM runs it, so there is no runner to wait for.
    """
    rows = _read_node_slots(task_id, session)
    if rows is None:
        return RunnerState(UNKNOWN, None, 0, 0)
    if not rows:
        vm_state = _live_vm_state(task_id, session)
        if vm_state is not None:
            # Its VM is its runner: reading DEAD here orphaned every booting VM on dev.
            return vm_state
        # No slot carries this task: no runner to wait for, and no node to count peers on.
        return RunnerState(DEAD, None, 0, 0)

    mine: Optional[tuple[str, Optional[float], Optional[float], Optional[float]]] = None
    dead_peers = 0
    judged_peers = 0
    wanted = int(task_id)
    for row in rows:
        verdict, unknown_for = _verdict_from_row(row)
        # Only the peer count is filtered; my own row is answered whatever its state.
        if _peer_expects_a_runner(row):
            if verdict == DEAD:
                dead_peers += 1
            if verdict in (DEAD, ALIVE):
                judged_peers += 1
        if row.task_id == wanted:
            mine = (verdict, unknown_for, _secs(row.since_node_spoke), _secs(row.since_observed))
    if mine is None:
        # Unreachable (the subquery and scan share one snapshot), kept as a floor. DEAD, not UNKNOWN: an
        # UNKNOWN with no age would make `_runner_held` hold forever.
        log.warning("runner-liveness: task %s resolved a node but not its own slot — treating as DEAD",
                    task_id)
        return RunnerState(DEAD, None, dead_peers, judged_peers)
    return RunnerState(mine[0], mine[1], dead_peers, judged_peers, mine[2], mine[3])


def node_wide_runner_loss(task_id: str, session: Optional[Session] = None) -> bool:
    """Did this task's whole node just lose `NODE_WIDE_LOSS_MIN` or more runners at once?

    True means the loss is the node's, so a caller ending the attempt may decline to charge it
    (`app/routes/tasks/runner_settle.py::reclaim_active_task`). Requires all three:

      1. my own verdict is DEAD (otherwise nobody is ending this row);
      2. at least `NODE_WIDE_LOSS_MIN` losses;
      3. at least `NODE_WIDE_LOSS_SHARE` of the judged slots.

    Fails closed to False (the attempt is charged): a refund on a bad read would let a churning task
    cycle past its ceiling.
    """
    state = runner_verdict(task_id, session)
    if state.verdict != DEAD or state.dead_peers < NODE_WIDE_LOSS_MIN:
        return False
    # `judged_peers` >= `dead_peers` >= NODE_WIDE_LOSS_MIN here, so the ratio is defined.
    return state.dead_peers >= NODE_WIDE_LOSS_SHARE * state.judged_peers


# The task's held slot as the reclaim path reports it: whether its launch was ever sighted, and who ran it.
_RUNNER_FACTS_SQL = text(
    """
    SELECT (w.acked_at IS NOT NULL
            AND (w.last_seen_live IS NULL OR w.last_seen_live <= w.acked_at)) AS never_sighted,
           n.name AS node_name, n.id AS node_id, (n.org_id = t.org_id) AS own_node,
           a.name AS account_name, t.runner_session_id,
           w.dispatch_id, w.generation, COALESCE(w.acked_at, t.github_synced_at) AS launch_at,
           pane.pane_log_written_at
      FROM worktrees w
      JOIN nodes n ON n.id = w.node_id
      LEFT JOIN accounts a ON a.id = w.leased_account_id
      LEFT JOIN tasks t ON t.id = w.task_id
      -- The session the incident's pane log is read from: the runner's own, else the task's newest. Pinned to
      -- the task's org, as `routes/tasks/runner_settle._vanished_trace` reads it.
      LEFT JOIN LATERAL (
            SELECT s.pane_log_written_at
              FROM sessions s
             WHERE s.task_id = t.id AND s.org_id = t.org_id
             ORDER BY (s.agent_session_id = t.runner_session_id) IS TRUE DESC, s.started_at DESC, s.id DESC
             LIMIT 1
      ) pane ON true
     WHERE w.task_id = :task_id AND w.state <> :free
     LIMIT 1
    """
)


class RunnerFacts(NamedTuple):
    """What the reclaim path records about a lost runner, read BEFORE `end_attempt` clears the task's binding.
    `never_sighted`: its launch was acked and the node never saw it running (`last_seen_live` never passed the
    ack) — the `launch_failed` cause rather than `vanished`. An un-acked launch is not one: the node never
    started it, which `tasks.charge_unannounced_launch` already ends as `launch_unannounced`.

    `launch` is the slot's `"<dispatch_id> gen <generation>"`; `launch_at` that launch's ack, else the task's
    latest transition stamp; `pane_log_written_at` the last write of the pane log the incident's session
    carries. `own_node`: the node is the task's org's, so its name may ride a body that org reads."""

    never_sighted: bool = False
    node: str = ""
    account: str = ""
    session_id: str = ""
    node_id: str = ""
    own_node: bool = False
    launch: str = ""
    launch_at: Optional[datetime] = None
    pane_log_written_at: Optional[datetime] = None


def runner_facts(task_id: str, session: Session) -> RunnerFacts:
    """`RunnerFacts` for the slot carrying TASK_ID, or the empty one (no slot, a MicroVM task, a failed read).
    Best-effort evidence for an incident: never raises, and a failed read rolls back as `_read_node_slots` does."""
    try:
        row = session.execute(
            _RUNNER_FACTS_SQL, {"task_id": int(task_id), "free": slot_state.FREE}
        ).first()
    except Exception as exc:  # noqa: BLE001 — evidence only; the reclaim proceeds without it
        log.warning("runner-liveness: runner-facts read failed for task %s (%s)", task_id, exc)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 — nothing left to salvage
            pass
        return RunnerFacts()
    if row is None:
        return RunnerFacts()
    return RunnerFacts(
        bool(row.never_sighted), row.node_name or "", row.account_name or "", row.runner_session_id or "",
        node_id=str(row.node_id) if row.node_id is not None else "",
        own_node=bool(row.own_node),
        launch=f"{row.dispatch_id} gen {row.generation}" if row.dispatch_id else "",
        launch_at=row.launch_at,
        pane_log_written_at=row.pane_log_written_at,
    )
