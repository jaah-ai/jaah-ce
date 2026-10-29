"""Tag group + tag CRUD and the active-group catalog — `/api/tags`, the Settings > Tasks > Tags page
behind it and the one source every tag-reading surface derives its group list from.

`GET /api/tags/groups` is the only source of `multi_select` and the group list (a task's `TagRefOut`
holds only its own assignments). The word is `tag` everywhere. The catalogue read is `tasks:view`;
usage and writes are the `tags` feature (see `app/authz/catalog.py::FEATURES`).

Every row lookup uses `select`, never `session.get` (see `_group`/`_tag`): the read seam enforces
(`app/tenancy/seam.py::_MODE`) and a primary-key load bypasses it, undoing the org boundary."""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Iterable, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.actors import actor_names_for, stamp_actor
from app.auth import require_user
from app.authz.catalog import permission_key
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.entity_shape import (
    EntityOut,
    StrictModel,
    bounded_text,
    entity_fields,
    flush_or_409,
    optional_uuid,
    row_by_uuid_or_404,
    validated_description,
    validated_name,
)
from app.models.identity import AdminUser
from app.models.task_extras import Tag, TagGroup, TaskTag

log = logging.getLogger("tags")

#: The authoring read's key, rendered, and the key every write declares; used by `list_tag_groups`'s
#: `include_draft` arm.
TAGS_VIEW_KEY = permission_key("tags", "view")

router = APIRouter(prefix="/api/tags", tags=["tags"])

# Errors are a human string in `detail`, raised from the handler, never a pydantic validator: the
# console's `apiDetail` (`apps/web/src/api.ts`) surfaces `detail` only when it is a string, and
# pydantic's 422 is a list. `extra="forbid"` refuses fields this boundary does not accept.

# The grammar, spelled once and quoted into every refusal.
TAG_GRAMMAR = "^[a-z0-9][a-z0-9_-]{0,31}$"
_TAG_RE = re.compile(TAG_GRAMMAR)
_ILLEGAL_RUN_RE = re.compile(r"[^a-z0-9_-]+")

STATES = ("draft", "active")

# Input bound: NFKD can expand its input (U+FDFA becomes 18 characters), so an unbounded value would be
# normalized and echoed in full on the refusal path.
MAX_TAG_INPUT = 200
# A vocabulary is tens of rows; the bound protects the refusal path, which joins the list into a detail.
MAX_REORDER = 500

# Length and character floors are enforced in code, not `Field(max_length=…)`, for the human-string
# rule above. Name/description grammar is `app/entity_shape.py`'s; only the tag grammar is this module's.


def _normalized_tag(value: str, *, derived_from_name: bool = False) -> str:
    """Fold an operator's typing into the grammar, or refuse with a 422 that names the value and the rule.

    "Features " -> "features"; "Front End" -> "front-end". Never truncates (a clipped code is one the
    operator did not choose). Accents fold to their base letter rather than being stripped as illegal."""
    if not value.strip():
        raise HTTPException(
            status_code=422,
            detail="tag cannot be blank; omit it entirely to derive the code from the name",
        )
    bounded_text(value, field="tag", limit=MAX_TAG_INPUT)
    folded = "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )
    candidate = _ILLEGAL_RUN_RE.sub("-", folded.strip().lower()).strip("-_")
    # `fullmatch`, not `match` (`$` matches before a trailing newline): TAG_GRAMMAR is published as the
    # rule, so code and rule must be the same statement.
    if _TAG_RE.fullmatch(candidate):
        return candidate
    if derived_from_name:
        raise HTTPException(
            status_code=422,
            detail=(
                f"no tag code can be derived from the name {value[:80]!r}; send an explicit `tag` "
                f"matching "
                f"{TAG_GRAMMAR} (a letter or digit, then letters, digits, '_' or '-', at most 32 characters)"
            ),
        )
    raise HTTPException(
        status_code=422,
        detail=(
            f"tag {value[:80]!r} is not a usable tag code"
            + (f" (it normalizes to {candidate[:80]!r})" if candidate and candidate != value else "")
            + f": it must match {TAG_GRAMMAR} — a letter or digit, then letters, digits, '_' or '-', at "
            "most 32 characters"
        ),
    )


