"""Committed task transition -> the work that transition owes, started now.

`app/events.py::emit_console_event` owes each activity below to the write's own transaction
(`cron_runtime.wake_after_commit`), so it fires once, from the committing process, and only if the write
commits. Each is a pass fired early via `cron_runtime.fire_now_activity`: `task-pipeline` on every
transition (an epic is its children's rollup), `lease-drain` on all but `active` (the drain's own
output) and `awaiting_response` (the row keeps its slot, so the drain has nothing to free or fill), `trigger-intake` on `done` and `system_failed` (a trigger's queue drains when its Task closes). Keyed on `activity_type`, never the editable `crons.name`. The done workflow is not here: the `done`
write enqueues it on its own transaction (`app/durable/task_done.py::enqueue_in_transaction`). The node is
not woken. Crons are loss-tolerant (each interval is the backstop). Nothing here raises.
"""
from __future__ import annotations

from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, DONE, SYSTEM_FAILED

# Registered names from `app/cron_system_activities.py`, as strings to keep this suite DB-free;
# `tests/test_task_wake.py` pins them against the registry.
PIPELINE_ACTIVITY = "task-pipeline"
DISPATCH_ACTIVITY = "lease-drain"
# The trigger processor (`app/trigger_intake.py`): a filed Task closing frees a trigger's concurrency slot, so its
# queue drains now rather than at the next interval (AC-11.1).
TRIGGER_INTAKE_ACTIVITY = "trigger-intake"
TRIGGER_INTAKE_WAKE_ACTIONS = frozenset({DONE, SYSTEM_FAILED})

# Fire the pipeline but not the drain: `active` is the drain's own output, and `awaiting_response` keeps its
# slot (`lease_drain._release_finished`), so neither changes what the drain frees or fills. An operator chat
# parks at every turn end, so waking the drain on it ran the pass back-to-back under a busy chat.
NO_DISPATCH_WAKE_ACTIONS = frozenset({ACTIVE, AWAITING_RESPONSE})


def wake_activities_for(action: str) -> tuple[str, ...]:
    """The activity types a task transition to `action` should fire, in firing order.

    Pure and total: an unknown action fires both crons, the safe direction (a redundant pass beats a
    missed fire).
    """
    activities = [PIPELINE_ACTIVITY]
    if action not in NO_DISPATCH_WAKE_ACTIONS:
        activities.append(DISPATCH_ACTIVITY)
    if action in TRIGGER_INTAKE_WAKE_ACTIONS:
        activities.append(TRIGGER_INTAKE_ACTIVITY)
    return tuple(activities)
