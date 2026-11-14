"""What a tracker task tells its issue: the write-back sweep (run by every `trigger-intake` pass, `run_pass`).

For each task with a `tracker_issue_links` row that moved recently, the sweep reads what happened and posts what the
issue has not been told yet, oldest first, through the tracker's adapter:

    ack          the task was linked                       adapter.ack (skipped for an intake link on an `ack_sync`
                                                           adapter: the ingress acknowledged it inside the request,
                                                           `intake.record_ack`)
    progress     a transition to `active` / `awaiting_response` (`activity_events` `task:<state>`; ref = its id) —
                 each one but an opening start (the ack said it), so a task relaunched after an answer says
                 "Started work." again
    question     each `task_questions` round still open    (ref = its id)
    pr_opened    each `task_prs` row                       (ref = its id)
    outcome      the task reached `done` / `system_failed` (ref = the state)
    done         the task's PR merged (`task_outcomes.merged_at`): adapter.mark_done with the connection's done state,
                 owed only once no other task linked to the issue is still at work (re-read each pass)

A link of either origin (`tracker_issue_links.origin`: started by the intake, or picked on the task form) is told the
same things from `link.created_at` on, once it has started: a draft is owed nothing. A link whose `writeback_scope` is `outcome` (an issue linked to a task that had
already finished) is owed the outcome only. Relinking a task deletes its `tracker_writebacks` rows
(`project_links.link_task`), so the new issue hears from the start.

Once, fleet-wide: a send first claims its `tracker_writebacks(task_id, kind, ref)` row and commits it; only the sweep
whose insert lands calls the adapter, so two sweeps (two replicas, a pass and a nudge) never post twice. A failed
call gives the claim back (the row is deleted) and logs a `skipped` delivery on the connection's trigger, keyed
`writeback:<task>:<kind>:<ref>`: those rows are the retry's backoff clock (one minute, doubling, at most an hour) and
show in the connection's Events. After `MAX_ATTEMPTS` failures the claim is kept and the write-back abandoned. A
process that dies between the claim and the call, or a call that times out (it may have landed), keeps its claim:
at most once, never twice. Nothing here
ever blocks or fails the task.

Why a step of `trigger-intake` and not a cron of its own: a system cron is a seeded row, and the intake pass already
runs every minute and is woken as soon as a tracker start is received — the pass that files the task acknowledges it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models.audit import ActivityEvent
from app.models.automations import TriggerDelivery
from app.models.outcomes import TaskOutcome
from app.models.task_extras import TaskPr
from app.models.tasks import Task, TaskQuestion
from app.models.trackers import TrackerConnection, TrackerIssueLink, TrackerWriteback
from app.task_lifecycle import ACTIVE, AWAITING_RESPONSE, DONE, DRAFT, SYSTEM_FAILED
from app.trackers import connections, intake, protocol
from app.trackers.vocabulary import (
    DEFAULT_DONE_STATE,
    ORIGIN_INTAKE,
    SCOPE_OUTCOME,
    TRACKER_LABELS,
    WRITEBACK_ACK,
    WRITEBACK_DONE,
    WRITEBACK_OUTCOME,
    WRITEBACK_PR_OPENED,
    WRITEBACK_PROGRESS,
    WRITEBACK_QUESTION,
)
from app.triggers import SKIPPED

log = logging.getLogger("tracker-writeback")

# A task that has not moved in this long, and whose PR has not merged in it, has nothing left to say.
LOOKBACK = timedelta(days=7)
# One adapter call; the sweep starts no call it could not finish inside its pass's budget.
CALL_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 8
BACKOFF_FIRST_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600
# The tracker's words for what a reason names.
_WHAT = {
    WRITEBACK_ACK: "acknowledgement", WRITEBACK_PROGRESS: "progress note", WRITEBACK_QUESTION: "question",
    WRITEBACK_PR_OPENED: "PR link", WRITEBACK_OUTCOME: "outcome", WRITEBACK_DONE: "done state",
}
_PROGRESS = {f"task:{ACTIVE}": "Started work.", f"task:{AWAITING_RESPONSE}": "Waiting for an answer."}


@dataclass(frozen=True)
class Pending:
    """One write-back the issue has not had: `markdown` for a comment, `state_name` for `done`."""
    kind: str
    ref: str
    at: datetime
    markdown: Optional[str] = None
    state_name: Optional[str] = None


def task_url(task_id: int) -> str:
    """The operator-facing URL of a task, from `FRONTEND_URL` (`app/config.py`) plus the web route `/tasks/:id`. No
    env var of its own: a second spelling of the console origin is a second thing to misconfigure."""
    from app.config import console_url

    return console_url(f"/tasks/{task_id}")


def dedup_key(task_id: int, kind: str, ref: str) -> str:
    return f"writeback:{task_id}:{kind}:{ref}"


# ── What is owed ──────────────────────────────────────────────────────────────────────────────────────


def links_with_work(session: Session, now: datetime) -> list[tuple[UUID, int]]:
    """(connection id, task id) of every tracker task that moved, was linked or had its PR merge inside `LOOKBACK`."""
    since = now - LOOKBACK
    merged_recently = select(TaskOutcome.task_id).where(TaskOutcome.merged_at >= since)
    rows = session.execute(
        select(TrackerIssueLink.connection_id, TrackerIssueLink.task_id)
        .join(Task, Task.id == TrackerIssueLink.task_id)
        .where(Task.archived_at.is_(None), or_(Task.updated_at >= since, Task.id.in_(merged_recently),
                                               TrackerIssueLink.created_at >= since))
        # Newest first: the task this very pass filed is acknowledged before older tasks' updates.
        .order_by(TrackerIssueLink.id.desc())
    ).all()
    return [(row.connection_id, row.task_id) for row in rows]


def pending_for(session: Session, conn: TrackerConnection, link: TrackerIssueLink, task: Task,
                adapter: protocol.TrackerAdapter) -> list[Pending]:
    """Everything the issue is owed and has not been sent, oldest first. Reads only."""
    sent = {(row.kind, row.ref) for row in session.execute(
        select(TrackerWriteback.kind, TrackerWriteback.ref).where(TrackerWriteback.task_id == task.id))}
    link_line = f"[Task #{task.id}]({task_url(task.id)})"
    owed: list[Pending] = []
    if task.unsaved or task.state == DRAFT:
        return []  # nothing has started: a draft linked by hand tells its issue once it is started
    if link.writeback_scope == SCOPE_OUTCOME:
        if task.state in (DONE, SYSTEM_FAILED) and (WRITEBACK_OUTCOME, task.state) not in sent:
            return [Pending(WRITEBACK_OUTCOME, task.state, task.updated_at, markdown=outcome_markdown(task, link_line))]
        return []
    if not (link.origin == ORIGIN_INTAKE and adapter.ack_sync):
        owed.append(Pending(WRITEBACK_ACK, "", link.created_at))
    first_start, previous_kind = True, None
    for event in session.scalars(select(ActivityEvent).where(
        ActivityEvent.data["id"].astext == str(task.id), ActivityEvent.data["entity"].astext == "task",
        ActivityEvent.kind.in_(tuple(_PROGRESS)), ActivityEvent.created_at >= link.created_at,
    ).order_by(ActivityEvent.id)):
        opening, first_start = first_start and event.kind == f"task:{ACTIVE}", False
        repeat, previous_kind = event.kind == previous_kind, event.kind
        if opening:
            continue  # the acknowledgement already said the task started; a relaunch after an answer still says so
        if repeat:
            continue  # one wait can record its state more than once (the ask, then answer recovery): one note per wait
        owed.append(Pending(WRITEBACK_PROGRESS, str(event.id), event.created_at,
                            markdown=f"{_PROGRESS[event.kind]} {link_line}"))
    for question in session.scalars(select(TaskQuestion).where(
        TaskQuestion.task_id == task.id, TaskQuestion.status == "open").order_by(TaskQuestion.id)):
        owed.append(Pending(WRITEBACK_QUESTION, str(question.id), question.created_at,
                            markdown=question_markdown(question, link_line)))
    for pr in session.scalars(select(TaskPr).where(TaskPr.task_id == task.id).order_by(TaskPr.id)):
        owed.append(Pending(WRITEBACK_PR_OPENED, str(pr.id), pr.opened_at,
                            markdown=f"Opened a pull request: {pr.pr_url}"))
    if task.state in (DONE, SYSTEM_FAILED):
        owed.append(Pending(WRITEBACK_OUTCOME, task.state, task.updated_at,
                            markdown=outcome_markdown(task, link_line)))
    merged_at = session.scalar(select(func.min(TaskOutcome.merged_at)).where(
        TaskOutcome.task_id == task.id, TaskOutcome.merged_at.is_not(None)))
    if merged_at is not None and not _other_task_at_work(session, link):
        state_name = connections.settings_of(conn)["done_state"] or DEFAULT_DONE_STATE
        owed.append(Pending(WRITEBACK_DONE, "", merged_at, state_name=state_name))
    # The acknowledgement first whatever the clocks say; the rest in the order they happened.
    owed.sort(key=lambda item: (item.kind != WRITEBACK_ACK, item.at))
    return [item for item in owed if (item.kind, item.ref) not in sent]


def _other_task_at_work(session: Session, link: TrackerIssueLink) -> bool:
    """True while another task linked to the same issue (any origin) is still at work: the issue stays open."""
    return session.scalar(
        select(TrackerIssueLink.id).join(Task, Task.id == TrackerIssueLink.task_id).where(
            TrackerIssueLink.connection_id == link.connection_id, TrackerIssueLink.issue_key == link.issue_key,
            TrackerIssueLink.task_id != link.task_id, intake.live_clause(),
            # A draft nobody started holds no issue open.
            Task.state != DRAFT, Task.unsaved.is_(False),
        ).limit(1)
    ) is not None


def question_markdown(question: TaskQuestion, link_line: str) -> str:
    head = f"**Question: {question.header}**" if question.header else "**Question**"
    lines = [head, "", question.question.strip()]
    labels = [_option_label(option) for option in question.options or []]
    labels = [label for label in labels if label]
    if labels:
        lines.append("")
        lines.extend(f"{n}. {label}" for n, label in enumerate(labels, 1))
    lines += ["", f"Reply here to answer, or answer in {link_line}."]
    return "\n".join(lines)


def _option_label(option: Any) -> str:
    if isinstance(option, dict):
        return str(option.get("label") or "").strip()
    return str(option or "").strip()


def outcome_markdown(task: Task, link_line: str) -> str:
    if task.state == DONE:
        parts = [f"Done. {link_line}"]
        if (task.outcome_summary or "").strip():
            parts += ["", task.outcome_summary.strip()]
        if task.outcome_pr_url:
            parts += ["", f"Pull request: {task.outcome_pr_url}"]
        return "\n".join(parts)
    # `stop_reason` is an internal code (`balance`, ...): the issue gets the summary only, the task link the rest.
    reason = (task.outcome_summary or "").strip()
    return f"Failed. {link_line}" + (f"\n\n{reason}" if reason else "")


# ── The sweep ─────────────────────────────────────────────────────────────────────────────────────────


def sweep(*, deadline: Optional[float] = None, now: Optional[datetime] = None) -> int:
    """Post what every recently moved tracker task owes its issue; returns how many landed. Stops starting calls
    once `deadline` (a `time.monotonic()` value) is too close to finish one, raising `SweepOutOfTime` when tasks are
