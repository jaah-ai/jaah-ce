"""What reached a Task's live runner pane from outside it — `GET /api/tasks/{task_id}/control-messages`.

Two slot-intent verbs type into a runner's pane: `deliver_message` (a control-plane notice, minted by
`app/live_runner.py::deliver_message_to_live_runner`) and `deliver_answer` (an operator's answer to the
runner's question, minted by `routes/tasks/answers.py::_deliver_answer_to_live_runner`). The intent rows are the
only record, so this read projects them; nothing is stored twice.

Who spoke rides the payload (`sender` for a message, `sender_id` for an answer; both additive). A row
minted before either key existed reads "Control plane" / "Operator".

Retention is the table's own (`fleet_verdicts.purge_unexecutable_intents`): a delivered (`done`) row stays,
a superseded or repeatedly refused one is deleted an hour after its mint, and every row goes with its node
or project (CASCADE). The list is therefore what was delivered plus what is still in flight.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import actors
from app.auth import require_user
from app.authz.declarations import feature
from app.db import get_session
from app.fleet_verdicts import (
    OUTCOME_DONE,
    SLOT_INTENT_RETIRING_OUTCOMES,
    VERB_DELIVER_ANSWER,
    VERB_DELIVER_MESSAGE,
)
from app.live_runner import CONTROL_PLANE_LABEL
from app.models.identity import AdminUser
from app.models.worktrees import SlotIntent
from app.task_lookup import get_task_or_404

router = APIRouter(tags=["tasks"])

# The newest this many, oldest first: a lane-heavy task collects notices, and the reader wants the end.
CONTROL_MESSAGES_LIMIT = 200
# …and each row's text cut to this many characters: a pasted log in an answer must not make the page read megabytes.
CONTROL_MESSAGE_TEXT_MAX = 8_000

_KINDS = {VERB_DELIVER_MESSAGE: "message", VERB_DELIVER_ANSWER: "answer"}


class ControlMessageOut(BaseModel):
    """One pane write. `delivered_at` is null until the node acks `done`; `dropped` is a row retired
    without delivery (superseded by a newer message, parked, or refused by an older node) — never
    `delivered` and never coming."""

    id: int
    kind: Literal["message", "answer"]
    created_at: datetime
    sender: str
    text: str
    delivered: bool
    delivered_at: Optional[datetime]
    dropped: bool


def _sender_id(payload: dict[str, Any]) -> Optional[UUID]:
    try:
        return UUID(str(payload["sender_id"]))
    except (KeyError, ValueError):
        return None


def _message_text(row: SlotIntent) -> str:
    """The text without the pane label: the list says who spoke in its own column."""
    payload = row.payload or {}
    if row.verb == VERB_DELIVER_ANSWER:
        return str(payload.get("answer") or "")
    text = str(payload.get("text") or "")
    return text.removeprefix(CONTROL_PLANE_LABEL).lstrip(" ")


@router.get("/api/tasks/{task_id}/control-messages", response_model=list[ControlMessageOut])
@feature("tasks", "view", object_type="task", object_params=("task_id",))
def list_control_messages(
    task_id: int,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[ControlMessageOut]:
    """This task's `deliver_message` and `deliver_answer` intents, oldest first (see the module docstring)."""
    get_task_or_404(task_id, session)
    rows = list(reversed(session.scalars(
        select(SlotIntent)
        .where(
            SlotIntent.verb.in_(tuple(_KINDS)),
            SlotIntent.payload["task_id"].astext == str(task_id),
        )
        .order_by(SlotIntent.created_at.desc(), SlotIntent.id.desc())
        .limit(CONTROL_MESSAGES_LIMIT)
    ).all()))
    names = actors.actor_names(session, [_sender_id(row.payload or {}) for row in rows])
    out: list[ControlMessageOut] = []
    for row in rows:
        payload = row.payload or {}
        if row.verb == VERB_DELIVER_ANSWER:
            answerer = _sender_id(payload)
            sender = names.get(answerer, "Operator") if answerer else "Operator"
        else:
            sender = str(payload.get("sender") or "Control plane")
        delivered = row.outcome == OUTCOME_DONE
        out.append(ControlMessageOut(
            id=row.id,
            kind=_KINDS[row.verb],
            created_at=row.created_at,
            sender=sender,
            text=_message_text(row)[:CONTROL_MESSAGE_TEXT_MAX],
            delivered=delivered,
            delivered_at=row.acked_at if delivered else None,
            dropped=not delivered and row.outcome in SLOT_INTENT_RETIRING_OUTCOMES,
        ))
    return out
