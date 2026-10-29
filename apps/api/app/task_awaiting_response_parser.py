"""Pure-function parser for the runner-charter BLOCK step's options convention.

Splits a `Task.awaiting_response_body` (the raw `🛑 FLEET BLOCKED` comment) into question prose and
options; the convention is defined in `portfolio/fleet/runner-charter.md`. No I/O, never raises: any
absent or malformed options block degrades to "no options", never a partial parse. Both public functions
delegate to one `_split` so where the block starts is decided once (the Slack card needs the halves apart).
"""
from __future__ import annotations

import re

__all__ = ["parse_response_options", "question_prose"]

_SENTINEL_RE = re.compile(r"^\*\*Options:\*\*[ \t]*$", re.MULTILINE)
_LIST_ITEM_RE = re.compile(r"^(?:[-*]|\d+[.)])\s+(.+?)\s*$")


def _split(comment_body: str | None) -> tuple[str, list[str] | None]:
    """The single parse both public functions read: `(prose, options)`.

    `options` is `None` for every degraded shape, a non-empty list only for a well-formed trailing
    block. `prose` is the comment minus that block, else the whole comment; a comment that is only an
    options block yields `("", [...])`, a real answer rather than a parse failure.
    """
    if not comment_body:
        return "", None

    # Normalize CRLF so the line-anchored regexes don't see a trailing \r.
    comment_body = comment_body.replace("\r\n", "\n")

    sentinel_match = _SENTINEL_RE.search(comment_body)
    if not sentinel_match:
        return comment_body.strip(), None

    remainder_lines = comment_body[sentinel_match.end():].splitlines()
    n = len(remainder_lines)
    idx = 0

    # Blank lines between the sentinel and the first option are fine.
    while idx < n and not remainder_lines[idx].strip():
        idx += 1

    options: list[str] = []
    while idx < n:
        item_match = _LIST_ITEM_RE.match(remainder_lines[idx].strip())
        if not item_match:
            break
        options.append(item_match.group(1).strip())
        idx += 1

    if not options:
        return comment_body.strip(), None

    # Any remaining non-blank content after the list is the whole-comment fallback trigger.
    if any(line.strip() for line in remainder_lines[idx:]):
        return comment_body.strip(), None

    return comment_body[: sentinel_match.start()].strip(), options


def parse_response_options(comment_body: str | None) -> list[str] | None:
    """Parse a `🛑 FLEET BLOCKED` comment body for the runner-charter's options convention.

    Returns the ordered options when the comment ends in a well-formed block: a line reading exactly
    `**Options:**`, then (after blank lines) one or more markdown list items, then only blank lines.
    Prose before the sentinel never affects the result.

    Returns `None` for no sentinel, a sentinel without list items, or non-blank content after the
    block. Never raises.
    """
    return _split(comment_body)[1]


def question_prose(comment_body: str | None) -> str:
    """The question half: the body minus a well-formed trailing `**Options:**` block, stripped.

    Wherever `parse_response_options` returns `None` this returns the comment unchanged (bar the strip),
    so a malformed block stays in the prose rather than being cut at a guessed start. `""` for an
    absent or blank body.
    """
    return _split(comment_body)[0]
