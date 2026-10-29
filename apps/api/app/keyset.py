"""Keyset paging: the opaque cursor and the strictly-after clause, the one codec every keyset-paged
listing mints and reads through.

The cursor is the last row's value for every ORDER BY term plus the terms' names (`shape`), so a cursor
minted for another ordering is refused rather than misread. Wire: unpadded urlsafe base64 of
`json [shape, values]`, datetimes as `isoformat()`, UUIDs as `str()` — so no `+` reaches a query string,
where Starlette's `unquote_plus` would turn it into a space.
"""
from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, literal, or_, tuple_


def encode_cursor(shape: list[str], values: Sequence[Any]) -> str:
    wire = [v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, UUID) else v for v in values]
    return base64.urlsafe_b64encode(json.dumps([shape, wire]).encode()).decode().rstrip("=")


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
        got_shape, wire = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if got_shape != shape or len(wire) != len(shape):
            raise ValueError("shape")
        values: list[Any] = []
        for name, v in zip(shape, wire):
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
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(status_code=status, detail=detail) from None


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