def _validated_state(value: str) -> str:
    if value not in STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state {value[:80]!r} is not one of {', '.join(repr(s) for s in STATES)}",
        )
    return value


class TagOut(EntityOut):
    tag: str
    position: int
    tag_group_id: str


class TagGroupOut(EntityOut):
    state: str
    multi_select: bool
    position: int
    # Nested, so a group's chips never come from a stale separate membership fetch.
    tags: list[TagOut] = []


class UsageOut(BaseModel):
    """Queried, never estimated: the delete confirmation names it."""

    task_count: int


class TagGroupCreateIn(StrictModel):
    # `extra="forbid"` enforces two omissions: `state` (a group is born draft) and `position`
    # (server-assigned). Ignoring either would mislead the caller.
    name: str
    # `Optional`, so an explicit null clears to `''` as on the PUT (typed `str = ""` it would 422 as a list).
    description: Optional[str] = None
    multi_select: bool = False


class TagGroupUpdateIn(StrictModel):
    """A partial update; only the fields present are touched. Renaming is legitimate: the name is the
    Group-by handle."""

    name: Optional[str] = None
    description: Optional[str] = None
    state: Optional[str] = None
    multi_select: Optional[bool] = None


class TagCreateIn(StrictModel):
    name: str
    # Optional; derived from `name` when absent (so the form needs no TypeScript grammar). Present but
    # blank is a 422 on POST and PUT alike.
    tag: Optional[str] = None
    description: Optional[str] = None


class TagUpdateIn(StrictModel):
    tag: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None


class TagOrderIn(StrictModel):
    """That group's complete tag list in the intended order, not a delta (see `_validated_order`)."""

    tag_ids: list[str]


class GroupOrderIn(StrictModel):
    group_ids: list[str]


# ── The two row lookups, and why neither is `session.get` ─────────────────────────────────────────
# Every write reaches its row through one of these; a `get` is invisible to the read seam and would turn
# a wrong-org 404 into a cross-tenant edit. `row_by_uuid_or_404` is the `select` form.


def _group(session: Session, group_id: str) -> TagGroup:
    return row_by_uuid_or_404(session, TagGroup, group_id, detail="no such tag group")


def _tag(session: Session, tag_id: str) -> Tag:
    return row_by_uuid_or_404(session, Tag, tag_id, detail="no such tag")


def _assert_group_name_free(
    session: Session, name: str, *, org_id: UUID | None, exclude_id: Optional[UUID] = None
) -> None:
    """Group names are compared case-insensitively. The DB unique is exact-match, so "By Type" and "By type"
    would otherwise coexist as two groups a reader cannot tell apart in the Group-by control.

    Within `org_id` only, spelled here rather than borrowed from the read seam, so the rule mirrors the
    `(org_id, name)` unique (revision 0255) whoever is asking. `org_id` is where the row lands: the
    actor's org on create, the row's on rename. `None` matches no row (and the write seam refuses an
    unstamped create): fail closed.

    Racy and accepted: closing it needs `unique(org_id, lower(name))`, deliberately not added; the
    exact-match unique backstops identical spellings."""
    stmt = (
        select(TagGroup.name)
        .where(TagGroup.org_id == org_id)
        .where(func.lower(TagGroup.name) == name.strip().lower())
    )
    if exclude_id is not None:
        stmt = stmt.where(TagGroup.id != exclude_id)
    existing = session.scalar(stmt.limit(1))
    if existing is None:
        return
    if existing == name.strip():
        raise HTTPException(status_code=409, detail=f"a tag group named {existing!r} already exists")
    raise HTTPException(
        status_code=409,
        detail=(
            f"a tag group named {existing!r} already exists; {name!r} differs only in case, which would "
            "leave two groups a reader cannot tell apart"
        ),
    )


# `position` is server-assigned `max + 1` in scope; a 0 would sort above every seeded row. Reorder is a
# whole-list renumber, never two single-row writes, so it is idempotent and cannot land a duplicate.
def _next_position(session: Session, column, *where) -> int:
    """`max(position) + 1` within the scope, never a client-supplied value and never 0.

    Racy, accepted: concurrent creates may tie, resolved by the `name` tie-break; not worth a lock."""
    current = session.scalar(select(func.max(column)).where(*where))
    return int(current or 0) + 1


