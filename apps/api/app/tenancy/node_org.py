"""`nodes.org_id` changes only through the operator's org transfer — the trigger that makes it so, and nothing else.

A database trigger, not a Python check: the ORM seams (`seam.py`, `write_seam.py`) are blind to Core
`__table__.update()`; the trigger holds for ORM, Core, `text()` and psql. A node changes org only through the
operator transfer: the engine names the moving node ids in the transaction-local setting
`NODE_ORG_TRANSFER_SETTING` around its one `rehome_set` (`transfer.py::_rehome`, the only writer — a census
test holds it there) and the trigger admits a change only for a node so named. Every other write is refused.
NULL → org (adoption) is admitted too — unreachable now the column is NOT NULL, but keep it
(a test pins it).

The DDL lives here, not in the revision, so a drift test reads the same constants the revision imports.
"""

from __future__ import annotations

NODES_TABLE = "nodes"

# Named here so the attachment test and the DDL read the same string.
NODE_ORG_IMMUTABLE_TRIGGER_NAME = "trg_nodes_org_id_immutable"
NODE_ORG_IMMUTABLE_FUNCTION = "public.refuse_node_org_reassignment"

# Schema-pinned: plpgsql resolves relations against the invoking session's `search_path` (`pg_temp`
# first). The body reads no relation today; the `SET` keeps that safe for the next edit.
_FUNCTION_SEARCH_PATH = "SET search_path = pg_catalog, public"

#: The transaction-local setting naming, comma-separated, the node ids the operator transfer is moving.
NODE_ORG_TRANSFER_SETTING = "jaah.node_org_transfer"

# `restrict_violation` (23001), an integrity-constraint class code a caller can render instead of a
# 500. The message names the remedy for an operator in psql.
NODE_ORG_IMMUTABLE_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {NODE_ORG_IMMUTABLE_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql {_FUNCTION_SEARCH_PATH} AS $$
BEGIN
    -- The adoption arm: a row that has never had an org may be given one, exactly once. See this
    -- module's docstring for why the stricter reading buys no boundary and costs the contraction.
    -- The transfer arm: the operator's org transfer names the node it moves. `IS NOT TRUE`, never `NOT`: an
    -- unset setting reads NULL, and a NULL here must refuse.
    IF OLD.org_id IS NOT NULL AND NEW.org_id IS DISTINCT FROM OLD.org_id
       AND (OLD.id::text = ANY(string_to_array(current_setting('{NODE_ORG_TRANSFER_SETTING}', true), ','))) IS NOT TRUE
    THEN
        RAISE EXCEPTION
            'nodes.org_id is immutable outside the operator org transfer: node % is in org %, and only that transfer releases the old org''s slots, grants and credential directories on it',
            OLD.id, OLD.org_id
          USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

# `UPDATE OF org_id`: fires only when the SET list names `org_id`, so heartbeat UPDATEs pay nothing;
# `IS DISTINCT FROM` admits a same-value write. Known edge: a future BEFORE-UPDATE trigger on this table
# that assigns `NEW.org_id` without the statement naming it would bypass this (DDL-only, unreachable
# from application code).
NODE_ORG_IMMUTABLE_TRIGGER_SQL = (
    f"CREATE TRIGGER {NODE_ORG_IMMUTABLE_TRIGGER_NAME} "
    f"BEFORE UPDATE OF org_id ON {NODES_TABLE} FOR EACH ROW "
    f"EXECUTE FUNCTION {NODE_ORG_IMMUTABLE_FUNCTION}()"
)

# `ENABLE ALWAYS` so the trigger also fires under `session_replication_role = 'replica'` and in a
# logical-replication apply worker. A separate statement because `CREATE TRIGGER` has no clause for it.
NODE_ORG_IMMUTABLE_TRIGGER_ALWAYS_SQL = (
    f"ALTER TABLE {NODES_TABLE} ENABLE ALWAYS TRIGGER {NODE_ORG_IMMUTABLE_TRIGGER_NAME}"
)


def node_org_immutable_function_drop_sql() -> str:
    """The `downgrade()` half. This function has exactly one trigger and one installer, unlike the
    shared `entity_shape.set_updated_at()`."""
    return f"DROP FUNCTION IF EXISTS {NODE_ORG_IMMUTABLE_FUNCTION}()"
