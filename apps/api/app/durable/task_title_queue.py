"""The auto-title queue — the LLM title a created task owes itself, as durable work.

Titling is an event, not a cadence: nothing ever re-derives a lost title, so the row would keep its
`short_title(body)` placeholder for ever. The enqueue is its own write after the create's commit, so a
crash between the two still loses the title; what is recovered is the vendor call and the write behind
it. `BackgroundTasks` was rejected because a deploy mid-window drops the work with no record.

`POST /api/tasks`, the fire engine (`app/automation_fire.py`) and a runner's filing or amend
(`routes/agent/agent_filing.py`, with the filer's `authored` title) enqueue; every other producer authors its
title, and `app/task_title.py`'s guard re-checks anyway. Each API start also sweeps untouched filed drafts
still wearing an over-cap title (`request_filed_title_sweep`, `app/task_title.py::filed_titles_owed`): a lost enqueue, and the drafts filed before
filings were refined. The body is synchronous
and touches no node bus, so none of `runtime._CaptureServingLoop`'s loop-hopping applies. No leader: the
deterministic per-task ID admits one workflow, and the placeholder guard no-ops a second.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from dbos import DBOS

from app.durable import runtime
from app.durable.runtime import enqueue_or_warn

# Separate from `app/task_title.py`'s `cockpit-task-title` so enqueue failures and refinement failures
# can be filtered apart.
log = logging.getLogger("task-title")

# Creates arrive at human pace and a title has no deadline.
TITLE_QUEUE = runtime.declare_queue("task-title", **runtime.VENDOR_QUEUE)


def title_workflow_id(task_id: Any, authored: str | None = None) -> str:
    """The deterministic ID every enqueue for one task must agree on.

    Keyed on the task alone: a task owes exactly one generated title, which makes a recovery re-enqueue
    idempotent. A filed title adds its digest: a filer's amend owes a new one, a re-send of the same title
    does not. Pure, so it is testable without a live DBOS."""
    if authored is None:
        return f"task-title:{task_id}"
    return f"task-title:{task_id}:{hashlib.sha256(authored.encode()).hexdigest()[:16]}"


# Adding a workflow is not a reason to bump `runtime.ACCOUNTS_WORKFLOW_VERSION` (a bump strands all
# in-flight work). Residual: during a rolling deploy an old replica may recover a row for a function it
# never registered; the row goes back on the queue and a replica that has it runs it.


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5.0)
def refine(task_id: int, authored: str | None = None) -> bool:
    """One attempt at the refinement, as a checkpointed, retried step.

    Only database faults retry: `title_from_description` swallows vendor failures and returns the
    fallback, which `refine_title` reports as no change. A retry re-pays the vendor call (bounded at
    three, inside the rate limiter).

    The org bind (the step wakes on a worker thread with nothing bound; `refine_title` owns its own
    sessions and transactions), the pool release across the LLM round trip and the deleted-while-queued
    `False` (not three failed attempts) are `runtime.delegate_for_task`'s."""
    from app.task_title import refine_title

    return runtime.delegate_for_task(
        "durable:task_title.refine", task_id,
        gone=f"task-title: task {task_id} no longer exists — nothing to retitle", logger=log,
        call=lambda _org: refine_title(task_id, authored), fallback=False,
    )


@DBOS.workflow()
def generate_task_title(task_id: int, authored: str | None = None) -> None:
    """One durable title for one created task.

    Imported by `app/durable/runtime.py` for its decorator side effect — an unregistered workflow cannot
    be enqueued or recovered. Only the id travels; body and current title are re-read, so a recovered run
    declines if an operator renamed the task meanwhile.
    """
    refine(task_id, authored)


def enqueue_title(task_id: int, authored: str | None = None) -> bool:
    """Enqueue one refinement. True when this call is the one that placed it. `authored`: a filer's title.

    Never raises into the create path: the task is already committed, so a failure is one warning and a
    False, and the row keeps its placeholder title.
    """
    return enqueue_or_warn(
        TITLE_QUEUE, title_workflow_id(task_id, authored), generate_task_title, task_id, authored, log=log,
        failure=f"task-title: could not enqueue the refinement for task {task_id}",
        consequence="the row keeps the title it was created with",
    )


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5.0)
def owed_step() -> list[tuple[int, str]]:
    """`app/task_title.py::filed_titles_owed` as a checkpointed, retried step (a database fault retries)."""
    from app.task_title import filed_titles_owed

    return filed_titles_owed()


@DBOS.workflow()
def sweep_filed_titles() -> None:
    """One pass of `owed_step`, asked once per API start, enqueueing one refinement per owed title.
    Each refinement's ID is keyed on the task and title, so two replicas' passes cost one vendor call per draft."""
    owed = owed_step()
    for task_id, title in owed:
        enqueue_title(task_id, title)
    if owed:
        log.info("task-title: sweep enqueued %d filed title(s)", len(owed))


def request_filed_title_sweep() -> bool:
    """Ask for one sweep from the api's startup. Never raises."""
    from app.entity_shape import new_uuid7

    return enqueue_or_warn(
        TITLE_QUEUE, f"task-title-sweep:{new_uuid7()}", sweep_filed_titles, log=log,
        failure="task-title: could not enqueue the filed-title sweep",
        consequence="filed drafts keep their titles until the next start",
    )
