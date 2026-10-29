"""The composite org keys: `(parent_id, org_id) REFERENCES parent(id, org_id)`, one per derivation path.

Derived from `app/tenancy/manifest.py`, never typed out or counted. `on_delete` is read from the
manifest, never inferred; SET NULL uses the column-list form so `org_id` is never nulled. With
`org_id` NOT NULL (`models.OrgScoped`) cross-org children are unrepresentable to origin sessions
(`session_replication_role = 'replica'` bypasses FKs).
`same_org_keys()` renders `SameOrg` references (declared on `Stored` and `Derived` entries alike) in the
same shape, kept apart from `composite_keys()` (which alone feeds the seams); `all_org_keys()` is the union.
`ON UPDATE` is NO ACTION.
"""

from __future__ import annotations

import re
from typing import NamedTuple

#: The tenancy column, on the child and on the parent alike.
ORG_COLUMN = "org_id"

#: The parent column a derivation reaches: always the primary key, since every parent is `Stored`.
PARENT_ID_COLUMN = "id"

#: Postgres's identifier ceiling. An over-long name is silently truncated, so the migration's
#: `pg_constraint` lookup would never find it. Checked.
_MAX_IDENTIFIER = 63

#: Every table and column name reaching a rendered statement is checked against this (identifier hygiene,
#: not injection control). `\A`/`\Z` rather than `^`/`$`, whose `$` matches before a trailing newline:
#: the constant is exported (`app/tenancy/rehome.py`), so the pattern itself must be safe.
_IDENTIFIER_RE = re.compile(r"\A[a-z][a-z0-9_]*\Z")

#: The delete actions a composite key may carry, and whether each nulls its own columns (deciding the
#: column-list form). A new action must be classified here.
_NULLS_ITS_COLUMNS: dict[str, bool] = {
    "CASCADE": False,
    "RESTRICT": False,
    "NO ACTION": False,
    "SET NULL": True,
    "SET DEFAULT": True,
}


class CompositeKey(NamedTuple):
    """One org key — a derivation path's or a same-org reference's; the same shape and type.
    `child_columns` and `parent_columns` are positional pairs.

    Consumers that render or validate keys need not know why a key exists. The read and write seams call
    `composite_keys()` directly, so a same-org reference is never read as an org source.
    """

    table: str
    parent_table: str
    child_columns: tuple[str, str]
    parent_columns: tuple[str, str]
    on_delete: str
    name: str


def key_name(table: str, column: str) -> str:
    """`fk_task_ratings_user_id_org` — the constraint's name, and the one place it is spelled.

    Keyed on the local column, since `task_dependencies` reaches one parent through two columns.
    """
    return f"fk_{table}_{column}_org"


def _action_sql(on_delete: str, referencing_column: str) -> str:
    """`ON DELETE CASCADE`, or `ON DELETE SET NULL (referencing_col)` for an action that nulls its columns.

    The column list names this key's own referencing column (`child_columns[0]`), never the parent's `id`,
    which keeps `org_id` out of the nulled set. Requires Postgres 15+.
    """
    action = on_delete.strip().upper()
    if action not in _NULLS_ITS_COLUMNS:
        raise ValueError(
            f"unrecognized ON DELETE action {on_delete!r} for a composite org key — expected one of "
            f"{sorted(_NULLS_ITS_COLUMNS)}. An action nobody has classified must not be rendered: "
            f"whether it nulls its own columns is what decides if `{ORG_COLUMN}` survives a parent "
            "delete (app/tenancy/keys.py::_NULLS_ITS_COLUMNS)"
        )
    if _NULLS_ITS_COLUMNS[action]:
        return f"ON DELETE {action} ({referencing_column})"
    return f"ON DELETE {action}"


