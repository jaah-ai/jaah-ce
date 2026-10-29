"""The one workflow every finished Task runs: the Jira done comment, the auto re-tag, the epic fan-in, the
support-ticket links and the compliance seal's start, on the durable engine (`app/durable/runtime.py`).
No task notification step belongs here (Slack cards are `app/durable/task_notify_queue.py`'s);
the support leg only hands the tickets' announcements to `app/durable/support_notify_queue.py`.

Enqueued inside the `done` write's own transaction (`app/events.py::emit_console_event` →
`enqueue_in_transaction`), the one chokepoint every guarded `done` writer passes through, so a committed `done`
always has its run and a rolled-back one has none. The inline fan-in in
`routes/tasks/epics.py` and `scan_active_epics_for_rollup` remain. `tasks.done_workflow_at` is stamped only by
`mark_converged` and is the sole admission predicate (`unconverged_actions`). DBOS retries bound one
workflow; the per-action and per-row attempt counters bound how many workflows a bad row costs.
`external_comment` and `retag` run with `max_attempts=1`; the step order is fixed for DBOS replay.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from dbos import DBOS

from app import edition_features
from app.durable import runtime

log = logging.getLogger("cockpit-task-done")

WORKFLOW_NAME = "task_done"

# How many workflows a row that keeps failing may cost before it is given up on
# (`tasks.done_workflow_attempts`; charged by `mark_converged`, read by `_admission`). Operator-editable
# (`app/operator_settings/`); the import is deferred because this module is imported at DBOS
# registration time, before the app is wired.
def max_attempts(*, org: UUID) -> int:
    """The live per-row workflow give-up bound for `org` (the task's own; the knob is org-tier), read per
    call. The caller reads the org off the task row."""
    from app.operator_settings import reads as settings_reads

    return settings_reads.get_int("task_done_workflow_max_attempts", org=org)

# ── The status vocabulary every step answers in ───────────────────────────────────────────────────────
# A closed set: `mark_converged` decides on it, and a typo could stamp a task converged with an action
# still waiting for its knob.
DONE = "done"            # ran now, succeeded
ALREADY = "already"      # this action's own marker said it had already run
SKIPPED = "skipped"      # not applicable to this task at all
DISABLED = "disabled"    # an operator knob is off — converges anyway (see `unconverged_actions`)
EXHAUSTED = "exhausted"  # the per-task attempt counter is spent; given up on permanently

STATUSES = (DONE, ALREADY, SKIPPED, DISABLED, EXHAUSTED)

# A step that exhausted its DBOS retry ladder is recorded as `failed: <reason>` (the reason is the useful
# half); `unconverged_actions` matches on this prefix.
_FAILED_PREFIX = "failed: "

# Failure-reason length cap: persisted in DBOS workflow output and logged (see `_failure_reason`).
_REASON_MAX = 300


# --------------------------------------------------------------------------- steps
# Every step is synchronous (on a DBOS pool thread), opens its own session, and defers its imports (this
# module is imported at DBOS registration time). None touches the node bus.


@DBOS.step(retries_allowed=True, max_attempts=1)
def external_comment(task_id: int) -> str:
    """Post the done comment on the Jira issue this task came from.

    `max_attempts=1`: the per-task allowance is meant to span days, and the success marker is committed
    after the network call, so a retry could post twice. The retry is a later offer of the row.

    The org bind, the pool release before the delegate's Jira POST (15s budget) and the vanished-row
    `"skipped"` (a `failed:` status would withhold convergence forever) are `runtime.delegate_for_task`'s."""
    from app.task_external_comment import comment_on_done_task

    return runtime.delegate_for_task(
        "durable:task_done.external_comment", task_id,
        gone=f"task-done: task {task_id} no longer exists — nothing to comment on", logger=log,
        call=lambda org: comment_on_done_task(task_id, org=org), fallback="skipped",
    )


@DBOS.step(retries_allowed=True, max_attempts=1)
def retag(task_id: int) -> str:
    """Classify the finished task into every active tag group, replacing its tags.

    The most expensive step (one ~20s billable classifier call); `task_retag` raises without spending an
    attempt on a deployment-wide 409/503. `max_attempts=1` and the org bind follow `external_comment`."""
    from app.task_retag import retag_done_task

    return runtime.delegate_for_task(
        "durable:task_done.retag", task_id,
        gone=f"task-done: task {task_id} no longer exists — nothing to re-tag", logger=log,
        call=lambda org: retag_done_task(task_id, org=org), fallback="skipped",
    )


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5.0)
def epic_fan_in(task_id: int) -> str:
    """Publish the epic's final report and roll the epic up — as ONE step, deliberately.

    `_publish_epic_final_report` flushes into the transaction `_roll_up_epic` commits (on every branch),
    so a checkpoint between them would leave the report uncommitted. A no-op without an epic parent; the
    other call site is the inline fast path in `routes/tasks/`.

    Lock order: the publisher takes the epic `FOR UPDATE` and the insert takes `FOR KEY SHARE` on the
    child, so never hold a child and reach for its epic. This step starts holding nothing.
    """
    from app.db import SessionLocal
    from app.models.tasks import Task
    from app.routes.tasks.epics import _maybe_roll_up_parent
    from app.tenancy import seam

    with SessionLocal() as session:
        # The org comes from the task via `bind_from`, not `operator_scope()` (which would let one task's
        # fan-in roll up every org's epics). A region, not `bind()`: a `ContextVar.set()` on a pool thread
        # dies with the thread.
        try:
            with seam.bind_from(session, Task, task_id):
                _maybe_roll_up_parent(task_id, session)
                # Covers only the no-epic-parent branch; `_roll_up_epic` commits on every other.
                session.commit()
        except seam.TenancyRowMissing:
            # A vanished child is the wrapped function's own silent no-op; letting `bind_from` raise
            # would become a `failed:` status and a permanently unconverged row.
            log.info("task-done: task %s no longer exists — nothing to roll up", task_id)
    return DONE


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5.0)
def support_links(task_id: int) -> str:
    """Mark every support ticket linked to this task as having its task done.

    The task is the operator's and each link sits in its TICKET'S org, so the links are found fleet-wide
    by id alone (`support_access.links_for_task`), then each ticket is entered in its own org, on a fresh
    session, to stamp `done_at` on the links not yet stamped. `done_at` is this action's own marker
    (like `external_done_comment_at` for Jira): a re-run finds nothing unstamped and answers `ALREADY`.
    `SKIPPED` when nothing links the task. The announcement is not here: a step cannot start a workflow
    (DBOS refuses a child enqueue outside a workflow body), so `task_done` enqueues what
    `support_announcements` returns. `SKIPPED` too in an edition without the Help assistant."""
    if not edition_features.enabled("support_assistant"):
        return SKIPPED
    from sqlalchemy import select

    from app import support_access
    from app.db import SessionLocal
    from app.models.support import Ticket, TicketTaskLink
    from app.support_activity import now
    from app.tenancy import exclusion_log, seam

    with exclusion_log.caller("durable:task_done.support_links"):
        links = support_access.links_for_task(task_id)
        if not links:
            return SKIPPED
        stamped = 0
        for ticket_id in dict.fromkeys(ticket_id for _link, ticket_id in links):
            with SessionLocal() as session:
                try:
                    with seam.bind_from(session, Ticket, ticket_id):
                        rows = session.scalars(
                            select(TicketTaskLink)
                            .where(
                                TicketTaskLink.ticket_id == ticket_id,
                                TicketTaskLink.task_id == task_id,
                                TicketTaskLink.done_at.is_(None),
                            )
                            .with_for_update()
                        ).all()
                        if rows:
                            at = now(session)
                            for link in rows:
                                link.done_at = at
                            session.commit()
                            stamped += len(rows)
                except seam.TenancyRowMissing:
                    log.info("task-done: ticket %s no longer exists — its link to task %s is gone", ticket_id, task_id)
    return DONE if stamped else ALREADY


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5.0)
def support_announcements(task_id: int) -> list[list[Any]]:
    """`[[ticket_id, link_id, operator_id], …]` — every operator owed word of each of this task's links
    that is done and not yet acknowledged. Ids only, read fleet-wide (`support_access`).

    Every such link, not only the ones this run stamped: the deliveries are keyed on the link and the
    recipient (`support_notify_queue.workflow_id`), so re-offering one already placed is an attach, and a
    run that stamped some links and then failed announces them when it is re-driven."""
    from app import support_access
    from app.tenancy import exclusion_log

    with exclusion_log.caller("durable:task_done.support_announcements"):
        links = support_access.links_for_task(task_id, done_unacked=True)
        if not links:
            return []
        operators = support_access.operator_recipients()
        return [[ticket_id, link_id, str(operator)] for link_id, ticket_id in links for operator in operators]