def _tags_by_group(session: Session, group_ids: list[UUID]) -> dict[UUID, list[Tag]]:
    """One query for every group on the page, so cost does not scale per group."""
    if not group_ids:
        return {}
    rows = session.scalars(
        select(Tag).where(Tag.tag_group_id.in_(group_ids)).order_by(Tag.position, Tag.name)
    )
    grouped: dict[UUID, list[Tag]] = {group_id: [] for group_id in group_ids}
    for row in rows:
        grouped.setdefault(row.tag_group_id, []).append(row)
    return grouped


def _tag_out(row: Tag, actor_names: dict[UUID, str]) -> TagOut:
    return TagOut(
        **entity_fields(row, actor_names),
        tag=row.tag,
        position=int(row.position),
        tag_group_id=str(row.tag_group_id),
    )


def _group_out(row: TagGroup, tags: list[Tag], actor_names: dict[UUID, str]) -> TagGroupOut:
    return TagGroupOut(
        **entity_fields(row, actor_names),
        state=row.state,
        multi_select=bool(row.multi_select),
        position=int(row.position),
        tags=[_tag_out(tag, actor_names) for tag in tags],
    )


def _actor_names(
    session: Session, groups: Iterable[TagGroup], tags: Iterable[Tag]
) -> dict[UUID, str]:
    return actor_names_for(session, [*groups, *tags])


def _groups_out(session: Session, groups: list[TagGroup]) -> list[TagGroupOut]:
    tags = _tags_by_group(session, [group.id for group in groups])
    flat = [tag for rows in tags.values() for tag in rows]
    names = _actor_names(session, groups, flat)
    return [_group_out(group, tags.get(group.id, []), names) for group in groups]


def _one_group_out(session: Session, group: TagGroup) -> TagGroupOut:
    return _groups_out(session, [group])[0]


def _one_tag_out(session: Session, row: Tag) -> TagOut:
    return _tag_out(row, _actor_names(session, [], [row]))


def _ordered_groups(
    session: Session, *, include_draft: bool, lock: bool = False
) -> list[TagGroup]:
    stmt = select(TagGroup).order_by(TagGroup.position, TagGroup.name)
    if not include_draft:
        stmt = stmt.where(TagGroup.state == "active")
    if lock:
        # `routes/groups.py`'s idiom: without the lock a concurrent DELETE makes the renumber a
        # `StaleDataError` 500.
        stmt = stmt.with_for_update()
    return list(session.scalars(stmt))


def _tasks_with_several_tags_in(session: Session, group_id: UUID) -> list[int]:
    """Which tasks hold more than one of this group's tags — the multi -> single flip's blast radius.

    Ids, not a count, so the refusal names a sample the operator can act on."""
    return list(
        session.scalars(
            select(TaskTag.task_id)
            .join(Tag, Tag.id == TaskTag.tag_id)
            .where(Tag.tag_group_id == group_id)
            .group_by(TaskTag.task_id)
            .having(func.count() > 1)
            .order_by(TaskTag.task_id)
        )
    )


def _renumber(rows: list, order: list[UUID]) -> None:
    """Assign `position` 1..n in the given order, touching only the rows that actually move.

    Does not stamp the actor (operator decision: position is presentation), and leaving unmoved rows
    unwritten keeps the `set_updated_at` trigger off them."""
    by_id = {row.id: row for row in rows}
    for position, row_id in enumerate(order, start=1):
        row = by_id[row_id]
        if int(row.position) != position:
            row.position = position


def _sample(ids: list[str], limit: int = 10) -> str:
    """A refusal names examples, never the caller's whole list."""
    head = ", ".join(ids[:limit])
    return head if len(ids) <= limit else f"{head} (and {len(ids) - limit} more)"


