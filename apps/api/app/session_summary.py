"""LLM title generation for epics and Tasks: `title_from_description` turns a create-form Description into
a short Title, for `POST /api/tasks` (`routes/tasks/`) and `automation_fire.py`; `titled_description` also names the
description's language (`app/task_language.py`).

The module name is historical; it holds only the title helper. The provider client and its config live in
``app/llm_client.py``.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import NamedTuple

# `summary_llm` is bound to the local name `_summary_llm` because tests patch
# `app.session_summary._summary_llm`; the call below stays unqualified so those stubs take effect.
from app.llm_client import NO_THINKING_OPTIONS, first_text, image_block, reads_images, summary_llm as _summary_llm

# `sanitize_title` is re-exported because tests assert the sanitize-then-clip invariant against
# `session_summary.sanitize_title`.
from app.short_title import keep_leading_tag, link_only_title, sanitize_title, short_title  # noqa: F401  (sanitize_title: deliberate re-export)
from app.task_language import normalized_language

log = logging.getLogger("session-summary")

_TITLE_PROMPT = (
    "Write a short title (5-6 words) for the task described below.\n"
    "Name the actual subject so a reader seeing only the title can tell which task it is — never a "
    "mid-sentence fragment of the description.\n"
    "Title what the DESCRIPTION asks. Screenshots, when any follow it, only show what the description refers "
    "to, and often show another task's page or list: never copy a title, heading or breadcrumb shown in an image.\n"
    "Use only what the description and any attached images show. Never name a technology, product, "
    "language, framework or tool they do not; when they name no subject, say what is asked in plain words.\n"
    "Write the title in the language the description's own prose is written in — not the language of its "
    "screenshots, attachments, code or quoted text.\n"
    "Output the title on the first line — no quotes, no markdown, no preamble, no trailing punctuation — and on "
    "the second line `LANGUAGE: ` followed by the ISO 639-1 code of that language. Nothing else.\n\n"
    "DESCRIPTION:\n{description}"
)
#: The reply's second line: the description's language (`app/task_language.py`).
_LANGUAGE_LINE_RE = re.compile(r"^\W*language\W*:\s*`?([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)?)`?\W*$", re.IGNORECASE)
#: Between the description and its screenshots: what the pictures are, so the model reads them as context.
_TITLE_IMAGES_LEAD_IN = "SCREENSHOTS attached to the description (context only, not the subject):"
#: The last block, after any screenshots, so the description and not the last picture is what the reply answers.
_TITLE_CUE = "TITLE and LANGUAGE (of the description above):"

#: A description of at least this many words names its own subject, so it is titled from its text alone. Sent
#: anyway, a screenshot of another task's page handed the model that task's title to copy: a ~100-word report
#: that another task "is in a broken state. See screenshot" wore the screenshotted task's title.
#: A shorter body still gets its screenshot; there only the order and `_TITLE_CUE` keep the text in charge.
TEXT_ONLY_TITLE_WORDS = 12


def title_needs_images(description: str) -> bool:
    """Does `description` leave its subject to its screenshots ("Fix that visually")? Counted on the prose
    `sanitize_title` keeps, so an inline image or link reference adds no words."""
    return len(sanitize_title(description).split()) < TEXT_ONLY_TITLE_WORDS


# A reply that talks to the user instead of naming the task ("I can see you've provided a link", "Sure, here
# is…", "It looks like…"): a chat answer, never a title.
_CONVERSATIONAL_REPLY_RE = re.compile(
    r"^(?:i(?:'m|'ll|'d|'ve|\s+(?:am|can|cannot|can't|could|don't|see|will|would|need|notice|understand)\b)"
    # An interjection counts only before its punctuation, so "OK button misaligned" or "Hi-DPI icons" is a title.
    r"|(?:sure|certainly|of course|absolutely|okay|ok|sorry|hello|hi|hey|thanks|thank you)[,.!]"
    r"|unfortunately\b"
    r"|it (?:looks|seems|appears)\b|looks like\b|seems like\b|you(?:'ve|'re)\b"
    r"|here(?:'s|\s+is)\b|let me\b|could you\b|can you\b|please (?:provide|share|clarify)\b)",
    re.IGNORECASE,
)


def _unquoted(line: str) -> str:
    """The reply's title without the quotes the model wrapped it in (`"Fix the picker"`) or a stray one at an edge;
    a quoted phrase inside it is the title's own (`Add a line "Last live check"`) and keeps both of its marks."""
    if len(line) >= 2 and line[0] == line[-1] == '"' and line[1:-1].count('"') % 2 == 0:
        return line[1:-1].strip()
    if line.count('"') % 2:
        # One mark too many: a stray edge quote is the model's, not the title's.
        if line.startswith('"'):
            return line[1:].strip()
        if line.endswith('"'):
            return line[:-1].strip()
    return line