left, so the pass fires a follow-up."""
    from app.trigger_intake import fleet_session

    with fleet_session() as session:
        pairs = links_with_work(session, now or utcnow())
    sent = 0
    for connection_id, task_id in pairs:
        if _out_of_time(deadline):
            raise SweepOutOfTime(sent)
        try:
            sent += sweep_task(connection_id, task_id, deadline=deadline)
        except SweepOutOfTime as stopped:
            raise SweepOutOfTime(sent + stopped.sent) from None
        except Exception:  # noqa: BLE001 — one task's fault must not starve the rest
            log.exception("tracker-writeback: task %s failed this sweep", task_id)
    return sent


class SweepOutOfTime(Exception):
    """The sweep stopped for its pass's budget with tasks left; `sent` landed before it did."""

    def __init__(self, sent: int):
        super().__init__(f"out of time after {sent} write-back(s)")
        self.sent = sent


def _out_of_time(deadline: Optional[float]) -> bool:
    return deadline is not None and time.monotonic() + CALL_TIMEOUT_SECONDS >= deadline


@dataclass(frozen=True)
class _Work:
    connection: TrackerConnection
    issue: protocol.IssueRef
    session_id: Optional[str]
    owed: list[Pending]
    failures: dict[str, tuple[int, datetime]]


