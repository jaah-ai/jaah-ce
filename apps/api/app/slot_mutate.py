"""The operator-mutation writer on the slot row — `slot_columns.MUTATED` and nothing else.

The declared exception to the two-writer split (the node's observation writes what it senses, the CP what it
decides); sole writer enforced by `tests/test_slot_two_writers.py`. A Core `UPDATE` naming only its own
columns, not an ORM load-assign-commit, whose flush would overwrite what the ~5s observation committed in between.

`actor_id` is required: this is the only write path of `worktrees.updated_by_id` (a server default
applies only on INSERT). The observation does not write it, so `updated_at` is the last CP write
while `updated_by_id` is the last deliberate one.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import update

from app.models.worktrees import Worktree
from app.node_identity import node_ref


def mutate_statement(
    node_id: str,
    project_id: int,
    number: int,
    *,
    actor_id: UUID,
    last_used: Optional[datetime] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
):
    """``UPDATE worktrees SET <mutated subset> WHERE <identity>`` — or None when nothing was supplied.
    `node_id` is the node uuid.

    Only supplied fields are named; `MUTATED ∩ SENSED == ∅` keeps this from racing the observation.
    `""` is a real value for `name`/`description`, so "not supplied" is `None`. Identity matches on
    the resolved `project_id` so a rename cannot make it write nothing.

    `task_id` is deliberately absent: it belongs to `OCCUPANCY`, bound by `slot_state.lease()`.
    `updated_by_id` is written only if something else is, so no change is claimed that nobody made.
    """
    values: dict[str, object] = {}
    if last_used is not None:
        values["last_used"] = last_used
    if name is not None:
        values["name"] = name
    if description is not None:
        values["description"] = description
    if not values:
        return None
    values["updated_by_id"] = actor_id
    return (
        update(Worktree)
        .where(
            # Uuid resolved inside the statement; a non-uuid or unknown one matches no row and
            # `apply_mutation` reports 0 rows.
            Worktree.node_id == node_ref(node_id),
            Worktree.project_id == project_id,
            Worktree.number == number,
        )
        .values(**values)
    )


def apply_mutation(
    session,
    node_id: str,
    project_id: int,
    number: int,
    *,
    actor_id: UUID,
    last_used: Optional[datetime] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> int:
    """Rows touched — 0 when the slot has no row. Never an upsert: existence has one author (the
    sensor). The count reaches the wire on `SlotHolderOut.mutated`, so a dropped write is reported."""
    statement = mutate_statement(
        node_id,
        project_id,
        number,
        actor_id=actor_id,
        last_used=last_used,
        name=name,
        description=description,
    )
    if statement is None:
        return 0
    return session.execute(statement).rowcount or 0
