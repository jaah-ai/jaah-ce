"""Question and Q&A-trail rendering: option details, question rows and the prompt's fitted trail."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, Iterable, Optional
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features
from app.entity_shape import from_row
from app.identities import operator_label
from app.models.audit import ActivityEvent
from app.models.identity import AdminUser
from app.models.merge_lane import MergeLaneRow
from app.models.tasks import Task, TaskQuestion
from app.question_plain import QuestionPlain, stored_plain
from app.task_artifacts import visible_artifact_clause, visible_artifact_clause_joined
from app.task_awaiting_response_parser import parse_response_options
from app.task_lifecycle import AWAITING_RESPONSE


# ── One offered option, in full ──────────────────────────────────────────────────────────────────────
# The DISPLAY wire for a question's options, beside the answer wire (`options`), never instead of it.
class QuestionOptionOut(BaseModel):
    """One offered option with everything its author wrote about it. Built ONLY by
    `_question_option_details`, the single home for how the writers' option shapes differ."""

    # JOIN KEY to the answer wire: `options[i] == option_details[i].label` by construction, and the answer
    # route validates `selections` against exactly this string. Emitted VERBATIM, never stripped.
    label: str
    # Multi-line prose; blank normalizes to null (absence-is-not-a-state).
    description: Optional[str]
    # PRE-FORMATTED monospace mockup, carried apart from `description` so a renderer keeps its columns
    # and a prompt (`_task_qa_trail`) can fold them.
    preview: Optional[str]
    # From the hook's trailing `{"recommended_index": N}` marker or the `(Recommended)` label suffix. A
    # BOOL PER OPTION, not an index on the round: `_question_option_details` SKIPS label-less entries, so
    # a stored index would count positions that are not emitted.
    recommended: bool
    # The option is sent with the comment box's text as its value (`checkout_gate.CHANGE_BRANCH`'s branch), so the
    # console focuses the box instead of sending it empty. Only a dict option's literal `true` sets it.
    needs_comment: bool = False


# ── Detail-only Q&A projection ──────────────────────────────────────────────────────────────────────
# Reads the `activity_events` rows `record_task_note` writes as `task_note:answered`/`task_note:ignored`;
# DETAIL route only — the list stays lightweight.
class QAEntryOut(BaseModel):
    """One 🛑 FLEET BLOCKED question and its resolution: a durable `task_note:answered` or
    `task_note:ignored` note (question/options snapshotted at resolve time), plus, on a still-`blocked`
    Task, a trailing UNRESOLVED entry built live from `awaiting_response_body`."""

    question: Optional[str]
    # The `task_questions` row this entry resolved — what Revise needs. Snapshotted into the blob by
    # `_record_answered_round`/`ignore_question`; NULL on earlier notes (no backfill: the text is not a
    # key) and on the trailing unresolved entry. A null withholds Revise honestly.
    question_id: Optional[int] = None
    # `answered`, `dismissed`, or `unresolved`. A column of its own: `resolution is None` cannot tell a
    # dismissal from an unanswered ask, and `_task_qa_trail` reads it to keep dismissals out of a prompt.
    # Spelled in `models/tasks.py::TaskQuestion.status`'s vocabulary for the console's `resolvedVerb`.
    status: str = "answered"
    # Null on a note written before it was recorded and on a round whose runner stated none.
    header: Optional[str]
    options: list[str]
    # The options WITH their author's text, beside `options`. EMPTY LIST, never null, as `options` coalesces.
    option_details: list[QuestionOptionOut]
    resolution: Optional[str]
    # Is the answer half runner-authored (clicked option / joined multi-select) rather than typed?
    # `_task_qa_trail` uses it so a clicked giant option cannot inherit the human-words exemption from
    # `_QA_TRAIL_MAX_CHARS`. Default False: historical notes and free text are the human's.
    machine_authored: bool = False
    resolved_at: Optional[datetime]
    # Display name, resolved from the NOTE's actor (the only record reaching back past migration 0136).
    # Null on the unresolved entry, an anonymous historical note, or a deleted account
    # (`_note_resolver_label`). NO DEFAULT: both construction sites are in `_task_qa_entries`.
    resolved_by: Optional[str]