def _validated_order(requested: list[str], present: list[UUID], *, noun: str) -> list[UUID]:
    """A whole-list replace: the payload must be exactly the current set, once each. A partial list is
    refused, since merging would let a stale tab drop a row to position 0."""
    if len(requested) > MAX_REORDER:
        raise HTTPException(
            status_code=422,
            detail=(
                f"the {noun} order lists {len(requested)} ids; the limit is {MAX_REORDER} "
                f"(this catalogue holds {len(present)})"
            ),
        )
    order = []
    for value in requested:
        parsed = optional_uuid(value)
        if parsed is None:
            # 422, not 404: the id is body data, so the request is malformed.
            raise HTTPException(
                status_code=422, detail=f"{value[:80]!r} is not a {noun} id"
            )
        order.append(parsed)
    if len(set(order)) != len(order):
        raise HTTPException(
            status_code=422, detail=f"the {noun} order lists the same {noun} more than once"
        )
    expected = set(present)
    if set(order) != expected:
        missing = sorted(str(row_id) for row_id in expected - set(order))
        unknown = sorted(str(row_id) for row_id in set(order) - expected)
        raise HTTPException(
            status_code=422,
            detail=(
                f"the {noun} order must list every {noun} exactly once "
                f"({len(expected)} expected, {len(order)} given"
                + (f"; missing {_sample(missing)}" if missing else "")
                + (f"; not in this set: {_sample(unknown)}" if unknown else "")
                + ")"
            ),
        )
    return order


@router.get("/groups", response_model=list[TagGroupOut])
@feature("tasks", "view")
def list_tag_groups(
    include_draft: bool = False,
    # `require_user` authenticates; the `include_draft` arm reads the principal instead.
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[TagGroupOut]:
    """The catalogue. Active only by default, ordered by `position` then `name`, each group's tags likewise.

    `?include_draft=true` is the authoring read and needs `tags:view` (or superadmin) on top of the
    declared `tasks:view`: drafts are unpublished vocabulary and must not reach task surfaces. Without
    `tags:view` admitted here, a `tags:edit` holder could not list drafts and `PUT /group-order`
    (which requires every group) would deadlock, with its 422 `missing` list leaking the draft ids.

    Every built-in role carries `tags:view` beside `tasks:view` (`app/access_catalog.py`), so this arm
    refuses no role-holder today; it stays so the draft read never outlives the key it rides on."""
    # `principal.has` already answers True for a superadmin. `None` is unreachable (`require_user`
    # refused it) but spelled fail-closed so adding `agent_allowed` later cannot disclose drafts.
    if include_draft and not (principal is not None and principal.has(TAGS_VIEW_KEY)):
        raise HTTPException(
            status_code=403,
            detail=(
                "include_draft needs `tags:view` or superadmin; draft tag groups are not a task surface"
            ),
        )
    return _groups_out(session, _ordered_groups(session, include_draft=include_draft))


@router.post("/groups", response_model=TagGroupOut, status_code=201)
@feature("tags", "create")
def create_tag_group(
    body: TagGroupCreateIn,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TagGroupOut:
    """A group is born draft with no tags (`state` is refused), so going live requires the activation
    check on PUT, which needs a tag."""
    name = validated_name(body.name, field="name")
    # The actor's org is where the row lands: the write seam stamps the bound org onto the `Stored` row.
    _assert_group_name_free(session, name, org_id=user.org_id)
    row = TagGroup(
        name=name,
        description=validated_description(body.description),
        state="draft",
        multi_select=bool(body.multi_select),
        position=_next_position(session, TagGroup.position),
    )
    stamp_actor(row, user, created=True)
    session.add(row)
    flush_or_409(session, f"a tag group named {name!r} already exists")
    session.commit()
    session.refresh(row)
    return _one_group_out(session, row)


@router.put("/groups/{group_id}", response_model=TagGroupOut)
@feature("tags", "edit")
def update_tag_group(
    group_id: str,
    body: TagGroupUpdateIn,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TagGroupOut:
    """Partial update. Every refusal is decided before the first `setattr`, so a rejected edit changes
    nothing.

    Two guards against data contradicting a control the UI renders:
      * Activation needs at least one tag.
      * Multi -> single is refused while any task holds more than one of this group's tags (the existing
        rows would persist and display arbitrarily); the count is named. Single -> multi is always
        allowed."""
    # Deactivating a group with assigned tags is allowed silently (operator decision): `task_tags` rows
    # persist and return on reactivation, so DELETE is not the only way to retire a vocabulary.
    row = _group(session, group_id)
    fields = body.model_dump(exclude_unset=True)
    for field in ("name", "state", "multi_select"):
        if field in fields and fields[field] is None:
            # None of these three has a meaningful empty value; only `description` clears (to `''`).
            raise HTTPException(status_code=422, detail=f"{field} cannot be cleared")
    name = validated_name(fields["name"], field="name") if "name" in fields else None
    state = _validated_state(fields["state"]) if "state" in fields else None
    description = validated_description(fields["description"]) if "description" in fields else None
    if name is not None:
        _assert_group_name_free(session, name, org_id=row.org_id, exclude_id=row.id)
    if state == "active":
        tag_count = int(
            session.scalar(select(func.count()).select_from(Tag).where(Tag.tag_group_id == row.id))
            or 0
        )
        if tag_count == 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"tag group {row.name!r} has no tags and cannot be activated; add at least one tag "
                    "first"
                ),
            )
    # Checked whenever single-select is requested, not only on a flip: an already-single group can
    # still hold doubly-tagged tasks (earlier flips, raw SQL).
    if fields.get("multi_select") is False:
        affected = _tasks_with_several_tags_in(session, row.id)
        if affected:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"tag group {row.name!r} cannot become single-select: {len(affected)} "
                    + ("task already holds" if len(affected) == 1 else "tasks already hold")
                    + " more than one of its tags — task "
                    + _sample([str(task_id) for task_id in affected])
                    + "; remove the extra tags from those tasks first"
                ),
            )
    if name is not None:
        row.name = name
    if state is not None:
        row.state = state
    if description is not None:
        row.description = description
    if "multi_select" in fields:
        row.multi_select = bool(fields["multi_select"])
    if fields:
        stamp_actor(row, user)
    flush_or_409(session, f"a tag group named {(name if name is not None else row.name)!r} already exists")
    session.commit()
    session.refresh(row)
    return _one_group_out(session, row)


