"""The roster's tree mode, one level per request: `GET /api/credentials/tree`.

Three levels, each loaded only when the reader opens its parent: an operator's TENANTS (orgs holding a row in
scope), a tenant's HOMES (`Credential.home_key`: org shared, system, project, personal), and a home's FOLDER (the
next name segments under a path, with the rows that sit directly in it as leaves). Every statement applies
`roster.roster_where`, so a node's count can never disagree with the flat roster under the same filters.

THE SKIP-SCAN. A level's children are the distinct values of a prefix, found by a recursive CTE that probes an
index once per child (`FolderLevel.walk`, `HomesLevel.walk`) rather than reading every row below. A folder's rows
are found by `parent_path || '/'` (`FOLDER_KEY`, `ix_credentials_home_folder_key`), compared byte-wise (`~<~`, the
`text_pattern_ops` operators): on that key every child's rows are ONE contiguous range, `[B||s||'/', B||s||'0')`
under base `B`, so the next child after `s` is the first key at or past `B||s||'0'` (`0` follows `/`). On
`parent_path` itself they are not: `a!` and `a-b` sort between `a` and `a/x`, since `!`, `-` and `.` precede `/`, so a
walk there revisits `a` or skips siblings. The filtered GROUP BY orders by the same key (`COLLATE "C"`).

FILTERED COUNTS. A level's matching rows are counted first (capped at `SPARSE_CAP`). When few match (a filter, or a
narrow scope such as a member's own rows), one GROUP BY over exactly those rows names the children and their exact
counts. When many match, the walk over the caller's scope alone names candidates, each gets a filtered capped count,
and a candidate counting 0 is hidden. Either way no child with a match is missing and none without one is shown.

Counts stop at `NODE_COUNT_CAP` (shown "1,000+"), not the roster total's 10,000: 100 children at 10,000 rows each
would not fit the 1 s budget.
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal, Optional, Sequence
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, Text, collate, exists, func, literal, literal_column, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import Grouping

from app import keyset
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.object_access import OWNERSHIP_SYSTEM
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.models.orgs import Org
from app.models.projects import Project
from app.routes.credentials.credential_wire import (
    CredentialOut,
    _NAME_MAX_LEN,
    _actor_names,
    _credential_out,
    _home_labels,
)
from app.routes.credentials.guards import _may_edit
from app.routes.credentials.roster import (
    STALE_CURSOR,
    RosterFilters,
    RosterHealth,
    RosterSecrets,
    capped_count,
    reads_as_operator,
    roster_filters_from_query,
    roster_where,
)
from app.tenancy.labels import org_refs

router = APIRouter(tags=["credentials"])

#: A node's count stops here; past it the count is this and `count_capped` true ("1,000+").
NODE_COUNT_CAP = 1_000
#: The most filtered rows under a level read by one GROUP BY (the sparse branch); past it, the dense one.
SPARSE_CAP = 10_000
#: Children per page, by level; a folder's leaves page at `FOLDER_PAGE` too.
TENANT_PAGE = 50
HOME_PAGE = 100
FOLDER_PAGE = 100
#: The dense branch reads at most this many candidate pages to fill one page of non-empty children, bounding a
#: request's work. A page it cannot fill comes back short (even empty) WITH a cursor: the client loads more.
_DENSE_ROUNDS = 3

_HOME_KINDS = {"o": "org", "s": "system", "p": "project", "u": "user"}

TreeLevel = Literal["tenants", "homes", "folder"]


class TenantNodeOut(BaseModel):
    org_id: UUID
    name: str
    count: int
    count_capped: bool = False


class HomeNodeOut(BaseModel):
    #: `Credential.home_key`, the `home` of this home's `level=folder` requests.
    key: str
    kind: Literal["org", "system", "project", "user"]
    #: The home's own segment of `CredentialOut.home_path`: the org's name, `system`, the project's name or the
    #: owner's email (an id when it no longer resolves).
    label: str
    count: int
    count_capped: bool = False


class FolderNodeOut(BaseModel):
    #: One segment, as stored in `parent_path` (lowercase); the child's `path` is the parent's plus `/name`.
    name: str
    count: int
    count_capped: bool = False


class CredentialTreeOut(BaseModel):
    """One level: only the lists that level fills are non-empty. `next_cursor` continues the same level (a folder
    pages its subfolders first, then its leaves); null on the last page."""

    tenants: list[TenantNodeOut] = []
    homes: list[HomeNodeOut] = []
    folders: list[FolderNodeOut] = []
    leaves: list[CredentialOut] = []
    next_cursor: Optional[str] = None


# ── Shared pieces ─────────────────────────────────────────────────────────────


def _is_filtered(f: RosterFilters) -> bool:
    """Does `f` narrow beyond the caller's scope? `mine` is part of the scope, so it is not a filter here."""
    return any((f.type, f.q, f.owner_ids, f.org_id, f.secrets, f.health))