def unconverged_actions(results: dict[str, str]) -> list[str]:
    """The actions still owed something, sorted — empty means the task has converged.

    A pure seam so convergence is testable without DBOS. Only `failed: …` withholds: `done_workflow_at`
    is the admission predicate, so stamping over a failure would refuse every future offer. `EXHAUSTED`
    does not withhold (permanently given up). `DISABLED` does not withhold either: the stamp is per row
    and a knob per action, so withholding would suppress the other actions too. Nothing replays a
    converged row when a knob returns.

    `epic_fan_in` has no counter, so it re-runs on every offer while failing (as the rollup rescan does);
    the row counter (`max_attempts()`) keeps that finite.
    """
    return sorted(
        name for name, status in results.items()
        if status.startswith(_FAILED_PREFIX)
    )


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5.0)
def mark_converged(task_id: int, results: dict[str, str]) -> bool:
    """Settle `done_workflow_at` for this run: stamp it when nothing is outstanding, CLEAR it when
    something is. Returns whether it stamped.

    Retryable, since `workflow_id_for` keys on the counter only this step writes. Known residual: a run
    that goes terminal without reaching here (ladder exhausted, `MAX_RECOVERY_ATTEMPTS_EXCEEDED`, operator
    cancel) wedges the task, as later events attach to the dead run. Manual remedy: set
    `tasks.done_workflow_attempts` to an unused value below `max_attempts()`. Do not add a liveness read
    to `enqueue_in_transaction`. A process dying before this step stays `PENDING` and recovers under the same id.

    Pins `updated_at` for `app/task_retag.py::mark_task_columns`'s reason, stamping no actor (allowlisted by the
    test that pins every task-state write site).
    """
    from sqlalchemy import func, update

    from app.db import SessionLocal
    from app.models.tasks import Task
    from app.tenancy import exclusion_log, seam

    withheld = unconverged_actions(results)
    # Attributed, as in `external_comment`; covers both arms, each with its own bind.
    with exclusion_log.caller("durable:task_done.mark_converged"):
        if withheld:
            # Cleared, not left alone: a failed step on a re-offered, stamped row must clear the stamp. The
            # attempt is charged in the same unguarded statement, bounding rows with no per-action counter.
            # The org is bound via `bind_from` (not `operator_scope()`); the read seam ignores DML, but it
            # keeps every durable site bound for anything this region calls.
            with SessionLocal() as session:
                try:
                    with seam.bind_from(session, Task, task_id) as org:
                        session.execute(
                            update(Task)
                            .where(Task.id == task_id)
                            .values(
                                updated_at=Task.updated_at,
                                done_workflow_at=None,
                                done_workflow_attempts=Task.done_workflow_attempts + 1,
                            )
                        )
                        session.commit()
                except seam.TenancyRowMissing:
                    # A task deleted mid-flight is a no-op: the UPDATE would match nothing. Swallowed only
                    # for a deleted row (no row, no `done` event, no wedge); every other fault must reach the
                    # retry ladder, since a failed run here wedges the id.
                    log.info(
                        "task-done: task %s no longer exists — nothing to unstamp; %s was left outstanding",
                        task_id, ", ".join(withheld),
                    )
                    return False
            log.info(
                "task-done: task %s not converged — %s still outstanding; this row is re-driven the next "
                "time it is offered, until they settle or it reaches %d attempts",
                task_id, ", ".join(withheld), max_attempts(org=org),
            )
            return False
        # The stamping arm binds separately: the arms are exclusive and each opens its own session, which
        # `bind_from` derives from.
        with SessionLocal() as session:
            try:
                with seam.bind_from(session, Task, task_id):
                    session.execute(
                        update(Task)
                        .where(Task.id == task_id)
                        .values(updated_at=Task.updated_at, done_workflow_at=func.now())
                    )
                    session.commit()
            except seam.TenancyRowMissing:
                # Same disposition. False is literally true (no stamp); nothing will re-drive a deleted row.
                log.info(
                    "task-done: task %s no longer exists — every action converged, but there is no row "
                    "left to stamp", task_id,
                )
                return False
        return True


