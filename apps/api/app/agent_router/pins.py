"""`tasks.pin_source` — who pinned a task's runner or model. THE ONE WRITER of the column.

A person's write (the task form: `routes/tasks/create.py`, `routes/tasks/update.py`) stamps `human`; an
automation's (`app/automation_fire.py`: a Runner cron or trigger, which is how a runner's `schedule_create` model
reaches a task) stamps `system`. Either makes the router fall back (`human_pinned` / `system_pinned`). Clearing all
three fields clears the stamp. Never inferred from the fields alone: the claim stamps `bound_provider` itself.
"""
from __future__ import annotations

from app.models.tasks import PIN_HUMAN, PIN_SYSTEM, Task

HUMAN, SYSTEM = PIN_HUMAN, PIN_SYSTEM


def stamp(task: Task, source: str) -> None:
    """Record that `source` just wrote `task`'s runner or model fields: `source` while any of `agent_id`,
    `bound_provider` or `model` is set, else NULL. Does not flush."""
    pinned = task.agent_id is not None or bool(task.bound_provider) or bool(task.model)
    task.pin_source = source if pinned else None