def is_conversational_reply(text: str) -> bool:
    """Does the model's reply open like a chat answer rather than a title? Typographic apostrophes count."""
    return bool(_CONVERSATIONAL_REPLY_RE.match(text.strip().strip('"').replace("\u2019", "'")))


class TitledDescription(NamedTuple):
    """The title model's answer: the title, and the description's language when the reply named one."""

    title: str
    language: str | None


def title_from_description(
    description: str, *, fallback: str | None = None, images: Sequence[tuple[str, bytes]] = (),
) -> str:
    """`titled_description`'s title alone."""
    return titled_description(description, fallback=fallback, images=images).title


def titled_description(
    description: str, *, fallback: str | None = None, images: Sequence[tuple[str, bytes]] = (),
) -> TitledDescription:
    """Auto-generate a task Title from its Description (the create form has no Title field), in the language of
    the description's prose, and name that language (`language`, None when the reply did not).

    `images` are the task's attached screenshots as `(sniffed media type, bytes)`, sent after the text and only
    when the description is too short to name its subject (`title_needs_images`): a body like "Fix that
    visually" names its subject only in the picture, and without it the model invents one.

    Falls back to `fallback` (default: the deterministic `short_title(description)`) on an absent key/SDK,
    an empty description, or any LLM failure; never raises. The reply goes through `short_title`, so an
    over-long model reply cannot leak through — the cap cannot live in the prompt alone. A conversational reply
    (`is_conversational_reply`) is discarded for the fallback.

    A body that is only a link (`link_only_title`) is titled from the link with no call: the model answers such a
    body with chat ("I can see you've provided a link") and cannot open the link anyway."""
    linked = link_only_title(description)
    if linked:
        return TitledDescription(keep_leading_tag(linked, description), None)
    fallback = short_title(description) if fallback is None else fallback
    kept = TitledDescription(fallback, None)
    llm = _summary_llm()
    if llm is None or not (description or "").strip():
        return kept
    try:
        # Screenshots only to a model that reads them: DeepSeek's v4-pro guesses at image input it is
        # billed for (measured) — how a "Fix that visually" body invented its subject before.
        sent_images = images if reads_images(llm.provider, llm.model) and title_needs_images(description) else ()
        # The request first, then its screenshots under a lead-in, then the cue: a picture ahead of the text
        # (or last before the reply) is what the model titled.
        content = [{"type": "text", "text": _TITLE_PROMPT.format(description=description)}]
        if sent_images:
            content += [
                {"type": "text", "text": _TITLE_IMAGES_LEAD_IN},
                *(image_block(media_type, body) for media_type, body in sent_images),
            ]
        content.append({"type": "text", "text": _TITLE_CUE})
        message = llm.client.messages.create(
            model=llm.model,
            # A title needs no reasoning; `NO_THINKING_OPTIONS` turns DeepSeek's thinking off and keeps a
            # thinking-sized cap for a model that ignores the switch (measured). A model that
            # always thinks takes its own options instead (`SupportLLM.request_options`).
            **(llm.request_options or NO_THINKING_OPTIONS),
            messages=[{"role": "user", "content": content}],
        )
        # Which provider and model the text went to, never the text (it left for a third party).
        log.info("title_from_description: sent to %s (model=%s)", llm.provider, llm.model)
        text = first_text(message)
        if not text:
            log.warning("title_from_description: the model returned no text (stop_reason=%s); keeping the fallback",
                        getattr(message, "stop_reason", None))
            return kept
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # The title is the first line, whatever it says ("Language: fix the picker" is a title); the language
        # only ever a later one.
        language = _reply_language(lines[1:])
        if language is None:
            log.warning("title_from_description: the reply named no language; the task reads as the default")
        if is_conversational_reply(lines[0]):
            log.warning("title_from_description: the model replied conversationally; keeping the fallback")
            return kept
        title = short_title(_unquoted(lines[0].strip()))
        return TitledDescription(keep_leading_tag(title, description) if title else fallback, language)
    except Exception as exc:
        # WARNING, not DEBUG: a title that silently keeps its placeholder looks like a model that stopped
        # working, and the one line that says why must be in the log at the default level. The class
        # and message only; the exception text never carries the key.
        log.warning("title_from_description LLM call failed: %s: %s", type(exc).__name__, exc)
        return kept


def _reply_language(lines: list[str]) -> str | None:
    """The code on the reply's `LANGUAGE:` line, normalized (`app/task_language.py`); None when absent or invalid."""
    for line in lines:
        matched = _LANGUAGE_LINE_RE.match(line)
        if matched:
            return normalized_language(matched.group(1))
    return None
