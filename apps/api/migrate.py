"""Local migration runner — ``python migrate.py [revision]`` → ``alembic upgrade``.

Plain CLI the compose api service runs on boot
(``python migrate.py && uvicorn …``). Defaults to ``head``; pass an explicit
revision to target a specific one. Idempotent.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
from typing import Iterator

from alembic.config import Config

log = logging.getLogger(__name__)

# The advisory-lock class for "this database's schema is being migrated or seeded": one key for the whole
# boot step, so `upgrade()` and `app.seed.main()` serialize under the SAME lock across every process that
# boots against one database. Sibling of `routes/tasks/answers.py::ANSWER_LOCK_CLASSID` and
# `cron_runtime.CRON_RUN_LOCK_CLASSID`; the classes keep the lock spaces apart. objid is always 0.
MIGRATE_LOCK_CLASSID = 0x6D696772  # 'migr'


@contextlib.contextmanager
def serialized() -> Iterator[None]:
    """Hold the schema lock for the block — BLOCKING, on a dedicated autocommit connection.

    WHY: the control plane runs as two ECS tasks and its deployment allows 200%, so ECS
    launches both replacement tasks at once and each boots with `migrate.upgrade("head"); app.seed.main([])`
    (the container's start command). Unserialized, a deploy carrying a pending revision has
    two processes run the same `CREATE TABLE`: one dies on a duplicate-object error and counts against the
    ECS circuit breaker, and the seed can double-insert. `pg_advisory_lock`, never `pg_try_advisory_lock`:
    the loser must WAIT for the winner and then find nothing left to do, not skip the step (a task that
    skipped its migration would serve against a schema it never checked).

    A dedicated connection because a pooled one carrying a session lock hands it to the next checkout
    (`cron_runtime.RunLock`, `routes/tasks/answers.py::_AnswerLock`); through `db_credentials`, not `psycopg`
    directly, so the rotating RDS credential is injected. Closing the connection frees the lock regardless
    of the unlock, so an unlock failure is logged and never raised — the body's own outcome is the result.
    """
    from app import db_credentials

    conn = db_credentials.psycopg_connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s, %s)", (MIGRATE_LOCK_CLASSID, 0))
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", (MIGRATE_LOCK_CLASSID, 0))
        except Exception as exc:  # noqa: BLE001 — closing the connection frees the lock regardless
            log.warning("migrate: schema lock unlock failed (%s); closing the connection", exc)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def alembic_config() -> Config:
    """This app's Alembic ``Config``, with both paths derived from THIS file's own location.

    Single home for that derivation — deliberately here, in the production runner that already owned
    it, rather than in either test suite. ``alembic.ini``'s ``script_location`` is the *relative*
    ``migrations``, so it only resolves when the process cwd happens to be ``apps/api``; deriving from
    ``__file__`` instead makes the config correct from any cwd, and identically inside the container
    (the local compose stack mounts ``apps/api/`` at ``/app``) and on a host checkout.

    Every test that needs an Alembic ``Config`` imports it from here; none builds its own. That is the
    whole claim, and it is mechanically checkable without trusting this sentence — building a config
    means calling ``set_main_option``, so ``git grep -l set_main_option`` over the API test suite must find
    nothing. (Six of those seven files were themselves deleted by the first collapse, of 0001..0091 —
    the revisions they walked no longer exist, and eight further collapses have since carried them
    into ``202610251700_baseline``, the tree's base revision file. The file names below therefore resolve
    in git history, which is where the incident they record lives; the claim about ``set_main_option``
    is about the tests that remain and is still checkable.) Seven files in the API test suite each
    carried a byte-identical copy hardcoding the
    container-absolute config + ``script_location`` under ``/app``; off-container those paths do not
    exist, so **72 of the 73 tests those files collect DIED** in ``ScriptDirectory.from_config`` with
    ``CommandError: Path doesn't exist: '/app/migrations'`` *before* reaching a single assertion — in
    ``tests/test_rbac_floor.py`` at fixture level, erroring all seven of its tests at once.

    **Two different quantities, and this sentence is about the deaths, not the population:** 73 is the
    *collection* count (3+3+5+2+2+7+51 across the seven files, no ``parametrize``, so defs == collected);
    72 is how many of them the hardcode actually killed. The one survivor is
    ``test_commands_backup.py::test_export_defaults_to_fleet_backup_dir`` — its whole body is
    ``backup_dir()`` plus a ``FLEET_BACKUP_DIR`` assert, it never enters ``_scratch_db()`` and never
    builds a config, and no autouse fixture builds one for it, so it passed off-container both before the
    sweep and after. Do not collapse the two numbers back together: a count is only meaningful next to the
    quantity it counts, which is the same failure mode as the harness blindness described below.

    That blindness is how B1 (revision ``0065``) shipped a forked Alembic head to develop: the head
    test failed identically on the branch and on the develop control for this path reason, so it was
    written off as pre-existing noise in a delta comparison and its revision literal was never
    evaluated. A test must fail for its own reason, not its harness's.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "migrations"))
    return cfg


def deploy_schema_note(cfg: Config) -> str:
    """The one line `upgrade()` prints before migrating — which of THREE cases this deploy is.

    The third case is why this is not just a pending-count. A database stamped BELOW a collapsed
    baseline (`202610251700_baseline`) holds a revision this tree no longer contains, and walking `head` back to
    it raises — which an "on error, report nothing pending" helper turned into `already at head — this
    deploy changes no schema`, printed one line before alembic raised `Can't locate revision`. The log
    denied the exact failure the collapse introduces, at the one moment somebody is reading it.

    Never raises: this is a log line's input, and a diagnostic must not be the thing that stops a
    deploy. `command.upgrade` runs next and fails loudly and for the right reason if anything here was
    unreadable.
    """
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    from app.db import database_url

    try:
        script = ScriptDirectory.from_config(cfg)
        engine = create_engine(database_url())
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        if current is not None and current not in {rev.revision for rev in script.walk_revisions()}:
            return (
                f"migrate: this database is stamped at {current}, which is NOT in this tree's revision "
                f"history — migrations below it were collapsed into a baseline. `alembic upgrade` is "
                f"about to fail with `Can't locate revision`; rebuild the stack with `./dc reset`."
            )
        pending = [
            rev.revision for rev in script.iterate_revisions("head", current) if rev.revision != current
        ]
    except Exception:
        return "migrate: could not read this database's revision — migrating anyway"

    if not pending:
        return "migrate: already at head — this deploy changes no schema"
    return (
        f"migrate: {len(pending)} pending revision(s) — this deploy CHANGES THE SCHEMA while the "
        f"previous release is still serving: {', '.join(pending)}"
    )


#: Postgres SQLSTATEs for "this object already exists": column, table/relation, object, function, schema.
DUPLICATE_OBJECT_SQLSTATES = frozenset({"42701", "42P07", "42710", "42723", "42P06"})


def duplicate_object_note(exc: BaseException) -> str | None:
    """The diagnosis to print when `alembic upgrade` died because a pending revision's object already exists.

    WHY: the database records only a revision id, never which file wrote it. When a rebase forces you to
    renumber your migration (develop merged a revision under the id you had), your slot's database still
    says it is at that id, and it already carries your columns. Alembic sees nothing wrong: it treats the
    id as develop's revision and runs your migration again under its new id, which dies on
    `DuplicateColumn`, and the api crash-loops on every boot. Nothing in that error points at the cause.

    Returns None for any other failure, so only the real duplicate-object case gets the hint. Walks the
    `__cause__`/`__context__` chain and SQLAlchemy's `.orig`, because the driver error arrives wrapped.
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        err = stack.pop()
        if err is None or id(err) in seen:
            continue
        seen.add(id(err))
        if getattr(err, "sqlstate", None) in DUPLICATE_OBJECT_SQLSTATES:
            return (
                f"migrate: a pending revision created an object that already exists ({type(err).__name__}). "
                "The database's recorded revision already carries that revision's changes. On a local "
                "stack this usually means a migration was renumbered after a rebase while this database "
                "still records its OLD id. Rebuild it: `FORCE=1 ./dc reset && ./dc up -d` (boot "
                "migrates and seeds; re-run `./dc seed --dev` if you used the dev fixture)."
            )
        orig = getattr(err, "orig", None)
        stack.extend([orig if isinstance(orig, BaseException) else None, err.__cause__, err.__context__])
    return None


def upgrade(revision: str = "head") -> None:
    """Migrate to `revision`, announcing first whether this deploy changes the schema at all.

    THE ANNOUNCEMENT IS THE POINT, and it is not decoration. The control plane deploys with an OVERLAP now
    (the retired leadership lock): the incoming task migrates and starts serving while the OUTGOING task — running
    the previous release — is still answering requests. For the seconds that overlap lasts, old code runs
    against the new schema, so a migration must be additive; a drop or a rename needs to wait a release
    (expand/contract).

    Most merges carry no migration at all and are skew-free by construction. This line is what tells the
    two cases apart in the deploy log, at the one moment somebody debugging a bad deploy needs to know
    which one they are looking at.

    The whole body runs under `serialized()`, the announcement included: read outside the lock, it would
    report the revision the OTHER booting task is mid-way through applying.
    """
    from alembic import command

    with serialized():
        cfg = alembic_config()
        print(deploy_schema_note(cfg), flush=True)
        try:
            command.upgrade(cfg, revision)
        except Exception as exc:
            note = duplicate_object_note(exc)
            if note is not None:
                print(note, file=sys.stderr, flush=True)
            raise


if __name__ == "__main__":
    upgrade(sys.argv[1] if len(sys.argv) > 1 else "head")