def _capped(n: int) -> tuple[int, bool]:
    return (NODE_COUNT_CAP, True) if n > NODE_COUNT_CAP else (n, False)


def _node_count(session: Session, where: Sequence[Any]) -> tuple[int, bool]:
    return capped_count(session, where, NODE_COUNT_CAP)


def _pat(column: Any, op: str, value: Any) -> Any:
    """A byte-wise comparison (`~<~`, `~>=~`, …), the operators `text_pattern_ops` indexes."""
    # Grouped: Postgres gives `~>~` and `||` one precedence, so `a ~>~ b || c` would read `(a ~>~ b) || c`.
    bound = literal(value, Text) if isinstance(value, str) else Grouping(value)
    return column.op(op, is_comparison=True)(bound)


# ── The cursor ────────────────────────────────────────────────────────────────


def _result(*parts: Any) -> str:
    """Which listing a cursor pages: level, place and filters. Any difference refuses the cursor."""
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:16]


_SHAPES: dict[str, list[str]] = {
    "tenants": ["tree", "result", "org_lname", "org_id"],
    "homes": ["tree", "result", "home"],
    "folders": ["tree", "result", "seg"],
    "leaves": ["tree", "result", "lname", "id"],
}


def _encode(phase: str, result: str, *values: Any) -> str:
    return keyset.encode_cursor(_SHAPES[phase], [phase, result, *values])


def _decode(raw: str, phases: Sequence[str], result: str) -> tuple[str, list[Any]]:
    """`(phase, key values)` of a cursor minted for this listing in one of `phases`, else a 422."""
    for phase in phases:
        try:
            values = keyset.decode_cursor(
                raw, _SHAPES[phase],
                is_stamp=lambda _name: False,
                is_uuid=lambda name: name in ("id", "org_id"),
                is_text=lambda name: name in ("tree", "result", "org_lname", "home", "seg", "lname"),
                detail=STALE_CURSOR, status=422,
            )
        except HTTPException:
            continue
        if values[0] == phase and values[1] == result:
            return phase, values[2:]
    raise HTTPException(status_code=422, detail=STALE_CURSOR)


# ── Children: the walk, the sparse GROUP BY, the dense candidates ─────────────


class TreeLevelSql:
    """One level's children as SQL: `under` (the rows beneath every child), `seg` (a row's child), `walk` (the
    skip-scan's child names after a given one, in order) and `child` (the rows under one child)."""

    def under(self) -> list[Any]:
        raise NotImplementedError

    def seg(self) -> Any:
        raise NotImplementedError

    def ordered(self, expr: Any) -> Any:
        """`expr` in the walk's order, for the GROUP BY's ORDER BY and its cursor bound."""
        return expr

    def walk(self, scope: Sequence[Any], after: Optional[str], limit: int) -> Select:
        raise NotImplementedError

    def child(self, name: str) -> list[Any]:
        raise NotImplementedError


class HomesLevel(TreeLevelSql):
    """The distinct `home_key`s under one org (or the caller's whole scope), by `ix_credentials_org_home_key` (a
    tenant's seam adds its org)."""

    def __init__(self, org_id: Optional[UUID]) -> None:
        self.org_id = org_id

    def under(self) -> list[Any]:
        return [] if self.org_id is None else [Credential.org_id == self.org_id]

    def seg(self) -> Any:
        return Credential.home_key

    def walk(self, scope: Sequence[Any], after: Optional[str], limit: int) -> Select:
        common = [*scope, *self.under()]

        def probe(bound: Optional[Any]) -> Any:
            stmt = select(Credential.home_key).where(*common)
            if bound is not None:
                stmt = stmt.where(Credential.home_key > bound)
            return stmt.order_by(Credential.home_key).limit(1).scalar_subquery()

        anchor = select(probe(after).label("seg"))
        walk = anchor.cte("home_walk", recursive=True)
        walk = walk.union_all(select(probe(walk.c.seg)).where(walk.c.seg.is_not(None)))
        return select(walk.c.seg).where(walk.c.seg.is_not(None)).limit(limit)

    def child(self, name: str) -> list[Any]:
        return [*self.under(), Credential.home_key == name]


