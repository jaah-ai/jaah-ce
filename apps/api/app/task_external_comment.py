"""The done comment: a Task linked to a Jira issue that reaches `done` gets one comment saying so.

The row is the trigger, not a hook at each of the ~ten `done` writers: an outbound third-party call
must not delay or fail a transition (`app/task_retag.py`'s argument). Driven by the durable done
workflow (`app/durable/task_done.py`'s `external_comment` step, enqueued by the `done` write); this
module holds one task's worth of work, its knob and its cross-run give-up counter. Turning the knob on
affects only tasks that finish afterwards.

The issue's status is never touched: `jira_api.post_comment` is the sole outbound call, and
`tests/test_task_external_comment.py` asserts no transition helper is used.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session

from app import jira_api
from app.operator_settings import reads as settings_reads
from app.config import get_settings
from app.db import SessionLocal
from app.models.credentials import Credential
from app.models.projects import Project
from app.models.tasks import Task
from app.task_lifecycle import DONE
from app.task_retag import mark_task_columns

log = logging.getLogger("cockpit-task-external-comment")

# No scheduling constants: the workflow is started by the `done` event, which has no cadence.

# The operator knob (`app/operator_settings/`), read per step so a toggle needs no restart.
SETTING_KEY = "task_comment_external_on_done"


def max_attempts(*, org: UUID) -> int:
    """The live per-task give-up bound for `org` (the task's own; both knobs here are org-tier): the cross-run
    bound on how many workflows a permanently bad target (deleted or invisible issue) costs, distinct from
    DBOS's per-workflow step retries. Operator-editable (`app/operator_settings/`, where default and bounds
    live). Read once per step into a local: it is compared in Python and again in the guarded UPDATE, and
    both must see one value."""
    return settings_reads.get_int("task_external_comment_max_attempts", org=org)

# The one tracker with a client (`app/jira_api.py`); any other `external_task_system` is skipped.
SYSTEM_JIRA = "jira"

# The comment: a Done line, the task title and a link to the console task. `post_comment` renders one
# paragraph, so the Done line and title share one string.
_DONE_LINE = "The job has been done:"
_LINK_LABEL = "Open the console task"


def _comment_text(title: str) -> str:
    return f"{_DONE_LINE} {(title or '').strip()}"


def _console_task_url(task_id: int) -> str:
    """The operator-facing URL for a task, from `FRONTEND_URL` (`app/config.py`) plus the web route `/tasks/:id`.

    No separate env var: a second spelling of the console origin is a second thing to misconfigure.
    """
    return f"{get_settings().frontend_url.rstrip('/')}/tasks/{task_id}"


# No worklist here: `comment_on_done_task` asks about the one task its workflow was started for.


def _jira_site(session: Session, task: Task) -> jira_api.JiraSite | None:
    """The Jira site `task`'s project points at, or None when Jira is not usable for it.

    Resolved task → project → `jira_credential_id` → `Credential` fresh every run, so a rotated token
    applies without restart. None is a configuration condition and spends no attempt. Logged without any
    part of the row.
    """
    project = session.get(Project, task.project_id)
    credential_id = getattr(project, "jira_credential_id", None)
    if credential_id is None:
        log.info(
            "task-external-comment: project %s has no Jira credential — task %s skipped, no attempt "
            "spent", task.project_id, task.id,
        )
        return None
    credential = session.get(Credential, credential_id)
    if credential is None:
        log.warning(
            "task-external-comment: project %s names credential %s, which no longer exists — task %s "
            "skipped, no attempt spent", task.project_id, credential_id, task.id,
        )
        return None
    try:
        return jira_api.site_from_credential(credential)
    except jira_api.JiraCredentialInvalid as exc:
        log.warning(
            "task-external-comment: project %s Jira credential is unusable (%s) — task %s skipped, no "
            "attempt spent", task.project_id, exc, task.id,
        )
        return None


def _no_usable_site(session: Session, task_id: int, task: Task) -> str:
    """What to do when `_jira_site` said no; the answer depends on which no.

    Neither posts anything. No credential named (or the row deleted): `"skipped"`, nothing charged, so an
    unwired project does not burn attempts before it is wired up.

    A credential that exists but is unusable: `"skipped"` with one attempt charged. Every later edit of the
    finished task re-offers the row, so uncharged it would never retire; the charge lets
    `comment_on_done_task`'s cap check answer `"exhausted"`. This action's own counter, not the shared
    `done_workflow_attempts`, so one broken integration cannot exhaust the epic fan-in's allowance.
    """
    if task.project_id is None:
        return "skipped"
    project = session.get(Project, task.project_id)
    if project is None or getattr(project, "jira_credential_id", None) is None:
        return "skipped"
    mark_task_columns(
        session, task_id,
        external_done_comment_attempts=Task.external_done_comment_attempts + 1,
    )
    session.commit()
    return "skipped"


def comment_on_done_task(task_id: int, *, org: UUID) -> str:
    """Comment on one finished task in its own transaction. The body of the workflow's first step.

    `org` is the task's own (the step derives it with `bind_from`): both knobs are read for it, before
    a session is opened.

    Returns the done workflow's status vocabulary (`app/durable/task_done.py`): `"done"`, `"already"`,
    `"skipped"` (no commentable Jira issue), `"disabled"` (knob off; converges, see
    `task_done.unconverged_actions`, and nothing re-offers it later) or `"exhausted"` (cap spent).

    A transient failure raises, so the DBOS step retries; `"exhausted"` does not. The attempt counter is
    committed before the raise so a crash mid-ladder keeps the accounting.

    `session.rollback()` before the network call is load-bearing: the whole-app pool ceiling (`app/db.py`)
    must not be pinned across a 15s call. It expires ORM instances, so needed values are read before it.
    """
    # First, before a session is opened: an off knob costs nothing.
    if not settings_reads.get_bool(SETTING_KEY, org=org):
        return "disabled"
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task is None or task.state != DONE:
            # Vanished or moved on since admission.
            return "skipped"
        if task.archived_at is not None:
            # Archived while the workflow was in flight (`task_done._admission` filters it at admission). An archived
            # decomposition was superseded, not done, and a Jira comment cannot be taken back, so re-check here.
            return "skipped"
        # The stamp is the idempotency: replays, re-offers and concurrent retries never comment twice.
        if task.external_done_comment_at is not None:
            return "already"
        # One read: the Python compare and the guarded UPDATE below must use the same ceiling.
        ceiling = max_attempts(org=org)
        if task.external_done_comment_attempts >= ceiling:
            return "exhausted"
        if not task.external_task_id:
            # Not an error and not logged: every finished task reaches here, most unlinked.
            return "skipped"
        if (task.external_task_system or "") != SYSTEM_JIRA:
            log.info(
                "task-external-comment: task %s names tracker %r, for which there is no client — "
                "skipped, no attempt spent", task_id, task.external_task_system,
            )
            return "skipped"
        site = _jira_site(session, task)
        if site is None:
            return _no_usable_site(session, task_id, task)
        issue_key = task.external_task_id
        text = _comment_text(task.title)
        attempt = task.external_done_comment_attempts + 1

        # Charge the attempt before the call, not after it fails: if `post_comment` succeeds and the recording
        # commit then fails (e.g. a pool timeout after the rollback) or the process dies, the next run would post
        # again. So the counter means "outbound calls made", bounding posts at `max_attempts()`.
        # The charge is also the claim: re-testing the cap inside the UPDATE makes it a compare-and-swap, so
        # racing runs post at most `ceiling` times. `rowcount == 0` means another run took the last attempt:
        # `"exhausted"`, no post.
        charged = session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.external_done_comment_attempts < ceiling,
            )
            .values(
                updated_at=Task.updated_at,
                external_done_comment_attempts=Task.external_done_comment_attempts + 1,
            )
        )
        session.commit()
        if charged.rowcount == 0:
            log.info(
                "task-external-comment: task %s — another pass took the last of its %d attempts; "
                "not commenting", task_id, ceiling,
            )
            return "exhausted"
        session.rollback()
        try:
            jira_api.post_comment(
                site, issue_key, text=text, link_label=_LINK_LABEL,
                link_url=_console_task_url(task_id),
            )
        except Exception as exc:
            # Already counted; report and re-raise. The message is `jira_api`'s (a 404 means missing or invisible).
            log.warning(
                "task-external-comment: task %s — commenting on %s failed (%s); attempt %d of %d",
                task_id, issue_key, exc, attempt, ceiling,
            )
            raise
        # The stamp is the idempotency: one comment per task, ever.
        mark_task_columns(session, task_id, external_done_comment_at=datetime.now(timezone.utc))
        session.commit()
        log.info("task-external-comment: task %s commented on %s", task_id, issue_key)
        return "done"
