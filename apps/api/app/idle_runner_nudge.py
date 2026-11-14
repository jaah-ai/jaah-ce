"""The control plane's idle-runner nudge: an `active` task whose runner is alive but idle, with nothing written to
its transcript for `IDLE_NUDGE_AFTER`, gets a `[control plane]` message typed into its pane — again every
`IDLE_NUDGE_AFTER` for as long as that holds. Uncapped by decision: a runner that keeps idling keeps being woken.

Why here and not only on the node: the in-session wake (the harness's async idle-nudge Stop hook) is a process
inside the runner's own reach, and a runner's broad `pkill` that matched it left the task idle with nothing to
wake it. The control plane is out of that reach, so it is the backstop; the node hook stays the fast path (10
minutes) and the harness's kill guard keeps the runner from killing it.

Selection (`_IDLE_SQL`), all on one held slot:
  * the task is `active` (a parked question is `awaiting_response`, never nudged), not the `advisor` chat (its
    operator is live), and has not moved for the window (a fresh claim is a fresh runner);
  * the slot's latest observation is fresh and reports an `idle` session — a dead runner is the stall detector's
    (`task_pipeline.reclaim_silent_active_tasks`); a `busy` one, or one whose status the node cannot tell (a
    vendor or ACP runner's `running`, a failed probe's NULL), is left alone rather than typed into mid-work;
  * its transcript's last write is older than the window (NULL = never sighted, not judged);
  * no nudge from `SENDER` was minted for the task inside the window. The pane write itself is the stamp: a
    `deliver_message` row is kept after delivery (`slot_intents`, indexed for exactly this read), so the cadence
    survives deploys and needs no column. The delivered nudge also writes the transcript, which re-arms the clock.

A MicroVM run holds no slot and is not covered; `live_runner.deliver_message_to_live_runner` mints nothing for a
task with no live, identified session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from app.clock import utcnow
from app.task_lifecycle import ACTIVE

log = logging.getLogger("cockpit-scheduler")

IDLE_NUDGE_AFTER = timedelta(minutes=30)
# Names the speaker on the task page's Timeline and scopes intent supersession (`fleet_verdicts.issue_intents`).
SENDER = "Idle nudge"
NUDGE_TEXT = (
    "Your task is still active, but you have been idle for 30 minutes with nothing set to wake you. "
    "If you are waiting on background work, check it now. If you need the operator, ask with AskUserQuestion. "
    "Otherwise continue the task, or settle it if it is done."
)

_IDLE_SQL = """
SELECT DISTINCT t.id FROM tasks t
JOIN worktrees w ON w.task_id = t.id AND w.state = 'task'
WHERE t.state = :active
  AND t.persona <> :advisor
  AND t.github_synced_at <= :cutoff
  AND coalesce(w.session, false) AND w.observed_at > :observed_fresh
  AND w.session_status = 'idle'
  AND w.transcript_written_at <= :cutoff
  AND (CAST(:only AS bigint) IS NULL OR t.id = :only)
  AND NOT EXISTS (
      SELECT 1 FROM slot_intents i
       WHERE i.verb = 'deliver_message' AND (i.payload ->> 'task_id') = t.id::text
         AND (i.payload ->> 'sender') = :sender AND i.created_at > :cutoff)
ORDER BY 1
"""


def list_idle_runner_tasks(now: Optional[datetime] = None, *, only: Optional[int] = None) -> list[int]:
    """The ids of the `active` tasks to nudge now (`_IDLE_SQL`), or just `only` when it qualifies; `now` is the
    clock, injectable for tests."""
    from sqlalchemy import text

    from app.routes.tasks.policy import PERSONA_ADVISOR
    from app.runner_liveness import OBSERVATION_FRESH_SECONDS
    from app.task_pipeline_cp import _cp_session

    now = now or utcnow()
    params = {
        "active": ACTIVE, "advisor": PERSONA_ADVISOR, "only": only, "sender": SENDER, "cutoff": now - IDLE_NUDGE_AFTER,
        "observed_fresh": now - timedelta(seconds=OBSERVATION_FRESH_SECONDS),
    }
    with _cp_session() as session:
        return list(session.scalars(text(_IDLE_SQL), params).all())


def nudge_idle_runners(now: Optional[datetime] = None) -> list[int]:
    """One pass: nudge every task `list_idle_runner_tasks` names. Returns the ids actually nudged. Best effort —
    a failed read or mint is logged and retried on the next pass."""
    try:
        from app import live_runner
        from app.task_pipeline_cp import _cp_session

        idle = list_idle_runner_tasks(now)
    except Exception as exc:  # noqa: BLE001 — a missed nudge costs one pass, never the sweep
        log.warning("idle-nudge: work-list read failed — nudging nobody this pass: %s", exc)
        return []
    nudged: list[int] = []
    for task_id in idle:
        try:
            with _cp_session() as session:
                if live_runner.deliver_message_to_live_runner(session, task_id, NUDGE_TEXT, sender=SENDER):
                    nudged.append(task_id)
        except Exception as exc:  # noqa: BLE001 — one task's refusal must not starve the rest
            log.warning("idle-nudge: task %s not nudged (%s); retried next pass", task_id, exc)
    if nudged:
        log.info("idle-nudge: nudged %d idle runner(s): %s", len(nudged), nudged)
    return nudged