#: A row's folder key: `parent_path || '/'`, indexed as `ix_credentials_home_folder_key` (the module docstring says
#: why not `parent_path` itself). Inline `'/'`, not a bound one, so the planner matches the index's expression.
FOLDER_KEY = Credential.parent_path.op("||")(literal_column("'/'", Text))
#: Spelled with the table's own name: every probe selects from `credentials` unaliased (the seam adds criteria,
#: never an alias). `USING ~<~` has no SQLAlchemy spelling.
_FOLDER_KEY_ORDER = text("(credentials.parent_path || '/') USING ~<~")


class FolderLevel(TreeLevelSql):
    """The next segments under `path` in `home`, by `ix_credentials_home_folder_key`. `base` is `''` at the home's
    root, else `path || '/'`: every folder key under the level lies in `(base, path || '0')`, one child's in
    `[base || name || '/', base || name || '0')` (`0` follows `/`). At the root a row's key is `'/'` itself (no
    folder), so the root's range is the two sides of it."""

    def __init__(self, home: str, path: str) -> None:
        self.home = home
        self.path = path
        self.base = "" if path == "" else f"{path}/"

    def _ranges(self) -> list[list[Any]]:
        """The level as index ranges: two at the root, around `'/'`; one below."""
        if self.path == "":
            return [[_pat(FOLDER_KEY, "~<~", "/")], [_pat(FOLDER_KEY, "~>~", "/")]]
        return [[_pat(FOLDER_KEY, "~>~", self.base), _pat(FOLDER_KEY, "~<~", f"{self.path}0")]]

    def under(self) -> list[Any]:
        if self.path == "":
            return [Credential.home_key == self.home, Credential.parent_path != ""]
        return [Credential.home_key == self.home, *self._ranges()[0]]

    def _seg_of(self, key: Any) -> Any:
        rest = key if self.base == "" else func.substr(key, len(self.base) + 1)
        return func.split_part(rest, "/", 1)

    def seg(self) -> Any:
        return self._seg_of(Credential.parent_path)

    def ordered(self, expr: Any) -> Any:
        # A child's place in the walk is its key's: `name || '/'`, byte-wise.
        return collate(expr.op("||")(literal_column("'/'", Text)), "C")

    def walk(self, scope: Sequence[Any], after: Optional[Any], limit: int) -> Select:
        common = [*scope, Credential.home_key == self.home]

        def first_after(seg: Optional[Any]) -> Any:
            """The first folder key past child `seg`'s block (from the level's start when None), over each range
            in order: every key of the first range sorts before any of the second."""
            probes = []
            for bounds in self._ranges():
                past = [] if seg is None else [_pat(FOLDER_KEY, "~>=~", literal(self.base, Text) + seg + "0")]
                probes.append(
                    select(FOLDER_KEY).where(*common, *bounds, *past)
                    # The index's own order, so LIMIT 1 is one index probe.
                    .order_by(_FOLDER_KEY_ORDER).limit(1).scalar_subquery()
                )
            return func.coalesce(*probes) if len(probes) > 1 else probes[0]

        start = first_after(None if after is None else literal(after, Text))
        walk = select(self._seg_of(start).label("seg")).cte("folder_walk", recursive=True)
        walk = walk.union_all(select(self._seg_of(first_after(walk.c.seg))).where(walk.c.seg.is_not(None)))
        return select(walk.c.seg).where(walk.c.seg.is_not(None)).limit(limit)

    def child(self, name: str) -> list[Any]:
        q = f"{self.base}{name}"
        return [Credential.home_key == self.home, _pat(FOLDER_KEY, "~>=~", f"{q}/"), _pat(FOLDER_KEY, "~<~", f"{q}0")]


