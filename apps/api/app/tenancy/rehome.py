"""Moving a set of rows, and everything derived from them, to another org — in one statement per linked group.

`keys.py`'s composite `(<fk>, org_id)` keys are `ON UPDATE NO ACTION` and not deferrable, so neither
parent nor child can move alone; a data-modifying CTE is one statement, checked at its end, so moving
children and parents together passes. `rehome_set` is the only deliberate re-home path (`rehome_row` is
one root of it); a sequence of single-row moves deadlocks (`project_credentials` blocks a project and its
credential in either order), so a multi-entity move is ONE set.

Which rows move: `composite_keys()` via `child_tables_of` (one hop; every derivation parent is
`Stored`). Which keys can refuse: `all_org_keys()` via `_blockers`, including same-org keys
pointing at something that moves; a same-org key never widens the move. A refusable move raises
`RehomeBlocked` before any write. A reference with no key at all is neither moved nor refused: the org
transfer (`app/tenancy/transfer.py`) gives each one a policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text

# Imported rather than re-spelled: `keys.py` is the single home of the identifier shape.
from app.tenancy.keys import (
    ORG_COLUMN,
    PARENT_ID_COLUMN,
    CompositeKey,
    _IDENTIFIER_RE,
    all_org_keys,
    composite_keys,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Collection, Mapping, Sequence

    from sqlalchemy.orm import Session


def _identifier(value: str, what: str) -> str:
    """`value`, if it is a bare identifier of the shape this schema uses — otherwise a refusal.

    Refuses rather than quotes, like `keys.py`. `fullmatch` because `$` matches before a newline."""
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{what} needs quoting and this statement interpolates bare; got {value!r}"
        )
    return value


def child_tables_of(parent_table: str) -> dict[str, tuple[str, ...]]:
    """`{child table: the fk columns that reach `parent_table`}`, off the census.

    Columns are grouped per table so a table reaching the parent through several (`task_dependencies`:
    `task_id`, `depends_on_task_id`) is one `WHERE ... OR ...` update. Answers "which rows move", not
    "may they" (`_blockers`). Reads `composite_keys()` alone on purpose: a same-org referencer
    (`role_bindings`) must not follow its object across the boundary."""
    found: dict[str, list[str]] = {}
    for key in composite_keys():
        if key.parent_table != parent_table:
            continue
        # `child_columns` is `(<fk column>, org_id)` by construction; the fk column is the one read.
        fk_column = next(c for c in key.child_columns if c != ORG_COLUMN)
        found.setdefault(key.table, [])
        if fk_column not in found[key.table]:
            found[key.table].append(fk_column)
    return {table: tuple(columns) for table, columns in found.items()}


class RehomeBlocked(Exception):
    """A move cannot happen because a keyed row would be left naming a pair it no longer holds.

    Raised before any write. `child_table`/`constraint` name the first refusing key; `rows` lists EVERY
    refusing row as `(table, primary key)` — the manifest an operator acts on. `child_moves` picks the
    remedy: a moving row is blocked by a parent that is not moving (a wider move would cascade without
    bound); a non-moving row derives its org elsewhere and must be re-pointed or deleted first."""

    def __init__(
        self,
        parent_table: str,
        row_id: object,
        child_table: str,
        constraint: str,
        *,
        child_moves: bool,
        rows: "Sequence[tuple[str, object]]" = (),
    ) -> None:
        self.parent_table = parent_table
        self.row_id = row_id
        self.child_table = child_table
        self.constraint = constraint
        self.child_moves = child_moves
        self.rows: list[tuple[str, object]] = list(rows)
        if child_moves:
            detail = (
                f"its {child_table} row(s) also belong to a parent that is not moving, which "
                f"{constraint} would refuse. Moving that parent too would cascade without bound, so "
                "this is a refusal rather than a wider move."
            )
        else:
            detail = (
                f"{child_table} row(s) reference it through {constraint} and do NOT move with it — "
                f"{child_table} derives its own org elsewhere, so the move would leave those rows naming "
                "a pair this row no longer holds. Delete or re-point them first; they must not follow "
                "their object across the boundary."
            )
        super().__init__(f"{parent_table} {row_id!r} cannot be re-homed: {detail}")


def _fk_column(key: CompositeKey) -> str:
    """The key's referencing column — `child_columns` is `(<fk column>, org_id)` by construction."""
    return _identifier(next(c for c in key.child_columns if c != ORG_COLUMN), "foreign-key column")


def _arms(roots: "Mapping[str, str]") -> dict[str, list[tuple[str, str]]]:
    """`{table: [(column, bind name), ...]}` — which rows of each table move, as OR'd `column = ANY(:bind)`.

    `roots` maps each root table to the bind name of its id array. A root moves by `id`; each `Derived`
    child follows through every fk column reaching a moving root (`child_tables_of`). ONE entry per table
    however it is reached, because Postgres applies only one of two modifications to the same row in one
    statement."""
    arms: dict[str, list[tuple[str, str]]] = {}
    for table, bind in roots.items():
        arms.setdefault(table, []).append((PARENT_ID_COLUMN, bind))
        for child, columns in child_tables_of(table).items():
            for column in columns:
                arms.setdefault(_identifier(child, "child table"), []).append(
                    (_identifier(column, "foreign-key column"), bind)
                )
    return arms


