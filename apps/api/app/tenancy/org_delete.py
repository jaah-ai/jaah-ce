"""An org may be deleted only when nothing carries its id — the trigger that makes it so, and nothing else.

Installs a `BEFORE DELETE` trigger on `orgs` refusing the delete if any table still holds a row with this
`org_id` — any org, the operator plane included. The cascade that empties those tables first is
`app/tenancy/org_lifecycle.py`'s; this is the floor that proves it missed nothing, since the ORM seams are blind to Core/`text()`/psql and `orgs`
has no inbound FK (`app/models/base.py::OrgScoped`). `TRUNCATE` is not covered (no statement trigger).

The scan asks `pg_catalog` for every `public` table with an `org_id` column, so a new table is covered
with no revision; `tenancy_exclusions` (`bound_org`) is out of scope by spelling. `enabled` is not
checked — that is operator policy in `org_lifecycle.py`. Stdlib only: an alembic revision imports it.
"""

from __future__ import annotations

ORGS_TABLE = "orgs"

#: The tenancy column's one spelling (`app/models/base.py::OrgScoped`); the catalogue scan matches this literal.
ORG_COLUMN = "org_id"

# Shared by the installing DDL and the test asserting it is attached.
ORG_DELETE_GUARD_TRIGGER_NAME = "trg_orgs_refuse_delete_with_dependents"
ORG_DELETE_GUARD_FUNCTION = "public.refuse_org_delete_with_dependents"

# Pinned because plpgsql resolves relations against the invoking session's `search_path`, where `pg_temp`
# precedes `public`. This body reads relations, so the pin is load-bearing; the
# dynamic probe is also qualified `public.%I`.
_FUNCTION_SEARCH_PATH = "SET search_path = pg_catalog, public"

# `restrict_violation` (23001) for the refusal. FK RESTRICT raises 23001 too, so the caller
# (`app/tenancy/org_lifecycle.py::ORG_DELETE_REFUSED_SQLSTATES`) discriminates on `constraint_name`.
ORG_DELETE_GUARD_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {ORG_DELETE_GUARD_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql {_FUNCTION_SEARCH_PATH} AS $$
DECLARE
    dependent_table text;
    still_there boolean;
BEGIN
    -- EVERY base table in `public` carrying a column named `org_id`, asked of the catalogue rather than
    -- listed here — this module's docstring carries why. `attisdropped` excludes a dropped column's
    -- tombstone row; `relkind IN ('r','p')` takes ordinary and partitioned tables and leaves views,
    -- matviews and foreign tables alone (a view's rows are its base table's, counted there).
    FOR dependent_table IN
        SELECT c.relname
          FROM pg_catalog.pg_attribute a
          JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE a.attname = '{ORG_COLUMN}'
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND c.relkind IN ('r', 'p')
           AND n.nspname = 'public'
         ORDER BY c.relname
    LOOP
        -- EXISTS AND NOT count(*): the message names the table, never how many rows it held, so a
        -- count would scan a whole table to render a number nobody reads. `USING` binds the id as a
        -- parameter — `format('%I')` quotes the identifier, and an interpolated value would not be.
        --
        -- THE COMPARISON ASSUMES `org_id` IS A uuid EVERYWHERE, and that is a real assumption rather
        -- than an obvious one: a table whose `org_id` were some other type would make `org_id = $1`
        -- raise `42883` (no operator) and break EVERY org delete, not just its own. Safe because the
        -- column is declared exactly once, as `PG_UUID`, on the `OrgScoped` mixin, and
        -- `apps/api/test_tenancy_org_column.py` holds the census and the mixin equal — so a table can
        -- only acquire the column by acquiring the type with it.
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM public.%I WHERE {ORG_COLUMN} = $1)', dependent_table)
           INTO still_there USING OLD.id;
        IF still_there THEN
            RAISE EXCEPTION
                'org % (%) still owns rows in %: an org is deleted only once nothing carries its id',
                OLD.slug, OLD.id, dependent_table
              USING ERRCODE = 'restrict_violation';
        END IF;
    END LOOP;

    -- `OLD` AND NOT `NULL`. A BEFORE-DELETE trigger returning NULL CANCELS the delete and reports
    -- success, which is the one outcome worse than either answer here: the operator is told the tenant
    -- is gone and the row is still there.
    RETURN OLD;
END;
$$
"""

# Fires for every deleted row: one `EXISTS` probe (mostly seq scans) per org-scoped table per deleted
# org, acceptable for a rare operator action and unskippable by statement shape.
ORG_DELETE_GUARD_TRIGGER_SQL = (
    f"CREATE TRIGGER {ORG_DELETE_GUARD_TRIGGER_NAME} "
    f"BEFORE DELETE ON {ORGS_TABLE} FOR EACH ROW "
    f"EXECUTE FUNCTION {ORG_DELETE_GUARD_FUNCTION}()"
)

# `ENABLE ALWAYS`: a plain trigger is skipped under `session_replication_role = 'replica'` and in a
# logical-replication apply worker. A separate statement since `CREATE TRIGGER` has no syntax for it.
ORG_DELETE_GUARD_TRIGGER_ALWAYS_SQL = (
    f"ALTER TABLE {ORGS_TABLE} ENABLE ALWAYS TRIGGER {ORG_DELETE_GUARD_TRIGGER_NAME}"
)


def org_delete_guard_function_drop_sql() -> str:
    """The `downgrade()` half, owned by the one revision that created the function."""
    return f"DROP FUNCTION IF EXISTS {ORG_DELETE_GUARD_FUNCTION}()"
