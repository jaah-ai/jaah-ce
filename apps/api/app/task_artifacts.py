"""The one test for whether an artifact row a Task's runner produced is still shown.

A Reset soft-deletes what the abandoned attempt produced (Q&A rounds, notes, report, activity history)
by one watermark, `tasks.artifacts_hidden_before` (its model comment is the home for why). Lives apart
from `app/routes/tasks/` so Slack, notification and agent-token readers share one spelling; imports
only SQLAlchemy, `app.models` and `app.task_manifest`, so it cannot cycle.

Hidden means strictly older. The watermark is the reset UPDATE's `statement_timestamp()`, not `now()`
(`routes/tasks._RESET_TO_DRAFT_COLUMNS`). Every predicate here fails open if a reader omits it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import ColumnElement, or_, select, true
from sqlalchemy.orm import Session

from app.models.task_extras import TaskAttachment
from app.models.tasks import Task
from app.task_manifest import DOCUMENT_KINDS


#: The `activity_events` note kinds no activity reader shows (a Task's Activity Log, the Audit page, the SIEM export).
#: Every kind here outlives its producer: rows written before retirement are still in the table, so removing an
#: entry is a display regression.
ACTIVITY_EXCLUDED_NOTE_KINDS: tuple[str, ...] = (
    "task_note:kill_intent", "task_note:kill_intent_done", "task_note:active_label_stripped",
)

#: The `activity_events` note kinds a Reset soft-deletes. `task_note:stage_output` stays beside
#: `task_note:outcome` so pre-cutover history remains hideable. Kind membership is necessary, not
#: sufficient: `is_hidden_note_kind` keeps outcome audit rows (no `data["summary"]`) visible.
RESET_HIDDEN_NOTE_KINDS: tuple[str, ...] = (
    "task_note:answered",
    "task_note:stage_output",
    "task_note:outcome",
)


def is_hidden_note_kind(kind: str, data: Optional[dict]) -> bool:
    """Whether a note row is one a Reset may hide at all — the kind test plus the `task_note:outcome`
    exception. The caller still compares timestamps.

    An outcome row is an artifact only if `data` has a `"summary"` key (the retired outcome verb's audit
    row carries its line in the `summary` column instead) — the same test as
    `app/routes/tasks/task_wire.py::_has_settled_output_body`. Key presence, not truthiness: an empty body is still
    an artifact."""
    if kind not in RESET_HIDDEN_NOTE_KINDS:
        return False
    if kind != "task_note:outcome":
        return True
    return "summary" in (data or {})


def visible_artifact_clause(
    created_at_col: ColumnElement[datetime],
    hidden_before: Optional[datetime],
) -> ColumnElement[bool]:
    """Rows this Task still shows, given its watermark value. `None` (never reset) is a true-clause so
    the caller stays branchless."""
    if hidden_before is None:
        return true()
    return created_at_col >= hidden_before


def visible_artifact_clause_joined(created_at_col: ColumnElement[datetime]) -> ColumnElement[bool]:
    """The same rule for a query that joins `tasks`, where the watermark is a column rather than a value.

    The caller must have joined `tasks`; nothing enforces it. Without the join SQLAlchemy warns and
    builds a cartesian product that passes every row (fails open). The NULL arm is explicit because a
    comparison against NULL would hide every never-reset Task's artifacts."""
    return or_(
        Task.artifacts_hidden_before.is_(None),
        created_at_col >= Task.artifacts_hidden_before,
    )


def activity_row_is_visible(created_at: datetime, hidden_before: Optional[datetime]) -> bool:
    """`visible_artifact_clause`'s rule in Python for one `activity_events` row of one task (its Activity
    Log and Run metrics clock).

    Kind-blind on purpose: callers already narrow to this task's `task:%` / `task_note:%` rows, and a
    kind list would drift open. The `task:reset` row hides itself (its `now()` precedes the
    `statement_timestamp()` watermark), so the tab is empty right after a Reset — intended. The
    system-wide audit log (`app/routes/audit.py`) does not use this rule."""
    return hidden_before is None or created_at >= hidden_before


