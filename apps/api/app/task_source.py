"""Where a Task came from — the single writer of `tasks.source`.

Set at filing and never changed. A top-level row takes its filer's source (`console`, `cron`, `agent`, or
`trigger:<kind>`); a row with a parent epic takes the EPIC's, so filtering the board to one source shows a
whole pipeline (`source_for`). The DB CHECK (migration 0383) lists the same values; `SOURCE_LABELS` is the
only copy of the human labels — the web reads them from `GET /api/tasks/sources`.
"""
from __future__ import annotations

from typing import Optional, Protocol

CONSOLE = "console"
CRON = "cron"
AGENT = "agent"
TRIGGER_PREFIX = "trigger:"
TRIGGER_KINDS = ("webhook", "github", "gmail", "slack", "user_registered")

# Display order: the board's Source filter lists them in this order.
SOURCES = (CONSOLE, CRON, AGENT, *(TRIGGER_PREFIX + kind for kind in TRIGGER_KINDS))

SOURCE_LABELS = {
    CONSOLE: "Console",
    CRON: "Schedule",
    AGENT: "Agent",
    "trigger:webhook": "Webhook",
    "trigger:github": "GitHub",
    "trigger:gmail": "Email",
    "trigger:slack": "Slack",
    "trigger:user_registered": "New user",
}
assert tuple(SOURCE_LABELS) == SOURCES, "every source needs exactly one label"

# Revision 0383's constraint and index, declared on `models.Task`.
TASK_SOURCE_CHECK = "ck_tasks_source"
TASK_SOURCE_CHECK_SQL = "source IN (" + ", ".join(f"'{value}'" for value in SOURCES) + ")"
TASK_SOURCE_INDEX = "ix_tasks_project_id_source"


class _HasSource(Protocol):
    source: str


def validate_source(source: str) -> str:
    """`source`, or ValueError — a programming error at a filing seam, never user input."""
    if source not in SOURCES:
        raise ValueError(f"unknown task source {source!r}; expected one of {SOURCES}")
    return source


def trigger_source(kind: str) -> str:
    """The source a trigger of `kind` files under; refuses an unknown kind."""
    return validate_source(TRIGGER_PREFIX + kind)


def source_for(parent_epic: Optional[_HasSource], filer_source: str) -> str:
    """The one rule: a row under an epic takes the epic's source, otherwise its filer's own. The epic's value is
    copied unchecked: it was validated when the epic was filed, and a code a newer release wrote must not
    stop its children being filed (the DB CHECK stays the gate)."""
    if parent_epic is not None:
        return parent_epic.source
    return validate_source(filer_source)


def source_label(source: str) -> str:
    """The readable label; an unknown value (a newer release's row) reads as itself rather than failing."""
    return SOURCE_LABELS.get(source, source)