def _moves(arms: dict[str, list[tuple[str, str]]], table: str, alias: str) -> str:
    """SQL that is TRUE when `alias` (a row of `table`) is in the moving set — NULL, not FALSE, for a row
    reached only through a NULL fk.

    Kept bare (no `COALESCE`) so each `= ANY` stays index-usable; a negation spells `IS NOT TRUE`, never
    `NOT`, which a NULL would turn into a hidden blocker."""
    if table not in arms:
        return "FALSE"
    return "(" + " OR ".join(f"{alias}.{column} = ANY(:{bind})" for column, bind in arms[table]) + ")"


def _primary_key(table: str) -> list[str]:
    """`table`'s primary-key columns, off `Base.metadata` — a join row (`project_credentials`) has no `id`.

    Deferred import, as in `keys.py`: `app.models` at module scope breaks the bare-host alembic walk."""
    from app.models.base import Base

    return [_identifier(c.name, "primary-key column") for c in Base.metadata.tables[table].primary_key]


def _value(alias: str, table: str, column: str, repoints: "Mapping[tuple[str, str], tuple[str, str]]") -> str:
    """SQL for the value `alias.column` holds AFTER the statement: the re-pointed value when `rehome_set` was
    asked to re-point that column, else the column itself. A value missing from the map keeps its own."""
    if (table, column) not in repoints:
        return f"{alias}.{column}"
    old, new = repoints[(table, column)]
    return (
        f"COALESCE((SELECT m.n FROM unnest(:{old}, :{new}) AS m(o, n) WHERE m.o = {alias}.{column}), "
        f"{alias}.{column})"
    )


def _blockers(
    session: "Session",
    arms: dict[str, list[tuple[str, str]]],
    params: dict[str, object],
    repoints: "Mapping[tuple[str, str], tuple[str, str]]" = {},  # noqa: B006 - read-only
) -> "list[tuple[CompositeKey, str, object, bool]]":
    """Every `(key, table, primary key, whether that row moves)` the move would leave violating a key.

    The whole set is treated as moving: after the statement a moving row holds `new_org`, a still row its
    own org, so a key breaks exactly when one side moves and the other does not end in `new_org`. Rows
    where both or neither move are untouched. Every key from both generators is probed (`all_org_keys()`)
    whose table or parent moves; moving-table keys first, so the reported refusal prefers the moving row
    as `rehome_row`'s probe did. One `UNION ALL` branch per moving side, each driven by its indexed
    `= ANY`. A NULL fk is never a blocker (MATCH SIMPLE): the inner join drops it. A row refused through
    two keys is listed once. A moving row's re-pointed column is judged on the value it will hold
    (`_value`); a still row is never re-pointed."""
    keys = [k for k in all_org_keys() if k.table in arms or k.parent_table in arms]
    keys.sort(key=lambda k: k.table not in arms)
    found: list[tuple[CompositeKey, str, object, bool]] = []
    for key in keys:
        child = _identifier(key.table, "referencing table")
        key_parent = _identifier(key.parent_table, "referenced table")
        child_moves = _moves(arms, key.table, "c")
        parent_moves = _moves(arms, key.parent_table, "p")
        pk = _primary_key(key.table)
        head = (
            f"SELECT {', '.join(f'c.{column}' for column in pk)}, {{moves}} "
            f"FROM {child} c JOIN {key_parent} p ON p.{PARENT_ID_COLUMN} = {{fk}} WHERE "
        )
        plain = f"c.{_fk_column(key)}"
        branches = []
        if key.table in arms:  # a moving row whose parent stays outside `new_org`
            branches.append(
                head.format(moves="TRUE", fk=_value("c", key.table, _fk_column(key), repoints))
                + f"{child_moves} AND {parent_moves} IS NOT TRUE "
                f"AND p.{ORG_COLUMN} IS DISTINCT FROM :new_org"
            )
        if key.parent_table in arms:  # a still row, outside `new_org`, naming a moving parent
            branches.append(
                head.format(moves="FALSE", fk=plain) + f"{parent_moves} AND {child_moves} IS NOT TRUE "
                f"AND c.{ORG_COLUMN} IS DISTINCT FROM :new_org"
            )
        found.extend(
            (key, key.table, row[0] if len(pk) == 1 else tuple(row[:-1]), bool(row[-1]))
            for row in session.execute(text(" UNION ALL ".join(branches)), params)
        )
    unique: dict[tuple[str, object], tuple[CompositeKey, str, object, bool]] = {}
    for blocker in found:
        unique.setdefault((blocker[1], blocker[2]), blocker)
    return list(unique.values())


def _components(tables: "Collection[str]") -> list[list[str]]:
    """The moving tables grouped into connected components of the composite-key graph (`all_org_keys()`).

    Tables a key links must share a statement — the key is checked at statement end — and unlinked ones
    need not, so each component is one statement and none grows past what its keys demand."""
    group = {table: table for table in tables}

    def root(table: str) -> str:
        while group[table] != table:
            table = group[table]
        return table

    for key in all_org_keys():
        if key.table in group and key.parent_table in group:
            group[root(key.table)] = root(key.parent_table)
    components: dict[str, list[str]] = {}
    for table in sorted(tables):
        components.setdefault(root(table), []).append(table)
    return list(components.values())