# ── task_questions projection (single home for TaskOut.questions + the /questions read route) ────────
# Rows are written by the `AskUserQuestion` capture hook (`routes/agent/task_terminal.py::create_task_question`).
class TaskQuestionOut(BaseModel):
    id: int
    task_id: int
    question: str
    # Short per-round name inside a batch ("Missing input"). NULL = none (`models/tasks.py::TaskQuestion.header`).
    header: Optional[str]
    options: Optional[list[Any]]
    # DISPLAY wire beside the ANSWER wire, never collapsed into it: an option OBJECT in `options` took the
    # task page down (React error #31) and 500'd the detail read. Index-aligned by construction
    # (`_question_option_details`).
    option_details: Optional[list[QuestionOptionOut]]
    # Several `options` rather than one (models/tasks.py:TaskQuestion): checkboxes, not buttons. NO DEFAULT:
    # `_task_question_out` is the sole constructor.
    multi_select: bool
    # Null while `open` or dismissed without a choice.
    chosen_resolution: Optional[str]
    # Carried apart from `chosen_resolution`: "they picked X, and they said Y" are two facts.
    comment: Optional[str]
    status: str
    # `models/tasks.py::TaskQuestion.resolved_by_id` says what the pair means. Both null while `open`;
    # `resolved_by` alone null when the author is unrecoverable. A DISPLAY NAME, never an id, spelled by
    # `identities.operator_label`, as the Slack card's is, so this byline and the card agree.
    resolved_by: Optional[str]
    resolved_at: Optional[datetime]
    # When the resolution was handed to the runner (`_settle_gate_if_nothing_outstanding`, which holds every
    # answer until no round is open). Null while held or open. Handed over, not consumed: the task page reads
    # pickup off the agent's own events.
    delivered_at: Optional[datetime]
    # 1-based round number, DERIVED AT READ TIME from the task's created_at,id sequence — no column.
    # `_task_questions_out` assigns it from list position; a single-row projection is passed its ordinal.
    round: int
    created_at: datetime
    # The round reworded in plain terms, DISPLAY ONLY (`app/question_plain.py`): `plain.options[i]` rewords
    # `option_details[i]`, and an answer still posts `options[i]`. Null until reworded, or when the blob is malformed.
    plain: Optional[QuestionPlain]


# Answer + operator comment as ONE line; single home so the delivery payload and the Q&A trail agree, and
# the phrase names WHOSE words the tail is (the head is usually runner-authored, the tail never).
_COMMENT_JOINER = " — operator comment: "
# The joiner is a STRUCTURAL DELIMITER of the delivered payload; agent-authored text has the label
# rewritten to this by `_neutralize_batch_delimiters`.
_NEUTRALIZED_COMMENT_LABEL = "agent-authored-operator-comment"
# Matched case-insensitively and independently of the dash: the reader defended is a language model, not
# a parser, so `–`, `--`, `Operator comment` must all be caught.
_COMMENT_LABEL_RE = re.compile(r"operator\s+comment", re.IGNORECASE)


def _normalized_comment(comment: Optional[str]) -> Optional[str]:
    """The operator's comment as STORED: trimmed, blank normalized to None (absence-is-not-a-state)."""
    return (comment or "").strip() or None


def _answer_with_comment(answer: str, comment: Optional[str]) -> str:
    """`answer` with the operator's comment appended; blank/absent returns the answer UNCHANGED."""
    text = _normalized_comment(comment)
    return f"{answer}{_COMMENT_JOINER}{text}" if text else answer


_RECOMMENDED_LABEL_SUFFIX = "(Recommended)"


def _label_marks_recommended(label: str) -> bool:
    """Whether a label carries the `AskUserQuestion` "(Recommended)" convention — the same test
    the node's question-capture hook (`recommended_index`) applies on the way in (trailing whitespace
    tolerated). Call with a RAW stored label: `_neutralize_batch_delimiters` rewrites `)` to `]`."""
    return label.rstrip().endswith(_RECOMMENDED_LABEL_SUFFIX)


def _option_detail_text(value: Any) -> Optional[str]:
    """An option's descriptive field as PROJECTED: the string when it has content, else None. A
    non-string is absence, not an error (the column is `list[Any]`)."""
    return value if isinstance(value, str) and value.strip() else None


def _question_option_details(options: Any) -> Optional[list[QuestionOptionOut]]:
    """Project a stored `options` blob into FULL options — the single home for how the writers differ.

    The console stores bare strings; the `AskUserQuestion` hook stores option objects plus a trailing
    `{"recommended_index": N}` marker. Normalizing at read time repairs blobs already stored; a non-list
    is "no options". Order-preserving; a dict without a non-blank string `label` is skipped (dropping the
    marker). A bare `""` label passes: the blank-glyph refusal lives in `_gate_answer_or_422`.

    `recommended` is a bool per option: the marker names a verbatim position, translated through the
    skip map (a non-int, bool, out-of-range or skipped index marks nothing); also true for a
    `(Recommended)` label suffix, which older rows carry without a marker."""
    if not isinstance(options, list):
        return None
    out: list[QuestionOptionOut] = []
    # verbatim position -> emitted position, built AS the walk skips.
    emitted_at: dict[int, int] = {}
    marker_index: Any = None
    for verbatim_index, opt in enumerate(options):
        if isinstance(opt, str):
            emitted_at[verbatim_index] = len(out)
            out.append(QuestionOptionOut(label=opt, description=None, preview=None, recommended=False))
            continue
        if isinstance(opt, dict):
            label = opt.get("label")
            if not (isinstance(label, str) and label.strip()):
                # No label, nothing clickable; the hook's trailing marker is read before being dropped.
                if "recommended_index" in opt:
                    marker_index = opt.get("recommended_index")
                continue
            emitted_at[verbatim_index] = len(out)
            out.append(QuestionOptionOut(
                # NOT `label.strip()`: the answer route matches a selection against the stored string.
                label=label,
                description=_option_detail_text(opt.get("description")),
                preview=_option_detail_text(opt.get("preview")),
                recommended=_label_marks_recommended(label),
                needs_comment=opt.get("needs_comment") is True,
            ))
            continue
        emitted_at[verbatim_index] = len(out)
        out.append(QuestionOptionOut(label=str(opt), description=None, preview=None, recommended=False))
    # `isinstance(True, int)` is True: without the bool test `{"recommended_index": true}` recommends option 0.
    if isinstance(marker_index, int) and not isinstance(marker_index, bool):
        emitted_index = emitted_at.get(marker_index)
        if emitted_index is not None:
            out[emitted_index].recommended = True
    return out