def compliance_seal(task_id: int) -> str:
    """Start the task's compliance seal (`app/durable/compliance_seal.py`) and return — never wait for it here.

    Not a step: a step may not start a workflow, so this runs in the workflow body like the support enqueue, and
    its one read is the `seal_owed` step (so a replay enqueues the same id, an attach). `SKIPPED` when nothing is
    owed (not done, archived, or a manifest already exists); a failed enqueue raises into `failed: …`, and the
    `compliance-seal-sweep` cron is the backstop either way. `SKIPPED` too in an edition without compliance."""
    if not edition_features.enabled("compliance"):
        return SKIPPED
    from app.durable import compliance_seal as seal

    seq = seal.seal_owed(task_id)
    if seq is None:
        return SKIPPED
    seal.enqueue_seal(task_id, seq)
    return DONE


# --------------------------------------------------------------------------- the workflow

# The order is the contract (module docstring); a tuple keeps the loop one deterministic walk for replay.
_ACTIONS: tuple[tuple[str, Any], ...] = (
    ("external_comment", external_comment),
    ("retag", retag),
    ("epic_fan_in", epic_fan_in),
    ("support_links", support_links),
    # Last: it only enqueues the seal, which waits for transcripts on its own queue.
    ("compliance_seal", compliance_seal),
)


