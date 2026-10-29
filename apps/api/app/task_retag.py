"""Re-tag orchestration: the manual route's body, and the step that runs it when a Task reaches `done`.

`retag_task` is what both `POST /api/tasks/{id}/retag` and the done workflow's `retag` step run: one
classifier, lock, writer (`app/task_tags.py::replace_task_tags`) and catalogue re-read. It is not a hook
at each `done` transition because a ~20s billable LLM call would delay or fail the transition; it is a
step of `app/durable/task_done.py`, and `tasks.auto_retagged_at IS NULL` makes a rerun free.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.operator_settings import reads as settings_reads
from app.db import SessionLocal
from app.events import emit_console_event
from app.models.tasks import Task
from app.entity_shape import get_or_404
from app.task_classifier import (
    TaskDocument,
    active_groups,
    budget_for_attempt,
    catalogue_identity,
    classify_task_tags,
)
from app.task_lifecycle import DONE
from app.task_tags import TagRefOut, replace_task_tags

log = logging.getLogger("cockpit-task-retag")

# No scheduling constants: the done workflow is enqueued by the `done` write (`app/durable/task_done.py`).

# The on/off operator knob (`app/operator_settings/`), read per step.
SETTING_KEY = "task_auto_retag_on_done"


def max_attempts(*, org: UUID) -> int:
    """The live per-task give-up bound for `org` (the task's own; both knobs here are org-tier), read per
    step. A cross-run bound on how many workflows a permanently-bad task costs (each attempt is billable),
    distinct from DBOS's per-workflow retries. The manual Re-tag button stays unlimited. Operator-editable
    (`app/operator_settings/`)."""
    return settings_reads.get_int("task_retag_max_attempts", org=org)

# Refusals about the deployment, not the task: 503 = no model key, 409 = no active tag group with tags.
# They spend no attempt; the step raises so DBOS retries later with every task's allowance intact.
_DEPLOYMENT_REFUSALS = frozenset({409, 503})

# The actor the log line names for the done workflow; the route passes the acting user's id.
_WORKFLOW_ACTOR = "the done workflow"


def retag_task(session: Session, task: Task, *, actor: str, attempt: int = 1) -> list[TagRefOut]:
    """Classify `task` and REPLACE its tags across every active group. Does NOT commit — the caller does.

    `attempt` (1-based) sizes the reply budget only (`task_classifier.budget_for_attempt`).

    Nothing is held across the LLM call: `session.rollback()` releases the connection so concurrent
    Re-tags cannot starve the fixed pool (`app/db.py`). It also expires ORM instances and discards any
    pending work on `session`, hence the `TaskDocument` snapshot and id read beforehand.

    The row lock is still mandatory for the write: two concurrent replaces would otherwise commit the
    union of their selections under READ COMMITTED. Every failure raises before the write."""
    task_id = task.id
    groups = active_groups(session)
    document = TaskDocument.from_task(task)
    session.rollback()
    tag_ids = classify_task_tags(document, groups, max_tokens=budget_for_attempt(attempt))
    locked = get_or_404(session, Task, task_id, detail=f"task {task_id} not found", lock=True)
    # Narrowed, not closed: a group published between this read and the delete in `replace_task_tags`
    # still loses its rows (the lock is on `tasks`, not `tag_groups`). Closing it needs `FOR SHARE` or
    # REPEATABLE READ; a sub-millisecond, Re-tag-recoverable loss did not earn either.
    if catalogue_identity(active_groups(session)) != catalogue_identity(groups):
        raise HTTPException(
            status_code=409,
            detail=(
                "the tag catalogue changed while this task was being classified — "
                "nothing was written, press Re-tag again"
            ),
        )
    # No ActivityEvent (`record_transition=False`), but this ships task text to a third party, so which
    # task and by whom is logged here, the provider and model by the classifier. Never the prompt.
    log.info("retag: task %s classified for %s (tags=%d)", task_id, actor, len(tag_ids))
    tags = replace_task_tags(session, locked, tag_ids)
    emit_console_event(
        session, entity="task", id=task_id, action="tagged", record_transition=False
    )
    return tags


# No worklist: applicability (marker, attempt counter, `hidden`) is asked once by `retag_done_task`.


def mark_task_columns(session: Session, task_id: int, **values) -> None:
    """Write this action's own bookkeeping columns WITHOUT moving `tasks.updated_at`.

    A tag write must never stamp the task: the web edit form is keyed on `id:updated_at` and a bumped
    stamp remounts it, discarding the operator's typing. Assigning the column its own value beats the
    model's `onupdate`. For the same reason it is the one `update(Task)` that must not carry
    `actors.task_actor_values(...)` (allowlisted in the task-state write-site test)."""
    session.execute(
        update(Task).where(Task.id == task_id).values(updated_at=Task.updated_at, **values)
    )


def retag_done_task(task_id: int, *, org: UUID) -> str:
    """Classify ONE finished task in its own transaction. The body of the workflow's `retag` step.

    `org` is the task's own (the step derives it with `bind_from`): both knobs are read for it, before
    a session is opened.

    Returns the done workflow's status vocabulary (`app/durable/task_done.py`): `"done"`, `"already"`
    (marker set), `"skipped"` (gone, not `done`, hidden, archived, or no active tag group — re-checked since
    the workflow may run long after the event), `"disabled"` (knob off; converges, and nothing re-offers the row
    when the knob returns — see `task_done.py::unconverged_actions`) or `"exhausted"` (`max_attempts()` spent).

    A transient failure raises so the DBOS step retries; `"exhausted"` is a permanent give-up and does
    not. The attempt counter is committed in its own transaction before the raise, so a process dying
    mid-retry cannot lose the accounting and re-bill a bad task forever."""
    # First, before a session is opened: an off knob must cost this row nothing.
    if not settings_reads.get_bool(SETTING_KEY, org=org):
        return "disabled"
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task is None or task.state != DONE:
            # Vanished, or moved on since admission.
            return "skipped"
        if task.hidden or task.archived_at is not None:
            # Checked per task because this caller is not a worklist
            # (`app/durable/task_done.py::_admission`).
            return "skipped"
        # The stamp is the idempotency: replays, re-offers and retries never re-classify.
        if task.auto_retagged_at is not None:
            return "already"
        # An install with no published tag group (every fresh community edition) has nothing to classify
        # into: skip without the 409 round trip and its log line on every finished task.
        if not active_groups(session):
            return "skipped"
        ceiling = max_attempts(org=org)
        if task.auto_retag_attempts >= ceiling:
            return "exhausted"
        # Read now: the failure arm moves the counter and the log names this attempt.
        attempt = task.auto_retag_attempts + 1
        try:
            retag_task(session, task, actor=_WORKFLOW_ACTOR, attempt=attempt)
        except Exception as exc:
            session.rollback()
            if isinstance(exc, HTTPException) and exc.status_code in _DEPLOYMENT_REFUSALS:
                log.info(
                    "task-retag: task %s deferred — classification is unavailable on this deployment "
                    "(%s: %s); no attempt spent",
                    task_id, exc.status_code, exc.detail,
                )
                raise
            # Every other failure, an unexpected type included, charges the counter: a failed step withholds
            # convergence (`task_done.py::unconverged_actions`), so without a charge the row would be
            # re-classified on every offer forever. Only `_DEPLOYMENT_REFUSALS` legitimately spends no attempt.
            mark_task_columns(
                session, task_id,
                auto_retag_attempts=Task.auto_retag_attempts + 1,
            )
            session.commit()
            reason = (
                f"{exc.status_code}: {exc.detail}" if isinstance(exc, HTTPException)
                else f"{type(exc).__name__}: {exc}"
            )
            log.warning(
                "task-retag: task %s classification failed (%s) — attempt %d of %d",
                task_id, reason, attempt, ceiling,
            )
            raise
        mark_task_columns(session, task_id, auto_retagged_at=datetime.now(timezone.utc))
        session.commit()
        return "done"
