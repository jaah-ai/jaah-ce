"""Tag-assignment rules shared by every surface that reads a Task's tags.

A drafted `tag_groups` row is invisible: its assignments stay in `task_tags` but are absent from every
payload, so the rule is one query predicate here, never a delete (two copies would let one surface
serve a group another hides). This module is the one writer of `task_tags` and the batch loader
every payload reads chips through.

An assignment never touches the task, so `tasks.updated_at` never moves: the web edit form is keyed on
`${task.id}:${task.updated_at}` and a stamped tag write would discard the operator's unsaved edits.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from app.models.task_extras import Tag, TagGroup, TaskTag
from app.models.tasks import Task

# The published value of `ck_tag_groups_state`, named so it is not confused with `Task.state`'s `active`.
TAG_GROUP_ACTIVE = "active"


def active_tag_ids_select() -> Select:
    """`select(Tag.id)` narrowed to tags whose group is active — the tag vocabulary every payload may show.

    Used as a subquery predicate (`TaskTag.tag_id.in_(...)`) to drop drafted groups' assignments, or
    executed as the vocabulary itself, e.g. to zero-fill a per-tag count map.
    """
    return (
        select(Tag.id)
        .join(TagGroup, TagGroup.id == Tag.tag_group_id)
        .where(TagGroup.state == TAG_GROUP_ACTIVE)
    )


class TagRefOut(BaseModel):
    """One tag on a task, denormalized with its group so a chip renders from the row alone.

    The board sorts chips by `(group_position, position)`; a tag editor needs `group_multi_select`.
    """

    id: UUID
    tag: str
    name: str
    group_id: UUID
    group_name: str
    group_multi_select: bool
    group_position: int
    position: int


def _active_ref_select():
    """The one SELECT that defines a tag ref: an assignment whose group is active, joined to both endpoints,
    in the operator's order.

    Order is contract: the board renders the first N chips, so an unordered result reshuffles on every
    SSE refetch. `name` tie-breaks `position`, which defaults to 0.
    """
    return (
        select(
            TaskTag.task_id,
            Tag.id,
            Tag.tag,
            Tag.name,
            Tag.position,
            TagGroup.id,
            TagGroup.name,
            TagGroup.multi_select,
            TagGroup.position,
        )
        .join(Tag, Tag.id == TaskTag.tag_id)
        .join(TagGroup, TagGroup.id == Tag.tag_group_id)
        .where(TagGroup.state == TAG_GROUP_ACTIVE)
        .order_by(
            TaskTag.task_id,
            TagGroup.position,
            TagGroup.name,
            Tag.position,
            Tag.name,
        )
    )


def tag_refs_for(session: Session, task_ids: list[int]) -> dict[int, list[TagRefOut]]:
    """Every listed task's active-group tags, in one grouped query — the shared batch loader.

    One query per page, never per row (called from the task list route). A task with no tags is absent
    from the mapping. An empty `task_ids` issues no query.
    """
    if not task_ids:
        return {}
    refs: dict[int, list[TagRefOut]] = {}
    rows = session.execute(_active_ref_select().where(TaskTag.task_id.in_(task_ids)))
    for (
        task_id,
        tag_id,
        tag_code,
        tag_name,
        tag_position,
        group_id,
        group_name,
        group_multi_select,
        group_position,
    ) in rows:
        refs.setdefault(task_id, []).append(
            TagRefOut(
                id=tag_id,
                tag=tag_code,
                name=tag_name,
                group_id=group_id,
                group_name=group_name,
                group_multi_select=group_multi_select,
                group_position=group_position,
                position=tag_position,
            )
        )
    return refs


# No `actor` parameter, an accepted risk: `user_activity.record_task_note` could audit this, but a tag
# bears no privilege and feeds no decision. Once a tag feeds a decision, auditing stops being optional.
def replace_task_tags(session: Session, task: Task, tag_ids: list[UUID]) -> list[TagRefOut]:
    """Full replace of `task`'s selection across every active group; the one writer of `task_tags`.

    Not additive: a group the payload omits ends up empty. Last-write-wins with no version precondition,
    safe only because the caller holds a row lock (see the route); without it this interleaves into a union.

    The whole set is validated before anything is written, so a rejection writes nothing. Each rejection
    is a 422 `{detail: ...}`: unknown `tag_id`, a drafted group, or several from a single-select group.
    No state guard, like every task edit route. Returns refs through the shared loader.
    """
    # Collapse duplicates first: naming a tag twice is not two picks from a single-select group.
    # `dict.fromkeys` keeps the first offending id below deterministic.
    wanted: list[UUID] = list(dict.fromkeys(tag_ids))

    if wanted:
        found = {
            tag_id: (group_state, group_id, group_name, multi_select)
            for tag_id, group_state, group_id, group_name, multi_select in session.execute(
                select(
                    Tag.id, TagGroup.state, TagGroup.id, TagGroup.name, TagGroup.multi_select
                )
                .join(TagGroup, TagGroup.id == Tag.tag_group_id)
                .where(Tag.id.in_(wanted))
            )
        }
        # Absent and drafted get separate messages so the operator does not hunt for a typo.
        for tag_id in wanted:
            if tag_id not in found:
                raise HTTPException(status_code=422, detail=f"tag {tag_id} not found")
        for tag_id in wanted:
            group_state, _group_id, group_name, _multi = found[tag_id]
            if group_state != TAG_GROUP_ACTIVE:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"tag {tag_id} belongs to tag group '{group_name}', which is not active — "
                        "a draft group is not an assignable tag source"
                    ),
                )
        # Single-select is per-group data (`models.TagGroup.multi_select`), so it is enforced here, not in the schema.
        per_group: dict[UUID, list[UUID]] = {}
        for tag_id in wanted:
            _state, group_id, _name, _multi = found[tag_id]
            per_group.setdefault(group_id, []).append(tag_id)
        for group_id, group_tag_ids in per_group.items():
            _state, _gid, group_name, multi_select = found[group_tag_ids[0]]
            if not multi_select and len(group_tag_ids) > 1:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"tag group '{group_name}' is single-select — "
                        f"{len(group_tag_ids)} tags were given for it"
                    ),
                )

    # Validation is done. The delete is scoped to active groups, so a draft group's assignments survive:
    # the payload could not have named them.
    session.execute(
        delete(TaskTag).where(
            TaskTag.task_id == task.id,
            # The shared predicate, not a second copy (see the module docstring).
            TaskTag.tag_id.in_(active_tag_ids_select()),
        )
    )
    if wanted:
        # ORM instances, not a Core `insert(TaskTag)`: `app/tenancy/write_seam.py` is a `before_flush` listener
        # and only stamps `org_id` on ORM instances. A NULL-org row is unreadable under the enforcing read seam,
        # so the route would answer 200 with no tags. `add_all` still flushes as one executemany.
        session.add_all([TaskTag(task_id=task.id, tag_id=tag_id) for tag_id in wanted])
        session.flush()
    return tag_refs_for(session, [task.id]).get(task.id, [])