def _failure_reason(exc: BaseException) -> str:
    """The message worth recording for a step that escaped, unwrapped and length-bounded.

    Unwrapped because an exhausted step raises DBOS's `DBOSMaxStepRetriesExceeded`, whose real causes are
    on `.errors`; falls back to `__cause__`/`__context__`. Bounded because it is persisted and an
    exception's `str()` (e.g. SQLAlchemy's `StatementError`) may append statements and parameters.
    """
    # The sequence test belongs in the condition: `pydantic.ValidationError.errors` is a truthy bound
    # method, which would otherwise take the branch and skip the chained cause.
    inner = getattr(exc, "errors", None)
    if isinstance(inner, (list, tuple)) and inner:
        exc = inner[-1]
    # `__context__` too: a bare `raise` inside an `except` sets only that.
    elif exc.__cause__ is not None:
        exc = exc.__cause__
    elif exc.__context__ is not None:
        exc = exc.__context__
    reason = f"{type(exc).__name__}: {exc}"
    # Marked when truncated (cf. `app/workflow_engine.py::_payload`): also rendered on the Workflows page.
    if len(reason) <= _REASON_MAX:
        return reason
    return f"{reason[:_REASON_MAX]}… (truncated)"


@DBOS.workflow()
def task_done(task_id: int) -> dict[str, str]:
    """Run every action this finished task is owed, then record whether it converged.

    Each step is wrapped so one action's permanent failure never blocks another: an exhausted step is
    caught as `failed: <reason>`, the rest still run, and `mark_converged` withholds the stamp so a later
    offer retries only the failed one. Broad `Exception`, since an unexpected fault is still this action's.
    Returns the per-action status map, the run's own record.
    """
    results: dict[str, str] = {}
    for name, action in _ACTIONS:
        try:
            results[name] = action(task_id)
        except Exception as exc:
            # The reason, not the traceback; the action modules' messages never quote bodies or credentialed URLs.
            results[name] = f"{_FAILED_PREFIX}{_failure_reason(exc)}"
            log.warning("task-done: task %s — %s failed: %s", task_id, name, results[name])
    # Enqueued from the workflow body because a step may not start a workflow; the ids make a replay an
    # attach. Only after the stamping step answered, so a failed stamp announces nothing.
    # Best-effort, and never in the way of `mark_converged`: the bell and the list marker derive from
    # `done_at`, so a lost DM costs the Slack half only.
    if results.get("support_links") in (DONE, ALREADY):
        # Never without the Help assistant, whose `support_links` skips: the queue does not ship there.
        if edition_features.enabled("support_assistant"):
            from app.durable import support_notify_queue

            try:
                owed = support_announcements(task_id)
            except Exception as exc:
                owed = []
                log.warning("task-done: task %s — support announcements not read: %s", task_id, _failure_reason(exc))
            for ticket_id, link_id, operator in owed:
                support_notify_queue.enqueue_delivery(ticket_id, "task_done", link_id, operator)
    converged = mark_converged(task_id, results)
    log.info(
        "task-done: task %s %s — %s", task_id,
        "converged" if converged else "left pending",
        ", ".join(f"{name}={status}" for name, status in results.items()),
    )
    return results


# --------------------------------------------------------------------------- starting one
#
# The expensive leg is a multi-second vendor call (the ~20s classifier) that a burst of settles can queue up.
DONE_QUEUE = runtime.declare_queue("task-done", **runtime.VENDOR_QUEUE)