#: Latest-wins ordering, spelled once so `current_artifact` (settle manifest) and
#: `visible_document_artifacts` (Artifacts tab) never disagree. `id DESC` breaks ties within one
#: statement timestamp; index `(task_id, kind, id DESC)`.
_LATEST_FIRST = (TaskAttachment.created_at.desc(), TaskAttachment.id.desc())


def uploaded_clause() -> ColumnElement[bool]:
    """Rows whose bytes are actually there — `uploaded_at IS NOT NULL`.

    Orthogonal to the watermark: uploads are presigned PUTs (`app/attachment_storage.py`), so a row
    exists before its bytes are confirmed and must be listed, served and counted toward a settle's
    manifest by nothing. Fails open if omitted, like the watermark. Every pre-existing row was stamped,
    so this hides no history.
    """
    return TaskAttachment.uploaded_at.is_not(None)


def current_artifact(session: Session, task: Task, kind: str) -> Optional[TaskAttachment]:
    """The one latest-wins artifact read: the row a Task currently counts as its `kind` deliverable, or
    `None`. Superseded rows are kept (ordering, not a unique constraint).

    The watermark and upload filters are inside the ordering, so a hidden newest row is skipped rather
    than returned. Raises `ValueError` for a non-document kind: the human's `ATTACHMENT_KIND_FILE` input
    is never Reset-hidden, and derived/reference kinds are never rows — both are caller bugs."""
    if kind not in DOCUMENT_KINDS:
        raise ValueError(
            f"current_artifact: {kind!r} is not a document artifact kind — expected one of "
            f"{DOCUMENT_KINDS}. Only these become `task_attachments` rows "
            f"(`app/task_manifest.py::DOCUMENT_KINDS`); the human's dispatch input is read without the "
            f"Reset watermark, and a derived or reference kind is never a row."
        )
    return session.scalars(
        select(TaskAttachment)
        .where(
            TaskAttachment.task_id == task.id,
            TaskAttachment.kind == kind,
            visible_artifact_clause(
                TaskAttachment.created_at, task.artifacts_hidden_before
            ),
            uploaded_clause(),
        )
        .order_by(*_LATEST_FIRST)
        .limit(1)
    ).first()


def visible_document_artifacts(session: Session, task: Task) -> list[TaskAttachment]:
    """Every document artifact this Task still shows, newest first — what the Artifacts tab renders
    (`apps/web/src/components/tasks/TaskArtifacts.tsx`).

    Superseded rows are listed (`current_artifact_ids` flags the live one). Document kinds only, so every
    row the tab offers opens. Obeys the watermark and upload filters."""
    return list(
        session.scalars(
            select(TaskAttachment)
            .where(
                TaskAttachment.task_id == task.id,
                TaskAttachment.kind.in_(DOCUMENT_KINDS),
                visible_artifact_clause(
                    TaskAttachment.created_at, task.artifacts_hidden_before
                ),
                uploaded_clause(),
            )
            .order_by(*_LATEST_FIRST)
        ).all()
    )


def current_artifact_ids(rows: Sequence[TaskAttachment]) -> set[int]:
    """Which of `rows` are the current row of their kind — the Artifacts tab's `current` flag.

    Re-derives the winner from each row's `_LATEST_FIRST` key rather than trusting the caller's order,
    so it structurally agrees with `current_artifact`. A NULL `created_at` sorts oldest; the id
    tie-break is what makes this agree with the SQL ordering."""
    def sort_key(row: TaskAttachment) -> tuple[bool, datetime, int]:
        # The leading flag is what keeps a NULL out of the datetime comparison: two keys only ever
        # reach their second element once both carry a real timestamp.
        return (row.created_at is not None, row.created_at, row.id)

    best: dict[str, tuple[bool, datetime, int]] = {}
    winner: dict[str, int] = {}
    for row in rows:
        key = sort_key(row)
        if row.kind not in best or key > best[row.kind]:
            best[row.kind] = key
            winner[row.kind] = row.id
    return set(winner.values())
