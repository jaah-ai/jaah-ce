"""Where a task's effective memory arm came from (`project_memory/switch.py::effective_memory`): the closed set
`tasks.memory_arm_source`'s CHECK renders from. Stdlib-only, so the task model reads it in either edition; the
community edition, which ships no project memory, keeps the column and its CHECK."""

from __future__ import annotations

ARM_SOURCES: tuple[str, ...] = ("task", "epic", "default", "holdout", "killed", "plan")
