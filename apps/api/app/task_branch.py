"""A task's branch: `{prefix}fleet/task-<id>`, carrying the `fleet/` segment once whatever the project's prefix.

A project's `branch_prefix` is its push namespace (the guard admits only refs under it), so a `fleet/` prefix stays
stored: only the task branch's spelling drops the prefix's trailing `fleet/` (`task_branch_prefix`). Branches pushed
before that (`fleet/fleet/task-<id>`) still match `runner_branch_pattern`, so their open PRs keep their task.
The node daemon spells the same rule in `apps/jaahd/worktree_mechanics/core.py::task_branch`."""
from __future__ import annotations

import re

#: What every task branch is after its prefix; the task id follows.
TASK_SEGMENT = "fleet/"


def task_branch_prefix(prefix: str | None) -> str:
    """`prefix` without a trailing `fleet/` segment, which the task branch already starts with (`fleet/` → `''`,
    `sn/fleet/` → `sn/`); any other prefix as it is."""
    text = prefix or ""
    if text == TASK_SEGMENT or text.endswith("/" + TASK_SEGMENT):
        return text[: -len(TASK_SEGMENT)]
    return text


def task_branch(prefix: str | None, task_id: object) -> str:
    """The branch a task's runner works on and pushes."""
    return f"{task_branch_prefix(prefix)}{TASK_SEGMENT}task-{task_id}"


def runner_branch_pattern(prefix: str | None) -> str:
    """A regex fullmatching a runner's branch under `prefix`, group 1 the task id: today's spelling, or the doubled
    one an older dispatch pushed (`fleet/fleet/task-<id>`)."""
    spellings = sorted({re.escape(prefix or ""), re.escape(task_branch_prefix(prefix))}, key=len, reverse=True)
    return rf"(?:{'|'.join(spellings)}){re.escape(TASK_SEGMENT)}task-(\d+)"
