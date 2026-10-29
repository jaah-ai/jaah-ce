"""Alembic environment — URL from DATABASE_URL, metadata from app.models."""

import functools
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import database_url  # noqa: E402
from app.models.base import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    # `disable_existing_loggers=False` IS THE LOAD-BEARING ARGUMENT — do not drop it back to
    # `fileConfig(config.config_file_name)` (alembic's stock template, which is what this was).
    # `fileConfig` defaults that flag to **True**, meaning "set `disabled = True` on every logger that
    # already exists and is not named in this ini". alembic.ini names only root/sqlalchemy/alembic, so
    # a `fileConfig` here silently and permanently kills `cockpit-task-notify`, `fleet-decide`,
    # `jaah-ingest`, `agent-routes` — every named logger the app created — for the REST OF THE
    # PROCESS. That is harmless for `python migrate.py` (a standalone process with no app loggers yet),
    # but this module also runs whenever alembic is driven IN-PROCESS — which today is
    # `test_migrations_logging_isolation.py` itself, the file that guards this line. (So did six
    # `test_*_migration.py` modules, until the 0001..0091 collapse removed the revisions they walked,
    # and so did `tests/conftest.py`, until `app/fixture_db.py` moved the upgrade into the template
    # build's own SUBPROCESS — where a disabled logger dies with the process and reaches nobody.)
    #
    # The damage that caused: a task-notify test asserting a relay failure is logged without raising
    # passed standalone (16/16) and failed in the full suite with `assert 'NOT delivered' in ''` — because
    # `test_dispatch_identity_migration.py` (collected earlier, `d` < `t`) had already run an upgrade and
    # disabled the logger it asserts on. `test_fleet_routes.py`'s `shadow_log` fixture documents the same
    # symptom for `fleet-decide` and works around it with a hand-attached handler; that workaround exists
    # because of THIS line. A test must fail for its own reason, not for an unrelated module's
    # process-global side effect.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Alembic's Config.set_main_option() writes through configparser's BasicInterpolation, which treats a
# bare "%" as the start of a %(name)s reference — a real, percent-encoded password (e.g. containing
# "%2A") raises ValueError: invalid interpolation syntax. "%%" is the documented escape; get_main_option
# (and engine_from_config, used below) unescape it back to a single "%" on read, so this round-trips
# correctly for both online and offline migration modes.
config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
target_metadata = Base.metadata


#: Foreign keys declared in `app/models/` whose delete action the database spells and the model cannot:
#: `ON DELETE SET NULL (feature_id)` / `(org_template_id)` (see the `Task` class), so autogenerate would replace it
#: on every run.
_UNSPELLABLE_FOREIGN_KEYS = frozenset({("tasks", "fk_tasks_feature_project"), ("tasks", "fk_tasks_org_template_id")})


#: Columns the outgoing release still maps and this one does not: hidden for ONE release, then dropped by the next
#: contraction revision (which also takes a hidden column out of this set, so a column cannot stay hidden by
#: accident). `tenant_email_sends.task_id` is the uuid the model replaced with `sent_by_task_id` (revision
#: 202610240300); the rest are the pool caps dynamic worktrees retired (revision 202610241500): the
#: per-project pool fields, a node-group line's worktree count, and the two org/plan caps whose CHECKs stay with them.
_RETIRING_COLUMNS = frozenset({
    ("tenant_email_sends", "task_id"),
    ("projects", "pool_max"),
    ("projects", "worktree_pool_enabled"),
    ("projects", "default_static_slots"),
    ("projects", "prefer_static_nodes"),
    ("node_group_projects", "worktrees_per_node"),
    ("orgs", "limit_runner_slots"),
    ("orgs", "limit_active_worktrees"),
    ("billing_plans", "limit_runner_slots"),
    ("billing_plans", "limit_active_worktrees"),
})


@functools.cache
def _hidden_columns() -> frozenset[tuple[str, str]]:
    """Each `Shared(legacy_org_id=True)` table's `org_id` — a column the outgoing release still reads and the
    model must not map (`app/tenancy/manifest.py`) — and `_RETIRING_COLUMNS`."""
    from app.tenancy.keys import ORG_COLUMN
    from app.tenancy.manifest import TENANCY, Shared

    return _RETIRING_COLUMNS | frozenset(
        (cls.__tablename__, ORG_COLUMN) for cls, verdict in TENANCY.items()
        if isinstance(verdict, Shared) and verdict.legacy_org_id
    )


