"""The CP drain: the deciding side of the dispatch loop.

Policy lives in `routes/fleet.py::build_dispatch_plan`; this module supplies which node
(`pool_state.live_nodes`), one allocation table per pass (`routes/fleet.py::fleet_allocation`, shared by
every node so two nodes cannot lease two slots for one task), the hold (`slot_state.lease` before the
intent, so a pass is safe to run twice), the account and ports, and the prompt (`dispatch_compose.compose.compose`).

Within a pass: settle finished holds, then dispatch. Fails closed on `pool_state_ok` (an unusable
occupancy read looks like an idle fleet). A slot is its task's until the task is done, deleted or reset;
an un-acked dispatch is re-issued as a `lease_slot` onto the slot the task already owns.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from concurrent.futures import as_completed
from typing import Any, Callable, NamedTuple, Optional
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app import (
    account_auth,
    acp_registry,
    acp_runtime,
    actors,
    agent_providers,
    agent_resolution,
    agent_session_lifecycle,
    agent_sessions,
    dispatch_wait,
    edition_features,
    events,
    fleet_decide,
    fleet_verdicts,
    git_access,
    idle_stacks,
    jaah_scheduler,
    launch_cpu,
    launch_disk,
    launch_failures,
    launch_memory,
    merge_lane_state,
    node_platform,
    placement_grants,
    pool_state,
    port_grid,
    project_env,
    shared_storage,
    slot_allocation,
    slot_state,
    task_ancestry,
    task_lifecycle,
    tenant_defaults,
    user_activity,
)
from app.operator_settings import reads as settings_reads
from app.dispatch_compose import compose, task_prompt
from app.db import SessionLocal, background_executor
from app.entity_shape import SYSTEM_ACTOR_ID
from app.models.credentials import Account
from app.models.nodes import Node
from app.models.projects import Project
from app.models.tasks import Task
from app.models.worktrees import WORKTREE_KIND_DYNAMIC, SlotIntent, Worktree
from app.node_clone import account_id_for_dir
from app.node_identity import names_by_id, node_id_or_none, node_matches, node_org, node_ref
from app.one_line import one_line
from app.tenancy import exclusion_log, seam

log = logging.getLogger("lease-drain")

# The schedule is the `lease-drain` cron row's (defaults registered by `app/cron_system_activities.py`). A
# backstop: committed task transitions fire it early (`app/task_wake.py`); everything else waits for this
# interval. A pass lasts about as long as its slowest node (~3s on dev), so 10s avoids back-to-back runs.
INTERVAL_SECONDS = 10.0
# The cron lock never overlaps a firing with itself, so this bounds how many ticks a wedged pass swallows:
# ~20x a measured pass, six ticks.
TIMEOUT_SECONDS = 60.0

# The re-dispatch budget is the task's: `fleet_dispatch_cap()` against `Task.dispatch_attempts`, charged on
# the `ready → active` claim (`routes/tasks/dispatch.py::_active_dispatch_values`) and on an acked launch that never
# claimed within `fleet_decide.ACKED_REDISPATCH_GRACE_SECONDS`, charged once inside the re-arm's fenced
# transaction (`routes/tasks/runner_settle.py::charge_unannounced_launch`), writing a `launch_unannounced` row. Uncharged:
# a dispatch refused before spawn (the launch fence runs before it) and a relaunch whose previous attempt claimed.

# The per-pass decision journal, rendered into the `cron_runs` row's `detail`
# (`app/cron_system_activities.py::_invoke_lease_drain`). One entry per decision made or declined with a
# reason; an empty journal keeps an idle fleet's ledger empty. A `ready` task the pool cannot serve writes
# a row per tick; raise `cron_runs_per_cron_cap` rather than thin this. Fields: ms since pass start,
# `point` (release | idle-stop | purge-intents | free-slot-reap | placement | prelude | dispatch | node),
# upper-case `decision`, why. Deduped per pass as `(xN)`; thread-safe; passed as a parameter.

# Distinct decisions one pass may hold (memory bound); ledger fit is `cron_runtime.MAX_DETAIL_CHARS`'s call.
_MAX_JOURNAL_ENTRIES = 200

# Clipped like the reason (`projects.name` is operator-editable). Generous: a clipped subject names
# something that does not exist; 72 clipped real ones.
_MAX_SUBJECT_CHARS = 160

# The shared-credential refusals here run ~230 chars and are quoted verbatim; this passes them whole while
# bounding an arbitrary exception.
_MAX_WHY_CHARS = 240


@dataclass(frozen=True)
class _Decision:
    """One decision without its timing; the four fields are the dedup key.

    `subject` names only what is material to the reason, so per-node fan-out entries still collapse.
    """

    point: str
    subject: str
    decision: str
    why: str

    def render(self, elapsed_ms: int, times: int) -> str:
        """`+912ms dispatch tuf/acme task42 DECLINED: no free slot in the pool`."""
        head = f"+{elapsed_ms}ms {self.point}"
        if self.subject:
            head = f"{head} {self.subject}"
        line = f"{head} {self.decision}: {self.why}"
        return f"{line} (x{times})" if times > 1 else line


class DecisionLog:
    """One pass's journal. PUBLIC because the cron trigger renders it and the tests build one."""

    def __init__(
        self,
        *,
        cap: int = _MAX_JOURNAL_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Monotonic, so a stepping wall clock cannot date a decision before the pass. Injectable for tests.
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        # key -> [first stamp seen at, times seen]
        self._entries: dict[_Decision, list[int]] = {}
        # Tie-break for two decisions inside one millisecond — `lines()` orders on the stamp.
        self._order: list[_Decision] = []
        self._cap = max(0, int(cap))
        self._dropped = 0
        # Written by worker threads, read by `_fail_abandoned_dispatches` after they join, under the journal's
        # lock. At most one task per free slot.
        self._declined_tasks: set[int] = set()

    def record(self, point: str, decision: str, why: str, *, subject: str = "") -> None:
        """Record one decision. No I/O, bounded, own lock only; not wrapped in `try`, since swallowing would
        hide a defect. The stamp is taken before the lock so waiting does not date it later.
        """
        elapsed_ms = max(0, int((self._clock() - self._started) * 1000))
        key = _Decision(
            point=point,
            subject=one_line(subject, _MAX_SUBJECT_CHARS, marker="..."),
            decision=decision,
            why=one_line(why, _MAX_WHY_CHARS, marker="..."),
        )
        with self._lock:
            seen = self._entries.get(key)
            if seen is not None:
                seen[1] += 1
                return
            if len(self._entries) >= self._cap:
                # Counted, so the renderer can say "200 shown" is not "200 decided".
                self._dropped += 1
                return
            self._entries[key] = [elapsed_ms, 1]
            self._order.append(key)

    def note_declined_task(self, task_id: object) -> None:
        """Record that this pass planned to dispatch `task_id` and did not lease it (see `declined_tasks`)."""
        try:
            key = int(task_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # No task id: a `stop_stack`/`reset_to_base` decline is about a slot.
            return
        with self._lock:
            self._declined_tasks.add(key)

    @property
    def declined_tasks(self) -> frozenset[int]:
        """The tasks this pass planned for dispatch and did not lease, whatever the reason.

        Read by `_fail_abandoned_dispatches`, which must not fail these: they were considered and journaled,
        usually for a transient reason.
        """
        with self._lock:
            return frozenset(self._declined_tasks)

    @property
    def dropped(self) -> int:
        """`record` calls the cap refused (a repeating reason counts once per call). Zero on normal passes."""
        with self._lock:
            return self._dropped

    def __len__(self) -> int:
        """DISTINCT decisions recorded — what the trigger reads to decide `acted` against `noop`."""
        return len(self._entries)

    def __bool__(self) -> bool:
        """Always true, so an empty journal is not falsy; ask `len(journal)` for "did this pass decide
        anything".
        """
        return True

    def lines(self) -> list[str]:
        """Every decision, one rendered line each, oldest first."""
        with self._lock:
            snapshot = [
                (self._entries[key][0], index, key, self._entries[key][1])
                for index, key in enumerate(self._order)
            ]
        snapshot.sort(key=lambda row: (row[0], row[1]))
        return [key.render(elapsed_ms, times) for elapsed_ms, _index, key, times in snapshot]


def _ledger_safe(exc: BaseException) -> str:
    """One exception as `TypeName: message`, with the failing statement and bound parameters removed.

    `str(exc)` on a `StatementError` includes the SQL and bound values (possibly a whole charter), which
    would land in the cleartext `cron_runs.detail`. Callers also `log.exception`.
    """
    head = str(exc).split("\n[SQL:", 1)[0].split("[SQL:", 1)[0]
    return f"{type(exc).__name__}: {head}" if head.strip() else type(exc).__name__


def _finish(summary: dict[str, Any], journal: DecisionLog) -> dict[str, Any]:
    """Stamp the journal onto the summary. Every return in `drain_once`/`_drain_pass` goes through here."""
    summary["decisions"] = journal.lines()
    summary["decisions_dropped"] = journal.dropped
    return summary


def _release_finished(session, journal: DecisionLog) -> int:
    """Free every slot whose task is `done` or `system_failed`, except one still owing its runner a kill.
    Returns how many were freed.

    The only release here, of a terminal task's hold; deleted/reset edges are in
    `app/routes/tasks/`. `ready`, `paused` and `awaiting_response` keep the slot. Also where a terminal
    `end_attempt`'s release happens: kill, then release, because freeing first unbinds the address the
    node's kill is fenced on. The wait predicate is `fleet_verdicts.slots_awaiting_a_kill`.
    """
    # Not `task_lifecycle._RUNNER_END_STATES`, which includes PAUSED and would release Stopped tasks' slots.
    _slot_release_terminal_states_excludes_paused = (task_lifecycle.DONE, task_lifecycle.SYSTEM_FAILED)
    # One query per pass; a kill retired mid-loop waits for the next pass.
    awaiting_a_kill = fleet_verdicts.slots_awaiting_a_kill(session)
    released = 0
    for hold in slot_state.settled_holds(session, _slot_release_terminal_states_excludes_paused):
        subject = f"{hold['project']}#{hold['slot']} task{hold['task_id']}"
        # Must match the key the mint builds (`routes/tasks/slots.py::_mint_kill_runner_intents`); `str(int(...))`
        # normalises a JSON-string id too.
        if (int(hold["project_id"]), int(hold["slot"]), str(int(hold["task_id"]))) in awaiting_a_kill:
            # Its own verdict token, distinct from `KEPT` (a fenced release matching no row).
            journal.record(
                "release", "WAITING",
                "its runner's kill has not been enacted yet — freeing the slot now would unbind the "
                "very address that kill is fenced on",
                subject=subject,
            )
            continue
        freed = _release(session, hold, f"task {hold['task_id']} has settled")
        released += freed
        # Both outcomes journaled: a release that matched no row is a slot still owned by a settled task.
        if freed:
            journal.record(
                "release", "RELEASED",
                "its task has settled, so the hold has nothing left to own",
                subject=subject,
            )
        else:
            journal.record(
                "release", "KEPT",
                "the generation-fenced release matched no row: the hold moved, was already free, or "
                "is no longer enrolled",
                subject=subject,
            )
    return released


# How long a `destroy_slot` the node has not acked waits before it is ordered again: ten minutes, the
# occupant reaper's pacing (`_OCCUPANT_REMINT_COOLDOWN`), since both orders sweep a tree.
_DESTROY_REMINT_COOLDOWN = timedelta(minutes=10)


def _destroy_free_dynamic(session, journal: DecisionLog) -> int:
    """Order every `free` dynamic worktree destroyed (R7), and order again a destroy the node has not acked
    within the cooldown. Returns how many destroys were ordered.

    A free dynamic worktree is a state that never persists (AC2.3): whichever edge freed it — its task
    settled, was deleted or reset, or an operator released it — this pass retires it, so one sweep serves
    every edge. A static slot is never touched. Rows are locked, so a concurrent lease waits and then finds
    the row retiring (`slot_state.lease`). A re-order never follows a recorded refusal (`destroy_error`): that
    waits for an operator. Commits."""
    ordered = 0
    rows = session.execute(
        select(Worktree, Project.name)
        .join(Project, Project.id == Worktree.project_id)
        .where(
            Worktree.kind == WORKTREE_KIND_DYNAMIC,
            Worktree.state == slot_state.FREE,
            Worktree.number >= 1,
            Worktree.retiring_at.is_(None),
        )
        .order_by(Worktree.node_id, Worktree.project_id, Worktree.number)
        # A row locked elsewhere (a lease, a converge) is left for the next pass, never waited on.
        .with_for_update(of=Worktree, skip_locked=True)
    ).all()
    for row, project in rows:
        slot_allocation.retire(session, row, reason="its dynamic worktree was freed")
        ordered += 1
        journal.record("destroy", "ORDERED", "a dynamic worktree is destroyed once it is freed",
                       subject=f"{project}#{row.number}")
    cutoff = datetime.now(timezone.utc) - _DESTROY_REMINT_COOLDOWN
    last_order = (
        select(func.max(SlotIntent.created_at))
        .where(
            SlotIntent.verb == fleet_verdicts.VERB_DESTROY_SLOT,
            SlotIntent.node_id == Worktree.node_id,
            SlotIntent.project_id == Worktree.project_id,
            SlotIntent.number == Worktree.number,
        )
        .scalar_subquery()
    )
    unacked = session.execute(
        select(Worktree, Project.name)
        .join(Project, Project.id == Worktree.project_id)
        .where(
            Worktree.retiring_at.is_not(None),
            Worktree.retiring_at < cutoff,
            Worktree.destroy_error.is_(None),
            or_(last_order.is_(None), last_order < cutoff),
        )
        .order_by(Worktree.node_id, Worktree.project_id, Worktree.number)
    ).all()
    for row, project in unacked:
        slot_allocation.remint_destroy(
            session, row, reason="the node has not acknowledged this worktree's destroy — ordering it again",
        )
        ordered += 1
        journal.record(
            "destroy", "REORDERED",
            f"the node has not acknowledged the destroy within "
            f"{_DESTROY_REMINT_COOLDOWN.total_seconds() / 60.0:.0f}m",
            subject=f"{project}#{row.number}",
        )
    session.commit()
    return ordered


def _release_stopped_question_holds(session, journal: DecisionLog) -> int:
    """Free every clean slot whose task's runner was stopped on an unanswered question. Returns how many.

    The one release of a non-terminal task's hold: the task stays `awaiting_response` and the tree is given
    back once the node sees it empty after the kill (`slot_state.stopped_question_clean_holds`). Waits on the
    kill as `_release_finished` does.
    """
    awaiting_a_kill = fleet_verdicts.slots_awaiting_a_kill(session)
    released = 0
    for hold in slot_state.stopped_question_clean_holds(session):
        subject = f"{hold['project']}#{hold['slot']} task{hold['task_id']}"
        if (int(hold["project_id"]), int(hold["slot"]), str(int(hold["task_id"]))) in awaiting_a_kill:
            journal.record(
                "release", "WAITING",
                "its stopped runner's kill has not been enacted yet — freeing the slot now would unbind "
                "the very address that kill is fenced on",
                subject=subject,
            )
            continue
        freed = _release(
            session, hold,
            f"task {hold['task_id']}'s runner was stopped on an unanswered question and its tree holds "
            "no work in progress",
        )
        released += freed
        if freed:
            journal.record(
                "release", "RELEASED",
                "its task waits on an answer with no runner and an empty tree, so the hold has nothing "
                "left to keep",
                subject=subject,
            )
    return released


def _release_leaked_user_holds(session, journal: DecisionLog) -> int:
    """Free every `user` hold that has outlived the operator's window with no session left in it.
    Returns how many were freed.

    The one place a hold ends on an inference rather than a recorded decision (operator decision 2026-09-19). The
    safety conjuncts (`user` holds only, no session sensed, a fresh observation) live in
    `slot_state.leaked_user_holds`; this owns the window and the journal. It exists because a console killed
    without its `SessionEnd` hook leaves the hold forever. Uses `_release`, so the node sweeps the tree and
    syncs it to base. Its own `try` at the call site: a failure costs its releases, not the dispatch.
    """
    window_hours = settings_reads.get_float("pool_user_hold_release_hours")
    released = 0
    for hold in slot_state.leaked_user_holds(
        session,
        held_secs=window_hours * 3600.0,
        # The freshness bound is the fleet's existing one; `pool_state` is its single home.
        fresh_secs=pool_state.max_observation_age_secs(),
    ):
        subject = f"{hold['project']}#{hold['slot']}"
        held_hours = hold["held_hours"]
        why = (
            f"a console hold with no session in it for {held_hours:.1f}h "
            f"(window {window_hours:.0f}h)"
        )
        freed = _release(session, hold, why)
        released += freed
        if freed:
            journal.record(
                "release", "RELEASED",
                f"held by a console session that never reported its exit; the node has been reporting "
                f"the tree EMPTY for {held_hours:.1f}h, past the {window_hours:.0f}h window, so the hold "
                f"was protecting nothing",
                subject=subject,
            )
        else:
            # As `_release_finished`'s KEPT: the fenced UPDATE matched no row (the hold moved). Not retried.
            journal.record(
                "release", "KEPT",
                "the generation-fenced release matched no row: the hold moved or was already free",
                subject=subject,
            )
    return released


def _disable_drained_nodes(session, summary: dict[str, Any], journal: DecisionLog) -> int:
    """Finish every drain that has run out of work: `enabled=f, draining=f` on each draining node with no
    `state='task'` slot left on it. Returns how many nodes were disabled.

    The only thing that ends a drain by itself (`app/models/nodes.py::Node.draining`). A `user` hold does not keep
    it open. Runs after `_release_finished`, whose settlements it waits on. Count and flip are one UPDATE
    with `NOT EXISTS`, so a cancel wins and a concurrent lease cannot strand a live task off `live_nodes`.
    Not a clock: a drain behind a Stopped or waiting task waits indefinitely; the remedy is the operator's.
    """
    draining = session.execute(select(Node.id, Node.name).where(Node.draining.is_(True))).all()
    if not draining:
        return 0
    disabled = 0
    for node_id, name in draining:
        moved = session.execute(
            update(Node)
            .where(
                Node.id == node_id,
                Node.draining.is_(True),
                ~select(1).where(
                    Worktree.node_id == Node.id, Worktree.state == slot_state.TASK
                ).exists(),
            )
            # Explicit on a Core UPDATE: `updated_by_id` has no `onupdate`, so it would otherwise name the
            # last editor.
            .values(enabled=False, draining=False, updated_by_id=SYSTEM_ACTOR_ID)
        ).rowcount
        if not moved:
            # `rowcount == 0` has three causes, so report the condition.
            held = session.scalar(
                select(func.count())
                .select_from(Worktree)
                .where(Worktree.node_id == node_id, Worktree.state == slot_state.TASK)
            ) or 0
            # Logged, not journaled: a journal entry would retain ~360 no-op runs an hour.
            log.info(
                "lease-drain: node %s is still draining — %d task-held slot(s) on it, or the drain was "
                "cancelled under this pass; it disables when the last one settles", name, held,
            )
            continue
        events.emit_node_upserted(session, node_id)
        session.commit()
        disabled += 1
        # Counted inside the loop because this commits per node.
        summary["drained"] += 1
        journal.record(
            "drain", "DISABLED", "its last task-held slot is gone, so the drain is complete",
            subject=name,
        )
        log.info("lease-drain: node %s finished draining — no task-held slot left, so it is now disabled", name)
    return disabled


def _release(session, hold: dict[str, Any], why: str, *, rides_pending: bool = False) -> int:
    """Free one slot and tell its node to tear it down. Returns 1 when the row was actually freed.

    Release, then intent: an undrained intent leaves a visible orphan, while tearing down an owned slot
    kills a live runner. `session_id`/`pid` name the process meant. `worktree_id` comes off the hold, never
    re-resolved.

    `rides_pending`: the caller's uncommitted writes (a charge) stand or fall with this release — committed
    with it, rolled back when the generation fence matches nothing.
    """
    if not slot_state.release(
        session, worktree_id=hold["worktree_id"], generation=hold["generation"],
        reason=f"released: {why}", commit=not rides_pending,
    ):
        if rides_pending:
            session.rollback()
        return 0
    # The slot is gone, so is any Jaah-account session dispatched into it.
    if edition_features.enabled("jaah_ai_credit"):
        from app.jaah_routing import sessions as jaah_sessions

        jaah_sessions.end_for_task(session, hold.get("task_id"), jaah_sessions.END_DECLINED)
    if hold["node_id"]:
        fleet_verdicts.issue_intents(session, hold["node_id"], [
            # `project_id` goes on the `slot_intents` row, `project` is what the node and the console render.
            fleet_verdicts.build_slot_intent(
                fleet_verdicts.VERB_RELEASE_SLOT, hold["project_id"], hold["project"], hold["slot"],
                reason=f"{why} — release the slot",
                payload={
                    "task_id": hold["task_id"],
                    "session_id": hold["session_id"],
                    "pid": hold["pid"],
                },
            )
        ])
    # Best-effort, idempotent: a session already settled keeps its first reason. Always `normal`.
    agent_session_lifecycle.note_end(
        session, agent_session_id=hold.get("session_id") or "",
        end_reason=agent_sessions.END_REASON_NORMAL,
    )
    session.commit()
    log.info("lease-drain: released %s#%s — %s", hold["project"], hold["slot"], why)
    return 1


# ── Reaping a `free` slot somebody is still sitting in ────────────────────────────────────────────────

# How long one order is given before it is given again. This pass runs every ~10s over a sensed
# population, so without a cooldown a slot whose occupant refuses to die gets a new `release_slot` every
# tick. Ten minutes, matching `_KILL_INTENT_REMINT_COOLDOWN` (`app/routes/tasks/slots.py`): an enactment sweeps
# the tree and signals every process in it. It paces, it does not terminate: the sweep ends when `session`
# reads false, or (fail-closed, silently) when an unjudged probe re-stamps `session_since` after
# `state_since`. An unsignallable occupant (EPERM) is re-ordered every ten minutes, correctly.
_OCCUPANT_REMINT_COOLDOWN = timedelta(minutes=10)

# How far back the suppression read looks, derived from the cooldown and from
# `fleet_verdicts.UNEXECUTABLE_INTENT_MAX_AGE_SECS` (nothing older can still be pending), doubled for slack.
_SUPPRESSION_SCAN_FLOOR = timedelta(seconds=fleet_verdicts.UNEXECUTABLE_INTENT_MAX_AGE_SECS * 2)

# What a node-authored string is trimmed to: `session_id` is unbounded node input rendered into
# `slot_intents.reason` and the log, where a newline forges a line. Whitespace is collapsed before the cut.
_SESSION_ID_RENDER_MAX = 64


def _rendered_session_id(raw: Any) -> str:
    """One node-authored session id, safe to interpolate into a reason string and a log line."""
    flattened = " ".join(str(raw or "").split())
    return flattened[:_SESSION_ID_RENDER_MAX] or "unidentified"


def _recent_release_orders(session, cutoff: datetime) -> set[tuple[Any, int, int]]:
    """`(node_id, project_id, slot)` for every `release_slot` intent that is still pending or was minted
    since `cutoff`: the suppression set `_reap_occupied_free_slots` checks before ordering again.

    Pending: already on its way. Recent: the pacing half, since `session` clears only after the node
    re-observes. `outcome IS NULL` is explicit because `NOT IN` over NULL is NULL.

    Counts every `release_slot`, not just the reaper's, so a slot freed in the last ten minutes is not
    reap-ordered over its own teardown. The release's own enactment kills the registered session
    (`jaahd/reconcile/enact_reset.py::_clear_tree`); this is the backstop.
    """
    return {
        (node_id, int(project_id), int(number))
        for node_id, project_id, number in session.execute(
            select(SlotIntent.node_id, SlotIntent.project_id, SlotIntent.number).where(
                SlotIntent.verb == fleet_verdicts.VERB_RELEASE_SLOT,
                # The outer bound on `created_at` makes this indexable; without it this scanned every
                # `release_slot` row every ten seconds (a predecessor read measured 44% of non-advisory-lock
                # DB time on prod). A never-acked `release_slot` outlives the floor (no purge arm takes it),
                # but it is harmless: the superseding re-mint retires it and the purge then collects it.
                SlotIntent.created_at >= cutoff - _SUPPRESSION_SCAN_FLOOR,
                or_(
                    # Single-homed via `still_pending()`, whose callers must agree with pool-tick.
                    fleet_verdicts.still_pending(),
                    SlotIntent.created_at >= cutoff,
                ),
            )
        ).all()
    }


def reap_reason(occupied_mins: float, session_id: Any, session_pid: Any) -> str:
    """The reap order's `reason`: what an operator reads on the intent row afterwards.

    A public pure builder because the reason renders on a tenant's Slot Intents page, and
    `tests/test_tenant_system_rows.py` censuses it against `tenancy/copy.operator_vocabulary_in`. Names the
    occupant; `_rendered_session_id` bounds the node-authored half and `session_pid` is `int()`-cast.
    """
    pid = int(session_pid) if session_pid is not None else "unknown"
    return (
        f"`free` for {occupied_mins:.0f}m with a live Claude session still in it "
        f"(session {_rendered_session_id(session_id)}, pid {pid}) — clear the tree"
    )


def _reap_occupied_free_slots(session, journal: DecisionLog) -> int:
    """Order the tree cleared for every `free` slot the node still reports a live session in. Returns how
    many orders this pass issued.

    `free` with a live session is not a state this system offers: this reaps it (the population is
    `slot_state.occupied_free_slots`). The verb is `release_slot`, not `kill_runner`: the kill executor is
    task-binding fenced and these rows have `task_id` NULL, while `release_slot` addresses the slot and its
    enactment (`_clear_tree`) sweeps the tree and kills the registered session. No row transition: the row
    is already `free`; only the enactment failed.

    Four fences, none in this body: (1) `occupied_free_slots`' console, generation, affirmative-session and
    freshness conjuncts; (2) the node re-verifies the `session: True` precondition; (3) `_enact_release_slot`
    re-reads the CP's projection, catching a re-lease; (4) the node re-checks the pid's start time before
    signalling. Best effort; `issue_intents` commits per slot, so a mid-loop failure leaves earlier orders.
    """
    point = "free-slot-reap"
    cutoff = datetime.now(timezone.utc) - _OCCUPANT_REMINT_COOLDOWN
    suppressed = _recent_release_orders(session, cutoff)
    ordered = 0
    for row in slot_state.occupied_free_slots(
        session,
        grace_secs=slot_state.FREE_SLOT_OCCUPANT_GRACE_SECS,
        # The fleet's existing freshness floor (`pool_state`), as in `_release_leaked_user_holds`.
        fresh_secs=pool_state.max_observation_age_secs(),
    ):
        subject = f"{row['project']}#{row['slot']}"
        occupied_mins = row["occupied_secs"] / 60.0
        if (row["node_id"], int(row["project_id"]), int(row["slot"])) in suppressed:
            journal.record(
                point, "WAITING",
                f"a release order for this slot is already pending or was issued inside the "
                f"{_OCCUPANT_REMINT_COOLDOWN.total_seconds() / 60.0:.0f}m cooldown — the node has not "
                f"re-observed the tree yet",
                subject=subject,
            )
            continue
        why = reap_reason(occupied_mins, row["session_id"], row["session_pid"])
        fleet_verdicts.issue_intents(session, row["node_id"], [
            fleet_verdicts.build_slot_intent(
                fleet_verdicts.VERB_RELEASE_SLOT,
                row["project_id"], row["project"], row["slot"],
                reason=why,
                payload={
                    # The marker is stated, never inferred from a missing `task_id`:
                    # `slot_teardown.end_holder` also mints this verb with no task for a `user` hold, and
                    # taking the reap fork there would report `done` unswept.
                    "reap": True,
                    # When the slot was freed, so the node applies per session the rule the CP applied per
                    # slot (see `occupied_free_slots`).
                    "freed_at": row["freed_at"],
                    # No `task_id`: none holds this slot.
                    "task_id": None,
                    # Informational, not a fence: the executor re-derives the pid from its own probe. Here so
                    # the row says who was ordered killed; bounded like the reason string.
                    "session_id": _rendered_session_id(row["session_id"]),
                    "pid": row["session_pid"],
                },
                # The doubt this order carries must carry a reading: `reconcile.execute.execute_intent` ignores
                # preconditions whose expected value is None, so `{"session": None}` would fence nothing.
                # `observed_at` is deliberately absent, so `release_slot` stays unstamped
                # (`jaahd/reconcile/vocab.py::_UNSTAMPED_VERBS`) and pacing is `_OCCUPANT_REMINT_COOLDOWN`. A
                # re-lease between mint and enactment is not caught here but in `_enact_release_slot`.
                observation={"session": True},
                precondition_fields=("session",),
            )
        ])
        ordered += 1
        journal.record(
            point, "ORDERED",
            f"the control plane has promised this tree to nobody for {occupied_mins:.0f}m and the node "
            f"still reports a live session in it; the next dispatch would reset the tree under that "
            f"session, so it is cleared now",
            subject=subject,
        )
        log.warning(
            "lease-drain: ordering %s#%s cleared on %s — %s",
            row["project"], row["slot"], row["node_name"] or "unknown-node", why,
        )
    return ordered


def _resolved_env_for(session, project_id: int, decision) -> Optional[project_env.SlotEnv]:
    """The CP-configured environment one dispatch carries, already resolved, or None.

    A map on the intent; the node writes the slot's `.env` on claim and exports nothing into the runner.
    `app/project_env.py::resolve_for_slot` is the only resolution rule. Overrides are the task launcher's
    (`task_ancestry.launcher_user_id`). Not resolved on a resume: that would decrypt credential material for
    an answer `fleet_verdicts.build_lease_intent` discards.
    """
    if decision.resume:
        return None
    # `resolve_for_slot` reads under `no_autoflush`, so flushing pending work is the caller's job.
    session.flush()
    env = project_env.resolve_for_slot(
        session, project_id, task_ancestry.launcher_user_id(session, decision.task_id)
    )
    if env.values or env.omit:
        # Names, never values. `%r` for omitted names only: they may fail the env-var grammar and carry a
        # newline.
        log.info(
            "lease-drain: task %s carries %d configured variable(s) (%s); %d omitted (%r)",
            decision.task_id, len(env.values), ",".join(sorted(env.values)) or "-",
            len(env.omit), env.omit,
        )
    return env


def _git_author_for(session, task_id: object) -> Optional[dict[str, str]]:
    """`git_access.commit_identity_for_task` for the dispatched row, or None when the row is gone."""
    task = session.get(Task, task_id)
    return git_access.commit_identity_for_task(session, task) if task is not None else None


def _port_band_for(
    project_row: Optional[Project], slot: int, grid: port_grid.Grid
) -> dict[str, int]:
    """The deterministic port grid's band for one slot (`fleet_decide.port_band`). Empty when the project has no
    `port_block`, since inventing one would collide.

    `grid` (`app/port_grid.py`) is passed in so the whole plan uses one grid.
    """
    if project_row is None or project_row.port_block is None:
        return {}
    return fleet_decide.port_band(
        project_row.port_block,
        slot,
        project_row.port_vars,
        range_start_by_var=grid.range_start_by_var,
        block_size=grid.block_size,
        stride=grid.stride,
    )


# Each worker holds a DB session for its whole plan, so the width is the pool's, not CPU's.
_MAX_NODE_WORKERS = 8

# ── Why a planned dispatch produced no lease ────────────────────────────────────────────────────────────

# The ledger's words for `routes/fleet.py::DispatchDecision.decision` codes. `skip-no-granted-account` is
# absent (a per-node fact via `_report_decline`). An unknown code renders as itself, never silence. Spelled
# as literals: the drain imports `routes/fleet.py` only when it runs (its vocabulary's home).
_PLAN_SKIP_REASONS: dict[str, str] = {
    "skip-inflight": "a runner is already live on this task; a second slot would be a double dispatch",
    "skip-jaah-held": "held on the Jaah AI account (its platform account, balance or model) — it "
    "waits as ready and is not billed",
    "skip-acp-adapter-missing": "its run needs an ACP engine adapter, which this node does not "
    "attest yet (still installing its runner tools)",
}


def _provider_label(decision: str) -> str:
    """The vendor a `skip-no-account-of-bound-provider:<provider>` decision is waiting for, in the console's
    words (the catalogue `label` from `app/agent_providers.py`).

    `tasks.bound_provider` is open-vocabulary, so an unrecognised value is echoed rather than guessed, and
    capped since it reaches a human-read line.
    """
    _, _, value = decision.partition(":")
    record = agent_providers.by_value(value)
    if record is not None:
        return record.label
    # `!r` for the unrecognised arm only, as `routes/fleet.py`'s log does: quotes mark a raw value and `repr`
    # escapes control characters. Unreachable on this build; the fail-closed shape for a newer one.
    return repr(value[:64]) if value else "an unnamed vendor"


# The scheduler's sentences for an account no node has logged in (`jaah_scheduler.EXCLUSION_REASONS`).
_UNATTESTED_REASONS = frozenset(
    reason for key, _, reason in jaah_scheduler.EXCLUSION_REASONS
    if key in ("account_unobserved", "node_reports_no_credentials")
)
_TALLY_ENTRY = re.compile(r"\d+x (.+)")


def _only_unattested(why: Optional[str]) -> bool:
    """Whether the plan's tally (`routes/fleet.py::_no_account_why`, "<n>x <reason>; …") refused every account
    only for not being logged in on this node."""
    if not why:
        return False
    entries = [_TALLY_ENTRY.fullmatch(part.strip()) for part in why.split(";")]
    return all(entry is not None and entry.group(1) in _UNATTESTED_REASONS for entry in entries)


def _node_name(session, node_id: str) -> str:
    return session.scalar(select(Node.name).where(node_matches(node_id))) or node_id


# The log's dedup state; kept because the journal records every decline, so the log can stay quiet
# (see `_report_decline`).
_declines: dict[tuple[str, str, object], str] = {}
_declines_lock = threading.Lock()


def _report_decline(
    journal: DecisionLog, node_id: str, project: str, task_id: object, reason: str
) -> None:
    """Journal why a task the plan said to dispatch did not get a lease, and log it once per
    new-or-changed reason.

    The journal is written first and unconditionally; the log dedups. The log is a stream a human tails, so
    a waiting task is reported once; the ledger is per-tick evidence, so it repeats every pass (omitting an
    unchanged reason would claim the pass decided nothing).
    """
    journal.record("dispatch", "DECLINED", reason, subject=f"{node_id}/{project} task{task_id}")
    # Beside the ledger entry: this is what `_fail_abandoned_dispatches` reads so it spares this task.
    journal.note_declined_task(task_id)
    key = (node_id, project, task_id)
    with _declines_lock:
        if _declines.get(key) == reason:
            return
        _declines[key] = reason
    log.info(
        "lease-drain: node %s — task %s planned for dispatch in %s but NOT leased: %s "
        "(logged once until this reason changes)",
        node_id, task_id, project, reason,
    )


def _clear_decline(node_id: str, project: str, task_id: object) -> None:
    """Forget a task's last decline, so a later stall on the same task reports again."""
    with _declines_lock:
        _declines.pop((node_id, project, task_id), None)


def _dispatch_node_isolated(node_id: str, journal: DecisionLog, allocation: dict[int, str],
                            pass_facts=None) -> tuple[int, int]:
    """One node's dispatches on a session of its own: the unit `drain_once` fans out over.

    A session per node because a SQLAlchemy `Session` is not thread-safe; nodes cannot contend for a slot,
    and `slot_state.lease` is an atomic conditional UPDATE anyway. Rolls back before re-raising. Returns
    `(intents issued, decisions contained)`; see `_dispatch_node` for the second.

    Enters the fleet-wide plane here: a `ContextVar` does not cross a `ThreadPoolExecutor`, so the worker
    starts unbound and the enforcing seam would return no projects. Uses `detached()` + `operator_scope()`
    (`seam.detached`). The drain is irreducibly fleet-wide;
    per-task org confinement is explicit (`placement_grants.allowed_nodes`/`allowed_accounts`,
    `slot_state.lease`'s org conjunct). The `cron:lease-drain` caller label is also re-established here, or
    every read lands as `unattributed`. It agrees with `_run_activity`'s label only via the seeded cron
    row's `activity_type`, not the constant directly. The import is local as a prophylactic.
    """
    from app.cron_system_activities import LEASE_DRAIN

    contained: list[str] = []
    dispatched: set[int] = set()
    try:
        with (
            exclusion_log.caller(f"cron:{LEASE_DRAIN}"),
            seam.detached(),
            seam.operator_scope(),
            SessionLocal() as session,
        ):
            try:
                return _dispatch_node(session, node_id, journal, allocation, contained_out=contained,
                                      pass_facts=pass_facts, dispatched_out=dispatched), len(contained)
            except Exception:
                session.rollback()
                raise
    finally:
        # Every account place this node's plan booked for a decision that did not go out (no slot, a disk or
        # memory wait, a refused take, a contained or uncontained raise) goes back to the pass's other nodes.
        if pass_facts is not None:
            pass_facts.give_back_undispatched(node_id, dispatched)


def _shared_store_facts(session, node_id: str, *, accounts: Optional[list] = None,
                        ) -> tuple[frozenset[str], bool, str]:
    """`(the slugs of every shared account, does this node report a mounted token share, where)`: what an
    OLDER node needs to be told its token's `securestorage_dir` (a newer one finds the token itself, in its
    config dir or `.shared/<slug>`, and ignores the field). Never a dispatch gate: the node's own presence
    reports decide (`account_unobserved`, `node_reports_no_credentials`).

    The share is the node's own report (`nodes.shared_accounts_root`, checked on ingest), not a CP switch, and
    only while the node's org may use NodeLink.
    Two statements per node pass, not per dispatch. Slugs, not ids, because the loop holds a `config_dir`
    path and the slug is what is recoverable from it (`node_clone.account_slug_from_dir`). A node with no row
    answers `False` and root `""`. `accounts` is the pass's account rows (`routes/fleet.py::account_rows`),
    which spare the slug read.
    """
    shared_slugs = (
        frozenset(row.slug for row in accounts if row.shared is True) if accounts is not None
        else frozenset(session.scalars(select(Account.slug).where(Account.shared.is_(True))).all())
    )
    row = session.execute(select(Node.shared_accounts_root, Node.org_id).where(node_matches(node_id))).first()
    # An org whose NodeLink plan term is off is told no share, as before the switch was retired.
    share_root = (
        str(row.shared_accounts_root)
        if row and row.shared_accounts_root and tenant_defaults.shared_store_allowed(session, row.org_id)
        else ""
    )
    return shared_slugs, bool(share_root), share_root


def _account_providers(session, *, accounts: Optional[list] = None) -> dict[str, str]:
    """`{slug: provider}` for every account row (which vendor's CLI an account launches), read once.

    One statement per node pass, hoisted like `_shared_store_facts`, so a mid-pass edit applies to the next
    pass whole. Slugs because the loop holds an account reference (`account_auth.account_slug_from_ref`).
    `accounts.provider` is NOT NULL, so a missing slug means no row, which is `_resolve_account`'s business.
    `accounts` is the pass's account rows (`routes/fleet.py::account_rows`), read once for every node.
    """
    if accounts is not None:
        return {row.slug: row.provider for row in accounts}
    return {
        slug: provider
        for slug, provider in session.execute(select(Account.slug, Account.provider)).all()
    }


def _stopped_holders_reader(session, node_id: UUID, project_id: int) -> Callable[[], list[int]]:
    """The ids of the stopped (`paused`) tasks holding this node's slots for `project_id`, read at most once per
    repo pass and only when a "no free worktree" decline asks (`dispatch_wait.no_free_worktree`)."""
    found: list[list[int]] = []

    def read() -> list[int]:
        if not found:
            found.append(list(session.scalars(
                select(Task.id)
                .join(Worktree, Worktree.task_id == Task.id)
                .where(Worktree.node_id == node_id, Worktree.project_id == project_id,
                       Worktree.state == slot_state.TASK, Task.state == task_lifecycle.PAUSED)
                .order_by(Task.id)
            ).all()))
        return found[0]

    return read


def _dispatch_node(session, node_id: str, journal: DecisionLog, allocation: dict[int, str],
                   *, contained_out: list[str] | None = None,
                   pass_facts=None, dispatched_out: set[int] | None = None) -> int:
    """Decide and issue this node's dispatches. Returns how many `lease_slot` intents were issued.

    `contained_out` is the pass's only evidence that containment happened: the per-decision `try` turns a
    raise into one declined unit, and without a count a node whose every decision raised would look like a
    healthy node with no free slots. It is an appended out-collector (exception type names) rather than a
    second return value, keyword-only and defaulted, so existing callers are untouched.

    `allocation` is this pass's task-to-node table (`routes/fleet.py::fleet_allocation`), built once and
    shared unchanged: its inputs move within a pass, and per-node tables could lease two slots for one task.
    `pass_facts` (`routes/fleet.py::DispatchPassFacts`) is the same build's shared, node-independent reads.
    `dispatched_out` collects the id of every task whose model-run intent went out.
    """
    # Deferred: `app/routes/fleet.py` pulls the DB stack and GitHub client.
    # `SKIP_NO_ACCOUNT_OF_BOUND_PROVIDER` is the plan's vocabulary, spelled there once.
    from app.routes.fleet import (
        SKIP_ACP_ADAPTER_MISSING,
        SKIP_AGENT_UNRUNNABLE,
        SKIP_DYNAMIC_REFUSED,
        SKIP_JAAH_HELD,
        SKIP_NO_ACCOUNT_OF_BOUND_PROVIDER,
        SKIP_NODE_CANNOT_RUN_AGENTS,
        SKIP_NODE_MISSING_RUNNER_CLI,
        DispatchPlanIn,
        _max_runners_per_account,
        build_dispatch_plan,
    )

    if edition_features.enabled("microvm"):  # The community edition has no MicroVMs.
        from app import microvm_placement

        if microvm_placement.is_microvm_node(session, node_id):
            # A MicroVM node runs every session in its own VM (D21): no slot, no plan — admit what the pass's
            # allocation gave it. Static nodes go on through the unchanged plan below.
            return microvm_placement.dispatch_node(
                session, node_id, allocation,
                record=lambda decision, why, subject: journal.record("microvm", decision, why, subject=subject),
            )
    # `node_id` is the node's uuid string (`pool_state.live_nodes`), what the plan keys on.
    plan = build_dispatch_plan(DispatchPlanIn(node_id=node_id), session, allocation=allocation,
                               pass_facts=pass_facts)
    # The plan opened a Jaah-account session row per Jaah decision (`app/jaah_routing/`), uncommitted.
    jaah_planned = any(decision.bedrock for repo in plan.repos for decision in repo.dispatches)
    if not plan.pool_state_ok:
        if jaah_planned:
            # Nothing dispatches this pass, so none of them may outlive it.
            session.rollback()
        journal.record(
            "dispatch", "DISABLED",
            plan.pool_state_detail or "the pool occupancy read is not ok, so dispatch fails closed",
            subject=node_id,
        )
        log.warning(
            "lease-drain: node %s — DISPATCH DISABLED this pass: %s",
            node_id, plan.pool_state_detail or "pool state not ok",
        )
        return 0
    if jaah_planned:
        # Committed now, before any decision's containment rollback below could discard the rows of the others;
        # every one that does not go out is ended after the loop.
        session.commit()

    # One grid for the whole pass, read once, so two slots of a project cannot be banded off different grids.
    # It is this node's org's grid, passed in: the cron thread has nothing bound, and a host belongs to one
    # org for life.
    org = node_org(session, node_id)
    # Per org, so the pass's first node of an org reads it for the rest (a raise is not memoized: every node of
    # that org raises as before).
    grid = pass_facts.grid_by_org.get(org) if pass_facts is not None else None
    if grid is None:
        grid = port_grid.grid(session, org)
        if pass_facts is not None:
            pass_facts.grid_by_org.setdefault(org, grid)
    # The pass's account rows (`DispatchPassFacts.accounts`) when the drain read them, else this node's own.
    accounts = pass_facts.accounts if pass_facts is not None else None
    # The shared-credential facts for this pass, hoisted like the grid (see `_shared_store_facts`).
    shared_slugs, mounts_share, share_root = _shared_store_facts(session, node_id, accounts=accounts)
    # This pass's provider map, hoisted likewise (see `_account_providers`).
    account_providers = _account_providers(session, accounts=accounts)

    # This node's disk launch decision, read once (`app/launch_disk.py`): blocked, no decision here launches.
    # Memory is read per decision below instead (`app/launch_memory.py`): its runner ceiling counts the leases this
    # pass has already written, leaves the deciding task's own held slot out, and never holds a script run.
    disk_wait = launch_disk.wait_reason(session, node_id)
    # CPU likewise, once (`app/launch_cpu.py`): saturation holds every launch here, a lane run's included, except
    # a resume into the task's own held slot (below).
    cpu_wait = launch_cpu.wait_reason(session, node_id)

    issued = 0
    # Tasks whose model-run dispatch went out this pass: every other Jaah decision's session is ended below, and
    # every other booked account place given back (`_dispatch_node_isolated`).
    jaah_issued: set[int] = dispatched_out if dispatched_out is not None else set()
    # Why each task this node declined is waiting, in the console's words; stamped after the loop.
    waits: dict[int, str] = {}
    # This node's name, read once and only for a wait that names it (`dispatch_wait.no_account_logged_in`).
    node_name: Optional[str] = None
    for repo in plan.repos:
        if repo.skipped_reason:
            # Project, not node, in the subject: every reason here is a fact about the project row, so the
            # entry collapses across nodes.
            journal.record("dispatch", "SKIPPED", repo.skipped_reason, subject=repo.project)
            log.info("lease-drain: node %s — %s skipped: %s", node_id, repo.project, repo.skipped_reason)
            continue
        if repo.has_repo and not repo.base:
            # A folder project (`has_repo` false) has no base and needs none.
            # Fail closed on the one mandatory value the composer needs (not `branch_prefix`, whose empty
            # value is legitimate, nor the gate commands, whose empty value means "no gate").
            journal.record(
                "dispatch", "SKIPPED",
                "incomplete project config: base blank, so dispatch fails closed",
                # Project-level: this value is a project column no node can differ on.
                subject=repo.project,
            )
            log.warning(
                "lease-drain: node %s — %s carried an incomplete project config (base blank), "
                "skipping (fail-closed)",
                node_id, repo.project,
            )
            continue
        if not repo.dispatches:
            # Nothing to issue, so no identity to read (one SELECT per project per node).
            continue
        # The project row is the identity source for this repo's dispatches, read once, so the fenced id and
        # the `port_block` cannot come from different reads.
        # By the plan's `project_id`, never its display name: names are unique per org, not fleet-wide.
        project_row = session.scalars(select(Project).where(Project.id == repo.project_id)).first()
        if project_row is None:
            # Fail closed: a lease needs an identity (migration 0104 forbids inventing one). A plan id
            # resolving to nothing means the project was deleted mid-pass; decline the repo and re-plan next
            # pass.
            journal.record(
                "dispatch", "SKIPPED",
                "the plan names a project that resolves to no projects row (deleted since the plan), and a "
                "lease needs an identity that cannot be invented",
                # Project-level: the row is missing fleet-wide, not on this host.
                subject=repo.project,
            )
            log.warning(
                "lease-drain: node %s — the plan names project %r, which resolves to no `projects` row; "
                "skipping its dispatches this pass (fail-closed: a lease needs an identity and this one "
                "cannot be invented)",
                node_id, repo.project,
            )
            continue
        project_id = project_row.id
        stopped_holders = _stopped_holders_reader(session, node_id, project_id)
        for decision in repo.dispatches:
            if decision.decision.startswith(f"{SKIP_NO_ACCOUNT_OF_BOUND_PROVIDER}:"):
                # R4.3: the wait an operator can act on. Renders the vendor in the catalogue's words and says
                # what ends the wait, instead of a raw decision code. The actionable clause comes first
                # because the ledger clips at `_MAX_WHY_CHARS`. No indefinite article before the label (a/an
                # cannot be decided from a label this module does not own); pinned by
                # `tests/test_lease_drain_dispatch.py`.
                label = _provider_label(decision.decision)
                waits[decision.task_id] = dispatch_wait.no_account_of(label)
                _report_decline(
                    journal, node_id, repo.project, decision.task_id,
                    f"WAITING on {label} — this task has already run on {label} and is never handed "
                    f"to another vendor. No {label} account is dispatchable here now (quota, auth, "
                    f"no credential dir, or none granted to its launcher)",
                )
                continue
            if decision.decision == SKIP_NODE_MISSING_RUNNER_CLI:
                waits[decision.task_id] = dispatch_wait.NODE_MISSING_CLI
                # A host fault, not an account wait: every account is refused here for a missing runner
                # CLI, so the fix is on the node (`nodes.runner_clis`), never in the account roster.
                _report_decline(journal, node_id, repo.project, decision.task_id,
                                "this node attests no runner CLI the accounts need on the runner's PATH "
                                "(nodes.runner_clis), so every account is refused here — fix the CLI "
                                "install or its permissions on the node, not the accounts")
                continue
            if decision.decision == "skip-no-granted-account":
                if _only_unattested(decision.why):
                    if node_name is None:
                        node_name = _node_name(session, node_id)
                    waits[decision.task_id] = dispatch_wait.no_account_logged_in(node_name)
                else:
                    waits[decision.task_id] = dispatch_wait.NO_ACCOUNT
                # Reported via `_report_decline`: a task waiting on an account (quota/auth), not terminal
                # (`unplaceable_reason` fails only a task with no enabled usable account). The concurrency
                # cap is named, with the number that binds (an account's own cap, else the fleet's —
                # `jaah_scheduler.runner_cap`), because `_best_account` returns a bare `None` and nothing
                # else would tell the operator. So is Auto-assign, the other silent narrowing. Kept under
                # `_MAX_WHY_CHARS`, so the cap's number is not clipped off the end.
                # The plan's tally (`routes/fleet.py::_no_account_why`) names the actual exclusions, first
                # so the clip cannot take it; the generic list stays for a pinned task, which has none.
                _report_decline(journal, node_id, repo.project, decision.task_id,
                                f"no usable account on this node: {decision.why}" if decision.why else
                                "no credential account this task's launcher is granted is usable "
                                "from this node right now — waiting: quota, auth, no credential dir "
                                "here, Auto-assign off, or each at its runner cap (its own if set, "
                                f"else max_runners_per_account = {_max_runners_per_account()})")
                continue
            if decision.decision == SKIP_NODE_CANNOT_RUN_AGENTS:
                waits[decision.task_id] = dispatch_wait.held_on_node_without_sdk()
                _report_decline(journal, node_id, repo.project, decision.task_id,
                                "this task runs an SDK agent and this node cannot run one (no `agent` launch "
                                "kind reported, or not an Ubuntu node); it is placed on a node that can")
                continue
            if decision.decision == SKIP_AGENT_UNRUNNABLE:
                # The prelude spends the attempt (`_fail_unrunnable_agents`); nothing is leased here.
                _report_decline(journal, node_id, repo.project, decision.task_id,
                                f"its agent cannot run: {decision.why}")
                continue
            if decision.decision == SKIP_DYNAMIC_REFUSED:
                waits[decision.task_id] = dispatch_wait.no_free_worktree(stopped_holders())
                # R6: no free static slot and no dynamic worktree may be made here; the task stays `ready`.
                _report_decline(journal, node_id, repo.project, decision.task_id,
                                f"no free static slot, and no dynamic worktree can be made: {decision.why}")
                continue
            if decision.decision != "dispatch":
                if decision.why and decision.decision in (
                        SKIP_JAAH_HELD, SKIP_ACP_ADAPTER_MISSING):
                    waits[decision.task_id] = decision.why
                journal.record(
                    "dispatch", "SKIPPED",
                    _PLAN_SKIP_REASONS.get(
                        decision.decision, f"the plan decided {decision.decision}"
                    ),
                    # No node in the subject: these reasons are facts about the task, so nodes collapse onto
                    # one entry.
                    subject=f"{repo.project} task{decision.task_id}",
                )
                continue
            if decision.lease_target is None:
                # `select_slot` returns None for a full or undeclared pool; no target is a refusal.
                waits[decision.task_id] = dispatch_wait.no_free_worktree(stopped_holders())
                _report_decline(journal, node_id, repo.project, decision.task_id,
                                "no free slot in the pool")
                continue
            if disk_wait is not None:
                waits[decision.task_id] = disk_wait
                if pass_facts is not None:
                    # Now, not at the node's end: another node is planning on this account meanwhile.
                    pass_facts.give_back(node_id, decision.task_id)
                _report_decline(journal, node_id, repo.project, decision.task_id,
                                # No figure here: it moves every pass, and the log dedups on this text.
                                "the node's disk blocks launches (disk gate paused, or free disk below "
                                "runner_launch_disk_floor_gb); free disk on the node or lower the floor")
                continue
            # A merge-lane run is held by memory pressure only: the runners waiting on its merge fill the ceiling,
            # so the ceiling holding it would deadlock the node.
            lane = session.scalar(select(Task.persona).where(Task.id == decision.task_id)) == "merge_lane"
            memory_wait = launch_memory.wait_reason(session, node_id, task_id=decision.task_id, lane=lane)
            if memory_wait is not None:
                waits[decision.task_id] = memory_wait
                if pass_facts is not None:
                    # Now, not at the node's end: another node is planning on this account meanwhile.
                    pass_facts.give_back(node_id, decision.task_id)
                _report_decline(journal, node_id, repo.project, decision.task_id,
                                # No figure, as for disk: the log dedups on this text.
                                "the node's memory blocks launches (critical within the hold window, or its "
                                "runners fill it at runner_memory_reservation_gb each); wait, or lower the "
                                "reservation")
                continue
            # A resume replaces the runner its own slot already carried, so it adds no runner to the node; holding
            # it held a task whose run had only paused (once for 11 minutes after a usage limit).
            if cpu_wait is not None and not _resumes_own_slot(session, node_id, repo.project_id, decision):
                waits[decision.task_id] = cpu_wait
                if pass_facts is not None:
                    # Now, not at the node's end: another node is planning on this account meanwhile.
                    pass_facts.give_back(node_id, decision.task_id)
                _report_decline(journal, node_id, repo.project, decision.task_id,
                                # No figure, as for disk: the log dedups on this text.
                                "the node's CPU blocks launches (saturated through the hold window); wait for "
                                "its runners to finish")
                continue
            if decision.agent_id:
                # An SDK agent: no account, no compose, no `bind_account` (`_dispatch_agent`). Contained to this one decision like the model path below.
                try:
                    if _dispatch_agent(session, node_id, journal, repo, project_row, decision, grid):
                        issued += 1
                except Exception as exc:  # noqa: BLE001 — one row must not cost this node the rest of its pass
                    try:
                        session.rollback()
                    except Exception:  # noqa: BLE001 — a lost connection must not cost the ledger entry
                        log.exception("lease-drain: node %s — rolling back after a contained agent dispatch "
                                      "failure itself raised", node_id)
                    _report_decline(
                        journal, node_id, repo.project, decision.task_id,
                        f"the agent dispatch raised {type(exc).__name__} and THIS UNIT ALONE was declined; "
                        f"the next pass retries it",
                    )
                    log.exception("lease-drain: node %s — the agent dispatch of task %s onto %s#%s raised",
                                  node_id, decision.task_id, repo.project, decision.lease_target)
                    if contained_out is not None:
                        contained_out.append(type(exc).__name__)
                continue
            resolved = _resolve_account(
                journal, node_id, repo, decision, shared_slugs, mounts_share, share_root,
                account_providers,
            )
            if resolved is None:
                continue

            # Bound here so they exist however early the `try` dies (a `NameError` in the handler would defeat
            # the containment). `bound_slot` is the compensation handle (a lease to give back); `dispatched`
            # tells the journal the intent already went out.
            bound_slot: dict | None = None
            dispatched = False

            # ── ONE DECISION'S FAILURE IS ONE DECISION'S FAILURE ────────────────────────────────────
            # Contains a raise (e.g. the composer on an operator-typed row, which would otherwise repeat
            # every ten seconds) to one decision instead of the node's whole pass. Broad on purpose: nothing
            # is contained silently (ledger decline plus traceback) and every write inside commits itself.
            # Scoped to take, compose and intent; plan/project/allocation failures are not per-task, and
            # `_resolve_account` stays outside (no session; its plausible raise is a node mount fact).
            try:
                with _taken_slot(session, node_id, project_id, repo.project, decision) as taken:
                    if taken is None:
                        # Somebody else holds it: the plan's busy set is a snapshot and this take is the
                        # authority. Skip.
                        _report_decline(
                            journal, node_id, repo.project, decision.task_id,
                            f"slot {decision.lease_target} was already held when the lease was taken",
                        )
                        continue
                    if taken.get("declined"):
                        # Nothing was taken: the take ended the task (its unannounced launch was the last
                        # attempt, so `end_attempt` released the slot), or no dynamic worktree was made.
                        _report_decline(journal, node_id, repo.project, decision.task_id, taken["declined"])
                        continue
                    # The SLOT's kind, never the project's (`Worktree.has_repo`): a held git worktree
                    # resumed after its project's repository went is still a git worktree, and a folder is never
                    # handed a repository. A missing row reads as git, as on the node.
                    slot_has_repo = session.scalar(
                        select(Worktree.has_repo).where(Worktree.id == taken["worktree_id"])
                    ) is not False
                    # The operator's runtime switch for this account's pair, fixed for this attempt: a spillover
                    # re-pick is a new attempt and resolves again (`app/acp_runtime.py`).
                    runtime = acp_runtime.dispatch_runtime(
                        resolved.provider, api_key_account=resolved.api_key_slug, bedrock=bool(decision.bedrock),
                        node_os=node_platform.session_platform(session.execute(
                            select(Node.os, Node.arch, Node.microvm).where(node_matches(node_id))).first())[0])
                    verdict = _compose_or_undo(
                        journal, session, node_id, project_id, repo, decision, taken,
                        provider=resolved.provider, slot_has_repo=slot_has_repo,
                    )
                    if verdict is None:
                        continue
                    # Bind the slot to its account before the intent goes out, since the runner fetches its
                    # key in `launch.sh` before the node next reports. After `_compose_or_undo`, because on
                    # the `from_own_slot` path a declined verdict keeps the slot, and a prior bind would let
                    # `get_account_api_key` serve that account's key. An API-key account materializes no
                    # credential dir, so observation would never bind it (`slot_state.bind_account`).
                    slot_state.bind_account(
                        session,
                        worktree_id=taken["worktree_id"],
                        generation=taken["generation"],
                        account_id=account_id_for_dir(session, resolved.ref),
                    )
                    # Recorded the instant it is true: `bind_account` commits, so only this tells the handler
                    # to give it back.
                    bound_slot = taken
                    # Resolved inside the guarded block so a resolver DB or `TenancyScopeConflict` error lands
                    # in `_taken_slot`'s handler and the slot is given back. Other resolver failures only omit
                    # a name.
                    slot_env = _resolved_env_for(session, project_id, decision)
                    intent = fleet_verdicts.build_lease_intent(
                        # Identity then display: `project_id` for the row, `repo.project` for the node's
                        # roster, tmux window and logs. The node payload is unchanged for skew (`apps/jaahd`
                        # ships independently).
                        project_id, repo.project, decision.lease_target,
                        task_id=decision.task_id,
                        # A folder has no base (`slot_has_repo` above).
                        base=repo.base if slot_has_repo else None,
                        # The literal, not a constant: `slot_state` dropped `HOLDER_FLEET`, but the node still
                        # reads this wire field, so dropping it would break skew.
                        holder="fleet",
                        generation=taken["generation"],
                        dispatch_id=taken["dispatch_id"],
                        config_dir=resolved.config_dir,
                        # Spelled on every intent, `None` included, for the same skew reason.
                        securestorage_dir=resolved.securestorage_dir,
                        # The API-key kind in its own field (see `ResolvedAccount`): `""` for OAuth, an
                        # account slug (never a key) otherwise; the node resolves it over its own credential
                        # socket.
                        api_key_account=resolved.api_key_slug,
                        # Which vendor's CLI the node launches, off this account's `accounts.provider`;
                        # without it every node launched the default provider. Always spelled (`build_lease_intent`).
                        provider=resolved.provider,
                        # Resolved above, once.
                        runtime=runtime,
                        # The Jaah block (K2), only on a Jaah-account dispatch.
                        bedrock=decision.bedrock,
                        # Who the runner commits as: the person whose credential its push uses
                        # (`git_access.git_person_for_task`).
                        git_author=_git_author_for(session, decision.task_id),
                        # A folder slot runs no stack, so it gets no band (its roster entry carries none).
                        ports=_port_band_for(project_row, decision.lease_target, grid) if slot_has_repo else {},
                        # Spelled only when the slot or its project has no repository (`slot_allocation._kind_payload`).
                        has_repo=None if slot_has_repo and project_row.has_repo else slot_has_repo,
                        # The only key that may be absent and the only one with plaintext;
                        # `build_lease_intent` decides.
                        env_vars=slot_env,
                        resume=decision.resume,
                        prompt={
                            "system_prompt": verdict["system_prompt"],
                            "task_prompt": verdict["task_prompt"],
                            "model": verdict["model"],
                            "title": verdict["title"],
                            "attachments": verdict["attachments"],
                            "attachments_dir": verdict["attachments_dir"],
                            # The REQUIRED-input staging instruction, carried beside the supplementary one and
                            # acted on separately by the node: a failure here fails the intent (AC9.4).
                            "input_artifacts": verdict["input_artifacts"],
                            "input_artifacts_dir": verdict["input_artifacts_dir"],
                            # Whether the node may reap this runner as idle (`VISIBLE_AUTOCLOSE`): false only
                            # for `advisor`, a live conversation that may idle for hours. `.get(..., True) is
                            # not False`, so only an explicit False turns it off; always spelled, and the node
                            # applies the same rule (`reconcile.enact_launch._launch_into_slot`).
                            "autoclose": verdict.get("autoclose", True) is not False,
                            # Only a merge-lane run: its node wires the turn-end refusal (`check_lane_stop.py`).
                            # Spread, so every other launch's bundle is byte-for-byte what it was.
                            **({"lane": True} if merge_lane_state.is_lane_persona(
                                session.scalar(select(Task.persona).where(Task.id == decision.task_id))) else {}),
                        },
                        # Present only when this task's memory arm is ON; OFF leaves the payload unchanged.
                        memory=lease_memory_of(verdict),
                        # Present only where the Architectural Guard is available; elsewhere the payload is
                        # unchanged (`guard_stanza`).
                        guard=guard_stanza(session, project_id),
                    )
                    # One bare call: `issue_intents` already supersedes this slot's pending `lease_slot` rows
                    # and carries their budget; a second supersede would retire this mint's own
                    # row.
                    # A built-in run carries no SDK agent stamp (a stale one would steer the bundle route and
                    # answer delivery); a no-op unless this task once ran an agent.
                    agent_resolution.stamp_run(session, decision.task_id, None)
                    fleet_verdicts.issue_intents(session, node_id, [intent])
                    # The intent is committed, so this decision dispatched whatever happens next; everything
                    # below is bookkeeping. `taken["dispatched"]` signals `_taken_slot`, whose `__exit__` runs
                    # first and would otherwise release the hold on any exception, handing a launching
                    # runner's tree back to the pool.
                    dispatched = True
                    jaah_issued.add(decision.task_id)
                    taken["dispatched"] = True
                    bound_slot = None
                    issued += 1
                    _clear_decline(node_id, repo.project, decision.task_id)
                    journal.record(
                        "dispatch", "ISSUED",
                        f"lease_slot onto {repo.project}#{decision.lease_target} at generation "
                        f"{taken['generation']}"
                        + (", resuming that slot's existing tree" if decision.resume else ""),
                        subject=f"{node_id}/{repo.project} task{decision.task_id}",
                    )
                    log.info(
                        "lease-drain: node %s — issued lease_slot for task %s onto %s#%s (gen %s%s)",
                        node_id, decision.task_id, repo.project, decision.lease_target,
                        taken["generation"],
                        ", RESUMING that slot's existing tree" if decision.resume else "",
                    )
            except Exception as exc:  # noqa: BLE001 — one row must not cost this node the rest of its pass
                # Rolled back first: a failed statement poisons the transaction, and the next decision would
                # raise `PendingRollbackError`. The rollback is itself guarded, since a raise here would cost
                # the pass and the ledger entry (as in `_taken_slot`).
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001 — a lost connection must not cost the ledger entry
                    log.exception(
                        "lease-drain: node %s — rolling back after a contained dispatch failure itself "
                        "raised; this node's remaining decisions will likely fail too", node_id,
                    )
                # ── GIVE THE LEASE BACK, because `bind_account` COMMITTED IT and no intent went out ──
                # A lease is an authorization: `routes/agent/task_secrets.py::get_account_api_key` serves an account's live
                # key to a node holding a slot leased to it. `bind_account(None)` clears it, fenced on
                # generation and state so a slot another task took is untouched. The hold itself is
                # `_taken_slot`'s decision (S2 keeps a task's own tree).
                if bound_slot is not None and not dispatched:
                    try:
                        slot_state.bind_account(
                            session,
                            worktree_id=bound_slot["worktree_id"],
                            generation=bound_slot["generation"],
                            account_id=None,
                        )
                    except Exception:  # noqa: BLE001 — same reason as the rollback above
                        log.exception(
                            "lease-drain: node %s — could not give back the account lease on %s#%s after "
                            "a contained dispatch failure; that slot may still authorize an api-key "
                            "fetch for the account it was leased to until it is released",
                            node_id, repo.project, decision.lease_target,
                        )
                # ── AND IF THE INTENT ALREADY WENT OUT, THIS IS NOT A DECLINE ───────────────────────
                # A raise in the bookkeeping after `issue_intents` must not journal a decline for a dispatched
                # unit. The traceback still fires.
                if dispatched:
                    log.exception(
                        "lease-drain: node %s — task %s was DISPATCHED onto %s#%s and the bookkeeping "
                        "after the intent raised; the runner is launching and this is NOT a decline",
                        node_id, decision.task_id, repo.project, decision.lease_target,
                    )
                    if contained_out is not None:
                        contained_out.append(type(exc).__name__)
                    continue
                # Additive: `_taken_slot` already handled the hold and `_compose_or_undo` journaled which
                # dispatch died; this records that the failure was contained. The type only, never the
                # message: composer messages carry prompt and project bytes that must not reach the cleartext
                # `cron_runs.detail`. The message goes to `log.exception`.
                _report_decline(
                    journal, node_id, repo.project, decision.task_id,
                    f"the dispatch attempt raised {type(exc).__name__} and THIS UNIT ALONE was declined "
                    f"— every remaining dispatch decision of this node's pass was still made, and the "
                    f"next pass retries this one",
                )
                # Unconditional, unlike `_report_decline`'s log: the traceback is the only copy of what
                # raised.
                log.exception(
                    "lease-drain: node %s — the dispatch attempt for task %s onto %s#%s raised %s; "
                    "DECLINING that one unit and continuing this node's remaining dispatch decisions",
                    node_id, decision.task_id, repo.project, decision.lease_target,
                    type(exc).__name__,
                )
                # Counted on both arms, and only here; see `contained_out`.
                if contained_out is not None:
                    contained_out.append(type(exc).__name__)
                # Spelled though it is the loop's last statement: the containment is the control flow, and
                # code appended below must not run for a failed decision.
                continue
    # A Jaah-account session opened for a decision that did not go out is ended, so it can never be served.
    if edition_features.enabled("jaah_ai_credit"):
        from app.jaah_routing import sessions as jaah_sessions

        undispatched = [
            UUID(decision.bedrock["session_id"])
            for repo in plan.repos for decision in repo.dispatches
            if decision.bedrock and decision.task_id not in jaah_issued
        ]
        if undispatched:
            try:
                for jaah_session_id in undispatched:
                    jaah_sessions.end(session, jaah_session_id, jaah_sessions.END_DECLINED)
                session.commit()
            except Exception:  # noqa: BLE001 — the reap ends them within ten minutes
                session.rollback()
                log.exception("lease-drain: node %s — could not end %s undispatched Jaah session(s)",
                              node_id, len(undispatched))
    try:
        dispatch_wait.record(session, waits)
    except Exception:  # noqa: BLE001 — a console hint must never cost the pass
        session.rollback()
        log.exception("lease-drain: node %s — could not stamp %s dispatch wait reason(s)", node_id, len(waits))
    return issued


def _dispatch_agent(session, node_id: str, journal: DecisionLog, repo, project_row: Project, decision,
                    grid) -> bool:
    """Lease an SDK agent's slot and issue its `launch` intent (agent architecture §4.3): the take is the model
    path's (`_taken_slot`), but there is no account to resolve or bind and no prompt to compose. The agent is
    re-read and re-resolved under the take, so a pick, disable or refresh since the plan is honoured rather
    than launched stale; a task that no longer resolves to this agent is declined and re-planned next pass.
    The task's run stamps (`agent_resolution.stamp_run`) ride the same transaction, since the node fetches the
    bundle by them before claiming. The slot still gets its port band and resolved environment. Returns whether
    the intent went out; a raise leaves the hold to `_taken_slot`."""
    project_id = project_row.id
    with _taken_slot(session, node_id, project_id, repo.project, decision) as taken:
        if taken is None:
            _report_decline(journal, node_id, repo.project, decision.task_id,
                            f"slot {decision.lease_target} was already held when the lease was taken")
            return False
        if taken.get("declined"):
            _report_decline(journal, node_id, repo.project, decision.task_id, taken["declined"])
            return False
        # Fresh, not the plan's identity-map copies (`expire_on_commit=False`): a pick, disable or refresh since
        # the plan must be seen here.
        session.expire_all()
        task = session.scalars(select(Task).where(Task.id == decision.task_id)).first()
        choice = agent_resolution.resolve_agent(session, task, project_row) if task is not None else None
        if not isinstance(choice, agent_resolution.Sdk) or str(choice.agent.id) != decision.agent_id:
            # Nothing goes out; `_taken_slot` gives the take back and the next pass re-plans.
            why = f"task {decision.task_id}'s agent changed since the plan; re-planned next pass"
            taken["give_back"] = why
            _report_decline(journal, node_id, repo.project, decision.task_id, why)
            return False
        slot_has_repo = session.scalar(
            select(Worktree.has_repo).where(Worktree.id == taken["worktree_id"])
        ) is not False
        # A registry agent's bound key account, read ONCE: the launch names it and the slot is leased it.
        account = acp_registry.bound_account(session, choice.agent) if acp_registry.is_registry(choice.agent) else None
        launch = agent_resolution.launch_for(session, task, choice.agent,
                                             key_account=account.slug if account is not None else "")
        intent = fleet_verdicts.build_lease_intent(
            project_id, repo.project, decision.lease_target,
            task_id=decision.task_id,
            base=repo.base if slot_has_repo else None,
            holder="fleet",
            generation=taken["generation"],
            dispatch_id=taken["dispatch_id"],
            git_author=_git_author_for(session, decision.task_id),
            ports=_port_band_for(project_row, decision.lease_target, grid) if slot_has_repo else {},
            has_repo=None if slot_has_repo and project_row.has_repo else slot_has_repo,
            env_vars=_resolved_env_for(session, project_id, decision),
            resume=decision.resume,
            launch=launch,
        )
        try:
            if account is not None:
                # The slot is leased the account, so the node's key route serves its key to this run's ACP host
                # (`routes/agent/task_secrets.py::get_account_api_key`). `_unrunnable` checked it is usable. `bind_account`
                # commits, so a raise below clears it: no live key lease outlives a dispatch that never went out.
                slot_state.bind_account(session, worktree_id=taken["worktree_id"], generation=taken["generation"],
                                        account_id=account.id)
            agent_resolution.stamp_run(session, decision.task_id, choice.agent)
            fleet_verdicts.issue_intents(session, node_id, [intent])
        except Exception:
            if account is not None:
                session.rollback()
                slot_state.bind_account(session, worktree_id=taken["worktree_id"], generation=taken["generation"],
                                        account_id=None)
            raise
        taken["dispatched"] = True
    _clear_decline(node_id, repo.project, decision.task_id)
    journal.record(
        "dispatch", "ISSUED",
        f"agent {choice.agent.display_name} {choice.agent.version} onto {repo.project}#{decision.lease_target} "
        f"at generation {taken['generation']}",
        subject=f"{node_id}/{repo.project} task{decision.task_id}",
    )
    log.info("lease-drain: node %s — issued agent %s (%s) for task %s onto %s#%s (gen %s)", node_id,
             choice.agent.id, choice.agent.resolved_commit, decision.task_id, repo.project, decision.lease_target,
             taken["generation"])
    return True


def _undo_take(
    session, project: str, decision, taken: dict[str, Any], why: str
) -> None:
    """Give back the hold `_take_for_dispatch` just got, when the dispatch it was got for did not happen.

    A slot this task already owned is left alone (S2: only `done`, `system_failed`, deleted or reset end
    ownership); the next pass retries in the same slot. A fresh take goes straight back to the pool.
    `worktree_id` comes off `taken`, never re-resolved.
    """
    # Keyed on where the hold came from, not `decision.resume`: `select_slot`'s rule 2 reads a snapshot that
    # can go stale, so a `resume` decision can meet a fresh take, which must not be left standing.
    if taken.get("from_own_slot"):
        log.warning(
            "lease-drain: %s#%s — %s; the slot stays owned by task %s (its own slot is never released "
            "over a compose failure — S2) and the next pass retries the dispatch",
            project, decision.lease_target, why, decision.task_id,
        )
        return
    slot_state.release(
        session, worktree_id=taken["worktree_id"],
        generation=taken["generation"], reason=why,
    )


def _resumes_own_slot(session, node_id: str, project_id: Optional[int], decision) -> bool:
    """Whether DECISION relaunches its task into the slot that task holds now. Read off the hold, not
    `decision.resume`: the roster snapshot behind that flag can be stale, and a stale resume falls through to a
    fresh take (`_take_for_dispatch`), which is a new runner on the node."""
    if not decision.resume or decision.create_dynamic or project_id is None:
        return False
    worktree_id = slot_state.resolve_slot(session, node_id=node_id, project_id=project_id,
                                          number=decision.lease_target)
    hold = slot_state.read(session, worktree_id=worktree_id) if worktree_id is not None else None
    return hold is not None and hold.state == slot_state.TASK and hold.task_id == decision.task_id


def _take_for_dispatch(
    session, node_id: str, project_id: int, project: str, decision
) -> Optional[dict[str, Any]]:
    """Get the hold this dispatch needs, by whichever of the two routes into a slot applies. Returns the
    `{"generation", "dispatch_id", "worktree_id"}` the intent is fenced on, or None when the slot is not
    ours to use.

    A fresh slot: `slot_state.lease` (only while `free`). The task's own slot: `slot_state.redispatch`,
    re-armed for the same task with a bumped generation, never passing through `free` (rule 6). Fenced on
    the generation just read. The own-slot route skips `_slot_occupied`, since the previous runner is what
    the bump displaces. The wire triple becomes `worktree_id` here once; `None` also covers "not enrolled".

    An unresolvable org declines rather than raises (`slot_state.lease` refuses `None`, which would match
    every unstamped slot); `_fail_unplaceable_tasks` then fails the task with `org_mismatch`. With
    `tasks.org_id` NOT NULL this is reached only for a task deleted mid-pass.
    """
    org_id = placement_grants.placement_org(session, decision.task_id)
    if org_id is None:
        log.warning(
            "lease-drain: node %s — task %s resolves to no org (no org_id of its own, none on any "
            "ancestor, and no launcher carrying one), so there is no tenant to place it in; declining "
            "this pass. The feasibility sweep refuses it terminally with `org_mismatch`",
            node_id, decision.task_id,
        )
        return None
    if decision.create_dynamic:
        return _take_dynamic(session, node_id, project_id, org_id, decision)
    worktree_id = slot_state.resolve_slot(session, node_id=node_id, project_id=project_id,
                                          number=decision.lease_target)
    if worktree_id is None:
        log.warning(
            "lease-drain: node %s — %s#%s resolves to no enrolled slot; refusing to dispatch task %s",
            node_id, project, decision.lease_target, decision.task_id,
        )
        return None
    if not decision.resume:
        taken = slot_state.lease(
            session, worktree_id=worktree_id, org_id=org_id, task_id=decision.task_id,
            reason=fleet_verdicts.dispatch_reason(decision.task_id),
        )
        if taken is not None:
            taken["worktree_id"] = worktree_id
        return taken
    hold = slot_state.read(session, worktree_id=worktree_id)
    if hold is None or hold.state != slot_state.TASK or hold.task_id != decision.task_id:
        # Not this task's own slot after all (a stale roster snapshot); falls through to the fresh-take path.
        taken = slot_state.lease(
            session, worktree_id=worktree_id, org_id=org_id, task_id=decision.task_id,
            reason=fleet_verdicts.dispatch_reason(decision.task_id, own_tree=True),
        )
        if taken is not None:
            taken["worktree_id"] = worktree_id
        return taken
    # ── THE UN-ANNOUNCED-LAUNCH CHARGE RIDES THE RE-ARM'S OWN TRANSACTION ─────────────────────────────
    # Read the launch evidence before `redispatch` blanks it, charge uncommitted, and let the
    # generation-fenced re-arm commit it: only one of two racing passes matches, and the other rolls back, so
    # one launch is charged exactly once. The terminal arm differs: `end_attempt` kills the runner and
    # releases the slot, committing itself, and the caller gets the decline.
    if not session.scalar(select(fleet_verdicts.predecessor_gone()).where(Worktree.id == worktree_id)):
        # A stop landed between the plan and this take: wait, uncharged, for the node to report it gone.
        log.info(
            "lease-drain: task %s's own slot %s#%s still has a predecessor the node has not reported gone; "
            "the relaunch waits", decision.task_id, project, decision.lease_target,
        )
        return None
    charge = None
    node_uuid = node_id_or_none(session, node_id)
    node_key = str(node_uuid) if node_uuid is not None else None
    if node_key is not None and launch_failures.node_fault_on(session, decision.task_id, node_key):
        # The node cannot run it at all (a merge-lane fence that will not start): no tree here is worth keeping.
        return _move_off_node(session, hold, worktree_id, node_id, project, decision)
    if fleet_decide.launch_never_claimed(
        acked_at=hold.acked_at, dispatched_at=hold.dispatched_at,
        github_synced_at=session.scalar(select(Task.github_synced_at).where(Task.id == decision.task_id)),
    ):
        from app.routes.tasks.runner_settle import charge_unannounced_launch  # noqa: PLC0415 — the module's own idiom

        if node_key is not None and launch_failures.skipping_on(session, decision.task_id, node_key) and (
                launch_failures.charged_since(session, decision.task_id, node_key, hold.acked_at)):
            # Already charged for it in an earlier pass whose release did not land: move without a new charge.
            return _move_off_node(session, hold, worktree_id, node_id, project, decision)
        acked = hold.acked_at.astimezone(timezone.utc)
        charge = charge_unannounced_launch(
            decision.task_id, session, commit=False, node=node_key,
            account=str(hold.leased_account_id) if hold.leased_account_id is not None else None,
            detail=(
                f"launch acked at {acked:%H:%M:%S}Z was never announced (runner never claimed active); "
                "dispatch attempt charged"
            ),
        )
        if charge == task_lifecycle.SYSTEM_FAILED:
            return {
                "declined": (
                    "spent its last dispatch attempt on launches that were acked but never announced "
                    "themselves → system_failed"
                ),
            }
        if charge is None:
            # No longer `ready` (claimed or moved since the plan): leave the row alone.
            session.rollback()
            log.info(
                "lease-drain: task %s moved out of `ready` between the plan and the take; its acked "
                "launch on %s#%s is not charged and the resume is skipped this pass",
                decision.task_id, project, decision.lease_target,
            )
            return None
        session.flush()
        if node_key is not None and launch_failures.skipping_on(session, decision.task_id, node_key):
            return _move_off_node(session, hold, worktree_id, node_id, project, decision)
    elif node_key is not None and launch_failures.skipping_on(session, decision.task_id, node_key) and (
            launch_failures.charged_since(session, decision.task_id, node_key, hold.acked_at)):
        # The attempt that just ended was this launch, never sighted running (`launch_failed`, charged by the
        # reclaim), and the node has now failed the task enough: move it rather than relaunch here.
        return _move_off_node(session, hold, worktree_id, node_id, project, decision)
    re_armed = slot_state.redispatch(
        session, worktree_id=worktree_id, generation=hold.generation,
        reason=f"resuming task {decision.task_id} in place — its own slot holds the previous attempt's tree",
    )
    if re_armed is None and charge is not None:
        # Fence lost to another pass; `redispatch` commits only on a match, so this rollback prevents a double
        # charge.
        session.rollback()
    elif re_armed is not None and charge is not None:
        log.warning(
            "lease-drain: task %s's launch on %s#%s (acked %sZ) never announced itself; charged one "
            "dispatch attempt and relaunching into the same slot",
            decision.task_id, project, decision.lease_target, f"{acked:%H:%M:%S}",
        )
    if re_armed is not None:
        re_armed["worktree_id"] = worktree_id
        # This hold was already this task's own, so a failed dispatch leaves it standing (`_undo_take`).
        re_armed["from_own_slot"] = True
        log.warning(
            "lease-drain: resuming task %s in its own slot %s#%s at generation %s — the tree is untouched "
            "and the slot never passed through `free`",
            decision.task_id, project, decision.lease_target, re_armed["generation"],
        )
    return re_armed


def _stamp_without_nodes(session) -> None:
    """With no live node in the fleet the pass dispatches nothing, but the allocation still names each waiting
    task's cause (`routes/fleet.py::_stamp_unplaced`), so a fleet that is down does not leave a bare `ready`."""
    from app.routes.fleet import fleet_allocation

    waits: dict[int, str] = {}
    try:
        fleet_allocation(session, wait_reasons_out=waits)
        dispatch_wait.record(session, waits)
    except Exception:  # noqa: BLE001 — a console hint must never cost the pass
        session.rollback()
        log.exception("lease-drain: could not stamp %d wait reason(s) with no live node", len(waits))


def _stamp_failed_launch_waits(session, allocation: dict[int, str], failed_on: dict[int, frozenset[str]],
                               allowed_by_task: dict[int, Optional[frozenset[str]]]) -> None:
    """Say why a task its launches kept failing on is not placed — only when no node is left for it at all
    (`allowed_by_task`, this pass's exclusion already applied), so a task merely waiting for room elsewhere
    keeps whatever its node's pass says. `failed_on` is the pass's one read (`launch_failures.excluded_node_ids`),
    keyed by node uuid; the wait text renders the nodes' names, read once for every task it names."""
    pending: dict[int, frozenset[str]] = {}
    for task_id, node_ids in failed_on.items():
        if task_id in allocation or allowed_by_task.get(task_id) != frozenset():
            continue
        if slot_state.task_hold(session, task_id) is not None:
            continue
        pending[task_id] = node_ids
    named = {
        str(node_id): name
        for node_id, name in names_by_id(
            session, {UUID(node_id) for node_ids in pending.values() for node_id in node_ids}
        ).items()
    }
    # A node an operator's "Start fresh elsewhere" left is no failed launch: it gets its own sentence.
    left = launch_failures.left_node_ids(session, pending) if pending else {}
    waits = {
        task_id: (dispatch_wait.node_left if node_ids <= left.get(task_id, frozenset())
                  else dispatch_wait.node_failed_launches)(
            ", ".join(sorted(named.get(node_id, node_id) for node_id in node_ids)) or "its node"
        )
        for task_id, node_ids in pending.items()
    }
    dispatch_wait.record(session, waits)


def _move_off_node(session, hold: Worktree, worktree_id: UUID, node_id: str, project: str,
                   decision) -> Optional[dict[str, Any]]:
    """Release a `ready` task's own slot on a node its launches keep failing on, so placement can choose
    another (`app/launch_failures.py`). Returns the take's decline, or None when the fence was lost (nothing
    written). Commits, with any charge just written.

    The one release a failed attempt causes (operator decision). The node's `release_slot` clears
    the tree and keeps a branch holding unpushed commits. Uncommitted work in the
    tree is discarded — none from the launches that never announced themselves, but any an EARLIER attempt
    of this task left uncommitted is lost too; the operator accepted that over relaunching onto a broken
    node."""
    why = (
        f"task {decision.task_id} cannot run on {node_id} (launches never announced, or the node's own fault); "
        "moving it to another node"
    )
    # The charge just written rides the release: a lost fence (another pass got here first) rolls it back,
    # so one launch is charged once, exactly as the `redispatch` arm guarantees.
    freed = _release(session, {
        "worktree_id": worktree_id, "generation": hold.generation, "node_id": node_id,
        "project_id": hold.project_id, "project": project, "slot": hold.number,
        "task_id": decision.task_id, "session_id": hold.launch_session_id, "pid": hold.pid,
    }, why, rides_pending=True)
    if not freed:
        log.info("lease-drain: task %s — another pass already moved it off %s#%s", decision.task_id, project,
                 decision.lease_target)
        return None
    log.warning("lease-drain: %s (%s#%s released)", why, project, decision.lease_target)
    return {"declined": why}


def _take_dynamic(session, node_id: str, project_id: int, org_id: UUID, decision) -> dict[str, Any]:
    """Declare the dynamic worktree the plan chose at `decision.lease_target` and lease it, in ONE transaction
    (`slot_allocation.add_dynamic` flushes, `slot_state.lease` commits both). Returns the taken dict, or
    `{"declined": why}` with nothing written: a concurrent insert at the index loses the unique key and the
    next pass re-plans. The node's lease rung makes the tree; its ports are the grid's at that index."""
    index = decision.lease_target
    node = session.get(Node, node_id_or_none(session, node_id))
    project = session.get(Project, project_id)
    if node is None or project is None:
        return {"declined": "no dynamic worktree was made: the node or project row is gone"}
    if node.org_id != org_id:
        # A slot's org is its node's (revision 0181), so the lease could never take it.
        return {"declined": "no dynamic worktree was made: this node is not in the task's organization"}
    try:
        row = slot_allocation.add_dynamic(session, node, project, index)
    except ValueError as exc:
        session.rollback()
        return {"declined": f"no dynamic worktree was made: {exc}"}
    except IntegrityError:
        session.rollback()
        return {"declined": f"no dynamic worktree was made: index {index} taken concurrently"}
    worktree_id = row.id
    taken = slot_state.lease(
        session, worktree_id=worktree_id, org_id=org_id, task_id=decision.task_id,
        reason=fleet_verdicts.dispatch_reason(decision.task_id),
    )
    if taken is None:
        # Unreachable for a row this transaction declared; it is `free` and dynamic, so the next pass's sweep
        # retires it rather than leaving it standing.
        return {"declined": f"no dynamic worktree was leased: the new row at index {index} could not be taken"}
    taken["worktree_id"] = worktree_id
    log.info("lease-drain: node %s — declared dynamic worktree %s#%s for task %s",
             node_id, project.name, index, decision.task_id)
    return taken


class ResolvedAccount(NamedTuple):
    """Which account this dispatch launches on, and how the node is to authenticate as it.

    Named fields rather than one overloaded one: an OAuth account authenticates from a credential
    directory, an API-key account from a secret the node fetches by name (`app/account_auth.py`). Folding
    the second into `config_dir` fails silently ("Not logged in"). Exactly one of `config_dir` /
    `api_key_slug` is non-empty; `securestorage_dir` modifies the OAuth arm only.
    """

    #: The account's wire name (a `config_dir`, or `apikey:<slug>`), for the journal and decline messages.
    ref: str
    #: The runner's config-dir export (`agent_providers.CONFIG_DIR_ENV`). `""` for an API-key account; the node
    #: omits the export when empty, which is fatal for OAuth (see the fail-closed refusal below).
    config_dir: str
    #: The runner's secure-storage-dir export (`agent_providers.SECURESTORAGE_DIR_ENV`) for a SHARED OAuth
    #: account; `None` otherwise, which is the ordinary case and a DECISION the intent spells out rather than
    #: omits.
    securestorage_dir: Optional[str]
    #: The account slug whose API key the node is to fetch, `""` for an OAuth account. A SLUG, and never
    #: key material — the node asks its own credential socket for it by name at launch.
    api_key_slug: str
    #: Which vendor's CLI this dispatch launches, from this account's `accounts.provider`, carried to the
    #: provider-aware composer. Appended last so positional construction cannot be re-pointed.
    provider: str


def _is_jaah_ref(account: Optional[str]) -> bool:
    """Whether `account` names the Jaah account (`app/jaah_routing/`); never, in an edition without it."""
    if not edition_features.enabled("jaah_ai_credit"):
        return False
    from app.jaah_routing import routing as jaah_routing

    return jaah_routing.is_ref(account)


def _resolve_account(
    journal: DecisionLog, node_id: str, repo, decision, shared_slugs: frozenset[str], mounts_share: bool,
    share_root: str,
    account_providers: dict[str, str],
) -> Optional[ResolvedAccount]:
    """Which credential account this dispatch launches on and how the node is to authenticate as it (a
    `ResolvedAccount`), or None when no account can be honestly dispatched onto, already declined and
    logged.

    An API-key account takes a short path first: `config_dir=""`, and only the slug travels (the node
    fetches the key over its own socket). The task's own granted account wins over the unconfined
    `repo.account`.

    No account is not a dispatch: an empty `agent_providers.CONFIG_DIR_ENV` opens an unauthenticated default dir and the
    runner answers "Not logged in"; declined before the take, so nothing is held or spent. WHERE the token
    is, is the node's to resolve (its config dir, else `.shared/<slug>`); `securestorage_dir` is served only
    for an older node, when the account is `shared` and the node reports a mounted token share
    (`_shared_store_facts`), and None otherwise — never `""` (an empty `agent_providers.SECURESTORAGE_DIR_ENV`
    export means the session user's default config home). The slug comes from
    `account_auth.account_slug_from_ref`; the shared path from `app/shared_storage.py`, resolved here to keep
    it off `_best_account`'s wire shape.
    """
    account = decision.account or repo.account
    if _is_jaah_ref(account):
        # The Jaah account (`app/jaah_routing/`): no roster row, no credential dir and no key — the node runs
        # `agent_providers.JAAH_ACCOUNT_PROVIDER` on the plan's `bedrock` block and asks K3 for that session's
        # credentials.
        if not decision.bedrock:
            _report_decline(journal, node_id, repo.project, decision.task_id,
                            "a Jaah-account dispatch carried no Bedrock block, so the node could not "
                            "authenticate (fail closed)")
            return None
        return ResolvedAccount(
            ref=account, config_dir="", securestorage_dir=None, api_key_slug="",
            provider=agent_providers.DEFAULT_PROVIDER,
        )
    if not account:
        _report_decline(journal, node_id, repo.project, decision.task_id,
                        "the CP resolved no credential account for this node — a runner launched "
                        f"with no {agent_providers.CONFIG_DIR_ENV} opens the default, unauthenticated credential "
                        "dir and exits at once (fail closed)")
        return None
    # The slug behind either spelling via `account_auth.account_slug_from_ref`; never a second parse.
    account_slug = account_auth.account_slug_from_ref(account)
    # Which vendor's CLI this dispatch launches, off this account's row, resolved where the account is
    # decided.
    row_provider = account_providers.get(account_slug)
    provider = row_provider or agent_providers.DEFAULT_PROVIDER
    # Silent only for an `apikey:` ref whose slug does not parse, which is declined below. An unparseable
    # OAuth ref has no such decline and dispatches, so it must be journaled here.
    declined_below_for_its_slug = not account_slug and account_auth.is_api_key_account_ref(account)
    if not row_provider and not declined_below_for_its_slug:
        # An unanswered slug is not a refusal: such a reference dispatches as the default provider today, so defaulting
        # preserves that. Three different facts are reported apart: `None` (no row), `""` (blank provider,
        # which the column allows), and no slug at all. Journaled, not only logged, as an anomalous binding.
        detail = (
            "this account's reference composes no slug, so no roster row could be looked up"
            if not account_slug else
            "the account roster has no row under this slug"
            if row_provider is None else "this account's row carries a blank provider"
        )
        journal.record(
            "dispatch", "PROVIDER DEFAULTED",
            f"{detail} — dispatching as {provider}; the composed charter asserts an ask channel this "
            f"account's CLI was never checked for",
            subject=f"{node_id}/{repo.project} task{decision.task_id}",
        )
        log.warning(
            "lease-drain: account ref %r (slug %r) — %s; dispatching task %s as %s",
            # No cap here: this arm's `provider` is always `DEFAULT_PROVIDER`. The cap belongs to the `elif`
            # below.
            account, account_slug, detail, decision.task_id, provider,
        )
    elif row_provider and agent_providers.by_value(row_provider) is None:
        # An unknown provider is declined here, one unit at a time, rather than letting `compose` raise and
        # cost the node its whole pass every 10s during a rolling deploy.
        _report_decline(
            journal, node_id, repo.project, decision.task_id,
            # Capped at `[:64]` so the operator-facing tail of the reason survives `DecisionLog.record`'s 240
            # clip; `accounts.provider` is unbounded `Text`. The cap is about the budget, not safety.
            f"account {account_slug!r} declares provider {row_provider[:64]!r}, which this control "
            f"plane does not know — it cannot compose a charter for a vendor it cannot describe (a "
            f"newer build wrote this row; this node dispatches it once it is running that build)",
        )
        return None
    if account_auth.is_api_key_account_ref(account):
        # The API-key arm, ahead of every shared-store question. An unparseable slug composes nothing: the
        # node would otherwise ask for a name nobody minted and launch unauthenticated.
        if not account_slug:
            _report_decline(journal, node_id, repo.project, decision.task_id,
                            "this task's account is an API-key account whose reference composes no "
                            "slug, so the CP cannot tell the node which credential to fetch — a "
                            "runner launched without its API key is not logged in (fail closed)")
            return None
        return ResolvedAccount(
            ref=account, config_dir="", securestorage_dir=None, api_key_slug=account_slug,
            provider=provider,
        )
    securestorage_dir = (
        shared_storage.resolve_shared_auth_dir(account_slug, share_root) or None
        if account_slug in shared_slugs and mounts_share else None
    )
    return ResolvedAccount(
        ref=account, config_dir=account, securestorage_dir=securestorage_dir, api_key_slug="",
        provider=provider,
    )


@contextmanager
def _taken_slot(session, node_id: str, project_id: int, project: str, decision):
    """Take the hold this dispatch needs (`_take_for_dispatch`) and guarantee it is given back unless the
    block inside ends on a genuine dispatch. Yields the taken dict, or None when the slot was not ours to
    use (the caller should `continue`).

    One seam for every failure to dispatch onto a held slot: releases on any exception inside the block
    and on an explicit `taken["give_back"] = why`. Except once `taken["dispatched"] = True` (set when
    `issue_intents` commits): the node will launch, so releasing would hand a live tree to the pool. The
    rollback still runs and the exception still propagates.
    """
    taken = _take_for_dispatch(session, node_id, project_id, project, decision)
    if taken is None:
        yield None
        return
    try:
        yield taken
    except Exception:
        # Covers compose and intent issue. Rolled back before the give-back: a failed statement leaves the
        # transaction aborted, so `_undo_take`'s UPDATE would raise, leave the slot held and mask the
        # original error. The rollback discards nothing legitimate (every write inside commits itself; the
        # session is this node's own). The give-back is wrapped so its own failure is logged and the
        # original re-raised. Skipped entirely once `taken["dispatched"]` is set.
        try:
            session.rollback()
            if not taken.get("dispatched"):
                _undo_take(
                    session, project, decision, taken,
                    f"dispatch attempt raised while dispatching task {decision.task_id}",
                )
        except Exception:
            log.exception(
                "lease-drain: %s#%s — giving the just-taken hold back after the failed dispatch of task "
                "%s ITSELF raised; that slot may be left held for the next pass to find, and the "
                "ORIGINAL failure is what propagates",
                project, decision.lease_target, decision.task_id,
            )
        raise
    else:
        why = taken.get("give_back")
        if why is not None:
            _undo_take(session, project, decision, taken, why)


def _compose_or_undo(
    journal: DecisionLog, session, node_id: str, project_id: int, repo, decision, taken: dict[str, Any],
    *, provider: str, slot_has_repo: bool = True,
) -> Optional[dict[str, Any]]:
    """Compose this dispatch's prompt bundle and decide whether it actually dispatches. Returns the
    verdict dict on a `dispatch` action; on `skip`/`error` marks `taken` for `_taken_slot` to give back
    and returns None.

    The bundle is composed here, once; the node never fetches anything. Never partial: a `skip`/`error`
    means no dispatch this pass and the slot is given back, loudly logged. A compose raise is caught
    narrowly to journal what failed, then re-raised for `_taken_slot` to release.

    `slot_has_repo` false (a folder slot) composes with no repository values at all, whatever the
    plan carries for the project's other slots.
    """
    # `""`, never None, which the composer's string handling would crash on; what a folder prompt SAYS is the
    # composer's.
    def _git(value: Optional[str]) -> str:
        return (value or "") if slot_has_repo else ""

    try:
        # Resolved once per dispatch attempt and stamped beside the model below (R-SW-4, the A/B arm).
        memory_arm = _memory_arm(session, decision.task_id)
        verdict = compose.compose(
            session,
            repo.project,
            _git(repo.repo_path),
            decision.task_id,
            # No per-dispatch override: `_resolve_model` falls through to the task's own pin (how a Runner
            # cron's model reaches its runner).
            model_override="",
            # The project values come off the plan, not a second read that could differ.
            integration_base=_git(repo.base),
            branch_prefix=_git(repo.branch_prefix),
            gate_quick_cmd=_git(repo.gate_quick_cmd),
            gate_full_cmd=_git(repo.gate_full_cmd),
            manual=False,
            # The resolved account's own provider, passed as an argument, never looked up inside the composer.
            provider=provider,
            # The taken slot's kind: a folder composes the folder prompt (`_COND_FLAGS`'s `repo`).
            has_repo=slot_has_repo,
            # The dispatching node: a provider with no moving model alias resolves its tier from THIS
            # node's reported catalog (`dispatch_compose.compose._node_model_catalog`).
            node_id=node_id,
            # The resume terms ride the same compose: the runner must be told the tree holds a previous
            # attempt's uncommitted work.
            resume_worktree=(
                _worktree_path(session, node_id, project_id, decision.lease_target)
                if decision.resume else ""
            ),
            memory_arm=memory_arm[0],
            # Whether the merge lane merges this project (`merge_lane.lane_state`): its runner is told to report
            # the PR with `pull_request_enqueue` instead of running the full gate and arming auto-merge.
            merge_lane=merge_lane_state.lane_state(
                session, session.scalars(select(Project).where(Project.id == project_id)).one()
            ).active,
            # A slot runner is told the NodeLinks its node reports mounted.
            advertise_nodelinks=True,
            # Every repository the taken slot holds; `()` for a single-repository project or a folder.
            repos=(task_prompt.slot_repos_for_prompt(session, project_id, node_id, decision.lease_target)
                   if slot_has_repo else ()),
        )
    except Exception as exc:
        # Journaled before the undo, so the reason survives if giving the lease back raises next; it names
        # which dispatch died.
        journal.record(
            "dispatch", "NOT DISPATCHED",
            f"compose raised {type(exc).__name__} and the just-taken lease was given back",
            subject=f"{node_id}/{repo.project} task{decision.task_id}",
        )
        raise
    if verdict["action"] != "dispatch":
        taken["give_back"] = (
            f"compose {verdict['action']} for task {decision.task_id}: {verdict['reason']}"
        )
        journal.record(
            "dispatch", "NOT DISPATCHED",
            f"compose {verdict['action']}: {verdict['reason']}; the lease was released and the next "
            f"pass retries",
            subject=f"{node_id}/{repo.project} task{decision.task_id}",
        )
        log.warning(
            "lease-drain: node %s — compose %s for task %s onto %s#%s (%s) — lease released, "
            "NOT dispatching",
            node_id, verdict["action"], decision.task_id, repo.project, decision.lease_target,
            verdict["reason"],
        )
        return None
    _stamp_dispatched_model(session, decision.task_id, verdict, memory_arm=memory_arm)
    return verdict


def _memory_arm(session, task_id: object) -> tuple[bool, Optional[str]]:
    """`(on, source)` for this dispatch attempt (`project_memory.effective_memory`); `(False, None)` for a row
    that is gone, which compose then skips."""
    if not edition_features.enabled("project_memory"):
        # The community edition has no project memory: every dispatch is OFF.
        return False, None
    from app import project_memory

    task = session.scalars(select(Task).where(Task.id == int(task_id))).one_or_none()
    if task is None:
        return False, None
    return project_memory.effective_memory(session, task)


def _stamp_dispatched_model(
    session, task_id: object, verdict: dict[str, Any], *, memory_arm: tuple[bool, Optional[str]],
) -> None:
    """Record on the Task what this dispatch launches — the tier and the concrete `--model` — so the
    console shows "High → gpt-5.6-sol" (`models/tasks.py::Task.dispatched_tier`), and the project-memory arm it
    composed under. An ON prompt's units are recorded as `injected` events in the same commit. Commits itself,
    like every write in the decision's guarded block; empty is stored as NULL (no tier / the CLI's own
    default)."""
    session.execute(
        update(Task)
        .where(Task.id == int(task_id))
        .values(
            dispatched_tier=verdict.get("tier") or None,
            dispatched_model=verdict.get("model") or None,
            memory_arm=memory_arm[0],
            memory_arm_source=memory_arm[1],
            **actors.task_actor_values(session),
        )
    )
    _record_prompt_memory(session, task_id, verdict)
    session.commit()


def _record_prompt_memory(session, task_id: object, verdict: dict[str, Any]) -> None:
    """The prompt's units as `injected`/`prompt` events; nothing for an OFF verdict. Never commits."""
    memory = verdict.get("memory")
    if memory and memory["unit_ids"]:
        if not edition_features.enabled("project_memory"):
            return
        from app import project_memory

        project_memory.record_events(
            session, task_id=int(task_id),
            events=[(unit_id, "injected", "prompt") for unit_id in memory["unit_ids"]],
        )


def guard_stanza(session, project_id: Optional[int]) -> Optional[dict[str, Any]]:
    """The Architectural Guard stanza a launch or console open carries (`guard_runtime.lease_stanza`), or None in an
    edition without compliance, which has no guard — and then the payload is exactly what it was."""
    if not edition_features.enabled("compliance"):
        return None
    from app.governance import guard_runtime

    return guard_runtime.lease_stanza(session, project_id)


def lease_memory_of(verdict: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The lease payload's `memory` key for this verdict (§4.4), or None when its arm is OFF."""
    memory = verdict.get("memory")
    return memory["lease"] if memory else None


def _worktree_path(session, node_id: str, project_id: int, slot: object) -> str:
    """The node-side filesystem path of one slot's worktree, as the node last reported it.

    Read, never derived: a wrong path in a resume prompt makes the runner start over on top of prior work.
    Empty for an unreported slot, which the composer treats as "say nothing about a worktree". Unreported
    means no node observation (`observed_at`), not no `path`: a dynamic slot carries its declared path before
    any tree exists, and a first claim that died before building it left the runner told it was resuming a
    tree the node then builds fresh (`jaahd/reconcile/enact_launch.py` resume onto an absent tree).
    """
    return session.scalar(
        select(Worktree.path).where(
            # `node_id` is the node name; an unresolvable one yields NULL, matching no row, i.e. "".
            Worktree.node_id == node_ref(node_id),
            Worktree.project_id == int(project_id),
            Worktree.number == int(slot),
            Worktree.observed_at.is_not(None),
        )
    ) or ""


def _fail_unplaceable_tasks(session, summary: dict[str, Any], journal: DecisionLog) -> None:
    """Land `system_failed` on every dispatchable task whose node or account set makes
    placement structurally impossible, with the reason delivered twice. Counts into `summary["unplaceable"]`.

    Fleet-wide and asked once in the serial prelude: inside the fan-out it would refuse on the first node
    that cannot run it. Candidates are `dispatchable_tasks`, so a task waiting on a prerequisite or a held
    epic is never refused for a placement reason it has not reached.
    """
    # Function-local, to avoid the route-layer import cycle.
    from app.routes.tasks.dispatch import dispatchable_tasks
    from app.routes.tasks.runner_settle import end_attempt

    # One per-pass placement memo: an epic's children share a project and a launcher.
    grant_memo: dict = {}
    projects = session.scalars(select(Project)).all()
    for project in projects:
        for task in dispatchable_tasks(project.id, session):
            refusal = placement_grants.unplaceable_reason(session, task, memo=grant_memo)
            if refusal is None:
                continue
            # The note first, so `end_attempt`'s commit lands both atomically (a halved state is
            # unrecoverable, since the next pass reads `ready` only). `extra` carries project and cause only:
            # `refusal.detail` names nodes and accounts, and this row is served at `tasks:view`.
            user_activity.record_task_note(
                session, task.id, "placement_refused", refusal.sentence,
                {"project": project.name, "cause": refusal.cause},
            )
            # `sources=(READY,)` fences a task that never went `active`; `final=True` ignores the retry
            # counter, since no retry fixes this.
            landed = end_attempt(
                task.id, session, sources=(task_lifecycle.READY,), final=True,
                reason=refusal.sentence,
            )
            if landed != task_lifecycle.SYSTEM_FAILED:
                # The row left `ready` before the guarded UPDATE, so drop the pending note rather than let it
                # ride the next task's commit.
                journal.record(
                    "placement", "NOT REFUSED",
                    "the task left ready between the feasibility read and the guarded update, so nothing "
                    "landed and its note was rolled back",
                    subject=f"{project.name} task{task.id}",
                )
                session.rollback()
                continue
            # Counted on the summary, not a local, so a later raise cannot report committed failures as zero.
            summary["unplaceable"] += 1
            journal.record(
                "placement", "REFUSED",
                f"structurally unplaceable ({refusal.cause}) -> system_failed: {refusal.sentence}",
                subject=f"{project.name} task{task.id}",
            )
            log.warning(
                "lease-drain: task %s (%s) is structurally unplaceable (%s) -> system_failed. "
                "Told the launcher: %s | operator detail: %s",
                task.id, project.name, refusal.cause, refusal.sentence, refusal.detail,
            )


def _fail_unrunnable_agents(session, summary: dict[str, Any], journal: DecisionLog) -> None:
    """Spend one attempt on every dispatchable task whose agent cannot run (DD19, `agent_resolution.Refused`):
    disabled, removed from the catalog, not enabled for its org, never fetched, an explicit pick that does not
    serve its persona, or a playbook stage row of an org whose plan has Playbooks off. Counts into
    `summary["agent_unrunnable"]`.

    Fleet-wide and asked once in the serial prelude, like `_fail_unplaceable_tasks`; no slot is leased, so
    nothing can wedge. Each charge is one attempt with `[agent_unrunnable] <sentence>` as its reason
    (`routes/tasks/runner_settle.py::charge_unrunnable_agent`), so the task returns to `ready` and lands `system_failed` at
    the attempt cap like any runner failure (AC-15)."""
    from app.routes.tasks.runner_settle import charge_unrunnable_agent
    from app.routes.tasks.dispatch import dispatchable_tasks

    projects = session.scalars(select(Project)).all()
    for project in projects:
        for task in dispatchable_tasks(project.id, session):
            if task.agent_id is None and project.default_agent_id is None and task.playbook_stage is None:
                continue
            choice = agent_resolution.resolve_agent(session, task, project)
            if not isinstance(choice, agent_resolution.Refused):
                continue
            reason = agent_resolution.refusal_reason(choice)
            landed = charge_unrunnable_agent(task.id, session, reason=reason)
            if landed is None:
                continue
            summary["agent_unrunnable"] += 1
            if landed == task_lifecycle.READY:
                # Shown on the waiting task until the next charge (`dispatch_wait`).
                dispatch_wait.record(session, {task.id: reason})
            journal.record(
                "agent", "UNRUNNABLE", f"{reason} -> {landed}", subject=f"{project.name} task{task.id}",
            )
            log.warning("lease-drain: task %s (%s) %s -> %s", task.id, project.name, reason, landed)


def _fail_abandoned_dispatches(session, summary: dict[str, Any], journal: DecisionLog) -> None:
    """Land `system_failed` on every `ready` task still holding a pool slot behind a `lease_slot` order
    the node refused and nothing re-minted. Counts into `summary["abandoned"]`.

    Not the wedged-dispatch gate (`routes/agent/slot_intents.py::_park_task_on_wedged_intent`, which asks a human about
    unpushed commits): this is a machine fault with no question to ask. Otherwise such a task stays `ready`
    forever, invisible, leaking its slot.

    Safety is the "newest" and "parked" conjuncts: every other outcome leaves the intent re-served, and a
    re-mint produces a newer intent; only `parked` retires with nothing behind it. Age is measured off
    `created_at` (as `_may_park` and `purge_unexecutable_intents`); an order whose refusal reason kept
    changing can park already past the window and fail on the next pass. Runs after the fan-out (see
    `_drain_pass`). Candidates are `dispatchable_tasks`, as `_fail_unplaceable_tasks`.
    """
    # Function-local, to avoid the route-layer import cycle.
    from app.routes.tasks.dispatch import dispatchable_tasks
    from app.routes.tasks.runner_settle import end_attempt

    # Read at call time so a saved edit applies on the next pass.
    window_minutes = settings_reads.get_int("abandoned_dispatch_fail_minutes")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    # The slot-holders in one query; on a healthy fleet the set is empty.
    held: set[int] = {
        task_id for task_id in session.scalars(
            select(Worktree.task_id).where(
                Worktree.state == slot_state.TASK, Worktree.task_id.is_not(None)
            )
        )
    }
    if not held:
        return
    # Read once, after the fan-out joined, so every worker's decline is in it.
    declined = journal.declined_tasks
    projects = session.scalars(select(Project)).all()
    for project in projects:
        for task in dispatchable_tasks(project.id, session):
            if task.id not in held:
                continue
            if task.id in declined:
                # This pass considered it and journaled why it could not place it
                # (`DecisionLog.declined_tasks`); usually transient, so do not fail it.
                continue
            intent = session.scalars(
                select(SlotIntent)
                .where(
                    SlotIntent.verb == fleet_verdicts.VERB_LEASE_SLOT,
                    # `->>`, not `->`: text comparison matches whether `task_id` was written as a number or a
                    # string.
                    SlotIntent.payload["task_id"].astext == str(task.id),
                )
                # `id` breaks the tie: `created_at` is the transaction clock, so two intents in one
                # transaction share it.
                .order_by(SlotIntent.created_at.desc(), SlotIntent.id.desc())
                .limit(1)
            ).first()
            if intent is None or intent.outcome != fleet_verdicts.OUTCOME_PARKED:
                continue
            created = intent.created_at
            if created is None:
                # Only a hand-built row has no `created_at`; unknown age must never read as old.
                continue
            # Naive means UTC (`_may_park`'s reason): every writer is `func.now()` on `timestamptz`.
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created > cutoff:
                continue
            # Quoted because the node's reason was otherwise lost in a log line; redacted via
            # `fleet_verdicts.redact_node_detail`, since this row is served at `tasks:view`.
            detail = fleet_verdicts.redact_node_detail(intent.reason or "no reason reported")
            sentence = (
                f"The node refused this task's slot lease and the order was retired parked; nothing "
                f"re-offered it for {window_minutes} minutes, so the task is failed and its slot handed "
                # The same framing as `routes/agent/slot_intents.py::_note_dispatch_refusal`, so both notes read as one
                # guarantee.
                f"back to the pool. The node said, with file paths and URL credentials removed: {detail}"
            )
            # The note first, for `_fail_unplaceable_tasks`' atomicity reason. `extra` carries project, intent
            # and the redacted reason only.
            user_activity.record_task_note(
                session, task.id, "dispatch_abandoned", sentence,
                {"project": project.name, "intent_id": intent.id, "reason": detail},
            )
            # `final=True` because no retry fixes a dispatch nothing re-mints; `sources=(READY,)` fences a
            # moved row. The terminal landing returns the leaked slot: `end_attempt` mints the kill and
            # `_release_finished` frees the hold once it retires.
            landed = end_attempt(
                task.id, session, sources=(task_lifecycle.READY,), final=True, reason=sentence,
            )
            if landed != task_lifecycle.SYSTEM_FAILED:
                # The row left `ready` (perhaps a real re-mint landed), so drop the pending note.
                journal.record(
                    "abandoned-dispatch", "NOT FAILED",
                    "the task left ready between the intent read and the guarded update, so nothing landed "
                    "and its note was rolled back",
                    subject=f"{project.name} task{task.id}",
                )
                session.rollback()
                continue
            # Counted on the summary, as in `_fail_unplaceable_tasks`.
            summary["abandoned"] += 1
            journal.record(
                "abandoned-dispatch", "FAILED",
                f"lease_slot intent {intent.id} parked and nothing re-minted for "
                f"{window_minutes}min -> system_failed, slot released once its kill is enacted",
                subject=f"{project.name} task{task.id}",
            )
            log.warning(
                "lease-drain: task %s (%s) held a pool slot behind lease_slot intent %s, which the node "
                "refused and the CP retired `parked` — nothing re-minted it for %d minute(s), so the task is "
                "system_failed and its slot comes back once the runner's kill has been enacted. The "
                "node's reason: %s",
                task.id, project.name, intent.id, window_minutes, detail,
            )


def drain_once() -> dict[str, Any]:
    """One whole drain pass over every live node. Returns a small summary plus this pass's decision journal
    under `decisions` (see `DecisionLog`; read via `app/cron_system_activities.py::_invoke_lease_drain`).

    A per-node `except`, so one node's failure costs only its dispatches. Nodes are planned concurrently on
    their own sessions (`_dispatch_node_isolated`): a plan is mostly GitHub waiting, so a pass costs about its
    slowest node. The prelude stays serial on this function's own session.
    """
    # Minted here so every ledger stamp shares this pass's origin.
    journal = DecisionLog()
    summary: dict[str, Any] = {
        "nodes": 0, "issued": 0, "dispatch_errors": 0, "released": 0, "stacks_stopped": 0,
        "purged": 0, "slots_reaped": 0, "unplaceable": 0, "abandoned": 0, "drained": 0, "agent_unrunnable": 0,
        # Seeded like every other counter, so an early return reports zero rather than omitting the key.
        "user_holds_released": 0,
        "dynamic_destroyed": 0,
        "vms_swept": 0,
        "failed": [],
        # Stamped by `_finish` on every exit, never left to a caller to remember: the rendered journal and
        # the count of entries its own cap refused.
        "decisions": [], "decisions_dropped": 0,
    }
    try:
        return _drain_pass(summary, journal)
    except BaseException:
        # The decisions must not die with the pass: an unguarded raise makes the cron runtime record only the
        # exception. Logged here; the raise is untouched.
        lines = journal.lines()
        if lines:
            log.error(
                "lease-drain: this pass RAISED with %d decision(s) already made; the ledger will record "
                "the exception instead, so they are only here:\n%s", len(lines), "\n".join(lines),
            )
        raise


def _drain_pass(summary: dict[str, Any], journal: DecisionLog) -> dict[str, Any]:
    """`drain_once`'s body, split off only so the wrapper can catch a raise. Not a seam for other callers;
    every `return` still goes through `_finish`.
    """
    with SessionLocal() as session:
        summary["released"] = _release_finished(session, journal)
        # Right after the releases, so every dynamic worktree they freed is destroyed this pass (R7). Its own
        # `try` because it writes.
        try:
            summary["dynamic_destroyed"] = _destroy_free_dynamic(session, journal)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never cost the fleet its dispatch pass
            session.rollback()
            summary["dynamic_destroyed"] = 0
            journal.record(
                "prelude", "FAILED",
                f"the free dynamic worktree sweep raised {_ledger_safe(exc)}; they are destroyed next pass",
                subject="dynamic-destroy",
            )
            log.exception("lease-drain: the free dynamic worktree sweep failed this pass (%s)", exc)
        # Beside the settled releases: the capacity it frees serves this pass. Its own `try` (it writes).
        try:
            summary["stopped_question_holds_released"] = _release_stopped_question_holds(session, journal)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never cost the fleet its dispatch pass
            session.rollback()
            summary["stopped_question_holds_released"] = 0
            journal.record(
                "prelude", "FAILED",
                f"the stopped-question hold sweep raised {_ledger_safe(exc)}; those holds stay held and "
                f"are re-checked next pass",
                subject="stopped-question-holds",
            )
            log.exception("lease-drain: the stopped-question hold sweep failed this pass (%s)", exc)
        # After the settled releases and before the drain sweep: a leaked console hold keeps a draining node
        # open, and freed capacity serves this pass. Its own `try` because it writes.
        try:
            summary["user_holds_released"] = _release_leaked_user_holds(session, journal)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never cost the fleet its dispatch pass
            session.rollback()
            summary["user_holds_released"] = 0
            journal.record(
                "prelude", "FAILED",
                f"the leaked console-hold sweep raised {_ledger_safe(exc)}; every user hold stays held "
                f"and is re-checked next pass",
                subject="user-holds",
            )
            log.exception(
                "lease-drain: the leaked console-hold sweep failed this pass (%s: %s) — every user hold "
                "stays held and is re-checked next pass; dispatch is unaffected",
                type(exc).__name__, exc,
            )
        # The drain command's second half, right after the releases it waits on. Its own `try` because it
        # writes: a failed drain flip retries next pass, while a dead dispatch pass costs the fleet.
        try:
            _disable_drained_nodes(session, summary, journal)
        except Exception as exc:  # noqa: BLE001 — a drain flip must never cost the fleet its dispatch pass
            session.rollback()
            journal.record(
                "prelude", "FAILED",
                f"the drain sweep raised {_ledger_safe(exc)}; every draining node stays draining and "
                f"is re-checked next pass",
                subject="drain",
            )
            log.exception(
                "lease-drain: the drain sweep failed this pass (%s: %s) — draining nodes stay draining "
                "and are re-checked next pass; dispatch is unaffected", type(exc).__name__, exc,
            )
        # Head-of-line: a `lease_slot` minted without a `prompt_bundle` can never execute. Runs before the
        # live-node gate; pure DB cleanup.
        summary["purged"] = fleet_verdicts.purge_unexecutable_intents(session)
        if summary["purged"]:
            # One entry for the whole delete: the purge returns a rowcount only.
            journal.record(
                "purge-intents", "PURGED",
                f"{summary['purged']} pending intent(s) that can never execute (see "
                f"`purge_unexecutable_intents` for the four arms; it returns no breakdown)",
            )
        summary["stacks_stopped"] = idle_stacks.stop_idle_stacks(session, journal)
        # The reap is a prelude and may not abort the fan-out: rolled back, not just caught, since a failed
        # statement poisons the session the reads below need.
        try:
            # Before the fan-out, which only saves a pass of delay; it is not an exclusion. The fan-out can
            # still lease a slot just ordered cleared; the node then finds it taken and cancels the reap, and
            # the lease's own `reset_to_base` clears the tree. The survivor is re-selected once that task
            # settles. The reaper journals per slot, so no extra pass-level entry.
            summary["slots_reaped"] = _reap_occupied_free_slots(session, journal)
        except Exception as exc:  # noqa: BLE001 — a reap must never cost the fleet its dispatch pass
            session.rollback()
            journal.record(
                "prelude", "FAILED",
                f"the free-slot reap raised {_ledger_safe(exc)}; an occupied free slot stays occupied "
                f"until that clears, and the dispatch fan-out is unaffected",
                subject="free-slot-reap",
            )
            log.exception(
                "lease-drain: the free-slot reap failed this pass (%s: %s) — the dispatch fan-out below "
                "is unaffected, but an occupied `free` slot stays occupied until this clears",
                type(exc).__name__, exc,
            )
        # Its own `try`: this writes (a terminal transition plus an Activity note). Fleet-wide, asked once.
        try:
            _fail_unplaceable_tasks(session, summary, journal)
        except Exception as exc:  # noqa: BLE001 — a refusal must never cost the fleet its dispatch pass
            session.rollback()
            journal.record(
                "prelude", "FAILED",
                f"the placement-feasibility sweep raised {_ledger_safe(exc)} after refusing "
                f"{summary['unplaceable']} task(s); every task it had not reached keeps waiting",
                subject="placement",
            )
            log.exception(
                "lease-drain: the placement-feasibility sweep failed this pass after refusing %d task(s) "
                "(%s: %s) — dispatch is unaffected, and every task it had not yet reached keeps "
                "waiting until this clears",
                summary["unplaceable"], type(exc).__name__, exc,
            )
        # Its own `try` for the same reason: it writes (an attempt, a note, maybe a terminal transition).
        try:
            _fail_unrunnable_agents(session, summary, journal)
        except Exception as exc:  # noqa: BLE001 — a refusal must never cost the fleet its dispatch pass
            session.rollback()
            journal.record(
                "prelude", "FAILED",
                f"the unrunnable-agent sweep raised {_ledger_safe(exc)} after charging "
                f"{summary['agent_unrunnable']} task(s); every task it had not reached keeps waiting",
                subject="agent",
            )
            log.exception("lease-drain: the unrunnable-agent sweep failed this pass (%s)", exc)
        # MicroVM teardowns and the failed-task retention clock (`app/microvm_lifecycle.py`, D23), plus the
        # convergence on every MicroVM node's reported VM list. Its own `try`. The community edition has no
        # MicroVMs.
        if edition_features.enabled("microvm"):
            from app import microvm_lifecycle

            try:
                swept = microvm_lifecycle.sweep(
                    session,
                    record=lambda decision, why, subject: journal.record("microvm", decision, why, subject=subject),
                )
                summary["vms_swept"] = swept["destroyed"] + swept["retained"] + swept["reclaimed"]
            except Exception as exc:  # noqa: BLE001 — a teardown must never cost the fleet its dispatch pass
                session.rollback()
                journal.record(
                    "prelude", "FAILED",
                    f"the MicroVM lifecycle sweep raised {_ledger_safe(exc)}; every VM it had not reached is "
                    f"re-checked next pass",
                    subject="microvm",
                )
                log.exception("lease-drain: the MicroVM lifecycle sweep failed this pass (%s: %s)",
                              type(exc).__name__, exc)
        nodes = pool_state.live_nodes(session)
        if not nodes:
            # Not a journal entry: with no live node nothing was decided, and a row every ten seconds while
            # the fleet is down would be noise (`node-liveness` alarms on that).
            log.info("lease-drain: no node has posted a complete slot enumeration recently — nothing to do")
            # Still say why each ready task waits (no node, or its nodes not live): nothing else will.
            _stamp_without_nodes(session)
            return _finish(summary, journal)
        summary["nodes"] = len(nodes)
        # The pass's one allocation table, built in the serial prelude and handed to every worker unchanged.
        # Its inputs move within a pass, so per-node tables could disagree and lease two slots for one task
        # (`routes/fleet.py::fleet_allocation`). Built after the releases so it sees the freed room.
        from app.routes.fleet import DispatchPassFacts, fleet_allocation
        from app.account_runners import fleet_assigned_runner_counts
        waiting: set[int] = set()
        # The pass's one read of where each task's launches kept failing, shared like the allocation.
        failed_on = launch_failures.excluded_node_ids(session)
        # Filled by the allocation and then shared with every node's plan: one answer per task per pass.
        allowed_by_task: dict = {}
        # Tasks the allocation left unplaced for a reason it names (`fleet_allocation`'s `wait_reasons_out`).
        wait_reasons: dict[int, str] = {}
        # Each live per-agent trial's agents wanted this pass, kept as its peak (`app/trial_agents.py`).
        demand: dict[UUID, int] = {}
        allocation = fleet_allocation(session, waiting_out=waiting, failed_on=failed_on,
                                      allowed_out=allowed_by_task, wait_reasons_out=wait_reasons, demand_out=demand)
        # Before the fan-out leases anything: the pass's own leases are counted by its account ledger alone.
        pass_facts = DispatchPassFacts(waiting=frozenset(waiting), allowed_by_task=allowed_by_task,
                                       failed_on=failed_on, runner_counts=fleet_assigned_runner_counts(session))
        # The roster's hosting half, the freshness bound and the account rows, once for every node.
        try:
            pass_facts.read_node_independent(session)
        except Exception as exc:  # noqa: BLE001 — a shared read failing must not cost the pass
            # Its fields stay None, so each node's plan makes its own reads, as before they were shared.
            session.rollback()
            log.warning("lease-drain: the pass's shared plan reads failed, each node reads its own (%s: %s)",
                        type(exc).__name__, exc)
        _stamp_failed_launch_waits(session, allocation, failed_on, allowed_by_task)
        try:
            dispatch_wait.record(session, {t: why for t, why in wait_reasons.items() if t not in allocation})
        except Exception:  # noqa: BLE001 — a console hint must never cost the pass
            session.rollback()
            log.exception("lease-drain: could not stamp %d wait reason(s)", len(wait_reasons))
        # A trial is a plan's: an edition without billing has none to tally.
        if edition_features.enabled("billing"):
            from app import trial_agents

            try:
                trial_agents.record_peaks(session, demand)
            except Exception:  # noqa: BLE001 — a trial's tally must never cost the pass
                session.rollback()
                log.exception("lease-drain: could not record %d trial peak(s)", len(demand))
    # No project had a dispatchable Task at pass start, so no node's plan can offer one: every project
    # outside `waiting` is reported and offered nothing (`build_dispatch_plan`), a task readied since is
    # skipped there the same way, and a MicroVM node admits only what `allocation` (a subset) gave it.
    # Not `not allocation`: a task holding a slot is outside the allocation and dispatches on its hold node.
    # The idle pass ends at its prelude (the fan-out was the idle api's hottest background cost), so
    # a node's DISABLED and a project's SKIPPED entries appear only on a pass with work: no idle health signal.
    if not waiting:
        log.debug("lease-drain: no project has a dispatchable task — %d node(s) not planned", len(nodes))
        nodes = []
    # Outside the prelude's `with`, so its connection returns before the fan-out claims one per node.
    # The background lane (`app/db.py`): the fan-out's sessions draw on their own pool, never the requests'.
    with background_executor(max(1, min(len(nodes), _MAX_NODE_WORKERS)), "lease-drain") as pool:
        futures = {
            pool.submit(_dispatch_node_isolated, node_id, journal, allocation, pass_facts): node_id
            for node_id in nodes
        }
        for future in as_completed(futures):
            node_id = futures[future]
            try:
                node_issued, node_contained = future.result()
                summary["issued"] += node_issued
                # The contained count is a work key, so a pass where every decision raised does not read as
                # idle.
                summary["dispatch_errors"] += node_contained
            except Exception as exc:  # noqa: BLE001 — one node must not abort the pass
                summary["failed"].append(node_id)
                journal.record(
                    "node", "FAILED",
                    f"this node's whole pass raised {_ledger_safe(exc)}; no other node's pass was "
                    f"aborted by it",
                    subject=node_id,
                )
                log.warning("lease-drain: node %s failed this pass (%s: %s)",
                            node_id, type(exc).__name__, exc)
    # ── The abandoned-dispatch repair, and it runs HERE rather than in the prelude ────────────────────
    # After the fan-out, because only then can a genuine wedge be told from a task an operator just recovered
    # with Stop/Start (identical rows until a re-mint lands; no column records the recovery). After the
    # fan-out a dispatchable task has a newer intent. Known limit: the prelude purges `parked` rows at one
    # hour even with no live node, so a fleet-down stretch loses the evidence;
    # `abandoned_dispatch_fail_minutes` is capped under that hour. Own session and `try`, because it writes.
    try:
        with SessionLocal() as session:
            _fail_abandoned_dispatches(session, summary, journal)
    except Exception as exc:  # noqa: BLE001 — a repair must never cost the fleet its dispatch pass
        journal.record(
            "prelude", "FAILED",
            f"the abandoned-dispatch sweep raised {_ledger_safe(exc)} after failing "
            f"{summary['abandoned']} task(s); every task it had not reached keeps holding its slot",
            subject="abandoned-dispatch",
        )
        log.exception(
            "lease-drain: the abandoned-dispatch sweep failed this pass after failing %d task(s) "
            "(%s: %s) — every dispatch this pass issued stands, and every task it had not yet reached "
            "keeps holding its slot until this clears",
            summary["abandoned"], type(exc).__name__, exc,
        )
    # Summarised only when the pass did something (an idle fleet would log ~360 zero lines an hour); a quiet
    # pass leaves a DEBUG trace. Derived from the dict, not a hand-copied tuple, so no counter silently stays
    # zero. `nodes` is excluded (an idle live fleet must read as nothing happened); the journal keys are still
    # falsy here.
    did_something = any(value for key, value in summary.items() if key != "nodes")
    log.log(
        logging.INFO if did_something else logging.DEBUG,
        "lease-drain: %d node(s), %d hold(s) released (task settled), "
        "%d idle stack(s) stopped, %d lease_slot intent(s) issued, %d unexecutable intent(s) purged, "
        "%d `free` slot(s) ordered cleared of a live session still sitting in them, "
        "%d structurally unplaceable task(s) failed, "
        "%d task(s) failed for a dispatch nothing re-minted, %d node(s) failed, "
        "%d dispatch decision(s) contained (each raised and was declined alone)",
        summary["nodes"], summary["released"],
        summary["stacks_stopped"], summary["issued"], summary["purged"],
        summary["slots_reaped"], summary["unplaceable"], summary["abandoned"],
        len(summary["failed"]), summary["dispatch_errors"])
    return _finish(summary, journal)