def sweep_task(connection_id: UUID, task_id: int, *, deadline: Optional[float] = None) -> int:
    """One task's owed write-backs, each claimed, sent and recorded on its own. Returns how many landed."""
    work = _load(connection_id, task_id)
    if work is None:
        return 0
    adapter = protocol.adapter_for(work.connection.tracker)
    if adapter is None:
        return 0
    now = utcnow()
    sent = 0
    for item in work.owed:
        if _out_of_time(deadline):
            raise SweepOutOfTime(sent)
        key = dedup_key(task_id, item.kind, item.ref)
        failed = work.failures.get(key)
        attempts = failed[0] if failed is not None else 0
        if failed is not None and now < failed[1] + timedelta(seconds=backoff_seconds(attempts)):
            continue
        claim = _claim(connection_id, task_id, item)
        if claim is None:
            continue  # another sweep holds it, or sent it
        try:
            _send(adapter, work, task_id, item)
        except Exception as exc:  # noqa: BLE001 — a tracker's failure is logged on the connection, never the task's
            log.warning("tracker-writeback: %s for task %s failed: %s", item.kind, task_id, exc, exc_info=True)
            _failed(connection_id, task_id, claim, item, work.issue, attempts + 1, exc)
            continue
        sent += 1
        if work.connection.last_error is not None:  # a call landed: the connection's last error is over
            _cleared(connection_id)
            work.connection.last_error = None
    return sent


