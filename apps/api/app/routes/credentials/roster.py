"""The credential roster's query builder: scope, filters, the capped total and keyset pages.

One seam for every roster reader (`GET /api/credentials`, and the bulk actions' `match`): `RosterFilters` is the
normalised filter set, `roster_where` turns it into clauses under the caller's scope, `capped_count` and
`keyset_page` read through them. Every filter is an indexed predicate on a stored column (revision 202610232130):
no row's config is opened, and no id list is bound (`= ANY(:array)` is one parameter).

The roster EXPLAIN oracle (kept in git history) ran the statements built here (`page_statements`,
`capped_count_statement`) against a 1M-row slot DB; a filter or sort added here needs the same proof.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Optional, Sequence
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, Uuid, and_, any_, exists, false, func, literal, null, or_, select, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app import keyset
from app.authz.principal import Principal
from app.authz.visibility import credential_write_clause, owned_credential_clause
from app.credentials.registry import CREDENTIAL_TYPES
from app.entity_shape import optional_uuid
from app.list_page import like_pattern
from app.models.credentials import Credential
from app.models.identity import AdminUser, UserIdentity
from app.models.orgs import Org
from app.routes.credentials.guards import _VIEW_KEY
from app.tenancy import seam

#: The roster's `sort` vocabulary; each order ends in `id`, a total order.
RosterSort = Literal["newest", "oldest", "name", "type", "org"]
RosterSecrets = Literal["yes", "no"]
RosterHealth = Literal["expired", "expiring", "never_used", "test_failed"]

#: The most rows `capped_count` counts; past it `total` is this and `total_capped` true. Patched by tests.
ROSTER_COUNT_CAP = 10_000
#: How far ahead `health=expiring` looks (D7).
EXPIRING_WITHIN = timedelta(days=7)
#: What every refused cursor says: the client reloads, it never guesses a page.
STALE_CURSOR = "This page link is out of date. Reload the list."

#: The `org_id` value selecting rows with no org; an absent parameter already means "do not filter",
#: and `none` cannot collide with a UUID.
ORG_ID_NONE = "none"

#: Who the roster's Owner filter files a row under: a personal row's owner — whom an Org Owner/Admin may
#: have made it for (`authz.validated_owner_user`) — else its creator. One expression for the filter, its
#: option list (`reads.list_credential_owners`) and the operator's email search, so an option always finds its
#: rows. Indexed as `ix_credentials_owner_expr`.
ROSTER_OWNER = func.coalesce(Credential.owner_id, Credential.created_by_id)


def _uuid_array(ids: Sequence[UUID]) -> Any:
    """`ids` as ONE bound `uuid[]`, for `= ANY(...)`: never an `IN` list (Postgres caps those at 65535)."""
    return any_(literal(list(ids), type_=ARRAY(Uuid())))


# ── The filters ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RosterFilters:
    """The roster's narrowing, normalised: `q` stripped (None when blank), `owner_ids` sorted and unique, `org_id`
    a UUID or `ORG_ID_NONE`. Built by `roster_filters_from_query`, which owns every 422."""

    type: Optional[str] = None
    q: Optional[str] = None
    owner_ids: tuple[UUID, ...] = ()
    org_id: Optional[UUID | str] = None
    secrets: Optional[RosterSecrets] = None
    health: Optional[RosterHealth] = None
    mine: bool = False

    def digest(self) -> str:
        """A short hash of the normalised filters, part of every cursor's shape: a cursor minted under other
        filters is refused rather than read as a position in this result."""
        wire = [self.type, self.q, [str(i) for i in self.owner_ids], None if self.org_id is None else str(self.org_id),
                self.secrets, self.health, self.mine]
        return hashlib.sha256(json.dumps(wire).encode()).hexdigest()[:16]


def roster_filters_from_query(
    *,
    type_slug: Optional[str] = None,
    q: Optional[str] = None,
    owner_ids: Sequence[UUID] = (),
    org_id: Optional[str] = None,
    secrets: Optional[str] = None,
    health: Optional[str] = None,
    mine: bool = False,
) -> RosterFilters:
    """The query parameters as `RosterFilters`. An unparseable value is a 422 naming the parameter, never an
    empty or unfiltered page, and never echoing the value."""
    if type_slug is not None and type_slug not in CREDENTIAL_TYPES:
        raise HTTPException(status_code=422, detail="type is not a known credential type")
    # Total rather than a catch-all `else`: "YES" must not get the inverted roster with a 200.
    if secrets is not None and secrets not in ("yes", "no"):
        raise HTTPException(status_code=422, detail="secrets must be 'yes' or 'no'")
    if health is not None and health not in ("expired", "expiring", "never_used", "test_failed"):
        raise HTTPException(
            status_code=422, detail="health must be 'expired', 'expiring', 'never_used' or 'test_failed'"
        )
    parsed_org: Optional[UUID | str] = None
    if org_id is not None:
        # An empty value is a 422; only an absent parameter means "do not narrow". The sentinel is one spelling.
        if org_id == ORG_ID_NONE:
            parsed_org = ORG_ID_NONE
        else:
            parsed_org = optional_uuid(org_id)
            if parsed_org is None:
                raise HTTPException(status_code=422, detail=f"org_id must be a uuid or '{ORG_ID_NONE}'")
    needle = q.strip() if q is not None else ""
    if "\x00" in needle:
        # Postgres text cannot hold NUL: a 422, never a driver error's 500.
        raise HTTPException(status_code=422, detail="q must not contain a NUL character")
    return RosterFilters(
        type=type_slug,
        q=needle or None,
        owner_ids=tuple(sorted(set(owner_ids), key=str)),
        org_id=parsed_org,
        secrets=secrets,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        mine=mine,
    )


class RosterFilterSet(BaseModel):
    """`RosterFilters` on the wire (a bulk action's `match`): the same names as the query parameters, validated by
    the same function (`to_filters`)."""

    type: Optional[str] = None
    q: Optional[str] = None
    owner_id: list[UUID] = []
    org_id: Optional[str] = None
    secrets: Optional[RosterSecrets] = None
    health: Optional[RosterHealth] = None
    mine: bool = False

    def to_filters(self) -> RosterFilters:
        return roster_filters_from_query(
            type_slug=self.type, q=self.q, owner_ids=self.owner_id, org_id=self.org_id,
            secrets=self.secrets, health=self.health, mine=self.mine,
        )


# ── Scope and WHERE ───────────────────────────────────────────────────────────


def roster_scope(principal: Principal | None, *, mine: bool) -> Any:
    """The rows the roster may list for `principal`, as one clause (`app/authz/`) that the page, the `total` and the
    owner options all apply. The `mine` arm calls `owned_credential_clause` directly: it asks about ownership
    (`owner_id`), which no `credentials:*` key answers. Read that clause before adding a scope here."""
    if mine:
        return owned_credential_clause(principal)
    return credential_write_clause(principal, _VIEW_KEY)


def reads_as_operator(principal: Principal | None, f: RosterFilters) -> bool:
    """D1: the operator reads the roster across tenants — on the operator plane (no tenant bound) and not
    narrowed to its own rows. Only this reader's `q` also matches org and owner email."""
    return (
        not f.mine
        and seam.current_scope() is seam.OPERATOR
        and (principal is None or principal.is_operator)
    )


def _search(session: Session, principal: Principal | None, f: RosterFilters) -> ColumnElement[bool]:
    """`q`: name or description by substring, through their trigram indexes (`lower(...) LIKE`). Never the config.

    [op] The operator also finds a row by its org's name or slug and its owner's email (`ROSTER_OWNER`). Both are
    resolved first, uncapped, through their own trigram indexes, and join the same OR as one bound array each, so
    the planner ORs four index scans. A tenant caller gets neither arm: no member's email is probed beyond today."""
    pattern = like_pattern((f.q or "").lower())
    arms: list[Any] = [
        func.lower(Credential.name).like(pattern, escape="\\"),
        func.lower(Credential.description).like(pattern, escape="\\"),
    ]
    if reads_as_operator(principal, f):
        org_ids = session.scalars(
            select(Org.id).where(or_(
                func.lower(Org.name).like(pattern, escape="\\"),
                func.lower(Org.slug).like(pattern, escape="\\"),
            ))
        ).all()
        owner_ids = session.scalars(
            select(AdminUser.id)
            .join(UserIdentity, UserIdentity.id == AdminUser.identity_id)
            .where(func.lower(UserIdentity.email).like(pattern, escape="\\"))
        ).all()
        if org_ids:
            arms.append(Credential.org_id == _uuid_array(org_ids))
        if owner_ids:
            arms.append(ROSTER_OWNER == _uuid_array(owner_ids))
    return or_(*arms)


def _health(health: RosterHealth) -> ColumnElement[bool]:
    """Each value in the exact form its partial index is declared with (revision 202610232130)."""
    if health == "expired":
        return Credential.expires_at <= func.now()
    if health == "expiring":
        return and_(Credential.expires_at > func.now(), Credential.expires_at <= func.now() + EXPIRING_WITHIN)
    if health == "never_used":
        return Credential.last_used_at.is_(None)
    return Credential.last_test_ok == false()


def roster_where(session: Session, principal: Principal | None, f: RosterFilters) -> list[ColumnElement[bool]]:
    """The caller's scope (`roster_scope`) plus every filter in `f`, as the clause list the page and the total
    both apply, so neither can drift from the other. `session` serves the operator search's pre-queries only."""
    where: list[Any] = [roster_scope(principal, mine=f.mine)]
    if f.type is not None:
        where.append(Credential.type == f.type)
    if f.q is not None:
        where.append(_search(session, principal, f))
    if len(f.owner_ids) == 1:
        # By user id, never display name; `GET /api/credentials/owners` supplies ids. One id is an equality, so
        # its index walks in `created_at` order; several are a union, one bound array.
        where.append(ROSTER_OWNER == f.owner_ids[0])
    elif f.owner_ids:
        where.append(ROSTER_OWNER == _uuid_array(f.owner_ids))
    if f.org_id == ORG_ID_NONE:
        where.append(Credential.org_id.is_(None))
    elif f.org_id is not None:
        # A dangling id is valid (no FK to `orgs`, revision 0181) and returns its rows.
        where.append(Credential.org_id == f.org_id)
    if f.secrets == "yes":
        where.append(Credential.has_stored_secret.is_(True))
    elif f.secrets == "no":
        # `NOT has_stored_secret`, the partial index's own predicate (`ix_credentials_no_secret`).
        where.append(~Credential.has_stored_secret)
    if f.health is not None:
        where.append(_health(f.health))
    return where


# ── The capped total ──────────────────────────────────────────────────────────


def capped_count_statement(where: Sequence[Any], cap: int) -> Select:
    """`count(*)` over at most `cap + 1` matching rows: the scan stops at the cap, however many match. Ordered by
    the newest-first index's key so the planner walks an index (or a partial one) rather than a sequential scan."""
    capped = (
        select(literal(1)).select_from(Credential).where(*where)
        .order_by(Credential.created_at.desc(), Credential.id.desc()).limit(cap + 1).subquery()
    )
    return select(func.count()).select_from(capped)


def capped_count(session: Session, where: Sequence[Any], cap: Optional[int] = None) -> tuple[int, bool]:
    """`(total, total_capped)`: the exact count up to `cap` (default `ROSTER_COUNT_CAP`), else `(cap, True)`."""
    cap = ROSTER_COUNT_CAP if cap is None else cap
    counted = session.scalar(capped_count_statement(where, cap)) or 0
    return (cap, True) if counted > cap else (counted, False)


# ── Keyset pages ──────────────────────────────────────────────────────────────

_LNAME = func.lower(Credential.name)

#: Each sort's key: `(cursor name, expression, ascending)`, ending in `id`. All one direction, so the next page
#: is one row-value range (`keyset.row_after`) on the matching index. `org` is `_org_page`'s own walk.
_KEYS: dict[str, tuple[tuple[str, Any, bool], ...]] = {
    "newest": (("created_at", Credential.created_at, False), ("id", Credential.id, False)),
    "oldest": (("created_at", Credential.created_at, True), ("id", Credential.id, True)),
    "name": (("lname", _LNAME, True), ("id", Credential.id, True)),
    "type": (("type", Credential.type, True), ("lname", _LNAME, True), ("id", Credential.id, True)),
}
_ORG_KEY = ("org_lname", "org_id", "lname", "id")


def _key_names(sort: str) -> tuple[str, ...]:
    return _ORG_KEY if sort == "org" else tuple(name for name, _expr, _asc in _KEYS[sort])


def _shape(sort: str) -> list[str]:
    """A cursor's shape: `result` (whose value names the sort, page size and filters) then its key's names."""
    return ["result", *_key_names(sort)]


def _result(f: RosterFilters, sort: str, per: int) -> str:
    """Which result a cursor pages: any difference makes it a cursor for another result (AC-2.3)."""
    return f"{sort}/{per}/{f.digest()}"


def decode_roster_cursor(raw: str, f: RosterFilters, sort: str, per: int) -> list[Any]:
    """The cursor's key values, or a 422 (`STALE_CURSOR`) for one minted under another sort, size or filter set,
    or not minted here at all."""
    values = keyset.decode_cursor(
        raw, _shape(sort),
        is_stamp=lambda name: name == "created_at",
        is_uuid=lambda name: name in ("id", "org_id"),
        is_text=lambda name: name in ("result", "lname", "type", "org_lname"),
        nullable=lambda name: name == "org_lname",
        detail=STALE_CURSOR, status=422,
    )
    if values[0] != _result(f, sort, per):
        raise HTTPException(status_code=422, detail=STALE_CURSOR)
    return values[1:]


def page_statements(
    session: Session, where: Sequence[Any], f: RosterFilters, sort: str, per: int, after: Optional[list[Any]],
) -> list[Select]:
    """The page queries, run in order until `per + 1` rows are in hand: each returns up to `per + 1` rows strictly
    after `after` (the decoded cursor's key, None for the first page) as `(id, *key values)` in `sort`'s order. The
    `+ 1` row only says whether a next page exists. One statement per sort, but two for `org` (`_org_page`)."""
    if sort == "org":
        return _org_page(session, where, f, per, after)
    keys = _KEYS[sort]
    stmt = select(Credential.id, *(expr.label(f"k_{name}") for name, expr, _asc in keys)).where(*where)
    if after is not None:
        stmt = stmt.where(keyset.row_after([(expr, asc) for _name, expr, asc in keys], after))
    return [stmt.order_by(*(expr.asc() if asc else expr.desc() for _name, expr, asc in keys)).limit(per + 1)]


def _org_page(
    session: Session, where: Sequence[Any], f: RosterFilters, per: int, after: Optional[list[Any]],
) -> list[Select]:
    """A9: the `org` sort as a walk over `orgs` in `(lower(name), id)` order, each org's matching rows taken by a
    LATERAL in `(lower(name), id)` order (`ix_credentials_org_lname_id`), LIMIT per+1 overall — so a page reads
    about `per` rows whatever the table holds. Rows whose `org_id` names no org (no FK) follow every org, in name
    order: the tail, a second statement `keyset_page` runs only when the walk leaves the page short. (Two
    statements, not a UNION ALL: SQL promises no order between a union's arms.)

    The walk is narrowed to one org when the caller or the filter names it (a tenant, `org_id=`), and the tail is
    skipped when that org exists: the tail is an anti-join that would otherwise scan the named org's rows."""
    named = seam.bound_org() or (f.org_id if isinstance(f.org_id, UUID) else None)
    named_exists = named is not None and bool(session.scalar(select(exists().where(Org.id == named))))
    walk = named is None or named_exists
    tail = named is None or not named_exists
    in_tail = after is not None and after[0] is None

    statements = []
    if walk and not in_tail:
        orgs = select(Org.id.label("oid"), func.lower(Org.name).label("oname"))
        if named is not None:
            orgs = orgs.where(Org.id == named)
        if after is not None:
            # From the cursor's org on, inclusive: its remaining rows come first.
            orgs = orgs.where(or_(
                and_(func.lower(Org.name) == after[0], Org.id == after[1]),
                keyset.row_after([(func.lower(Org.name), True), (Org.id, True)], after[:2]),
            ))
        o = orgs.subquery("o")
        inner = select(Credential.id.label("cid"), _LNAME.label("lname")).where(Credential.org_id == o.c.oid, *where)
        if after is not None:
            inner = inner.where(or_(
                o.c.oid != after[1],
                keyset.row_after([(_LNAME, True), (Credential.id, True)], after[2:]),
            ))
        c = inner.order_by(_LNAME, Credential.id).limit(per + 1).lateral("c")
        statements.append(
            select(c.c.cid, o.c.oname, o.c.oid, c.c.lname, c.c.cid.label("k_id"))
            .select_from(o).join(c, true())
            .order_by(o.c.oname, o.c.oid, c.c.lname, c.c.cid)
            .limit(per + 1)
        )
    if tail:
        dangling = select(
            Credential.id.label("cid"), null().label("oname"), Credential.org_id.label("oid"), _LNAME.label("lname"),
            Credential.id.label("k_id"),
        ).where(*where, ~exists().where(Org.id == Credential.org_id))
        if after is not None and in_tail:
            dangling = dangling.where(keyset.row_after([(_LNAME, True), (Credential.id, True)], after[2:]))
        statements.append(dangling.order_by(_LNAME, Credential.id).limit(per + 1))
    return statements


def keyset_page(
    session: Session, where: Sequence[Any], f: RosterFilters, sort: str, per: int, cursor: Optional[str],
) -> tuple[list[Credential], Optional[str]]:
    """One keyset page: its rows in `sort`'s order and the cursor for the next one (None on the last page). There
    is no previous cursor (A6): a client walks back by replaying the cursors it holds."""
    after = decode_roster_cursor(cursor, f, sort, per) if cursor is not None else None
    found: list[Any] = []
    for statement in page_statements(session, where, f, sort, per, after):
        found += session.execute(statement).all()
        if len(found) > per:
            break
    page, more = found[:per], len(found) > per
    by_id = {
        row.id: row
        # Under `where` too, so every statement of the page carries the caller's scope.
        for row in session.scalars(
            select(Credential).where(*where, Credential.id == _uuid_array([r[0] for r in page]))
        )
    } if page else {}
    rows = [by_id[r[0]] for r in page if r[0] in by_id]
    next_cursor = (
        keyset.encode_cursor(_shape(sort), [_result(f, sort, per), *page[-1][1:]]) if more else None
    )
    return rows, next_cursor
