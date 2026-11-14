"""A task's language: the language its description's prose is written in, an ISO 639-1 code on `tasks.language`.

Detected by the title model in the same call that writes the title (`app/session_summary.py::titled_description`,
`app/task_title.py::refine_title`); a missing reply stores `DEFAULT_LANGUAGE`. Re-detected only by "Re-generate
title", and never over a language a person picked (`tasks.language_pinned`). Editable on a top-level task, and
every write covers the whole subtree, so epic children always carry their epic's language
(`routes/tasks/update.py::_apply_language`); children are born with it.

What follows it: the runner's questions, reports, artifacts and summary (`language_directive` in the dispatch
prompt). Code, commits, PRs and repo docs stay
in the repository's own language. NULL (a row older than detection) reads as `DEFAULT_LANGUAGE`.
"""
from __future__ import annotations

import re

#: The language a task gets when the title model gives none (no key, a failure, an unparsable reply).
DEFAULT_LANGUAGE = "en"

#: An ISO 639-1 code (two letters), or a 639-2/3 code for a language 639-1 lacks: lowercase ASCII only, so the
#: value can be spliced into a prompt line without fencing.
_CODE_RE = re.compile(r"^[a-z]{2,3}$")


def normalized_language(value: str | None) -> str | None:
    """`value` as a stored code (trimmed, lowercased, a region suffix such as `pt-BR` cut), or None if it is
    not one."""
    if value is None:
        return None
    code = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return code if _CODE_RE.match(code) else None


def effective_language(value: str | None) -> str:
    """The language a row is treated as having: its own, else `DEFAULT_LANGUAGE`."""
    return normalized_language(value) or DEFAULT_LANGUAGE


def language_directive(value: str | None) -> str:
    """The dispatch prompt's line for a task in another language than `DEFAULT_LANGUAGE`; empty for it, so an
    English task's prompt is unchanged."""
    code = effective_language(value)
    if code == DEFAULT_LANGUAGE:
        return ""
    return (
        f"Task language: `{code}` (ISO 639-1). Write in this language every question you ask (AskUserQuestion: "
        "question, header and options), every report and artifact you upload, and your settle summary. Code, "
        "code comments, commit messages, PR titles and bodies, and repository docs stay in the repository's own "
        "language.\n\n"
    )
