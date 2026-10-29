"""The outbound-delivery queue: every operator alert a task transition owes, as durable DBOS work.

A queue, not a cron: a lost `blocked` alert is not recoverable later, since its transition has
committed and nothing re-emits it. `app/events.py`'s LISTEN task stays the transport; its subscriber in
`app/task_notify.py` enqueues here. The workflow body is synchronous (DBOS worker thread) and touches no
node bus, so it needs no serving-loop hop.

No leader: every replica enqueues each transition. Layered dedupe: (1) the deterministic workflow ID;
(2) `alert_stamps`' per-task claim taken before relay; (3) the `uq_alerts_dedup` unique key.
"""
from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS

from app.durable import runtime
from app.durable.runtime import enqueue_or_warn

log = logging.getLogger("task-notify")

# Same-task ordering is serialized by the claim, so concurrency only spreads across tasks.
NOTIFY_QUEUE = runtime.declare_queue("task-notify", **runtime.VENDOR_QUEUE)


def notification_workflow_id(task_id: Any, action: str, window_start: Any) -> str:
    """The deterministic ID two replicas racing one transition must agree on (pure, testable without DBOS).

    Keyed on `(task, action, window)`, the identity `uq_alerts_dedup` collapses on
    (`app/alert_record.py::current_window_start`), so the queue admits exactly what the DB would: a finer
    key wastes relays, a coarser one drops alerts.
    """
    return f"task-notify:{task_id}:{action}:{window_start.isoformat()}"


@DBOS.workflow()
def deliver_task_notification(event: dict) -> None:
    """One durable delivery for one committed transition.

    Imported by `app/durable/runtime.py` for its decorator side effect; an unregistered workflow cannot be
    enqueued or recovered.

    `event` carries only `{entity, id, action}`; everything is re-read so a recovered workflow renders the
    task as it is now. The org is bound here because each of those re-reads opens its own session on an
    unbound worker thread, where an enforcing seam would silently find nothing; `bind_from`'s ContextVar
    region covers them all.
    """
    # Deferred: imported at DBOS registration time, before the app is wired.
    from app.models.tasks import Task
    from app.task_notify import deliver_notification
    from app.tenancy import seam

    entity = event.get("entity")
    if entity != "task":
        # Unreachable (`app/task_notify.py::_run_task_notifier` drops non-task events), handled anyway:
        # there is no task to derive an org from. Warn and return; raising would leave a permanent
        # `failed` workflow row.
        log.warning(
            "task-notify: dropping a %r event — this queue delivers task notifications only, so there "
            "is no task to derive an org from and nothing correct to send", entity,
        )
        return
    try:
        task_id = int(event["id"])
    except (KeyError, TypeError, ValueError):
        # Same drop: an event with no integer id names no row.
        log.warning(
            "task-notify: dropping a task event whose id is %r — there is no row to derive an org from",
            event.get("id"),
        )
        return
    # A session only for `bind_from`'s resolving read; `deliver_notification` opens its own and the ContextVar
    # region carries the scope. A task deleted between enqueue and delivery has nothing to announce; raising
    # would burn retries and leave a `failed` row.
    gone = f"task-notify: task {task_id} no longer exists — its {event.get('action')!r} alert is dropped undelivered"
    with (
        runtime.step_session("durable:task_notify.deliver", gone=gone, logger=log) as session,
        seam.bind_from(session, Task, task_id),
    ):
        # Release the idle-in-transaction connection before the slow Slack relay (`app/tenancy/seam.py::bind_from`).
        session.rollback()
        deliver_notification(event)


def enqueue_notification(event: dict, window_start: Any) -> bool:
    """Enqueue one delivery. True when this call is the one that placed it.

    Never raises into the listener, which also serves the SSE stream; a failure is a warning and False,
    and `app/task_pipeline.py::sweep_needs_attention_notify` is the backstop.
    """
    task_id = event.get("id")
    action = event.get("action")
    return enqueue_or_warn(
        NOTIFY_QUEUE, notification_workflow_id(task_id, action, window_start), deliver_task_notification,
        event, log=log,
        failure=f"task-notify: could not enqueue the delivery for task {task_id}",
        consequence="the pipeline sweep's needs-attention backstop is what announces it now",
    )