def _question_options_out(options: Any) -> Optional[list[str]]:
    """The LABEL projection of `_question_option_details` — the `string[]` ANSWER wire that
    `_gate_answer_or_422` and `_resolution_for_delivery` speak. Derived from the detail list, not walked
    again, so the two lists are index-aligned BY CONSTRUCTION."""
    details = _question_option_details(options)
    return None if details is None else [detail.label for detail in details]


def _task_question_out(
    row: TaskQuestion, round_index: int, *, resolved_by: Optional[str]
) -> TaskQuestionOut:
    """Project one TaskQuestion row at a caller-supplied 1-based `round_index`.

    `resolved_by` is required and NOT looked up here: the label is a query, and `_task_questions_out`
    batches it. An `open` round has no resolver, so its one single-row caller passes None."""
    # One walk, then the label projection off it — never `_question_options_out` as well.
    option_details = _question_option_details(row.options)
    return from_row(
        TaskQuestionOut, row,
        # `None` PROPAGATES rather than coalescing to `[]`: "no options at all" is a carried state.
        options=None if option_details is None else [detail.label for detail in option_details],
        option_details=option_details,
        resolved_by=resolved_by,
        round=round_index,
        plain=stored_plain(row.plain),
    )


def _task_questions_out(session: Session, rows: list[TaskQuestion]) -> list[TaskQuestionOut]:
    """Project an ordered (created_at, id) list of TaskQuestion rows, deriving `round` from position — the
    caller MUST pass them ordered. Resolver labels are looked up here in one query for the whole trail."""
    labels = _operator_labels(session, (row.resolved_by_id for row in rows))
    return [
        _task_question_out(
            row,
            idx,
            resolved_by=labels.get(row.resolved_by_id) if row.resolved_by_id else None,
        )
        for idx, row in enumerate(rows, start=1)
    ]


def _load_task_questions_out(session: Session, task_id: int) -> list[TaskQuestionOut]:
    """Load + project a Task's VISIBLE TaskQuestion history, ordered, rounds derived. Reset-soft-deleted
    rounds are excluded (`app/task_artifacts.py`), which is what gives a re-run's first question Round 1."""
    rows = session.scalars(
        select(TaskQuestion)
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(TaskQuestion.task_id == task_id)
        .where(visible_artifact_clause_joined(TaskQuestion.created_at))
        .order_by(TaskQuestion.created_at, TaskQuestion.id)
    ).all()
    return _task_questions_out(session, list(rows))


def _note_resolver_label(
    event: ActivityEvent, labels: dict[UUID, str]
) -> Optional[str]:
    """Who a `task_note:answered` row says settled the question, as a name a human reads.

    The joined name only, never the note's frozen `actor_label` (an email): after an account delete that
    would upgrade the disclosure, and `task_questions.resolved_by_id` cascades to NULL on the same delete,
    so both surfaces go quiet together (`activity_events` remains the audit trail). None for an
    actor-less note or a deleted account; the console then prints no byline."""
    if event.user_id is None:
        return None
    return labels.get(event.user_id)


# The note kinds this trail reads, mapped to the round outcome each records — one fact, read by the
# query and by the loop stamping `QAEntryOut.status`; `models/tasks.py::TaskQuestion.status` owns the values.
_QA_NOTE_STATUS: dict[str, str] = {
    "task_note:answered": "answered",
    "task_note:ignored": "dismissed",
}