def _cleared(connection_id: UUID) -> None:
    from app.trackers import project_links

    with intake.connection_session(connection_id) as (session, _org):
        project_links.record_outbound(session, connection_id, None)
        session.commit()


def backoff_seconds(attempts: int) -> int:
    """How long after its `attempts`-th failure a write-back is tried again."""
    return min(BACKOFF_FIRST_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS)


def _load(connection_id: UUID, task_id: int) -> Optional[_Work]:
    with intake.connection_session(connection_id) as (session, _org):
        conn = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
        link = session.scalar(select(TrackerIssueLink).where(TrackerIssueLink.task_id == task_id))
        task = session.scalar(select(Task).where(Task.id == task_id))
        adapter = protocol.adapter_for(conn.tracker) if conn is not None else None
        if conn is None or link is None or task is None or adapter is None:
            return None
        owed = pending_for(session, conn, link, task, adapter)
        failures: dict[str, tuple[int, datetime]] = {}
        if owed and conn.trigger_id is not None:
            keys = [dedup_key(task_id, item.kind, item.ref) for item in owed]
            for row in session.execute(
                select(TriggerDelivery.dedup_key, func.count(), func.max(TriggerDelivery.received_at))
                .where(TriggerDelivery.trigger_id == conn.trigger_id, TriggerDelivery.dedup_key.in_(keys),
                       TriggerDelivery.state == SKIPPED)
                .group_by(TriggerDelivery.dedup_key)
            ):
                failures[row[0]] = (int(row[1]), row[2])
        issue = protocol.IssueRef(tracker=conn.tracker, site=conn.site or "", key=link.issue_key,
                                  url=link.issue_url, container_id=link.container_id)
        session.expunge(conn)
        return _Work(conn, issue, link.session_id, owed, failures)