def workflow_id_for(task_id: int, attempt: int) -> str:
    """This run's durable identity: the task, and WHICH ATTEMPT at it this is.

    Two constraints: a bare `task-done-<id>` would make a legitimate second run impossible (the id would
    collide with the finished run), and a per-call id (e.g. a clock) would let a re-emitted `done` admit a
    second run beside one in flight, double-billing and double-commenting. `tasks.done_workflow_attempts`
    moves only when a run left something owed, so every `done` of one spell computes the same id, and a
    re-offer after a charge mints a fresh one. A pure function; this id is the exclusion (see
    `enqueue_in_transaction`).
    """
    return f"task-done-{task_id}-{attempt}"


_UNSTAMPED = object()


def _admission(session: Any, task_id: int) -> Optional[int]:
    """The attempt number this task is owed, or None when it owes nothing. ONE read, deciding BOTH.

    One query so a concurrent `mark_converged` cannot advance the counter between admission and identity.
    Terms:

      * `state = 'done'` — a task can be reopened before the write commits.
      * `archived_at IS NULL` — an archived task was superseded, not done.
      * `done_workflow_at IS NULL` — convergence; makes a re-emitted `done` event (any later edit) free.
      * `done_workflow_attempts < max_attempts()` — the row's give-up bound.

    Reads on the writer's own session, so it sees the `done` it is about to commit.
    """
    from contextlib import nullcontext

    from sqlalchemy import select

    from app.models.tasks import Task
    from app.task_lifecycle import DONE as DONE_STATE
    from app.tenancy import exclusion_log, seam

    # The writer's scope already decides what it sees when it is the task's org or the operator plane, and
    # `bind_from` refuses to re-point a deciding scope; only an unbound writer (whose ORM SELECT would read
    # `false()`, silently stopping every done-action fleet-wide) derives the org from the row.
    scope = seam.current_scope()
    deciding = scope is seam.OPERATOR or isinstance(scope, UUID)
    # The derived org's read stamps the writer's session (`seam.stamp_scope`), and a savepoint does not undo
    # `session.info`: put the stamp back, or the writer's next unit on this session would conflict with it.
    stamp = session.info.get(seam.SESSION_SCOPE_KEY, _UNSTAMPED)
    # Attributed, as in `external_comment`; the region also covers the `max_attempts()` read.
    with exclusion_log.caller("durable:task_done._admission"):
        try:
            with nullcontext() if deciding else seam.bind_from(session, Task, task_id):
                row = session.execute(
                    select(
                        Task.org_id,
                        Task.state,
                        Task.archived_at,
                        Task.done_workflow_at,
                        Task.done_workflow_attempts,
                    ).where(Task.id == task_id)
                ).one_or_none()
                if row is None:
                    return None
                org, state, archived_at, converged_at, attempts = row
                if state != DONE_STATE or archived_at is not None or converged_at is not None:
                    return None
                attempts = attempts or 0
                return attempts if attempts < max_attempts(org=org) else None
        except seam.TenancyRowMissing:
            return None
        finally:
            if not deciding:
                if stamp is _UNSTAMPED:
                    session.info.pop(seam.SESSION_SCOPE_KEY, None)
                else:
                    session.info[seam.SESSION_SCOPE_KEY] = stamp


def enqueue_in_transaction(session: Any, task_id: Any) -> None:
    """Enqueue one run for `task_id` INSIDE the `done` write's transaction (`app/events.py::emit_console_event`,
    which every `done` writer passes through): the run exists iff the `done` commits. NEVER RAISES.

    The workflow id is the only exclusion: a re-emitted `done` while a run of the same attempt exists computes the
    same id (`workflow_id_for`), and the engine leaves that row unchanged. Any failure rolls back only the
    savepoint, is logged, and costs this task its done-actions, never its `done`.
    """
    # Flushed OUTSIDE the guard: `begin_nested` autoflushes before its SAVEPOINT, and a failure of the writer's own
    # pending state is the writer's error to see, not a lost enqueue.
    session.flush()
    try:
        task_id = int(task_id)
        with session.begin_nested():
            attempt = _admission(session, task_id)
            if attempt is None:
                return
            runtime.enqueue_in_transaction(
                session, DONE_QUEUE, workflow_id_for(task_id, attempt), WORKFLOW_NAME, task_id
            )
    except Exception as exc:  # noqa: BLE001 — see the docstring: a follow-up never fails its cause
        log.warning(
            "task-done: task %s — the done workflow could not be enqueued (%s: %s); its done-actions stay "
            "unpaid until the task is edited again",
            task_id, type(exc).__name__, exc,
        )
