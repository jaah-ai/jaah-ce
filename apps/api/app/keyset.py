"""Keyset paging: the opaque cursor and the strictly-after clause, the one codec every keyset-paged
listing mints and reads through.

The cursor is the last row's value for every ORDER BY term plus the terms' names (`shape`), so a cursor
minted for another ordering is refused rather than misread. Wire: unpadded urlsafe base64 of
`json [shape, values]`, datetimes as `isoformat()`, UUIDs as `str()` — so no `+` reaches a query string,
where Starlette's `unquote_plus` would turn it into a space.
"""
from __future__ import annotations

import binascii
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, literal, or_, tuple_
from sqlalchemy.orm import Session

from app.b64url import b64url, unb64url


#: The refusal of a cursor this listing did not mint (`decode_cursor`'s `detail`), for a keyset list with no wording of
#: its own.
CURSOR_DETAIL = "'cursor' is not one this listing issued: send back a next_cursor unchanged, or leave it out"


def encode_cursor(shape: list[str], values: Sequence[Any]) -> str:
    wire = [v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v for v in values]
    return b64url(json.dumps([shape, wire]).encode())


def decode_cursor(
    raw: str,
    shape: list[str],
    *,
    is_stamp: Callable[[str], bool],
    detail: str,
    int_range: Optional[tuple[int, int]] = None,
    is_uuid: Optional[Callable[[str], bool]] = None,
    is_text: Optional[Callable[[str], bool]] = None,
    nullable: Optional[Callable[[str], bool]] = None,
    status: int = 400,
) -> list[Any]:
    """The cursor's values, typed back: a datetime where `is_stamp(name)`, a UUID where `is_uuid(name)`, a
    string where `is_text(name)`, else an int (within `int_range` when given); `None` only where
    `nullable(name)`. `status` with `detail` for anything this ordering did not mint."""
    try:
        got_shape, wire = json.loads(unb64url(raw))
        if got_shape != shape or len(wire) != len(shape):
            raise ValueError("shape")
        values: list[Any] = []
        for name, v in zip(shape, wire, strict=False):
            if v is None and nullable is not None and nullable(name):
                values.append(None)
            elif is_stamp(name):
                stamp = datetime.fromisoformat(v)
                if stamp.tzinfo is None:  # a minted stamp always carries its offset
                    raise ValueError(name)
                values.append(stamp)
            elif is_uuid is not None and is_uuid(name):
                if not isinstance(v, str):
                    raise ValueError(name)
                values.append(UUID(v))
            elif is_text is not None and is_text(name):
                # NUL: Postgres text cannot hold one, so binding it would be a 500, not this refusal.
                if not isinstance(v, str) or "\x00" in v:
                    raise ValueError(name)
                values.append(v)
            elif (
                isinstance(v, int) and not isinstance(v, bool)
                and (int_range is None or int_range[0] <= v <= int_range[1])
            ):
                values.append(v)
            else:
                raise ValueError(name)
        return values
    # `RecursionError`: a cursor of ten thousand `[`s is a 400 like any other junk, never a 500.
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, RecursionError):
        raise HTTPException(status_code=status, detail=detail) from None


def page(session: Session, stmt: Any, limit: int, *, scalars: bool = True) -> tuple[list[Any], bool]:
    """`limit` rows of `stmt` and whether another page follows: one row past the page answers it without a
    count. Entities (`session.scalars`) by default; `scalars=False` for a multi-column statement's `Row`s."""
    sized = stmt.limit(limit + 1)
    rows = list(session.scalars(sized)) if scalars else session.execute(sized).all()
    return rows[:limit], len(rows) > limit


def stamp_id_cursor_page(
    session: Session, stmt: Any, stamp: Any, ident: Any, cursor: Optional[str], limit: int, *,
    shape: list[str], detail: str, status: int = 422, int_range: Optional[tuple[int, int]] = None,
    is_uuid: bool = False, scalars: bool = True,
) -> tuple[list[Any], Optional[str]]:
    """One newest-first page of `stmt` in the `(stamp, ident) DESC` total order most feeds page by, and the next
    page's cursor (None on the last). `cursor`, the previous page's under `shape` (the two terms' names, stamp
    first), narrows to the rows strictly older; a cursor this ordering did not mint — the empty string included — is `status`
    with `detail` (a caller that reads an empty query value as "no cursor" passes `cursor or None`).
    `ident` decodes as a UUID when `is_uuid`, else an int within `int_range`. The next cursor is read off the
    page's last row — the entity, or the `Row`'s first column when `scalars=False` — by the columns' names."""
    if cursor is not None:
        values = decode_cursor(
            cursor, shape, is_stamp=lambda name: name == shape[0], status=status, detail=detail, int_range=int_range,
            is_uuid=(lambda name: name == shape[1]) if is_uuid else None,
        )
        stmt = stmt.where(tuple_(stamp, ident) < tuple(values))
    rows, more = page(session, stmt.order_by(stamp.desc(), ident.desc()), limit, scalars=scalars)
    if not more:
        return rows, None
    last = rows[-1] if scalars else rows[-1][0]
    return rows, encode_cursor(shape, [getattr(last, stamp.key), getattr(last, ident.key)])


def after_clause(terms: Sequence[tuple[Any, bool]], values: Sequence[Any]):
    """Rows strictly after `values` in the (expression, ascending) ordering: the lexicographic OR-chain."""
    arms = []
    for i, (expr, ascending) in enumerate(terms):
        head = [terms[j][0] == values[j] for j in range(i)]
        arms.append(and_(*head, expr > values[i] if ascending else expr < values[i]))
    return or_(*arms)


def row_after(terms: Sequence[tuple[Any, bool]], values: Sequence[Any]):
    """`after_clause` for an ordering whose terms share ONE direction, as a row-value comparison
    (`(a, b, c) > (:a, :b, :c)`): Postgres reads it as one range bound on a matching btree index, where the
    OR-chain can cost a scan per arm. Mixed directions take `after_clause`."""
    directions = {ascending for _expr, ascending in terms}
    if len(directions) != 1:
        raise ValueError("row_after needs every term in one direction; use after_clause")
    left = tuple_(*(expr for expr, _ascending in terms))
    right = tuple_(*(literal(value, type_=expr.type) for (expr, _ascending), value in zip(terms, values, strict=True)))
    return left > right if directions == {True} else left < right
