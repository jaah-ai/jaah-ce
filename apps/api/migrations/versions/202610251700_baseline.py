"""baseline — the whole schema in one revision; 0131..202610251700 collapsed

Revision ID: 202610251700
Revises: nothing — this IS the base
Create Date: 2026-10-26

Collapsing to a revision EVERY CONTROL PLANE IS STAMPED AT is what makes this invisible: main was at
202610251700 and dev past it when it was cut (each one's last green Jaah Deploy, of master d57e8a9cb and develop
91a08cd24, ran `migrate.py` before serving), so neither re-runs this file and nothing is re-stamped. It
supersedes the earlier collapses on the same argument (whose bases were 0131, 0242, 0269, 0279, 0349, 0368,
0401 and 202610130700). THE COST: no upgrade path below 202610251700 (a `./dc` stack stamped lower fails loudly
at `Can't locate revision` and needs `./dc reset` — `migrate.py::deploy_schema_note` names that case), and
`downgrade()` drops the whole schema, one revision before the base being an empty database.

HOW IT WAS CUT, AND THE PROOF. Autogenerate rendered the tables and indexes below from a scratch database
migrated by the old chain (cycle ends deferred to `_DEFERRED_FKS`, serial defaults left to `autoincrement`,
the composite org keys left out); what reflection CANNOT see is authored after them: the `vector`
extension, the functions and their triggers, the `account_fold` view, the composite and same-org keys,
the names two renames left behind and the `cp_readonly` role. The trigger and key censuses were read off
that database's catalogue. 0369..0401, 0402..202610130700 and 202610130800..202610251700 were folded the same
way: a database built by the previous base and one built by the chain rendered, the table section re-rendered
whole from the chain's (autogenerate's own order, the cycle ends deferred, the composite org keys left out, the
serial defaults left to `autoincrement`); the censuses, functions and seeds were moved by the two catalogues' and
row sets' differences. The result `pg_dump --schema-only`s identical to the 202610251700-built database (save
the one line below), and its rows match modulo uuidv7 ids and timestamps, except for those recorded below.

WHAT A REBUILT DATABASE DOES NOT MATCH, each RECORDED rather than reproduced.

  * `refuse_org_delete_with_dependents()`'s body carries `app/tenancy/org_delete.py`'s comments here; the
    chain's 202610130300 wrote the same statements without them. The shared functions are imported from their
    owning module (`_install_functions_and_triggers`), so only the comments differ.

  * `plan_terms_snapshots` holds the operator org's one row at `revision = '202610171800'`: the revision that
    introduced the table snapshotted every org's terms as it stood, and a fresh database reproduces that row
    (`_seed`) so the plan engine reads the same inheritance baseline on every control plane.

  * `app_settings.app_title` — 'Jaah' here, the retired product name in a chain-built database (and on both control
    planes). 0292 moved the column default to 'Jaah' and kept existing rows, and the chain seeded its rows
    before that; a fresh database now takes the product's own default, by operator decision.

  * `activity_events` rows describing a backfill a fresh database never ran — kind `access_migration_note`
    ("org_without_owner", 0287's role-binding backfill) and kind `org_plan_overrides_migrated` (0374's
    products override) about the orgs a chain-built database held — are not seeded, and `activity_events_id_seq` starts
    unused.

  * ONE ORG, not two: a fresh database holds only the operator org. A chain-built database also
    holds the tenant every pre-boundary row was placed in (the same tenant on both control planes), with its own
    settings row, groups, port grid and tag catalogue; here those rows belong to the operator org, and the
    system actor sits in the operator org on both (revision 0406).

  * `permissions_id_seq.last_value` differs from main CP's, which burned values re-walking the catalogue in
    old collapsed revisions. The (id, key) pairs are 1..64 here; every reader joins on `key`.

  * TOMBSTONE columns: a dropped column leaves `........pg.dropped.N........` behind in a chain-built
    database, so `relnatts` and later `attnum`s differ on every table a collapsed revision dropped a column
    from. `pg_dump` cannot see it and no reader can: the live column ORDER is unchanged, and the one place
    this repo reads `ordinal_position` (`tests/test_tenancy_composite_keys_schema.py`) only orders by it.

WHAT IS DELIBERATELY NOT HERE. A backfill is a no-op on a database created from scratch — there are no rows
to rewrite — so none is carried (0282's fence fold, 0283's credential reseal, which is also what keeps this
file from needing an encryption key at migrate time, 0287/0288's grant mapping, 0297/0300's workstream
conversion, 0350's override seeding, 0357's trial hostings, 0358's spent slots, 0360/0361's person
backfill and factor dedupe, 0368's `activated_at`, 0369's tenant rename and rehome, 0373's knob credentials,
0375's plan-end resync, 0383's task source, 0384's trial adoption, and the rest). WHERE A LATER UPDATE
REACHED A SEEDED ROW IT IS WRITTEN INTO THE INSERT instead: the system actor's `new_task_project =
"focused"` (0141 — unreachable, the actor never logs in, and reproduced anyway: a divergence nothing can
observe is still one nobody can explain), the operator org's zero caps (0299, 0347), isolation exceptions
(0318) and Trial plan with its overrides (0374, 0384), its Everyone group (0287), the system actor's person
(0360: the membership's own id, its `first_name` the label 202610141600 moved there), the plan catalogue as the
five-plan band left it (202610181200..202610251700: `(default)`, Free, Business trial the Default plan, Business,
and the Trial, Team and Enterprise rows those revisions re-termed), the system crons 0371, 0411, 202610151700
and 202610240700 removed and later revisions added, every one with its automation parent (0417, 202610141700),
the Shared tier's disk cap (0419), the instance-type allowlist with its provider (0420, 202610211437) and the
operator org's provider pair (202610251500). The seeds import their single homes where one exists.
"""
# additive-check: exempt — only fires on a database created FROM SCRATCH, never on one a previous release
# is reading during a live overlap. The RENAMEs it flags (`_restore_the_names_the_rename_left_behind`,
# called from upgrade()) exist purely to make a freshly-created database's auto-derived names match an
# already-migrated one's. Both control planes are stamped at or past this revision (docstring), so it
# never runs again on a database an old release still reads — there is no "old code" to break against.
import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from migrations._edition import vector

revision: str = "202610251700"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Carried from 0021, with the uuids 0105 derived for them (`'01900000-0002-7000-8000-' ||
# lpad(to_hex(<old int id>), 12, '0')`). The ids are literals rather than left to the `uuidv7()` default
# because every already-migrated database holds exactly these three, and a rebuilt one that minted its own
# would differ from a migrated one in a column `user_group_memberships` and `object_grants` reference.
# superadmin deliberately gets no permission rows: a seeded edge is an unreviewed live grant (`_seed`).
_GROUPS = [
    (
        "01900000-0002-7000-8000-000000000001",
        "superadmin",
        "Full bypass — supersedes all group/permission checks (is_superadmin).",
    ),
    (
        "01900000-0002-7000-8000-000000000002",
        "developer",
        "Engineering access to worktrees, runners, trackers, resources.",
    ),
    (
        "01900000-0002-7000-8000-000000000003",
        "product_manager",
        "Product access to overview, trackers, compliance, trends, command log.",
    ),
]


# Carried verbatim from 0123, which owned it as a file-local constant — there is no module in `app/` that
# single-homes the tag catalogue, so this IS its home now. Shape: (group, description, multi_select,
# position, ((tag code, tag name, position), ...)). These ids are NOT pinned the way `_GROUPS`' are: the
# old chain minted them from `uuidv7()` at migration time too, so they already differ between any two
# databases and nothing outside the tag tables names one.
_TAG_SEED: tuple[tuple[str, str, bool, int, tuple[tuple[str, str, int], ...]], ...] = (
    (
        "By type",
        "What kind of work this task is.",
        False,
        1,
        (
            ("feature", "Features", 1),
            ("bug", "Bugs", 2),
            ("other", "Other", 3),
        ),
    ),
    (
        "By function",
        "Which part of the system this task touches.",
        True,
        2,
        (
            ("frontend", "Frontend", 1),
            ("backend", "Backend", 2),
            ("security", "Security", 3),
            ("infra", "Infrastructure", 4),
            ("documentation", "Documentation", 5),
        ),
    ),
)


class _Vector(sa.types.UserDefinedType):
    """`vector(768)` (`app/support_kb/embeddings.py::DIMENSIONS`) as literal DDL rather than imported from
    `pgvector.sqlalchemy`: the baseline must migrate after the app drops a third-party import."""

    cache_ok = True

    def get_col_spec(self, **kw) -> str:
        return "vector(768)"


# The tables carrying the standard entity shape whose shared BEFORE UPDATE trigger fires on every
# UPDATE; `_NARROWED_ENTITY_TRIGGERS` holds the nine that fire on a column list. The two together are
# every `set_updated_at` trigger a migrated database carries — read off its catalogue when this baseline
# was cut, as is every census below. The DDL is `app/entity_shape.py`'s; this is only where it attaches.
_ENTITY_TABLES = (
    "accounts",
    "admin_users",
    "allowed_instance_types",
    "automations",
    "billing_plan_products",
    "billing_plans",
    "built_in_playbooks",
    "cloud_node_defaults",
    "email_templates",
    "features",
    "gov_campaigns",
    "gov_exceptions",
    "gov_managed_packs",
    "gov_packs",
    "gov_rules",
    "groups",
    "identities",
    "model_prices",
    "node_orders",
    "node_rentals",
    "nodelinks",
    "nodes",
    "nodevault_keys",
    "org_bundled_products",
    "org_comps",
    "org_invitations",
    "org_plan_products",
    "org_shared_stores",
    "orgs",
    "playbooks",
    "port_grid_vars",
    "project_memories",
    "rental_prices",
    "sessions",
    "tag_groups",
    "tags",
    "task_triggers",
    "worktrees",
)

# The nine entity tables whose trigger fires only for an UPDATE naming one of these columns — Postgres's
# `BEFORE UPDATE OF a, b`. `credentials` (202610232200) leaves out the roster facts its testers and readers
# stamp (`last_used_at`, `last_test_*`, `last_error`), so a credential's use never reads as an edit;
# `api_keys` (202610231947), `gmail_connections` (202610240100) and `tenant_mail_addresses` (202610241100)
# list only the columns an operator edits, so a key's use, a mailbox's sync and an address's receipt do not. `crons` needs it because `last_tick_at` is stamped every tick and an
# unrestricted trigger made `updated_at` advance on a row no human had touched (live home:
# `app/crons.py::CRON_MACHINERY_COLUMNS`); `node_groups` carries the same shape for
# `last_reconciled_at`/`last_error`, and `gha_runner_pools` for the five columns its node reports every
# sixty seconds (`phase`, `last_error`, `observed`, `observed_at`, `runner_prefix`). `project_env_vars`
# leaves out `credential_id` alone, so the `ON DELETE SET NULL` cascade from a deleted credential does not
# read as an operator's edit; an operator re-pointing a variable stamps `updated_by_id`, which IS listed.
# `updated_at` stays in every list so the trigger remains authoritative over it. THE LISTS ARE NOT THE
# SAME SHAPE AND MUST NOT BE MADE SO: `node_groups`' list predates its `org_id`, and a "tidied" list here
# is drift against every already-migrated database.
_NARROWED_ENTITY_TRIGGERS = (
    (
        "agents",
        ("name", "description", "updated_at", "updated_by_id", "ref", "subpath", "status", "repo_access_user_id"),
    ),
    (
        "api_keys",
        ("name", "description", "grants_all", "permissions", "org_admin", "ip_allowlist", "revoked_at", "updated_at"),
    ),
    (
        "credentials",
        (
            "id", "name", "description", "created_at", "updated_at", "created_by_id", "updated_by_id",
            "org_id", "type", "owner_id", "system", "home_project_id", "config", "nodevault",
            "has_stored_secret", "expires_at",
        ),
    ),
    (
        "crons",
        (
            "id", "name", "description", "created_at", "updated_at", "created_by_id",
            "updated_by_id", "schedule_kind", "cron_expression", "interval_seconds",
            "activity_type", "activity_params", "enabled", "retention_mode", "min_gap_seconds",
            "timeout_seconds", "is_system", "org_id",
        ),
    ),
    (
        "gha_runner_pools",
        (
            "id", "name", "description", "created_at", "updated_at", "created_by_id",
            "updated_by_id", "org_id", "node_id", "project_id", "credential_id", "desired_count",
            "desired_state",
        ),
    ),
    (
        "gmail_connections",
        ("name", "description", "kind", "address", "sealed_secret", "can_receive", "updated_at"),
    ),
    (
        "node_groups",
        (
            "id", "name", "description", "created_at", "updated_at", "created_by_id",
            "updated_by_id", "credential_id", "enabled",
        ),
    ),
    (
        "project_env_vars",
        (
            "id", "name", "description", "created_at", "updated_at", "created_by_id",
            "updated_by_id", "org_id", "project_id", "user_id", "source", "literal",
            "credential_field",
        ),
    ),
    (
        "tenant_mail_addresses",
        ("name", "description", "address", "trigger_id", "updated_at"),
    ),
)

# FROZEN, NEVER READ LIVE. The org-inherit trigger set with each table's `(parent table, local fk column)`
# pairs, then the composite derivation keys and the same-org keys, as `app/tenancy/manifest.py` and
# `app/tenancy/keys.py` stood when this baseline was cut. They are living censuses — a table's later
# removal would change what a fresh database gets here, or stop it migrating at all — so a baseline spells
# them out (`tests/test_frozen_migration_imports.py` bans the live reads). `worktrees` is `Derived` and
# carries a key but no trigger: it was closed before the trigger band, and no migrated database has one.
_ORG_INHERIT = {
    'access_requests': (('admin_users', 'requester_id'),),
    'account_observations': (('nodes', 'node_id'),),
    'cloud_node_defaults': (('credentials', 'credential_id'),),
    'compliance_manifests': (('tasks', 'task_id'),),
    'console_api_key_leases': (('accounts', 'account_id'),),
    'credential_oauth_flows': (('nodes', 'node_id'),),
    'cron_runs': (('crons', 'cron_id'),),
    'features': (('projects', 'project_id'),),
    'fetch_stamps': (('nodes', 'node_id'),),
    'gha_runner_pools': (('nodes', 'node_id'),),
    'gov_campaign_items': (('gov_campaigns', 'campaign_id'),),
    'gov_edges': (('gov_entities', 'src_id'), ('gov_entities', 'dst_id')),
    'gov_facts': (('gov_entities', 'entity_id'),),
    'gov_pack_releases': (('gov_packs', 'pack_id'),),
    'gov_rules': (('gov_packs', 'pack_id'),),
    'gov_scan_targets': (('gov_scans', 'scan_id'),),
    'hosting_changes': (('node_rentals', 'rental_id'),),
    'legal_acceptances': (('admin_users', 'user_id'),),
    'live_cast_items': (('projects', 'project_id'),),
    'live_cast_projects': (('projects', 'project_id'),),
    'memory_capture_log': (('tasks', 'task_id'),),
    'memory_claims': (('projects', 'project_id'),),
    'memory_events': (('tasks', 'task_id'),),
    'memory_rejections': (('projects', 'project_id'),),
    'memory_sources': (('project_memories', 'unit_id'),),
    'memory_votes': (('project_memories', 'unit_id'),),
    'microvm_intents': (('nodes', 'node_id'),),
    'microvms': (('nodes', 'node_id'),),
    'node_disk_events': (('nodes', 'node_id'),),
    'node_group_capacity': (('node_groups', 'group_id'),),
    'node_group_nodelinks': (('node_groups', 'node_group_id'),),
    'node_group_projects': (('node_groups', 'group_id'),),
    'node_host_metrics': (('nodes', 'node_id'),),
    'node_nodelinks': (('nodes', 'node_id'),),
    'org_shared_stores': (('credentials', 'credential_id'),),
    'org_signups': (('admin_users', 'owner_id'),),
    'outcome_reviews': (('tasks', 'task_id'),),
    'password_resets': (('admin_users', 'user_id'),),
    'project_credentials': (('projects', 'project_id'),),
    'project_env_vars': (('projects', 'project_id'),),
    'project_memory_canon': (('projects', 'project_id'),),
    'project_memory_state': (('projects', 'project_id'),),
    'role_bindings': (('admin_users', 'user_id'), ('groups', 'group_id')),
    'runners': (('nodes', 'node_id'),),
    'site_chat_messages': (('site_chat_conversations', 'conversation_id'),),
    'slot_intents': (('nodes', 'node_id'),),
    'support_docs_misses': (('support_conversations', 'conversation_id'),),
    'support_messages': (('support_conversations', 'conversation_id'),),
    'tags': (('tag_groups', 'tag_group_id'),),
    'task_attachments': (
        ('tasks', 'task_id'), ('tickets', 'ticket_id'), ('support_conversations', 'conversation_id'),
        ('automations', 'automation_id'),
    ),
    'task_body_versions': (('tasks', 'task_id'),),
    'task_comment_mentions': (('tasks', 'task_id'), ('tickets', 'ticket_id')),
    'task_comment_reactions': (('tasks', 'task_id'), ('tickets', 'ticket_id')),
    'task_comments': (('tasks', 'task_id'), ('tickets', 'ticket_id')),
    'task_dependencies': (('tasks', 'task_id'), ('tasks', 'depends_on_task_id')),
    'task_pr_changes': (('tasks', 'task_id'),),
    'task_pr_stats': (('tasks', 'task_id'),),
    'task_questions': (('tasks', 'task_id'),),
    'task_ratings': (('tasks', 'task_id'), ('admin_users', 'user_id')),
    'task_tags': (('tasks', 'task_id'),),
    'ticket_reads': (('tickets', 'ticket_id'),),
    'ticket_task_links': (('tickets', 'ticket_id'),),
    'trigger_dedup': (('task_triggers', 'trigger_id'),),
    'user_git_connections': (('admin_users', 'user_id'),),
    'user_group_memberships': (('admin_users', 'user_id'),),
    'user_mfa_factors': (('admin_users', 'user_id'),),
    'user_recovery_codes': (('admin_users', 'user_id'),),
    'user_trusted_devices': (('admin_users', 'user_id'),),
    'vnode_enrolments': (('virtual_nodes', 'virtual_node_id'),),
    'vnode_hosts': (('nodes', 'node_id'),),
}
_COMPOSITE_KEY_CENSUS = (
    ('access_requests', 'requester_id', 'admin_users', 'CASCADE'),
    ('account_observations', 'node_id', 'nodes', 'CASCADE'),
    ('api_key_secrets', 'api_key_id', 'api_keys', 'CASCADE'),
    ('budgets', 'group_id', 'groups', 'CASCADE'),
    ('budgets', 'project_id', 'projects', 'CASCADE'),
    ('cloud_node_defaults', 'credential_id', 'credentials', 'CASCADE'),
    ('compliance_manifests', 'task_id', 'tasks', 'CASCADE'),
    ('console_api_key_leases', 'account_id', 'accounts', 'CASCADE'),
    ('credential_oauth_flows', 'node_id', 'nodes', 'CASCADE'),
    ('cron_runs', 'cron_id', 'crons', 'CASCADE'),
    ('dmarc_report_rows', 'dmarc_report_id', 'dmarc_reports', 'CASCADE'),
    ('features', 'project_id', 'projects', 'CASCADE'),
    ('fetch_stamps', 'node_id', 'nodes', 'CASCADE'),
    ('gha_runner_pools', 'node_id', 'nodes', 'CASCADE'),
    ('gov_campaign_items', 'campaign_id', 'gov_campaigns', 'CASCADE'),
    ('gov_edges', 'dst_id', 'gov_entities', 'CASCADE'),
    ('gov_edges', 'src_id', 'gov_entities', 'CASCADE'),
    ('gov_facts', 'entity_id', 'gov_entities', 'CASCADE'),
    ('gov_pack_releases', 'pack_id', 'gov_packs', 'CASCADE'),
    ('gov_rules', 'pack_id', 'gov_packs', 'CASCADE'),
    ('gov_scan_targets', 'scan_id', 'gov_scans', 'CASCADE'),
    ('hosting_changes', 'rental_id', 'node_rentals', 'CASCADE'),
    ('live_cast_items', 'project_id', 'projects', 'CASCADE'),
    ('live_cast_projects', 'project_id', 'projects', 'CASCADE'),
    ('memory_capture_log', 'task_id', 'tasks', 'CASCADE'),
    ('memory_claims', 'project_id', 'projects', 'CASCADE'),
    ('memory_events', 'task_id', 'tasks', 'CASCADE'),
    ('memory_rejections', 'project_id', 'projects', 'CASCADE'),
    ('memory_sources', 'unit_id', 'project_memories', 'CASCADE'),
    ('memory_votes', 'unit_id', 'project_memories', 'CASCADE'),
    ('microvm_intents', 'node_id', 'nodes', 'CASCADE'),
    ('microvms', 'node_id', 'nodes', 'CASCADE'),
    ('node_disk_events', 'node_id', 'nodes', 'CASCADE'),
    ('node_group_capacity', 'group_id', 'node_groups', 'CASCADE'),
    ('node_group_nodelinks', 'node_group_id', 'node_groups', 'CASCADE'),
    ('node_group_projects', 'group_id', 'node_groups', 'CASCADE'),
    ('node_host_metrics', 'node_id', 'nodes', 'CASCADE'),
    ('node_nodelinks', 'node_id', 'nodes', 'CASCADE'),
    ('node_projects', 'node_id', 'nodes', 'CASCADE'),
    ('node_session_usage', 'node_id', 'nodes', 'CASCADE'),
    ('nodevault_grants', 'node_id', 'nodes', 'CASCADE'),
    ('nodevault_operation_items', 'operation_id', 'nodevault_operations', 'CASCADE'),
    ('org_shared_stores', 'credential_id', 'credentials', 'CASCADE'),
    ('org_signups', 'owner_id', 'admin_users', 'RESTRICT'),
    ('outcome_reviews', 'task_id', 'tasks', 'CASCADE'),
    ('project_credentials', 'project_id', 'projects', 'CASCADE'),
    ('project_env_vars', 'project_id', 'projects', 'CASCADE'),
    ('project_memory_canon', 'project_id', 'projects', 'CASCADE'),
    ('project_memory_repo_state', 'project_id', 'projects', 'CASCADE'),
    ('project_memory_state', 'project_id', 'projects', 'CASCADE'),
    ('project_repos', 'project_id', 'projects', 'CASCADE'),
    ('role_bindings', 'group_id', 'groups', 'CASCADE'),
    ('role_bindings', 'user_id', 'admin_users', 'CASCADE'),
    ('runners', 'node_id', 'nodes', 'CASCADE'),
    ('site_chat_messages', 'conversation_id', 'site_chat_conversations', 'CASCADE'),
    ('slot_intents', 'node_id', 'nodes', 'CASCADE'),
    ('support_docs_misses', 'conversation_id', 'support_conversations', 'CASCADE'),
    ('support_messages', 'conversation_id', 'support_conversations', 'CASCADE'),
    ('tags', 'tag_group_id', 'tag_groups', 'CASCADE'),
    ('task_attachments', 'automation_id', 'automations', 'CASCADE'),
    ('task_attachments', 'conversation_id', 'support_conversations', 'CASCADE'),
    ('task_attachments', 'task_id', 'tasks', 'CASCADE'),
    ('task_attachments', 'ticket_id', 'tickets', 'CASCADE'),
    ('task_body_versions', 'task_id', 'tasks', 'CASCADE'),
    ('task_comment_mentions', 'task_id', 'tasks', 'CASCADE'),
    ('task_comment_mentions', 'ticket_id', 'tickets', 'CASCADE'),
    ('task_comment_reactions', 'task_id', 'tasks', 'CASCADE'),
    ('task_comment_reactions', 'ticket_id', 'tickets', 'CASCADE'),
    ('task_comments', 'task_id', 'tasks', 'CASCADE'),
    ('task_comments', 'ticket_id', 'tickets', 'CASCADE'),
    ('task_dependencies', 'depends_on_task_id', 'tasks', 'CASCADE'),
    ('task_dependencies', 'task_id', 'tasks', 'CASCADE'),
    ('task_pr_changes', 'task_id', 'tasks', 'CASCADE'),
    ('task_pr_stats', 'task_id', 'tasks', 'CASCADE'),
    ('task_prs', 'task_id', 'tasks', 'CASCADE'),
    ('task_questions', 'task_id', 'tasks', 'CASCADE'),
    ('task_ratings', 'task_id', 'tasks', 'CASCADE'),
    ('task_ratings', 'user_id', 'admin_users', 'CASCADE'),
    ('task_tags', 'task_id', 'tasks', 'CASCADE'),
    ('ticket_reads', 'ticket_id', 'tickets', 'CASCADE'),
    ('ticket_task_links', 'ticket_id', 'tickets', 'CASCADE'),
    ('trigger_dedup', 'trigger_id', 'task_triggers', 'CASCADE'),
    ('user_git_connections', 'user_id', 'admin_users', 'CASCADE'),
    ('user_group_memberships', 'user_id', 'admin_users', 'CASCADE'),
    ('vnode_enrolments', 'virtual_node_id', 'virtual_nodes', 'CASCADE'),
    ('vnode_hosts', 'node_id', 'nodes', 'RESTRICT'),
    ('worktrees', 'node_id', 'nodes', 'CASCADE'),
)
# The person's security records (0361): `user_id` names the membership that acted and no longer owns the
# row, so these keys are `ON DELETE SET NULL` on BOTH columns — the plain form, not `keys.py`'s
# column-list `SET NULL (user_id)` — and `clear_org_id_with_user_id` empties `org_id` when it fires.
_IDENTITY_RECORD_TABLES = (
    "user_mfa_factors",
    "user_recovery_codes",
    "user_trusted_devices",
    "password_resets",
    "legal_acceptances",
)
# The two whose TOTP secret's AAD and code hashes are keyed on a membership id (`app/mfa.py`).
_KEYED_RECORD_TABLES = ("user_mfa_factors", "user_recovery_codes")
_SAME_ORG_KEY_CENSUS = (
    ('access_requests', 'account_id', 'accounts', 'CASCADE'),
    ('access_requests', 'project_id', 'projects', 'CASCADE'),
    ('features', 'lead_id', 'admin_users', 'RESTRICT'),
    ('gov_campaign_items', 'project_id', 'projects', 'CASCADE'),
    ('gov_scan_targets', 'project_id', 'projects', 'CASCADE'),
    ('memory_events', 'unit_id', 'project_memories', 'CASCADE'),
    ('memory_sources', 'task_id', 'tasks', 'SET NULL'),
    ('memory_votes', 'task_id', 'tasks', 'CASCADE'),
    ('node_group_nodelinks', 'nodelink_id', 'nodelinks', 'CASCADE'),
    ('node_nodelinks', 'nodelink_id', 'nodelinks', 'CASCADE'),
    ('project_credentials', 'credential_id', 'credentials', 'CASCADE'),
    ('role_bindings', 'account_id', 'accounts', 'CASCADE'),
    ('role_bindings', 'project_id', 'projects', 'CASCADE'),
    ('user_git_connections', 'credential_id', 'credentials', 'CASCADE'),
)

# The trigger functions no app module owns, carried verbatim from the revision that last defined each:
# the task-feature resolver (0300), the email-proof reset (0303) and the immutable token ledger (0336). Schema-pinned where the original was: a `pg_temp` table
# must not shadow the lookup.
_RESOLVE_FEATURE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.resolve_task_feature() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.parent_task_id IS NOT NULL AND NEW.feature_id IS NULL THEN
            SELECT parent.feature_id INTO NEW.feature_id
              FROM public.tasks parent
             WHERE parent.id = NEW.parent_task_id
               AND parent.project_id = NEW.project_id
               FOR SHARE;
        END IF;
    ELSIF NEW.project_id IS DISTINCT FROM OLD.project_id
       AND NEW.feature_id IS NOT DISTINCT FROM OLD.feature_id
    THEN
        NEW.feature_id := NULL;
    END IF;
    RETURN NEW;
END;
$$
"""

_CLEAR_EMAIL_PROOF_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION admin_users_clear_email_proof() RETURNS trigger AS $$
BEGIN
  IF lower(NEW.email) IS DISTINCT FROM lower(OLD.email) THEN
    NEW.email_verified_at := NULL;
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

_TOKEN_LEDGER_IMMUTABLE_FUNCTION_SQL = """
        CREATE FUNCTION token_ledger_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND TG_LEVEL = 'ROW'
               AND OLD.org_id::text = ANY(string_to_array(current_setting('token_billing.purging_org', true), ',')) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'token_ledger_entries is immutable: % refused', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """

# The person's functions (0360, 0361), carried verbatim: the membership insert that attaches or creates
# its identity, the legacy-column write-through, the identity's own email-proof reset, and the security
# records' fill, key, orphan and reap functions. Revision 2 of the identity contraction drops the first two.
#: The display preferences that move to the person, frozen (`app/preferences.py::IDENTITY_PREFERENCE_KEYS`).
# The compliance ledger's append-only guard (0378), sharing the org purge's one door with the token ledger.
_GOV_LEDGER_IMMUTABLE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.gov_ledger_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- The org purge's one door (`app/tenancy/org_lifecycle.py::purge_org_rows`), shared with
    -- `token_ledger_entries`: erasing an org erases its ledger. Transaction-local, naming that org only.
    IF TG_OP = 'DELETE'
       AND OLD.org_id::text = ANY(string_to_array(current_setting('token_billing.purging_org', true), ',')) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'gov_ledger is append-only: % of row % refused', TG_OP, OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

_DISPLAY_KEYS_SQL = "ARRAY['theme', 'wide_mode', 'nav_collapsed_groups']"


def _display_preferences(source: str) -> str:
    """SQL: the display keys of the jsonb `source`, as an object (`'{}'` when it holds none)."""
    return (
        f"(SELECT coalesce(jsonb_object_agg(e.key, e.value), '{{}}'::jsonb) "
        f"FROM jsonb_each(CASE WHEN jsonb_typeof({source}) = 'object' THEN {source} ELSE '{{}}'::jsonb END) "
        f"AS e(key, value) WHERE e.key = ANY({_DISPLAY_KEYS_SQL}))"
    )


#: The person-level columns, frozen as this revision applied them (`app/models/identity.py::IDENTITY_FIELDS`).
_FIELDS = (
    "email",
    "email_verified_at",
    "password_hash",
    "first_name",
    "last_name",
    "google_sub",
    "github_user_id",
    "microsoft_tenant_id",
    "microsoft_object_id",
)

_ATTACH_FUNCTION = f"""
CREATE OR REPLACE FUNCTION public.admin_users_identity_attach() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  existing identities%ROWTYPE;
  has_rows boolean;
BEGIN
  IF NEW.identity_id IS NOT NULL THEN
    RETURN NEW;
  END IF;
  SELECT * INTO existing FROM identities
   WHERE lower(email) = lower(NEW.email)
   ORDER BY created_at, id
   LIMIT 1
   FOR UPDATE;
  IF NOT FOUND THEN
    INSERT INTO identities (
      email, email_verified_at, password_hash, first_name, last_name,
      google_sub, github_user_id, microsoft_tenant_id, microsoft_object_id,
      created_by_id, updated_by_id, preferences
    ) VALUES (
      NEW.email, NEW.email_verified_at, NEW.password_hash, NEW.first_name, NEW.last_name,
      NEW.google_sub, NEW.github_user_id, NEW.microsoft_tenant_id, NEW.microsoft_object_id,
      NEW.created_by_id, NEW.updated_by_id, {_display_preferences("NEW.preferences")}
    ) RETURNING id INTO NEW.identity_id;
    RETURN NEW;
  END IF;
  NEW.identity_id := existing.id;
  SELECT EXISTS (SELECT 1 FROM admin_users WHERE identity_id = existing.id) INTO has_rows;
  IF NOT has_rows THEN
    -- An orphaned identity (every membership deleted): the new row is the person now.
    UPDATE identities SET
      email = NEW.email, email_verified_at = NEW.email_verified_at,
      password_hash = NEW.password_hash, first_name = NEW.first_name, last_name = NEW.last_name,
      google_sub = NEW.google_sub, github_user_id = NEW.github_user_id,
      microsoft_tenant_id = NEW.microsoft_tenant_id, microsoft_object_id = NEW.microsoft_object_id
     WHERE id = existing.id;
    RETURN NEW;
  END IF;
  -- A live person: only a caller that names the identity (an invitation accept, a self-create) may add a
  -- membership to it. An insert that merely carries their email is refused exactly as the retired global
  -- unique refused it — so neither release can attach someone to a person by typing their address.
  RAISE EXCEPTION 'an account already holds the email %', NEW.email
    USING ERRCODE = 'unique_violation', CONSTRAINT = 'admin_users_email_key';
END
$$
"""

_SYNC_FUNCTION = """
CREATE OR REPLACE FUNCTION public.admin_users_identity_sync() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.identity_id IS NULL OR NEW.identity_id IS DISTINCT FROM OLD.identity_id THEN
    RETURN NEW;
  END IF;
  -- Only the columns THIS update changed: a sibling membership's stale legacy copy is never written back.
  UPDATE identities AS i SET
""" + ",\n".join(
    f"    {c} = CASE WHEN NEW.{c} IS DISTINCT FROM OLD.{c} THEN NEW.{c} ELSE i.{c} END" for c in _FIELDS
) + f""",
    preferences = i.preferences || (
      SELECT coalesce(jsonb_object_agg(k, NEW.preferences -> k), '{{}}'::jsonb)
        FROM unnest({_DISPLAY_KEYS_SQL}) AS k
       WHERE jsonb_typeof(NEW.preferences) = 'object' AND NEW.preferences ? k
         AND (NEW.preferences -> k) IS DISTINCT FROM (OLD.preferences -> k)
    )
   WHERE i.id = NEW.identity_id
     AND (""" + " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in (*_FIELDS, "preferences")) + """);
  RETURN NEW;
END
$$
"""

_EMAIL_PROOF_FUNCTION = """
CREATE OR REPLACE FUNCTION public.identities_clear_email_proof() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF lower(NEW.email) IS DISTINCT FROM lower(OLD.email) THEN
    NEW.email_verified_at := NULL;
  END IF;
  RETURN NEW;
END
$$
"""

_FILL_FUNCTION = "public.fill_identity_id_from_user"

_FILL_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {_FILL_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  SELECT a.identity_id INTO NEW.identity_id FROM public.admin_users AS a WHERE a.id = NEW.user_id;
  RETURN NEW;
END
$$
"""

_KEY_FUNCTION = "public.fill_key_user_id_from_user"

