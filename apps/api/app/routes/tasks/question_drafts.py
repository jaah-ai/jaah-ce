"""Unsent answers: what a person picked, typed and attached on a task's open rounds before Send.

`GET /api/tasks/{task_id}/question-drafts` (the caller's drafts on the task's OPEN rounds) and
`PUT /api/tasks/{task_id}/questions/{question_id}/draft` (replace one; an empty draft deletes the row). Storage:
`app/models/tasks.py::TaskQuestionDraft`. A round's drafts, everyone's, are cleared when it resolves
(`app/question_drafts.py`); a draft never posts anything — Send does.

Both routes are per person: one never reads another's draft. The write is `tasks:edit`, the answer routes' own
permission, since only someone who may answer has an answer to keep.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.auth import adopt_user, require_user
from app.authz.declarations import feature
from app.db_async import on_session
from app.models.identity import AdminUser
from app.models.task_extras import ATTACHMENT_KIND_FILE, TaskAttachment
from app.models.tasks import Task, TaskQuestion, TaskQuestionDraft
from app.routes.attachments import AttachmentOut
from app.routes.tasks.answers import _MAX_ANSWER_ATTACHMENTS, _answer_attachments_or_422
from app.routes.tasks.policy import MAX_TASK_TEXT_LEN
from app.task_artifacts import uploaded_clause, visible_artifact_clause
from app.task_lookup import get_task_or_404

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Bounds at the trust boundary; the answer routes hold the same caps on Send.
_MAX_PICKED = 64


class QuestionDraftIn(BaseModel):
    """One round's unsent answer. `picked` is SOURCE indices into the round's `options`."""

    picked: list[int] = Field(default_factory=list, max_length=_MAX_PICKED)
    note: str = Field(default="", max_length=MAX_TASK_TEXT_LEN)
    attachment_ids: list[int] = Field(default_factory=list, max_length=_MAX_ANSWER_ATTACHMENTS)


class QuestionDraftOut(BaseModel):
    """One round's unsent answer as the console restores it: files as rows, not ids, so a chip can draw at once.
    A file deleted since it was attached is simply absent."""

    question_id: int
    picked: list[int]
    note: str
    files: list[AttachmentOut]


def _open_round_or_409(session: Session, task_id: int, question_id: int) -> TaskQuestion:
    """The round, row-locked: an answer or dismissal updates this row, so the lock orders the status check and
    the draft write against it — a save never lands after Send cleared the round's drafts.

    The TASK row is locked first, in the answer routes' order (`answers.py::_open_round_or_409`): locking the
    round first deadlocked against a Send, which holds the task row and then updates the round, while this
    insert's foreign-key check waited on the task row."""
    session.execute(select(Task.id).where(Task.id == task_id).with_for_update())
    question = session.scalar(
        select(TaskQuestion).where(TaskQuestion.id == question_id, TaskQuestion.task_id == task_id).with_for_update()
    )
    if question is None:
        raise HTTPException(status_code=404, detail=f"question {question_id} is not on task {task_id}")
    if question.status != "open":
        raise HTTPException(status_code=409, detail="This question was already answered.")
    return question


def _valid_picked(question: TaskQuestion, picked: list[int]) -> list[int]:
    """The picks, de-duplicated in order, or a 422 naming the ones the round does not offer."""
    count = len(question.options or [])
    wanted = list(dict.fromkeys(picked))
    bad = [i for i in wanted if i < 0 or i >= count]
    if bad:
        raise HTTPException(status_code=422, detail=f"option(s) {', '.join(map(str, bad))} are not offered by this question")
    if not question.multi_select and len(wanted) > 1:
        raise HTTPException(status_code=422, detail="this question takes one option")
    return wanted


@router.get("/{task_id}/question-drafts", response_model=list[QuestionDraftOut])
@feature("tasks", "view", object_type="task", object_params=("task_id",))
@on_session
def list_question_drafts(
    session: Session, task_id: int, user: AdminUser = Depends(require_user)
) -> list[QuestionDraftOut]:
    """The caller's unsent answers on this task's open rounds, oldest round first."""
    user = adopt_user(session, user)
    task = get_task_or_404(task_id, session)
    rows = session.scalars(
        select(TaskQuestionDraft)
        .join(TaskQuestion, TaskQuestion.id == TaskQuestionDraft.question_id)
        .where(
            TaskQuestionDraft.task_id == task_id,
            TaskQuestionDraft.user_id == user.id,
            TaskQuestion.status == "open",
            # The rounds the pages show: a reset hides the ones before it.
            visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
        )
        .order_by(TaskQuestionDraft.question_id)
    ).all()
    wanted = {att_id for row in rows for att_id in row.attachment_ids}
    files = {
        att.id: AttachmentOut.model_validate(att)
        for att in session.scalars(
            select(TaskAttachment).where(
                TaskAttachment.id.in_(wanted),
                TaskAttachment.task_id == task_id,
                TaskAttachment.kind == ATTACHMENT_KIND_FILE,
                uploaded_clause(),
            )
        ).all()
    } if wanted else {}
    return [
        QuestionDraftOut(
            question_id=row.question_id,
            picked=list(row.picked),
            note=row.note,
            files=[files[att_id] for att_id in row.attachment_ids if att_id in files],
        )
        for row in rows
    ]


@router.put("/{task_id}/questions/{question_id}/draft", status_code=204)
@feature("tasks", "edit", object_type="task", object_params=("task_id",))
@on_session
def save_question_draft(
    session: Session,
    task_id: int,
    question_id: int,
    payload: QuestionDraftIn,
    user: AdminUser = Depends(require_user),
) -> Response:
    """Replace the caller's unsent answer to one open round; an empty one deletes it.

    An upsert on the (round, person) key, so a burst of saves cannot race into a 500. 409 once the round has
    resolved: the draft has nothing left to answer."""
    user = adopt_user(session, user)
    task = get_task_or_404(task_id, session)
    question = _open_round_or_409(session, task_id, question_id)
    picked = _valid_picked(question, payload.picked)
    # The answer route's own check (this task's uploaded files), so a stored draft is always one Send accepts.
    attachment_ids = [att.id for att in _answer_attachments_or_422(session, task_id, payload.attachment_ids)]
    if not picked and not payload.note.strip() and not attachment_ids:
        session.execute(
            delete(TaskQuestionDraft).where(
                TaskQuestionDraft.question_id == question_id, TaskQuestionDraft.user_id == user.id
            )
        )
    else:
        # `org_id` named: a Core insert flushes nothing, so the write seam cannot stamp it (`routes/task_ratings.py`).
        stmt = pg_insert(TaskQuestionDraft).values(
            question_id=question_id, user_id=user.id, task_id=task_id, org_id=task.org_id,
            picked=picked, note=payload.note, attachment_ids=attachment_ids,
        )
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[TaskQuestionDraft.question_id, TaskQuestionDraft.user_id],
                set_={
                    "picked": stmt.excluded.picked,
                    "note": stmt.excluded.note,
                    "attachment_ids": stmt.excluded.attachment_ids,
                    "updated_at": func.now(),
                },
            )
        )
    session.commit()
    return Response(status_code=204)
