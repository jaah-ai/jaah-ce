"""A comment on an issue whose task is still at work: the issue's way to answer or follow up (`intake._handle`).

    comment → who wrote it → an answer to the task's open question, or a comment on the task

- Who: the org member the tracker user is (`intake.member_for`, a verified email only), and only if that member may
  answer this task — the same rule a Slack reply meets (`slack_inbound._may_answer`, read off `answer_task`'s own
  authorization declaration). Anyone else is told once, on the issue, `NOT_ALLOWED_REPLY`; the task is not touched.
  `allow_unmapped_users` lets an unmapped user START a task, never act on one.
- An open `task_questions` round: the comment answers the oldest, through the console's own answer flow
  (`routes.tasks.answers.answer_in_process`, which Slack calls too), as the member, so the task resumes exactly as it
  would from the console. That flow is async; the intake runs on a worker thread, so the delivery is logged here and
  the answer is given by the ingress route after the commit (`answer`), which then rewrites the delivery's reason with
  what came of it. A refused answer is said on the issue.
- No open round: the comment is a follow-up on the task, written as a mention's is (`intake._follow_up`).

A comment on an issue with no live task is not for Jaah and logs nothing. The adapter never hands the intake a
comment Jaah itself wrote (`protocol`), or every reply would answer itself; one written as the account another
connection of the same site writes as (`intake.jaah_accounts`) is ignored here unless its author may act, never refused; the refusal sentence is ignored here too,
the one write that would otherwise loop. A mention on an issue with a live task comes here as well (`intake._handle`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features
from app.models.automations import TaskTrigger, TriggerDelivery
from app.models.identity import AdminUser
from app.models.tasks import Task, TaskQuestion
from app.models.trackers import TrackerConnection
from app.trackers import intake, protocol
from app.trackers.protocol import IssueEvent
from app.trackers.vocabulary import NOT_ALLOWED_REPLY
from app.triggers import DEDUPED, FILTERED

log = logging.getLogger("tracker-replies")


@dataclass(frozen=True)
class ReplyWork:
    """What the ingress route does after the commit: post `notice` on the issue, or give `text` as the member's
    answer to `question_id` (then rewrite delivery `delivery_id`'s reason)."""
    connection_id: UUID
    issue: protocol.IssueRef
    session_id: Optional[str]
    delivery_id: int
    task_id: int
    notice: Optional[str] = None
    member_id: Optional[UUID] = None
    question_id: Optional[int] = None
    text: Optional[str] = None


def may_act(session: Session, member: Optional[AdminUser], task: Task) -> bool:
    """Whether `member` may answer `task`: `slack_inbound`'s rule, the one derivation of `answer_task`'s own
    declaration (never restated here)."""
    if member is None:
        return False
    if not edition_features.enabled("slack"):
        # The rule's home ships with Slack; an edition without it (the community edition, whose console offers no
        # issue trackers either) refuses every tracker reply rather than restating the rule.
        return False
    from app.slack_inbound import _may_answer

    return _may_answer(session, member, task)


def handle(session: Session, org: UUID, conn: TrackerConnection, trigger: TaskTrigger, event: IssueEvent,
           key: str, works: list[ReplyWork]) -> Optional[TriggerDelivery]:
    """The comment's delivery row (staged; the caller commits), appending what the route must do after the commit to
    `works`. None for a comment that is not for Jaah."""
    text = (event.text or "").strip()
    live = intake.live_link(session, conn.id, event.issue.key)
    if live is None or not text or text == NOT_ALLOWED_REPLY:
        # The last: Jaah's own refusal, should an adapter ever hand it back, must not be refused in turn, forever.
        return None
    preview = intake._preview(event)
    first = session.scalar(select(TriggerDelivery.id).where(
        TriggerDelivery.trigger_id == trigger.id, TriggerDelivery.dedup_key == key).limit(1))
    if first is not None:
        return intake._log(session, trigger, DEDUPED, f"duplicate of delivery #{first}", preview=preview)
    task = session.scalar(select(Task).where(Task.id == live.task_id))
    member = intake.member_for(session, org, event.actor)
    preview["tracker"]["member_id"] = str(member.id) if member is not None else None
    who = event.actor.name or event.actor.external_id
    if member is None or task is None or not may_act(session, member, task):
        if event.actor.external_id in intake.jaah_accounts(session, conn):
            # Another connection of this site writes as this account (its adapter filters only its own): Jaah's
            # write-back, never refused. A person's own token still answers, above, when that person may act.
            return None
        row = intake._log(session, trigger, FILTERED, f"{who} is not allowed to act on Task #{live.task_id}",
                          preview=preview, task_id=live.task_id, dedup_key=key)
        works.append(ReplyWork(conn.id, event.issue, event.session_id, row.id, live.task_id, notice=NOT_ALLOWED_REPLY))
        return row
    question_id = session.scalar(select(TaskQuestion.id).where(
        TaskQuestion.task_id == task.id, TaskQuestion.status == "open").order_by(TaskQuestion.id).limit(1))
    if question_id is None:
        intake._follow_up(session, conn.tracker, task.id, event.issue.key, who, text)
        return intake._log(session, trigger, FILTERED, intake.followed_up_reason(task.id), preview=preview,
                           task_id=task.id, dedup_key=key)
    row = intake._log(session, trigger, FILTERED, f"answering Task #{task.id}'s question", preview=preview,
                      task_id=task.id, dedup_key=key)
    works.append(ReplyWork(conn.id, event.issue, event.session_id, row.id, task.id, member_id=member.id,
                           question_id=question_id, text=text))
    return row


async def resolve(work: ReplyWork) -> Optional[str]:
    """The route's first half of one `ReplyWork`, on a worker thread's own loop (sync sessions all the way down): the
    sentence to post on the issue, or None. Never raises: the delivery is already accepted."""
    if work.notice is not None:
        return work.notice
    try:
        return await answer(work)
    except Exception:  # noqa: BLE001 — logged, settled and said; the tracker's request has been answered
        log.exception("tracker-replies: task %s: the answer failed", work.task_id)
        with intake.connection_session(work.connection_id) as (session, _org):
            session.rollback()
            return _settle(session, work, f"Task #{work.task_id}: the answer failed", _couldnt(work.task_id))


def connection_of(connection_id: UUID) -> Optional[TrackerConnection]:
    """The connection, detached, for the adapter call; None once disconnected."""
    with intake.connection_session(connection_id) as (session, _org):
        conn = session.scalar(select(TrackerConnection).where(TrackerConnection.id == connection_id))
        if conn is not None:
            session.expunge(conn)
        return conn


def _couldnt(task_id: int) -> str:
    from app.trackers.writeback import task_url

    return f"Couldn't record that answer. Answer in [Task #{task_id}]({task_url(task_id)})."


async def answer(work: ReplyWork) -> Optional[str]:
    """Give the comment as the member's answer to the open round. Returns the sentence to post on the issue when the
    answer was not recorded, else None. Rewrites the delivery's reason with the result. A refusal's own detail is
    logged on the delivery, never posted on the issue: the tracker is outside Jaah."""
    from fastapi import HTTPException

    from app.actors import set_request_actor
    from app.routes.tasks.answers import AnswerIn, answer_in_process

    with intake.connection_session(work.connection_id) as (session, _org):
        user = session.scalar(select(AdminUser).where(AdminUser.id == work.member_id))
        if user is None:
            return _settle(session, work, "the member is gone", NOT_ALLOWED_REPLY)
        try:
            body = AnswerIn(answer=work.text, question_id=work.question_id)
        except ValidationError:
            return _settle(session, work, "the reply is too long to record",
                           "That reply is too long to record as an answer. Send a shorter one, or answer in Jaah.")
        set_request_actor(session, user.id)
        try:
            await answer_in_process(session, task_id=work.task_id, body=body, user=user, principal=None)
        except HTTPException as exc:
            session.rollback()
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return _settle(session, work, f"Task #{work.task_id} refused the answer ({exc.status_code}): {detail}",
                           _couldnt(work.task_id))
        finally:
            set_request_actor(session, None)
        _settle(session, work, f"answered Task #{work.task_id}'s question", None)
        return None


def _settle(session: Session, work: ReplyWork, reason: str, notice: Optional[str]) -> Optional[str]:
    row = session.scalar(select(TriggerDelivery).where(TriggerDelivery.id == work.delivery_id))
    if row is not None:
        row.reason = reason
        session.commit()
    return notice