def level_children(
    session: Session, level: TreeLevelSql, *, scope: Sequence[Any], where: Sequence[Any], filtered: bool,
    after: Optional[str], per: int,
) -> tuple[list[tuple[str, int, bool]], Optional[str]]:
    """Up to `per` children after `after` as `(name, count, capped)`, and the child to resume after (None: the
    last page)."""
    # Counted even unfiltered: a caller whose scope is narrow (a member's own rows) is sparse too.
    _matched, dense = capped_count(session, [*where, *level.under()], SPARSE_CAP)
    if not dense:
        return _sparse(session, level, where, after, per)
    out: list[tuple[str, int, bool]] = []
    cursor = after
    for _round in range(_DENSE_ROUNDS if filtered else 1):
        # Unfiltered, `where` IS the scope, and every child the walk finds holds a row.
        names = list(session.scalars(level.walk(scope, cursor, per + 1)))
        for index, name in enumerate(names[:per]):
            count, capped = _node_count(session, [*where, *level.child(name)])
            cursor = name
            if count == 0:
                continue
            out.append((name, count, capped))
            if len(out) == per:
                return out, (name if index + 1 < len(names) else None)
        if len(names) <= per:
            return out, None
    # The candidate budget ran out before the page filled: the next page resumes where the walk stopped.
    return out, cursor


def _sparse(
    session: Session, level: TreeLevelSql, where: Sequence[Any], after: Optional[str], per: int,
) -> tuple[list[tuple[str, int, bool]], Optional[str]]:
    """Few rows match: one GROUP BY over exactly those rows names every child and its exact count."""
    # Named in a subquery, so GROUP BY and ORDER BY read one column rather than two bindings of one expression.
    seg = select(level.seg().label("seg")).where(*where, *level.under()).subquery("matched").c.seg
    grouped = select(seg, func.count()).group_by(seg)
    if after is not None:
        grouped = grouped.where(level.ordered(seg) > level.ordered(literal(after, Text)))
    rows = session.execute(grouped.order_by(level.ordered(seg)).limit(per + 1)).all()
    page = [(name, *_capped(count)) for name, count in rows[:per]]
    return page, (page[-1][0] if len(rows) > per else None)


# ── Levels ────────────────────────────────────────────────────────────────────


def tenant_nodes(
    session: Session, where: Sequence[Any], after: Optional[list[Any]], result: str,
) -> CredentialTreeOut:
    """Orgs holding at least one row in scope, by `(lower(name), id)` (`ix_orgs_lname_id`), each tested by an
    EXISTS on `ix_credentials_org_created_id`. A row whose `org_id` names no org is under no tenant."""
    lname = func.lower(Org.name)
    holds = exists().where(Credential.org_id == Org.id, *where)
    stmt = select(Org.id, Org.name, lname.label("lname")).where(holds)
    if after is not None:
        stmt = stmt.where(keyset.row_after([(lname, True), (Org.id, True)], after))
    rows = session.execute(stmt.order_by(lname, Org.id).limit(TENANT_PAGE + 1)).all()
    page = rows[:TENANT_PAGE]
    tenants = []
    for row in page:
        count, capped = _node_count(session, [*where, Credential.org_id == row.id])
        tenants.append(TenantNodeOut(org_id=row.id, name=row.name, count=count, count_capped=capped))
    more = len(rows) > TENANT_PAGE
    return CredentialTreeOut(
        tenants=tenants,
        next_cursor=_encode("tenants", result, page[-1].lname, page[-1].id) if more else None,
    )


def _home_labels_of(session: Session, keys: Sequence[str]) -> dict[str, str]:
    """Each home key's label (`HomeNodeOut.label`), one query per kind."""
    by_kind: dict[str, list[str]] = {}
    for key in keys:
        kind, _sep, ident = key.partition(":")
        by_kind.setdefault(kind, []).append(ident)
    orgs = org_refs(session, [UUID(i) for i in by_kind.get("o", [])])
    projects = dict(session.execute(
        select(Project.id, Project.name).where(Project.id.in_([int(i) for i in by_kind.get("p", [])]))
    ).tuples().all()) if by_kind.get("p") else {}
    emails = dict(session.execute(
        select(AdminUser.id, AdminUser.email).where(AdminUser.id.in_([UUID(i) for i in by_kind.get("u", [])]))
    ).tuples().all()) if by_kind.get("u") else {}
    labels = {}
    for key in keys:
        kind, _sep, ident = key.partition(":")
        if kind == "o":
            ref = orgs.get(UUID(ident))
            labels[key] = ref.name if ref is not None else ident
        elif kind == "s":
            labels[key] = OWNERSHIP_SYSTEM
        elif kind == "p":
            labels[key] = projects.get(int(ident), ident)
        else:
            labels[key] = emails.get(UUID(ident), ident)
    return labels


