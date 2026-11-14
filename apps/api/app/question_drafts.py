"""Clearing unsent answers (`app/models/tasks.py::TaskQuestionDraft`) once a round resolves.

Every path that answers or dismisses a round calls `clear_round_drafts`: the console's answer and dismiss
(`app/routes/tasks/answers.py`) and an edit that dismisses the open rounds (`app/live_task_edits.py`). A draft left
behind would come back if the round were ever reopened. The routes that read and write drafts:
`app/routes/tasks/question_drafts.py`.
"""
from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.tasks import TaskQuestionDraft


def clear_round_drafts(session: Session, question_id: int) -> None:
    """Forget every person's draft of one round. The caller commits."""
    session.execute(delete(TaskQuestionDraft).where(TaskQuestionDraft.question_id == question_id))