def composite_keys() -> tuple[CompositeKey, ...]:
    """Every composite org key this schema owes, derived from the manifest. Sorted for stable output.

    Deferred manifest import: a module-scope `app.models` import breaks the bare-host `alembic heads`
    walk. A non-`Stored` parent raises a named error.
    """
    from app.tenancy.manifest import TENANCY, Derived, Stored

    keys: list[CompositeKey] = []
    for cls, verdict in TENANCY.items():
        if not isinstance(verdict, Derived):
            continue
        # `Derived.pairs` is the single home of the pairing rule and raises on a length mismatch.
        try:
            pairs = verdict.pairs
        except ValueError as exc:
            raise ValueError(f"{cls.__name__}: {exc}") from exc
        for column, parent in pairs:
            if not isinstance(TENANCY.get(parent), Stored):
                raise ValueError(
                    f"{cls.__name__}.{column} derives through {parent.__name__}, which the census does "
                    "not call `Stored` — a composite key needs the parent's UNIQUE(id, org_id), which "
                    "only `Stored` carries. The fix is to promote that parent, never to relax this "
                    "(app/tenancy/manifest.py, the one-hop invariant)"
                )
            keys.append(
                CompositeKey(
                    table=cls.__tablename__,
                    parent_table=parent.__tablename__,
                    child_columns=(column, ORG_COLUMN),
                    parent_columns=(PARENT_ID_COLUMN, ORG_COLUMN),
                    on_delete=verdict.on_delete.strip().upper(),
                    name=key_name(cls.__tablename__, column),
                )
            )

    _refuse_unrenderable(keys)
    return tuple(sorted(keys, key=lambda key: (key.table, key.child_columns[0])))


def same_org_keys() -> tuple[CompositeKey, ...]:
    """Every same-org key this schema owes for a non-derivation tenant-owned reference, from the census.

    Input is `Stored.same_org` and `Derived.same_org`; output is the same `CompositeKey` shape. Deferred imports for
    `composite_keys()`'s reason; `org_unique` owns the target name.

    Refuses, before Postgres can fail opaquely:
      * a parent not declaring `UNIQUE(id, org_id)` (a `Derived` parent may declare one);
      * a column that is also a derivation path (would duplicate a constraint name);
      * a parent the census does not call tenant-owned.

    `null_is_legitimate` is not read: it changes no statement; `test_tenancy_composite_keys.py` grades it.
    """
    from app.models.base import org_unique
    from app.tenancy.manifest import TENANCY, Derived, Stored

    tenant_owned = {cls for cls, verdict in TENANCY.items() if isinstance(verdict, (Stored, Derived))}
    keys: list[CompositeKey] = []
    for cls, verdict in TENANCY.items():
        if not isinstance(verdict, (Stored, Derived)) or not verdict.same_org:
            continue
        # Wrapped as in `composite_keys()` so a malformed entry's error names its table. Skipped for entries
        # that declare nothing; a Stored entry has no derivation path to collide with.
        try:
            derivation_columns = (
                {column for column, _parent in verdict.pairs} if isinstance(verdict, Derived) else set()
            )
        except ValueError as exc:
            raise ValueError(f"{cls.__name__}: {exc}") from exc
        for entry in verdict.same_org:
            if entry.column in derivation_columns:
                raise ValueError(
                    f"{cls.__name__}.{entry.column} is declared in `same_org` AND is one of this "
                    "entry's derivation paths, so both generators would render the constraint name "
                    f"{key_name(cls.__tablename__, entry.column)!r}. A column is one or the other: "
                    "drop it from `same_org` (app/tenancy/manifest.py::SameOrg)"
                )
            if entry.parent not in tenant_owned:
                raise ValueError(
                    f"{cls.__name__}.{entry.column} declares a same-org key against "
                    f"{entry.parent.__name__}, which the census does not call tenant-owned — it carries "
                    f"no `{ORG_COLUMN}` for this key to pair with (app/tenancy/manifest.py)"
                )
            # Checked by columns and order, not name: a `(org_id, id)` unique is a different object that
            # `REFERENCES parent(id, org_id)` cannot target (`contraction.py::unique_steps`).
            target = org_unique(entry.parent.__tablename__).name
            declared = {
                constraint.name: tuple(column.name for column in constraint.columns)
                for constraint in entry.parent.__table__.constraints
            }
            wanted = (PARENT_ID_COLUMN, ORG_COLUMN)
            if declared.get(target) != wanted:
                raise ValueError(
                    f"{cls.__name__}.{entry.column} declares a same-org key against "
                    f"{entry.parent.__name__}, which declares no {target!r} over {wanted} — it has "
                    f"{declared.get(target)!r}. SQL requires a unique constraint over EXACTLY the "
                    "referenced columns, in that order, before a composite key may point at them, and "
                    "the parent's own primary key does not satisfy that. Add "
                    "`org_unique(__tablename__)` to that class AND a revision that builds it "
                    "(app/models/base.py::org_unique; app/tenancy/contraction.py::apply_unique_constraint is "
                    "the form)"
                )
            keys.append(
                CompositeKey(
                    table=cls.__tablename__,
                    parent_table=entry.parent.__tablename__,
                    child_columns=(entry.column, ORG_COLUMN),
                    parent_columns=(PARENT_ID_COLUMN, ORG_COLUMN),
                    on_delete=entry.on_delete.strip().upper(),
                    name=key_name(cls.__tablename__, entry.column),
                )
            )

    _refuse_unrenderable(keys)
    return tuple(sorted(keys, key=lambda key: (key.table, key.child_columns[0])))