def _task_qa_entries(
    session: Session, task: Task, *, defang_for_prompt: bool = False
) -> list[QAEntryOut]:
    """Assemble a Task's Q&A resolutions for the detail route, from the durable `task_note:answered` and
    `task_note:ignored` notes (the `task_note:` namespace keeps them out of the dwell analytics). Each
    note is a resolved entry, oldest first; a still-`blocked` Task also gets one trailing unresolved
    entry built from `awaiting_response_body`.

    Dismissals are included (the trail is where a round is revised from) with no resolution;
    `_task_qa_trail` skips them. One entry per round, newest note wins: otherwise a re-dispatched runner
    would get a superseded answer. Notes without `question_id` cannot be folded and are all kept.

    `defang_for_prompt` splits the consumers: the console renders resolutions verbatim (defanging would
    misstate what was clicked), while `_task_qa_trail` feeds a runner's prompt and defangs the
    agent-authored half. It covers only the resolution half; `_task_qa_trail` flattens and neutralizes
    `entry.question` itself, and `dispatch_compose.fencing.defang_fence_markers` cannot close the `Q:`/`A:`
    shape. Typed answers and comments are the human's words and keep their newlines."""
    # A Reset soft-deletes the abandoned attempt's interview; see `app/task_artifacts.py`.
    answered = session.scalars(
        select(ActivityEvent)
        # Both resolve verbs' notes (see the docstring); `_QA_NOTE_STATUS` keeps query and status in step.
        .where(ActivityEvent.kind.in_(tuple(_QA_NOTE_STATUS)))
        .where(ActivityEvent.data["id"].astext == str(task.id))
        .where(visible_artifact_clause(
            ActivityEvent.created_at, task.artifacts_hidden_before))
        .order_by(ActivityEvent.created_at, ActivityEvent.id)
    ).all()
    # Every note's actor named in one query (`_operator_labels`); the prompt consumer ignores them rather
    # than using a second query shape.
    resolver_labels = _operator_labels(session, (event.user_id for event in answered))
    qa_entries: list[QAEntryOut] = []
    for event in answered:
        data = event.data or {}
        # The outcome off the note's kind, never the blob; the `or` arm is unreachable while the query selects
        # only these kinds, and stops a new kind silently defaulting to "answered".
        status = _QA_NOTE_STATUS.get(event.kind or "", "answered")
        # The note snapshots `options` verbatim (possibly objects); normalized by the row read's helper, labels
        # projected from the same walk.
        note_details = _question_option_details(data.get("options")) or []
        note_options = [detail.label for detail in note_details]
        # Same provenance test as the wire — an answer matching an option is runner-authored — but applied
        # only for the prompt consumer (see the docstring), and UNCAPPED: a prompt is not an argv.
        answer_text = data.get("answer")
        # ORed with the round's `multi_select` (see `_classified_for_delivery`): membership is only a proxy.
        # Read from the note blob; `is True` is strict, and an absent key (older notes) is False.
        machine_authored = data.get("multi_select") is True
        # Two questions that must not share an answer:
        # * Whether to defang is a safety question, erring toward the runner: "did this round offer options at
        # all", since membership fails open when a transport reshapes a label (Slack's clipped, sanitized value).
        # * Whether it is clippable is whose words these are, erring toward the human: the narrow membership
        # test, or typed answers would lose their `_QA_TRAIL_MAX_CHARS` exemption. Known residual: many
        # reshaped Slack rounds (~2,000 chars each, defanged) can bloat the section; each costs a human click.
        defang_answer = machine_authored or bool(note_options)
        answer_is_machine = machine_authored or (
            isinstance(answer_text, str) and answer_text in note_options
        )
        if defang_for_prompt and isinstance(answer_text, str) and defang_answer:
            # Flattened as well as defanged: this trail is newline-structured, so a runner-authored line break could
            # stage a forged `Q:`/`A:` pair. `_neutralize_batch_delimiters` folds Cc/Cf but not `U+2028`/`U+2029`;
            # `_flatten` splits on those too.
            answer_text = _flatten(_neutralize_batch_delimiters(answer_text))
        # The comment is stitched on here (via the shared `_COMMENT_JOINER`) so neither one-line surface drops
        # the operator's own words.
        qa_entries.append(
            QAEntryOut(
                question=data.get("question"),
                # Normalized like every other blob value: JSONB rows are read back for ever, and a non-`str` here
                # would 500 `QAEntryOut`.
                header=_option_detail_text(data.get("header")),
                # Which round, for Revise (`QAEntryOut.question_id`): an `int` or nothing, since `True` is an `int`.
                question_id=_note_question_id(data.get("question_id")),
                options=note_options,
                option_details=note_details,
                # A dismissal prints nothing: that absence is the record. Belt-and-braces against a future writer.
                resolution=(
                    None if status == "dismissed"
                    # `or ""` / `or None` so a note with a blank answer and a real comment keeps the human's words.
                    else _answer_with_comment(answer_text or "", data.get("comment")) or None
                ),
                status=status,
                # The answer half's provenance for `_task_qa_trail`'s cap. A comment on a machine-authored answer shares
                # its clippability — accepted, since splitting the line would change a format three surfaces read.
                machine_authored=answer_is_machine,
                resolved_at=event.created_at,
                resolved_by=_note_resolver_label(event, resolver_labels),
            )
        )
    # One entry per round, newest note wins, applied to the walk (the query would need a window function).
    # The kept note holds its own chronological place, matching the `resolved_at` it carries.
    qa_entries = _fold_revised_rounds(qa_entries)
    # A round that is open again carries no answer. `revise-question` reopens a round without touching the
    # answered note (the note-log is the audit trail), so the fold would keep the withdrawn answer and
    # `_task_qa_trail` would hand a runner a retracted ruling as settled. Cleared, not dropped: the console
    # still renders the question, with `status="unresolved"`.
    qa_entries = _clear_reopened_resolutions(session, task, qa_entries)
    # Trailing OPEN question: the ask the operator has not resolved yet, surfaced with null
    # resolution/resolved_at so the UI can render it alongside the resolved history above.
    answered_body = next((
        entry for entry in reversed(qa_entries)
        if task.state == AWAITING_RESPONSE and entry.question_id is not None
        and entry.status == "answered" and entry.resolved_at is not None
        and entry.question == task.awaiting_response_body
    ), None)
    newer_question = session.scalar(
        select(ActivityEvent.id).where(
            ActivityEvent.kind == "task_note:question_raised",
            ActivityEvent.data["id"].astext == str(task.id),
            ActivityEvent.created_at > answered_body.resolved_at,
            visible_artifact_clause(ActivityEvent.created_at, task.artifacts_hidden_before),
        ).limit(1)
    ) if answered_body is not None else None
    # A warm answer leaves the task parked until delivery is acknowledged. Its mirrored body is not a
    # second unanswered question. A later question_raised note preserves a new escalation, even when it
    # repeats the old wording and has no structured round of its own.
    if task.state == AWAITING_RESPONSE and task.awaiting_response_body and (answered_body is None or newer_question):
        qa_entries.append(
            QAEntryOut(
                question=task.awaiting_response_body,
                # No header and no detail: this entry is parsed from prose (`parse_response_options`).
                header=None,
                options=parse_response_options(task.awaiting_response_body) or [],
                option_details=[],
                resolution=None,
                # No round and no outcome: the legacy `awaiting_response_body` gate writes no `task_questions` row, so
                # there is nothing to Revise.
                question_id=None,
                status="unresolved",
                resolved_at=None,
                # Nobody has settled it — the whole point of this entry.
                resolved_by=None,
            )
        )

    return qa_entries


