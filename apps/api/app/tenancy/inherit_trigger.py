"""A `Derived` row's `org_id` is filled from its parent by the table, not by whoever wrote the row.

`SET NOT NULL` survives only if nothing can write a NULL, including the previous release still serving
a rolling deploy; a trigger is the one guard both releases are behind. Permanent: revision 0181's keys
refuse a disagreeing org, and this closes the fill side for every statement shape (`node_org.py` shape).

One generic function, one trigger per table, (parent, FK column) pairs in `TG_ARGV` from the manifest
`keys.py` uses; the first pair that resolves wins, and a disagreement is the key's refusal, never this.
`WHEN (NEW.org_id IS NULL)` is the fast path; `BEFORE INSERT` only, never overwriting a named org. Not
`ENABLE ALWAYS`: replicated rows already carry their org.
"""

from __future__ import annotations

import re

# Every identifier Python interpolates here goes through `_ident` (the run-time ones are `%I`-quoted
# inside the function). `\A`/`\Z` plus `fullmatch`, because `$` matches before a trailing newline.
_IDENT_RE = re.compile(r"\A[a-z_][a-z0-9_]{0,62}\Z")

# Postgres' `NAMEDATALEN - 1`. A longer identifier is silently truncated, which would surface as a
# confusing schema-test mismatch rather than a refusal.
_MAX_IDENTIFIER = 63


def _ident(value: str, what: str) -> str:
    """`value`, if it is a plain lowercase identifier — otherwise a refusal naming what was wrong.

    Refuses rather than quotes: a bad name here is a revision mistake. Single home for both DDL modules
    (`contraction.py` imports it). The type is checked first so a non-string gets this refusal rather
    than an anonymous `TypeError`."""
    if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
        raise ValueError(
            f"{what} must be a bare lowercase SQL identifier for this module to interpolate it; "
            f"got {value!r}"
        )
    return value


def _bounded(name: str, what: str) -> str:
    """`name`, if Postgres will store it whole — otherwise a refusal. See `_MAX_IDENTIFIER`.

    Separate from `_ident`: this grades a derived name, and `trg_{table}_org_id_inherit` adds 19
    characters to an input `_ident` admits."""
    if len(name) > _MAX_IDENTIFIER:
        raise ValueError(
            f"{what} would be {len(name)} characters, past Postgres' {_MAX_IDENTIFIER}-character limit, "
            f"and would be SILENTLY TRUNCATED rather than refused; got {name!r}"
        )
    return name


# Named once so the DDL, `downgrade()` and catalogue-reading tests agree on one string.
ORG_INHERIT_FUNCTION = "public.inherit_org_id_from_parent"

# Schema-pinned and load-bearing: the body resolves a parent table by name, and without this a session
# could shadow it with a `pg_temp` table.
_FUNCTION_SEARCH_PATH = "SET search_path = pg_catalog, public"

# `format()` with `%I`: the identifiers arrive at run time via `TG_ARGV`, so quoting must happen inside
# the database. `TG_ARGV` itself is set only by DDL.
ORG_INHERIT_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {ORG_INHERIT_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql {_FUNCTION_SEARCH_PATH} AS $$
DECLARE
    found_org uuid;
    i         int := 0;
BEGIN
    -- The pairs arrive as (parent table, foreign-key column), flattened, in the manifest's own order.
    -- Tried in order, first non-NULL wins; see this module's docstring for why that is right for both
    -- the XOR case and the must-agree case, and why this fills rather than adjudicates.
    --
    -- ONE STATEMENT PER PAIR, WITH THE FOREIGN KEY READ INSIDE IT RATHER THAN INTO A VARIABLE, and that
    -- is a correctness requirement rather than a saving. These keys are NOT ONE TYPE: `tasks.id` and
    -- `projects.id` are `integer` while every other parent's is `uuid`, so any local variable holding
    -- the value would have to name one of them and would fail on the other half of the census. Reading
    -- `($1).%I` in the SELECT keeps the comparison in the parent's own type, which is also what lets it
    -- use the primary-key index. A NULL foreign key simply matches no row and falls through to the next
    -- pair, so it needs no test of its own.
    WHILE i < TG_NARGS LOOP
        EXECUTE format('SELECT p.org_id FROM %I p WHERE p.id = ($1).%I', TG_ARGV[i], TG_ARGV[i + 1])
           INTO found_org USING NEW;
        IF found_org IS NOT NULL THEN
            NEW.org_id := found_org;
            RETURN NEW;
        END IF;
        i := i + 2;
    END LOOP;
    -- NOTHING RESOLVED, AND THE ROW IS RETURNED UNCHANGED RATHER THAN REFUSED. The NOT NULL constraint
    -- is one statement away and will refuse it in a moment with the error an operator can act on; a
    -- RAISE here would replace that with a message about a trigger, and would additionally make this
    -- guard the thing that rejects a row on a table whose column is not NOT NULL yet — which is exactly
    -- the window each revision installs it in. Unreachable once the band lands: every parent in the
    -- census is `Stored`, every `Stored` table's `org_id` is NOT NULL, and every derivation column but
    -- `role_bindings`' XOR pair is itself NOT NULL.
    RETURN NEW;
END;
$$
"""


def org_inherit_trigger_name(table: str) -> str:
    """This table's trigger. One per table because a trigger is attached to a table; the FUNCTION is the
    part that is shared."""
    return _bounded(f"trg_{_ident(table, 'table')}_org_id_inherit", "the org-inherit trigger name")


def org_inherit_trigger_sql(table: str, pairs: tuple[tuple[str, str], ...]) -> str:
    """`CREATE TRIGGER` for one table, carrying its derivation as trigger arguments.

    `pairs` is (parent table, local FK column) in the manifest's order (`Derived.pairs` is the home),
    passed through so this module forms no second opinion. Identifiers are fixed census literals (see
    `contraction.py::not_null_steps`); the arguments are string literals, re-quoted by `format('%I')`."""
    args = ", ".join(
        f"'{_ident(parent, 'parent table')}', '{_ident(column, 'foreign-key column')}'"
        for parent, column in pairs
    )
    return (
        f"CREATE TRIGGER {org_inherit_trigger_name(table)} "
        f"BEFORE INSERT ON {_ident(table, 'table')} FOR EACH ROW WHEN (NEW.org_id IS NULL) "
        f"EXECUTE FUNCTION {ORG_INHERIT_FUNCTION}({args})"
    )


def org_inherit_function_drop_sql() -> str:
    """Dropping the shared function is not a per-table downgrade and no revision should call it.

    Left uncalled deliberately: every trigger depends on it, so dropping it would fail or uninstall the
    guard for tables still contracted. `CREATE OR REPLACE` in every installer makes that safe."""
    return f"DROP FUNCTION IF EXISTS {ORG_INHERIT_FUNCTION}()"