def _claim(connection_id: UUID, task_id: int, item: Pending) -> Optional[int]:
    """Insert and commit the write-back's row; its id when this sweep won it, None when it was already there."""
    with intake.connection_session(connection_id) as (session, org):
        claimed = session.scalar(pg_insert(TrackerWriteback).values(
            org_id=org, task_id=task_id, kind=item.kind, ref=item.ref,
        ).on_conflict_do_nothing(constraint="uq_tracker_writebacks_task_kind_ref").returning(TrackerWriteback.id))
        session.commit()
        return claimed


def _send(adapter: protocol.TrackerAdapter, work: _Work, task_id: int, item: Pending) -> None:
    from app.db_async import run_on_loop

    conn, issue, session_id = work.connection, work.issue, work.session_id
    if item.kind == WRITEBACK_ACK:
        call = adapter.ack(conn, issue, task_url(task_id), session_id)
    elif item.kind == WRITEBACK_DONE:
        call = adapter.mark_done(conn, issue, item.state_name or DEFAULT_DONE_STATE, session_id)
    else:
        call = adapter.post_comment(conn, issue, item.markdown or "", session_id)
    run_on_loop(call, timeout=CALL_TIMEOUT_SECONDS)


def outbound_sentence(tracker: str, exc: BaseException) -> str:
    """The connection's `last_error` for a failed outbound call: the adapter's own sentence (`TrackerError`), else a
    plain one — never an exception's text, which may carry a URL or a token."""
    label = TRACKER_LABELS.get(tracker, tracker)
    if isinstance(exc, protocol.TrackerError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return f"{label} didn't answer in time; try again."
    return f"{label} couldn't be reached; try again."


def _failed(connection_id: UUID, task_id: int, claim: int, item: Pending, issue: protocol.IssueRef, attempts: int,
            exc: Exception) -> None:
    """Give the claim back (kept once the attempts are spent: abandoned) and log the failure on the trigger."""
    with intake.connection_session(connection_id) as (session, _org):
        conn = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
        trigger = connections.trigger_of(session, conn) if conn is not None else None
        # A timed-out call may have landed: retrying it could post twice, so it is kept as sent. A connection with
        # no trigger has nowhere to count its tries, so its first failure is its last.
        timed_out = isinstance(exc, TimeoutError)
        gave_up = timed_out or trigger is None or attempts >= MAX_ATTEMPTS
        if not gave_up:
            session.execute(delete(TrackerWriteback).where(TrackerWriteback.id == claim))
        if conn is not None:
            from app.trackers import project_links

            project_links.record_outbound(session, connection_id, outbound_sentence(conn.tracker, exc))
        if trigger is not None:
            what = _WHAT.get(item.kind, item.kind)
            error = "timed out" if timed_out else (str(exc) or type(exc).__name__).splitlines()[0][:200]
            then = ("not retried: it may have landed" if timed_out
                    else f"gave up after {attempts} tries" if gave_up
                    else f"retrying in {backoff_seconds(attempts) // 60} min")
            intake._log(
                session, trigger, SKIPPED,
                f"Couldn't post the {what} to {TRACKER_LABELS.get(issue.tracker, issue.tracker)} {issue.key} "
                f"({error}); {then}",
                preview={"tracker": {"kind": f"writeback:{item.kind}", "issue_key": issue.key,
                                     "issue_url": issue.url}},
                dedup_key=dedup_key(task_id, item.kind, item.ref), task_id=task_id,
            )
        session.commit()
