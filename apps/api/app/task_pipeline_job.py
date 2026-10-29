"""The `task-pipeline` sweep: the Task-pipeline auto-advance, run in the control plane.

The CP is `desiredCount=1`, so the singleton is structural. Metrics capture stays on the node
(`apps/jaahd/session_sweep.py`), which can read the live transcript. The CP cannot see host
processes, so `runner_verdict` asks whether the task's node is talking to us and what it says about
the runner: ALIVE / DEAD / UNKNOWN, since "gone" and "quiet" want opposite handling. Fail-closed:
unproven is UNKNOWN, never DEAD, so a bug costs latency, not correctness. Every step is blocking DB
work (`app/task_pipeline_cp.py`), run on the cron wrapper's executor thread, never the event loop.
"""
from __future__ import annotations

import logging

from app import task_pipeline, task_pipeline_cp
from app.alert_record import record_alert
from app.db import SessionLocal
# Re-exported, not re-spelled (see the liveness note below `HOLDER_LIVE_WINDOW_SECONDS`): only the
# names already spelled through this module; `LAUNCH_SETTLE_SECONDS` because this module's tests read it.
from app.runner_liveness import (  # noqa: F401 — re-export
    ALIVE,
    DEAD,
    LAUNCH_SETTLE_SECONDS,
    NODE_STILL_TALKING_SECONDS,
    OBSERVATION_FRESH_SECONDS,
    UNKNOWN,
    runner_verdict,
)
from app.task_lifecycle import SYSTEM_FAILED

log = logging.getLogger("cockpit-scheduler")

# The cadence and bound are the `task-pipeline` cron row's; `app/cron_system_activities.py` registers
# them as the activity defaults, and the early-wake floor is `crons.min_gap_seconds`.
#
# The 60s interval is a backstop: a committed task transition fires this cron early (`app/task_wake.py`),
# so an epic advances between stages in seconds.
INTERVAL_SECONDS = 15.0
# Bounds a wedge, not an overlap: `cron_runtime.RunLock` already refuses a second open pass.
# It decides how long a wedged pass blocks later ticks; 90s keeps `lease-drain`'s ratio (~6 ticks),
# still far more than a real pass needs.
TIMEOUT_SECONDS = 90.0
# The UNKNOWN grace only: how long to keep waiting when the node has gone quiet or its probe could not
# judge. Generous on purpose — the failure direction is "refuse to reclaim", and a silent node may still
# be running the runner, so redispatching early risks two writers on one tree.
HOLDER_LIVE_WINDOW_SECONDS = 900.0
# The same grace for the merge lane's row: its run is restartable, and the lane
# drops a row another run has since taken (a 409), so waiting out the general grace only keeps every queued PR
# waiting behind a node that went quiet.
LANE_HOLDER_LIVE_WINDOW_SECONDS = 300.0
# The UNKNOWN grace while the task's node still heartbeats (`nodes.last_seen` within
# `NODE_STILL_TALKING_SECONDS`) but has stopped observing this slot (`observed_at` older than
# `runner_liveness.OBSERVATION_FRESH_SECONDS`): a starved daemon, whose runners keep
# working. On 2026-10-17 a node at load average 328 lost its observation, and the 900s timer reclaimed 45
# LIVE runners whose transcripts ran on for minutes. Bounded, so a node that never observes again
# still releases its rows; the node-liveness cron alarms on the not-observing node meanwhile.
TALKING_NODE_HOLDER_LIVE_WINDOW_SECONDS = 3600.0
# ── The liveness predicate: single-homed in `app/runner_liveness.py` ──────────────────────────────
# Re-exported so existing callers and `monkeypatch.setattr(task_pipeline_job, "runner_verdict", ...)`
# still reach what `_runner_held` calls. The module docstring above is the home for why the verdict is
# three-valued; that file for what each value requires.