def all_org_keys() -> tuple[CompositeKey, ...]:
    """Both generators' keys, in one sorted tuple — the one place that union is spelled.

    For callers that need every constraint: `migrations/env.py::include_object` (else autogenerate drops
    them) and the unkeyed-reference register. The sort is total because the sets are disjoint on (table, column). The
    union is re-validated because a cross-set name collision would silently halve `include_object`'s list.
    """
    keys = list(composite_keys() + same_org_keys())
    _refuse_unrenderable(keys)
    return tuple(sorted(keys, key=lambda key: (key.table, key.child_columns[0])))


def _refuse_unrenderable(keys: list[CompositeKey]) -> None:
    """Fail loudly on a name or an identifier this module cannot render, on a duplicate name, and on an
    ON DELETE action nobody has classified.

    The action is checked at census read, not first in `add_sql`, so an unclassified action fails before
    any statement rather than midway through the revision's autocommit block. Classification stays in
    `_NULLS_ITS_COLUMNS`.
    """
    bad = sorted(
        {
            identifier
            for key in keys
            for identifier in (
                key.table, key.parent_table, key.name, *key.child_columns, *key.parent_columns
            )
            if not _IDENTIFIER_RE.fullmatch(identifier)
        }
    )
    if bad:
        raise ValueError(
            f"these identifiers need quoting and the rendered DDL interpolates bare: {bad}"
        )
    long = sorted(key.name for key in keys if len(key.name) > _MAX_IDENTIFIER)
    if long:
        raise ValueError(
            f"these constraint names exceed Postgres's {_MAX_IDENTIFIER}-character identifier limit and "
            f"would be silently truncated by the server: {long}"
        )
    for key in keys:
        # Rendered for its exception only: `_action_sql` is the classification.
        _action_sql(key.on_delete, key.child_columns[0])
    seen: dict[str, CompositeKey] = {}
    for key in keys:
        if key.name in seen:
            raise ValueError(
                f"two keys generate the constraint name {key.name!r}: "
                f"{seen[key.name]} and {key}. A name collision would make one of them invisible to the "
                "revision's own idempotency check"
            )
        seen[key.name] = key


def add_sql(key: CompositeKey) -> str:
    """The `NOT VALID` add — catalogue-only, so it never scans the table.

    This is the locking budget: a validating add holds its lock across the scan (see `app/db.py` on ALB
    timeouts). `validate_sql` scans under a weaker lock.
    """
    child = ", ".join(key.child_columns)
    parent = ", ".join(key.parent_columns)
    return (
        f"ALTER TABLE {key.table} ADD CONSTRAINT {key.name} "
        f"FOREIGN KEY ({child}) REFERENCES {key.parent_table} ({parent}) "
        f"{_action_sql(key.on_delete, key.child_columns[0])} NOT VALID"
    )


def validate_sql(key: CompositeKey) -> str:
    """The separate validation step. Takes SHARE UPDATE EXCLUSIVE on the child and ROW SHARE on the parent,
    blocking neither readers nor writers; bounded by `statement_timeout`. Idempotent.
    """
    return f"ALTER TABLE {key.table} VALIDATE CONSTRAINT {key.name}"