def rehome_set(
    session: "Session",
    roots: "Mapping[str, Collection[object]]",
    new_org: UUID,
    *,
    repoint: "Mapping[str, Mapping[str, Mapping[object, object]]] | None" = None,
) -> dict[str, int]:
    """Move every root row, and every row derived from one, into `new_org`; `{table: rows moved}`.

    `roots` maps each `Stored` table to the ids that move. The set moves as one: a key between two moving
    rows is satisfied by the move itself, which is why a project, its `project_credentials` and the
    credential they name — refused in either order by `rehome_row` — move here. Raises `RehomeBlocked`
    (every refusing row in `rows`) before any write. Does not commit — the caller's transaction is the
    unit of work.

    One data-modifying CTE per connected component of the composite-key graph (`_components`), one arm per
    table with its reaching predicates OR'd (`_arms`). CTE arm order is presentation (one snapshot).
    `org_id IS DISTINCT FROM :new_org` on every arm makes a partial re-home complete and a rerun safe;
    never `<>`, which is NULL for an unstamped row (`app/placement_grants.py::placement_org`). Every
    identifier is validated even though all come from the census.

    `repoint` (`{table: {column: {old value: new value}}}`) rewrites a moving row's reference in the SAME
    statement that moves it — the only order a keyed reference into a table that does not move can take,
    since it must agree with the row's org at statement end (`app/tenancy/transfer.py`'s `Repoint`: a
    feature's lead, a rating's author, re-pointed to the same person's membership in `new_org`). Only rows
    that move are rewritten, and a value absent from its map keeps its own.
    """
    # The UUID object, never `str(new_org)`: an untyped `text()` bind of a str is sent as `varchar` and
    # fails against a `uuid` column, and `:new_org::uuid` is misparsed by `text()`.
    params: dict[str, object] = {"new_org": new_org}
    binds: dict[str, str] = {}
    for index, (table, ids) in enumerate(sorted(roots.items())):
        if isinstance(ids, (str, bytes)):
            raise TypeError(f"rehome_set: {table}'s ids must be a collection of ids, not {ids!r}")
        if ids:  # an empty array has no element type to bind, and moves nothing anyway
            binds[_identifier(table, "root table")] = f"ids{index}"
            params[f"ids{index}"] = list(ids)
    arms = _arms(binds)
    repoints: dict[tuple[str, str], tuple[str, str]] = {}
    for table, columns in sorted((repoint or {}).items()):
        for column, mapping in sorted(columns.items()):
            if not mapping:
                continue
            if _identifier(table, "re-pointed table") not in arms:
                raise ValueError(f"rehome_set: {table} is re-pointed but none of its rows move")
            index = len(repoints)
            repoints[(table, _identifier(column, "re-pointed column"))] = (f"rpo{index}", f"rpn{index}")
            params[f"rpo{index}"] = list(mapping)
            params[f"rpn{index}"] = list(mapping.values())

    # Asked before anything is written, so a blocked move leaves no half transaction.
    blocked = _blockers(session, arms, params, repoints)
    if blocked:
        key, _table, _row, child_moves = blocked[0]
        moving = [(table, row_id) for table in binds for row_id in roots[table]]
        parent_table, row_id = moving[0] if len(moving) == 1 else (
            ", ".join(binds), tuple(row_id for _, row_id in moving)
        )
        raise RehomeBlocked(
            parent_table, row_id, key.table, key.name, child_moves=child_moves,
            rows=[(table, row) for _key, table, row, _moves in blocked],
        )

    moved: dict[str, int] = {}
    for component in _components(arms):
        ctes = []
        for index, table in enumerate(component):
            sets = "".join(
                f", {column} = {_value('c', table, column, repoints)}"
                for (repointed, column) in repoints if repointed == table
            )
            ctes.append(
                f"a{index} AS (UPDATE {table} c SET {ORG_COLUMN} = :new_org{sets} "
                f"WHERE {_moves(arms, table, 'c')} AND c.{ORG_COLUMN} IS DISTINCT FROM :new_org RETURNING 1)"
            )
        counts = ", ".join(f"(SELECT count(*) FROM a{index})" for index in range(len(component)))
        row = session.execute(text(f"WITH {', '.join(ctes)} SELECT {counts}"), params).one()
        moved.update(zip(component, row))
    return moved


def rehome_row(session: "Session", parent_table: str, row_id: object, new_org: UUID) -> bool:
    """Move `parent_table`'s row `row_id`, and every row derived from it, into `new_org`.

    `rehome_set` over one root. Returns whether the parent moved (`False`: already in that org). Raises
    `RehomeBlocked` if the move is impossible; does not commit."""
    parent = _identifier(parent_table, "parent table")
    return rehome_set(session, {parent: [row_id]}, new_org).get(parent, 0) > 0