def _runner_held(
    number: str,
    window: float = HOLDER_LIVE_WINDOW_SECONDS,
    talking_window: float = TALKING_NODE_HOLDER_LIVE_WINDOW_SECONDS,
) -> bool:
    """The reclaimers' presence probe: True ⇒ do NOT reclaim. A thin adapter over `runner_verdict`.

    A boolean seam because the reclaimers in `app/task_pipeline.py` take an injected
    `lease_held(number)`. ALIVE holds, DEAD does not, UNKNOWN holds only until the grace runs out:
    `window`, or `talking_window` while the node is starved — it still heartbeats but stopped observing."""
    state = runner_verdict(number)
    if state.verdict == ALIVE:
        return True
    if state.verdict == DEAD:
        log.warning(
            "task-pipeline: task %s — its node is reporting normally and says NO runner is there, so the "
            "runner is CONFIRMED gone; reclaiming now instead of waiting out the %.0fs silence grace",
            number, window,
        )
        return False
    unknown_for = state.unknown_for_secs
    node_spoke, observed = state.node_spoke_secs, state.observed_secs
    starved = (
        node_spoke is not None and node_spoke <= NODE_STILL_TALKING_SECONDS
        and (observed is None or observed > OBSERVATION_FRESH_SECONDS)
    )
    if starved:
        window = max(window, talking_window)
    if unknown_for is not None and unknown_for > window:
        log.warning(
            "task-pipeline: task %s — still nobody can say whether its runner is alive after %.0fs; the "
            "silence grace is spent, so reclaiming on the timer (unchanged behaviour for a quiet node)",
            number, unknown_for,
        )
        return False
    log.warning(
        "task-pipeline: task %s — nobody can say whether its runner is alive (node quiet, or its probe "
        "could not judge); holding up to %.0fs rather than risk two writers on one tree",
        number, window,
    )
    return True


def _lane_runner_held(number: str) -> bool:
    """`_runner_held` with the lane's shorter silence grace (`LANE_HOLDER_LIVE_WINDOW_SECONDS`), not
    lengthened for a starved node: a queued PR must not wait behind one."""
    return _runner_held(number, LANE_HOLDER_LIVE_WINDOW_SECONDS, LANE_HOLDER_LIVE_WINDOW_SECONDS)


def _runner_confirmed_dead(number: str) -> bool:
    """The strict probe: True only when a live node has affirmatively said the runner is gone.

    `_runner_held` also reclaims on an expired UNKNOWN grace, which is fine for reclaimers that only
    requeue. The `awaiting_response` reclaimer and chats destroy a live conversation on a wrong verdict,
    and a node can stop reporting for a long time with a live runner, so an expired grace is not evidence.

    False on ALIVE, False on UNKNOWN however old, True only on DEAD. Fail-closed: any error is False."""
    try:
        state = runner_verdict(number)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED: never reclaim on a probe that did not answer
        log.warning(
            "task-pipeline: task %s — the strict liveness probe raised, so its parked question is NOT "
            "reclaimed this pass: %s", number, exc,
        )
        return False
    # No log line on the DEAD arm: this runs every 15s and would print forever for a row the reclaim then
    # permanently refuses. The sweep logs the reclaim that lands.
    return state.verdict == DEAD


