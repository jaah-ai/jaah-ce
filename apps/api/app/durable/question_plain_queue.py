"""The question-rewording queue — a recorded question round reworded in plain terms, as durable work.

Modelled on `task_title_queue.py`, whose reasons hold here: the rewording is an event, the enqueue is its own
write after the round's commit, and `BackgroundTasks` would drop it on a deploy. Both question routes enqueue
(`routes/agent/task_terminal.py::create_task_question`, `routes/agent_sdk.py::ask_question_batch`). It rides
`task-title`'s queue: the same model, key and vendor rate limit, and no second idle poller.
"""
from __future__ import annotations

import logging

from dbos import DBOS

from app.durable import runtime
from app.durable.runtime import enqueue_or_warn
from app.durable.task_title_queue import TITLE_QUEUE

log = logging.getLogger("question-plain")


def plain_workflow_id(question_id: int) -> str:
    """Keyed on the round alone: a round owes one rewording, so a recovery re-enqueue collapses. Pure."""
    return f"question-plain:{question_id}"


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=5.0)
def reword(task_id: int, question_id: int) -> bool:
    """One attempt, as a checkpointed, retried step. Only database faults retry: `plain_question` swallows
    vendor failures. The org is derived from the task (`runtime.delegate_for_task`); a deleted task is False."""
    from app.question_plain import reword_question

    return runtime.delegate_for_task(
        "durable:question_plain.reword", task_id,
        gone=f"question-plain: task {task_id} no longer exists — nothing to reword", logger=log,
        call=lambda _org: reword_question(question_id), fallback=False,
    )


@DBOS.workflow()
def reword_question_round(task_id: int, question_id: int) -> None:
    """One durable rewording for one recorded round. Only the ids travel; the row is re-read."""
    reword(task_id, question_id)


def enqueue_plain(task_id: int, question_id: int) -> bool:
    """Enqueue one rewording. Never raises into the question route: the round is already committed."""
    return enqueue_or_warn(
        TITLE_QUEUE, plain_workflow_id(question_id), reword_question_round, task_id, question_id, log=log,
        failure=f"question-plain: could not enqueue the rewording of round {question_id} on task {task_id}",
        consequence="the round shows the runner's own words",
    )
