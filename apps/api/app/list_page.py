"""Numbered-page listing: the one contract every roster of thousands (users, groups, a group's members,
customers, orgs, nodes, hosts) answers through — `?page=&per=&sort=&dir=` in, `{items, total, page, per}`
out, the dialect `routes/sessions.py` and `routes/credentials/` already serve.

Two ways to cut a page, one contract:

- `sql_page` — the filters are SQL, so the count and the page are two bounded statements over one filtered
  select. The default for any roster a table holds.
- `list_page` — the rows are built in Python first (a node's status is presence-derived, a customer's billing
  state comes from the Stripe mirror), so the sort and the cut run over that list. Cheap at thousands; the
  route already paid for the build.

Every ordering ends in a unique tie-breaker so offset paging never repeats or skips a row between two reads of
one unchanged roster. A page past the last is an empty page with the true `total`, never a 404 or a clamp: the
console's pager reads `total` and steps back itself.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Literal, Optional, TypeVar

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

#: The console's default and offered sizes are 25/50/100 (`apps/web/src/hooks/useListPage.ts`); the bound is
#: wider for a script or an auxiliary walk (`pages/nodes/hosts/api.ts::listAllHosts`).
PER_DEFAULT = 50
PER_MAX = 200
#: `page * per` stays far inside a 32-bit OFFSET at the widest page.
PAGE_MAX = 100_000
#: A search box's longest needle, every roster alike.
Q_MAX = 200

Direction = Literal["asc", "desc"]
T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """One page of a roster."""

    items: list[T]
    total: int = Field(description="How many rows match the filters, over every page.")
    page: int = Field(description="This page's 1-based number, as asked.")
    per: int = Field(description="The page size, as asked.")


@dataclass(frozen=True)
class PageQuery:
    page: int
    per: int
    #: A key of the route's sort table, or None for its default order.
    sort: Optional[str]
    dir: Direction

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per


def page_query(sorts: Sequence[str]) -> Callable[..., PageQuery]:
    """The FastAPI dependency for one route's paging parameters. `sort` outside `sorts` is a 422 naming
    the vocabulary, never a silent default: a header that quietly sorts by something else lies."""

    def dependency(
        page: int = Query(default=1, ge=1, le=PAGE_MAX),
        per: int = Query(default=PER_DEFAULT, ge=1, le=PER_MAX),
        sort: Optional[str] = Query(default=None, max_length=64),
        dir_: Direction = Query(default="asc", alias="dir"),
    ) -> PageQuery:
        if sort is not None and sort not in sorts:
            raise HTTPException(status_code=422, detail=f"unknown sort '{sort}' (one of: {', '.join(sorts)})")
        return PageQuery(page=page, per=per, sort=sort, dir=dir_)

    return dependency


def like_pattern(q: str) -> str:
    """A case-insensitive substring pattern for `ilike(..., escape="\\\\")`: LIKE's wildcards in what the
    person typed are literal characters, so a pasted `50%_off` matches itself."""
    return "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def sql_page(
    session: Session,
    stmt: Select[Any],
    query: PageQuery,
    *,
    sorts: Mapping[str, Any],
    default: Sequence[Any],
    tie: Any,
) -> tuple[list[Any], int]:
    """The page's rows and the filtered total. `stmt` is the filtered select (no ORDER BY, no LIMIT);
    `sorts` maps each sort key to its column expression, `default` is the ORDER BY without `?sort=`, and
    `tie` a unique column closing every ordering. A sort column's NULLs sit last in either direction."""
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    if query.sort is None:
        order = [*default, tie]
    else:
        column = sorts[query.sort]
        lead = column.asc() if query.dir == "asc" else column.desc()
        order = [lead.nullslast(), tie.asc() if query.dir == "asc" else tie.desc()]
    rows = session.execute(stmt.order_by(*order).offset(query.offset).limit(query.per)).all()
    # A one-entity select hands back the entity, as `session.scalars` would; a multi-column one the rows.
    single = len(stmt.column_descriptions) == 1
    return [row[0] if single else row for row in rows], total


def list_page(
    entries: Sequence[T],
    query: PageQuery,
    *,
    sorts: Mapping[str, Callable[[T], Any]],
) -> tuple[list[T], int]:
    """The page of an already-built, already-filtered list in its default order. A sort key's `None` sits
    last in either direction; Python's sort is stable, so equal keys keep the default order, which is the
    tie-breaker."""
    rows = list(entries)
    if query.sort is not None:
        key = sorts[query.sort]
        present = [row for row in rows if key(row) is not None]
        absent = [row for row in rows if key(row) is None]
        present.sort(key=key, reverse=query.dir == "desc")
        rows = present + absent
    return rows[query.offset: query.offset + query.per], len(rows)