@router.delete("/groups/{group_id}", status_code=204)
@feature("tags", "delete")
def delete_tag_group(
    group_id: str,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """Unconditional; the FKs cascade (migration 0123) the group, its tags and every assignment. No
    occupancy guard: the confirmation dialog reads `/usage` first, and an unoverridable refusal makes a
    vocabulary undeletable."""
    row = _group(session, group_id)
    # The actor is logged because `updated_by_id` leaves with the row, and the cascaded assignment count
    # with it.
    assignments = session.scalar(
        select(func.count())
        .select_from(TaskTag)
        .join(Tag, Tag.id == TaskTag.tag_id)
        .where(Tag.tag_group_id == row.id)
    )
    log.info(
        "tags: %s deleted tag group %r — cascading to its tags and %s task assignment(s)",
        user.email, row.name, int(assignments or 0),
    )
    session.delete(row)
    session.commit()
    return Response(status_code=204)


@router.get("/groups/{group_id}/usage", response_model=UsageOut)
@feature("tags", "view")
def tag_group_usage(
    group_id: str,
    _: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> UsageOut:
    """Distinct tasks holding any tag in this group (a task with two of them counts once).

    `tags:view`, not `tasks:view`: it is a task count, and its only caller is the delete confirmation."""
    row = _group(session, group_id)
    count = session.scalar(
        select(func.count(func.distinct(TaskTag.task_id)))
        .select_from(TaskTag)
        .join(Tag, Tag.id == TaskTag.tag_id)
        .where(Tag.tag_group_id == row.id)
    )
    return UsageOut(task_count=int(count or 0))


@router.post("/groups/{group_id}/tags", response_model=TagOut, status_code=201)
@feature("tags", "create")
def create_tag(
    group_id: str,
    body: TagCreateIn,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TagOut:
    group = _group(session, group_id)
    name = validated_name(body.name, field="name")
    code = (
        _normalized_tag(body.tag)
        if body.tag is not None
        else _normalized_tag(name, derived_from_name=True)
    )
    row = Tag(
        tag_group_id=group.id,
        tag=code,
        name=name,
        description=validated_description(body.description),
        position=_next_position(session, Tag.position, Tag.tag_group_id == group.id),
    )
    stamp_actor(row, user, created=True)
    session.add(row)
    flush_or_409(session, f"tag {code!r} already exists in tag group {group.name!r}")
    session.commit()
    session.refresh(row)
    return _one_tag_out(session, row)


@router.put("/groups/{group_id}/tag-order", response_model=TagGroupOut)
@feature("tags", "edit")
def reorder_tags(
    group_id: str,
    body: TagOrderIn,
    _: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TagGroupOut:
    """One transaction, `position` 1..n over the group's complete tag list. Idempotent."""
    group = _group(session, group_id)
    rows = list(
        session.scalars(
            select(Tag)
            .where(Tag.tag_group_id == group.id)
            .order_by(Tag.position, Tag.name)
            .with_for_update()
        )
    )
    order = _validated_order(body.tag_ids, [row.id for row in rows], noun="tag")
    _renumber(rows, order)
    session.commit()
    # Expire: `expire_on_commit=False` (`app/db.py`), so the trigger-bumped `updated_at` would read stale.
    session.expire_all()
    # Re-read the group rather than refresh it: a concurrent delete would make that a 500, and
    # `FOR UPDATE` locks nothing when the group has no tags.
    return _one_group_out(session, _group(session, group_id))


# Declared above `PUT /{tag_id}` on purpose: FastAPI would otherwise match `group-order` as that route's
# parameter and this endpoint would be unreachable.
@router.put("/group-order", response_model=list[TagGroupOut])
@feature("tags", "edit")
def reorder_tag_groups(
    body: GroupOrderIn,
    _: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[TagGroupOut]:
    """Every group in the intended order, drafts included, and the response carries drafts too (its only
    consumer, the Settings page, lists them)."""
    rows = _ordered_groups(session, include_draft=True, lock=True)
    order = _validated_order(body.group_ids, [row.id for row in rows], noun="tag group")
    _renumber(rows, order)
    session.commit()
    session.expire_all()  # `reorder_tags`' reason, one table up.
    return _groups_out(session, _ordered_groups(session, include_draft=True))


@router.get("/{tag_id}/usage", response_model=UsageOut)
@feature("tags", "view")
def tag_usage(
    tag_id: str,
    _: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> UsageOut:
    """`tags:view` for `tag_group_usage`'s reason — a task count, not a vocabulary."""
    row = _tag(session, tag_id)
    count = session.scalar(
        select(func.count()).select_from(TaskTag).where(TaskTag.tag_id == row.id)
    )
    return UsageOut(task_count=int(count or 0))


@router.put("/{tag_id}", response_model=TagOut)
@feature("tags", "edit")
def update_tag(
    tag_id: str,
    body: TagUpdateIn,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> TagOut:
    """Partial update of the three authorable fields. The group is not one: moving a tag would carry its
    assignments into another vocabulary, and `position` is group-scoped."""
    row = _tag(session, tag_id)
    fields = body.model_dump(exclude_unset=True)
    for field in ("tag", "name"):
        if field in fields and fields[field] is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be cleared")
    code = _normalized_tag(fields["tag"]) if "tag" in fields else None
    name = validated_name(fields["name"], field="name") if "name" in fields else None
    description = validated_description(fields["description"]) if "description" in fields else None
    # Read the group name before the first `setattr`: a query on a dirty row autoflushes, and a
    # duplicate would raise outside `flush_or_409` as a 500 instead of the 409.
    group_name = session.scalar(select(TagGroup.name).where(TagGroup.id == row.tag_group_id))
    if code is not None:
        row.tag = code
    if name is not None:
        row.name = name
    if description is not None:
        row.description = description
    if fields:
        stamp_actor(row, user)
    flush_or_409(session, f"tag {(code or row.tag)!r} already exists in tag group {group_name!r}")
    session.commit()
    session.refresh(row)
    return _one_tag_out(session, row)


@router.delete("/{tag_id}", status_code=204)
@feature("tags", "delete")
def delete_tag(
    tag_id: str,
    user: AdminUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    """Cascades to this tag's assignments (`task_tags.tag_id ON DELETE CASCADE`).

    Deleting an active group's last tag is allowed (operator decision): the zero-tag rule is an
    activation check, so the group stays active and empty until a tag is added or it is drafted."""
    row = _tag(session, tag_id)
    assignments = session.scalar(
        select(func.count()).select_from(TaskTag).where(TaskTag.tag_id == row.id)
    )
    log.info(
        "tags: %s deleted tag %r — cascading to %s task assignment(s)",
        user.email, row.tag, int(assignments or 0),
    )
    session.delete(row)
    session.commit()
    return Response(status_code=204)