def _fold_revised_rounds(entries: list[QAEntryOut]) -> list[QAEntryOut]:
    """`entries` with only the NEWEST entry per `question_id` kept, each in its own position. Entries with
    no `question_id` are all kept (the text is not a key).

    One backward walk, then reversed, so the kept entry sits at the last note's position."""
    kept: list[QAEntryOut] = []
    seen: set[int] = set()
    for entry in reversed(entries):
        if entry.question_id is not None:
            if entry.question_id in seen:
                continue
            seen.add(entry.question_id)
        kept.append(entry)
    kept.reverse()
    return kept


def _clear_reopened_resolutions(
    session: Session, task: Task, entries: list[QAEntryOut]
) -> list[QAEntryOut]:
    """`entries` with the resolution stripped off every entry whose round is currently OPEN (see the call
    site).

    One query, only when an entry has a `question_id`. The selector mirrors `_resolved_round_or_409`:
    same `task_id` scoping and soft-delete exclusion."""
    ids = {e.question_id for e in entries if e.question_id is not None}
    if not ids:
        return entries
    open_ids = set(session.scalars(
        select(TaskQuestion.id)
        .join(Task, Task.id == TaskQuestion.task_id)
        .where(
            TaskQuestion.task_id == task.id,
            TaskQuestion.id.in_(ids),
            TaskQuestion.status == "open",
            visible_artifact_clause_joined(TaskQuestion.created_at),
        )
    ).all())
    if not open_ids:
        return entries
    return [
        # The byline goes with the answer, all four fields together, as `revise_question` does on the row:
        # `unresolved` must not still name a resolver. Who withdrew it lives on the `task_note:answer_revised`
        # note. `model_copy` so a projection never edits its rows in place.
        e.model_copy(update={
            "resolution": None, "status": "unresolved", "resolved_by": None, "resolved_at": None,
        })
        if e.question_id in open_ids else e
        for e in entries
    ]


