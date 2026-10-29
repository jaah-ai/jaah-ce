"""A runner's question round reworded in plain terms: `plain_question` asks the title model, `reword_question`
writes the result onto `task_questions.plain`, and `stored_plain` reads it back for the console.

DISPLAY ONLY. An answer is exact-matched against the round's stored `options` (`routes/tasks/answers.py`), so
nothing here may change what an answer posts: the reworded options are index-aligned with the served
`option_details` and carry no labels of their own.

Enqueued after the round's commit by `app/durable/question_plain_queue.py`. Every failure (no key, timeout, a
reply that breaks the shape) is no rewording: the console shows the runner's own words.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

# Bound to a local name, as `app/session_summary.py` does, so tests patch `app.question_plain._summary_llm`.
from app.llm_client import NO_THINKING_OPTIONS, first_text, summary_llm as _summary_llm

log = logging.getLogger("question-plain")

# Stripped before the bound is checked; a reply over a bound is refused whole, never clipped mid-word.
_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PlainOption(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    short: Annotated[_Text, StringConstraints(max_length=40)]
    gist: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]] = None


class QuestionPlain(BaseModel):
    """The stored and served shape. `options` is index-aligned with the round's `option_details`; [] for a
    free-text round."""

    model_config = ConfigDict(extra="forbid", strict=True)

    headline: Annotated[_Text, StringConstraints(max_length=120)]
    why: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]] = None
    options: list[PlainOption] = Field(default_factory=list)


_PROMPT = (
    "A coding agent asked a person the question below. Reword it so someone who is not an engineer "
    "understands what is being decided.\n"
    "Reply with ONE JSON object and nothing else:\n"
    '{{"headline": "<the question as one plain sentence, at most 120 characters>", '
    '"why": "<one sentence of context, at most 200 characters, or null>", '
    '"options": [{{"short": "<the option in at most 40 characters>", '
    '"gist": "<what choosing it means, at most 120 characters, or null>"}}]}}\n'
    "Rules: exactly {count} entries in \"options\", in the order given ([] when there are none). Use only what "
    "the question says; never invent facts. Avoid code identifiers, file paths and jargon unless the decision "
    "cannot be understood without them. Be shorter than the question: \"headline\" and \"why\" together must "
    "not be longer than it. \"why\" never repeats the headline; use null when the headline says enough.\n\n"
    "QUESTION:\n{question}"
)


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    return value or None


def parse_plain(text: str, option_count: int) -> Optional[QuestionPlain]:
    """The first JSON object in `text` as a `QuestionPlain` with exactly `option_count` options, else None.

    `raw_decode` from the first `{`, as `app/task_classifier.py::_decode_verdict` reads its reply (a markdown
    fence or a preamble is skipped). Blank `why`/`gist` normalize to None (absence-is-not-a-state)."""
    try:
        data, _end = json.JSONDecoder().raw_decode(text, text.index("{"))
        plain = QuestionPlain.model_validate(data)
    except (ValueError, RecursionError, ValidationError) as exc:
        # `ValidationError` is a `ValueError`; named for the reader. The class only: the text is the model's.
        log.warning("question-plain: the reply is not the expected JSON shape (%s)", type(exc).__name__)
        return None
    if len(plain.options) != option_count:
        log.warning(
            "question-plain: the reply reworded %d option(s) for a round of %d", len(plain.options), option_count,
        )
        return None
    plain.why = _blank_to_none(plain.why)
    for option in plain.options:
        option.gist = _blank_to_none(option.gist)
    return plain


def plain_question(
    question: str, *, header: Optional[str], options: Optional[list[tuple[str, Optional[str]]]],
) -> Optional[QuestionPlain]:
    """Reword one round. `options` are the served `(label, description)` pairs in order; None for free text.

    None on an absent key/SDK, an empty question, any LLM failure, or a reply `parse_plain` refuses; never
    raises (the client and its timeout are `summary_llm`'s, as for titles)."""
    llm = _summary_llm()
    if llm is None or not question.strip():
        return None
    offered = options or []
    payload = {
        "header": header,
        "question": question,
        "options": [{"label": label, "description": description} for label, description in offered],
    }
    try:
        message = llm.client.messages.create(
            model=llm.model,
            # No reasoning needed; `session_summary.title_from_description` says why this pair.
            **(llm.request_options or NO_THINKING_OPTIONS),
            messages=[{"role": "user", "content": _PROMPT.format(
                count=len(offered), question=json.dumps(payload, indent=2, ensure_ascii=False),
            )}],
        )
        # Which provider and model the text went to, never the text.
        log.info("question-plain: sent to %s (model=%s)", llm.provider, llm.model)
        return parse_plain(first_text(message), len(offered))
    except Exception as exc:
        # The class and message only; the exception text never carries the key.
        log.warning("question-plain: LLM call failed: %s: %s", type(exc).__name__, exc)
        return None


def stored_plain(value: Any) -> Optional[QuestionPlain]:
    """The `plain` column as served: None when absent or malformed, never a raise (a bad blob must not 500
    the task page)."""
    if value is None:
        return None
    try:
        return QuestionPlain.model_validate(value)
    except ValidationError:
        return None


def reword_question(question_id: int) -> bool:
    """Write `plain` on one `task_questions` row. True when written.

    Idempotent: a deleted row or one already reworded is nothing to do (a recovered run does not re-pay the
    vendor). Status is not checked: a round answered meanwhile still reads better in the trail. No connection is
    held across the vendor call, as in `app/task_title.py::refine_title`. Raises only on a database failure (the
    step's retry)."""
    # Deferred: `routes/tasks/` imports this module for `QuestionPlain`.
    from app.db import SessionLocal
    from app.events import emit_console_event
    from app.models.tasks import TaskQuestion
    from app.routes.tasks.qa_render import _question_option_details

    with SessionLocal() as session:
        row = session.get(TaskQuestion, question_id)
        if row is None or row.plain is not None:
            return False
        question, header = row.question, row.header
        # The served projection, so the reworded options align with `option_details` index for index.
        details = _question_option_details(row.options)
        session.rollback()

        options = None if details is None else [(detail.label, detail.description) for detail in details]
        plain = plain_question(question, header=header, options=options)
        if plain is None:
            return False

        locked = session.get(TaskQuestion, question_id, with_for_update=True)
        if locked is None or locked.plain is not None:
            return False
        locked.plain = plain.model_dump()
        # A live signal, not a transition; an open task page re-reads its questions.
        emit_console_event(
            session, entity="task", id=locked.task_id, action="question_reworded", record_transition=False,
        )
        session.commit()
        log.info("question-plain: round %s reworded", question_id)
        return True
