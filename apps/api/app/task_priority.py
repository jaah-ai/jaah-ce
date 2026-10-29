"""The Task priority vocabulary: which named levels a priority int has.

`Task.priority` is a plain int (higher = sooner); the order key is `routes/tasks/policy.py::_PRIORITY_ORDER`
and this module never orders anything. The gaps between stored values let a raw int fall between
levels, and let `level_above` land on a named level whenever one exists.

The web mirrors this table in `apps/web/src/components/tasks/taskPriority.ts`; `test_task_priority.py`
pins the two together.

The API needs it for force-start: the forced task must sort ahead of the task it preempts, or the
re-queued victim re-takes the freed capacity. Bumping to the next named level keeps the value legible."""

from __future__ import annotations

#: `(stored value, operator-facing label)`, ordered HIGH → LOW — the order the selector renders them in.
PRIORITY_LEVELS: tuple[tuple[int, str], ...] = (
    (20, "Urgent"),
    (10, "High"),
    (0, "Normal"),
    (-10, "Low"),
)

#: What an absent/NULL `priority` means — the column's own server default.
DEFAULT_PRIORITY = 0


def priority_label(priority: int | None) -> str:
    """The operator-facing name of a priority: a named level's label, or `P<n>` for a raw int (the web's
    fallback spelling too)."""
    value = DEFAULT_PRIORITY if priority is None else priority
    for stored, label in PRIORITY_LEVELS:
        if stored == value:
            return label
    return f"P{value}"


def level_above(priority: int | None) -> int:
    """The smallest named level strictly above `priority`, else `priority + 1`. Normal (0) yields High (10);
    Urgent (20) yields 21."""
    value = DEFAULT_PRIORITY if priority is None else priority
    above = [stored for stored, _label in PRIORITY_LEVELS if stored > value]
    return min(above) if above else value + 1


def sorts_ahead(a_priority: int | None, a_id: int, b_priority: int | None, b_id: int) -> bool:
    """Whether row A dispatches before row B under the dispatcher's order key, `priority DESC, id`. NULL reads
    as the default, as `_PRIORITY_ORDER`'s `coalesce` does."""
    a = DEFAULT_PRIORITY if a_priority is None else a_priority
    b = DEFAULT_PRIORITY if b_priority is None else b_priority
    if a != b:
        return a > b
    return a_id < b_id