def _note_question_id(value: Any) -> Optional[int]:
    """The `question_id` a Q&A note snapshotted, or None — the same defensive read as every other blob
    value, since these rows are read back for ever.

    `bool` is excluded (`isinstance(True, int)`), or a stray `true` would become round 1 — a wrong id a
    Revise button would post. Non-positive ids are refused (sequence PK)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _task_merge_lane_verdict(session: Session, task: Task) -> Optional[str]:
    """Why the lane did not merge this Task's last PR, for the prompt a reopened runner is re-dispatched with
    (`merge_lane._reopen_settled_runner`): its newest row when `failed`, or `queued` with a conflict note. None
    otherwise (always, in an edition without the lane: it keeps no rows). Raw text: the composer defangs it."""
    if not edition_features.enabled("merge_lane"):
        return None
    from app import merge_lane

    row = session.scalars(
        select(MergeLaneRow).where(MergeLaneRow.task_id == task.id)
        .order_by(MergeLaneRow.created_at.desc(), MergeLaneRow.id.desc()).limit(1)
    ).first()
    if row is None:
        return None
    if row.state == merge_lane.FAILED:
        failure = row.failure or {}
        return (f"PR #{row.pr_number} ({row.pr_url}), failure kind: {failure.get('kind')}\n"
                + merge_lane.failure_message(
                    row, tree=None, tests=list(failure.get("tests") or []), excerpt=failure.get("excerpt") or ""))
    if row.state == merge_lane.QUEUED and (row.note or "").startswith("Conflicts with"):
        return f"PR #{row.pr_number} ({row.pr_url}): {row.note}"
    if row.state == merge_lane.CLOSED and row.note == merge_lane.NOTE_LANE_STOPPED:
        return (f"PR #{row.pr_number} ({row.pr_url}): the merge lane was stopped before it merged this PR. Bring "
                "it up to date and deliver it again: if the lane is on (your instructions say so), report it with "
                "`task_pr_queue`; otherwise run the full gate, merge it yourself and settle.")
    return None


def _task_qa_trail(session: Session, task: Task) -> Optional[str]:
    """A compact plain-text transcript of this Task's resolved Q&A, from the SAME durable notes
    `_task_qa_entries` reads. The per-persona composer folds it into the analysis prompt so a runner
    sees the human's prior answers as DB-sourced context. None when there is none.

    Includes the options offered, not just `Q:`/`A:`. The one defanging caller: this reaches a runner,
    so agent-authored resolutions are stripped of delimiters (see `_task_qa_entries`), and every string
    this renderer adds — header, label, description, preview, and the question — enters defanged and
    flattened (`_prompt_option_text`)."""
    # (is_operator_authored, line): only `A:` lines are the human's words; only agent-authored lines may be
    # clipped to fit (`_fit_qa_trail`).
    lines: list[tuple[bool, str]] = []
    for entry in _task_qa_entries(session, task, defang_for_prompt=True):
        if entry.status == "dismissed":
            # Dismissals are not in the prompt: this section is settled rulings a relaunched runner must not re-ask,
            # and a `Q:` without an `A:` is a shape it never has. Telling a runner "declined" is a separate change.
            continue
        if entry.question:
            header = _prompt_option_text(entry.header)
            # Defanged and flattened like every agent-authored string: a question with a newline plus `A: <ruling>`
            # would forge a human answer inside this trail, which a trusted restart line tells the runner is settled.
            # `defang_fence_markers` does not cover `Q: `/`A: `. No option clamp: `_fit_qa_trail` bounds the line.
            question = _flatten(_neutralize_batch_delimiters(entry.question))
            lines.append(
                (False, f"Q: [{header}] {question}" if header else f"Q: {question}"))
        if entry.option_details:
            lines.append((False, "   Options offered:"))
            for position, option in enumerate(entry.option_details, start=1):
                label = _prompt_option_text(option.label) or ""
                # The suffix test runs on the raw label (defanged reads `(Recommended]`), so it is never said twice.
                if option.recommended and not _label_marks_recommended(option.label):
                    label = f"{label} (recommended)"
                description = _prompt_option_text(option.description)
                lines.append((
                    False,
                    f"     {position}. {label}" + (f" — {description}" if description else ""),
                ))
                # Folded to one line: a multi-line preview would sit at column 0 and forge the `Q:`/`A:` shape.
                preview = _prompt_option_text(option.preview)
                if preview:
                    lines.append((False, f"        preview: {preview}"))
        if entry.resolution:
            # Tagged by who wrote it: a clicked option is runner text a human selected, so clippable; only typed
            # text is exempt from `_QA_TRAIL_MAX_CHARS` (see `QAEntryOut.machine_authored`).
            lines.append((not entry.machine_authored, f"A: {entry.resolution}"))
    return _fit_qa_trail(lines)


def _share_budget(lengths: list[int], budget: int) -> list[int]:
    """How many characters each of `lengths` may keep when they must together fit `budget` — an equal
    share, with whatever a SHORTER string does not need handed back to the longer ones.

    Shortest-first makes it exact: each takes the lesser of its length and an even cut of what remains,
    so slack is re-divided. A flat `budget // n` strands short strings' slack."""
    allowed = [0] * len(lengths)
    remaining = budget
    for rank, index in enumerate(sorted(range(len(lengths)), key=lambda i: lengths[i])):
        take = min(lengths[index], remaining // (len(lengths) - rank))
        allowed[index] = take
        remaining -= take
    return allowed


# The smallest budget worth attempting for an agent line: all but `Q:`/`A:` are indented (3, 5 or 8
# spaces), so less clips to whitespace and drops. Bounding the line count by it keeps a huge trail's
# section populated (see `_fit_qa_trail`).
_MIN_AGENT_LINE_CHARS = 24


def _fit_qa_trail(lines: list[tuple[bool, str]]) -> Optional[str]:
    """The tagged trail joined, and — only if it exceeds `_QA_TRAIL_MAX_CHARS` — fitted to that bound by
    clipping the AGENT-AUTHORED lines alone.

    The operator's answers are never cut: on the cold-resume path nothing else restates them. The
    runaways the cap targets are runner-authored option blobs. A section within the bound is returned
    byte-for-byte.

    The bound yields to that rule: answers alone may exceed it, and then every agent line is dropped.
    Truncation is marked in `_PREREQ_OUTPUT_MAX_CHARS`'s spelling. Lines are elided from the tail, so
    the newest rounds lose options first while every `A:` survives.

    Exact bound: when the operator's lines fit, the result is at most `_QA_TRAIL_MAX_CHARS` plus the
    truncation marker's length; otherwise unbounded, by the rule above."""
    trail = "\n".join(text for _is_operator, text in lines).strip()
    if len(trail) <= _QA_TRAIL_MAX_CHARS:
        return trail or None
    # `+ 1` per line for the joining newline, so the operator's share is its real cost.
    operator_cost = sum(len(text) + 1 for is_operator, text in lines if is_operator)
    agent_lengths = [len(text) for is_operator, text in lines if not is_operator]
    # Structure is charged as each line is emitted, not reserved: `_share_budget` divides text, but each
    # line also costs its `\n` and, if clipped, its `…`. A flat up-front reservation charges dropped lines
    # too and, past `_QA_TRAIL_MAX_CHARS / 2` lines, empties the section. A running `spent` keeps
    # `operator_cost + spent <= _QA_TRAIL_MAX_CHARS` by construction.
    text_budget = max(0, _QA_TRAIL_MAX_CHARS - operator_cost)
    # How many agent lines are worth attempting: an even split over tens of thousands of indented lines
    # starves each below its indent, and every line would vanish. So the line count is bounded first by
    # what each line actually needs (its own length, up to the minimum); the walk stops at the first line
    # that would not fit and elides the rest.
    considered = 0
    reserved = 0
    for length in agent_lengths:
        need = min(length + 1, _MIN_AGENT_LINE_CHARS)
        if reserved + need > text_budget:
            break
        reserved += need
        considered += 1
    allowed = _share_budget(agent_lengths[:considered], text_budget)
    fitted: list[str] = []
    clipped = False
    agent_seen = 0
    spent = 0
    for is_operator, text in lines:
        if is_operator:
            fitted.append(text)
            continue
        index = agent_seen
        agent_seen += 1
        if index >= considered:
            clipped = True
            continue
        # Charged only for what a line actually emits; charging dropped lines drained the budget.
        room = text_budget - spent
        if room <= 0:
            clipped = True
            continue
        if allowed[index] >= len(text) and len(text) + 1 <= room:
            fitted.append(text)
            spent += len(text) + 1          # the joining newline
            continue
        # Clipped: `- 2` reserves this line's own newline and the `…` that says it was cut.
        keep = min(allowed[index], room - 2)
        clipped = True
        # A line with nothing legible left is dropped rather than emitted as a stub. Tested on the clipped text:
        # `"   Options offered:"[:2]` is two spaces, which `rstrip`s to nothing.
        clipped_text = text[:keep].rstrip() if keep > 0 else ""
        if not clipped_text:
            continue
        fitted.append(clipped_text + "…")
        spent += len(clipped_text) + 2
    trail = "\n".join(fitted).strip()
    if clipped:
        trail += "\n…[truncated]"
    return trail or None


# Anti-runaway aggregate cap for the whole fenced Q&A section, the third section cap beside
# `_PREREQ_OUTPUT_MAX_CHARS` and `dispatch_compose.recon_map._RECON_MAP_MAX_CHARS`; this section grows with round
# count. A dozen fully-described rounds render ~20.7 KB (its test asserts above 18 KB and under this);
# runaways (~86-216 KB) come from option blobs, and `routes/agent/task_terminal.py::TaskQuestionIn.options` has no
# `max_items`. Spent on agent text only via `_fit_qa_trail`: operator `A:` lines are exempt and may push
# past it. Truncation is marked.
_QA_TRAIL_MAX_CHARS = 65536

# Anti-runaway per-string cap on agent-authored option text in a runner's prompt; 600 chars is several
# times the longest real description. It bounds one string, not the section (`_QA_TRAIL_MAX_CHARS` does).
# `_flatten_and_truncate` appends `…`, so a clipped value is 601 characters. The delivery path does not
# use it: there the text is a human's answer, which never gets a ceiling.
_QA_TRAIL_OPTION_TEXT_MAX_CHARS = 600


def _prompt_option_text(value: Optional[str]) -> Optional[str]:
    """One agent-authored option string as it may enter a RUNNER'S PROMPT: delimiters neutralized, folded
    to a single line, clamped. None for absent or blank input, so no empty scaffolding renders.

    Defang before folding: `_neutralize_batch_delimiters` turns Cc/Cf into spaces, which folding then
    collapses."""
    if not value:
        return None
    neutralized = _neutralize_batch_delimiters(value)
    return _flatten_and_truncate(neutralized, _QA_TRAIL_OPTION_TEXT_MAX_CHARS) or None


def _operator_labels(session: Session, ids: Iterable[Optional[UUID]]) -> dict[UUID, str]:
    """`operator_label` for MANY actor ids at once — the resolver for every "…by First Last" byline.

    One query (the callers are trail projections). Not `actors.actor_names`, which returns the
    entity-shape `name` that may default to the email (migration 0105); `operator_label` prefers
    first/last, matching the Slack card. Missing or unlabelable ids are absent; NULLs dropped."""
    wanted = {actor_id for actor_id in ids if actor_id is not None}
    if not wanted:
        return {}
    users = session.scalars(select(AdminUser).where(AdminUser.id.in_(wanted))).all()
    labels: dict[UUID, str] = {}
    for user in users:
        label = operator_label(user)
        if label:
            labels[user.id] = label
    return labels


# No truncation on this path (a cut answer lies); delivery is a bracketed paste
# (`apps/jaahd/answer_delivery.py`), so length and newlines are safe. `MAX_TASK_TEXT_LEN` bounds storage.
# These delimiters are neutralized in agent-authored halves only, or a runner could forge a resolved
# round. `'` guards the quoted single-round frame; substitution runs after NFKC (fullwidth `＇`); `;`
# stays though rounds are newline-joined. Removing an entry hands a delimiter back to the forger.
_BATCH_DELIMITER_SUBSTITUTIONS = {";": ",", ")": "]", "→": "-", "'": "’"}
# Categories `_neutralize_batch_delimiters` folds to a space: `Cc`/`Cf` (controls, zero-width formatters,
# newlines) plus `Zl`/`Zp` (`U+2028`/`U+2029`, which break lines too), so the helper is self-sufficient
# without `_flatten`.
_NEUTRALIZED_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")


def _neutralize_batch_delimiters(value: str) -> str:
    """Agent-authored text with the payload's structural delimiters replaced (see
    `_BATCH_DELIMITER_SUBSTITUTIONS`). NFKC first, so fullwidth delimiters and a fullwidth or
    ideographic-space comment label are caught.

    `_COMMENT_JOINER` is a phrase the table cannot express; an option could otherwise reproduce the
    operator's own labelled voice. So the label is rewritten, broadly (any dash, case, plural), since the
    reader is a language model. The Cc/Cf fold is part of the match: a zero-width space would slip past
    and `_scrub_control_chars` would later reassemble the label, so folding here makes the output
    scrub-stable.

    It raises the cost, it does not draw a boundary: paraphrases and homoglyphs still pass. The price is
    cosmetic hyphenation of legitimate prose."""
    folded = "".join(
        " " if unicodedata.category(ch) in _NEUTRALIZED_CATEGORIES
        else _BATCH_DELIMITER_SUBSTITUTIONS.get(ch, ch)
        for ch in unicodedata.normalize("NFKC", value or "")
    )
    return _COMMENT_LABEL_RE.sub(_NEUTRALIZED_COMMENT_LABEL, folded)


def _flatten(value: str) -> str:
    """One line, whole — every run of whitespace collapsed to a single space, NOTHING cut.

    Agent-authored halves only. Not by itself the forgery guard: the Cc/Cf fold in
    `_neutralize_batch_delimiters` removes newlines (and `str.split` does not touch `ESC`); this
    collapses the space runs the fold leaves. Split from `_flatten_and_truncate`: delivery takes no
    limit, while agent option text in a prompt does (`_QA_TRAIL_OPTION_TEXT_MAX_CHARS`)."""
    return " ".join((value or "").split())


def _flatten_and_truncate(value: str, limit: int) -> str:
    """`_flatten`, then a hard cut at `limit` with an `…`. AGENT-AUTHORED TEXT ONLY — see
    `_QA_TRAIL_OPTION_TEXT_MAX_CHARS` for the one caller and its bound."""
    flat = _flatten(value)
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"