def _home_rank(key: str) -> tuple[int, str]:
    """Org shared first, then system, projects and people: the order a tenant reads its homes in."""
    return ("ospu".index(key[0]) if key[:1] in _HOME_KINDS else 4, key)


def _homes(
    session: Session, org_id: Optional[UUID], *, scope: Sequence[Any], where: Sequence[Any], filtered: bool,
    after: Optional[str], result: str,
) -> CredentialTreeOut:
    children, resume = level_children(
        session, HomesLevel(org_id), scope=scope, where=where, filtered=filtered, after=after, per=HOME_PAGE,
    )
    labels = _home_labels_of(session, [name for name, _c, _x in children])
    homes = [
        HomeNodeOut(key=name, kind=_HOME_KINDS[name[0]], label=labels[name], count=count, count_capped=capped)  # type: ignore[arg-type]
        for name, count, capped in children
    ]
    # Within a page only: the walk's order is the key's (`o: < p: < s: < u:`), which pages stably; an org with more
    # than a page of homes shows later pages in key order.
    homes.sort(key=lambda home: _home_rank(home.key))
    return CredentialTreeOut(
        homes=homes, next_cursor=_encode("homes", result, resume) if resume is not None else None,
    )


def leaf_statement(
    where: Sequence[Any], home: str, path: str, after: Optional[list[Any]], per: int,
) -> Select:
    """Up to `per + 1` rows directly in the folder after `after`, by `(lower(name), id)` on
    `ix_credentials_home_parent_lname_id`."""
    lname = func.lower(Credential.name)
    stmt = select(Credential, lname.label("lname")).where(
        *where, Credential.home_key == home, Credential.parent_path == path,
    )
    if after is not None:
        stmt = stmt.where(keyset.row_after([(lname, True), (Credential.id, True)], after))
    return stmt.order_by(lname, Credential.id).limit(per + 1)


def _leaves(
    session: Session, principal: Principal | None, where: Sequence[Any], home: str, path: str,
    after: Optional[list[Any]], per: int,
) -> tuple[list[CredentialOut], Optional[list[Any]]]:
    """The rows directly in the folder, serialized, and the key to resume after (None: the last page)."""
    found = session.execute(leaf_statement(where, home, path, after, per)).all()
    rows = [r[0] for r in found[:per]]
    actors = _actor_names(session, rows)
    orgs = org_refs(session, [row.org_id for row in rows])
    homes = _home_labels(session, rows)
    out = [
        _credential_out(row, actor_names=actors, org_labels=orgs, home_labels=homes, editable=_may_edit(principal, row))
        for row in rows
    ]
    last = found[per - 1] if len(found) > per else None
    return out, ([last.lname, last[0].id] if last is not None else None)


def _folder(
    session: Session, principal: Principal | None, home: str, path: str, *, scope: Sequence[Any],
    where: Sequence[Any], filtered: bool, cursor: Optional[str], result: str,
) -> CredentialTreeOut:
    """The folder's subfolders, page by page, then its leaves: a page that ends the subfolders starts the
    leaves, so a small folder is one request."""
    phase, values = _decode(cursor, ("folders", "leaves"), result) if cursor is not None else ("folders", [])
    folders: list[FolderNodeOut] = []
    if phase == "folders":
        children, resume = level_children(
            session, FolderLevel(home, path), scope=scope, where=where, filtered=filtered,
            after=values[0] if values else None, per=FOLDER_PAGE,
        )
        # Shown by name: the walk's `name || '/'` order puts `a-b` before `a`, which reads as a mistake. Within the
        # page only, so paging stays the walk's; a folder rarely holds more than one page of subfolders.
        folders = [
            FolderNodeOut(name=name, count=count, count_capped=capped)
            for name, count, capped in sorted(children, key=lambda child: child[0].encode())
        ]
        if resume is not None:
            return CredentialTreeOut(folders=folders, next_cursor=_encode("folders", result, resume))
        values = []
    leaves, last = _leaves(session, principal, where, home, path, values or None, FOLDER_PAGE)
    return CredentialTreeOut(
        folders=folders, leaves=leaves, next_cursor=_encode("leaves", result, *last) if last else None,
    )