_KEY_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {_KEY_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.key_user_id := NEW.user_id;
  RETURN NEW;
END
$$
"""


_ORPHAN_FUNCTION = "public.clear_org_id_with_user_id"

# The two SET NULL keys fire in an unspecified order, and once `user_id` is NULL the composite key no longer
# matches the row — so the org is cleared here, by whichever runs first, never left naming a deleted org.
_ORPHAN_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {_ORPHAN_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.org_id := NULL;
  RETURN NEW;
END
$$
"""


_REAP_FUNCTION = "public.reap_identity_records_with_last_membership"
_REAP_TRIGGER = "trg_admin_users_identity_records_reap"

# A person with no membership left is no one's: 0360's insert trigger hands an orphaned identity to the next
# account made with its email, so their records must not be waiting there. Row-level AFTER triggers fire at
# the end of the statement, so a multi-row delete (an org purge) reads its final state.
_REAP_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {_REAP_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF OLD.identity_id IS NULL
     OR EXISTS (SELECT 1 FROM public.admin_users WHERE identity_id = OLD.identity_id) THEN
    RETURN NULL;
  END IF;
""" + "".join(
    f"  DELETE FROM public.{table} WHERE identity_id = OLD.identity_id;\n" for table in _IDENTITY_RECORD_TABLES
) + """  RETURN NULL;
END
$$
"""

_TRIGGERS_SQL = (
    "CREATE TRIGGER trg_tasks_resolve_feature BEFORE INSERT OR UPDATE OF project_id ON tasks "
    "FOR EACH ROW EXECUTE FUNCTION public.resolve_task_feature()",
    "CREATE TRIGGER trg_admin_users_email_proof BEFORE UPDATE OF email ON admin_users "
    "FOR EACH ROW EXECUTE FUNCTION admin_users_clear_email_proof()",
    "CREATE TRIGGER token_ledger_entries_immutable BEFORE UPDATE OR DELETE ON token_ledger_entries "
    "FOR EACH ROW EXECUTE FUNCTION token_ledger_immutable()",
    # Row triggers never see a TRUNCATE; this one refuses it outright (`TG_OP` is not DELETE, so no escape).
    "CREATE TRIGGER token_ledger_entries_no_truncate BEFORE TRUNCATE ON token_ledger_entries "
    "FOR EACH STATEMENT EXECUTE FUNCTION token_ledger_immutable()",
    "CREATE TRIGGER trg_gov_ledger_immutable BEFORE UPDATE OR DELETE ON gov_ledger "
    "FOR EACH ROW EXECUTE FUNCTION public.gov_ledger_immutable()",
    "CREATE TRIGGER trg_identities_email_proof BEFORE UPDATE OF email ON identities "
    "FOR EACH ROW EXECUTE FUNCTION identities_clear_email_proof()",
    "CREATE TRIGGER trg_admin_users_identity_attach BEFORE INSERT ON admin_users "
    "FOR EACH ROW EXECUTE FUNCTION admin_users_identity_attach()",
    "CREATE TRIGGER trg_admin_users_identity_sync BEFORE UPDATE OF "
    f"{', '.join(_FIELDS)}, preferences, identity_id ON admin_users "
    "FOR EACH ROW EXECUTE FUNCTION admin_users_identity_sync()",
    f"CREATE TRIGGER trg_admin_users_identity_records_reap AFTER DELETE ON admin_users FOR EACH ROW "
    f"EXECUTE FUNCTION {_REAP_FUNCTION}()",
    *(
        f"CREATE TRIGGER trg_{table}_identity_id_fill BEFORE INSERT ON {table} FOR EACH ROW "
        f"WHEN (NEW.user_id IS NOT NULL) EXECUTE FUNCTION {_FILL_FUNCTION}()"
        for table in _IDENTITY_RECORD_TABLES
    ),
    *(
        f"CREATE TRIGGER trg_{table}_key_user_id_fill BEFORE INSERT ON {table} FOR EACH ROW "
        f"WHEN (NEW.key_user_id IS NULL) EXECUTE FUNCTION {_KEY_FUNCTION}()"
        for table in _KEYED_RECORD_TABLES
    ),
    *(
        f"CREATE TRIGGER trg_{table}_org_id_clear BEFORE UPDATE OF user_id ON {table} FOR EACH ROW "
        f"WHEN (NEW.user_id IS NULL) EXECUTE FUNCTION {_ORPHAN_FUNCTION}()"
        for table in _IDENTITY_RECORD_TABLES
    ),
    "CREATE CONSTRAINT TRIGGER trg_automations_one_child AFTER INSERT OR UPDATE OF when_kind ON automations "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.automation_check_one_child()",
    "CREATE CONSTRAINT TRIGGER trg_crons_automation_one_child AFTER INSERT OR UPDATE OF automation_id ON crons "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.automation_check_one_child()",
    "CREATE CONSTRAINT TRIGGER trg_task_triggers_automation_one_child AFTER INSERT OR UPDATE OF automation_id "
    "ON task_triggers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.automation_check_one_child()",
    "CREATE TRIGGER trg_crons_drop_childless_automation AFTER DELETE ON crons FOR EACH ROW "
    "WHEN (OLD.automation_id IS NOT NULL) EXECUTE FUNCTION public.automation_drop_childless()",
    "CREATE TRIGGER trg_task_triggers_drop_childless_automation AFTER DELETE ON task_triggers FOR EACH ROW "
    "WHEN (OLD.automation_id IS NOT NULL) EXECUTE FUNCTION public.automation_drop_childless()",
)

# The automation one-child rule (0417): an automation owns exactly one schedule (`crons`) or event
# (`task_triggers`) child, checked at commit. `automation_assert_one_child` is called for each automation a write touched; a parent that
# no longer exists (deleted in the same transaction) is not a violation.
_AUTOMATION_ONE_CHILD_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.automation_assert_one_child(aid uuid) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    kind text;
    schedules int;
    events int;
BEGIN
    IF aid IS NULL THEN
        RETURN;
    END IF;
    SELECT when_kind INTO kind FROM automations WHERE id = aid;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    SELECT count(*) INTO schedules FROM crons WHERE automation_id = aid;
    SELECT count(*) INTO events FROM task_triggers WHERE automation_id = aid;
    IF (kind = 'schedule' AND schedules = 1 AND events = 0) OR (kind = 'event' AND events = 1 AND schedules = 0) THEN
        RETURN;
    END IF;
    RAISE EXCEPTION 'automation % must have exactly one % child (crons: %, task_triggers: %)',
        aid, kind, schedules, events USING ERRCODE = '23514', CONSTRAINT = 'ck_automations_one_child';
END;
$$
"""

_AUTOMATION_CHECK_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.automation_check_one_child() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'automations' THEN
        PERFORM public.automation_assert_one_child(NEW.id);
        RETURN NULL;
    END IF;
    PERFORM public.automation_assert_one_child(NEW.automation_id);
    IF TG_OP = 'UPDATE' AND OLD.automation_id IS DISTINCT FROM NEW.automation_id THEN
        PERFORM public.automation_assert_one_child(OLD.automation_id);
    END IF;
    RETURN NULL;
END;
$$
"""

_AUTOMATION_DROP_CHILDLESS_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.automation_drop_childless() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM automations a
    WHERE a.id = OLD.automation_id
      AND NOT EXISTS (SELECT 1 FROM crons c WHERE c.automation_id = a.id)
      AND NOT EXISTS (SELECT 1 FROM task_triggers t WHERE t.automation_id = a.id);
    RETURN NULL;
END;
$$
"""

# `cp_readonly`, the read-only login the operator's psql helper connects as by default, carried from 0322 (whose
# header is in git history). RDS ONLY: a no-op where `rds_iam` is absent — a `./dc` stack, the test
# template — since there is no IAM to authenticate a passwordless LOGIN against. Roles are cluster-wide
# and every statement is idempotent, so running it on a control plane that already has the role is harmless.
_CP_READONLY_ROLE_SQL = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
    RETURN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cp_readonly') THEN
    CREATE ROLE cp_readonly LOGIN;
  END IF;
  GRANT rds_iam TO cp_readonly;
  GRANT pg_read_all_data TO cp_readonly;
  REVOKE CREATE ON SCHEMA public FROM PUBLIC;
  ALTER ROLE cp_readonly CONNECTION LIMIT 5;
  ALTER ROLE cp_readonly SET statement_timeout = '5min';
  ALTER ROLE cp_readonly SET lock_timeout = '5s';
  ALTER ROLE cp_readonly SET idle_in_transaction_session_timeout = '2min';
  ALTER ROLE cp_readonly SET idle_session_timeout = '30min';
END
$$;
"""

# The system crons a fresh database comes up with, and their scheduling parameters: the end state of the
# collapsed band (crons seeded, deleted and retuned along the way), not its history. All are `is_system`,
# and all therefore live in org zero (0188), which is written into the insert rather than reached by a
# later UPDATE.
#
# LITERAL, AND NOT FOR THE REASON 0142 GAVE. That revision said a migration cannot import
# `app/cron_system_activities.py` without dragging the ORM into the alembic import graph. True of 0142;
# NOT true here, because `_seed` already imports `app.authz`, which imports `app.models`. The honest reason is narrower: that registry declares an activity's LABEL and handler, not
# its schedule, timeout, retention mode or armed state, so there is nothing there to import for eight of
# the ten fields below. `tests/test_cron_system_activities.py` is the drift guard, and it pins the
# schedule fields too, not only the labels.
#
# THREE ARE SEEDED DISABLED — `lease-drain`, `node-liveness` and `task-pipeline` — and arming them is a
# post-deploy step nothing performs for you; every other system cron ships armed. This is the home for that procedure now that 0142 is collapsed away:
# enable them ONE AT A TIME on the Cron page, least coupled first, and check each job's own effect before
# the next —
# `node-liveness` (a `noop` run inside 300s means every liveness invariant holds), then `task-pipeline`
# (an epic with a `draft` child advances it to `ready`), then `lease-drain` (a task marked `ready`
# reaches a `lease_slot` intent in seconds). ORDER MATTERS BECAUSE ONE OF THEM FAILS SILENTLY: the other
# two announce themselves by not dispatching and not advancing, but `node-liveness` off is invisible —
# `app/cron_engine_job.py`'s stall assertion scans `enabled` rows only, and every cron alarm is written
# BY a firing, so nothing detects a liveness cron that never runs.
_SYSTEM_CRONS = (
    (
        "Lease drain", "lease-drain", "interval", None, 10, False, "acted", 5, 60,
        "The fleet's dispatch decision: releases finished holds, reclaims un-acked dispatches and "
        "issues a lease_slot intent per dispatchable task. Migrated from the in-process scheduler.",
    ),
    (
        "Node groups", "node-groups", "interval", None, 60, True, "acted", None, 300,
        "Holds every enabled dynamic node group at its desired capacity: launches the nodes a capacity "
        "line is short, terminates the free ones it is over by, and declares each group's assigned "
        "worktrees on the nodes it owns.",
    ),
    (
        "Node liveness", "node-liveness", "interval", None, 300, False, "all", None, 120,
        "Reads the node roster, places release generations and asserts the fleet's liveness invariants. "
        "Migrated from the in-process scheduler.",
    ),
    (
        "Retention sweep", "retention_sweep", "cron", "0 20 3 * * *", None, True, "acted", None, 1800,
        "Prunes the cron ledger and DBOS's finished workflow history. Without it the control plane's "
        "system schema grows for ever — DBOS ships a collector but never calls it.",
    ),
    (
        "Task pipeline", "task-pipeline", "interval", None, 15, False, "acted", 5, 90,
        "Advances the Task pipeline: epic fan-out, epic rollups, Needs-attention notifies and the three "
        "crash reclaimers. Migrated from the in-process scheduler.",
    ),
    (
        "Headscale allowlist", "headscale-allowlist", "interval", None, 300, True, "acted", None, 60,
        "Writes the egress IPs of enabled nodes that reported in the last 7 days into this "
        "environment's headscale edge allow set.",
    ),
    (
        "Support knowledge base", "support-kb-index", "interval", None, 600, True, "acted", 30, 540,
        "Embeds the shipped documentation into the support agent's knowledge base, adding what changed "
        "and dropping what the corpus no longer holds.",
    ),
    (
        "Security event export", "security-event-export", "cron", "0 40 1 * * *", None, True, "all", 30, 600,
        "Exports each day's security events (sign-ins, access and role changes, credential use) to the "
        "write-once archive bucket, filling any day a missed night left out.",
    ),
    (
        "CloudWatch metrics", "metrics-emit", "interval", None, 60, True, "none", 10, 30,
        "Publishes cron starvation and node disk space as CloudWatch metrics every minute, for the paging "
        "alarms.",
    ),
    (
        "Billing events", "billing-events", "interval", None, 20, True, "acted", 5, 60,
        "Reads new events from the connected Stripe account and refreshes the billing mirrors from the "
        "objects they name.",
    ),
    (
        "Billing reconcile", "billing-reconcile", "cron", "0 15 3 * * *", None, True, "all", 30, 600,
        "Re-reads every open subscription, saved payment method and open invoice from the connected "
        "Stripe account, catching anything an event missed.",
    ),
    (
        "Billing card expiry", "billing-card-expiry", "cron", "0 0 14 * * *", None, True, "acted", 30, 300,
        "Warns each org's Owners and Admins about saved cards that expire within 30 days, within 7 days, "
        "or have expired — once per card per threshold.",
    ),
    (
        "Bedrock health", "bedrock-health", "interval", None, 300, True, "acted", 30, 120,
        "Checks the platform Bedrock account, repairs the tenant deny policy and alerts the operator when "
        "the account becomes unavailable.",
    ),
    (
        "Token billing audit", "token-billing-audit", "cron", "0 40 3 * * *", None, True, "acted", 60, 120,
        "Checks every Jaah AI balance against its ledger and alerts the operator if one disagrees.",
    ),
    (
        "Jaah session reap", "jaah-session-reap", "interval", None, 300, True, "acted", 60, 60,
        "Ends Jaah AI account sessions that never asked for credentials, so they can no longer get any.",
    ),
    (
        "Bedrock usage correction", "bedrock-usage-correct", "interval", None, 15, True, "acted", 5, 120,
        "Reads AWS's Bedrock invocation log and corrects each Jaah AI call's charge by what AWS recorded.",
    ),
    (
        "Bedrock cost reconciliation", "bedrock-reconcile", "cron", "0 0 6 * * *", None, True, "acted", 60, 300,
        "Compares each organization's Jaah AI cost for yesterday with AWS's and alerts the operator when "
        "they differ by more than the tolerance.",
    ),
    (
        "Node orders", "node-order-sweep", "interval", None, 300, True, "acted", None, 120,
        "Ends Add-node orders that cannot finish: a self-hosted machine that never joined, and a "
        "payment nobody completed.",
    ),
    (
        "Rentals", "rental-lifecycle", "interval", None, 300, True, "acted", None, 240,
        "Moves rented hosts and nodes through unpaid, stopped and deleted, and warns their owners first.",
    ),
    (
        "Support knowledge base: jaah.ai", "support-kb-site", "interval", None, 3600, True, "acted", 30, 3000,
        "Crawls the English pages of jaah.ai into the support agent's knowledge base, adding what "
        "changed and dropping what the site no longer publishes.",
    ),
    (
        "Plan products", "plan-bundles", "interval", None, 120, True, "acted", 5, 600,
        "Gives each organization the nodes and Jaah AI credit its plan includes, and releases the ones it "
        "no longer does.",
    ),
    (
        "Alarm relay", "alarm-relay", "interval", None, 30, True, "acted", None, 25,
        "Posts each CloudWatch alarm and its resolution to Slack: the alert channel and every superadmin.",
    ),
    (
        "Compliance reconcile", "compliance-reconcile", "interval", None, 60, True, "acted", None, 50,
        "Marks a compliance scan's repo as errored when its auditor run ended without a result, and lapses "
        "expired waivers.",
    ),
    (
        "Compliance daily scan", "compliance-daily", "cron", "0 0 3 * * *", None, True, "acted", 300, 600,
        "Rescans each organization's last compliance selection every day, where Compliance and its daily "
        "scan are on.",
    ),
    (
        "Trigger intake", "trigger-intake", "interval", None, 60, True, "acted", 2, 60,
        "Processes trigger deliveries: preflight, dedup, limits, and filing each Task.",
    ),
    (
        "Ops snapshot", "ops-snapshot", "interval", None, 3600, True, "acted", 60, 300,
        "Records node load, worktree occupancy, account quota and wallet floor each hour, kept a year for "
        "the operator dashboard's trends.",
    ),
    (
        "AI quality review", "outcome-review", "cron", "0 0 5 * * 1", None, True, "all", 300, 1800,
        "Each week, an AI model scores a sample of the tasks delivered in the last 7 days from 1 to 5, with "
        "a reason, for the operator dashboard's Quality panel.",
    ),
    (
        "Project memory curation", "memory-curate", "interval", None, 60, True, "acted", 5, 120,
        "Turns what settled tasks recorded into project memory: mines their findings, distils transcripts "
        "when a model is set, then checks, merges and accepts proposed lessons.",
    ),
    (
        "AI insight digest", "insight-digest", "cron", "0 0 6 * * 1", None, True, "all", 300, 900,
        "Each week, an AI model summarises what changed on the operator dashboard and the likely causes, "
        "each point citing its metric, for the dashboard's Insights panel.",
    ),
    (
        "Project memory nightly", "memory-nightly", "cron", "0 0 3 * * *", None, True, "all", 60, 900,
        "Each night, retires project memory lessons nobody used, merges duplicates, recomputes trust, picks "
        "each project's top lessons, and files a draft task for each lesson that has earned a home in the "
        "repo.",
    ),
    (
        "Trigger digest", "trigger-digest", "cron", "0 0 8 * * *", None, True, "all", 60, 300,
        "Stops triggers with no successful run for their idle days, then emails each trigger owner a digest "
        "of the last 24 hours and adds it to their notifications.",
    ),
    (
        "Node SSM access", "node-ssm", "interval", None, 3600, True, "acted", None, 300,
        "Attaches the Jaah SSM instance profile to every cloud node the control plane launched that has none, "
        "so each one is reachable over AWS Systems Manager.",
    ),
    (
        "Compliance manifest sweep", "compliance-seal-sweep", "interval", None, 300, True, "acted", None, 120,
        "Starts the compliance manifest seal of every finished task that has none, two minutes after it "
        "finished — the backstop for the seal the task-done workflow starts.",
    ),
    (
        "Live Cast evaluation", "live-cast-sweep", "interval", None, 30, True, "none", None, 25,
        "Adds new tasks and text files of each published Live Cast project and starts the evaluation of every "
        "item whose rules or content changed since it was last judged.",
    ),
    (
        "NodeLink provisioning", "nodelink-provision", "interval", None, 15, True, "acted", 5, 120,
        "Finishes provisioning every NodeLink someone asked to provision: its storage, access point and policy, "
        "until it is ready or AWS refuses.",
    ),
    (
        "Control plane ECS tasks", "cp-ecs-record", "interval", None, 300, True, "acted", None, 60,
        "Records the control plane's own ECS tasks, with each stopped task's stop reason and exit codes, before "
        "ECS forgets them.",
    ),
    (
        "Plan rollover", "plan-rollover", "interval", None, 60, True, "acted", 5, 50,
        "Moves each organization whose plan has ended to the plan it names next, and emails its Owners and Admins.",
    ),
    (
        "Budget alerts", "budget-alerts", "interval", None, 60, True, "acted", 5, 50,
        "Emails each organization's Owners and Admins when a project or group budget reaches 80% or 100% of its "
        "month.",
    ),
    (
        "SIEM export", "siem-export", "interval", None, 60, True, "acted", 5, 50,
        "Posts each organization's new activity events as JSON to its SIEM destination, retrying until the "
        "destination accepts them.",
    ),
    (
        "Sign-up drafts", "signup-drafts", "interval", None, 3600, True, "acted", 60, 300,
        "Deletes each sign-up left unfinished for a day, and any card it saved.",
    ),
    (
        "Support lessons", "support-lessons", "interval", None, 300, True, "acted", 30, 240,
        "Drafts a lesson from each solved support ticket for an operator to approve, and adds the approved ones "
        "to the support agent's knowledge base.",
    ),
    (
        "API key expiry", "api-keys-hourly", "interval", None, 3600, True, "acted", 60, 300,
        "Reminds each API key's owner 7 days and 1 day before it expires, records each key that expired, and "
        "revokes the keys of a disabled member.",
    ),
    (
        "SES inbound mail", "ses-inbound", "interval", None, 30, True, "acted", None, 25,
        "Stores each message SES received for this environment's mail domain and routes it to the first matching "
        "email trigger, else Unmatched emails.",
    ),
    (
        "SES delivery events", "ses-events", "interval", None, 30, True, "acted", None, 25,
        "Records what SES reported for each system email (delivered, delayed, bounced, complained) in the "
        "delivery log, and suppresses an address after a hard bounce or a complaint.",
    ),
    # The SOC 2 evidence collectors (202610141000, 202610141200): each files one control's evidence every night.
    (
        "Evidence: security archive", "evidence-security-archive", "cron", "0 0 4 * * *", None, True, "all", 60, 300,
        "Checks that each of the last 30 days' security events is in the write-once archive and files the result "
        "as SOC 2 evidence; a missing day raises an alarm.",
    ),
    (
        "Evidence: CloudWatch alarms", "evidence-cloudwatch-alarms", "cron", "0 5 4 * * *", None, True, "all", 60, 300,
        "Files every CloudWatch alarm and its state as SOC 2 evidence; an alarm firing outside an accepted risk "
        "raises an alarm.",
    ),
    (
        "Evidence: paging subscriptions", "evidence-paging-subscriptions", "cron", "0 10 4 * * *", None, True, "all",
        60, 300,
        "Files the paging topics' subscriptions as SOC 2 evidence; a topic nobody confirmed raises an alarm.",
    ),
    (
        "Evidence: retention sweep", "evidence-retention-sweep", "cron", "0 15 4 * * *", None, True, "all", 60, 300,
        "Checks that the retention sweep completed in the last 26 hours and files the result as SOC 2 evidence; a "
        "missed sweep raises an alarm.",
    ),
    (
        "Evidence: Security Hub findings", "evidence-securityhub", "cron", "0 20 4 * * *", None, True, "all", 60, 300,
        "Files this account's open high and critical Security Hub findings as SOC 2 evidence; any finding raises "
        "an alarm.",
    ),
    (
        "Evidence: edge TLS and WAF", "evidence-edge-tls", "cron", "0 25 4 * * *", None, True, "all", 60, 300,
        "Files the load balancers' TLS policies, HTTP redirects and web ACLs as SOC 2 evidence; a gap raises an "
        "alarm.",
    ),
    (
        "Evidence: access events", "evidence-access-events", "cron", "0 30 4 * * *", None, True, "all", 60, 300,
        "Files a week's counts of role changes, failed sign-ins and credential reads as SOC 2 evidence.",
    ),
    (
        "Evidence: backups", "evidence-backups", "cron", "0 35 4 * * *", None, True, "all", 60, 300,
        "Files the last day's backup and copy jobs and the database's Multi-AZ setting as SOC 2 evidence; a gap "
        "raises an alarm.",
    ),
    (
        "Evidence: transcript expiry", "evidence-storage-lifecycle", "cron", "0 40 4 * * *", None, True, "all", 60, 300,
        "Files the transcripts bucket's expiry rules as SOC 2 evidence; a missing 180-day rule raises an alarm.",
    ),
    (
        "Evidence: two-factor posture", "evidence-mfa-posture", "cron", "0 45 4 * * *", None, True, "all", 60, 300,
        "Files the list of organizations with two-factor sign-in skipped as SOC 2 evidence.",
    ),
    (
        "Evidence: image scans", "evidence-ecr-scan", "cron", "0 50 4 * * *", None, True, "all", 60, 300,
        "Files the deployed images' vulnerability scans as SOC 2 evidence; a high or critical finding past its fix "
        "deadline raises an alarm.",
    ),
    (
        "Evidence: GitHub settings", "evidence-github-settings", "cron", "0 0 5 * * *", None, True, "all", 60, 300,
        "Compares the repository's GitHub rulesets and settings with the declared ones and files the export as "
        "SOC 2 evidence; a difference raises an alarm.",
    ),
    (
        "Evidence: Dependabot alerts", "evidence-dependabot", "cron", "0 30 5 * * *", None, True, "all", 60, 300,
        "Files the open Dependabot alerts and their age as SOC 2 evidence; an alert past the vulnerability fix "
        "deadline raises an alarm.",
    ),
)

# Every system cron's automation parent (0417: an automation is the parent row over exactly one schedule or event
# child), as 0417's backfill wrote it and 202610141700 completed it: the cron's own name, description and action,
# a slug from its name.
_SYSTEM_CRON_PARENTS_SQL = """
WITH children AS MATERIALIZED (
    SELECT c.id, c.org_id, c.name, c.description, c.created_at, c.updated_at, c.created_by_id, c.updated_by_id,
           c.activity_type, c.activity_params, uuidv7() AS parent_id
    FROM crons c
    WHERE c.is_system AND c.automation_id IS NULL
),
parents AS (
    INSERT INTO automations (
        id, org_id, name, description, created_at, updated_at, created_by_id, updated_by_id, slug, when_kind,
        is_system, target_ids, action_type, action_params
    )
    SELECT parent_id, org_id, name, description, created_at, updated_at, created_by_id, updated_by_id,
           coalesce(nullif(trim(both '-' from left(trim(both '-' from
               regexp_replace(lower(name), '[^a-z0-9]+', '-', 'g')), 60)), ''), 'automation'),
           'schedule', true, '[]'::jsonb, activity_type, activity_params
    FROM children
    RETURNING id
)
UPDATE crons c SET automation_id = ch.parent_id FROM children ch WHERE c.id = ch.id
"""

# The cloud-node instance types an operator may launch (0420), seeded from the knob's default list when no
# override existed: KVM-capable x86 only, matching the active instance products `_RENTAL_PRICES` seeds.
# Every row is AWS's (`provider`, 202610211437) and none Windows-capable — the column defaults, left to them.
_ALLOWED_INSTANCE_TYPES = (
    "c7i.large", "c7i.xlarge", "c8i.large", "c8i.xlarge", "m7i.large", "m7i.xlarge", "m8i.large", "m8i.xlarge",
    "r7i.large", "r7i.xlarge", "r8i.large", "r8i.xlarge", "c7i.metal-24xl",
)

# The rental catalogue for a fresh database, unpriced in Stripe (`app/billing/rental_prices.py` pushes every active
# row on the operator's next save and in the nightly `billing-reconcile`). Instances are KVM-capable x86 only
# (a node must be able to host MicroVMs) — the c/m/r 7i and 8i families and a metal type; the t4g rows stay,
# inactive, so a database seeded before the swap and one seeded after carry the same keys. Priced from AWS us-east-1
# on-demand Linux hourly (price list read 2026-10-11; t4g micro..xlarge $0.0084/0.0168/0.0336/0.0672/0.1344,
# c7i.large $0.08925, c8i.large $0.09371, m7i.large $0.1008, m8i.large $0.10584, r7i.large $0.1323, r8i.large
# $0.13892, each xlarge double its large, c7i.metal-24xl $4.284) x 730 h x 1.5 for an instance, gp3 $0.08/GB-month
# x 1.5 for storage, each rounded up to the cent. Shared keeps its price (half the t4g.small it was derived from):
# a Shared node is a co-located one, outside the swap. Shape: (kind, key, name, monthly_cents, active, sort_order,
# vcpu, ram_gb).
_RENTAL_PRICES = (
    ("instance", "t4g.micro", "t4g.micro", 920, False, 10, 2, 1),
    ("instance", "t4g.small", "t4g.small", 1840, False, 20, 2, 2),
    ("instance", "t4g.medium", "t4g.medium", 3680, False, 30, 2, 4),
    ("instance", "t4g.large", "t4g.large", 7359, False, 40, 2, 8),
    ("instance", "t4g.xlarge", "t4g.xlarge", 14717, False, 50, 4, 16),
    ("instance", "c7i.large", "c7i.large", 9773, True, 60, 2, 4),
    ("instance", "c7i.xlarge", "c7i.xlarge", 19546, True, 70, 4, 8),
    ("instance", "c8i.large", "c8i.large", 10262, True, 80, 2, 4),
    ("instance", "c8i.xlarge", "c8i.xlarge", 20523, True, 90, 4, 8),
    ("instance", "m7i.large", "m7i.large", 11038, True, 100, 2, 8),
    ("instance", "m7i.xlarge", "m7i.xlarge", 22076, True, 110, 4, 16),
    ("instance", "m8i.large", "m8i.large", 11590, True, 120, 2, 8),
    ("instance", "m8i.xlarge", "m8i.xlarge", 23179, True, 130, 4, 16),
    ("instance", "r7i.large", "r7i.large", 14487, True, 140, 2, 16),
    ("instance", "r7i.xlarge", "r7i.xlarge", 28974, True, 150, 4, 32),
    ("instance", "r8i.large", "r8i.large", 15212, True, 160, 2, 16),
    ("instance", "r8i.xlarge", "r8i.xlarge", 30424, True, 170, 4, 32),
    ("instance", "c7i.metal-24xl", "c7i.metal-24xl", 469098, True, 180, 96, 192),
    ("storage", "20", "20 GB", 240, True, 10, None, None),
    ("storage", "50", "50 GB", 600, True, 20, None, None),
    ("storage", "100", "100 GB", 1200, True, 30, None, None),
    ("shared", "shared", "Shared", 920, True, 10, None, None),
)


# The plan catalogue as the five-plan band left it (202610181200 wrote Free, Team, Business, Business trial and
# Enterprise beside 0374's Trial; 202610211200 added `(default)`, the row every plan's unset term falls back to; the
# revisions up to 202610251700 re-termed them — caps, switches, `inherits`, the provider grid). Each dict holds the
# columns that differ from the table's defaults; `seed_billing_plans` writes them. Business trial is the Default
# plan a new org takes. THE ORDER IS KEY-LOAD-BEARING: `includes_plan_key` and `then_plan_key` reference rows
# already inserted — `(default)` first, then Free, Team (includes Free), Business (includes Team), Business trial
# (includes Business, then Free) and Enterprise (includes Business); the Trial references none.
_DEFAULT_PLAN_KEY = "business-trial"
_BILLING_PLANS: dict[str, dict[str, Any]] = {
    "_default": {
        "name": "(default)",
        "limit_nodes": 0,
        "limit_projects": 3,
        "limit_users": 1,
        "limit_active_worktrees": 0,
        "active": False,
        "project_memory_enabled": True,
        "slack_enabled": True,
        "per_agent": True,
        "min_agents": 1,
        "max_agents": 1,
        "limit_task_attachment_mb": 100,
        "limit_attachment_file_mb": 100,
        "limit_org_attachment_gb": 100,
        "limit_agent_requests_per_minute": 3000,
        "cron_min_period_seconds": 3600,
        "providers": [["claude", "api_key"]],
    },
    "trial": {
        "name": "Trial",
        "blurb": "Free for 14 days",
        "limit_runner_slots": 4,
        "limit_nodes": 2,
        "limit_projects": 10,
        "limit_users": 10,
        "limit_active_worktrees": 4,
        "self_serve": True,
        "sort_order": 9,
        "price_unit_amount": 0,
        "limit_github_runners": 2,
        "duration_days": 14,
        "project_memory_enabled": True,
        "slack_enabled": True,
        "max_agents": 4,
        "inherits": ["agent_requests_per_minute", "attachment_file_mb", "cron_min_period_seconds", "org_attachment_gb", "task_attachment_mb", "providers"],
    },
    "free": {
        "name": "Free",
        "blurb": "One agent, one user, three projects",
        "limit_runner_slots": 1,
        "limit_nodes": 0,
        "limit_projects": 3,
        "limit_users": 1,
        "limit_active_worktrees": 0,
        "self_serve": True,
        "sort_order": 5,
        "price_unit_amount": 0,
        "microvm_allowed": False,
        "per_agent": True,
        "min_agents": 1,
        "max_agents": 1,
        "inherits": ["agent_requests_per_minute", "attachment_file_mb", "cron_min_period_seconds", "org_attachment_gb", "task_attachment_mb", "providers"],
    },
    "team": {
        "name": "Team",
        "limit_runner_slots": 0,
        "limit_nodes": 0,
        "limit_projects": 0,
        "limit_users": 0,
        "limit_active_worktrees": 0,
        "sort_order": 10,
        "merge_lane_enabled": True,
        "playbooks_enabled": True,
        "project_memory_enabled": True,
        "includes_plan_key": "free",
        "slack_enabled": True,
        "per_agent": True,
        "min_agents": 3,
        "max_agents": 9,
        "inherits": ["agent_requests_per_minute", "attachment_file_mb", "cron_min_period_seconds", "org_attachment_gb", "task_attachment_mb", "providers"],
    },
    "business": {
        "name": "Business",
        "limit_runner_slots": 0,
        "limit_nodes": 0,
        "limit_projects": 0,
        "limit_users": 0,
        "limit_active_worktrees": 0,
        "sort_order": 15,
        "compliance_enabled": True,
        "nodevault_allowed": True,
        "nodelink_allowed": True,
        "advanced_compliance_enabled": True,
        "includes_plan_key": "team",
        "budgets_enabled": True,
        "per_agent": True,
        "min_agents": 10,
        "inherits": ["agent_requests_per_minute", "attachment_file_mb", "cron_min_period_seconds", "org_attachment_gb", "task_attachment_mb"],
        "providers": [["claude", "api_key"], ["sdk", "own"]],
    },
    "business-trial": {
        "name": "Business trial",
        "blurb": "Business for 14 days, then Free",
        "limit_runner_slots": 3,
        "limit_nodes": 0,
        "limit_projects": 0,
        "limit_users": 0,
        "limit_active_worktrees": 0,
        "self_serve": True,
        "sort_order": 8,
        "price_unit_amount": 0,
        "duration_days": 14,
        "includes_plan_key": "business",
        "then_plan_key": "free",
        "max_agents": 3,
        "inherits": ["agent_requests_per_minute", "attachment_file_mb", "cron_min_period_seconds", "org_attachment_gb", "task_attachment_mb"],
        "providers": [["claude", "api_key"], ["sdk", "own"]],
    },
    "enterprise": {
        "name": "Enterprise",
        "limit_runner_slots": 0,
        "limit_nodes": 0,
        "limit_projects": 0,
        "limit_users": 0,
        "limit_active_worktrees": 0,
        "sort_order": 20,
        "project_memory_enabled": True,
        "includes_plan_key": "business",
        "sso_enabled": True,
        "scim_enabled": True,
        "siem_export_enabled": True,
        "data_residency_enabled": True,
        "ip_allowlist_enabled": True,
        "org_retention_enabled": True,
        "staging_org_enabled": True,
        "inherits": ["agent_requests_per_minute", "attachment_file_mb", "cron_min_period_seconds", "org_attachment_gb", "task_attachment_mb"],
        "providers": [["claude", "api_key"], ["sdk", "own"]],
    },
}

#: `billing_plans` columns a raw insert hands over as JSON (the provider grid, 202610251500).
_PLAN_JSONB_COLUMNS = frozenset({"providers"})


def seed_billing_plans(conn) -> None:
    """Insert every `_BILLING_PLANS` row that is not there yet, each with the columns it names; the rest take the
    table's defaults. `tests/billing/conftest.py` calls this to put the catalogue back after a billing test."""
    for key, plan in _BILLING_PLANS.items():
        columns = ["key", *plan]
        values = ", ".join(
            f"CAST(:{column} AS jsonb)" if column in _PLAN_JSONB_COLUMNS
            else f"CAST(:{column} AS text[])" if column == "inherits"
            else f":{column}"
            for column in columns
        )
        conn.execute(
            text(f"INSERT INTO billing_plans ({', '.join(columns)}) VALUES ({values}) ON CONFLICT (key) DO NOTHING"),
            {
                "key": key,
                **{
                    column: json.dumps(value) if column in _PLAN_JSONB_COLUMNS and value is not None else value
                    for column, value in plan.items()
                },
            },
        )


# Carried verbatim from 0106, the last revision to redefine the view. Reflection cannot see a view, so
# this is authored. `node_name` (a JOIN onto `nodes`) rather than `node_id` in the nine ORDER BY
# tie-breaks is load-bearing and not cosmetic: 0106 turned `node_id` into a uuid, and ordering on it would
# have silently reordered the fold from name-lexical to uuid-lexical — a different winner for the same
# data. That revision's header is the single home for the rest of the fold's semantics.
_ACCOUNT_FOLD_VIEW = """
CREATE OR REPLACE VIEW account_fold AS
WITH ranked AS (
  SELECT o.node_id, n.name AS node_name, o.slug, o.config_dir, o.enabled, o.email, o.display_name,
         o.quota, o.extra, o.observed_at,
         CASE WHEN jsonb_typeof(o.quota->'fiveHourPercent') = 'number'
                   AND o.quota->>'fiveHourPercent' !~ '[.eE]'
                   AND (o.quota->>'fiveHourPercent')::numeric >= 0
              THEN (o.quota->>'fiveHourPercent')::int END                      AS five_pct,
         CASE WHEN jsonb_typeof(o.quota->'weeklyPercent') = 'number'
                   AND o.quota->>'weeklyPercent' !~ '[.eE]'
                   AND (o.quota->>'weeklyPercent')::numeric >= 0
              THEN (o.quota->>'weeklyPercent')::int END                        AS weekly_pct,
         CASE WHEN jsonb_typeof(o.quota->'sonnetWeeklyPercent') = 'number'
                   AND o.quota->>'sonnetWeeklyPercent' !~ '[.eE]'
                   AND (o.quota->>'sonnetWeeklyPercent')::numeric >= 0
              THEN (o.quota->>'sonnetWeeklyPercent')::int END                  AS sonnet_pct,
         CASE WHEN jsonb_typeof(o.quota->'overQuotaSpendPercent') = 'number'
                   AND o.quota->>'overQuotaSpendPercent' !~ '[.eE]'
                   AND (o.quota->>'overQuotaSpendPercent')::numeric >= 0
              THEN (o.quota->>'overQuotaSpendPercent')::int END                AS over_pct,
         ((o.quota->>'status' = 'auth_rejected') IS TRUE)                      AS rejected
    FROM account_observations o
    JOIN nodes n ON n.id = o.node_id
),
-- Every row entitled to speak about USAGE: it published a quota block and it is not the account's
-- rejection. A rejected block carries only NULLs anyway; excluding it here is what stops one node's dead
-- token erasing what the others measured.
contrib AS (
  SELECT r.*,
         (r.five_pct IS NOT NULL OR r.weekly_pct IS NOT NULL OR r.over_pct IS NOT NULL) AS has_percent
    FROM ranked r
   WHERE r.quota IS NOT NULL AND NOT r.rejected
),
five_win AS (
  SELECT DISTINCT ON (c.slug) c.slug, c.five_pct AS pct,
         c.quota->>'fiveHourResetsAt' AS resets_at
    FROM contrib c
   WHERE c.five_pct IS NOT NULL
   ORDER BY c.slug, c.five_pct DESC, (c.quota->>'observedAt') DESC NULLS LAST, c.node_name
),
weekly_win AS (
  SELECT DISTINCT ON (c.slug) c.slug, c.weekly_pct AS pct,
         c.quota->>'weeklyResetsAt' AS resets_at
    FROM contrib c
   WHERE c.weekly_pct IS NOT NULL
   ORDER BY c.slug, c.weekly_pct DESC, (c.quota->>'observedAt') DESC NULLS LAST, c.node_name
),
sonnet_win AS (
  SELECT DISTINCT ON (c.slug) c.slug, c.sonnet_pct AS pct,
         c.quota->>'sonnetWeeklyResetsAt' AS resets_at
    FROM contrib c
   WHERE c.sonnet_pct IS NOT NULL
   ORDER BY c.slug, c.sonnet_pct DESC, (c.quota->>'observedAt') DESC NULLS LAST, c.node_name
),
-- The block-level verdict and stamp are NOT per-field, so they keep the pick they always had: a real
-- reading out-ranks a verdict-only block (`has_percent`), then the freshest, then node order. Dating the
-- block by its freshest contributor is what the previous single-block pick did, so the staleness bound
-- `account_fold.quota_is_stale` enforces downstream is untouched by this change.
block AS (
  SELECT DISTINCT ON (c.slug) c.slug, c.quota->>'status' AS status,
         c.quota->>'observedAt' AS observed_at_txt
    FROM contrib c
   ORDER BY c.slug, c.has_percent DESC, (c.quota->>'observedAt') DESC NULLS LAST, c.node_name
),
stamped AS (
  SELECT c.slug, max(c.quota->>'observedAt') AS observed_at_txt
    FROM contrib c
   WHERE c.quota->>'observedAt' IS NOT NULL
   GROUP BY c.slug
),
over_max AS (
  SELECT c.slug, max(c.over_pct) AS pct FROM contrib c GROUP BY c.slug
),
extras AS (
  SELECT slug, jsonb_object_agg(k, v) AS extra
    FROM (
      SELECT DISTINCT ON (r.slug, e.key) r.slug, e.key AS k, e.value AS v
        FROM ranked r, jsonb_each(r.extra) AS e(key, value)
       WHERE e.value <> 'null'::jsonb
       ORDER BY r.slug, e.key, r.node_name
    ) first_per_key
   GROUP BY slug
),
folded AS (
  SELECT
    r.slug,
    COALESCE(
      (array_agg(r.config_dir ORDER BY r.node_name) FILTER (WHERE r.config_dir IS NOT NULL))[1],
      r.slug
    )                                                                          AS config_dir,
    COALESCE(
      (array_agg(r.enabled ORDER BY r.node_name) FILTER (WHERE r.enabled IS NOT NULL))[1],
      true
    )                                                                          AS enabled,
    (array_agg(r.email ORDER BY r.node_name) FILTER (WHERE r.email IS NOT NULL))[1]
                                                                               AS email,
    (array_agg(r.display_name ORDER BY r.node_name)
       FILTER (WHERE r.display_name IS NOT NULL))[1]                           AS display_name,
    bool_or(r.rejected)                                                        AS auth_rejected,
    max(r.observed_at)                                                         AS observed_at
  FROM ranked r
 GROUP BY r.slug
)
SELECT f.slug, f.config_dir, f.enabled, f.email, f.display_name,
       COALESCE(x.extra, '{}'::jsonb) AS extra,
       f.auth_rejected,
       -- NULL when no node had anything to say about quota at all (nothing observed, or every node
       -- rejected). Otherwise a full, stable key set: an explicit null reads as "no figure for this
       -- window", which every consumer already types as `number | null`.
       CASE WHEN b.slug IS NULL THEN NULL
            ELSE jsonb_build_object(
                   'fiveHourPercent',       fw.pct,
                   'fiveHourResetsAt',      fw.resets_at,
                   'weeklyPercent',         ww.pct,
                   'weeklyResetsAt',        ww.resets_at,
                   'sonnetWeeklyPercent',   sw.pct,
                   'sonnetWeeklyResetsAt',  sw.resets_at,
                   'overQuotaSpendPercent', om.pct,
                   'observedAt',            st.observed_at_txt,
                   'status',                b.status
                 )
       END                              AS quota,
       f.observed_at
  FROM folded f
  LEFT JOIN extras     x  ON x.slug  = f.slug
  LEFT JOIN block      b  ON b.slug  = f.slug
  LEFT JOIN five_win   fw ON fw.slug = f.slug
  LEFT JOIN weekly_win ww ON ww.slug = f.slug
  LEFT JOIN sonnet_win sw ON sw.slug = f.slug
  LEFT JOIN over_max   om ON om.slug = f.slug
  LEFT JOIN stamped    st ON st.slug = f.slug
"""


# Postgres derives a sequence name and (since 17) a NOT NULL constraint name from the table/column name
# AT CREATE TIME and revisits neither on a rename, so a migrated database carries the ORIGINAL spelling
# where a rebuilt one would get the current one — and `nextval(...)` / `RENAME CONSTRAINT` resolve against
# the name, so the two must agree. `runners` was `workers` until 0004; `tasks.is_plannable` was
# `is_decomposable` until 0122. Carried from the 0131 baseline VERBATIM AND UNEXTENDED: no revision in
# 0132..0349 renames a table or a column, so this is still the whole set. `runners.node_id`, `runners.org_id`
# and `tasks.org_id` need no rename — each became NOT NULL after the rename, so a fresh `SET NOT NULL`
# already produces the migrated spelling — and the primary key is named explicitly by `create_table`.
_HISTORICALLY_NAMED_NOT_NULLS = ("id", "cwd", "status", "last_seen", "ingested_at")

# The foreign-key cycles in this schema — `admin_users` with `credentials`, `identities` and
# `org_invitations` (actor columns one way, the git credential, the person and the invitation the other),
# `credentials`/`projects` (a credential's home project, a project's Jira credential — 202610222217),
# `crons`/`tasks` (each records which of the other created it) and `support_conversations`/`tickets` — so
# one end of each cannot be inline in its `create_table`. These are those ends, added once every table
# exists.
_DEFERRED_FKS = (
    ("fk_admin_users_git_credential_id", "admin_users", "credentials", "git_credential_id", "SET NULL"),
    ("credentials_home_project_id_fkey", "credentials", "projects", "home_project_id", "SET NULL"),
    ("fk_admin_users_identity_id", "admin_users", "identities", "identity_id", "RESTRICT"),
    ("fk_admin_users_invitation_id", "admin_users", "org_invitations", "invitation_id", "SET NULL"),
    ("automations_created_by_task_id_fkey", "automations", "tasks", "created_by_task_id", "SET NULL"),
    ("fk_crons_created_by_task_id", "crons", "tasks", "created_by_task_id", "SET NULL"),
    ("fk_support_conversations_ticket_id", "support_conversations", "tickets", "ticket_id", "SET NULL"),
    ("fk_tasks_created_by_trigger_id", "tasks", "task_triggers", "created_by_trigger_id", "SET NULL"),
)


def _restore_the_names_the_rename_left_behind() -> None:
    op.execute("ALTER SEQUENCE runners_id_seq RENAME TO workers_id_seq")
    for column in _HISTORICALLY_NAMED_NOT_NULLS:
        op.execute(
            f"ALTER TABLE runners RENAME CONSTRAINT runners_{column}_not_null "
            f"TO workers_{column}_not_null"
        )
    op.execute(
        "ALTER TABLE tasks RENAME CONSTRAINT tasks_is_plannable_not_null "
        "TO tasks_is_decomposable_not_null"
    )


def _install_functions_and_triggers() -> None:
    """The functions, triggers and view reflection cannot see.

    The shared functions' bodies and every census-driven trigger's DDL are IMPORTED from the module that
    owns them, as the collapsed revisions imported them; the censuses those triggers attach by are frozen
    above."""
    from app.entity_shape import SET_UPDATED_AT_FUNCTION_SQL, set_updated_at_trigger_sql
    from app.task_sections import CLEAR_STOP_RECORD_FUNCTION_SQL, CLEAR_STOP_RECORD_TRIGGER_SQL
    from app.tenancy.inherit_trigger import ORG_INHERIT_FUNCTION_SQL, org_inherit_trigger_sql
    from app.tenancy.node_org import (
        NODE_ORG_IMMUTABLE_FUNCTION_SQL,
        NODE_ORG_IMMUTABLE_TRIGGER_ALWAYS_SQL,
        NODE_ORG_IMMUTABLE_TRIGGER_SQL,
    )
    from app.tenancy.org_delete import (
        ORG_DELETE_GUARD_FUNCTION_SQL,
        ORG_DELETE_GUARD_TRIGGER_ALWAYS_SQL,
        ORG_DELETE_GUARD_TRIGGER_SQL,
    )

    op.execute(SET_UPDATED_AT_FUNCTION_SQL)
    for table in _ENTITY_TABLES:
        op.execute(set_updated_at_trigger_sql(table))
    for table, columns in _NARROWED_ENTITY_TRIGGERS:
        op.execute(set_updated_at_trigger_sql(table, columns))

    op.execute(ORG_INHERIT_FUNCTION_SQL)
    for table, pairs in _ORG_INHERIT.items():
        op.execute(org_inherit_trigger_sql(table, pairs))

    # `ENABLE ALWAYS` is the second statement of each guard and is invisible to a naive copy of the
    # `CREATE TRIGGER`: without it the trigger is skipped under `session_replication_role = replica`,
    # which is exactly the setting a restore runs under.
    op.execute(NODE_ORG_IMMUTABLE_FUNCTION_SQL)
    op.execute(NODE_ORG_IMMUTABLE_TRIGGER_SQL)
    op.execute(NODE_ORG_IMMUTABLE_TRIGGER_ALWAYS_SQL)
    op.execute(ORG_DELETE_GUARD_FUNCTION_SQL)
    op.execute(ORG_DELETE_GUARD_TRIGGER_SQL)
    op.execute(ORG_DELETE_GUARD_TRIGGER_ALWAYS_SQL)

    op.execute(CLEAR_STOP_RECORD_FUNCTION_SQL)
    op.execute(CLEAR_STOP_RECORD_TRIGGER_SQL)
    for function_sql in (
        _RESOLVE_FEATURE_FUNCTION_SQL,
        _CLEAR_EMAIL_PROOF_FUNCTION_SQL,
        _TOKEN_LEDGER_IMMUTABLE_FUNCTION_SQL,
        _GOV_LEDGER_IMMUTABLE_FUNCTION_SQL,
        _EMAIL_PROOF_FUNCTION,
        _ATTACH_FUNCTION,
        _SYNC_FUNCTION,
        _FILL_FUNCTION_SQL,
        _KEY_FUNCTION_SQL,
        _ORPHAN_FUNCTION_SQL,
        _REAP_FUNCTION_SQL,
        _AUTOMATION_ONE_CHILD_FUNCTION_SQL,
        _AUTOMATION_CHECK_FUNCTION_SQL,
        _AUTOMATION_DROP_CHILDLESS_FUNCTION_SQL,
    ):
        op.execute(function_sql)
    for trigger_sql in _TRIGGERS_SQL:
        op.execute(trigger_sql)

    op.execute(_ACCOUNT_FOLD_VIEW)


def _census_keys(census):
    """The frozen `(table, column, parent, on_delete)` census rows as the `CompositeKey`s `add_sql` renders."""
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
        for table, column, parent, on_delete in census
    ]


def _install_composite_org_keys() -> None:
    """The `(parent_id, org_id) REFERENCES parent(id, org_id)` keys: one per derivation path, plus the same-org keys.

    NOT DECLARED IN `Base.metadata` and so not rendered by `create_table` above — `migrations/env.py`'s
    `include_object` hides them from autogenerate for the reason stated there. `app/tenancy/keys.py` is
    their single home; this installs what it generated when the baseline was cut. Their parent targets are
    the `UNIQUE (id, org_id)` constraints `create_table` already rendered.

    ADDED `NOT VALID` THEN VALIDATED, in two statements, even though no row can violate a key on a table
    just created: the pair is what `add_sql`/`validate_sql` spell, and a database built here must be
    catalogue-identical to a migrated one, `convalidated` included."""
    from app.tenancy.keys import add_sql, validate_sql

    for key in _census_keys(_COMPOSITE_KEY_CENSUS) + _census_keys(_SAME_ORG_KEY_CENSUS):
        op.execute(add_sql(key))
        op.execute(validate_sql(key))
    for table in _IDENTITY_RECORD_TABLES:
        op.create_foreign_key(
            f"fk_{table}_user_id_org", table, "admin_users", ["user_id", "org_id"], ["id", "org_id"],
            ondelete="SET NULL",
        )


def _seed(conn) -> None:
    """The row-level state a fresh database is expected to come up with. Idempotent throughout, exactly
    as each of the collapsed revisions was.

    THE ORG PLACEMENT IS WRITTEN IN, NOT REPLAYED. A fresh database holds one org, the operator's, so every
    org-scoped seed row below names `ORG_ZERO_ID`. The chain placed the pre-boundary rows in a tenant
    (module docstring); a fresh deployment has no such tenant.

    THE ORDER IS FK-LOAD-BEARING. The system actor goes FIRST — every entity table's
    `created_by_id`/`updated_by_id` defaults to that row and RESTRICTs against `admin_users`, so seeding
    anything before it fails the key. `orgs` follows, because its own actor columns point back at it;
    `admin_users.org_id` carries no foreign key of its own, which is what keeps that pair from being a
    cycle."""
    # Deferred, not style: a module-scope app import breaks the bare-host `alembic heads` gate.
    from app.access_catalog import ALL_KEYS, ROLES
    from app.authz.catalog import PERMISSION_CATALOG
    from app.entity_shape import (
        SYSTEM_ACTOR_DESCRIPTION,
        SYSTEM_ACTOR_EMAIL,
        SYSTEM_ACTOR_ID,
        SYSTEM_ACTOR_NAME,
    )
    from app.model_prices import PRICE_COLUMNS, SEED_PRICES, TOKEN_CLASSES
    from app.playbook_built_ins import BUILT_INS
    from app.port_grid import SEED_RANGE_STARTS
    from app.tenancy.exclusion_log import LOG_STARTED_AT_KEY
    from app.tenancy.ids import OPERATOR_ORG, ORG_ZERO_ID

    org_zero = str(ORG_ZERO_ID)

    # FIRST — see the docstring. The row attributes to itself, which is why the insert names both actor
    # columns explicitly. It lives in org zero, the control plane's own org (0406 moved it there on a
    # migrated database). Its person (0360's backfill: the membership's own id, no display
    # preferences) goes in the SAME statement: each row's key names the other, and a statement checks its
    # keys only once it has written both. The label is `first_name` on both rows: 202610141600 moved every
    # person's display name into the name pair and dropped the `name` column.
    conn.execute(
        text(
            "WITH person AS ("
            "INSERT INTO identities (id, first_name, email, created_by_id, updated_by_id) "
            "VALUES (CAST(:id AS uuid), :name, :email, CAST(:id AS uuid), CAST(:id AS uuid)) "
            "ON CONFLICT (id) DO NOTHING) "
            "INSERT INTO admin_users "
            "(id, email, first_name, description, is_system, role, is_superadmin, created_by_id, "
            "updated_by_id, org_id, preferences, identity_id) "
            "VALUES (CAST(:id AS uuid), :email, :name, :description, true, 'admin', "
            "false, CAST(:id AS uuid), CAST(:id AS uuid), CAST(:org AS uuid), "
            "CAST(:preferences AS jsonb), CAST(:id AS uuid)) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": str(SYSTEM_ACTOR_ID),
            "email": SYSTEM_ACTOR_EMAIL,
            "name": SYSTEM_ACTOR_NAME,
            "description": SYSTEM_ACTOR_DESCRIPTION,
            "org": org_zero,
            # 0141's rewrite, folded in — see the header. The KEY and the VALUE are the two the
            # revision wrote; `app/preferences.py` owns what the column means and defaults to, and it
            # defaults this one to `"current"`, which is precisely the difference being closed here.
            "preferences": '{"new_task_project": "focused"}',
        },
    )

    # The operator org, as the chain's later UPDATEs left it: every cap 0 — unlimited — where NULL would
    # mean the self-service platform default (0299, 0347), except the Agents cap, which 202610221300 made
    # NULL with the Agents override pinned at 0, and the GitHub Runners cap, which 202610201000 raised to its
    # effective ceiling (0 became "off");
    # both isolation exceptions granted (0318), since a new tenant org is isolated by default. It sits on the
    # Trial plan with its live end and its products overridden to none (0374: no trial node, no bundled node
    # later), and its providers overridden to Claude by subscription or key (202610251500: the operator plane
    # runs its own fleet on its own logins). Every other org is a tenant created from the console; a fresh
    # database has none.
    for org in (OPERATOR_ORG,):
        conn.execute(
            text(
                "INSERT INTO orgs (id, slug, name, description, limit_runner_slots, limit_nodes, "
                "limit_projects, limit_users, limit_active_worktrees, limit_github_runners, "
                "colocation_allowed, shared_store_allowed, plan_key, plan_end_overridden, products_overridden, "
                "max_agents_override, providers_override) "
                "VALUES (CAST(:id AS uuid), :slug, :name, :description, NULL, 0, 0, 0, 0, 100000, true, true, "
                "'trial', true, true, 0, CAST('[[\"claude\", \"oauth\"], [\"claude\", \"api_key\"]]' AS jsonb)) "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(org.id), "slug": org.slug, "name": org.name, "description": org.description},
        )

    # ONE `app_settings` ROW PER ORG (0187): the operator org's. The id is explicit and `setval` moves the
    # sequence past it, so a console-created org's row never collides.
    for settings_id, org_id in ((1, org_zero),):
        conn.execute(
            text(
                "INSERT INTO app_settings (id, org_id) VALUES (:id, CAST(:org AS uuid)) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": settings_id, "org": org_id},
        )
    conn.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('app_settings', 'id'), "
            "(SELECT max(id) FROM app_settings))"
        )
    )

    # A group's name is unique per org, case-blind (202610190710 made room for SCIM-managed groups).
    for group_id, name, description in _GROUPS:
        conn.execute(
            text(
                "INSERT INTO groups (id, name, description, org_id) "
                "VALUES (CAST(:id AS uuid), :name, :description, CAST(:org AS uuid)) "
                "ON CONFLICT (org_id, lower(name)) DO NOTHING"
            ),
            {"id": group_id, "name": name, "description": description, "org": org_zero},
        )

    # The operator org's Everyone group (0287). A console-created org gets its own from
    # `access.everyone_group`.
    for org_id, name in ((org_zero, "Everyone"),):
        conn.execute(
            text(
                "INSERT INTO groups (name, description, org_id, is_everyone) "
                "VALUES (:name, 'Every user of the organization.', CAST(:org AS uuid), true) "
                "ON CONFLICT (org_id, lower(name)) DO NOTHING"
            ),
            {"name": name, "org": org_id},
        )

    # `permissions` is not in the tenancy census and carries no `org_id` — the catalogue is the same
    # vocabulary for every tenant.
    for spec in PERMISSION_CATALOG:
        conn.execute(
            text(
                "INSERT INTO permissions (key, description) VALUES (:key, :description) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": spec.key, "description": spec.description},
        )

    # No `group_permissions` and no `user_permissions` rows. Not an omission: a seeded edge is an
    # unreviewed live grant, so 0095 left the table empty and 0242's carry-across — which grants the
    # org-directory keys to whoever already held `system:*` — selects from rows that do not exist here.
    # And no ids: `permissions.id` is a sequence value and 0095 seeded from the LIVE catalog, so what a
    # chain assigned depended on WHEN it ran (main CP holds `credentials:*` at 35..38, a replay today at
    # 19..22). `key` is the stable identity every reader joins on.

    # The built-in roles and their keys (0286). An `ALL_KEYS` role takes every catalogue row; a named key
    # the catalogue lacks refuses the upgrade rather than seeding a role that grants less than it says.
    for role in ROLES:
        conn.execute(
            text(
                "INSERT INTO roles (id, slug, name, description, level, rank, builtin) "
                "VALUES (:id, :slug, :name, :description, :level, :rank, true) ON CONFLICT DO NOTHING"
            ),
            {
                "id": role.id, "slug": role.slug, "name": role.name,
                "description": role.description, "level": role.level, "rank": role.rank,
            },
        )
        keys = None if role.keys is ALL_KEYS else sorted(role.keys)
        found = conn.execute(
            text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT :id, id FROM permissions WHERE CAST(:keys AS text[]) IS NULL OR key = ANY(CAST(:keys AS text[])) "
                "ON CONFLICT DO NOTHING RETURNING permission_id"
            ),
            {"id": role.id, "keys": keys},
        ).fetchall()
        if keys is not None and len(found) != len(keys):
            raise RuntimeError(
                f"role {role.slug!r} names {len(keys)} permission keys but the permissions table holds "
                f"only {len(found)} of them — refusing to seed a role that grants less than it says"
            )

    for built_in in BUILT_INS:
        conn.execute(
            text(
                "INSERT INTO built_in_playbooks "
                "(id, name, description, entry, full_auto_default, default_seniority, stages) "
                "VALUES (:id, :name, :description, :entry, :full_auto_default, :default_seniority, "
                "CAST(:stages AS jsonb)) ON CONFLICT DO NOTHING"
            ),
            {
                "id": built_in.id,
                "name": built_in.name,
                "description": built_in.description,
                "entry": built_in.entry,
                "full_auto_default": built_in.full_auto_default,
                "default_seniority": built_in.default_seniority,
                "stages": json.dumps(list(built_in.stages)),
            },
        )

    conn.execute(
        text(
            "INSERT INTO rental_prices (kind, key, name, monthly_cents, active, sort_order, vcpu, ram_gb, disk_gb) "
            "VALUES (:kind, :key, :name, :monthly_cents, :active, :sort_order, :vcpu, :ram_gb, "
            # A Shared tier's disk cap (0419): the `vnode_disk_cap_gb` knob's default; no other kind has one.
            "CASE WHEN :kind = 'shared' THEN 20 END) "
            "ON CONFLICT (kind, key) DO NOTHING"
        ),
        [
            dict(zip(("kind", "key", "name", "monthly_cents", "active", "sort_order", "vcpu", "ram_gb"), row))
            for row in _RENTAL_PRICES
        ],
    )

    # Billing's singleton state row and the plan catalogue (`_BILLING_PLANS`); `billing_*` carries no org.
    # Business trial is the default plan a new org takes (`operator_settings.default_plan_key`, 202610181200).
    conn.execute(text("INSERT INTO billing_state (id) VALUES (1) ON CONFLICT DO NOTHING"))
    seed_billing_plans(conn)
    conn.execute(
        text(
            "INSERT INTO system_state (key, value) VALUES ('operator_settings', CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {"value": json.dumps({"default_plan_key": _DEFAULT_PLAN_KEY})},
    )
    # The operator org's plan-terms snapshot, as 202610171800 took it for every org then on the books: the
    # Trial's caps (all unlimited on the operator org) and switches as that revision read them, stamped with
    # its own revision id. The plan engine reads it as the inheritance baseline, so a fresh database carries
    # the row both control planes carry.
    conn.execute(
        text(
            "INSERT INTO plan_terms_snapshots (org_id, plan_key, plan_started_at, caps, switches, revision) "
            "SELECT id, plan_key, plan_started_at, CAST(:caps AS jsonb), CAST(:switches AS jsonb), '202610171800' "
            "FROM orgs WHERE id = CAST(:org AS uuid) "
            "AND NOT EXISTS (SELECT 1 FROM plan_terms_snapshots WHERE org_id = CAST(:org AS uuid))"
        ),
        {
            "org": org_zero,
            "caps": json.dumps({
                "nodes": None, "users": None, "projects": None, "runner_slots": None, "github_runners": None,
                "active_worktrees": None,
            }),
            "switches": json.dumps({
                "agents_enabled": False, "hosting_allowed": True, "microvm_allowed": True, "nodelink_allowed": False,
                "nodevault_allowed": False, "playbooks_enabled": False, "ci_runners_allowed": True,
                "compliance_enabled": False, "merge_lane_enabled": False, "macos_nodes_allowed": False,
                "ubuntu_nodes_allowed": True, "windows_nodes_allowed": False, "project_memory_enabled": True,
                "non_claude_engines_allowed": False, "advanced_compliance_enabled": False,
            }),
        },
    )

    for name, range_start, description in SEED_RANGE_STARTS:
        conn.execute(
            text(
                "INSERT INTO port_grid_vars (name, range_start, description, org_id) "
                "VALUES (:name, :range_start, :description, CAST(:org AS uuid)) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {
                "name": name,
                "range_start": range_start,
                "description": description,
                "org": org_zero,
            },
        )

    for group_name, group_description, multi_select, group_position, tags in _TAG_SEED:
        group_id = conn.execute(
            text(
                "INSERT INTO tag_groups (name, description, state, multi_select, position, org_id) "
                "VALUES (:name, :description, 'draft', :multi_select, :position, CAST(:org AS uuid)) "
                "ON CONFLICT (org_id, name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            ),
            {
                "name": group_name,
                "description": group_description,
                "multi_select": multi_select,
                "position": group_position,
                "org": org_zero,
            },
        ).scalar_one()
        for tag_code, tag_name, tag_position in tags:
            conn.execute(
                text(
                    "INSERT INTO tags (tag_group_id, tag, name, position, org_id) "
                    "VALUES (CAST(:tag_group_id AS uuid), :tag, :name, :position, CAST(:org AS uuid)) "
                    "ON CONFLICT (tag_group_id, tag) DO NOTHING"
                ),
                {
                    "tag_group_id": str(group_id),
                    "tag": tag_code,
                    "name": tag_name,
                    "position": tag_position,
                    "org": org_zero,
                },
            )

    # THE COLUMN LIST IS DERIVED FROM `PRICE_COLUMNS`, not typed out (0139's argument): `SEED_PRICES`'
    # tuple order is `(name, description, *TOKEN_CLASSES)`, and that class -> column mapping already has a
    # home. A swapped pair — a 1h cache write priced at the 5m rate — is a silent 1.6x error, not a
    # failure. `model_prices` is `Shared` in the census and so carries no `org_id`.
    price_columns = ["name", "description", *(PRICE_COLUMNS[cls] for cls in TOKEN_CLASSES)]
    price_values = ", ".join(
        f":{column}" if column in ("name", "description") else f"CAST(:{column} AS numeric)"
        for column in price_columns
    )
    price_insert = text(
        f"INSERT INTO model_prices ({', '.join(price_columns)}) VALUES ({price_values}) "
        f"ON CONFLICT (name) DO NOTHING"
    )
    for row in SEED_PRICES:
        # `strict=True`: a `SEED_PRICES` tuple that gained or lost a field must fail here rather than
        # have `zip` silently drop the tail and insert a row missing its output rate.
        conn.execute(price_insert, dict(zip(price_columns, row, strict=True)))

    for (
        name,
        activity_type,
        schedule_kind,
        cron_expression,
        interval_seconds,
        enabled,
        retention_mode,
        min_gap_seconds,
        timeout_seconds,
        description,
    ) in _SYSTEM_CRONS:
        conn.execute(
            text(
                "INSERT INTO crons (name, description, schedule_kind, cron_expression, "
                "interval_seconds, activity_type, enabled, retention_mode, min_gap_seconds, "
                "timeout_seconds, is_system, org_id) "
                "VALUES (:name, :description, :schedule_kind, :cron_expression, :interval_seconds, "
                ":activity_type, :enabled, :retention_mode, :min_gap_seconds, :timeout_seconds, "
                "true, CAST(:org AS uuid)) "
                "ON CONFLICT (activity_type) WHERE is_system DO NOTHING"
            ),
            {
                "name": name,
                "description": description,
                "schedule_kind": schedule_kind,
                "cron_expression": cron_expression,
                "interval_seconds": interval_seconds,
                "activity_type": activity_type,
                "enabled": enabled,
                "retention_mode": retention_mode,
                "min_gap_seconds": min_gap_seconds,
                "timeout_seconds": timeout_seconds,
                "org": org_zero,
            },
        )

    conn.execute(text(_SYSTEM_CRON_PARENTS_SQL))

    conn.execute(
        text(
            "INSERT INTO allowed_instance_types (name, instance_type, position, family) "
            "VALUES (:t, :t, :p, :f) ON CONFLICT (provider, instance_type) DO NOTHING"
        ),
        [
            {"t": instance_type, "p": position, "f": instance_type.split(".", 1)[0]}
            for position, instance_type in enumerate(_ALLOWED_INSTANCE_TYPES)
        ],
    )

    # The exclusion log's own epoch (0180). `now()` rather than a frozen literal: the value means "from
    # when has this log been counting", and freezing it would claim a coverage window this database never
    # had. `app/tenancy/exclusion_log.py` is the single home for what the marker is read for.
    conn.execute(
        text(
            "INSERT INTO system_state (key, value) "
            "VALUES (:key, jsonb_build_object('at', to_json(now()))) "
            "ON CONFLICT (key) DO NOTHING"
        ).bindparams(key=LOG_STARTED_AT_KEY)
    )

    # POST-CONDITIONS, carried from 0114/0123 and extended to the seeds this collapse absorbs — the seed
    # is what makes those revisions useful, and a seed that silently inserted nothing (a botched conflict
    # clause, a truncated constant) ships an empty port grid (every var reads as tier 2, every live stack
    # re-banded off its port at the next lease), an empty tag catalogue, an unpriced fleet, or a control
    # plane with no scheduled work at all. Every one of those is invisible until something downstream
    # renders.
    for table, expected in (
        ("port_grid_vars", len(SEED_RANGE_STARTS)),
        ("tags", sum(len(group[4]) for group in _TAG_SEED)),
        ("model_prices", len(SEED_PRICES)),
        ("crons", len(_SYSTEM_CRONS)),
        ("orgs", 1),
        ("permissions", len(PERMISSION_CATALOG)),
        ("roles", len(ROLES)),
        ("built_in_playbooks", len(BUILT_INS)),
        ("rental_prices", len(_RENTAL_PRICES)),
        ("automations", len(_SYSTEM_CRONS)),
        ("billing_plans", len(_BILLING_PLANS)),
        ("allowed_instance_types", len(_ALLOWED_INSTANCE_TYPES)),
    ):
        landed = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        if landed < expected:
            raise RuntimeError(f"{table} seed landed {landed} rows, expected at least {expected}")


def upgrade() -> None:
    """Create the whole schema, then everything reflection could not describe, then the seeds."""
    conn = op.get_bind()
    # pgvector, for `support_kb_chunks.embedding`: on the server in RDS Postgres 18 and in the
    # `pgvector/pgvector` image local and CI run. The local edition runs plain Postgres: no extension, and no
    # vector column or index below (`migrations/_edition.py`).
    if vector():
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # pg_trgm, for the trigram search indexes (202610141500): a trusted extension, created by the migrating role
    # like `vector`, on every edition.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── The tables and indexes, as autogenerate rendered them from the migrated database ────────────
    op.create_table('activity_events',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('actor_label', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('action_type', sa.VARCHAR(length=8), autoincrement=False, nullable=False),
    sa.Column('kind', sa.VARCHAR(length=48), autoincrement=False, nullable=False),
    sa.Column('summary', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('path', sa.VARCHAR(length=512), autoincrement=False, nullable=True),
    sa.Column('method', sa.VARCHAR(length=8), autoincrement=False, nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('status', sa.SMALLINT(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='activity_events_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_activity_events_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_activity_events_actor_newest', 'activity_events', ['org_id', 'user_id', sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_activity_events_browse', 'activity_events', ['action_type', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('ix_activity_events_created_at', 'activity_events', ['created_at'], unique=False)
    op.create_index('ix_activity_events_data_id', 'activity_events', [sa.literal_column("(data ->> 'id'::text)")], unique=False)
    op.create_index('ix_activity_events_data_path', 'activity_events', ['data'], unique=False, postgresql_ops={'data': 'jsonb_path_ops'}, postgresql_using='gin')
    op.create_index('ix_activity_events_dedup', 'activity_events', ['user_id', 'action_type', 'path', 'created_at'], unique=False)
    op.create_index('ix_activity_events_kind_pattern', 'activity_events', ['kind'], unique=False, postgresql_ops={'kind': 'varchar_pattern_ops'})
    op.create_index('ix_activity_events_org_newest', 'activity_events', ['org_id', sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_activity_events_refusal_identifier_trgm', 'activity_events', [sa.literal_column("(data ->> 'identifier'::text)")], unique=False, postgresql_ops={"(data ->> 'identifier'::text)": 'gin_trgm_ops'}, postgresql_using='gin', postgresql_where="((kind)::text = 'login_failed'::text)")
    op.create_index('ix_activity_events_search_trgm', 'activity_events', [sa.literal_column("((((((COALESCE(summary, ''::text) || ' '::text) || COALESCE(actor_label, ''::character varying)::text) || ' '::text) || COALESCE(path, ''::character varying)::text) || ' '::text) || kind::text)")], unique=False, postgresql_ops={"((((((COALESCE(summary, ''::text) || ' '::text) || COALESCE(actor_label, ''::character varying)::text) || ' '::text) || COALESCE(path, ''::character varying)::text) || ' '::text) || kind::text)": 'gin_trgm_ops'}, postgresql_using='gin')
    op.create_index('ix_activity_events_user_id', 'activity_events', ['user_id'], unique=False)
    op.create_table('admin_users',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('email', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.Column('password_hash', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('role', sa.VARCHAR(length=16), server_default=sa.text("'admin'::character varying"), autoincrement=False, nullable=False),
    sa.Column('first_name', sa.VARCHAR(length=120), autoincrement=False, nullable=True),
    sa.Column('last_name', sa.VARCHAR(length=120), autoincrement=False, nullable=True),
    sa.Column('activated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('preferences', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('session_epoch', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('last_seen_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('google_sub', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('is_superadmin', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('is_system', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('git_credential_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('github_user_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('microsoft_tenant_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('microsoft_object_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('email_verified_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('slack_link_code_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('slack_link_code_expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('disabled_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('disabled_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('identity_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('invitation_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('password_login', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('mfa_exempt', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('scim_external_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('sso_protocol', sa.VARCHAR(length=8), autoincrement=False, nullable=True),
    sa.Column('sso_issuer', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('sso_subject', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint('(microsoft_tenant_id IS NULL) = (microsoft_object_id IS NULL)', name='ck_admin_users_microsoft_identity_pair'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_admin_users_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['disabled_by_id'], ['admin_users.id'], name='fk_admin_users_disabled_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_admin_users_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='admin_users_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_admin_users_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('identity_id', 'org_id', name='uq_admin_users_identity_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_admin_users_last_seen_at', 'admin_users', ['last_seen_at'], unique=False)
    op.create_index('uq_admin_users_one_system', 'admin_users', ['is_system'], unique=True, postgresql_where='is_system')
    op.create_index('uq_admin_users_slack_link_code_hash', 'admin_users', ['slack_link_code_hash'], unique=True, postgresql_where='(slack_link_code_hash IS NOT NULL)')
    op.create_index('uq_admin_users_sso_subject', 'admin_users', ['org_id', 'sso_protocol', 'sso_issuer', 'sso_subject'], unique=True, postgresql_where='(sso_subject IS NOT NULL)')
    op.create_table('app_settings',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('app_title', sa.VARCHAR(length=120), server_default=sa.text("'Jaah'::character varying"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='app_settings_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_app_settings_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', name='uq_app_settings_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('bedrock_inference_profiles',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('model_family', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('source_model_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('profile_arn', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_bedrock_inference_profiles'),
    sa.UniqueConstraint('id', 'org_id', name='uq_bedrock_inference_profiles_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'model_family', name='uq_bedrock_inference_profiles_org_family', postgresql_nulls_not_distinct=False)
    )
    op.create_table('bedrock_reconciliations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('day', sa.DATE(), autoincrement=False, nullable=False),
    sa.Column('jaah_cost_micros', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('aws_cost_micros', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('tolerance_bps', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('over_tolerance', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_bedrock_reconciliations'),
    sa.UniqueConstraint('id', 'org_id', name='uq_bedrock_reconciliations_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'day', name='uq_bedrock_reconciliations_org_day', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_bedrock_reconciliations_day', 'bedrock_reconciliations', ['day'], unique=False)
    op.create_table('bedrock_revocations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_bedrock_revocations'),
    sa.UniqueConstraint('id', 'org_id', name='uq_bedrock_revocations_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', name='uq_bedrock_revocations_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('billing_checkouts',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('plan_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('setup_intent_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('subscription_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'started'::text"), autoincrement=False, nullable=False),
    sa.Column('attempt', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('created_by', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('agents', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('interval', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("state = ANY (ARRAY['started'::text, 'card_saved'::text, 'subscribing'::text, 'subscribed'::text, 'abandoned'::text])", name='ck_billing_checkouts_state'),
    sa.PrimaryKeyConstraint('id', name='pk_billing_checkouts'),
    sa.UniqueConstraint('id', 'org_id', name='uq_billing_checkouts_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_billing_checkouts_org_live', 'billing_checkouts', ['org_id', 'stripe_account_id', 'livemode'], unique=True, postgresql_where="(state = ANY (ARRAY['started'::text, 'card_saved'::text, 'subscribing'::text]))")
    op.create_table('billing_customers',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('stripe_customer_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('deleted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_billing_customers'),
    sa.UniqueConstraint('id', 'org_id', name='uq_billing_customers_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('stripe_customer_id', name='uq_billing_customers_stripe_customer_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_billing_customers_org_account', 'billing_customers', ['org_id', 'stripe_account_id', 'livemode'], unique=True, postgresql_where='(deleted_at IS NULL)')
    op.create_table('billing_events',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('stripe_event_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('object_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('outcome', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('attempts', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('processed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("outcome = ANY (ARRAY['applied'::text, 'ignored'::text, 'rejected'::text, 'error'::text])", name='ck_billing_events_outcome'),
    sa.PrimaryKeyConstraint('id', name='pk_billing_events'),
    sa.UniqueConstraint('stripe_event_id', name='uq_billing_events_stripe_event_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_billing_events_pending', 'billing_events', ['created', 'stripe_event_id'], unique=False, postgresql_where="((outcome IS NULL) OR (outcome = 'error'::text))")
    op.create_table('billing_invoices',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('stripe_invoice_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('number', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('amount_due', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('amount_paid', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('amount_refunded', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('post_payment_credit_notes_amount', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('payment_count', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('currency', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stripe_created', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('period_start', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('period_end', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('hosted_invoice_url', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('invoice_pdf', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('credit_note_pdfs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('next_payment_attempt', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('subscription_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_billing_invoices'),
    sa.UniqueConstraint('id', 'org_id', name='uq_billing_invoices_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('stripe_invoice_id', name='uq_billing_invoices_stripe_invoice_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('billing_notice_reads',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('notice_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('read_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('notice_id', 'user_id', name='pk_billing_notice_reads'),
    sa.UniqueConstraint('id', 'org_id', name='uq_billing_notice_reads_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('billing_notices',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('dedupe_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('subject', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('banner', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('resolved_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('emailed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("kind = ANY (ARRAY['payment.failed'::text, 'payment.action_required'::text, 'refund.issued'::text, 'card.expires_30d'::text, 'card.expires_7d'::text, 'card.expired'::text, 'recharge.failed'::text, 'jaah.low'::text, 'jaah.paused'::text, 'jaah.resumed'::text, 'rental.past_due'::text, 'rental.stopping'::text, 'rental.stopped'::text, 'rental.terminating'::text, 'rental.bundle_ending'::text])", name='ck_billing_notices_kind'),
    sa.PrimaryKeyConstraint('id', name='pk_billing_notices'),
    sa.UniqueConstraint('dedupe_key', name='uq_billing_notices_dedupe_key', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_billing_notices_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_billing_notices_org_open', 'billing_notices', ['org_id', 'created_at'], unique=False, postgresql_where='(resolved_at IS NULL)')
    op.create_table('billing_payment_methods',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('stripe_payment_method_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('brand', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('wallet', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last4', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('exp_month', sa.SMALLINT(), autoincrement=False, nullable=True),
    sa.Column('exp_year', sa.SMALLINT(), autoincrement=False, nullable=True),
    sa.Column('is_default', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('stripe_created', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_charge_failed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('detached_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_billing_payment_methods'),
    sa.UniqueConstraint('id', 'org_id', name='uq_billing_payment_methods_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('stripe_payment_method_id', name='uq_billing_payment_methods_stripe_payment_method_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('billing_previews',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('plan_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('subscription_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('from_price_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('proration_date', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('amount_due', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('currency', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('quantity', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('interval', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('from_quantity', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name='pk_billing_previews'),
    sa.UniqueConstraint('id', 'org_id', name='uq_billing_previews_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('billing_setup_attempts',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('setup_intent_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('failures', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('cancelled_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_billing_setup_attempts'),
    sa.UniqueConstraint('id', 'org_id', name='uq_billing_setup_attempts_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('setup_intent_id', name='uq_billing_setup_attempts_setup_intent_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('billing_state',
    sa.Column('id', sa.SMALLINT(), autoincrement=True, nullable=False),
    sa.Column('account_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('credential_updated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('pinned_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('marker_customer_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('events_cursor', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('last_poll_ok_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_allowed_event_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_poll_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint('id = 1', name='ck_billing_state_singleton'),
    sa.PrimaryKeyConstraint('id', name='pk_billing_state')
    )
    op.create_table('billing_subscriptions',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('stripe_subscription_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('stripe_customer_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('plan_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('plan_item_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stripe_price_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('price_unit_amount', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('quantity', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('cancel_at_period_end', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('current_period_start', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('current_period_end', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('schedule_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('scheduled_plan_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('scheduled_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('pending_update', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('collection_method', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('fetched_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('interval', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('trial_plan_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('trial_end', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name='pk_billing_subscriptions'),
    sa.UniqueConstraint('id', 'org_id', name='uq_billing_subscriptions_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('stripe_subscription_id', name='uq_billing_subscriptions_stripe_subscription_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_billing_subscriptions_org_live', 'billing_subscriptions', ['org_id', 'stripe_account_id', 'livemode'], unique=True, postgresql_where="(status <> ALL (ARRAY['canceled'::text, 'incomplete_expired'::text]))")
    op.create_table('cp_ecs_tasks',
    sa.Column('task_arn', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('task_definition_arn', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('availability_zone', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('private_ip', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last_status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('desired_status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('health_status', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('stopping_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('stopped_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('stopped_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stop_code', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('containers', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('first_seen_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('last_seen_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('deploy_run_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('task_arn', name='pk_cp_ecs_tasks')
    )
    op.create_index('ix_cp_ecs_tasks_last_seen_at', 'cp_ecs_tasks', ['last_seen_at'], unique=False)
    op.create_table('cp_peers',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('address', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('heartbeat_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='cp_peers_pkey')
    )
    op.create_table('credential_id_tombstones',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('deleted_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_credential_id_tombstones')
    )
    op.create_table('customer_events',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('category', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('summary', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('actor_kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('actor_user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('actor_label', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('mode', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('refs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('source_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.CheckConstraint("actor_kind = ANY (ARRAY['user'::text, 'stripe'::text, 'system'::text])", name='ck_customer_events_actor_kind'),
    sa.CheckConstraint("category = ANY (ARRAY['billing'::text, 'products'::text, 'plan'::text, 'access'::text, 'lifecycle'::text])", name='ck_customer_events_category'),
    sa.CheckConstraint("mode IS NULL OR (mode = ANY (ARRAY['with_billing'::text, 'without_billing'::text]))", name='ck_customer_events_mode'),
    sa.PrimaryKeyConstraint('id', name='customer_events_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_customer_events_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('source_key', name='uq_customer_events_source_key', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_customer_events_org_at', 'customer_events', ['org_id', sa.literal_column('at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_customer_events_org_category_at', 'customer_events', ['org_id', 'category', sa.literal_column('at DESC')], unique=False)
    op.create_table('dmarc_reports',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('reporter_domain', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('reporter_org', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('reporter_email', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('report_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('date_begin', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('date_end', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('policy_domain', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('policy', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('inbound_email_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name='pk_dmarc_reports'),
    sa.UniqueConstraint('id', 'org_id', name='uq_dmarc_reports_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('reporter_domain', 'report_id', name='uq_dmarc_reports_reporter_domain_report', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_dmarc_reports_date_end', 'dmarc_reports', [sa.literal_column('date_end DESC')], unique=False)
    op.create_table('email_deliveries',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('mail_type', sa.VARCHAR(length=64), autoincrement=False, nullable=False),
    sa.Column('essential', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('recipient', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('recipient_user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('subject', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('status', sa.VARCHAR(length=16), server_default=sa.text("'queued'::character varying"), autoincrement=False, nullable=False),
    sa.Column('status_detail', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('provider_message_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('attempts', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('sent_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_event_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('events', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.CheckConstraint("status IN ('queued', 'sent', 'delivered', 'delayed', 'bounced', 'complained', 'suppressed', 'failed', 'not_configured')", name='ck_email_deliveries_status'),
    sa.PrimaryKeyConstraint('id', name='pk_email_deliveries'),
    sa.UniqueConstraint('id', 'org_id', name='uq_email_deliveries_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('provider_message_id', name='uq_email_deliveries_provider_message_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_email_deliveries_created_at', 'email_deliveries', ['created_at'], unique=False)
    op.create_table('email_mailboxes',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('address', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('history_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('watch_expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_email_mailboxes'),
    sa.UniqueConstraint('address', name='uq_email_mailboxes_address', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_email_mailboxes_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('email_signups',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('email', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('code_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('code_expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('code_attempts', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('link_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('link_expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('verified_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('password_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last_sent_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('send_count', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('consumed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('profile', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.CheckConstraint('email = lower(email)', name='ck_email_signups_email_lower'),
    sa.PrimaryKeyConstraint('id', name='pk_email_signups'),
    sa.UniqueConstraint('id', 'org_id', name='uq_email_signups_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_email_signups_link_hash', 'email_signups', ['link_hash'], unique=True, postgresql_where='(link_hash IS NOT NULL)')
    op.create_index('uq_email_signups_open_email', 'email_signups', ['org_id', 'email'], unique=True, postgresql_where='(consumed_at IS NULL)')
    op.create_table('email_suppressions',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('address', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('reason', sa.VARCHAR(length=16), autoincrement=False, nullable=False),
    sa.Column('detail', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('source_delivery_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('cleared_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('cleared_by', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint("reason IN ('bounce', 'complaint', 'manual')", name='ck_email_suppressions_reason'),
    sa.PrimaryKeyConstraint('id', name='pk_email_suppressions'),
    sa.UniqueConstraint('id', 'org_id', name='uq_email_suppressions_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_email_suppressions_active_address', 'email_suppressions', ['address'], unique=True, postgresql_where='(cleared_at IS NULL)')
    op.create_table('gov_guard_decisions',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('session_ref', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('runtime', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('moment', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('rule_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('rule_version', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('mode', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('decision', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('path', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('line', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("decision = ANY (ARRAY['denied'::text, 'error'::text, 'waived'::text, 'escalated'::text])", name='ck_gov_guard_decisions_decision'),
    sa.CheckConstraint("moment = ANY (ARRAY['write'::text, 'commit'::text, 'push'::text])", name='ck_gov_guard_decisions_moment'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_guard_decisions'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_guard_decisions_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_gov_guard_decisions_org', 'gov_guard_decisions', ['org_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('ix_gov_guard_decisions_project', 'gov_guard_decisions', ['org_id', 'project_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('ix_gov_guard_decisions_rule', 'gov_guard_decisions', ['org_id', 'pack_id', 'rule_key', sa.literal_column('created_at DESC')], unique=False)
    op.create_table('gov_ledger',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('subject_kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('subject_ref', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('subject_project_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pack_version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('rules_snapshot', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('verdicts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('facts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('oracles', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('tool_version', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('scan_target_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('signature', postgresql.BYTEA(), autoincrement=False, nullable=True),
    sa.Column('sig_key_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('subject_repo_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name='pk_gov_ledger'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_ledger_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_gov_ledger_subject', 'gov_ledger', ['subject_project_id', 'pack_id', 'created_at'], unique=False)
    op.create_index('uq_gov_ledger_scan_target_id', 'gov_ledger', ['scan_target_id'], unique=True, postgresql_where='(scan_target_id IS NOT NULL)')
    op.create_table('gov_rule_oracles',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('rule_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('rule_version', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('ledger_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('checked_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("status = ANY (ARRAY['pass'::text, 'fail'::text])", name='ck_gov_rule_oracles_status'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_rule_oracles'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_rule_oracles_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'pack_id', 'rule_key', 'rule_version', name='uq_gov_rule_oracles_rule_version', postgresql_nulls_not_distinct=False)
    )
    op.create_table('insight_digests',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('week_start', sa.DATE(), autoincrement=False, nullable=False),
    sa.Column('generated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('bullets', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('usage_by_model', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.CheckConstraint("jsonb_typeof(bullets) = 'array'::text AND jsonb_array_length(bullets) <= 10", name='ck_insight_digests_bullets'),
    sa.CheckConstraint("status = ANY (ARRAY['ok'::text, 'failed'::text])", name='ck_insight_digests_status'),
    sa.PrimaryKeyConstraint('id', name='pk_insight_digests'),
    sa.UniqueConstraint('id', 'org_id', name='uq_insight_digests_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('week_start', name='uq_insight_digests_week_start', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_insight_digests_generated_at', 'insight_digests', ['generated_at'], unique=False)
    op.create_table('jaah_sessions',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('agent_session_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('credentials_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('ended_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('end_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("kind = ANY (ARRAY['runner'::text, 'console'::text])", name='ck_jaah_sessions_kind'),
    sa.PrimaryKeyConstraint('id', name='pk_jaah_sessions'),
    sa.UniqueConstraint('id', 'org_id', name='uq_jaah_sessions_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_jaah_sessions_open', 'jaah_sessions', ['started_at'], unique=False, postgresql_where='(ended_at IS NULL)')
    op.create_index('ix_jaah_sessions_task', 'jaah_sessions', ['task_id'], unique=False)
    op.create_table('live_viewer_events',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('visitor_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('tab_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('viewed_org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('device', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('browser', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('country', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("browser = ANY (ARRAY['Chrome'::text, 'Safari'::text, 'Firefox'::text, 'Edge'::text, 'Opera'::text, 'Samsung Internet'::text, 'Other'::text])", name='ck_live_viewer_events_browser'),
    sa.CheckConstraint("device = ANY (ARRAY['desktop'::text, 'mobile'::text, 'tablet'::text, 'other'::text])", name='ck_live_viewer_events_device'),
    sa.CheckConstraint("kind = ANY (ARRAY['page_view'::text, 'action'::text, 'console_click'::text])", name='ck_live_viewer_events_kind'),
    sa.PrimaryKeyConstraint('id', name='pk_live_viewer_events')
    )
    op.create_index('ix_live_viewer_events_at', 'live_viewer_events', ['at'], unique=False)
    op.create_index('ix_live_viewer_events_project_id_at', 'live_viewer_events', ['project_id', 'at'], unique=False)
    op.create_table('live_viewer_minutes',
    sa.Column('minute', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('visitor_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('tab_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('last_seen', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('viewed_org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('device', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('browser', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('country', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("browser = ANY (ARRAY['Chrome'::text, 'Safari'::text, 'Firefox'::text, 'Edge'::text, 'Opera'::text, 'Samsung Internet'::text, 'Other'::text])", name='ck_live_viewer_minutes_browser'),
    sa.CheckConstraint("device = ANY (ARRAY['desktop'::text, 'mobile'::text, 'tablet'::text, 'other'::text])", name='ck_live_viewer_minutes_device'),
    sa.PrimaryKeyConstraint('minute', 'visitor_id', 'tab_id', name='pk_live_viewer_minutes')
    )
    op.create_table('login_failures',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('identifier_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('source_ip', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('failed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='login_failures_pkey')
    )
    op.create_index('ix_login_failures_failed_at', 'login_failures', ['failed_at'], unique=False)
    op.create_index('ix_login_failures_identifier_hash_failed_at', 'login_failures', ['identifier_hash', 'failed_at'], unique=False)
    op.create_table('nodevault_operations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'running'::text"), autoincrement=False, nullable=False),
    sa.Column('step', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('key_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('prev_key_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('counts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('workflow_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("kind = ANY (ARRAY['setup'::text, 'rotation'::text, 'migration'::text, 'retire'::text])", name='ck_nodevault_operations_kind'),
    sa.CheckConstraint("state = ANY (ARRAY['running'::text, 'done'::text, 'failed'::text])", name='ck_nodevault_operations_state'),
    sa.CheckConstraint("step = ANY (ARRAY['distribute'::text, 'promote'::text, 'migrate_choice'::text, 'migrating'::text, 'rekey'::text, 'retiring'::text, 'result'::text])", name='ck_nodevault_operations_step'),
    sa.PrimaryKeyConstraint('id', name='pk_nodevault_operations'),
    sa.UniqueConstraint('id', 'org_id', name='uq_nodevault_operations_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_nodevault_operations_one_running', 'nodevault_operations', ['org_id'], unique=True, postgresql_where="(state = 'running'::text)")
    op.create_table('ops_snapshots',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('taken_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('values', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.CheckConstraint("kind = ANY (ARRAY['node_util'::text, 'worktree_occupancy'::text, 'account_quota'::text, 'wallet_floor'::text])", name='ck_ops_snapshots_kind'),
    sa.PrimaryKeyConstraint('id', name='pk_ops_snapshots'),
    sa.UniqueConstraint('id', 'org_id', name='uq_ops_snapshots_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_ops_snapshots_subject_hour', 'ops_snapshots', ['kind', 'taken_at', 'node_id', 'account_id', 'project_id', 'org_id'], unique=True, postgresql_nulls_not_distinct=True)
    op.create_table('org_consent_hashes',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('hash', sa.VARCHAR(length=64), autoincrement=False, nullable=False),
    sa.Column('consented_at', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('node', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_org_consent_hashes'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_consent_hashes_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', name='uq_org_consent_hashes_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('org_settings',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('key', sa.VARCHAR(length=64), autoincrement=False, nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='org_settings_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_settings_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'key', name='uq_org_settings_org_id_key', postgresql_nulls_not_distinct=False)
    )
    op.create_table('permissions',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('key', sa.VARCHAR(length=64), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name='permissions_pkey'),
    sa.UniqueConstraint('key', name='permissions_key_key', postgresql_nulls_not_distinct=False)
    )
    op.create_table('plan_terms_snapshots',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('plan_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('plan_started_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('plan_end', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('caps', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('switches', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('stripe_price_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('taken_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('revision', sa.TEXT(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_plan_terms_snapshots'),
    sa.UniqueConstraint('id', 'org_id', name='uq_plan_terms_snapshots_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', name='uq_plan_terms_snapshots_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('rate_limit_hits',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('bucket', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('hit_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='rate_limit_hits_pkey')
    )
    op.create_index('ix_rate_limit_hits_bucket_key_hit_at', 'rate_limit_hits', ['bucket', 'key', 'hit_at'], unique=False)
    op.create_table('roles',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('level', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('rank', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('builtin', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.CheckConstraint("level = ANY (ARRAY['org'::text, 'project'::text])", name='ck_roles_level'),
    sa.PrimaryKeyConstraint('id', name='pk_roles'),
    sa.UniqueConstraint('slug', name='uq_roles_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_table('site_chat_conversations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('site', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('page_url', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('locale', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('ip_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('user_agent', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('token_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('token_expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('turn_count', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("site = ANY (ARRAY['docs'::text, 'www'::text])", name='ck_site_chat_conversations_site'),
    sa.PrimaryKeyConstraint('id', name='pk_site_chat_conversations'),
    sa.UniqueConstraint('id', 'org_id', name='uq_site_chat_conversations_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_site_chat_conversations_created_at', 'site_chat_conversations', [sa.literal_column('created_at DESC')], unique=False)
    op.create_table('slack_events',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('event_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='slack_events_pkey'),
    sa.UniqueConstraint('event_id', name='uq_slack_events_event_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_slack_events_created_at', 'slack_events', ['created_at'], unique=False)
    op.create_table('sso_spent_requests',
    sa.Column('request_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('spent_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('request_id', name='pk_sso_spent_requests')
    )
    op.create_index('ix_sso_spent_requests_spent_at', 'sso_spent_requests', ['spent_at'], unique=False)
    op.create_table('support_kb_chunks',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('source', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('path', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('title', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('audience', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('ordinal', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('content_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('text', sa.TEXT(), autoincrement=False, nullable=False),
    *([sa.Column('embedding', _Vector(), autoincrement=False, nullable=False)] if vector() else []),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('url', sa.VARCHAR(length=512), autoincrement=False, nullable=True),
    sa.CheckConstraint("audience = ANY (ARRAY['everyone'::text, 'operator'::text])", name='ck_support_kb_chunks_audience'),
    sa.PrimaryKeyConstraint('id', name='pk_support_kb_chunks'),
    sa.UniqueConstraint('content_hash', name='uq_support_kb_chunks_content_hash', postgresql_nulls_not_distinct=False)
    )
    if vector():
        op.create_index('ix_support_kb_chunks_embedding', 'support_kb_chunks', ['embedding'], unique=False, postgresql_ops={'embedding': 'vector_cosine_ops'}, postgresql_with={'m': '16', 'ef_construction': '64'}, postgresql_using='hnsw')
    op.create_index('ix_support_kb_chunks_source_path', 'support_kb_chunks', ['source', 'path'], unique=False)
    op.create_table('system_state',
    sa.Column('key', sa.VARCHAR(length=64), autoincrement=False, nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('key', name='system_state_pkey')
    )
    op.create_table('task_session_usage_peaks',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('session_id', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('peak_rss_kb', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('peak_cpu_pct', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('cpu_seconds', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('first_seen_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('last_seen_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_task_session_usage_peaks'),
    sa.UniqueConstraint('id', 'org_id', name='uq_task_session_usage_peaks_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('task_id', 'node_id', 'session_id', name='uq_task_session_usage_peaks_key', postgresql_nulls_not_distinct=False)
    )
    op.create_table('tenancy_exclusions',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('occurred_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('mapped_class', sa.VARCHAR(length=64), autoincrement=False, nullable=False),
    sa.Column('scope_kind', sa.VARCHAR(length=24), autoincrement=False, nullable=False),
    sa.Column('bound_org', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('route_or_job', sa.VARCHAR(length=200), autoincrement=False, nullable=False),
    sa.Column('mode', sa.VARCHAR(length=24), autoincrement=False, nullable=False),
    sa.Column('excluded_count', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_tenancy_exclusions'),
    sa.UniqueConstraint('occurred_at', 'mapped_class', 'scope_kind', 'bound_org', 'route_or_job', 'mode', name='uq_tenancy_exclusions_bucket', postgresql_nulls_not_distinct=True)
    )
    op.create_table('tenant_email_sends',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('connection_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('from_address', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('actor_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('automation_run_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('to_addresses', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('cc_addresses', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('subject', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('status', sa.VARCHAR(length=8), autoincrement=False, nullable=False),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('gmail_message_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('sent_by_task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('trigger_delivery_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("status IN ('sent', 'failed')", name='ck_tenant_email_sends_status'),
    sa.PrimaryKeyConstraint('id', name='pk_tenant_email_sends'),
    sa.UniqueConstraint('id', 'org_id', name='uq_tenant_email_sends_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_tenant_email_sends_connection_id_created_at', 'tenant_email_sends', ['connection_id', 'created_at'], unique=False)
    op.create_index('ix_tenant_email_sends_org_id_created_at', 'tenant_email_sends', ['org_id', 'created_at'], unique=False)
    op.create_table('token_ledger_entries',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('amount_micros', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('free_delta_micros', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('paid_delta_micros', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('idempotency_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('usage_call_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('topup_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("kind = ANY (ARRAY['signup_credit'::text, 'operator_credit'::text, 'topup'::text, 'auto_recharge'::text, 'operator_debit'::text, 'refund'::text, 'spend'::text, 'spend_correction'::text])", name='ck_token_ledger_entries_kind'),
    sa.CheckConstraint('amount_micros = (free_delta_micros + paid_delta_micros)', name='ck_token_ledger_entries_split'),
    sa.PrimaryKeyConstraint('id', name='pk_token_ledger_entries'),
    sa.UniqueConstraint('id', 'org_id', name='uq_token_ledger_entries_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('idempotency_key', name='uq_token_ledger_entries_idempotency_key', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_token_ledger_entries_org_created', 'token_ledger_entries', ['org_id', 'created_at'], unique=False)
    op.create_table('token_topups',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('amount_micros', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('request_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('stripe_invoice_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stripe_payment_method_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('refunded_micros', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('failure_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('suppressed_until', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("kind = ANY (ARRAY['topup'::text, 'auto_recharge'::text])", name='ck_token_topups_kind'),
    sa.CheckConstraint("status = ANY (ARRAY['pending'::text, 'paid'::text, 'failed'::text])", name='ck_token_topups_status'),
    sa.CheckConstraint('amount_micros > 0', name='ck_token_topups_amount'),
    sa.CheckConstraint('refunded_micros >= 0 AND refunded_micros <= amount_micros', name='ck_token_topups_refunded'),
    sa.PrimaryKeyConstraint('id', name='pk_token_topups'),
    sa.UniqueConstraint('id', 'org_id', name='uq_token_topups_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('request_id', name='uq_token_topups_request_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('stripe_invoice_id', name='uq_token_topups_stripe_invoice_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_token_topups_org_created', 'token_topups', ['org_id', 'created_at'], unique=False)
    op.create_index('uq_token_topups_one_pending_recharge', 'token_topups', ['org_id'], unique=True, postgresql_where="((kind = 'auto_recharge'::text) AND (status = 'pending'::text))")
    op.create_table('token_wallets',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('balance_micros', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('free_micros', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('paid_micros', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('floor_state', sa.TEXT(), server_default=sa.text("'ok'::text"), autoincrement=False, nullable=False),
    sa.Column('low_alerted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("floor_state = ANY (ARRAY['ok'::text, 'low'::text, 'floor'::text])", name='ck_token_wallets_floor_state'),
    sa.CheckConstraint('balance_micros = (free_micros + paid_micros)', name='ck_token_wallets_split'),
    sa.PrimaryKeyConstraint('id', name='pk_token_wallets'),
    sa.UniqueConstraint('id', 'org_id', name='uq_token_wallets_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', name='uq_token_wallets_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('agents',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('is_catalog', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('display_name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('agent_type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('language', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('entrypoint', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('maintainer', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('personas', postgresql.ARRAY(sa.TEXT()), autoincrement=False, nullable=False),
    sa.Column('repo_url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('subpath', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('ref', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('resolved_commit', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('bundle_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('bundle_sha256', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('bundle_bytes', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('repo_access_user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'active'::text"), autoincrement=False, nullable=False),
    sa.Column('resolve_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('resolved_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('env', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.CheckConstraint("NOT is_catalog OR org_id = '01900000-0000-7000-8000-000000000010'::uuid", name='ck_agents_catalog_in_operator_org'),
    sa.CheckConstraint("resolved_commit IS NULL OR resolved_commit ~ '^[0-9a-f]{40}$'::text", name='ck_agents_commit'),
    sa.CheckConstraint("status = ANY (ARRAY['active'::text, 'disabled'::text, 'removed'::text])", name='ck_agents_status'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='agents_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['repo_access_user_id'], ['admin_users.id'], name='agents_repo_access_user_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='agents_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='agents_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_agents_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'slug', name='uq_agents_org_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_table('allowed_instance_types',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('instance_type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('position', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('family', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('vcpu', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('memory_mib', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('architecture', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('network_performance', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('current_generation', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('gpu_count', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('specs_region', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('specs_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('bare_metal', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('nested_virtualization', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('provider', sa.TEXT(), server_default=sa.text("'aws'::text"), autoincrement=False, nullable=False),
    sa.Column('windows_capable', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.CheckConstraint("architecture IS NULL OR (architecture = ANY (ARRAY['x86_64'::text, 'arm64'::text]))", name='ck_allowed_instance_types_architecture'),
    sa.CheckConstraint("provider = ANY (ARRAY['aws'::text, 'gcp'::text, 'azure'::text])", name='ck_allowed_instance_types_provider'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='allowed_instance_types_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='allowed_instance_types_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='allowed_instance_types_pkey'),
    sa.UniqueConstraint('provider', 'instance_type', name='uq_allowed_instance_types_provider_instance_type', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_allowed_instance_types_position', 'allowed_instance_types', ['position'], unique=False)
    op.create_table('api_keys',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('owner_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('grants_all', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('permissions', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('org_admin', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('ip_allowlist', postgresql.ARRAY(postgresql.CIDR()), server_default=sa.text("'{}'::cidr[]"), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('revoked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('revoked_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('revoke_reason', sa.VARCHAR(length=32), autoincrement=False, nullable=True),
    sa.Column('last_used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_used_ip', postgresql.INET(), autoincrement=False, nullable=True),
    sa.Column('reminded_7d_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('reminded_1d_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('reminder_seen_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('expiry_recorded_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_api_keys_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['owner_id'], ['admin_users.id'], name='fk_api_keys_owner_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['revoked_by_id'], ['admin_users.id'], name='fk_api_keys_revoked_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_api_keys_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_api_keys'),
    sa.UniqueConstraint('id', 'org_id', name='uq_api_keys_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_api_keys_owner_id', 'api_keys', ['owner_id'], unique=False)
    op.create_table('automations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('when_kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('is_system', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('target_kind', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('target_mode', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('target_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('action_type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('action_params', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('created_by_task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.CheckConstraint("jsonb_typeof(target_ids) = 'array'::text", name='ck_automations_target_ids_array'),
    sa.CheckConstraint("target_kind = ANY (ARRAY['projects'::text, 'nodes'::text])", name='ck_automations_target_kind'),
    sa.CheckConstraint("target_kind IS NOT NULL OR target_ids = '[]'::jsonb", name='ck_automations_no_target_no_ids'),
    sa.CheckConstraint("target_mode = ANY (ARRAY['all'::text, 'specific'::text, 'all_except'::text])", name='ck_automations_target_mode'),
    sa.CheckConstraint("target_mode IS DISTINCT FROM 'all'::text OR target_ids = '[]'::jsonb", name='ck_automations_all_has_no_ids'),
    sa.CheckConstraint("when_kind = ANY (ARRAY['schedule'::text, 'event'::text])", name='ck_automations_when_kind'),
    sa.CheckConstraint('(target_kind IS NULL) = (target_mode IS NULL)', name='ck_automations_target_pair'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='automations_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='automations_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='automations_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_automations_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'slug', name='uq_automations_org_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_automations_created_by_task_id', 'automations', ['created_by_task_id'], unique=False)
    op.create_index('uq_automations_system_action_type', 'automations', ['action_type'], unique=True, postgresql_where='is_system')
    op.create_table('billing_plans',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('blurb', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('limit_runner_slots', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_nodes', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_projects', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_users', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_active_worktrees', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('self_serve', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('active', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('sort_order', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('stripe_product_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stripe_price_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('price_unit_amount', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('currency', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('limit_github_runners', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('duration_days', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('hosting_allowed', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=True),
    sa.Column('compliance_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('nodevault_allowed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('static_slots', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('nodelink_allowed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('merge_lane_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('microvm_allowed', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=True),
    sa.Column('advanced_compliance_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('playbooks_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('ubuntu_nodes_allowed', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=True),
    sa.Column('macos_nodes_allowed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('windows_nodes_allowed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('project_memory_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('includes_plan_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('then_plan_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('slack_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('sso_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('scim_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('siem_export_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('data_residency_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('ip_allowlist_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('org_retention_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('staging_org_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('budgets_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('per_agent', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('min_agents', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('max_agents', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('yearly_stripe_price_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('yearly_price_unit_amount', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('limit_task_attachment_mb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_attachment_file_mb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('jaah_ai_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('trial_days', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('audit_enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=True),
    sa.Column('session_transcripts_enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=True),
    sa.Column('inherits', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('limit_org_attachment_gb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_agent_requests_per_minute', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('cron_min_period_seconds', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('aws_cloud_allowed', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=True),
    sa.Column('gcp_cloud_allowed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('azure_cloud_allowed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('trial_agent_ceiling', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('tenant_email_allowed', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=True),
    sa.Column('providers', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.CheckConstraint('cron_min_period_seconds IS NULL OR cron_min_period_seconds >= 1', name='ck_billing_plans_cron_min_period_seconds'),
    sa.CheckConstraint('duration_days IS NULL OR duration_days > 0', name='ck_billing_plans_duration_days'),
    sa.CheckConstraint('limit_active_worktrees IS NULL OR limit_active_worktrees >= 0', name='ck_billing_plans_limit_active_worktrees'),
    sa.CheckConstraint('limit_agent_requests_per_minute IS NULL OR limit_agent_requests_per_minute >= 0', name='ck_billing_plans_limit_agent_requests_per_minute'),
    sa.CheckConstraint('limit_attachment_file_mb IS NULL OR limit_attachment_file_mb >= 0', name='ck_billing_plans_limit_attachment_file_mb'),
    sa.CheckConstraint('limit_github_runners IS NULL OR limit_github_runners >= 0', name='ck_billing_plans_limit_github_runners'),
    sa.CheckConstraint('limit_nodes IS NULL OR limit_nodes >= 0', name='ck_billing_plans_limit_nodes'),
    sa.CheckConstraint('limit_org_attachment_gb IS NULL OR limit_org_attachment_gb >= 0', name='ck_billing_plans_limit_org_attachment_gb'),
    sa.CheckConstraint('limit_projects IS NULL OR limit_projects >= 0', name='ck_billing_plans_limit_projects'),
    sa.CheckConstraint('limit_runner_slots IS NULL OR limit_runner_slots >= 0', name='ck_billing_plans_limit_runner_slots'),
    sa.CheckConstraint('limit_task_attachment_mb IS NULL OR limit_task_attachment_mb >= 0', name='ck_billing_plans_limit_task_attachment_mb'),
    sa.CheckConstraint('limit_users IS NULL OR limit_users >= 0', name='ck_billing_plans_limit_users'),
    sa.CheckConstraint('max_agents IS NULL OR max_agents >= COALESCE(min_agents, 1)', name='ck_billing_plans_max_agents'),
    sa.CheckConstraint('min_agents IS NULL OR min_agents >= 1', name='ck_billing_plans_min_agents'),
    sa.CheckConstraint('static_slots >= 0', name='ck_billing_plans_static_slots'),
    sa.CheckConstraint('trial_agent_ceiling IS NULL OR trial_agent_ceiling > 0', name='ck_billing_plans_trial_agent_ceiling'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_billing_plans_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['includes_plan_key'], ['billing_plans.key'], name='fk_billing_plans_includes_plan_key', onupdate='CASCADE', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['then_plan_key'], ['billing_plans.key'], name='fk_billing_plans_then_plan_key', onupdate='CASCADE', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_billing_plans_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_billing_plans'),
    sa.UniqueConstraint('key', name='uq_billing_plans_key', postgresql_nulls_not_distinct=False)
    )
    op.create_table('built_in_playbooks',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('entry', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('full_auto_default', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('default_seniority', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('stages', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.CheckConstraint("default_seniority = ANY (ARRAY['junior'::text, 'mid'::text, 'senior'::text])", name='ck_built_in_playbooks_default_seniority'),
    sa.CheckConstraint("entry = ANY (ARRAY['new_task'::text, 'new_chat'::text])", name='ck_built_in_playbooks_entry'),
    sa.CheckConstraint("jsonb_typeof(stages) = 'array'::text", name='ck_built_in_playbooks_stages_array'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_built_in_playbooks_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_built_in_playbooks_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_built_in_playbooks'),
    sa.UniqueConstraint('name', name='uq_built_in_playbooks_name', postgresql_nulls_not_distinct=False)
    )
    op.create_table('console_tickets',
    sa.Column('ticket_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('peer_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('node_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('browser_ticket_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('node_name', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('read_only', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('session_epoch', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('session_iepoch', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('owner_address', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('node_redeemed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('browser_redeemed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('revoked', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('seen_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_input_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('revoked_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('client_ip', sa.TEXT(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['peer_id'], ['cp_peers.id'], name='fk_console_tickets_peer_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('ticket_hash', name='console_tickets_pkey')
    )
    op.create_index('uq_console_tickets_browser_ticket_hash', 'console_tickets', ['browser_ticket_hash'], unique=True)
    op.create_table('credentials',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('owner_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('system', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('nodevault', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('home_project_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('has_stored_secret', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_tested_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_test_ok', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('home_key', sa.TEXT(), sa.Computed("\nCASE\n    WHEN system THEN ('s:'::text || (org_id)::text)\n    WHEN (home_project_id IS NOT NULL) THEN ('p:'::text || (home_project_id)::text)\n    WHEN (owner_id IS NOT NULL) THEN ('u:'::text || (owner_id)::text)\n    ELSE ('o:'::text || (org_id)::text)\nEND", persisted=True), autoincrement=False, nullable=True),
    sa.Column('parent_path', sa.TEXT(), sa.Computed('\nCASE\n    WHEN (strpos(name, \'/\'::text) = 0) THEN \'\'::text\n    ELSE "substring"(lower(name), \'^(.*)/[^/]*$\'::text)\nEND', persisted=True), autoincrement=False, nullable=True),
    sa.CheckConstraint('NOT system OR owner_id IS NULL', name='ck_credentials_system_unowned'),
    sa.CheckConstraint('home_project_id IS NULL OR owner_id IS NULL AND NOT system', name='ck_credentials_home_project_org_row'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_credentials_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['owner_id'], ['admin_users.id'], name='fk_credentials_owner_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_credentials_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='credentials_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_credentials_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_credentials_created_at_id', 'credentials', [sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_credentials_description_trgm', 'credentials', [sa.literal_column('lower(description)')], unique=False, postgresql_ops={'lower(description)': 'gin_trgm_ops'}, postgresql_using='gin')
    op.create_index('ix_credentials_expires_at', 'credentials', ['expires_at'], unique=False, postgresql_where='(expires_at IS NOT NULL)')
    op.create_index('ix_credentials_home_folder_key', 'credentials', ['home_key', sa.literal_column("(parent_path || '/'::text)")], unique=False, postgresql_ops={"(parent_path || '/'::text)": 'text_pattern_ops'})
    op.create_index('ix_credentials_home_key', 'credentials', ['home_key'], unique=False)
    op.create_index('ix_credentials_home_parent_lname_id', 'credentials', ['home_key', 'parent_path', sa.literal_column('lower(name)'), 'id'], unique=False)
    op.create_index('ix_credentials_home_parent_pat', 'credentials', ['home_key', 'parent_path'], unique=False, postgresql_ops={'parent_path': 'text_pattern_ops'})
    op.create_index('ix_credentials_home_project_id', 'credentials', ['home_project_id'], unique=False, postgresql_where='(home_project_id IS NOT NULL)')
    op.create_index('ix_credentials_lname_id', 'credentials', [sa.literal_column('lower(name)'), 'id'], unique=False)
    op.create_index('ix_credentials_name_trgm', 'credentials', [sa.literal_column('lower(name)')], unique=False, postgresql_ops={'lower(name)': 'gin_trgm_ops'}, postgresql_using='gin')
    op.create_index('ix_credentials_never_used', 'credentials', [sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False, postgresql_where='(last_used_at IS NULL)')
    op.create_index('ix_credentials_no_secret', 'credentials', [sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False, postgresql_where='(NOT has_stored_secret)')
    op.create_index('ix_credentials_org_created_id', 'credentials', ['org_id', sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_credentials_org_home_key', 'credentials', ['org_id', 'home_key'], unique=False)
    op.create_index('ix_credentials_org_lname_id', 'credentials', ['org_id', sa.literal_column('lower(name)'), 'id'], unique=False)
    op.create_index('ix_credentials_org_type_lname_id', 'credentials', ['org_id', 'type', sa.literal_column('lower(name)'), 'id'], unique=False)
    op.create_index('ix_credentials_owner_expr', 'credentials', [sa.literal_column('COALESCE(owner_id, created_by_id)'), sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_credentials_test_failed', 'credentials', [sa.literal_column('created_at DESC'), sa.literal_column('id DESC')], unique=False, postgresql_where='(last_test_ok = false)')
    op.create_index('ix_credentials_type_lname_id', 'credentials', ['type', sa.literal_column('lower(name)'), 'id'], unique=False)
    op.create_table('dmarc_report_rows',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('dmarc_report_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('source_ip', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('count', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('disposition', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('dkim', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('spf', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('dmarc', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('header_from', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['dmarc_report_id'], ['dmarc_reports.id'], name='fk_dmarc_report_rows_dmarc_report_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_dmarc_report_rows')
    )
    op.create_index('ix_dmarc_report_rows_dmarc_report_id', 'dmarc_report_rows', ['dmarc_report_id'], unique=False)
    op.create_table('email_templates',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('fields', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('is_active', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.CheckConstraint("jsonb_typeof(fields) = 'object'::text", name='ck_email_templates_fields_object'),
    sa.CheckConstraint("type = ANY (ARRAY['user_invitation'::text, 'notification'::text, 'signup_verify'::text, 'signup_account_exists'::text, 'application_received'::text, 'welcome'::text, 'password_reset'::text, 'signup_alert'::text, 'trigger_failed'::text, 'trigger_digest'::text, 'trust_request_approved'::text, 'trust_request_declined'::text, 'org_rejected'::text])", name='ck_email_templates_type'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_email_templates_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_email_templates_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_email_templates'),
    sa.UniqueConstraint('type', 'name', name='uq_email_templates_type_name', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_email_templates_active_type', 'email_templates', ['type'], unique=True, postgresql_where='is_active')
    op.create_table('gmail_connections',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('kind', sa.VARCHAR(length=16), autoincrement=False, nullable=False),
    sa.Column('address', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('sealed_secret', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('can_receive', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('history_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('last_sync_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last_used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('sync_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("NOT can_receive OR kind::text = 'service_account'::text", name='ck_gmail_connections_receive_kind'),
    sa.CheckConstraint("kind IN ('oauth', 'service_account')", name='ck_gmail_connections_kind'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_gmail_connections_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_gmail_connections_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_gmail_connections'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gmail_connections_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'address', name='uq_gmail_connections_org_id_address', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_campaigns',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('rule_keys', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_gov_campaigns_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_gov_campaigns_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_campaigns'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_campaigns_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_managed_packs',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('current_version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('levels', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('pillars', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('unlock_threshold', sa.NUMERIC(), server_default=sa.text('0.8'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_gov_managed_packs_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_gov_managed_packs_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_managed_packs'),
    sa.UniqueConstraint('slug', name='uq_gov_managed_packs_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_org_settings',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('active_pack_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('active_pack_version', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last_selection', postgresql.ARRAY(sa.BIGINT()), server_default=sa.text("'{}'::bigint[]"), autoincrement=False, nullable=False),
    sa.Column('daily_scan_enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('guard_stuck_limit', sa.INTEGER(), server_default=sa.text('5'), autoincrement=False, nullable=False),
    sa.CheckConstraint('guard_stuck_limit >= 1 AND guard_stuck_limit <= 50', name='ck_gov_org_settings_guard_stuck_limit'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_gov_org_settings_updated_by_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_org_settings'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_org_settings_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', name='uq_gov_org_settings_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_pack_proposals',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('target_pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('from_version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('to_version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('diff', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('effect', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'proposed'::text"), autoincrement=False, nullable=False),
    sa.Column('decided_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('decided_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("status = ANY (ARRAY['proposed'::text, 'applied'::text, 'dismissed'::text])", name='ck_gov_pack_proposals_status'),
    sa.ForeignKeyConstraint(['decided_by_id'], ['admin_users.id'], name='fk_gov_pack_proposals_decided_by_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_pack_proposals'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_pack_proposals_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_scans',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pack_version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('trigger', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('started_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('finished_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("trigger = ANY (ARRAY['manual'::text, 'daily'::text, 'rescan_repo'::text])", name='ck_gov_scans_trigger'),
    sa.ForeignKeyConstraint(['started_by_id'], ['admin_users.id'], name='fk_gov_scans_started_by_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_scans'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_scans_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('groups',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('is_everyone', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('scim_managed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_groups_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_groups_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='groups_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_groups_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_groups_everyone_per_org', 'groups', ['org_id'], unique=True, postgresql_where='is_everyone')
    op.create_index('uq_groups_org_id_lower_name', 'groups', ['org_id', sa.literal_column('lower(name)')], unique=True)
    op.create_table('identities',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('email', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.Column('email_verified_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('password_hash', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('first_name', sa.VARCHAR(length=120), autoincrement=False, nullable=True),
    sa.Column('last_name', sa.VARCHAR(length=120), autoincrement=False, nullable=True),
    sa.Column('google_sub', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('github_user_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('microsoft_tenant_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('microsoft_object_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('preferences', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('session_epoch', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('last_active_admin_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint('(microsoft_tenant_id IS NULL) = (microsoft_object_id IS NULL)', name='ck_identities_microsoft_identity_pair'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_identities_created_by_id', ondelete='SET DEFAULT'),
    sa.ForeignKeyConstraint(['last_active_admin_id'], ['admin_users.id'], name='fk_identities_last_active_admin_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_identities_updated_by_id', ondelete='SET DEFAULT'),
    sa.PrimaryKeyConstraint('id', name='identities_pkey'),
    sa.UniqueConstraint('email', name='identities_email_key', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('github_user_id', name='identities_github_user_id_key', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('google_sub', name='identities_google_sub_key', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('microsoft_tenant_id', 'microsoft_object_id', name='uq_identities_microsoft_identity', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_identities_email_trgm', 'identities', [sa.literal_column('lower(email::text)')], unique=False, postgresql_ops={'lower(email::text)': 'gin_trgm_ops'}, postgresql_using='gin')
    op.create_index('ix_identities_name_trgm', 'identities', [sa.literal_column("lower((COALESCE(first_name, ''::character varying)::text || ' '::text) || COALESCE(last_name, ''::character varying)::text)")], unique=False, postgresql_ops={"lower((COALESCE(first_name, ''::character varying)::text || ' '::text) || COALESCE(last_name, ''::character varying)::text)": 'gin_trgm_ops'}, postgresql_using='gin')
    op.create_table('model_prices',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('input_per_mtok', sa.NUMERIC(precision=12, scale=4), autoincrement=False, nullable=False),
    sa.Column('cache_write_5m_per_mtok', sa.NUMERIC(precision=12, scale=4), autoincrement=False, nullable=False),
    sa.Column('cache_write_1h_per_mtok', sa.NUMERIC(precision=12, scale=4), autoincrement=False, nullable=False),
    sa.Column('cache_read_per_mtok', sa.NUMERIC(precision=12, scale=4), autoincrement=False, nullable=False),
    sa.Column('output_per_mtok', sa.NUMERIC(precision=12, scale=4), autoincrement=False, nullable=False),
    sa.CheckConstraint('input_per_mtok >= 0::numeric AND cache_write_5m_per_mtok >= 0::numeric AND cache_write_1h_per_mtok >= 0::numeric AND cache_read_per_mtok >= 0::numeric AND output_per_mtok >= 0::numeric', name='ck_model_prices_rates_nonnegative'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_model_prices_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_model_prices_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_model_prices'),
    sa.UniqueConstraint('name', name='uq_model_prices_name', postgresql_nulls_not_distinct=False)
    )
    op.create_table('node_bus_sockets',
    sa.Column('node_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('peer_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('socket_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('connected_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('last_seen_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['peer_id'], ['cp_peers.id'], name='fk_node_bus_sockets_peer_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_id', name='node_bus_sockets_pkey')
    )
    op.create_table('nodevault_keys',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('key_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('public_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('source', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('chain_blob', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('backup_confirmed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('retired_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'::text", name='ck_nodevault_keys_key_hash'),
    sa.CheckConstraint("source = ANY (ARRAY['generate'::text, 'upload'::text])", name='ck_nodevault_keys_source'),
    sa.CheckConstraint("state = ANY (ARRAY['pending'::text, 'current'::text, 'previous'::text, 'retiring'::text, 'retired'::text, 'abandoned'::text])", name='ck_nodevault_keys_state'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_nodevault_keys_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_nodevault_keys_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_nodevault_keys'),
    sa.UniqueConstraint('id', 'org_id', name='uq_nodevault_keys_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'key_hash', name='uq_nodevault_keys_org_key_hash', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_nodevault_keys_one_current', 'nodevault_keys', ['org_id'], unique=True, postgresql_where="(state = 'current'::text)")
    op.create_table('org_comps',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('quantity', sa.INTEGER(), server_default=sa.text('1'), autoincrement=False, nullable=False),
    sa.Column('tier_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('instance_type', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('storage_gb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('credit_cents', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('sort_order', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('end_mode', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('ends_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('ended_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('ended_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('backfill_tasks', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.CheckConstraint("(end_mode = 'date'::text) = (ends_at IS NOT NULL)", name='ck_org_comps_ends_at'),
    sa.CheckConstraint("backfill_tasks IS NULL OR kind = 'live_cast'::text", name='ck_org_comps_backfill_tasks'),
    sa.CheckConstraint("end_mode = ANY (ARRAY['never'::text, 'date'::text, 'plan'::text])", name='ck_org_comps_end_mode'),
    sa.CheckConstraint("kind <> 'dedicated_node'::text OR instance_type IS NOT NULL AND storage_gb IS NOT NULL", name='ck_org_comps_dedicated_node'),
    sa.CheckConstraint("kind <> 'jaah_ai_credit'::text OR credit_cents IS NOT NULL", name='ck_org_comps_jaah_ai_credit'),
    sa.CheckConstraint("kind <> 'live_cast'::text OR COALESCE(backfill_tasks, 0) >= 1", name='ck_org_comps_live_cast'),
    sa.CheckConstraint("kind <> 'shared_node'::text OR tier_key IS NOT NULL", name='ck_org_comps_shared_node'),
    sa.CheckConstraint("kind = ANY (ARRAY['shared_node'::text, 'dedicated_node'::text, 'jaah_ai_credit'::text, 'live_cast'::text])", name='ck_org_comps_kind'),
    sa.CheckConstraint('credit_cents IS NULL OR credit_cents >= 0', name='ck_org_comps_credit_cents'),
    sa.CheckConstraint('quantity >= 1', name='ck_org_comps_quantity'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='org_comps_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='org_comps_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='org_comps_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_comps_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_org_comps_org_id', 'org_comps', ['org_id'], unique=False)
    op.create_table('org_domains',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('domain', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("domain = lower(domain) AND domain <> ''::text", name='ck_org_domains_lowercase'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_org_domains_created_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_org_domains'),
    sa.UniqueConstraint('domain', name='uq_org_domains_domain', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_domains_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_org_domains_org_id', 'org_domains', ['org_id'], unique=False)
    op.create_table('org_ip_ranges',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('cidr', postgresql.CIDR(), autoincrement=False, nullable=False),
    sa.Column('label', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_org_ip_ranges_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_org_ip_ranges_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_org_ip_ranges'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_ip_ranges_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'cidr', name='uq_org_ip_ranges_org_id_cidr', postgresql_nulls_not_distinct=False)
    )
    op.create_table('org_plan_products',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('quantity', sa.INTEGER(), server_default=sa.text('1'), autoincrement=False, nullable=False),
    sa.Column('tier_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('instance_type', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('storage_gb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('credit_cents', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('sort_order', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('backfill_tasks', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.CheckConstraint("backfill_tasks IS NULL OR kind = 'live_cast'::text", name='ck_org_plan_products_backfill_tasks'),
    sa.CheckConstraint("kind <> 'dedicated_node'::text OR instance_type IS NOT NULL AND storage_gb IS NOT NULL", name='ck_org_plan_products_dedicated_node'),
    sa.CheckConstraint("kind <> 'jaah_ai_credit'::text OR credit_cents IS NOT NULL", name='ck_org_plan_products_jaah_ai_credit'),
    sa.CheckConstraint("kind <> 'live_cast'::text OR COALESCE(backfill_tasks, 0) >= 1", name='ck_org_plan_products_live_cast'),
    sa.CheckConstraint("kind <> 'shared_node'::text OR tier_key IS NOT NULL", name='ck_org_plan_products_shared_node'),
    sa.CheckConstraint("kind = ANY (ARRAY['shared_node'::text, 'dedicated_node'::text, 'jaah_ai_credit'::text, 'live_cast'::text])", name='ck_org_plan_products_kind'),
    sa.CheckConstraint('credit_cents IS NULL OR credit_cents >= 0', name='ck_org_plan_products_credit_cents'),
    sa.CheckConstraint('quantity >= 1', name='ck_org_plan_products_quantity'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_org_plan_products_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_org_plan_products_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_org_plan_products'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_plan_products_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_org_plan_products_org_id', 'org_plan_products', ['org_id'], unique=False)
    op.create_table('org_siem_destinations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('auth_mode', sa.VARCHAR(length=8), autoincrement=False, nullable=False),
    sa.Column('header_name', sa.VARCHAR(length=128), autoincrement=False, nullable=True),
    sa.Column('secret_sealed', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('cursor_event_id', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('last_attempt_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_success_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_status', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('last_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.CheckConstraint("auth_mode IN ('header', 'hmac')", name='ck_org_siem_destinations_auth_mode'),
    sa.CheckConstraint("url ~~ 'https://%'::text", name='ck_org_siem_destinations_url_https'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_org_siem_destinations_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_org_siem_destinations_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_org_siem_destinations'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_siem_destinations_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', name='uq_org_siem_destinations_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('org_signups',
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('owner_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('provider', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('email', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('signed_up_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('decided_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('decided_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('rejection_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('slack_posted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('slack_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('hubspot_contact_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('hubspot_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('occupation', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('job_title', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('intended_use', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('welcome_sent_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('welcome_sent_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('welcome_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('plan_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('country', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('submitted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('plan_confirmed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('setup_intent_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('payment_method_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('billing_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("provider = ANY (ARRAY['google'::text, 'github'::text, 'microsoft'::text, 'email'::text])", name='ck_org_signups_provider'),
    sa.ForeignKeyConstraint(['decided_by_id'], ['admin_users.id'], name='fk_org_signups_decided_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_id'], ['admin_users.id'], name='fk_org_signups_owner_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['welcome_sent_by_id'], ['admin_users.id'], name='fk_org_signups_welcome_sent_by_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('org_id', name='pk_org_signups')
    )
    op.create_index('ix_org_signups_owner_id', 'org_signups', ['owner_id'], unique=False)
    op.create_table('org_sso_domains',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('domain', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('verification_token', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('verified_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.CheckConstraint("domain = lower(domain) AND domain <> ''::text", name='ck_org_sso_domains_lowercase'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_org_sso_domains_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_org_sso_domains_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_org_sso_domains'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_sso_domains_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'domain', name='uq_org_sso_domains_org_id_domain', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_org_sso_domains_verified_domain', 'org_sso_domains', ['domain'], unique=True, postgresql_where='(verified_at IS NOT NULL)')
    op.create_table('playbooks',
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('entry', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('full_auto_default', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('default_seniority', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('stages', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.CheckConstraint("default_seniority = ANY (ARRAY['junior'::text, 'mid'::text, 'senior'::text])", name='ck_playbooks_default_seniority'),
    sa.CheckConstraint("entry = ANY (ARRAY['new_task'::text, 'new_chat'::text])", name='ck_playbooks_entry'),
    sa.CheckConstraint("jsonb_typeof(stages) = 'array'::text", name='ck_playbooks_stages_array'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_playbooks_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_playbooks_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_playbooks'),
    sa.UniqueConstraint('id', 'org_id', name='uq_playbooks_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'name', name='uq_playbooks_org_name', postgresql_nulls_not_distinct=False)
    )
    op.create_table('port_grid_vars',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('range_start', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='port_grid_vars_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='port_grid_vars_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='port_grid_vars_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_port_grid_vars_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('name', name='uq_port_grid_vars_name', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'name', name='uq_port_grid_vars_org_id_name', postgresql_nulls_not_distinct=False)
    )
    op.create_table('rental_prices',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('monthly_cents', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('active', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('sort_order', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('vcpu', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('ram_gb', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('stripe_product_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stripe_price_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('unit_amount', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('version', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('stripe_account_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('livemode', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('disk_gb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.CheckConstraint("kind = ANY (ARRAY['instance'::text, 'storage'::text, 'shared'::text, 'live_cast'::text, 'windows'::text])", name='ck_rental_prices_kind'),
    sa.CheckConstraint('monthly_cents >= 0', name='ck_rental_prices_monthly_cents'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_rental_prices_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_rental_prices_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_rental_prices'),
    sa.UniqueConstraint('kind', 'key', name='uq_rental_prices_kind_key', postgresql_nulls_not_distinct=False)
    )
    op.create_table('role_permissions',
    sa.Column('role_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('permission_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['permission_id'], ['permissions.id'], name='fk_role_permissions_permission_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], name='fk_role_permissions_role_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('role_id', 'permission_id', name='pk_role_permissions')
    )
    op.create_table('scim_tokens',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('token_hash', sa.VARCHAR(length=64), autoincrement=False, nullable=False),
    sa.Column('prefix', sa.VARCHAR(length=32), autoincrement=False, nullable=False),
    sa.Column('revoked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_scim_tokens_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_scim_tokens_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_scim_tokens'),
    sa.UniqueConstraint('id', 'org_id', name='uq_scim_tokens_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('token_hash', name='uq_scim_tokens_token_hash', postgresql_nulls_not_distinct=False)
    )
    op.create_table('site_chat_messages',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('conversation_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('role', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('content', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('tool_calls', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('provider', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('latency_ms', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('input_tokens', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('output_tokens', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('miss_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('redaction_hits', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('tool_exchange', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.CheckConstraint("role = ANY (ARRAY['user'::text, 'assistant'::text])", name='ck_site_chat_messages_role'),
    sa.ForeignKeyConstraint(['conversation_id'], ['site_chat_conversations.id'], name='fk_site_chat_messages_conversation_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_site_chat_messages')
    )
    op.create_index('ix_site_chat_messages_conversation_created', 'site_chat_messages', ['conversation_id', 'created_at'], unique=False)
    op.create_index('ix_site_chat_messages_created_at', 'site_chat_messages', ['created_at'], unique=False)
    op.create_table('site_enquiries',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('received_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('email', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('company', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('role', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('offer', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('note', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('note_truncated', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('recaptcha_score', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('slack_posted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('slack_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('hubspot_contact_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('hubspot_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('source', sa.TEXT(), server_default=sa.text("'form'::text"), autoincrement=False, nullable=False),
    sa.Column('site_chat_conversation_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('visitor_ip', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('visitor_ip_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('user_agent', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('recaptcha_reasons', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.CheckConstraint("source = ANY (ARRAY['form'::text, 'jarvis'::text])", name='ck_site_enquiries_source'),
    sa.ForeignKeyConstraint(['site_chat_conversation_id'], ['site_chat_conversations.id'], name='fk_site_enquiries_site_chat_conversation_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_site_enquiries'),
    sa.UniqueConstraint('id', 'org_id', name='uq_site_enquiries_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_site_enquiries_received_at', 'site_enquiries', [sa.literal_column('received_at DESC')], unique=False)
    op.create_table('slack_installations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('team_id', sa.VARCHAR(length=32), autoincrement=False, nullable=False),
    sa.Column('team_name', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('app_id', sa.VARCHAR(length=32), autoincrement=False, nullable=False),
    sa.Column('bot_user_id', sa.VARCHAR(length=32), autoincrement=False, nullable=False),
    sa.Column('token_sealed', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('disconnected_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('disconnect_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.CheckConstraint('(disconnected_at IS NULL) = (token_sealed IS NOT NULL)', name='ck_slack_installations_token_iff_connected'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_slack_installations_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_slack_installations_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_slack_installations'),
    sa.UniqueConstraint('id', 'org_id', name='uq_slack_installations_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', name='uq_slack_installations_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_slack_installations_connected_team_id', 'slack_installations', ['team_id'], unique=True, postgresql_where='(disconnected_at IS NULL)')
    op.create_table('support_conversations',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('ticket_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('turn_count', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_support_conversations_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_support_conversations'),
    sa.UniqueConstraint('id', 'org_id', name='uq_support_conversations_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_support_conversations_user_updated', 'support_conversations', ['user_id', sa.literal_column('updated_at DESC')], unique=False)
    op.create_index('uq_support_conversations_ticket_id', 'support_conversations', ['ticket_id'], unique=True, postgresql_where='(ticket_id IS NOT NULL)')
    op.create_table('tag_groups',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'draft'::text"), autoincrement=False, nullable=False),
    sa.Column('multi_select', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('position', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.CheckConstraint("state = ANY (ARRAY['draft'::text, 'active'::text])", name='ck_tag_groups_state'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='tag_groups_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='tag_groups_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='tag_groups_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_tag_groups_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'name', name='uq_tag_groups_org_name', postgresql_nulls_not_distinct=False)
    )
    op.create_table('token_usage_calls',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('call_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('jaah_session_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('model_family', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('model_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('node_tokens', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('aws_tokens', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('cost_micros', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('charge_micros', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('markup_bps', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('source', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('occurred_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('flag', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('node_call_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('aws_request_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("flag IS NULL OR (flag = ANY (ARRAY['unattributed'::text, 'aws_only'::text, 'node_only'::text, 'mismatch'::text]))", name='ck_token_usage_calls_flag'),
    sa.CheckConstraint("source = ANY (ARRAY['node'::text, 'aws'::text, 'both'::text])", name='ck_token_usage_calls_source'),
    sa.ForeignKeyConstraint(['jaah_session_id'], ['jaah_sessions.id'], name='token_usage_calls_jaah_session_id_fkey', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_token_usage_calls'),
    sa.UniqueConstraint('id', 'org_id', name='uq_token_usage_calls_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'call_key', name='uq_token_usage_calls_org_call_key', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_token_usage_calls_org_occurred', 'token_usage_calls', ['org_id', 'occurred_at'], unique=False)
    op.create_index('ix_token_usage_calls_session', 'token_usage_calls', ['jaah_session_id', 'occurred_at'], unique=False)
    op.create_index('uq_token_usage_calls_org_aws_request_id', 'token_usage_calls', ['org_id', 'aws_request_id'], unique=True, postgresql_where='(aws_request_id IS NOT NULL)')
    op.create_index('uq_token_usage_calls_org_node_call_key', 'token_usage_calls', ['org_id', 'node_call_key'], unique=True, postgresql_where='(node_call_key IS NOT NULL)')
    op.create_table('trust_document_requests',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('company', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('email', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('documents', postgresql.ARRAY(sa.TEXT()), autoincrement=False, nullable=False),
    sa.Column('purpose', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('nda_version', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('nda_accepted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('decided_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('decided_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('decline_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('notified_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('notify_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('slack_posted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('slack_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('ip_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('documents_sent_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('documents_sent_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint("state = ANY (ARRAY['pending'::text, 'approved'::text, 'declined'::text])", name='ck_trust_document_requests_state'),
    sa.ForeignKeyConstraint(['decided_by_id'], ['admin_users.id'], name='fk_trust_document_requests_decided_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['documents_sent_by_id'], ['admin_users.id'], name='fk_trust_document_requests_documents_sent_by_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_trust_document_requests'),
    sa.UniqueConstraint('id', 'org_id', name='uq_trust_document_requests_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_trust_document_requests_state_created_at', 'trust_document_requests', ['state', sa.literal_column('created_at DESC')], unique=False)
    op.create_table('agent_enablements',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('agent_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name='agent_enablements_agent_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='agent_enablements_created_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='agent_enablements_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_agent_enablements_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'agent_id', name='uq_agent_enablements_org_agent', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_agent_enablements_agent_id', 'agent_enablements', ['agent_id'], unique=False)
    op.create_table('api_key_secrets',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('api_key_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('token_hash', sa.CHAR(length=64), autoincrement=False, nullable=False),
    sa.Column('last4', sa.CHAR(length=4), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('valid_until', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], name='fk_api_key_secrets_api_key_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_api_key_secrets'),
    sa.UniqueConstraint('token_hash', name='uq_api_key_secrets_token_hash', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_api_key_secrets_api_key_id', 'api_key_secrets', ['api_key_id'], unique=False)
    op.create_table('billing_plan_products',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('plan_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('quantity', sa.INTEGER(), server_default=sa.text('1'), autoincrement=False, nullable=False),
    sa.Column('tier_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('instance_type', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('storage_gb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('credit_cents', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('sort_order', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('backfill_tasks', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.CheckConstraint("backfill_tasks IS NULL OR kind = 'live_cast'::text", name='ck_billing_plan_products_backfill_tasks'),
    sa.CheckConstraint("kind <> 'dedicated_node'::text OR instance_type IS NOT NULL AND storage_gb IS NOT NULL", name='ck_billing_plan_products_dedicated_node'),
    sa.CheckConstraint("kind <> 'jaah_ai_credit'::text OR credit_cents IS NOT NULL", name='ck_billing_plan_products_jaah_ai_credit'),
    sa.CheckConstraint("kind <> 'live_cast'::text OR COALESCE(backfill_tasks, 0) >= 1", name='ck_billing_plan_products_live_cast'),
    sa.CheckConstraint("kind <> 'shared_node'::text OR tier_key IS NOT NULL", name='ck_billing_plan_products_shared_node'),
    sa.CheckConstraint("kind = ANY (ARRAY['shared_node'::text, 'dedicated_node'::text, 'jaah_ai_credit'::text, 'live_cast'::text])", name='ck_billing_plan_products_kind'),
    sa.CheckConstraint('credit_cents IS NULL OR credit_cents >= 0', name='ck_billing_plan_products_credit_cents'),
    sa.CheckConstraint('quantity >= 1', name='ck_billing_plan_products_quantity'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_billing_plan_products_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['plan_id'], ['billing_plans.id'], name='fk_billing_plan_products_plan_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_billing_plan_products_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_billing_plan_products')
    )
    op.create_index('ix_billing_plan_products_plan_id', 'billing_plan_products', ['plan_id'], unique=False)
    op.create_table('cloud_node_defaults',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('region', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('ami_id', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('subnet_id', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('security_group_id', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('instance_profile', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('root_volume_gb', sa.INTEGER(), server_default=sa.text('50'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('shared_efs_id', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('shared_efs_access_point_id', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('shared_efs_mount_ip', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('provider', sa.TEXT(), server_default=sa.text("'aws'::text"), autoincrement=False, nullable=False),
    sa.Column('extra_subnet_ids', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('ami_pinned_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('network', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_cloud_node_defaults_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name='fk_cloud_node_defaults_credential_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_cloud_node_defaults_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_cloud_node_defaults')
    )
    op.create_index('ix_cloud_node_defaults_credential_id', 'cloud_node_defaults', ['credential_id'], unique=False)
    op.create_table('crons',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('schedule_kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('cron_expression', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('interval_seconds', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('schedule_anchor_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('activity_type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('activity_params', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('retention_mode', sa.TEXT(), server_default=sa.text("'all'::text"), autoincrement=False, nullable=False),
    sa.Column('min_gap_seconds', sa.NUMERIC(), autoincrement=False, nullable=True),
    sa.Column('timeout_seconds', sa.NUMERIC(), autoincrement=False, nullable=True),
    sa.Column('last_tick_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_fired_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('consecutive_skips', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('is_system', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('last_succeeded_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_by_task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('automation_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint("retention_mode = ANY (ARRAY['none'::text, 'acted'::text, 'all'::text])", name='ck_crons_retention_mode'),
    sa.CheckConstraint("schedule_kind = 'cron'::text AND cron_expression IS NOT NULL AND interval_seconds IS NULL OR schedule_kind = 'interval'::text AND interval_seconds IS NOT NULL AND cron_expression IS NULL", name='ck_crons_exactly_one_schedule'),
    sa.CheckConstraint("schedule_kind = ANY (ARRAY['cron'::text, 'interval'::text])", name='ck_crons_schedule_kind'),
    sa.CheckConstraint('cron_expression IS NULL OR length(cron_expression) <= 256', name='ck_crons_cron_expression_length'),
    sa.CheckConstraint('interval_seconds IS NULL OR interval_seconds > 0', name='ck_crons_interval_seconds_positive'),
    sa.CheckConstraint('min_gap_seconds IS NULL OR interval_seconds IS NULL OR min_gap_seconds < interval_seconds::numeric', name='ck_crons_min_gap_below_interval'),
    sa.CheckConstraint('min_gap_seconds IS NULL OR min_gap_seconds > 0::numeric', name='ck_crons_min_gap_seconds_positive'),
    sa.CheckConstraint('timeout_seconds IS NULL OR timeout_seconds > 0::numeric', name='ck_crons_timeout_seconds_positive'),
    sa.ForeignKeyConstraint(['automation_id'], ['automations.id'], name='crons_automation_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_crons_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_crons_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_crons'),
    sa.UniqueConstraint('automation_id', name='crons_automation_id_key', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_crons_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_crons_created_by_task_id', 'crons', ['created_by_task_id'], unique=False)
    op.create_index('uq_crons_system_activity_type', 'crons', ['activity_type'], unique=True, postgresql_where='is_system')
    op.create_table('gov_managed_pack_releases',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('changelog', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('released_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['pack_id'], ['gov_managed_packs.id'], name='fk_gov_managed_pack_releases_pack_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_managed_pack_releases'),
    sa.UniqueConstraint('pack_id', 'version', name='uq_gov_managed_pack_releases_version', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_packs',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('current_version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('levels', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('pillars', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('unlock_threshold', sa.NUMERIC(), server_default=sa.text('0.8'), autoincrement=False, nullable=False),
    sa.Column('parent_pack_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('parent_version', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('upstream_version', sa.TEXT(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_gov_packs_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['parent_pack_id'], ['gov_managed_packs.id'], name='fk_gov_packs_parent_pack_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_gov_packs_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_packs'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_packs_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'slug', name='uq_gov_packs_org_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_table('legal_acceptances',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('legal_version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('terms_url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('privacy_url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('accepted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('ip', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('user_agent', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('identity_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['identity_id'], ['identities.id'], name='fk_legal_acceptances_identity_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_legal_acceptances_user_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_legal_acceptances')
    )
    op.create_index('ix_legal_acceptances_identity_id', 'legal_acceptances', ['identity_id'], unique=False)
    op.create_index('ix_legal_acceptances_user_id', 'legal_acceptances', ['user_id'], unique=False)
    op.create_table('nodevault_operation_items',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('operation_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('phase', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('staged', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("phase = ANY (ARRAY['migrate'::text, 'rekey'::text])", name='ck_nodevault_operation_items_phase'),
    sa.CheckConstraint("state = ANY (ARRAY['pending'::text, 'sealed'::text, 'done'::text, 'skipped'::text, 'failed'::text])", name='ck_nodevault_operation_items_state'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name='fk_nodevault_operation_items_credential_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['operation_id'], ['nodevault_operations.id'], name='fk_nodevault_operation_items_operation_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_nodevault_operation_items'),
    sa.UniqueConstraint('operation_id', 'credential_id', 'phase', name='uq_nodevault_operation_items_op_credential_phase', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_nodevault_operation_items_credential_id', 'nodevault_operation_items', ['credential_id'], unique=False)
    op.create_table('org_invitations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('identity_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('grant', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('decided_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.CheckConstraint("state = ANY (ARRAY['pending'::text, 'accepted'::text, 'declined'::text])", name='ck_org_invitations_state'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_org_invitations_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['identity_id'], ['identities.id'], name='fk_org_invitations_identity_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_org_invitations_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_org_invitations'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_invitations_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_org_invitations_identity_id', 'org_invitations', ['identity_id'], unique=False)
    op.create_index('ix_org_invitations_org_id_created_at', 'org_invitations', ['org_id', 'created_at'], unique=False)
    op.create_index('uq_org_invitations_pending', 'org_invitations', ['org_id', 'identity_id'], unique=True, postgresql_where="(state = 'pending'::text)")
    op.create_table('org_shared_stores',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('region', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('subnet_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('ingress_cidr', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('file_system_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('file_system_arn', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('security_group_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('mount_target_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('mount_target_ip', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('access_point_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('access_point_arn', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('last_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('adopted', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('managed_location', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('policy_written_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('policy_access_point_arns', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('aws_account_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('provider', sa.TEXT(), server_default=sa.text("'aws'::text"), autoincrement=False, nullable=False),
    sa.Column('provider_state', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.CheckConstraint("managed_location IS NULL OR (managed_location = ANY (ARRAY['hosting_vpc'::text, 'main_vpc'::text]))", name='ck_org_shared_stores_managed_location'),
    sa.CheckConstraint("provider = ANY (ARRAY['aws'::text, 'gcp'::text, 'azure'::text])", name='ck_org_shared_stores_provider'),
    sa.CheckConstraint("status = ANY (ARRAY['pending'::text, 'provisioning'::text, 'ready'::text, 'failed'::text, 'deleting'::text])", name='ck_org_shared_stores_status'),
    sa.CheckConstraint('adopted OR ingress_cidr IS NOT NULL', name='ck_org_shared_stores_adopted_or_cidr'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_org_shared_stores_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name='fk_org_shared_stores_credential_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_org_shared_stores_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_org_shared_stores'),
    sa.UniqueConstraint('org_id', 'name', name='uq_org_shared_stores_org_name', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_org_shared_stores_credential_id', 'org_shared_stores', ['credential_id'], unique=False)
    op.create_index('uq_org_shared_stores_managed_location', 'org_shared_stores', ['managed_location'], unique=True, postgresql_where='(managed_location IS NOT NULL)')
    op.create_table('org_sso_connections',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('protocol', sa.VARCHAR(length=8), autoincrement=False, nullable=False),
    sa.Column('saml_idp_metadata_xml', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('saml_idp_metadata_url', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('oidc_credential_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('jit_enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('enforced', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('tested_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('tested_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.CheckConstraint("protocol IN ('saml', 'oidc')", name='ck_org_sso_connections_protocol'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_org_sso_connections_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['oidc_credential_id'], ['credentials.id'], name='fk_org_sso_connections_oidc_credential_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tested_by_id'], ['admin_users.id'], name='fk_org_sso_connections_tested_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_org_sso_connections_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_org_sso_connections'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_sso_connections_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', name='uq_org_sso_connections_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('orgs',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('default_role_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('lifecycle', sa.TEXT(), server_default=sa.text("'active'::text"), autoincrement=False, nullable=False),
    sa.Column('plan_chosen_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('limit_runner_slots', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_nodes', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_projects', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_users', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_active_worktrees', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('sso_provider', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('sso_tenant', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('sso_mfa_attested_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('sso_mfa_attested_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('colocation_allowed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('shared_store_allowed', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('cron_min_period_seconds', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('cron_min_period_own_seconds', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('mfa_skip', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('plan_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('stripe_plan_key', sa.TEXT(), server_default=sa.text("'-'::text"), autoincrement=False, nullable=True),
    sa.Column('onboarding_completed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('limit_github_runners', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('account_type', sa.TEXT(), server_default=sa.text("'organization'::text"), autoincrement=False, nullable=False),
    sa.Column('limit_agent_requests_per_minute', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_task_attachment_mb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('limit_org_attachment_gb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('plan_started_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('plan_end_overridden', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('plan_end_override', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('hosting_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('products_overridden', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('pending_plan_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('compliance_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('nodevault_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('nodelink_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('merge_lane_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('microvm_vcpus', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('microvm_ram_mib', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('microvm_disk_gib', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('microvm_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('advanced_compliance_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('hipaa_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('hipaa_baa_effective_date', sa.DATE(), autoincrement=False, nullable=True),
    sa.Column('hipaa_acks', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('playbooks_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('deleted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('deleted_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('ubuntu_nodes_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('macos_nodes_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('windows_nodes_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('project_memory_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('slack_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('sso_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('scim_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('siem_export_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('data_residency_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('ip_allowlist_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('org_retention_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('staging_org_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('budgets_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('agents_bought', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('billing_interval', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('slack_plan_drops', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('limit_attachment_file_mb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('retention_transcripts_days', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('retention_events_days', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('retention_artifacts_days', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('parent_org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('data_region', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('jaah_ai_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('min_agents_override', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('max_agents_override', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('static_slots_override', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('then_plan_overridden', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('then_plan_key_override', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('audit_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('session_transcripts_enabled_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('aws_cloud_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('gcp_cloud_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('azure_cloud_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('agents_trial_until', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('agents_trial_peak', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('tenant_email_allowed_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('providers_override', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.CheckConstraint("account_type = ANY (ARRAY['individual'::text, 'organization'::text])", name='ck_orgs_account_type'),
    sa.CheckConstraint("billing_interval IS NULL OR (billing_interval = ANY (ARRAY['month'::text, 'year'::text]))", name='ck_orgs_billing_interval'),
    sa.CheckConstraint("lifecycle = ANY (ARRAY['pending'::text, 'active'::text, 'rejected'::text])", name='ck_orgs_lifecycle'),
    sa.CheckConstraint("sso_provider IS NULL OR (sso_provider = ANY (ARRAY['microsoft'::text, 'google'::text]))", name='ck_orgs_sso_provider'),
    sa.CheckConstraint('(sso_provider IS NULL) = (sso_tenant IS NULL)', name='ck_orgs_sso_tenant'),
    sa.CheckConstraint('cron_min_period_own_seconds IS NULL OR cron_min_period_own_seconds > 0', name='ck_orgs_cron_min_period_own_seconds'),
    sa.CheckConstraint('cron_min_period_seconds IS NULL OR cron_min_period_seconds > 0', name='ck_orgs_cron_min_period_seconds'),
    sa.CheckConstraint('limit_active_worktrees IS NULL OR limit_active_worktrees >= 0', name='ck_orgs_limit_active_worktrees'),
    sa.CheckConstraint('limit_agent_requests_per_minute IS NULL OR limit_agent_requests_per_minute >= 0', name='ck_orgs_limit_agent_requests_per_minute'),
    sa.CheckConstraint('limit_attachment_file_mb IS NULL OR limit_attachment_file_mb >= 0', name='ck_orgs_limit_attachment_file_mb'),
    sa.CheckConstraint('limit_github_runners IS NULL OR limit_github_runners >= 0', name='ck_orgs_limit_github_runners'),
    sa.CheckConstraint('limit_nodes IS NULL OR limit_nodes >= 0', name='ck_orgs_limit_nodes'),
    sa.CheckConstraint('limit_org_attachment_gb IS NULL OR limit_org_attachment_gb >= 0', name='ck_orgs_limit_org_attachment_gb'),
    sa.CheckConstraint('limit_projects IS NULL OR limit_projects >= 0', name='ck_orgs_limit_projects'),
    sa.CheckConstraint('limit_runner_slots IS NULL OR limit_runner_slots >= 0', name='ck_orgs_limit_runner_slots'),
    sa.CheckConstraint('limit_task_attachment_mb IS NULL OR limit_task_attachment_mb >= 0', name='ck_orgs_limit_task_attachment_mb'),
    sa.CheckConstraint('limit_users IS NULL OR limit_users >= 0', name='ck_orgs_limit_users'),
    sa.CheckConstraint('max_agents_override IS NULL OR max_agents_override >= 0', name='ck_orgs_max_agents_override'),
    sa.CheckConstraint('min_agents_override IS NULL OR min_agents_override > 0', name='ck_orgs_min_agents_override'),
    sa.CheckConstraint('parent_org_id IS NULL OR parent_org_id <> id', name='ck_orgs_parent_not_self'),
    sa.CheckConstraint('sso_mfa_attested_at IS NULL OR sso_provider IS NOT NULL', name='ck_orgs_sso_attested'),
    sa.CheckConstraint('static_slots_override IS NULL OR static_slots_override >= 0', name='ck_orgs_static_slots_override'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_orgs_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['default_role_id'], ['roles.id'], name='fk_orgs_default_role_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['parent_org_id'], ['orgs.id'], name='fk_orgs_parent_org_id'),
    sa.ForeignKeyConstraint(['sso_mfa_attested_by_id'], ['admin_users.id'], name='fk_orgs_sso_mfa_attested_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['then_plan_key_override'], ['billing_plans.key'], name='fk_orgs_then_plan_key_override', onupdate='CASCADE', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_orgs_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_orgs'),
    sa.UniqueConstraint('slug', name='uq_orgs_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_orgs_lname_id', 'orgs', [sa.literal_column('lower(name)'), 'id'], unique=False)
    op.create_index('ix_orgs_name_trgm', 'orgs', [sa.literal_column('lower(name)')], unique=False, postgresql_ops={'lower(name)': 'gin_trgm_ops'}, postgresql_using='gin')
    op.create_index('ix_orgs_slug_trgm', 'orgs', [sa.literal_column('lower(slug)')], unique=False, postgresql_ops={'lower(slug)': 'gin_trgm_ops'}, postgresql_using='gin')
    op.create_index('uq_orgs_parent_org_id', 'orgs', ['parent_org_id'], unique=True, postgresql_where='((parent_org_id IS NOT NULL) AND (deleted_at IS NULL))')
    op.create_table('password_resets',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('token_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('requested_ip', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('identity_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['identity_id'], ['identities.id'], name='fk_password_resets_identity_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_password_resets_user_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_password_resets'),
    sa.UniqueConstraint('token_hash', name='uq_password_resets_token_hash', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_password_resets_identity_id', 'password_resets', ['identity_id'], unique=False)
    op.create_index('ix_password_resets_user_id', 'password_resets', ['user_id'], unique=False)
    op.create_table('projects',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('repo_url', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('fleet_enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('pool_max', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=True),
    sa.Column('port_vars', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=True),
    sa.Column('notes', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=True),
    sa.Column('ingested_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('default_model', sa.TEXT(), server_default=sa.text("'high'::text"), autoincrement=False, nullable=True),
    sa.Column('port_block', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('worktree_pool_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True),
    sa.Column('tech_stack', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('integration_branch', sa.TEXT(), server_default=sa.text("'develop'::text"), autoincrement=False, nullable=False),
    sa.Column('branch_prefix', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('gate_quick_cmd', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('gate_full_cmd', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('push_enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('stack_up_cmd', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('stack_down_cmd', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('business_analyst_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('architect_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('jira_credential_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('agent_instructions', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('forge', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('forge_base_url', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('default_static_slots', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('verification_slots', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('system_kind', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('microvm_vcpus', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('microvm_ram_mib', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('microvm_disk_gib', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('egress_allow', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('egress_deny', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('merge_lane_develop_red', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('default_agent_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('default_provider', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('merge_lane_sweep', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('merge_lane_batch_max', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('prefer_static_nodes', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.CheckConstraint("system_kind = 'nodes'::text", name='ck_projects_system_kind'),
    sa.CheckConstraint('NOT (default_agent_id IS NOT NULL AND default_provider IS NOT NULL)', name='ck_projects_default_agent_or_provider'),
    sa.CheckConstraint('default_static_slots >= 0', name='ck_projects_default_static_slots'),
    sa.CheckConstraint('system_kind IS NULL OR repo_url IS NULL', name='ck_projects_system_kind_no_repo'),
    sa.CheckConstraint('verification_slots IS NULL OR verification_slots >= 1', name='ck_projects_verification_slots'),
    sa.ForeignKeyConstraint(['architect_id'], ['admin_users.id'], name='fk_projects_architect', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['business_analyst_id'], ['admin_users.id'], name='fk_projects_business_analyst', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_projects_created_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['default_agent_id'], ['agents.id'], name='projects_default_agent_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['jira_credential_id'], ['credentials.id'], name='fk_projects_jira_credential_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='projects_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_projects_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'name', name='uq_projects_org_name', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'port_block', name='uq_projects_org_id_port_block', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_projects_org_id_system_kind', 'projects', ['org_id', 'system_kind'], unique=True, postgresql_where='(system_kind IS NOT NULL)')
    op.create_table('support_messages',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('conversation_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('role', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('content', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('tool_calls', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('provider', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('latency_ms', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('input_tokens', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('output_tokens', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('redaction_hits', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('tool_exchange', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.CheckConstraint("role = ANY (ARRAY['user'::text, 'assistant'::text])", name='ck_support_messages_role'),
    sa.ForeignKeyConstraint(['conversation_id'], ['support_conversations.id'], name='fk_support_messages_conversation_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_support_messages')
    )
    op.create_index('ix_support_messages_conversation_created', 'support_messages', ['conversation_id', 'created_at'], unique=False)
    op.create_table('tags',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('tag_group_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('tag', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('position', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='tags_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tag_group_id'], ['tag_groups.id'], name='tags_tag_group_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='tags_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='tags_pkey'),
    sa.UniqueConstraint('tag_group_id', 'tag', name='uq_tags_group_tag', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_tags_tag_group_id', 'tags', ['tag_group_id'], unique=False)
    op.create_table('telegram_links',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('identity_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('chat_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('linked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('chat_label', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('code_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('code_expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('code_admin_user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['code_admin_user_id'], ['admin_users.id'], name='telegram_links_code_admin_user_id_fkey', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['identity_id'], ['identities.id'], name='telegram_links_identity_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='telegram_links_pkey'),
    sa.UniqueConstraint('identity_id', name='uq_telegram_links_identity_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('uq_telegram_links_code_hash', 'telegram_links', ['code_hash'], unique=True, postgresql_where='(code_hash IS NOT NULL)')
    op.create_table('tickets',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('title', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'open'::text"), autoincrement=False, nullable=False),
    sa.Column('priority', sa.TEXT(), server_default=sa.text("'normal'::text"), autoincrement=False, nullable=False),
    sa.Column('requester_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('conversation_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('last_public_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_reply_side', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last_customer_activity_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_responder_public_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('lesson_status', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('lesson_problem', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('lesson_solution', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("last_reply_side IS NULL OR (last_reply_side = ANY (ARRAY['user'::text, 'staff'::text]))", name='ck_tickets_last_reply_side'),
    sa.CheckConstraint("lesson_status IS NULL OR (lesson_status = ANY (ARRAY['pending'::text, 'draft'::text, 'approved'::text, 'dismissed'::text]))", name='ck_tickets_lesson_status'),
    sa.CheckConstraint("priority = ANY (ARRAY['low'::text, 'normal'::text, 'high'::text, 'urgent'::text])", name='ck_tickets_priority'),
    sa.CheckConstraint("status = ANY (ARRAY['open'::text, 'pending'::text, 'solved'::text, 'closed'::text])", name='ck_tickets_status'),
    sa.CheckConstraint('char_length(title) <= 200', name='ck_tickets_title_length'),
    sa.ForeignKeyConstraint(['conversation_id'], ['support_conversations.id'], name='fk_tickets_conversation_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_tickets_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requester_id'], ['admin_users.id'], name='fk_tickets_requester_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_tickets_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_tickets'),
    sa.UniqueConstraint('id', 'org_id', name='uq_tickets_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_tickets_org_status_updated', 'tickets', ['org_id', 'status', sa.literal_column('updated_at DESC')], unique=False)
    op.create_index('ix_tickets_requester_updated', 'tickets', ['requester_id', sa.literal_column('updated_at DESC')], unique=False)
    op.create_index('uq_tickets_conversation_id', 'tickets', ['conversation_id'], unique=True, postgresql_where='(conversation_id IS NOT NULL)')
    op.create_table('user_git_connections',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('root', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('forge', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.CheckConstraint("forge = ANY (ARRAY['github'::text, 'gitlab'::text, 'bitbucket'::text, 'azure_devops'::text])", name='ck_user_git_connections_forge'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_user_git_connections_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name='fk_user_git_connections_credential_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_user_git_connections_updated_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_user_git_connections_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_user_git_connections'),
    sa.UniqueConstraint('user_id', 'root', name='uq_user_git_connections_user_root', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_user_git_connections_credential_id', 'user_git_connections', ['credential_id'], unique=False)
    op.create_table('user_group_memberships',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('group_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('resource_type', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('resource_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['groups.id'], name='user_group_memberships_group_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='user_group_memberships_user_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='user_group_memberships_pkey'),
    sa.UniqueConstraint('user_id', 'group_id', 'resource_type', 'resource_id', name='uq_user_group_memberships_user_group_resource', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_user_group_memberships_resource', 'user_group_memberships', ['resource_type', 'resource_id'], unique=False)
    op.create_index('ix_user_group_memberships_user_id', 'user_group_memberships', ['user_id'], unique=False)
    op.create_table('user_mfa_factors',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('secret_sealed', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('confirmed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_used_step', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('pending_nonce', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('pending_failures', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('last_failure_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('identity_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('key_user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['identity_id'], ['identities.id'], name='fk_user_mfa_factors_identity_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_user_mfa_factors_user_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_user_mfa_factors'),
    sa.UniqueConstraint('identity_id', name='uq_user_mfa_factors_identity_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('user_id', name='uq_user_mfa_factors_user_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('user_recovery_codes',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('code_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('identity_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('key_user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['identity_id'], ['identities.id'], name='fk_user_recovery_codes_identity_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_user_recovery_codes_user_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_user_recovery_codes'),
    sa.UniqueConstraint('user_id', 'code_hash', name='uq_user_recovery_codes_user_hash', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_user_recovery_codes_identity_id', 'user_recovery_codes', ['identity_id'], unique=False)
    op.create_index('ix_user_recovery_codes_user_id', 'user_recovery_codes', ['user_id'], unique=False)
    op.create_table('user_trusted_devices',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('token_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('label', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('last_used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('identity_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['identity_id'], ['identities.id'], name='fk_user_trusted_devices_identity_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_user_trusted_devices_user_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_user_trusted_devices'),
    sa.UniqueConstraint('token_hash', name='uq_user_trusted_devices_token_hash', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_user_trusted_devices_identity_id', 'user_trusted_devices', ['identity_id'], unique=False)
    op.create_index('ix_user_trusted_devices_user_id', 'user_trusted_devices', ['user_id'], unique=False)
    op.create_table('budgets',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('group_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('monthly_usd_micros', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('warned_month', sa.DATE(), autoincrement=False, nullable=True),
    sa.Column('held_month', sa.DATE(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.CheckConstraint('(project_id IS NOT NULL) <> (group_id IS NOT NULL)', name='ck_budgets_one_scope'),
    sa.CheckConstraint('monthly_usd_micros > 0', name='ck_budgets_monthly_usd_micros_positive'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_budgets_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['group_id'], ['groups.id'], name='fk_budgets_group_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_budgets_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_budgets_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_budgets')
    )
    op.create_index('uq_budgets_group_id', 'budgets', ['group_id'], unique=True, postgresql_where='(group_id IS NOT NULL)')
    op.create_index('uq_budgets_project_id', 'budgets', ['project_id'], unique=True, postgresql_where='(project_id IS NOT NULL)')
    op.create_table('features',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('lead_id', sa.UUID(), autoincrement=False, nullable=False, comment="The feature's lead, shown as Owner. Not owner_id: owner means access here, and this column grants none."),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'active'::text"), autoincrement=False, nullable=False),
    sa.Column('shipped_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('shipped_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('position', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('icon', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('show_progress', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.CheckConstraint("(status = 'shipped'::text) = (shipped_at IS NOT NULL AND shipped_by_id IS NOT NULL)", name='ck_features_shipped_stamped'),
    sa.CheckConstraint("icon IS NULL OR (icon = ANY (ARRAY['dot-red'::text, 'dot-orange'::text, 'dot-yellow'::text, 'dot-green'::text, 'dot-teal'::text, 'dot-blue'::text, 'dot-purple'::text, 'dot-pink'::text, 'square-red'::text, 'square-orange'::text, 'square-yellow'::text, 'square-green'::text, 'square-teal'::text, 'square-blue'::text, 'square-purple'::text, 'square-pink'::text, 'triangle-red'::text, 'triangle-orange'::text, 'triangle-yellow'::text, 'triangle-green'::text, 'triangle-teal'::text, 'triangle-blue'::text, 'triangle-purple'::text, 'triangle-pink'::text]))", name='ck_features_icon'),
    sa.CheckConstraint("status = ANY (ARRAY['active'::text, 'shipped'::text])", name='ck_features_status'),
    sa.CheckConstraint('(shipped_at IS NULL) = (shipped_by_id IS NULL)', name='ck_features_shipped_pair'),
    sa.CheckConstraint('char_length(btrim(name)) >= 1 AND char_length(btrim(name)) <= 200', name='ck_features_name_length'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_features_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['lead_id'], ['admin_users.id'], name='fk_features_lead_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_features_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['shipped_by_id'], ['admin_users.id'], name='fk_features_shipped_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_features_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_features'),
    sa.UniqueConstraint('id', 'project_id', name='uq_features_id_project', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_features_project_status', 'features', ['project_id', 'status'], unique=False)
    op.create_table('gov_campaign_items',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('campaign_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('ordinal', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['gov_campaigns.id'], name='fk_gov_campaign_items_campaign_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_gov_campaign_items_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_campaign_items'),
    sa.UniqueConstraint('campaign_id', 'project_id', name='uq_gov_campaign_items_campaign_project', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_entities',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('attrs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("type = ANY (ARRAY['project'::text, 'repo'::text, 'service'::text, 'path'::text, 'team'::text, 'person'::text, 'agent'::text, 'model'::text, 'data_class'::text, 'environment'::text])", name='ck_gov_entities_type'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_gov_entities_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_entities'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_entities_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'type', 'key', name='uq_gov_entities_org_type_key', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_gov_entities_project_id', 'gov_entities', ['project_id'], unique=False)
    op.create_table('gov_exceptions',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('rule_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('scope', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('owner_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('compensating_control', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'active'::text"), autoincrement=False, nullable=False),
    sa.Column('revisit_notified_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("status = ANY (ARRAY['active'::text, 'expired'::text, 'revoked'::text])", name='ck_gov_exceptions_status'),
    sa.CheckConstraint('num_nonnulls(project_id, scope) = 1', name='ck_gov_exceptions_one_scope'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_gov_exceptions_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['owner_id'], ['admin_users.id'], name='fk_gov_exceptions_owner_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_gov_exceptions_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_gov_exceptions_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_exceptions'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_exceptions_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_gov_exceptions_due', 'gov_exceptions', ['expires_at'], unique=False, postgresql_where="(status = 'active'::text)")
    op.create_table('gov_pack_releases',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('version', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('changelog', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('released_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['pack_id'], ['gov_packs.id'], name='fk_gov_pack_releases_pack_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_pack_releases'),
    sa.UniqueConstraint('pack_id', 'version', name='uq_gov_pack_releases_version', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_remediations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('rule_keys', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('campaign_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_gov_remediations_created_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_gov_remediations_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_remediations'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_remediations_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_gov_remediations_project', 'gov_remediations', ['project_id', 'pack_id'], unique=False)
    op.create_table('gov_rules',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('version', sa.INTEGER(), server_default=sa.text('1'), autoincrement=False, nullable=False),
    sa.Column('subject_kind', sa.TEXT(), server_default=sa.text("'structural'::text"), autoincrement=False, nullable=False),
    sa.Column('owner_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('intent', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('rationale', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('pillar', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('level', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('applies_when', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text('\'{"all": []}\'::jsonb'), autoincrement=False, nullable=False),
    sa.Column('na_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('scope', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text('\'{"all": []}\'::jsonb'), autoincrement=False, nullable=False),
    sa.Column('projections', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text('\'{"audit": {}}\'::jsonb'), autoincrement=False, nullable=False),
    sa.Column('check', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('oracles', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('mode', sa.TEXT(), server_default=sa.text("'advise'::text"), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('exceptions_policy', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text('\'{"max_days": 180, "waivable": true}\'::jsonb'), autoincrement=False, nullable=False),
    sa.Column('guidance', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("mode = ANY (ARRAY['observe'::text, 'advise'::text, 'nudge'::text, 'gate'::text, 'lock'::text])", name='ck_gov_rules_mode'),
    sa.CheckConstraint('level >= 1 AND level <= 5', name='ck_gov_rules_level'),
    sa.CheckConstraint('version >= 1', name='ck_gov_rules_version'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_gov_rules_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['owner_id'], ['admin_users.id'], name='fk_gov_rules_owner_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['pack_id'], ['gov_packs.id'], name='fk_gov_rules_pack_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_gov_rules_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_rules'),
    sa.UniqueConstraint('pack_id', 'key', name='uq_gov_rules_pack_key', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_scan_targets',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('scan_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('ledger_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('project_repo_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("status = ANY (ARRAY['not_scannable_no_repo'::text, 'not_scannable_no_node'::text, 'queued'::text, 'running'::text, 'done'::text, 'error'::text])", name='ck_gov_scan_targets_status'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_gov_scan_targets_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['scan_id'], ['gov_scans.id'], name='fk_gov_scan_targets_scan_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_scan_targets')
    )
    op.create_index('ix_gov_scan_targets_task_id', 'gov_scan_targets', ['task_id'], unique=False)
    op.create_index('uq_gov_scan_targets_scan_project_repo', 'gov_scan_targets', ['scan_id', 'project_id', sa.literal_column('COALESCE(project_repo_id, 0::bigint)')], unique=True)
    op.create_table('gov_twin_cells',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('rule_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('mode', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('verdict', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('confidence', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('first_failed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('prev_state', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('ledger_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('exception_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_gov_twin_cells_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_twin_cells'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_twin_cells_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('project_id', 'pack_id', 'rule_key', name='uq_gov_twin_cells_project_pack_rule', postgresql_nulls_not_distinct=False)
    )
    op.create_table('gov_twin_repos',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('pack_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('level', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('pillars', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('debt_count', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('ledger_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('scanned_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_gov_twin_repos_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_twin_repos'),
    sa.UniqueConstraint('id', 'org_id', name='uq_gov_twin_repos_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('project_id', 'pack_id', name='uq_gov_twin_repos_project_pack', postgresql_nulls_not_distinct=False)
    )
    op.create_table('live_cast_items',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('item_kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('item_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('verdict', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('rules_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('content_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('evaluated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('attempts', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('last_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('override', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('override_by', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('override_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('in_scope', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.CheckConstraint("item_kind = ANY (ARRAY['task'::text, 'attachment'::text])", name='ck_live_cast_items_item_kind'),
    sa.CheckConstraint("override IS NULL OR (override = ANY (ARRAY['published'::text, 'hidden'::text]))", name='ck_live_cast_items_override'),
    sa.CheckConstraint("verdict = ANY (ARRAY['pending'::text, 'published'::text, 'hidden'::text])", name='ck_live_cast_items_verdict'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_live_cast_items_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_live_cast_items'),
    sa.UniqueConstraint('item_kind', 'item_id', name='uq_live_cast_items_item', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_live_cast_items_project', 'live_cast_items', ['project_id', 'item_kind'], unique=False)
    op.create_index('ix_live_cast_items_task', 'live_cast_items', ['task_id'], unique=False)
    op.create_table('live_cast_projects',
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('default_mode', sa.TEXT(), server_default=sa.text("'all'::text"), autoincrement=False, nullable=False),
    sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('published_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('rules_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('backfill', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('short_name', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('featured', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.CheckConstraint("default_mode = ANY (ARRAY['all'::text, 'none'::text])", name='ck_live_cast_projects_default_mode'),
    sa.CheckConstraint("jsonb_typeof(rules) = 'array'::text AND jsonb_array_length(rules) <= 20", name='ck_live_cast_projects_rules'),
    sa.CheckConstraint('published_at IS NULL OR rules_sha IS NOT NULL', name='ck_live_cast_projects_frozen'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='live_cast_projects_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_live_cast_projects_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='live_cast_projects_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('project_id', name='pk_live_cast_projects'),
    sa.UniqueConstraint('org_id', 'slug', name='uq_live_cast_projects_org_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_live_cast_projects_published', 'live_cast_projects', ['org_id'], unique=False, postgresql_where='(published_at IS NOT NULL)')
    op.create_index('uq_live_cast_projects_featured', 'live_cast_projects', ['featured'], unique=True, postgresql_where='featured')
    op.create_index('uq_live_cast_projects_short_name', 'live_cast_projects', ['short_name'], unique=True)
    op.create_table('memory_claims',
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('sha', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('job', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('claimed_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('done_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("job = ANY (ARRAY['verify'::text, 'codemap'::text])", name='ck_memory_claims_job'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_memory_claims_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', 'sha', 'job', name='pk_memory_claims')
    )
    op.create_table('memory_rejections',
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('feed', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('day', sa.DATE(), server_default=sa.text('CURRENT_DATE'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('count', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.CheckConstraint("feed = ANY (ARRAY['propose'::text, 'settle'::text, 'stop_delta'::text, 'distill'::text, 'codemap'::text])", name='ck_memory_rejections_feed'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_memory_rejections_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', 'feed', 'reason', 'day', name='pk_memory_rejections')
    )
    op.create_table('merge_lane_certificates',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('tree_hash', sa.VARCHAR(length=40), autoincrement=False, nullable=False),
    sa.Column('gate_cmd_hash', sa.VARCHAR(length=64), autoincrement=False, nullable=False),
    sa.Column('develop_sha', sa.VARCHAR(length=40), autoincrement=False, nullable=False),
    sa.Column('lane_task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('pr_numbers', postgresql.ARRAY(sa.INTEGER()), server_default=sa.text("'{}'::integer[]"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_merge_lane_certificates_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_merge_lane_certificates'),
    sa.UniqueConstraint('id', 'org_id', name='uq_merge_lane_certificates_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('project_id', 'tree_hash', 'gate_cmd_hash', name='uq_merge_lane_certificates_project_tree_gate', postgresql_nulls_not_distinct=False)
    )
    op.create_table('merge_lane_flakes',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('test_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('tree_hash', sa.VARCHAR(length=40), autoincrement=False, nullable=False),
    sa.Column('lane_task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('fix_task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_merge_lane_flakes_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_merge_lane_flakes'),
    sa.UniqueConstraint('id', 'org_id', name='uq_merge_lane_flakes_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_merge_lane_flakes_project_test', 'merge_lane_flakes', ['project_id', 'test_id'], unique=False)
    op.create_table('node_groups',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('last_reconciled_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_error', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('connection_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('nodelinks_none', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['connection_id'], ['cloud_node_defaults.id'], name='fk_node_groups_connection_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_node_groups_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name='fk_node_groups_credential_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_node_groups_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_node_groups'),
    sa.UniqueConstraint('id', 'org_id', name='uq_node_groups_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_node_groups_connection_id', 'node_groups', ['connection_id'], unique=False)
    op.create_index('ix_node_groups_credential_id', 'node_groups', ['credential_id'], unique=False)
    op.create_table('nodelinks',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('store_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('kind', sa.TEXT(), server_default=sa.text("'general'::text"), autoincrement=False, nullable=False),
    sa.Column('mount_path', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('root_directory', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('access_point_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('access_point_arn', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('last_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('in_policy', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('pending_unmount_node_ids', postgresql.ARRAY(sa.UUID()), autoincrement=False, nullable=True),
    sa.Column('deleting_since', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('access_point_adopted', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('provider_state', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.CheckConstraint("kind = ANY (ARRAY['accounts'::text, 'general'::text])", name='ck_nodelinks_kind'),
    sa.CheckConstraint("mount_path ~~ '{jaah-home}/%'::text", name='ck_nodelinks_mount_path'),
    sa.CheckConstraint("status = ANY (ARRAY['pending'::text, 'provisioning'::text, 'ready'::text, 'failed'::text, 'deleting'::text])", name='ck_nodelinks_status'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='nodelinks_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['store_id'], ['org_shared_stores.id'], name='nodelinks_store_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='nodelinks_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='nodelinks_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_nodelinks_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'name', name='uq_nodelinks_org_name', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'slug', name='uq_nodelinks_org_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_nodelinks_store_id', 'nodelinks', ['store_id'], unique=False)
    op.create_index('uq_nodelinks_org_accounts', 'nodelinks', ['org_id'], unique=True, postgresql_where="(kind = 'accounts'::text)")
    op.create_table('org_transfer_carryovers',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('transfer_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('worktree_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('launch_session_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('from_org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('to_org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('ended_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['from_org_id'], ['orgs.id'], name='org_transfer_carryovers_from_org_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['to_org_id'], ['orgs.id'], name='org_transfer_carryovers_to_org_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='org_transfer_carryovers_pkey')
    )
    op.create_index('ix_org_transfer_carryovers_open_node_task', 'org_transfer_carryovers', ['node_id', 'task_id'], unique=False, postgresql_where='(ended_at IS NULL)')
    op.create_index('uq_org_transfer_carryovers_open_task', 'org_transfer_carryovers', ['task_id'], unique=True, postgresql_where='(ended_at IS NULL)')
    op.create_table('project_credentials',
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_project_credentials_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name='fk_project_credentials_credential_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_project_credentials_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', 'credential_id', name='pk_project_credentials')
    )
    op.create_table('project_env_vars',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('source', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('literal', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('credential_field', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("credential_field IS NULL OR credential_field ~ '^[^\\n\\r]+$'::text", name='ck_project_env_vars_credential_field'),
    sa.CheckConstraint("literal IS NULL OR literal ~ '^[^\\n\\r]+$'::text", name='ck_project_env_vars_literal'),
    sa.CheckConstraint("name ~ '^[A-Za-z_][A-Za-z0-9_]{0,127}$'::text", name='ck_project_env_vars_name'),
    sa.CheckConstraint("source = 'literal'::text AND literal IS NOT NULL AND credential_id IS NULL AND credential_field IS NULL OR source = 'credential'::text AND literal IS NULL AND credential_field IS NOT NULL", name='ck_project_env_vars_shape'),
    sa.CheckConstraint("source = ANY (ARRAY['literal'::text, 'credential'::text])", name='ck_project_env_vars_source'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_project_env_vars_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name='fk_project_env_vars_credential_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_project_env_vars_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_project_env_vars_updated_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_project_env_vars_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_project_env_vars')
    )
    op.create_index('ix_project_env_vars_credential_id', 'project_env_vars', ['credential_id'], unique=False)
    op.create_index('ix_project_env_vars_project_id', 'project_env_vars', ['project_id'], unique=False)
    op.create_index('ix_project_env_vars_user_id', 'project_env_vars', ['user_id'], unique=False)
    op.create_index('uq_project_env_vars_default', 'project_env_vars', ['project_id', 'name'], unique=True, postgresql_where='(user_id IS NULL)')
    op.create_index('uq_project_env_vars_override', 'project_env_vars', ['project_id', 'name', 'user_id'], unique=True, postgresql_where='(user_id IS NOT NULL)')
    op.create_table('project_memory_canon',
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('route_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('doc_path', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('triggers', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('directive_text', sa.TEXT(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_project_memory_canon_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', 'route_id', name='pk_project_memory_canon')
    )
    op.create_table('project_memory_state',
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('next_number', sa.INTEGER(), server_default=sa.text('1'), autoincrement=False, nullable=False),
    sa.Column('bundle_revision', sa.BIGINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('codemap_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('verified_head', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('canon_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_project_memory_state_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', name='pk_project_memory_state')
    )
    op.create_table('project_repos',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('repo_url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('forge', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('forge_base_url', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('integration_branch', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('is_primary', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('position', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_project_repos_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_project_repos_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_project_repos_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_project_repos')
    )
    op.create_index('ix_project_repos_project_id', 'project_repos', ['project_id'], unique=False)
    op.create_index('uq_project_repos_primary', 'project_repos', ['project_id'], unique=True, postgresql_where='is_primary')
    op.create_index('uq_project_repos_project_url', 'project_repos', ['project_id', sa.literal_column('lower(btrim(repo_url))')], unique=True)
    op.create_table('support_docs_misses',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('conversation_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('message_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('question', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('question_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('requester_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("reason = ANY (ARRAY['no_passages'::text, 'uncovered'::text])", name='ck_support_docs_misses_reason'),
    sa.ForeignKeyConstraint(['conversation_id'], ['support_conversations.id'], name='fk_support_docs_misses_conversation_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['support_messages.id'], name='fk_support_docs_misses_message_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['requester_id'], ['admin_users.id'], name='fk_support_docs_misses_requester_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_support_docs_misses'),
    sa.UniqueConstraint('message_id', name='uq_support_docs_misses_message_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_support_docs_misses_created_at', 'support_docs_misses', ['created_at'], unique=False)
    op.create_index('ix_support_docs_misses_question_key', 'support_docs_misses', ['question_key'], unique=False)
    op.create_table('ticket_reads',
    sa.Column('ticket_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('last_read_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name='fk_ticket_reads_ticket_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_ticket_reads_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('ticket_id', 'user_id', name='pk_ticket_reads')
    )
    op.create_table('gov_edges',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('src_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('dst_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('source', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('observed_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("status = ANY (ARRAY['inferred'::text, 'confirmed'::text])", name='ck_gov_edges_status'),
    sa.ForeignKeyConstraint(['dst_id'], ['gov_entities.id'], name='fk_gov_edges_dst_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['src_id'], ['gov_entities.id'], name='fk_gov_edges_src_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_edges'),
    sa.UniqueConstraint('src_id', 'dst_id', 'type', name='uq_gov_edges_src_dst_type', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_gov_edges_dst_id', 'gov_edges', ['dst_id'], unique=False)
    op.create_table('gov_facts',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('entity_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('predicate', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('value', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('source', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('observed_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('confirmed_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('confirmed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("status = ANY (ARRAY['inferred'::text, 'confirmed'::text, 'rejected'::text])", name='ck_gov_facts_status'),
    sa.ForeignKeyConstraint(['confirmed_by_id'], ['admin_users.id'], name='fk_gov_facts_confirmed_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['entity_id'], ['gov_entities.id'], name='fk_gov_facts_entity_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_gov_facts'),
    sa.UniqueConstraint('entity_id', 'predicate', 'value', name='uq_gov_facts_entity_predicate_value', postgresql_nulls_not_distinct=False)
    )
    op.create_table('node_group_capacity',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('group_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('market', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('instance_type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('desired', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.CheckConstraint("market = ANY (ARRAY['on_demand'::text, 'spot'::text])", name='ck_node_group_capacity_market'),
    sa.CheckConstraint('desired >= 0 AND desired <= 100', name='ck_node_group_capacity_desired'),
    sa.ForeignKeyConstraint(['group_id'], ['node_groups.id'], name='fk_node_group_capacity_group_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_node_group_capacity'),
    sa.UniqueConstraint('group_id', 'market', 'instance_type', name='uq_node_group_capacity_group_market_type', postgresql_nulls_not_distinct=False)
    )
    op.create_table('node_group_nodelinks',
    sa.Column('node_group_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('nodelink_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='node_group_nodelinks_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_group_id'], ['node_groups.id'], name='node_group_nodelinks_node_group_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['nodelink_id'], ['nodelinks.id'], name='node_group_nodelinks_nodelink_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_group_id', 'nodelink_id', name='node_group_nodelinks_pkey')
    )
    op.create_index('ix_node_group_nodelinks_nodelink_id', 'node_group_nodelinks', ['nodelink_id'], unique=False)
    op.create_table('node_group_projects',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('group_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('worktrees_per_node', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.CheckConstraint('worktrees_per_node >= 0', name='ck_node_group_projects_worktrees_per_node'),
    sa.ForeignKeyConstraint(['group_id'], ['node_groups.id'], name='fk_node_group_projects_group_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_node_group_projects_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_node_group_projects'),
    sa.UniqueConstraint('group_id', 'project_id', name='uq_node_group_projects_group_project', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_node_group_projects_project_id', 'node_group_projects', ['project_id'], unique=False)
    op.create_table('project_memory_repo_state',
    sa.Column('project_repo_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('codemap_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('verified_head', sa.TEXT(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_project_memory_repo_state_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_repo_id'], ['project_repos.id'], name='fk_project_memory_repo_state_project_repo_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_repo_id', name='pk_project_memory_repo_state')
    )
    op.create_index('ix_project_memory_repo_state_project_id', 'project_memory_repo_state', ['project_id'], unique=False)
    op.create_table('nodes',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('role', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('capacity', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('infra', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('config_snapshot', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('config_reported_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('first_seen', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('last_seen', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('observed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('runners_updated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('worktrees_updated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('config_desired', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('shared_accounts_enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('node_token_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('node_token_issued_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('node_token_used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('node_group_capacity_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('runner_clis', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('shared_accounts_root', sa.TEXT(), server_default=sa.text("'/mnt/jaah-shared/accounts'::text"), autoincrement=False, nullable=False),
    sa.Column('draining', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('tailnet_reenrol_until', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('egress_ipv4', postgresql.INET(), autoincrement=False, nullable=True),
    sa.Column('egress_ipv4_reported_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('model_catalogs', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('jaah_home', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('os', sa.TEXT(), server_default=sa.text("'ubuntu'::text"), autoincrement=False, nullable=False),
    sa.Column('arch', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('os_version', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('os_reported', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('autoupdate_override', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('nodevault_identity_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('nodevault_identity_fp', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('nodevault_capable', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('nodevault_identity_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('org_transfer_cleanup_pending', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('virtualization', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('kvm', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('disk_gate', sa.TEXT(), server_default=sa.text("'open'::text"), autoincrement=False, nullable=False),
    sa.Column('disk_gate_changed_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('microvm', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('microvm_vcpus', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('microvm_ram_mib', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('microvm_disk_gib', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('launch_kinds', postgresql.ARRAY(sa.TEXT()), autoincrement=False, nullable=True),
    sa.Column('operator_allocated', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('log_findings', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('update_history', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('gha_recent_jobs', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('recent_errors', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('senses_acp_adapters', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('pending_node_token_hash', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('nodevault_capable_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("disk_gate = ANY (ARRAY['open'::text, 'paused'::text])", name='ck_nodes_disk_gate'),
    sa.CheckConstraint("os = ANY (ARRAY['ubuntu'::text, 'macos'::text, 'windows'::text])", name='ck_nodes_os'),
    sa.CheckConstraint("os_reported = ANY (ARRAY['linux'::text, 'macos'::text, 'windows'::text])", name='ck_nodes_os_reported'),
    sa.CheckConstraint('NOT (draining AND NOT enabled)', name='ck_nodes_draining_implies_enabled'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_nodes_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_group_capacity_id'], ['node_group_capacity.id'], name='fk_nodes_node_group_capacity_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_nodes_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='nodes_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_nodes_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_nodes_node_group_capacity_id', 'nodes', ['node_group_capacity_id'], unique=False, postgresql_where='(node_group_capacity_id IS NOT NULL)')
    op.create_index('uq_nodes_node_token_hash', 'nodes', ['node_token_hash'], unique=True, postgresql_where='(node_token_hash IS NOT NULL)')
    op.create_index('uq_nodes_pending_node_token_hash', 'nodes', ['pending_node_token_hash'], unique=True, postgresql_where='(pending_node_token_hash IS NOT NULL)')
    op.create_table('accounts',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('quota_status', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('quota_updated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('quota_status_until', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('shared', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('auth_kind', sa.TEXT(), server_default=sa.text("'oauth'::text"), autoincrement=False, nullable=False),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('provider', sa.TEXT(), server_default=sa.text("'claude'::text"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('auto_assign', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('max_runners', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('owner_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('signed_in_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.Column('quota_status_detail', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('node_removed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("auth_kind <> 'api_key'::text OR credential_id IS NOT NULL", name='ck_accounts_api_key_has_credential'),
    sa.CheckConstraint('max_runners IS NULL OR max_runners > 0', name='ck_accounts_max_runners_positive'),
    sa.CheckConstraint('quota_status IS NULL OR quota_status_until IS NOT NULL', name='ck_accounts_quota_status_has_a_deadline'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_accounts_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name='fk_accounts_credential_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_accounts_node_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_id'], ['admin_users.id'], name='fk_accounts_owner_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_accounts_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='accounts_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_accounts_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('slug', name='uq_accounts_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_table('alerts',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('repo', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('issue', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('window_start', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('question_id', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_alerts_node_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='alerts_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_alerts_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('repo', 'issue', 'kind', 'window_start', 'task_id', 'question_id', name='uq_alerts_dedup', postgresql_nulls_not_distinct=False)
    )
    op.create_table('command_audit_log',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('command_type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('args', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('actor', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('completed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_command_audit_log_node_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='command_audit_log_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_command_audit_log_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_command_audit_log_created_at', 'command_audit_log', ['created_at'], unique=False)
    op.create_table('credential_oauth_flows',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('type_slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('driver', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_credential_oauth_flows_node_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_credential_oauth_flows_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_credential_oauth_flows')
    )
    op.create_index('ix_credential_oauth_flows_user_id', 'credential_oauth_flows', ['user_id'], unique=False)
    op.create_index('uq_credential_oauth_flows_live_per_user', 'credential_oauth_flows', ['user_id'], unique=True, postgresql_where='(error IS NULL)')
    op.create_table('fetch_stamps',
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('fetched_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_fetch_stamps_node_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_fetch_stamps_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_id', 'project_id', name='pk_fetch_stamps')
    )
    op.create_table('gha_runner_pools',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('credential_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('desired_count', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('desired_state', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('phase', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('last_error', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('observed', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('observed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('runner_prefix', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.CheckConstraint("desired_state = ANY (ARRAY['running'::text, 'paused'::text, 'absent'::text])", name='ck_gha_runner_pools_desired_state'),
    sa.CheckConstraint("phase = ANY (ARRAY['pending'::text, 'converged'::text, 'error'::text])", name='ck_gha_runner_pools_phase'),
    sa.CheckConstraint('desired_count >= 1 AND desired_count <= 32', name='ck_gha_runner_pools_desired_count'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_gha_runner_pools_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['credential_id'], ['credentials.id'], name='fk_gha_runner_pools_credential_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_gha_runner_pools_node_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_gha_runner_pools_project_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_gha_runner_pools_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_gha_runner_pools'),
    sa.UniqueConstraint('node_id', name='uq_gha_runner_pools_node_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_gha_runner_pools_credential_id', 'gha_runner_pools', ['credential_id'], unique=False)
    op.create_index('ix_gha_runner_pools_project_id', 'gha_runner_pools', ['project_id'], unique=False)
    op.create_table('microvm_intents',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('vm_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('intent_seq', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('generation', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('verb', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('acked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('outcome', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('refusals', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('refusal_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('next_attempt_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("verb = ANY (ARRAY['provision'::text, 'launch'::text, 'destroy'::text])", name='ck_microvm_intents_verb'),
    sa.CheckConstraint('intent_seq >= 1', name='ck_microvm_intents_seq'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='microvm_intents_node_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='microvm_intents_pkey'),
    sa.UniqueConstraint('vm_id', 'intent_seq', name='uq_microvm_intents_vm_seq', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_microvm_intents_pending', 'microvm_intents', ['node_id', 'created_at'], unique=False, postgresql_where="(outcome IS DISTINCT FROM 'done'::text)")
    op.create_index('ix_microvm_intents_vm_id', 'microvm_intents', ['vm_id'], unique=False)
    op.create_table('node_disk_events',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('occurred_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('trigger', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('used_pct', sa.REAL(), autoincrement=False, nullable=True),
    sa.Column('freed', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('reclaim_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint("kind = ANY (ARRAY['reclaim'::text, 'pause'::text, 'resume'::text])", name='ck_node_disk_events_kind'),
    sa.CheckConstraint("trigger IS NULL OR (trigger = ANY (ARRAY['scheduled'::text, 'warn'::text, 'converge'::text]))", name='ck_node_disk_events_trigger'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='node_disk_events_node_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='node_disk_events_pkey'),
    sa.UniqueConstraint('reclaim_id', name='uq_node_disk_events_reclaim_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_node_disk_events_node_occurred', 'node_disk_events', ['node_id', sa.literal_column('occurred_at DESC')], unique=False)
    op.create_table('node_host_metrics',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('bucket_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('cpu_pct', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('mem_pct', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('cpu_count', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('mem_total_kb', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('window_secs', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('samples', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('disk_free_kb', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('disk_total_kb', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('reboot_required', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('last_upgrade_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('net_rx_bps', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('net_tx_bps', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('tmp_inodes_free', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('tmp_inodes_total', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('load_1', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('load_5', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('load_15', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('swap_total_kb', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('swap_free_kb', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('psi_cpu_pct', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('psi_mem_pct', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('psi_io_pct', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('top_processes', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='node_host_metrics_node_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='node_host_metrics_pkey')
    )
    op.create_index('uq_node_host_metrics_node_bucket', 'node_host_metrics', ['node_id', 'bucket_at'], unique=True)
    op.create_table('node_nodelinks',
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('nodelink_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='node_nodelinks_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='node_nodelinks_node_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['nodelink_id'], ['nodelinks.id'], name='node_nodelinks_nodelink_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_id', 'nodelink_id', name='node_nodelinks_pkey')
    )
    op.create_index('ix_node_nodelinks_nodelink_id', 'node_nodelinks', ['nodelink_id'], unique=False)
    op.create_table('node_projects',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('static_slots', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint('static_slots IS NULL OR static_slots >= 0', name='ck_node_projects_static_slots'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_node_projects_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_node_projects_node_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_node_projects_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_node_projects_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_node_projects'),
    sa.UniqueConstraint('node_id', 'project_id', name='uq_node_projects_node_project', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_node_projects_project_id', 'node_projects', ['project_id'], unique=False)
    op.create_table('node_rentals',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('billing', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status_since', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('cancel_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('stopped_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('stop_cause', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('host_node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('instance_price_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('storage_price_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('shared_price_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('instance_type', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('storage_gb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('monthly_cents', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('stripe_price_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('stripe_subscription_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('owns_subscription', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('first_invoice_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('overlap_credit_cents', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('attempt', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('first_invoice_due_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('windows_price_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint("billing = ANY (ARRAY['monthly'::text, 'free'::text])", name='ck_node_rentals_billing'),
    sa.CheckConstraint("kind = ANY (ARRAY['dedicated'::text, 'shared'::text, 'bundled'::text])", name='ck_node_rentals_kind'),
    sa.CheckConstraint("status = ANY (ARRAY['pending_payment'::text, 'active'::text, 'past_due'::text, 'stopped'::text, 'cancelling'::text, 'terminated'::text, 'failed'::text])", name='ck_node_rentals_status'),
    sa.CheckConstraint("stop_cause IS NULL OR (stop_cause = ANY (ARRAY['non_payment'::text, 'cancel'::text, 'plan_end'::text]))", name='ck_node_rentals_stop_cause'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_node_rentals_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['host_node_id'], ['nodes.id'], name='fk_node_rentals_host_node_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['instance_price_id'], ['rental_prices.id'], name='fk_node_rentals_instance_price_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_node_rentals_node_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['shared_price_id'], ['rental_prices.id'], name='fk_node_rentals_shared_price_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['storage_price_id'], ['rental_prices.id'], name='fk_node_rentals_storage_price_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_node_rentals_updated_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['windows_price_id'], ['rental_prices.id'], name='fk_node_rentals_windows_price_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_node_rentals'),
    sa.UniqueConstraint('id', 'org_id', name='uq_node_rentals_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_node_rentals_host_node_id', 'node_rentals', ['host_node_id'], unique=False)
    op.create_index('ix_node_rentals_node_id', 'node_rentals', ['node_id'], unique=False)
    op.create_index('ix_node_rentals_org_id_created_at', 'node_rentals', ['org_id', 'created_at'], unique=False)
    op.create_table('node_session_usage',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('session_id', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('bucket_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('cpu_pct', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=False),
    sa.Column('rss_kb', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('procs', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('pid', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_node_session_usage_node_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_node_session_usage')
    )
    op.create_index('ix_node_session_usage_node_bucket', 'node_session_usage', ['node_id', 'bucket_at'], unique=False)
    op.create_index('ix_node_session_usage_task_bucket', 'node_session_usage', ['task_id', 'bucket_at'], unique=False)
    op.create_index('uq_node_session_usage_key_bucket', 'node_session_usage', ['node_id', 'task_id', 'session_id', 'bucket_at'], unique=True)
    op.create_table('nodevault_grants',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('key_hash', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('blob', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('blob_kind', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'waiting'::text"), autoincrement=False, nullable=False),
    sa.Column('last_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('generation', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('delivered_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("blob_kind IS NULL OR (blob_kind = ANY (ARRAY['node'::text, 'chain'::text, 'peer'::text]))", name='ck_nodevault_grants_blob_kind'),
    sa.CheckConstraint("last_error IS NULL OR (last_error = ANY (ARRAY['offline'::text, 'timeout'::text, 'crypto_unavailable'::text, 'rejected'::text, 'hash_mismatch'::text, 'wrong_recipient'::text, 'missing_wrapping_key'::text, 'open_failed'::text, 'malformed'::text]))", name='ck_nodevault_grants_last_error'),
    sa.CheckConstraint("state = ANY (ARRAY['waiting'::text, 'delivered'::text, 'failed'::text, 'retire_pending'::text, 'retired'::text])", name='ck_nodevault_grants_state'),
    sa.CheckConstraint('generation >= 0', name='ck_nodevault_grants_generation'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_nodevault_grants_node_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_nodevault_grants'),
    sa.UniqueConstraint('node_id', 'key_hash', name='uq_nodevault_grants_node_key', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_nodevault_grants_org_key_hash', 'nodevault_grants', ['org_id', 'key_hash'], unique=False)
    op.create_table('run_telemetry',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('org_repo', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_run_telemetry_node_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='run_telemetry_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_run_telemetry_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('run_id', name='uq_run_telemetry_run_id', postgresql_nulls_not_distinct=False)
    )
    op.create_table('slot_intents',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('number', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('verb', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('decided_on_version', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('preconditions', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('acked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('outcome', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('decided_on_observed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('refusals', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('refusal_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('next_attempt_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_slot_intents_node_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_slot_intents_project_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='slot_intents_pkey'),
    sa.UniqueConstraint('node_id', 'project_id', 'number', 'verb', 'decided_on_observed_at', name='uq_slot_intents_slot_verb_observed_at', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('node_id', 'project_id', 'number', 'verb', 'decided_on_version', name='uq_slot_intents_slot_verb_version', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_slot_intents_pane_writes_task', 'slot_intents', [sa.literal_column("(payload ->> 'task_id'::text)"), 'created_at'], unique=False, postgresql_where="(verb = ANY (ARRAY['deliver_message'::text, 'deliver_answer'::text]))")
    op.create_index('ix_slot_intents_pending', 'slot_intents', ['node_id', 'created_at'], unique=False, postgresql_where="(outcome IS DISTINCT FROM 'done'::text)")
    op.create_table('vnode_hosts',
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('failure_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last_slot', sa.SMALLINT(), server_default=sa.text("'0'::smallint"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('shared_capacity', sa.SMALLINT(), autoincrement=False, nullable=True),
    sa.Column('used_slots', postgresql.ARRAY(sa.SMALLINT()), server_default=sa.text("'{}'::smallint[]"), autoincrement=False, nullable=False),
    sa.Column('supports_tags', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('root_total_kb', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('root_fs_type', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('root_grow_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('root_reported_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('booted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint('shared_capacity IS NULL OR shared_capacity >= 1 AND shared_capacity <= 99', name='ck_vnode_hosts_shared_capacity'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_vnode_hosts_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_vnode_hosts_node_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('node_id', name='pk_vnode_hosts')
    )
    op.create_table('account_observations',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('config_dir', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('enabled', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('email', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('display_name', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('quota', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('observed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('ingested_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('attested_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='fk_account_observations_account_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_account_observations_node_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='account_observations_pkey'),
    sa.UniqueConstraint('node_id', 'slug', name='uq_account_observations_node_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_table('console_api_key_leases',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('account_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_by', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='fk_console_api_key_leases_account_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by'], ['admin_users.id'], name='fk_console_api_key_leases_created_by', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_console_api_key_leases_node_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_console_api_key_leases')
    )
    op.create_index('ix_console_api_key_leases_account_id', 'console_api_key_leases', ['account_id'], unique=False)
    op.create_index('ix_console_api_key_leases_expires_at', 'console_api_key_leases', ['expires_at'], unique=False)
    op.create_index('ix_console_api_key_leases_node_expires', 'console_api_key_leases', ['node_id', 'expires_at'], unique=False)
    op.create_table('hosting_changes',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('rental_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('mode', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('from_instance_type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('to_instance_type', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('from_storage_gb', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('to_storage_gb', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('from_price_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('to_price_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('from_stripe_price_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('to_stripe_price_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('proration_date', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('amount_due_cents', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('currency', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('quote_expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('invoice_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('revert_invoice_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('step', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('new_host_node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('sessions_at_quote', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.CheckConstraint("mode = ANY (ARRAY['in_place'::text, 'reprovision'::text])", name='ck_hosting_changes_mode'),
    sa.CheckConstraint("state = ANY (ARRAY['quoted'::text, 'paying'::text, 'resizing_storage'::text, 'changing_type'::text, 'reprovisioning'::text, 'reverting'::text, 'done'::text, 'reverted'::text, 'stuck'::text, 'declined'::text, 'expired'::text])", name='ck_hosting_changes_state'),
    sa.CheckConstraint('to_storage_gb >= from_storage_gb', name='ck_hosting_changes_no_shrink'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='hosting_changes_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['new_host_node_id'], ['nodes.id'], name='fk_hosting_changes_new_host_node_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['rental_id'], ['node_rentals.id'], name='fk_hosting_changes_rental_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='hosting_changes_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='hosting_changes_pkey')
    )
    op.create_index('ix_hosting_changes_rental_id_created_at', 'hosting_changes', ['rental_id', 'created_at'], unique=False)
    op.create_index('uq_hosting_changes_in_flight', 'hosting_changes', ['rental_id'], unique=True, postgresql_where="(state = ANY (ARRAY['paying'::text, 'resizing_storage'::text, 'changing_type'::text, 'reprovisioning'::text, 'reverting'::text, 'stuck'::text]))")
    op.create_table('node_orders',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('path', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('stage', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('failed_stage', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('project_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('host_node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('rental_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('os', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('dismissed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("os = ANY (ARRAY['ubuntu'::text, 'macos'::text, 'windows'::text])", name='ck_node_orders_os'),
    sa.CheckConstraint("path = ANY (ARRAY['rent_dedicated'::text, 'rent_shared'::text, 'cloud'::text, 'self_hosted'::text, 'co_locate'::text, 'bundle_keep'::text])", name='ck_node_orders_path'),
    sa.CheckConstraint("stage = ANY (ARRAY['paying'::text, 'launching'::text, 'joining'::text, 'placing'::text, 'waiting_for_machine'::text, 'ready'::text, 'failed'::text])", name='ck_node_orders_stage'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_node_orders_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['host_node_id'], ['nodes.id'], name='fk_node_orders_host_node_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_node_orders_node_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['rental_id'], ['node_rentals.id'], name='fk_node_orders_rental_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_node_orders_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_node_orders'),
    sa.UniqueConstraint('id', 'org_id', name='uq_node_orders_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_node_orders_open_host', 'node_orders', ['host_node_id'], unique=False, postgresql_where="(stage <> ALL (ARRAY['ready'::text, 'failed'::text]))")
    op.create_index('ix_node_orders_open_node', 'node_orders', ['node_id'], unique=False, postgresql_where="(stage <> ALL (ARRAY['ready'::text, 'failed'::text]))")
    op.create_index('ix_node_orders_org_id_created_at', 'node_orders', ['org_id', 'created_at'], unique=False)
    op.create_table('org_bundled_products',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('fingerprint', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('ordinal', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('hosting_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('state', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('released_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('attempts', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('comp_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint("kind = ANY (ARRAY['shared_node'::text, 'dedicated_node'::text])", name='ck_org_bundled_products_kind'),
    sa.CheckConstraint("state = ANY (ARRAY['provisioning'::text, 'failed'::text, 'live'::text, 'released'::text, 'kept'::text, 'ended'::text])", name='ck_org_bundled_products_state'),
    sa.CheckConstraint('ordinal >= 1', name='ck_org_bundled_products_ordinal'),
    sa.ForeignKeyConstraint(['comp_id'], ['org_comps.id'], name='fk_org_bundled_products_comp_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_org_bundled_products_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['hosting_id'], ['node_rentals.id'], name='fk_org_bundled_products_hosting_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_org_bundled_products_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_org_bundled_products'),
    sa.UniqueConstraint('id', 'org_id', name='uq_org_bundled_products_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_org_bundled_products_comp_id', 'org_bundled_products', ['comp_id'], unique=False)
    op.create_index('ix_org_bundled_products_hosting_id', 'org_bundled_products', ['hosting_id'], unique=False)
    op.create_index('uq_org_bundled_products_comp_live', 'org_bundled_products', ['comp_id', 'fingerprint', 'ordinal'], unique=True, postgresql_where="((state <> ALL (ARRAY['kept'::text, 'ended'::text])) AND (comp_id IS NOT NULL))")
    op.create_index('uq_org_bundled_products_plan_live', 'org_bundled_products', ['org_id', 'fingerprint', 'ordinal'], unique=True, postgresql_where="((state <> ALL (ARRAY['kept'::text, 'ended'::text])) AND (comp_id IS NULL))")
    op.create_table('role_bindings',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('group_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('role_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('scope_org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.CheckConstraint('((scope_org_id IS NOT NULL)::integer + (project_id IS NOT NULL)::integer + (account_id IS NOT NULL)::integer) = 1', name='ck_role_bindings_one_scope'),
    sa.CheckConstraint('(user_id IS NOT NULL) <> (group_id IS NOT NULL)', name='ck_role_bindings_one_principal'),
    sa.CheckConstraint('scope_org_id IS NULL OR expires_at IS NULL', name='ck_role_bindings_org_scope_never_expires'),
    sa.CheckConstraint('scope_org_id IS NULL OR scope_org_id = org_id', name='ck_role_bindings_scope_org_is_own_org'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='fk_role_bindings_account_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_role_bindings_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['group_id'], ['groups.id'], name='fk_role_bindings_group_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_role_bindings_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], name='fk_role_bindings_role_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['scope_org_id'], ['orgs.id'], name='fk_role_bindings_scope_org_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_role_bindings_updated_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_role_bindings_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_role_bindings'),
    sa.UniqueConstraint('user_id', 'group_id', 'role_id', 'scope_org_id', 'project_id', 'account_id', name='uq_role_bindings', postgresql_nulls_not_distinct=True)
    )
    op.create_index('ix_role_bindings_expires_at', 'role_bindings', ['expires_at'], unique=False, postgresql_where='(expires_at IS NOT NULL)')
    op.create_index('ix_role_bindings_group_account', 'role_bindings', ['group_id', 'account_id'], unique=False, postgresql_where='(account_id IS NOT NULL)')
    op.create_index('ix_role_bindings_group_org', 'role_bindings', ['group_id', 'scope_org_id'], unique=False, postgresql_where='(scope_org_id IS NOT NULL)')
    op.create_index('ix_role_bindings_group_project', 'role_bindings', ['group_id', 'project_id'], unique=False, postgresql_where='(project_id IS NOT NULL)')
    op.create_index('ix_role_bindings_user_account', 'role_bindings', ['user_id', 'account_id'], unique=False, postgresql_where='(account_id IS NOT NULL)')
    op.create_index('ix_role_bindings_user_org', 'role_bindings', ['user_id', 'scope_org_id'], unique=False, postgresql_where='(scope_org_id IS NOT NULL)')
    op.create_index('ix_role_bindings_user_project', 'role_bindings', ['user_id', 'project_id'], unique=False, postgresql_where='(project_id IS NOT NULL)')
    op.create_table('runners',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('pid', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('cwd', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('worktree_path', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('branch', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('last_seen', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('ingested_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('observed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('proc_started_at', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='fk_runners_account_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_runners_node_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='workers_pkey'),
    sa.UniqueConstraint('node_id', 'pid', name='uq_runners_node_pid', postgresql_nulls_not_distinct=False)
    )
    op.create_table('tasks',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('parent_task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('is_plannable', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('title', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('body', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'draft'::text"), autoincrement=False, nullable=False),
    sa.Column('github_synced_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('priority', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=True),
    sa.Column('outcome_summary', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('outcome_pr_url', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('worktree_ref', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('persona', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('autonomous', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('sweep_opt_out', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('runner_session_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('clarity_score', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('spec_verdict', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('critic_verdict', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('fanout_attempts', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('stale_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stale_since', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('recon_map', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('phase_index', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('phase_name', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('outcome_no_pr_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('dispatch_attempts', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('transition_mode', sa.TEXT(), server_default=sa.text("'auto'::text"), autoincrement=False, nullable=False),
    sa.Column('run_requested_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('outcome_no_pr_evidence', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('hidden', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('rule_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('asset_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('merged_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('compliance_group_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('compliance_path', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('awaiting_response_body', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('archived_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('finish_follow_up_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('auto_retagged_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('auto_retag_attempts', sa.SMALLINT(), server_default=sa.text("'0'::smallint"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('external_task_system', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('external_task_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('external_done_comment_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('external_done_comment_attempts', sa.SMALLINT(), server_default=sa.text("'0'::smallint"), autoincrement=False, nullable=False),
    sa.Column('artifacts_hidden_before', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_by_cron_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_by_cron_name', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('dedup_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('settle_refusals', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('done_workflow_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('done_workflow_attempts', sa.SMALLINT(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('started_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('bound_provider', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_by_task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('unsaved', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('assigned_to_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('question_runner_stopped_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('dispatched_tier', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('dispatched_model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('feature_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('stop_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stop_note', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stopped_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('stopped_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('playbook', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('playbook_stage', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('source', sa.TEXT(), server_default=sa.text("'console'::text"), autoincrement=False, nullable=False),
    sa.Column('created_by_trigger_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_by_trigger_name', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('project_memory', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('memory_arm', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('memory_arm_source', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('has_repo', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('pinned_node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_by_automation_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_by_automation_name', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('dispatch_wait_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('dispatch_wait_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('agent_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('run_agent_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('run_agent_commit', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('done_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('compliance_seal_seq', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('bound_account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint("(stop_reason IS NULL) = (stopped_at IS NULL) AND (stopped_by_id IS NULL OR stop_reason IS NOT NULL) AND (stopped_by_id IS NOT NULL OR stop_reason IS NULL OR (stop_reason = ANY (ARRAY['balance'::text, 'jaah_unavailable'::text])))", name='ck_tasks_stop_record_complete'),
    sa.CheckConstraint("memory_arm_source = ANY (ARRAY['task'::text, 'epic'::text, 'default'::text, 'holdout'::text, 'killed'::text, 'plan'::text])", name='ck_tasks_memory_arm_source'),
    sa.CheckConstraint("source = ANY (ARRAY['console'::text, 'cron'::text, 'agent'::text, 'trigger:webhook'::text, 'trigger:github'::text, 'trigger:gmail'::text, 'trigger:slack'::text, 'trigger:user_registered'::text])", name='ck_tasks_source'),
    sa.CheckConstraint("stop_note IS NULL OR stop_reason = 'waiting_on_customer'::text AND char_length(stop_note) <= 2000", name='ck_tasks_stop_note'),
    sa.CheckConstraint("stop_reason IS NULL OR (stop_reason = ANY (ARRAY['operator'::text, 'waiting_on_customer'::text, 'balance'::text, 'jaah_unavailable'::text]))", name='ck_tasks_stop_reason'),
    sa.CheckConstraint('NOT (agent_id IS NOT NULL AND bound_provider IS NOT NULL)', name='ck_tasks_agent_or_provider'),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name='tasks_agent_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['assigned_to_id'], ['admin_users.id'], name='fk_tasks_assigned_to_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['bound_account_id'], ['accounts.id'], name='tasks_bound_account_id_fkey', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_automation_id'], ['automations.id'], name='tasks_created_by_automation_id_fkey', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_cron_id'], ['crons.id'], name='fk_tasks_created_by_cron_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_tasks_created_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_task_id'], ['tasks.id'], name='fk_tasks_created_by_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['feature_id', 'project_id'], ['features.id', 'features.project_id'], name='fk_tasks_feature_project', ondelete='SET NULL (feature_id)'),
    sa.ForeignKeyConstraint(['parent_task_id'], ['tasks.id'], name='fk_tasks_parent_task_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_tasks_project_id'),
    sa.ForeignKeyConstraint(['started_by_id'], ['admin_users.id'], name='fk_tasks_started_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['stopped_by_id'], ['admin_users.id'], name='fk_tasks_stopped_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_tasks_updated_by_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='tasks_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_tasks_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_tasks_agent_id', 'tasks', ['agent_id'], unique=False, postgresql_where='(agent_id IS NOT NULL)')
    op.create_index('ix_tasks_assigned_to_id', 'tasks', ['assigned_to_id'], unique=False)
    op.create_index('ix_tasks_bound_account_id', 'tasks', ['bound_account_id'], unique=False, postgresql_where='(bound_account_id IS NOT NULL)')
    op.create_index('ix_tasks_created_by_automation_node', 'tasks', ['created_by_automation_id', 'pinned_node_id', sa.literal_column('id DESC')], unique=False, postgresql_where='(created_by_automation_id IS NOT NULL)')
    op.create_index('ix_tasks_created_by_automation_project', 'tasks', ['created_by_automation_id', 'project_id', sa.literal_column('id DESC')], unique=False, postgresql_where='(created_by_automation_id IS NOT NULL)')
    op.create_index('ix_tasks_created_by_cron_id', 'tasks', ['created_by_cron_id'], unique=False)
    op.create_index('ix_tasks_created_by_id', 'tasks', ['created_by_id'], unique=False)
    op.create_index('ix_tasks_created_by_task_id', 'tasks', ['created_by_task_id'], unique=False)
    op.create_index('ix_tasks_created_by_trigger_id', 'tasks', ['created_by_trigger_id'], unique=False)
    op.create_index('ix_tasks_done_workflow_pending', 'tasks', ['done_workflow_attempts', 'id'], unique=False, postgresql_where="((state = 'done'::text) AND (done_workflow_at IS NULL))")
    op.create_index('ix_tasks_external_task_id', 'tasks', ['external_task_id'], unique=False, postgresql_where='(external_task_id IS NOT NULL)')
    op.create_index('ix_tasks_feature_id', 'tasks', ['feature_id'], unique=False, postgresql_where='(feature_id IS NOT NULL)')
    op.create_index('ix_tasks_parent_task_id', 'tasks', ['parent_task_id'], unique=False)
    op.create_index('ix_tasks_persona_dedup_key', 'tasks', ['persona', 'dedup_key'], unique=False, postgresql_ops={'dedup_key': 'text_pattern_ops'})
    op.create_index('ix_tasks_pinned_node_id', 'tasks', ['pinned_node_id'], unique=False, postgresql_where='(pinned_node_id IS NOT NULL)')
    op.create_index('ix_tasks_project_id', 'tasks', ['project_id'], unique=False)
    op.create_index('ix_tasks_project_id_source', 'tasks', ['project_id', 'source'], unique=False)
    op.create_index('ix_tasks_project_state_dispatchable', 'tasks', ['project_id', 'state'], unique=False, postgresql_where="(state = 'ready'::text)")
    op.create_index('ix_tasks_started_by_id', 'tasks', ['started_by_id'], unique=False)
    op.create_index('uq_tasks_compliance_slot', 'tasks', ['project_id', 'compliance_path'], unique=True, postgresql_where="((persona = 'compliance_fix'::text) AND (compliance_path IS NOT NULL) AND ((state = ANY (ARRAY['ready'::text, 'active'::text])) OR ((state = 'awaiting_response'::text) AND (outcome_pr_url IS NOT NULL))))")
    op.create_index('uq_tasks_open_dedup_key', 'tasks', ['project_id', 'persona', 'dedup_key'], unique=True, postgresql_where="((dedup_key IS NOT NULL) AND (state <> ALL (ARRAY['done'::text, 'system_failed'::text, 'paused'::text, 'awaiting_response'::text])))")
    op.create_table('virtual_nodes',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('host_node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('former_node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('tenant_org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('slot', sa.SMALLINT(), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('failure_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('tailnet_device_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('access_point_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('shared_efs_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('shared_efs_region', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('shared_efs_mount_ip', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('tag', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('disk_cap_gb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('inode_cap', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('disk_used_kb', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('inodes_used', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('usage_measured_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("state = ANY (ARRAY['provisioning'::text, 'ready'::text, 'failed'::text, 'deleting'::text])", name='ck_virtual_nodes_state'),
    sa.CheckConstraint("tag ~ '^[a-z][a-z0-9]{7}$'::text", name='ck_virtual_nodes_tag'),
    sa.CheckConstraint('slot >= 1 AND slot <= 99', name='ck_virtual_nodes_slot'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_virtual_nodes_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['host_node_id'], ['vnode_hosts.node_id'], name='fk_virtual_nodes_host_node_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_virtual_nodes_node_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_virtual_nodes_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_virtual_nodes'),
    sa.UniqueConstraint('host_node_id', 'slot', name='uq_virtual_nodes_host_slot', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_virtual_nodes_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('node_id', name='uq_virtual_nodes_node_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_virtual_nodes_tenant_org_id', 'virtual_nodes', ['tenant_org_id'], unique=False)
    op.create_index('uq_virtual_nodes_host_tag', 'virtual_nodes', ['host_node_id', 'tag'], unique=True, postgresql_where='(tag IS NOT NULL)')
    op.create_table('worktrees',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('number', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('path', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('branch', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('ports', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('last_active', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('session_status', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('session_started_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('ahead', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('behind', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('window_name', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('dirty', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('observed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('merged', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('session', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('session_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('console', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('probes', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('head_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('probed_config_dirs', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('age_last_used_secs', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('age_last_commit_secs', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('age_index_mtime_secs', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('last_used', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('tmux_window_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('tmux_target', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('compose_project_name', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('stack_present', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('stack_up', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('default_title', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('host_env', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('session_pid', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('session_proc_started_at', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('session_since', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'free'::text"), autoincrement=False, nullable=False),
    sa.Column('state_since', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('generation', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('launch_claimed_generation', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('dispatch_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('dispatched_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('acked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('pid', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('pid_start_time', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('launch_session_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last_seen_live', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('owner_user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('leased_account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), server_default=sa.text("'static'::text"), autoincrement=False, nullable=False),
    sa.Column('retiring_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('destroy_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('has_repo', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('repos', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('transcript_written_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("kind = ANY (ARRAY['static'::text, 'dynamic'::text])", name='ck_worktrees_kind'),
    sa.CheckConstraint("state = 'free'::text AND task_id IS NULL AND owner_user_id IS NULL OR state = 'task'::text AND task_id IS NOT NULL AND owner_user_id IS NULL OR state = 'user'::text AND owner_user_id IS NOT NULL AND task_id IS NULL", name='ck_worktrees_state_ownership'),
    sa.CheckConstraint("state = ANY (ARRAY['free'::text, 'task'::text, 'user'::text])", name='ck_worktrees_state'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='fk_worktrees_account_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_worktrees_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['leased_account_id'], ['accounts.id'], name='fk_worktrees_leased_account_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_worktrees_node_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['admin_users.id'], name='fk_worktrees_owner_user_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_worktrees_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_worktrees_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='worktrees_pkey'),
    sa.UniqueConstraint('node_id', 'project_id', 'number', name='uq_worktrees_node_project_number', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_worktrees_project_id', 'worktrees', ['project_id'], unique=False)
    op.create_index('ix_worktrees_task_id', 'worktrees', ['task_id'], unique=False)
    op.create_table('access_requests',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('requester_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('role_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('scope_org_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('note', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('decided_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('decided_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('binding_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('requester_seen_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('slack_messages', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.CheckConstraint("state = ANY (ARRAY['pending'::text, 'approved'::text, 'denied'::text, 'withdrawn'::text])", name='ck_access_requests_state'),
    sa.CheckConstraint('((scope_org_id IS NOT NULL)::integer + (project_id IS NOT NULL)::integer + (account_id IS NOT NULL)::integer) = 1', name='ck_access_requests_one_scope'),
    sa.CheckConstraint('scope_org_id IS NULL OR scope_org_id = org_id', name='ck_access_requests_scope_org_is_own_org'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='fk_access_requests_account_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['binding_id'], ['role_bindings.id'], name='fk_access_requests_binding_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_access_requests_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['decided_by_id'], ['admin_users.id'], name='fk_access_requests_decided_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_access_requests_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['requester_id'], ['admin_users.id'], name='fk_access_requests_requester_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], name='fk_access_requests_role_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['scope_org_id'], ['orgs.id'], name='fk_access_requests_scope_org_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_access_requests_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_access_requests')
    )
    op.create_index('uq_access_requests_pending', 'access_requests', ['requester_id', 'role_id', 'scope_org_id', 'project_id', 'account_id'], unique=True, postgresql_where="(state = 'pending'::text)", postgresql_nulls_not_distinct=True)
    op.create_table('compliance_manifests',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('version', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('trigger', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('generated_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('generated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('payload_sha256', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('kid', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('json_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('html_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('failure_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('session_digest_snapshot', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("state = 'sealed'::text AND payload_sha256 IS NOT NULL AND failure_reason IS NULL OR state = 'failed'::text AND payload_sha256 IS NULL AND failure_reason IS NOT NULL", name='ck_compliance_manifests_outcome'),
    sa.CheckConstraint("state = ANY (ARRAY['sealed'::text, 'failed'::text])", name='ck_compliance_manifests_state'),
    sa.CheckConstraint("trigger = ANY (ARRAY['auto'::text, 'regenerate'::text])", name='ck_compliance_manifests_trigger'),
    sa.CheckConstraint('version >= 1', name='ck_compliance_manifests_version'),
    sa.ForeignKeyConstraint(['generated_by_id'], ['admin_users.id'], name='fk_compliance_manifests_generated_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_compliance_manifests_task_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_compliance_manifests'),
    sa.UniqueConstraint('task_id', 'version', name='uq_compliance_manifests_task_version', postgresql_nulls_not_distinct=False)
    )
    op.create_table('cron_runs',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('cron_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('fired_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('finished_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('outcome', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('skip_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('detail', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.CheckConstraint("outcome = ANY (ARRAY['acted'::text, 'noop'::text, 'skipped'::text, 'missed'::text, 'error'::text])", name='ck_cron_runs_outcome'),
    sa.ForeignKeyConstraint(['cron_id'], ['crons.id'], name='fk_cron_runs_cron_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_cron_runs_task_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_cron_runs')
    )
    op.create_index('ix_cron_runs_cron_id_acted', 'cron_runs', ['cron_id', sa.literal_column('fired_at DESC')], unique=False, postgresql_where="(outcome = 'acted'::text)")
    op.create_index('ix_cron_runs_cron_id_fired_at', 'cron_runs', ['cron_id', sa.literal_column('fired_at DESC')], unique=False)
    op.create_table('memory_capture_log',
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('feed', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('done_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('produced', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('rejected', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.CheckConstraint("feed = ANY (ARRAY['propose'::text, 'settle'::text, 'stop_delta'::text, 'distill'::text, 'codemap'::text])", name='ck_memory_capture_log_feed'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_memory_capture_log_task_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_id', 'feed', name='pk_memory_capture_log')
    )
    op.create_table('merge_lane_rows',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('pr_number', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('pr_url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('head_sha', sa.VARCHAR(length=40), autoincrement=False, nullable=False),
    sa.Column('base_branch', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('git_person_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'queued'::text"), autoincrement=False, nullable=False),
    sa.Column('lane_task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('batch_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('recheck_after', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('merge_sha', sa.VARCHAR(length=40), autoincrement=False, nullable=True),
    sa.Column('failure', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('applied_events', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('note', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('waiting_on', sa.VARCHAR(length=40), autoincrement=False, nullable=True),
    sa.Column('merged_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("state = ANY (ARRAY['queued'::text, 'testing'::text, 'merged'::text, 'failed'::text, 'superseded'::text, 'closed'::text])", name='ck_merge_lane_rows_state'),
    sa.ForeignKeyConstraint(['lane_task_id'], ['tasks.id'], name='fk_merge_lane_rows_lane_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_merge_lane_rows_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_merge_lane_rows_task_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_merge_lane_rows'),
    sa.UniqueConstraint('id', 'org_id', name='uq_merge_lane_rows_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_merge_lane_rows_lane_task_id', 'merge_lane_rows', ['lane_task_id'], unique=False)
    op.create_index('ix_merge_lane_rows_project_state', 'merge_lane_rows', ['project_id', 'state'], unique=False)
    op.create_index('uq_merge_lane_open', 'merge_lane_rows', ['project_id', 'pr_number'], unique=True, postgresql_where="(state = ANY (ARRAY['queued'::text, 'testing'::text]))")
    op.create_table('microvms',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('console_session_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'provisioning'::text"), autoincrement=False, nullable=False),
    sa.Column('generation', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('vcpus', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('ram_mib', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('disk_gib', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('allowlist', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('wait_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('last_sighted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("kind = 'task'::text AND task_id IS NOT NULL AND console_session_id IS NULL OR kind = 'console'::text AND console_session_id IS NOT NULL AND task_id IS NULL", name='ck_microvms_owner'),
    sa.CheckConstraint("kind = ANY (ARRAY['task'::text, 'console'::text])", name='ck_microvms_kind'),
    sa.CheckConstraint("state = ANY (ARRAY['provisioning'::text, 'running'::text, 'retained'::text, 'destroying'::text, 'destroyed'::text, 'failed'::text])", name='ck_microvms_state'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='microvms_created_by_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='microvms_node_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='microvms_project_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='microvms_task_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='microvms_updated_by_id_fkey', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='microvms_pkey')
    )
    op.create_index('ix_microvms_node_state', 'microvms', ['node_id', 'state'], unique=False)
    op.create_index('uq_microvms_live_task', 'microvms', ['task_id'], unique=True, postgresql_where="(state <> ALL (ARRAY['destroyed'::text, 'failed'::text]))")
    op.create_table('outcome_reviews',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('run_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('score', sa.SMALLINT(), autoincrement=False, nullable=True),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('usage_by_model', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint('char_length(reason) <= 200', name='ck_outcome_reviews_reason'),
    sa.CheckConstraint('score >= 1 AND score <= 5', name='ck_outcome_reviews_score'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_outcome_reviews_task_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_outcome_reviews'),
    sa.UniqueConstraint('task_id', name='uq_outcome_reviews_task_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_outcome_reviews_created_at', 'outcome_reviews', ['created_at'], unique=False)
    op.create_table('project_memories',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('number', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('scope', sa.TEXT(), server_default=sa.text("'project'::text"), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('body', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('paths', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('personas', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('keywords', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('commands', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('feed', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('injectable', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('status_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('trust', sa.REAL(), server_default=sa.text('0.5'), autoincrement=False, nullable=False),
    sa.Column('confirms', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('flags', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('anchor_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('anchor_blobs', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('verified_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('verified_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('valid_from', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('valid_to', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('superseded_by', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('slice', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('graduation_task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('graduated_to', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('content_hash', sa.TEXT(), autoincrement=False, nullable=False),
    *([sa.Column('embedding', _Vector(), autoincrement=False, nullable=True)] if vector() else []),
    sa.Column('embedding_model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('tsv', postgresql.TSVECTOR(), sa.Computed("(to_tsvector('english'::regconfig, ((name || ' '::text) || body)) || array_to_tsvector(keywords))", persisted=True), autoincrement=False, nullable=True),
    sa.Column('accepted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_injected_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_used_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('project_repo_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("feed = ANY (ARRAY['propose'::text, 'settle'::text, 'stop_delta'::text, 'distill'::text, 'codemap'::text])", name='ck_project_memories_feed'),
    sa.CheckConstraint("kind = ANY (ARRAY['codemap'::text, 'gotcha'::text, 'recipe'::text, 'env'::text, 'qa'::text])", name='ck_project_memories_kind'),
    sa.CheckConstraint("scope = ANY (ARRAY['project'::text, 'org'::text])", name='ck_project_memories_scope'),
    sa.CheckConstraint("status = ANY (ARRAY['proposed'::text, 'accepted'::text, 'flagged'::text, 'stale'::text, 'retired'::text, 'graduated'::text])", name='ck_project_memories_status'),
    sa.CheckConstraint('char_length(body) <= 1200', name='ck_project_memories_body_length'),
    sa.CheckConstraint('char_length(name) <= 80', name='ck_project_memories_name_length'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_project_memories_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['graduation_task_id'], ['tasks.id'], name='fk_project_memories_graduation_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_project_memories_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_repo_id'], ['project_repos.id'], name='fk_project_memories_project_repo_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['superseded_by'], ['project_memories.id'], name='fk_project_memories_superseded_by', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_project_memories_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_project_memories'),
    sa.UniqueConstraint('id', 'org_id', name='uq_project_memories_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('project_id', 'number', name='uq_project_memories_project_number', postgresql_nulls_not_distinct=False)
    )
    if vector():
        op.create_index('ix_project_memories_embedding', 'project_memories', ['embedding'], unique=False, postgresql_ops={'embedding': 'vector_cosine_ops'}, postgresql_with={'m': '16', 'ef_construction': '64'}, postgresql_using='hnsw')
    op.create_index('ix_project_memories_paths', 'project_memories', ['paths'], unique=False, postgresql_using='gin')
    op.create_index('ix_project_memories_project_repo_id', 'project_memories', ['project_repo_id'], unique=False, postgresql_where='(project_repo_id IS NOT NULL)')
    op.create_index('ix_project_memories_project_status', 'project_memories', ['project_id', 'status', 'injectable'], unique=False)
    op.create_index('ix_project_memories_tsv', 'project_memories', ['tsv'], unique=False, postgresql_using='gin')
    op.create_index('uq_project_memories_live_hash', 'project_memories', ['project_id', 'kind', 'content_hash'], unique=True, postgresql_where="(status = ANY (ARRAY['proposed'::text, 'accepted'::text, 'flagged'::text, 'stale'::text]))")
    op.create_table('sessions',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('agent_session_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('agent_kind', sa.TEXT(), server_default=sa.text("'claude_code'::text"), autoincrement=False, nullable=False),
    sa.Column('parent_session_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('node_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('account_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('ended_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'running'::text"), autoincrement=False, nullable=False),
    sa.Column('end_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('worktree_ref', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('cwd', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('branch', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('holder', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('pid', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('proc_started_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('tokens_in', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('tokens_out', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('subagent_count', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('transcript_bucket', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('transcript_prefix', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('transcript_manifest_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('transcript_status', sa.TEXT(), server_default=sa.text("'pending'::text"), autoincrement=False, nullable=False),
    sa.Column('transcript_bytes', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('transcript_sha256', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('transcript_attempts', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('transcript_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('transcript_leased_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('memory_arm', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('agent_ref', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('runtime', sa.TEXT(), server_default=sa.text("'legacy'::text"), autoincrement=False, nullable=False),
    sa.Column('native_session_ref', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('events_last_seq', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('events_ended', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('dispatched_runtime', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('pane_log_tail', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('pane_log_redactions', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('pane_log_captured_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('pane_log_written_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('end_detail', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('events_refused', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.CheckConstraint("runtime = ANY (ARRAY['legacy'::text, 'acp'::text])", name='ck_sessions_runtime'),
    sa.CheckConstraint("status = ANY (ARRAY['running'::text, 'ended'::text, 'unknown'::text])", name='ck_sessions_status'),
    sa.CheckConstraint("transcript_status <> 'uploaded'::text OR transcript_manifest_key IS NOT NULL", name='ck_sessions_uploaded_names_a_manifest'),
    sa.CheckConstraint("transcript_status <> 'uploading'::text OR transcript_leased_at IS NOT NULL", name='ck_sessions_uploading_holds_a_lease'),
    sa.CheckConstraint("transcript_status = ANY (ARRAY['pending'::text, 'uploading'::text, 'uploaded'::text, 'failed'::text, 'gave_up'::text, 'unavailable'::text, 'not_applicable'::text])", name='ck_sessions_transcript_status'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='fk_sessions_account_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_sessions_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], name='fk_sessions_node_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_sessions_project_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_sessions_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_sessions_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_sessions'),
    sa.UniqueConstraint('agent_session_id', name='uq_sessions_agent_session_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_sessions_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_sessions_node_id', 'sessions', ['node_id'], unique=False)
    op.create_index('ix_sessions_started_at', 'sessions', ['started_at'], unique=False)
    op.create_index('ix_sessions_task_id', 'sessions', ['task_id'], unique=False)
    op.create_index('ix_sessions_transcript_sweep', 'sessions', ['transcript_status', 'transcript_leased_at'], unique=False, postgresql_where="(transcript_status = ANY (ARRAY['pending'::text, 'uploading'::text, 'failed'::text]))")
    op.create_table('slack_threads',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('channel_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('thread_ts', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('admin_user_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('question_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['admin_user_id'], ['admin_users.id'], name='slack_threads_admin_user_id_fkey', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='slack_threads_task_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='slack_threads_pkey'),
    sa.UniqueConstraint('channel_id', 'thread_ts', name='uq_slack_threads_channel_thread', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_slack_threads_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_slack_threads_task_id', 'slack_threads', ['task_id'], unique=False)
    op.create_table('task_body_versions',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('body', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('source', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('restored_from_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['restored_from_id'], ['task_body_versions.id'], name='task_body_versions_restored_from_id_fkey', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='task_body_versions_task_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='task_body_versions_pkey')
    )
    op.create_index('ix_task_body_versions_task_id', 'task_body_versions', ['task_id'], unique=False)
    op.create_table('task_comments',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('body', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('author_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('edited_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('ticket_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('visibility', sa.TEXT(), server_default=sa.text("'public'::text"), autoincrement=False, nullable=False),
    sa.CheckConstraint("visibility = 'public'::text OR ticket_id IS NOT NULL", name='ck_task_comments_internal_is_ticket'),
    sa.CheckConstraint("visibility = ANY (ARRAY['public'::text, 'internal'::text])", name='ck_task_comments_visibility'),
    sa.CheckConstraint('num_nonnulls(task_id, ticket_id) = 1', name='ck_task_comments_one_subject'),
    sa.ForeignKeyConstraint(['author_id'], ['admin_users.id'], name='fk_task_comments_author_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_task_comments_task_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name='fk_task_comments_ticket_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_task_comments')
    )
    op.create_index('ix_task_comments_author_id', 'task_comments', ['author_id'], unique=False)
    op.create_index('ix_task_comments_task_id', 'task_comments', ['task_id', 'id'], unique=False)
    op.create_index('ix_task_comments_ticket_id', 'task_comments', ['ticket_id', 'id'], unique=False)
    op.create_table('task_dependencies',
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('depends_on_task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['depends_on_task_id'], ['tasks.id'], name='fk_task_dependencies_depends_on_task_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_task_dependencies_task_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_id', 'depends_on_task_id', name='pk_task_dependencies')
    )
    op.create_index('ix_task_dependencies_depends_on_task_id', 'task_dependencies', ['depends_on_task_id'], unique=False)
    op.create_table('task_pr_changes',
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('pr_number', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pr_url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('forge', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('merge_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('commits', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('files', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('captured_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("status = ANY (ARRAY['captured'::text, 'partial'::text, 'not_captured'::text])", name='ck_task_pr_changes_status'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_task_pr_changes_task_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_id', 'pr_number', name='pk_task_pr_changes')
    )
    op.create_table('task_pr_stats',
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('pr_url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('forge', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('additions', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('deletions', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('files_changed', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('excluded_additions', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('excluded_deletions', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('captured_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("status = ANY (ARRAY['captured'::text, 'partial'::text, 'not_captured'::text])", name='ck_task_pr_stats_status'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_task_pr_stats_task_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_id', name='pk_task_pr_stats')
    )
    op.create_table('task_prs',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('project_repo_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('repo_path', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('forge', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('pr_number', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('pr_url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('is_primary', sa.BOOLEAN(), autoincrement=False, nullable=False),
    sa.Column('state', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('opened_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('merged_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("state = ANY (ARRAY['open'::text, 'merged'::text, 'closed'::text])", name='ck_task_prs_state'),
    sa.ForeignKeyConstraint(['project_repo_id'], ['project_repos.id'], name='fk_task_prs_project_repo_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_task_prs_task_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_task_prs'),
    sa.UniqueConstraint('task_id', 'pr_url', name='uq_task_prs_task_url', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_task_prs_project_repo_id', 'task_prs', ['project_repo_id'], unique=False, postgresql_where='(project_repo_id IS NOT NULL)')
    op.create_table('task_questions',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('question', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('chosen_resolution', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('status', sa.TEXT(), server_default=sa.text("'open'::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('delivered_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('comment', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('multi_select', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('resolved_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('resolved_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('header', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('guard_context', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['resolved_by_id'], ['admin_users.id'], name='fk_task_questions_resolved_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_task_questions_task_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='task_questions_pkey')
    )
    op.create_index('ix_task_questions_task_id', 'task_questions', ['task_id'], unique=False)
    op.create_table('task_ratings',
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('stars', sa.SMALLINT(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.CheckConstraint('stars >= 1 AND stars <= 5', name='ck_task_ratings_stars_1_5'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='task_ratings_task_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='task_ratings_user_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_id', 'user_id', name='task_ratings_pkey')
    )
    op.create_index('ix_task_ratings_user_id', 'task_ratings', ['user_id'], unique=False)
    op.create_table('task_tags',
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('tag_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], name='task_tags_tag_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='task_tags_task_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_id', 'tag_id', name='task_tags_pkey')
    )
    op.create_index('ix_task_tags_tag_id', 'task_tags', ['tag_id'], unique=False)
    op.create_table('task_triggers',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('slug', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('source_kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('source_config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('ingress_token', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('secret_sealed', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('secret_revealed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('persona', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('title_template', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('prompt_template', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('full_auto', sa.BOOLEAN(), server_default=sa.text('true'), autoincrement=False, nullable=False),
    sa.Column('hidden', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('preflight_kind', sa.TEXT(), server_default=sa.text("'none'::text"), autoincrement=False, nullable=False),
    sa.Column('preflight_script_path', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('preflight_prompt', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('preflight_credential_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('preflight_timeout_seconds', sa.INTEGER(), server_default=sa.text('120'), autoincrement=False, nullable=False),
    sa.Column('state_document', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('concurrency_cap', sa.INTEGER(), server_default=sa.text('1'), autoincrement=False, nullable=False),
    sa.Column('queue_depth', sa.INTEGER(), server_default=sa.text('10'), autoincrement=False, nullable=False),
    sa.Column('rate_limit_per_hour', sa.INTEGER(), server_default=sa.text('10'), autoincrement=False, nullable=False),
    sa.Column('budget_usd', sa.NUMERIC(precision=10, scale=4), autoincrement=False, nullable=True),
    sa.Column('auto_pause_failures', sa.INTEGER(), server_default=sa.text('5'), autoincrement=False, nullable=True),
    sa.Column('auto_pause_idle_days', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('paused_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('paused_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('paused_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('failed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('failed_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('consecutive_failures', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('last_failure_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('budget_breach_task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('budget_breach_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last_delivery_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('last_success_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_by_task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('resumed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('automation_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint("preflight_kind = ANY (ARRAY['none'::text, 'script'::text, 'model'::text])", name='ck_task_triggers_preflight_kind'),
    sa.CheckConstraint("source_kind = ANY (ARRAY['webhook'::text, 'github'::text, 'gmail'::text, 'slack'::text, 'user_registered'::text])", name='ck_task_triggers_source_kind'),
    sa.CheckConstraint('auto_pause_failures IS NULL OR auto_pause_failures >= 1', name='ck_task_triggers_auto_pause_failures'),
    sa.CheckConstraint('auto_pause_idle_days IS NULL OR auto_pause_idle_days >= 1', name='ck_task_triggers_auto_pause_idle_days'),
    sa.CheckConstraint('concurrency_cap >= 1 AND concurrency_cap <= 20', name='ck_task_triggers_concurrency_cap'),
    sa.CheckConstraint('preflight_timeout_seconds >= 5 AND preflight_timeout_seconds <= 165', name='ck_task_triggers_preflight_timeout'),
    sa.CheckConstraint('queue_depth >= 0 AND queue_depth <= 100', name='ck_task_triggers_queue_depth'),
    sa.CheckConstraint('rate_limit_per_hour >= 1 AND rate_limit_per_hour <= 1000', name='ck_task_triggers_rate_limit'),
    sa.ForeignKeyConstraint(['automation_id'], ['automations.id'], name='task_triggers_automation_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['budget_breach_task_id'], ['tasks.id'], name='fk_task_triggers_budget_breach_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_task_triggers_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_task_id'], ['tasks.id'], name='fk_task_triggers_created_by_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['paused_by_id'], ['admin_users.id'], name='fk_task_triggers_paused_by_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['preflight_credential_id'], ['credentials.id'], name='fk_task_triggers_preflight_credential_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_task_triggers_project_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_task_triggers_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_task_triggers'),
    sa.UniqueConstraint('automation_id', name='task_triggers_automation_id_key', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_task_triggers_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('ingress_token', name='uq_task_triggers_ingress_token', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('org_id', 'slug', name='uq_task_triggers_org_slug', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_task_triggers_created_by_task_id', 'task_triggers', ['created_by_task_id'], unique=False)
    op.create_index('ix_task_triggers_project_id', 'task_triggers', ['project_id'], unique=False)
    op.create_table('ticket_task_links',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('ticket_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('task_title_snapshot', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('done_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('done_acked_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_ticket_task_links_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_ticket_task_links_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name='fk_ticket_task_links_ticket_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_ticket_task_links'),
    sa.UniqueConstraint('ticket_id', 'task_id', name='uq_ticket_task_links_ticket_task', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_ticket_task_links_task_id', 'ticket_task_links', ['task_id'], unique=False)
    op.create_table('vnode_enrolments',
    sa.Column('virtual_node_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('sealed_node_token', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('sealed_tailnet_key', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('cp_base_url', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['virtual_node_id'], ['virtual_nodes.id'], name='fk_vnode_enrolments_virtual_node_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('virtual_node_id', name='pk_vnode_enrolments')
    )
    op.create_table('memory_events',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('unit_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('channel', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('provisional', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('session_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("channel = ANY (ARRAY['prompt'::text, 'hook'::text, 'tool'::text, 'summary'::text, 'pr'::text])", name='ck_memory_events_channel'),
    sa.CheckConstraint("kind = ANY (ARRAY['injected'::text, 'read'::text, 'cited'::text, 'followed'::text])", name='ck_memory_events_kind'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_memory_events_task_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['unit_id'], ['project_memories.id'], name='fk_memory_events_unit_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_memory_events'),
    sa.UniqueConstraint('task_id', 'unit_id', 'kind', name='uq_memory_events_task_unit_kind', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_memory_events_project_at', 'memory_events', ['project_id', 'at'], unique=False)
    op.create_index('ix_memory_events_unit_id', 'memory_events', ['unit_id'], unique=False)
    op.create_table('memory_sources',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('unit_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('session_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('persona', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('model', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('feed', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('observed_sha', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('restricted', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("feed = ANY (ARRAY['propose'::text, 'settle'::text, 'stop_delta'::text, 'distill'::text, 'codemap'::text])", name='ck_memory_sources_feed'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_memory_sources_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['unit_id'], ['project_memories.id'], name='fk_memory_sources_unit_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_memory_sources'),
    sa.UniqueConstraint('unit_id', 'task_id', name='uq_memory_sources_unit_task', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_memory_sources_task_id', 'memory_sources', ['task_id'], unique=False)
    op.create_table('memory_votes',
    sa.Column('unit_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('verdict', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('source', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("source = ANY (ARRAY['tool'::text, 'citation'::text, 'stale'::text, 'settle'::text])", name='ck_memory_votes_source'),
    sa.CheckConstraint("verdict = ANY (ARRAY['confirm'::text, 'flag'::text])", name='ck_memory_votes_verdict'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_memory_votes_task_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['unit_id'], ['project_memories.id'], name='fk_memory_votes_unit_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('unit_id', 'task_id', name='pk_memory_votes')
    )
    op.create_index('ix_memory_votes_task_id', 'memory_votes', ['task_id'], unique=False)
    op.create_table('task_attachments',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('filename', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('content_type', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('size_bytes', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('uploaded_by_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('kind', sa.TEXT(), server_default=sa.text("'attachment'::text"), autoincrement=False, nullable=False),
    sa.Column('source_task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('s3_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('uploaded_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('ticket_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('visibility', sa.TEXT(), server_default=sa.text("'public'::text"), autoincrement=False, nullable=False),
    sa.Column('comment_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('conversation_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('automation_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('etag', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('s3_bucket', sa.TEXT(), autoincrement=False, nullable=True),
    sa.CheckConstraint("visibility = 'public'::text OR ticket_id IS NOT NULL", name='ck_task_attachments_internal_is_ticket'),
    sa.CheckConstraint("visibility = ANY (ARRAY['public'::text, 'internal'::text])", name='ck_task_attachments_visibility'),
    sa.CheckConstraint('num_nonnulls(task_id, ticket_id, conversation_id, automation_id) = 1', name='ck_task_attachments_one_subject'),
    sa.ForeignKeyConstraint(['automation_id'], ['automations.id'], name='fk_task_attachments_automation_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['comment_id'], ['task_comments.id'], name='fk_task_attachments_comment_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['conversation_id'], ['support_conversations.id'], name='fk_task_attachments_conversation_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_task_id'], ['tasks.id'], name='task_attachments_source_task_id_fkey', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='task_attachments_task_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name='fk_task_attachments_ticket_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploaded_by_id'], ['admin_users.id'], name='task_attachments_uploaded_by_id_fkey', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='task_attachments_pkey')
    )
    op.create_index('ix_task_attachments_automation_id', 'task_attachments', ['automation_id'], unique=False)
    op.create_index('ix_task_attachments_comment_id', 'task_attachments', ['comment_id'], unique=False)
    op.create_index('ix_task_attachments_conversation_id', 'task_attachments', ['conversation_id'], unique=False)
    op.create_index('ix_task_attachments_org_id', 'task_attachments', ['org_id'], unique=False)
    op.create_index('ix_task_attachments_s3_key', 'task_attachments', ['s3_key'], unique=False)
    op.create_index('ix_task_attachments_source_task_id', 'task_attachments', ['source_task_id'], unique=False)
    op.create_index('ix_task_attachments_task_id', 'task_attachments', ['task_id'], unique=False)
    op.create_index('ix_task_attachments_task_kind_id', 'task_attachments', ['task_id', 'kind', sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_task_attachments_ticket_id', 'task_attachments', ['ticket_id'], unique=False)
    op.create_table('task_comment_mentions',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('comment_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('read_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('ticket_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.CheckConstraint('num_nonnulls(task_id, ticket_id) = 1', name='ck_task_comment_mentions_one_subject'),
    sa.ForeignKeyConstraint(['comment_id'], ['task_comments.id'], name='fk_task_comment_mentions_comment_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_task_comment_mentions_task_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name='fk_task_comment_mentions_ticket_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_task_comment_mentions_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_task_comment_mentions'),
    sa.UniqueConstraint('comment_id', 'user_id', name='uq_task_comment_mentions_comment_user', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_task_comment_mentions_comment_id', 'task_comment_mentions', ['comment_id'], unique=False)
    op.create_index('ix_task_comment_mentions_task_id', 'task_comment_mentions', ['task_id'], unique=False)
    op.create_index('ix_task_comment_mentions_ticket_id', 'task_comment_mentions', ['ticket_id'], unique=False)
    op.create_index('ix_task_comment_mentions_unread', 'task_comment_mentions', ['user_id', 'id'], unique=False, postgresql_where='(read_at IS NULL)')
    op.create_table('task_comment_reactions',
    sa.Column('id', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('comment_id', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('ticket_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('emoji', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('read_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('removed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.CheckConstraint("emoji = ANY (ARRAY['thumbs_up'::text, 'heart'::text, 'laugh'::text, 'tada'::text, 'surprised'::text, 'sad'::text, 'eyes'::text, 'rocket'::text])", name='ck_task_comment_reactions_emoji'),
    sa.CheckConstraint('num_nonnulls(task_id, ticket_id) = 1', name='ck_task_comment_reactions_one_subject'),
    sa.ForeignKeyConstraint(['comment_id'], ['task_comments.id'], name='fk_task_comment_reactions_comment_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_task_comment_reactions_task_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name='fk_task_comment_reactions_ticket_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_task_comment_reactions_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_task_comment_reactions'),
    sa.UniqueConstraint('comment_id', 'user_id', 'emoji', name='uq_task_comment_reactions_comment_user_emoji', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_task_comment_reactions_task_id', 'task_comment_reactions', ['task_id'], unique=False)
    op.create_index('ix_task_comment_reactions_ticket_id', 'task_comment_reactions', ['ticket_id'], unique=False)
    op.create_index('ix_task_comment_reactions_user_id', 'task_comment_reactions', ['user_id'], unique=False)
    op.create_table('tenant_mail_addresses',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('description', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('created_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('updated_by_id', sa.UUID(), server_default=sa.text("'01900000-0000-7000-8000-000000000001'::uuid"), autoincrement=False, nullable=False),
    sa.Column('address', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('trigger_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['admin_users.id'], name='fk_tenant_mail_addresses_created_by_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['trigger_id'], ['task_triggers.id'], name='fk_tenant_mail_addresses_trigger_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by_id'], ['admin_users.id'], name='fk_tenant_mail_addresses_updated_by_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_tenant_mail_addresses'),
    sa.UniqueConstraint('address', name='uq_tenant_mail_addresses_address', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_tenant_mail_addresses_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_tenant_mail_addresses_trigger_id', 'tenant_mail_addresses', ['trigger_id'], unique=False)
    op.create_table('trigger_dedup',
    sa.Column('trigger_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('dedup_key', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('first_seen_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('delivery_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_trigger_dedup_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['trigger_id'], ['task_triggers.id'], name='fk_trigger_dedup_trigger_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('trigger_id', 'dedup_key', name='pk_trigger_dedup')
    )
    op.create_index('ix_trigger_dedup_task_id', 'trigger_dedup', ['task_id'], unique=False, postgresql_where='(task_id IS NOT NULL)')
    op.create_table('trigger_notices',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('trigger_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('read_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.CheckConstraint("kind = ANY (ARRAY['trigger_failed'::text, 'trigger_digest'::text, 'automation_alert'::text])", name='ck_trigger_notices_kind'),
    sa.ForeignKeyConstraint(['trigger_id'], ['task_triggers.id'], name='fk_trigger_notices_trigger_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['admin_users.id'], name='fk_trigger_notices_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_trigger_notices'),
    sa.UniqueConstraint('id', 'org_id', name='uq_trigger_notices_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_trigger_notices_trigger_id', 'trigger_notices', ['trigger_id'], unique=False)
    op.create_index('ix_trigger_notices_user_created', 'trigger_notices', ['user_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('uq_trigger_notices_daily', 'trigger_notices', ['user_id', 'org_id', 'kind', sa.literal_column("(summary ->> 'day'::text)")], unique=True)
    op.create_table('inbound_emails',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('mailbox', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('gmail_message_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('thread_id', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('from_address', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('to_address', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('subject', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('received_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('snippet', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('body_text', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('body_truncated', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('stored_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('trigger_routed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('provider', sa.TEXT(), server_default=sa.text("'gmail'::text"), autoincrement=False, nullable=False),
    sa.Column('provider_message_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('source_ref', sa.TEXT(), server_default=sa.text("'system'::text"), autoincrement=False, nullable=False),
    sa.Column('recipient', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('verdicts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), autoincrement=False, nullable=False),
    sa.Column('rejected_reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('tenant_connection_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('tenant_address_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint("provider = ANY (ARRAY['gmail'::text, 'ses'::text, 'tenant_gmail'::text])", name='ck_inbound_emails_provider'),
    sa.CheckConstraint("rejected_reason = ANY (ARRAY['spam'::text, 'virus'::text, 'dmarc'::text, 'rate_limited'::text, 'plan_off'::text])", name='ck_inbound_emails_rejected_reason'),
    sa.ForeignKeyConstraint(['tenant_address_id'], ['tenant_mail_addresses.id'], name='fk_inbound_emails_tenant_address_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_connection_id'], ['gmail_connections.id'], name='fk_inbound_emails_tenant_connection_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pk_inbound_emails'),
    sa.UniqueConstraint('gmail_message_id', name='uq_inbound_emails_gmail_message_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('id', 'org_id', name='uq_inbound_emails_id_org_id', postgresql_nulls_not_distinct=False),
    sa.UniqueConstraint('provider', 'source_ref', 'provider_message_id', 'recipient', name='uq_inbound_emails_provider_message', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_inbound_emails_rate', 'inbound_emails', ['org_id', sa.literal_column("(verdicts ->> 'sender_domain'::text)"), 'stored_at'], unique=False, postgresql_where='(rejected_reason IS NULL)')
    op.create_index('ix_inbound_emails_received_at', 'inbound_emails', [sa.literal_column('received_at DESC')], unique=False)
    op.create_index('ix_inbound_emails_tenant_address_id', 'inbound_emails', ['tenant_address_id'], unique=False, postgresql_where='(tenant_address_id IS NOT NULL)')
    op.create_index('ix_inbound_emails_tenant_connection_id', 'inbound_emails', ['tenant_connection_id'], unique=False, postgresql_where='(tenant_connection_id IS NOT NULL)')
    op.create_table('trigger_deliveries',
    sa.Column('id', sa.BIGINT(), sa.Identity(always=False, start=1, increment=1, minvalue=1, maxvalue=9223372036854775807, cycle=False, cache=1), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('trigger_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('parent_delivery_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('received_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('processed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('state', sa.TEXT(), server_default=sa.text("'received'::text"), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('item_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('dedup_key', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('missing_paths', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('event', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('preview', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('claimed_until', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('preflighted_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('attempts', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('budget_exceeded_usd', sa.NUMERIC(precision=10, scale=4), autoincrement=False, nullable=True),
    sa.Column('outcome_counted', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False),
    sa.Column('inbound_email_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('slack_event_id', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('owed_targets', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.CheckConstraint("state = ANY (ARRAY['received'::text, 'preflighting'::text, 'queued'::text, 'dispatched'::text, 'filtered'::text, 'deduped'::text, 'rate_capped'::text, 'skipped'::text, 'failed'::text, 'unmatched'::text, 'filing'::text])", name='ck_trigger_deliveries_state'),
    sa.ForeignKeyConstraint(['inbound_email_id'], ['inbound_emails.id'], name='fk_trigger_deliveries_inbound_email_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['parent_delivery_id'], ['trigger_deliveries.id'], name='fk_trigger_deliveries_parent_delivery_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_trigger_deliveries_task_id', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['trigger_id'], ['task_triggers.id'], name='fk_trigger_deliveries_trigger_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_trigger_deliveries'),
    sa.UniqueConstraint('id', 'org_id', name='uq_trigger_deliveries_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_trigger_deliveries_parent_delivery_id', 'trigger_deliveries', ['parent_delivery_id'], unique=False)
    op.create_index('ix_trigger_deliveries_task_id', 'trigger_deliveries', ['task_id'], unique=False, postgresql_where='(task_id IS NOT NULL)')
    op.create_index('ix_trigger_deliveries_trigger_received', 'trigger_deliveries', ['trigger_id', sa.literal_column('received_at DESC')], unique=False)
    op.create_index('ix_trigger_deliveries_trigger_state', 'trigger_deliveries', ['trigger_id', 'state'], unique=False)
    op.create_index('uq_trigger_deliveries_one_preflighting', 'trigger_deliveries', ['trigger_id'], unique=True, postgresql_where="(state = 'preflighting'::text)")
    op.create_table('automation_run_targets',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuidv7()'), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('automation_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('cron_run_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('delivery_id', sa.BIGINT(), autoincrement=False, nullable=True),
    sa.Column('target_kind', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('target_id', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('target_name', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('outcome', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('reason', sa.TEXT(), server_default=sa.text("''::text"), autoincrement=False, nullable=False),
    sa.Column('task_id', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.CheckConstraint("outcome = ANY (ARRAY['filed'::text, 'skipped'::text, 'error'::text, 'not_reached'::text, 'queued'::text])", name='ck_automation_run_targets_outcome'),
    sa.CheckConstraint("target_kind = ANY (ARRAY['projects'::text, 'nodes'::text])", name='ck_automation_run_targets_target_kind'),
    sa.CheckConstraint('num_nonnulls(cron_run_id, delivery_id) = 1', name='ck_automation_run_targets_one_run'),
    sa.ForeignKeyConstraint(['automation_id'], ['automations.id'], name='automation_run_targets_automation_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['cron_run_id'], ['cron_runs.id'], name='automation_run_targets_cron_run_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['delivery_id'], ['trigger_deliveries.id'], name='automation_run_targets_delivery_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='automation_run_targets_task_id_fkey', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='automation_run_targets_pkey'),
    sa.UniqueConstraint('id', 'org_id', name='uq_automation_run_targets_id_org_id', postgresql_nulls_not_distinct=False)
    )
    op.create_index('ix_automation_run_targets_automation_id', 'automation_run_targets', ['automation_id'], unique=False)
    op.create_index('ix_automation_run_targets_cron_run', 'automation_run_targets', ['cron_run_id'], unique=False, postgresql_where='(cron_run_id IS NOT NULL)')
    op.create_index('ix_automation_run_targets_delivery', 'automation_run_targets', ['delivery_id'], unique=False, postgresql_where='(delivery_id IS NOT NULL)')
    # ### end Alembic commands ###
    # The cycles' other ends (`_DEFERRED_FKS`), now that every table exists.
    for name, table, parent, column, on_delete in _DEFERRED_FKS:
        op.create_foreign_key(name, table, parent, [column], ["id"], ondelete=on_delete)

    # ── What reflection cannot see ─────────────────────────────────────────────────────────────────
    _restore_the_names_the_rename_left_behind()
    _install_composite_org_keys()
    _install_functions_and_triggers()
    op.execute(_CP_READONLY_ROLE_SQL)

    # ── The rows a fresh database comes up with ────────────────────────────────────────────────────
    _seed(conn)


def downgrade() -> None:
    """One revision before the base is an empty database — so this drops the schema, not a step of it.

    The view goes first, then every table in one `CASCADE` drop (every trigger and key falls with its
    table), then the functions. The `vector` extension and the `cp_readonly` role stay: the extension costs
    nothing unused, and the role is cluster-wide and owns nothing.

    GUARDED, BECAUSE THIS IS THE BASE (the guard came with the 0368 collapse, not from 0131). That baseline had
    revisions stacked above it, so `alembic downgrade -1` against a head meant "undo one revision" and
    the whole-schema drop was several deliberate steps away. This file is the BASE revision, so `-1` from it,
    `base` and `202610251700` all resolve HERE: the habitual roll-back-one reflex now drops every table on
    whatever database the DSN points at, and on a control plane that is every credential envelope, node
    token hash, org, task and grant. Nothing automated reaches it — `migrate.py` calls only
    `command.upgrade`, and no workflow, compose file or `./dc` path invokes `alembic downgrade` — so this
    is a human footgun rather than a deploy hazard, which is exactly the kind an env-var handshake is
    the right size for. `./dc reset` is the local answer and wipes the volume instead."""
    import os

    if os.environ.get("ALEMBIC_ALLOW_BASELINE_DROP") != "yes-drop-the-whole-schema":
        raise RuntimeError(
            "202610251700 is the BASE revision: this downgrade drops every table, not one revision's worth of "
            "schema. Set ALEMBIC_ALLOW_BASELINE_DROP=yes-drop-the-whole-schema if that is genuinely "
            "what you want, or use `./dc reset` for a local stack."
        )

    from sqlalchemy import inspect

    from app.entity_shape import set_updated_at_drop_function_sql
    from app.tenancy.inherit_trigger import org_inherit_function_drop_sql
    from app.tenancy.node_org import node_org_immutable_function_drop_sql
    from app.tenancy.org_delete import org_delete_guard_function_drop_sql

    op.execute("DROP VIEW IF EXISTS account_fold")
    tables = sorted(set(inspect(op.get_bind()).get_table_names()) - {"alembic_version"})
    op.execute("DROP TABLE " + ", ".join(f'"{table}"' for table in tables) + " CASCADE")
    for function in (
        "clear_task_stop_record",
        "resolve_task_feature",
        "admin_users_clear_email_proof",
        "token_ledger_immutable",
        "gov_ledger_immutable",
        "identities_clear_email_proof",
        "admin_users_identity_attach",
        "admin_users_identity_sync",
        "fill_identity_id_from_user",
        "fill_key_user_id_from_user",
        "clear_org_id_with_user_id",
        "reap_identity_records_with_last_membership",
        "automation_check_one_child",
        "automation_drop_childless",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.execute("DROP FUNCTION IF EXISTS automation_assert_one_child(uuid)")
    op.execute(node_org_immutable_function_drop_sql())
    op.execute(org_delete_guard_function_drop_sql())
    op.execute(org_inherit_function_drop_sql())
    op.execute(set_updated_at_drop_function_sql())