def include_object(object_, name, type_, reflected, _compare_to) -> bool:
    """Hide from `alembic check` / `--autogenerate` what the database holds on purpose and `Base.metadata`
    does not: the composite org keys (below), the hidden columns (`_hidden_columns`) with any database-only
    foreign key over them, and `_UNSPELLABLE_FOREIGN_KEYS`. Everything else must match, which
    the schema-drift test holds the full gate to.

    The composite org keys:

    WITHOUT THIS, THE NEXT AUTOGENERATED REVISION UNINSTALLS THE ORG BOUNDARY AND EVERY GATE STAYS GREEN.
    The schema carries one `(parent_col, org_id) REFERENCES parent(id, org_id)` foreign key per
    derivation path, installed in raw SQL generated from `app/tenancy/keys.py` — by
    `202610251700_baseline.py::_install_composite_org_keys` on a fresh database, and by the collapsed revisions
    that first added them on one that predates it — and NONE of them is declared in
    `Base.metadata` — there is nowhere declarative to put a key whose whole definition is derived from a
    census. Autogenerate compares foreign keys, so it sees every one of those constraints as unknown to
    the models and emits `op.drop_constraint` for each; the additive-migration check flags dropped tables,
    dropped columns and raw-SQL DROPs but not `op.drop_constraint`, and the drift test would go green on
    a model that simply agreed, so nothing downstream would object either.

    NARROW BY CONSTRUCTION: the names come from `keys.py::all_org_keys()`, so this hides exactly the
    keys those generators produce and nothing else — never a pattern, never a prefix. A key the census
    stops generating stops being hidden on the same run, which is what keeps this from silently covering a
    constraint nobody owns any more. The tenancy composite-key test pins that the hidden set is
    the generated set. No COUNT is written here — `app/tenancy/keys.py`'s docstring is the single home
    for how many times that figure has gone stale on this axis, and one more copy is one more to drift.

    IT READS THE UNION AND NOT `composite_keys()` ALONE, which is the one thing to get right when this
    boundary grows a second generator. `keys.py::same_org_keys` renders the same key shape for a
    non-derivation reference — e.g. `role_bindings`' scope columns — and those keys are no
    more declarative than the derivation keys are. Hiding only the first set would leave autogenerate
    emitting `op.drop_constraint` for the second, which is the "every gate stays green" failure above
    with a narrower blast radius and no other check standing behind it. `all_org_keys()` is the single
    home for that union.

    MATCHED ON (TABLE, NAME) AND NOT ON THE NAME ALONE: POSTGRES SCOPES A CONSTRAINT NAME TO ITS TABLE, so two tables in one schema can each
    hold a constraint called `x`. Keyed on the name alone this hide-list would extend to a constraint of
    the same name on a DIFFERENT table — making a real, unrelated foreign key invisible to autogenerate,
    which is precisely the "every gate stays green" failure above with the roles reversed. The two
    surfaces now agree on what identifies a key.

    THE HONEST ALTERNATIVE IS TO DECLARE THEM, and it is a boundary of its own rather than a tidy-up: it
    means appending a `ForeignKeyConstraint` object per derivation path onto mapped tables at import
    time, which changes the unit of work's own table-dependency sort for every write in the process. Recorded as a deferred
    follow-up rather than folded into the revision that installs them."""
    # `.table` is the object's own table; `getattr` twice rather than an attribute chain so an object
    # autogenerate hands over without one is simply not hidden, never an AttributeError raised out of the
    # comparison pass.
    table = getattr(getattr(object_, "table", None), "name", None)
    if type_ == "column":
        # Database side only: a hidden column the model maps again is compared like any other.
        return not reflected or (table, name) not in _hidden_columns()
    if type_ != "foreign_key_constraint":
        return True
    from app.tenancy.keys import all_org_keys

    if (table, name) in _UNSPELLABLE_FOREIGN_KEYS:
        return False
    if reflected and any((table, column) in _hidden_columns() for column in getattr(object_, "column_keys", ())):
        return False
    return (table, name) not in {(key.table, key.name) for key in all_org_keys()}


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