# ── The route ─────────────────────────────────────────────────────────────────


def _valid_home(home: str) -> str:
    kind, sep, ident = home.partition(":")
    try:
        if not sep or kind not in _HOME_KINDS:
            raise ValueError(home)
        int(ident) if kind == "p" else UUID(ident)
    except ValueError:
        raise HTTPException(status_code=422, detail="home is not a credential home") from None
    return home


def _valid_path(path: str) -> str:
    """A folder path as `parent_path` stores it: lowercase, no trailing `/`, `''` at the home's root."""
    if "\x00" in path or path != path.lower() or path.endswith("/") or path.startswith("/"):
        raise HTTPException(status_code=422, detail="path must be a lowercase folder path without a trailing '/'")
    return path


# Included before `reads.router` in `main.py`: its `/api/credentials/{credential_id}` would swallow `/tree`.
@router.get("/api/credentials/tree", response_model=CredentialTreeOut)
@feature("credentials", "view", own_credential=True)
def credential_tree(
    level: TreeLevel = Query(),
    org_id: Optional[UUID] = Query(default=None),
    home: Optional[str] = Query(default=None, max_length=64),
    path: str = Query(default="", max_length=_NAME_MAX_LEN),
    cursor: Optional[str] = Query(default=None, max_length=2048),
    # The roster's filters (`GET /api/credentials`), parsed by the same function. No `org_id` filter: here
    # `org_id` names the tenant whose homes to list, and the console narrows orgs by its global filter's header.
    type_: Optional[str] = Query(default=None, alias="type"),
    q: Optional[str] = Query(default=None, max_length=_NAME_MAX_LEN),
    secrets: Optional[RosterSecrets] = Query(default=None),
    mine: bool = Query(default=False),
    owner_id: list[UUID] = Query(default=[]),
    health: Optional[RosterHealth] = Query(default=None),
    _user: AdminUser = Depends(require_user),
    principal: Principal | None = Depends(current_principal),
    session: Session = Depends(get_session),
) -> CredentialTreeOut:
    """One level of the tree under the roster's filters (module docstring). `tenants` is the operator's root and a
    422 for anyone else; `homes` takes `org_id` (required of the operator); `folder` takes `home` and `path`."""
    filters = roster_filters_from_query(
        type_slug=type_, q=q, owner_ids=owner_id, secrets=secrets, health=health, mine=mine,
    )
    where = roster_where(session, principal, filters)
    # The walk's candidates: the caller's scope alone, so a filter never hides a child the dense branch counts.
    scope = roster_where(session, principal, RosterFilters(mine=filters.mine))
    filtered = _is_filtered(filters)
    # D1: only the operator plane lists tenants (`mine` or not); a tenant's root is its own org's homes.
    operator = reads_as_operator(principal, RosterFilters())
    if level == "tenants":
        if not operator:
            raise HTTPException(status_code=422, detail="level=tenants is the operator's; list homes instead")
        result = _result("tenants", filters.digest())
        after = _decode(cursor, ("tenants",), result)[1] if cursor is not None else None
        return tenant_nodes(session, where, after, result)
    if level == "homes":
        if operator and org_id is None:
            raise HTTPException(status_code=422, detail="org_id is required")
        result = _result("homes", org_id, filters.digest())
        after = _decode(cursor, ("homes",), result)[1][0] if cursor is not None else None
        return _homes(session, org_id, scope=scope, where=where, filtered=filtered, after=after, result=result)
    if home is None:
        raise HTTPException(status_code=422, detail="home is required")
    home, path = _valid_home(home), _valid_path(path)
    result = _result("folder", home, path, filters.digest())
    return _folder(
        session, principal, home, path, scope=scope, where=where, filtered=filtered, cursor=cursor, result=result,
    )