def _alert(
    kind: str, repo: str, reason: str, task_id: int | None = None, *, body: str | None = None
) -> None:
    """In-process alert. A fresh session per alert, because `record_alert` commits. Best-effort — an alert
    that fails must not break the sweep.

    `task_id` scopes the dedup identity, so two tasks in one project inside one bucket stay two alerts.
    It is passed as `record_alert`'s `task_id` parameter, not `payload["task_id"]`: the payload key makes
    an alert answerable, and these must stay non-answerable (R5.4, R5.9).

    `node_id` stays a name and is not resolved here: the control plane is not a `nodes` row, and
    `alert_record.record_alert` owns turning an unknown name into NULL (fail-open)."""
    try:
        with SessionLocal() as session:
            record_alert(
                session, kind=kind, node_id=task_pipeline.control_plane_api.this_node_id(),
                repo=repo or None, reason=reason, task_id=task_id,
                payload=_headline_payload(session, task_id, body) if body is not None else None,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        log.warning("task-pipeline: %s alert failed: %s", kind, exc)


def _headline_payload(session, task_id: int | None, body: str) -> dict | None:
    """The fields that make this alert read like every other task alert — `{project}|{id} {title}:
    {status}` — or `None` when the Task is gone.

    Without `task_id`, deliberately: that key is the answerable signal (R5.4, R5.9);
    `alert_relay.task_parts` reads the id off `record_alert`'s own parameter. `body` says which state,
    persona and visibility the row is in."""
    # Function-local, as in `notify_targets.resolve_slack_target`: avoids a wide import closure.
    from app.models.projects import Project
    from app.models.tasks import Task
    from app.org_names import org_name
    from app.task_external_comment import _console_task_url

    # Fail-open: a faulting read costs the alert its task line, never the alert.
    try:
        task = session.get(Task, task_id) if task_id is not None else None
        project = session.get(Project, task.project_id) if task is not None else None
        payload = None if task is None else {
            "task_title": task.title,
            "project": project.name if project is not None else None,
            "org": org_name(session, task.org_id),
            "question_body": body,
            "console_url": _console_task_url(task.id),
        }
        # `get` began a transaction and `record_alert` commits on this session; end the read first.
        session.rollback()
    except Exception:  # noqa: BLE001 — best-effort by contract
        log.warning("task-pipeline: no headline for task %s's alert", task_id, exc_info=True)
        payload = None
    return payload


def _notify_epic_fanout_blocked(epic_id: int, repo: str, reason: str) -> None:
    _alert("blocked", repo,
           f"epic task {epic_id} fanout blocked — issue-create ceiling reached: {reason}",
           task_id=epic_id)


def _notify_stuck_epic(epic_id: int, repo: str, reason: str, stuck_seconds: float) -> None:
    _alert("self-heal", repo,
           f"epic task {epic_id} stuck — active with all children done for "
           f"{stuck_seconds / 60:.0f}m, rollup blocked: {reason}",
           task_id=epic_id)


def _notify_needs_attention(row: dict) -> None:
    state = row.get("state")
    # "hidden", not "hidden system task": a Runner cron can file the operator's own `coder` work hidden,
    # and the persona is already in the message.
    hidden = " (hidden)" if row.get("hidden") else ""
    _alert(
        # The comparison reads `Task.state` (so uses the constant); the emitted strings are alert kinds
        # (`alert_relay.ALERT_KINDS`, drift-guarded against the node daemon's notifier), a separate vocabulary,
        # so they stay literals.
        "failed" if state == SYSTEM_FAILED else "blocked",
        row.get("project") or "",
        f"needs attention: task {row.get('id')} [{row.get('persona')}]{hidden} is `{state}` — "
        f"{row.get('title') or ''}",
        # Per-task dedup identity (see `_alert`), read defensively; `None` falls back to the repo-wide identity.
        task_id=row.get("id") if isinstance(row.get("id"), int) else None,
        body=f"[{row.get('persona')}]{hidden} is `{state}`",
    )


def sweep_once() -> None:
    """The node's `--sweep-tasks` body, same steps in the same order, minus the metrics quarter.

    The notifier wiring is set and cleared around the pass: the module is reused for the control
    plane's lifetime, so a closure left wired would fire from an unrelated later call."""
    # No lease collector: `worktrees` occupancy (`free`/`task`/`user`) has no expiry rule — a hold ends only
    # when its task is terminal/deleted/reset or a user exits, the row is reused, and an un-acked dispatch
    # is re-dispatched into the same slot.
    task_pipeline.set_epic_fanout_blocked_notifier(_notify_epic_fanout_blocked)
    task_pipeline.set_stuck_epic_notifier(_notify_stuck_epic)
    task_pipeline.set_needs_attention_notifier(_notify_needs_attention)
    try:
        task_pipeline.sweep_epic_children_fanout()
        task_pipeline.sweep_epic_rollups()
        task_pipeline.sweep_needs_attention_notify()
    finally:
        task_pipeline.set_epic_fanout_blocked_notifier(None)
        task_pipeline.set_stuck_epic_notifier(None)
        task_pipeline.set_needs_attention_notifier(None)

    # The crash reclaimers, each fail-closed through the probe above; outside the notifier block because
    # they need no notifier. `_runner_held` answers liveness directly, so no second `lease_stale` probe.
    task_pipeline.reclaim_stale_active_analysis_children(_runner_held)
    task_pipeline.sweep_finish_phases(_runner_held)
    task_pipeline.reclaim_stale_active_coders(_runner_held)
    # The restart-only rows (the `outcome` and `operator` criteria). Two probes: the loose one for
    # `outcome` rows (a wrong verdict costs a re-run), the strict one for chats (it costs a live
    # conversation, and a quiet node may still be running them).
    task_pipeline.reclaim_stale_active_restartable_tasks(_runner_held, _runner_confirmed_dead, _lane_runner_held)
    # The stall backstop, after the reclaimers so a row they can judge keeps their cause; its own clock, no probe.
    task_pipeline.reclaim_silent_active_tasks()
    # Rows parked on an `AskUserQuestion` whose runner died are unanswerable. Uses the strict probe: a wrong
    # verdict destroys a live conversation, so it waits for an affirmative DEAD. Shares no candidate rows
    # with the reclaimers above, so its position is legibility, not a dependency.
    task_pipeline.reclaim_stale_awaiting_response_tasks(_runner_confirmed_dead)
    # Rows parked on a question whose runner is alive but whose human has not answered; no probe.
    #
    # Must run after the reclaimer: a confirmed-dead runner's row is reclaimed for re-dispatch, never
    # expired for a timeout its death caused. The ordering is the precedence.
    task_pipeline.expire_timed_out_awaiting_response_tasks()
    # The merge lane's scheduler backstop (L9c), after the reclaimers so a lane run they just ended has its
    # rows requeued and is re-filed in this same pass. Its own failure costs nothing above.
    try:
        task_pipeline_cp.merge_lane_tick()
    except Exception:  # noqa: BLE001 — a lane pass that raised is retried on the next tick
        log.exception("task-pipeline: the merge-lane tick raised; retried next pass")
