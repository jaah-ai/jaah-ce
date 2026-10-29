"""Same-org keys on every project and credential reference: `(col, org_id) REFERENCES parent(id, org_id)`

`app/tenancy/manifest.py` now declares a `SameOrg` entry on each reference into `projects` and `credentials` (Stored
entries included), so an org-A row can no longer name org B's project or credential. The keys are frozen here
(`_KEYS`); `app/tenancy/keys.py::add_sql`/`validate_sql` render them, as the baseline's do.

Rows that already cross orgs are repaired first, each step logging `table.column: n rows` only:
  * hidden `adhoc` system tasks filed into the Alarm project under the org the alarm was about (cron and
    node-liveness alarms, before alarms became operator-only) are DELETED;
  * a nullable reference is set NULL;
  * a node group on another org's credential MOVES, with everything derived from it, to that credential's org
    (`app/tenancy/rehome.py::rehome_row`), first dropping its project entitlements and NodeLinks outside it;
  * any other row whose NOT NULL reference crosses (an API-key account's key included) ABORTS the revision, naming
    the reference and row ids: in a forged reference the row is the forger's, so moving it to the org it names
    would hand it to the victim, and a row derived from a parent cannot move alone.

Additive for the overlap: no column changes. The previous release's alarm filing, still tenant-stamped, is refused
by the new key for the overlap's seconds; alarm delivery is best-effort and logs it.

Revision ID: 202610290030
Revises: 202610282358
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "202610290030"
down_revision: Union[str, None] = "202610282358"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")

#: (table, column, parent, ON DELETE, nullable) — the census rows this revision installs.
_KEYS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("accounts", "credential_id", "credentials", "RESTRICT", True),
    ("admin_users", "git_credential_id", "credentials", "SET NULL", True),
    ("credentials", "home_project_id", "projects", "SET NULL", True),
    ("gha_runner_pools", "credential_id", "credentials", "RESTRICT", False),
    ("gha_runner_pools", "project_id", "projects", "RESTRICT", False),
    ("gov_entities", "project_id", "projects", "CASCADE", True),
    ("gov_exceptions", "project_id", "projects", "CASCADE", True),
    ("gov_remediations", "project_id", "projects", "CASCADE", False),
    ("gov_twin_cells", "project_id", "projects", "CASCADE", False),
    ("gov_twin_repos", "project_id", "projects", "CASCADE", False),
    ("merge_lane_certificates", "project_id", "projects", "CASCADE", False),
    ("merge_lane_flakes", "project_id", "projects", "CASCADE", False),
    ("merge_lane_rows", "project_id", "projects", "CASCADE", False),
    ("microvms", "project_id", "projects", "CASCADE", False),
    ("node_group_projects", "project_id", "projects", "CASCADE", False),
    ("node_groups", "credential_id", "credentials", "RESTRICT", False),
    ("node_projects", "project_id", "projects", "CASCADE", False),
    ("nodevault_operation_items", "credential_id", "credentials", "CASCADE", False),
    ("org_sso_connections", "oidc_credential_id", "credentials", "RESTRICT", True),
    ("project_env_vars", "credential_id", "credentials", "SET NULL", True),
    ("project_memories", "project_id", "projects", "CASCADE", False),
    ("projects", "jira_credential_id", "credentials", "SET NULL", True),
    ("sessions", "project_id", "projects", "SET NULL", True),
    ("task_triggers", "preflight_credential_id", "credentials", "SET NULL", True),
    ("task_triggers", "project_id", "projects", "CASCADE", False),
    ("tasks", "project_id", "projects", "NO ACTION", False),
)


def _crossing(table: str, column: str, parent: str, select: str) -> str:
    """`SELECT <select>` over the rows of `table` whose `column` names a `parent` row of another org."""
    return (
        f"SELECT {select} FROM {table} c JOIN {parent} p ON p.id = c.{column} "
        f"WHERE c.org_id IS DISTINCT FROM p.org_id"
    )


#: The alarms filed under the org they were about (`app/cron_alarms.py::SKIP_KEY_PREFIX`, `app/node_liveness.py`'s
#: `fleet-node-liveness-*` keys), frozen: the only writers that ever stamped a task off its project's org.
_ALARM_KEY_PATTERNS = ("cron-skip:%", "fleet-node-liveness-%")


# additive-check: exempt — the only DROP is the revision's own temporary index, created a few statements earlier
#: Scaffolding for the alarm DELETE only, dropped before the revision ends (no schema change). Each deleted body
#: version fires `restored_from_id`'s `ON DELETE SET NULL`, a per-row lookup on an unindexed column: on dev, 55k
#: doomed versions in a 138k-row table seq-scanned for longer than the deploy's health-check window.
_RESTORED_FROM_INDEX = "tmp_202610290030_task_body_versions_restored_from_id"


def _repair(conn) -> None:
    from sqlalchemy.orm import Session

    from app.tenancy.rehome import RehomeBlocked, rehome_row

    conn.execute(text(f"CREATE INDEX {_RESTORED_FROM_INDEX} ON task_body_versions (restored_from_id)"))
    # The alarms: one hidden card per condition, refreshed in place; nothing but the alarm sweeps reads them.
    gone = conn.execute(text(
        "DELETE FROM tasks t USING projects p WHERE p.id = t.project_id AND t.org_id <> p.org_id "
        "AND t.hidden AND t.persona = 'adhoc' AND t.dedup_key LIKE ANY(:patterns) RETURNING t.state"
    ), {"patterns": list(_ALARM_KEY_PATTERNS)}).scalars().all()
    if gone:
        by_state = {state: gone.count(state) for state in sorted(set(gone))}
        log.info("tasks.project_id: deleted %d cross-org alarm tasks %s", len(gone), by_state)
    conn.execute(text(f"DROP INDEX {_RESTORED_FROM_INDEX}"))

    blocked: list[str] = []
    # A node group is the operator's to move (its launch credential decides whose capacity it is); first its links
    # to the group's old org — project entitlements and NodeLinks — which could never hold after the move.
    groups = conn.execute(text(_crossing("node_groups", "credential_id", "credentials", "c.id, p.org_id"))).all()
    with Session(bind=conn) as session:
        for group_id, org in groups:
            for link, group_column, parent, column in (
                ("node_group_projects", "group_id", "projects", "project_id"),
                ("node_group_nodelinks", "node_group_id", "nodelinks", "nodelink_id"),
            ):
                conn.execute(text(
                    f"DELETE FROM {link} g USING {parent} p WHERE p.id = g.{column} "
                    f"AND g.{group_column} = :id AND p.org_id <> :org"
                ), {"id": group_id, "org": org})
            try:
                rehome_row(session, "node_groups", group_id, org)
            except RehomeBlocked as exc:
                blocked.append(f"node_groups.credential_id: {group_id} ({exc})")
            else:
                log.info("node_groups.credential_id: moved group %s to org %s", group_id, org)
        session.flush()

    for table, column, parent, _on_delete, nullable in _KEYS:
        if table == "node_groups":
            continue
        if nullable and table != "accounts":
            nulled = conn.execute(text(
                f"UPDATE {table} c SET {column} = NULL FROM {parent} p "
                f"WHERE p.id = c.{column} AND c.org_id IS DISTINCT FROM p.org_id"
            )).rowcount
            if nulled:
                log.info("%s.%s: nulled %d cross-org references", table, column, nulled)
            continue
        if table == "accounts":
            # An API-key account cannot lose its key (`ck_accounts_api_key_has_credential`); any other is nulled.
            nulled = conn.execute(text(
                "UPDATE accounts c SET credential_id = NULL FROM credentials p WHERE p.id = c.credential_id "
                "AND c.org_id IS DISTINCT FROM p.org_id AND c.auth_kind <> 'api_key'"
            )).rowcount
            if nulled:
                log.info("accounts.credential_id: nulled %d cross-org references", nulled)
        # Never moved: in a forged reference the row is the forger's and the project or credential the victim's,
        # so moving it would hand it to the victim's org. A human decides.
        ids = conn.execute(text(_crossing(table, column, parent, "c.id"))).scalars().all()
        if ids:
            blocked.append(f"{table}.{column}: {', '.join(str(i) for i in ids)}")
    if blocked:
        raise RuntimeError(
            "rows name a project or credential of another org and cannot be repaired here (moving the row would "
            f"hand it to the org it names): {'; '.join(blocked)}. Inspect and remove or fix them, then rerun "
            "(revision 202610290030)."
        )


def _keys():
    from app.tenancy.keys import ORG_COLUMN, PARENT_ID_COLUMN, CompositeKey, key_name

    return [
        CompositeKey(
            table=table,
            parent_table=parent,
            child_columns=(column, ORG_COLUMN),
            parent_columns=(PARENT_ID_COLUMN, ORG_COLUMN),
            on_delete=on_delete,
            name=key_name(table, column),
        )
        for table, column, parent, on_delete, _nullable in _KEYS
    ]


def upgrade() -> None:
    from app.tenancy.keys import add_sql, validate_sql

    _repair(op.get_bind())
    for key in _keys():
        op.execute(add_sql(key))
        op.execute(validate_sql(key))


def downgrade() -> None:
    for key in reversed(_keys()):
        op.drop_constraint(key.name, key.table, type_="foreignkey")
