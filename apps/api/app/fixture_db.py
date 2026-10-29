"""fixture_db.py — the migration-chain-keyed template database, and the run-scoped clones taken from it.

One template per migration chain and seeder (`cockpit_fixture_<head>_<digest>`: migrate -> seed -> dev
fixture), never written after build; pytest workers and `./dc e2e-db` clone it (a file copy). The alembic
head plus a digest of every revision file (`app/schema_head.py::chain_digest`) and of the two seeder
modules (`SEEDERS`) is the cache key, so a renumbered, re-chained or edited revision rebuilds it, and so
does an edit to `app/seed.py` or `app/seed_dev_profile.py`. A seeder's edit to a module those two import
is not seen: drop the template by hand (`DROP DATABASE`).

Nobody connects to the template (Postgres refuses to clone an occupied one), so the build runs in a
subprocess; the final rename needs no retry (Postgres waits up to 5s for backends), so never open a
parent-side connection to the scratch DB. Advisory locks and catalogue statements all go to the
`postgres` database (locks are per-database); only `claim_id_space` connects elsewhere.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import time
import zlib
from urllib.parse import urlsplit
from typing import Callable, Optional

import psycopg

from app import db_credentials
from app import fixture_keys
from app.fixture_keys import FIXTURE_LOCAL_PRIVATE_KEY

#: The key the fixture's sealed `credentials.config` rows are encrypted under; the template build and every
#: clone reader must agree. Its home and its deny-list: `app/fixture_keys.py`.
FIXTURE_CREDENTIAL_KEY = fixture_keys.FIXTURE_CREDENTIAL_KEY

#: The bcrypt cost the template's logins are sealed at, pinned because whichever process builds the
#: template decides it for every clone until the migration chain moves. `tests/conftest.py` reads it.
FIXTURE_BCRYPT_ROUNDS = "4"

#: The template database's name prefix; `<head>_<digest>` is appended to it.
TEMPLATE_PREFIX = "cockpit_fixture_"

#: Every run-scoped clone starts with this. `\l` after a run must show none of them (A2.2).
RUN_DB_PREFIX = "cockpit_test"

#: The Playwright suite's database (the e2e suite's global setup and teardown). A fixed
#: name: the stack's one api container can target only one DB, and a leaked copy then has one name to
#: drop. Not matched by `reclaim`/`drop_run_databases`, so a concurrent `./dc test` never drops it;
#: `_fork` refuses it.
E2E_DB_NAME = "cockpit_e2e"

#: The line prefix `dc` parses the clone URL from and must not reprint (it carries the DB password, and
#: `global-setup.ts` runs the verb into a retained CI log).
E2E_URL_LINE_PREFIX = "E2E_DATABASE_URL"

#: The second line of that contract: the key the clone's sealed rows were written under
#: (`template_credential_key()`). Filtered from the reprint as key material.
E2E_KEY_LINE_PREFIX = "E2E_CREDENTIAL_KEY"
#: The third line: the RSA private key the clone's tier-2 secret envelopes are wrapped under
#: (`template_local_private_key()`). Filtered from the reprint as key material.
E2E_LOCAL_KEY_LINE_PREFIX = "E2E_LOCAL_PRIVATE_KEY"

#: One shared key, held only for a template build and its stale-template reap, so concurrent workers
#: never build twice.
BUILD_LOCK_KEY = 195387600

#: `key1` of the two-int advisory key each run holds for its lifetime; `key2` is the run id's crc32, so
#: reclaim can test whether a leftover's owner is alive. The two-int space cannot collide with
#: `BUILD_LOCK_KEY`.
RUN_LOCK_NAMESPACE = 195387601

#: `key1` of the advisory key giving each concurrent test process a private `tasks.id` slice (`key2` is
#: the slot). Needed because the one object store keys attachments by `tasks.id`
#: (`app/attachment_storage.py::object_key`), which no database clone isolates. A claimed slot, not a
#: hash, so live processes on one Postgres server never share. Limit: advisory locks are server-local,
#: so runs on different hosts sharing a bucket can still collide.
ID_SLOT_NAMESPACE = 195387602

#: Slots and width: `tasks.id` is `integer`, and 32 x 60M ends near 1.98e9, below 2^31.
ID_SLOT_COUNT = 32
ID_SLOT_SPAN = 60_000_000

#: The shape of a run id, single-homed: `tests/conftest.py` mints/validates it and `_RUN_DB_RE` reads
#: it back, so every run DB stays reclaimable.
RUN_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")

#: `cockpit_test_gw3_1a2b3c4d` / `cockpit_test_1a2b3c4d` — a clone owned by the run whose id it carries.
#: The run-id half is `RUN_ID_RE`'s own pattern, spliced rather than retyped.
_RUN_DB_RE = re.compile(
    r"^cockpit_test(?:_gw\d+)?_(?P<run>" + RUN_ID_RE.pattern.strip("^$") + r")$"
)

#: How many hex characters of the template digest the name carries (48 bits: a collision is not a concern).
_DIGEST_LEN = 12

#: The seeders the build runs (`app.seed.bootstrap`, then its dev profile), beside this module. Their bytes
#: join the key (`template_digest`), so seeding a new fixture row rebuilds the template without a migration.
SEEDERS = ("seed.py", "seed_dev_profile.py")

#: The longest head put in a name: the scratch name is 39 + head chars (prefix, `_<digest>`,
#: `__building`), so 24 keeps it within Postgres's 63-character limit. `_ident` raises rather than
#: truncating; this bound keeps that from firing mid-build with an identifier message instead of a
#: revision one.
_MAX_HEAD_LEN = 24

#: A template this module minted, the only shape the stale-template reap may drop (`LIKE` treats `_`
#: as a wildcard and would match foreign names). `template_name` validates heads against the same
#: class, so no template can exist that the reap would never collect. The digest is optional so the
#: reap also collects a template named by head alone (before the chain digest joined the key).
_TEMPLATE_HEAD_RE = re.compile(r"^[0-9a-zA-Z]{1,%d}$" % _MAX_HEAD_LEN)
_TEMPLATE_DIGEST_RE = re.compile(r"^[0-9a-f]{%d}$" % _DIGEST_LEN)
_TEMPLATE_DB_RE = re.compile(
    r"^cockpit_fixture_[0-9a-zA-Z]{1,%d}(?:_[0-9a-f]{%d})?(?:__building)?$" % (_MAX_HEAD_LEN, _DIGEST_LEN)
)

#: Pre-template long-lived databases; nothing creates them any more.
_LEGACY_DB_RE = re.compile(r"^cockpit_test(?:_gw\d+)?$")

#: An identifier safe to interpolate into DDL (`CREATE DATABASE` takes no parameters; fork names are typed).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

#: Names a fork may never take, whatever the pattern above allows.
_RESERVED = {"postgres", "template0", "template1"}

#: Build-lock wait bound: a build takes tens of seconds, so this only bounds a wedge.
_BUILD_LOCK_TIMEOUT = 600.0


def announce_to_stdout(message: str) -> None:
    print(message, flush=True)


def _noop(message: str) -> None:  # pragma: no cover - the default for callers that want silence
    pass


#: The catalogue question the housekeeping paths ask; `_like_prefix` escapes `_`, a `LIKE` wildcard.
_DATNAME_LIKE = r"SELECT datname FROM pg_database WHERE datname LIKE %s ESCAPE '\'"


def _like_prefix(prefix: str) -> str:
    return prefix.replace("_", r"\_") + "%"


def _ident(name: str) -> str:
    """Validate a database name, or raise. Returns it quoted for DDL."""
    if not _IDENT_RE.match(name or ""):
        raise ValueError(
            f"{name!r} is not a usable database name here — letters, digits and underscores only, "
            "starting with a letter or underscore, at most 63 characters"
        )
    return f'"{name}"'


def base_url() -> str:
    """The `postgresql://user:pw@host:port/db` this deployment talks to, from its single home.

    `db_credentials.raw_dsn()`, never `os.environ` (`test_db_credentials.py` enforces it). Callers
    replace the database segment.
    """
    return db_credentials.raw_dsn()


def current_database() -> str:
    """The database name the ambient url points at — which, inside a pytest process, is this run's own
    clone rather than `cockpit`."""
    return base_url().rpartition("/")[2]


def template_credential_key() -> str:
    """The key this tree's template is sealed under, resolved once for the two processes that must agree.

    The build seals rows with it (`_build_into`); pytest (via `tests/conftest.py`) and the api under
    `./dc e2e-db up` must present it too, or every credential in the clone is undecryptable and
    `/api/credentials` 500s. `or`, not `setdefault`: empty counts as absent."""
    return os.environ.get("CREDENTIAL_ENCRYPTION_KEY") or FIXTURE_CREDENTIAL_KEY


def template_local_private_key() -> str:
    """The RSA private key this tree's template wraps secret data keys under (`app/credential_keys.py`),
    by the same rule and for the same three processes as `template_credential_key`: the build, pytest
    and the `./dc e2e-db` api must present one key, or the clone's secrets cannot be opened."""
    return "".join(os.environ.get("CREDENTIAL_LOCAL_PRIVATE_KEY", "").split()) or FIXTURE_LOCAL_PRIVATE_KEY


def database_url_for(name: str) -> str:
    """A `DATABASE_URL` naming `name` — in the `postgresql+psycopg://` spelling, not the bare one.

    SQLAlchemy reads bare `postgresql://` as psycopg2, which this image lacks; `base_url` stays bare for
    callers that pass it to psycopg directly.
    """
    head, _, _tail = base_url().rpartition("/")
    return f"{head}/{name}".replace("postgresql://", "postgresql+psycopg://", 1)


def template_digest(chain_digest: str) -> str:
    """A sha256 hex digest of `chain_digest` and every `SEEDERS` file's bytes: what the template was built from."""
    here = os.path.dirname(os.path.abspath(__file__))
    digest = hashlib.sha256(chain_digest.encode())
    for seeder in SEEDERS:
        with open(os.path.join(here, seeder), "rb") as f:
            digest.update(b"\0" + seeder.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    return digest.hexdigest()


def template_name(head: Optional[str] = None, digest: Optional[str] = None) -> str:
    """`cockpit_fixture_<head>_<digest>`, refusing a head this module cannot name safely.

    Each part defaults to this tree's (`app/schema_head.py`); `digest` is the first `_DIGEST_LEN` hex
    characters of `template_digest(chain_digest())`. Keeps the name and the reap predicate
    (`_TEMPLATE_DB_RE`) from disagreeing.
    """
    if head is None or digest is None:
        from app.schema_head import chain_key

        tree_head, tree_digest = chain_key()
        head = tree_head if head is None else head
        digest = template_digest(tree_digest)[:_DIGEST_LEN] if digest is None else digest
    if not _TEMPLATE_HEAD_RE.match(head):
        raise RuntimeError(
            f"alembic head {head!r} cannot be used as a fixture-template cache key: this module names "
            f"the template `{TEMPLATE_PREFIX}<head>_<digest>` and reclaims it by that shape, which admits "
            f"letters and digits only, at most {_MAX_HEAD_LEN} of them. Rename the revision."
        )
    if not _TEMPLATE_DIGEST_RE.match(digest):
        raise RuntimeError(
            f"chain digest {digest!r} cannot be used as a fixture-template cache key: expected "
            f"{_DIGEST_LEN} lowercase hex characters."
        )
    return f"{TEMPLATE_PREFIX}{head}_{digest}"


def run_lock_key(run_id: str) -> tuple[int, int]:
    """The two-int advisory key a run holds for its own lifetime. `key2` must fit in a signed int4."""
    raw = zlib.crc32(run_id.encode())
    return RUN_LOCK_NAMESPACE, raw - 2**32 if raw >= 2**31 else raw


#: The database every maintenance statement and advisory lock targets; never one this module drops.
MAINTENANCE_DB = "postgres"


def refuse_unless_stack_local() -> None:
    """The condition under which this module may touch a database at all. Raises, or returns.

    Two independent checks, fail-closed: `DB_SECRET_ARN` set (RDS; checked first, since there the URL
    host is not what gets dialled), and a URL host outside `db_credentials.LOCAL_DB_HOSTS` (catches
    forwards and the `awsdb` profile). CREATE/DROP FORCE/seeding a wrong DB is not undoable. A function
    because `_build_here` writes via `app.db.SessionLocal`, bypassing `maintenance_connection`. Not a
    security boundary.
    """
    if db_credentials.configured():
        raise RuntimeError(
            "app/fixture_db.py is a local test harness and refuses to run against a "
            "DB_SECRET_ARN-configured (RDS) deployment: it creates and drops databases and seeds the "
            "`--profile dev` fixture into them."
        )
    host = (urlsplit(base_url()).hostname or "").lower()
    if host not in db_credentials.LOCAL_DB_HOSTS:
        raise RuntimeError(
            f"app/fixture_db.py refuses to run against host {host or '<none>'!r}: it CREATEs and DROPs "
            f"databases and seeds the `--profile dev` fixture into them, so it runs only against a "
            f"stack-local database ({sorted(db_credentials.LOCAL_DB_HOSTS)}). Check DATABASE_URL."
        )


def maintenance_connection() -> psycopg.Connection:
    """One autocommit connection to `MAINTENANCE_DB`, credentialed through the single home
    (`db_credentials.psycopg_connect`, enforced by `test_db_credentials.py`). `dbname=` overrides only
    the database.
    """
    refuse_unless_stack_local()
    return db_credentials.psycopg_connect(dbname=MAINTENANCE_DB, autocommit=True)


def _exists(conn: psycopg.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone())


def _drop(conn: psycopg.Connection, name: str, *, force: bool) -> bool:
    """Drop `name`, reporting whether it went. `force` terminates whatever is still connected —
    right for a database this run owns, wrong for one that might belong to somebody else.

    A name this module may not interpolate is skipped, not raised, so a foreign-styled database cannot
    fail `pytest_configure` during housekeeping.
    """
    try:
        quoted = _ident(name)
    except ValueError:
        return False
    stmt = f"DROP DATABASE IF EXISTS {quoted}" + (" WITH (FORCE)" if force else "")
    try:
        conn.execute(stmt)
        return True
    except psycopg.Error:
        # An in-use database we chose not to force; its owner or a later reclaim drops it.
        return False


def _hold_build_lock(conn: psycopg.Connection, timeout: float = _BUILD_LOCK_TIMEOUT) -> None:
    """Poll (never a blocking DB call) until the build lock is ours, or raise."""
    deadline = time.monotonic() + timeout
    while True:
        if conn.execute("SELECT pg_try_advisory_lock(%s)", (BUILD_LOCK_KEY,)).fetchone()[0]:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"another session has held the fixture-template build lock (key={BUILD_LOCK_KEY}) "
                f"for {timeout:.0f}s. A build is a migrate plus two seeds and takes tens of seconds, "
                "so this is a wedged or leaked connection rather than a queue."
            )
        time.sleep(0.5)


def _release_build_lock(conn: psycopg.Connection) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (BUILD_LOCK_KEY,))


def _reap_stale_templates(conn: psycopg.Connection, keep: str, announce: Callable[[str], None]) -> None:
    """Drop every `cockpit_fixture_*` that is not the template for this migration chain.

    Only under the build lock, so dropping a half-built scratch is safe. Each template is ~35MB.
    """
    rows = conn.execute(
        _DATNAME_LIKE, (_like_prefix(TEMPLATE_PREFIX),)
    ).fetchall()
    for (name,) in rows:
        # The regex is the predicate; the LIKE only narrows.
        if name == keep or not _TEMPLATE_DB_RE.match(name):
            continue
        if _drop(conn, name, force=False):
            announce(f"fixture: dropped stale template {name} (this tree's migration chain or seeders moved)")


def ensure_template(
    *, announce: Callable[[str], None] = _noop
) -> str:
    """Return the name of this migration chain's template database, building it first if it is not there.

    Lock-free in the common case. The build goes to a scratch name renamed into place at the end, so a
    killed build leaves a reapable `…__building`, never a half-built template.
    """
    name = template_name()
    with maintenance_connection() as conn:
        if _exists(conn, name):
            # No reap here: a stale template implies a moved chain, which takes the build path (which
            # reaps). Reaping here would race another session between `_exists` and its clone.
            announce(f"fixture: reusing template {name} (migration chain unchanged)")
            return name

        _hold_build_lock(conn)
        try:
            if _exists(conn, name):
                announce(f"fixture: reusing template {name} (another session built it)")
                return name
            _reap_stale_templates(conn, name, announce)
            scratch = f"{name}__building"
            _drop(conn, scratch, force=True)
            announce(f"fixture: building template {name} (no template for this migration chain)")
            started = time.monotonic()
            conn.execute(f"CREATE DATABASE {_ident(scratch)}")
            try:
                _build_into(scratch)
            except BaseException:
                _drop(conn, scratch, force=True)
                raise
            conn.execute(f"ALTER DATABASE {_ident(scratch)} RENAME TO {_ident(name)}")
            announce(f"fixture: built template {name} in {time.monotonic() - started:.1f}s")
            return name
        finally:
            _release_build_lock(conn)


def _build_into(target: str) -> None:
    """Run `migrate -> seed -> dev fixture` against `target`, in a subprocess.

    Because `app/db.py` builds its engine at import from `DATABASE_URL`, and because process exit is
    the only disconnection guarantee that survives a seed raising half-way.
    """
    env = dict(os.environ)
    env[db_credentials.URL_VAR] = database_url_for(target)
    # The seed's fixture logins and `--profile dev` require an affirmative APP_ENV; scoped to the child.
    env["APP_ENV"] = "test"
    # Hence an explicit key: `credential_crypto`'s dev fallback fires only for a local process, and a
    # stack `.env` ships this empty.
    env["CREDENTIAL_ENCRYPTION_KEY"] = template_credential_key()
    # The secret-wrapping keypair, by the same rule: the child's `APP_ENV=test` refuses the published dev
    # keypair, and the rows it seals must open under the key pytest and the e2e api present.
    env["CREDENTIAL_LOCAL_PRIVATE_KEY"] = template_local_private_key()
    # The bcrypt cost, unconditionally, since the builder's inherited env would otherwise decide it
    # (see `FIXTURE_BCRYPT_ROUNDS`).
    env["BCRYPT_ROUNDS"] = FIXTURE_BCRYPT_ROUNDS
    # The shared governance library the seed imports: a host-run pytest reaches it only through the
    # `sys.path` entry the API's root conftest adds, which a fresh interpreter does not inherit — so it is
    # handed over on PYTHONPATH (the image sets PYTHONPATH=/packages already, and this is then a no-op).
    governance = importlib.util.find_spec("governance")
    if governance is not None and governance.origin:
        packages = os.path.dirname(os.path.dirname(governance.origin))
        inherited = env.get("PYTHONPATH", "")
        if packages not in inherited.split(os.pathsep):
            env["PYTHONPATH"] = os.pathsep.join(p for p in (packages, inherited) if p)
    proc = subprocess.run(
        [sys.executable, "-m", "app.fixture_db", "build"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        capture_output=True,
        text=True,
    )
    # Never reprint `DEV_NODE_CREDENTIAL` (a plaintext node token; this lands in CI logs); nothing needs
    # it here. Everything else is reprinted so a failing seed can say why.
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            if not line.startswith("DEV_NODE_CREDENTIAL "):
                print(line, flush=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"building the fixture template in {target} failed (exit {proc.returncode}) — the build's "
            "own output is above"
        )


def _build_here() -> None:
    """The `build` subcommand's body: migrate, seed, dev fixture, against `DATABASE_URL`.

    Refuses any target that is not a template under construction, so a mis-set `DATABASE_URL` cannot
    seed a fixture into a human's `cockpit`.
    """
    # Host before name: this path writes via `SessionLocal`, not `maintenance_connection`.
    refuse_unless_stack_local()
    target = current_database()
    if not target.startswith(TEMPLATE_PREFIX):
        raise SystemExit(
            f"app.fixture_db build refuses to seed {target!r}: it only ever writes to a "
            f"{TEMPLATE_PREFIX}* database, and DATABASE_URL does not name one."
        )

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from migrate import upgrade

    upgrade()

    from app.durable.runtime import migrate_system_schema

    # The dev seed's Live Cast publish enqueues into the engine's schema, which only a launch creates otherwise.
    migrate_system_schema()

    from app.db import SessionLocal
    from app.seed import bootstrap

    # One call, `./dc seed --dev`'s spelling: the ordinary seed, then the dev profile.
    with SessionLocal() as session:
        bootstrap(session, ensure=True, profile="dev")
        session.commit()


def clone(
    target: str,
    *,
    template: Optional[str] = None,
    announce: Callable[[str], None] = _noop,
) -> str:
    """`CREATE DATABASE <target> TEMPLATE <this chain's template>`. Returns the template used.

    Re-ensures the template once if it vanished between the lock-free resolve and the clone (a moved
    chain's reap, or a hand drop). One retry that re-resolves through `ensure_template` (build-locked);
    a second failure is raised.
    """
    for attempt in (1, 2):
        if template is None:
            template = ensure_template(announce=announce)
        try:
            with maintenance_connection() as conn:
                conn.execute(f"CREATE DATABASE {_ident(target)} TEMPLATE {_ident(template)}")
            return template
        except psycopg.errors.InvalidCatalogName:
            if attempt == 2:
                raise
            announce(
                f"fixture: template {template} disappeared before {target} could be cloned from it "
                "(a chain move, or a hand-dropped template) — rebuilding it and retrying once"
            )
            template = None
    raise AssertionError("unreachable")  # pragma: no cover


def drop_run_databases(
    run_id: str,
    *,
    announce: Callable[[str], None] = _noop,
) -> list[str]:
    """Drop every clone belonging to `run_id`. `WITH (FORCE)`: the connections it terminates are this
    run's own."""
    dropped: list[str] = []
    with maintenance_connection() as conn:
        rows = conn.execute(
            _DATNAME_LIKE, (_like_prefix(RUN_DB_PREFIX),)
        ).fetchall()
        for (name,) in rows:
            match = _RUN_DB_RE.match(name)
            if match and match.group("run") == run_id and _drop(conn, name, force=True):
                dropped.append(name)
    if dropped:
        announce(f"fixture: dropped {', '.join(sorted(dropped))}")
    return dropped


def reclaim(
    *,
    run_id: str,
    announce: Callable[[str], None] = _noop,
) -> list[str]:
    """Drop every clone whose owning run is gone, plus any pre-template long-lived database.

    The only reclaim path for killed runs (no teardown ran). Liveness is asked, not assumed: each run
    holds an advisory lock on its id, so a takeable lock means a dead owner, and concurrent sessions
    never eat each other's databases.
    """
    reclaimed: list[str] = []
    with maintenance_connection() as conn:
        rows = conn.execute(
            _DATNAME_LIKE, (_like_prefix(RUN_DB_PREFIX),)
        ).fetchall()
        for (name,) in rows:
            if _LEGACY_DB_RE.match(name):
                # Pre-template residue; never forced, in case an old session still uses it.
                if _drop(conn, name, force=False):
                    reclaimed.append(name)
                continue
            match = _RUN_DB_RE.match(name)
            if not match:
                continue
            owner = match.group("run")
            if owner == run_id:
                continue
            key1, key2 = run_lock_key(owner)
            if not conn.execute("SELECT pg_try_advisory_lock(%s, %s)", (key1, key2)).fetchone()[0]:
                continue  # its owner is alive and still running tests
            try:
                if _drop(conn, name, force=True):
                    reclaimed.append(name)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s, %s)", (key1, key2))
    if reclaimed:
        announce(f"fixture: reclaimed {len(reclaimed)} orphaned database(s): {', '.join(sorted(reclaimed))}")
    return reclaimed


def id_slice_start(slot: int) -> int:
    """The first `tasks.id` a process holding `slot` may use.

    A function so its contract is testable: slot 0 must not keep the template's own band, slices are
    `ID_SLOT_SPAN` apart, and the last ends within `integer`. Slot 0 is unobservable from inside a run
    (the suite holds it).
    """
    return (slot + 1) * ID_SLOT_SPAN


def claim_id_space(dbname: str) -> "tuple[psycopg.Connection | None, int | None]":
    """Give `dbname` a private slice of the `tasks.id` space, and return the lock holding it.

    See `ID_SLOT_NAMESPACE`. Hold the returned connection open for the process's life; closing it
    releases the slot. Out of slots returns `(None, None)` silently (never a failure); the caller
    reports it (`tests/conftest.py`, from an xdist worker).
    """
    holder = maintenance_connection()
    for slot in range(ID_SLOT_COUNT):
        if holder.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", (ID_SLOT_NAMESPACE, slot)
        ).fetchone()[0]:
            start = id_slice_start(slot)
            with db_credentials.psycopg_connect(dbname=dbname, autocommit=True) as conn:
                conn.execute(f"ALTER SEQUENCE tasks_id_seq RESTART WITH {start}")
            return holder, slot
    holder.close()
    return None, None


def _fork(name: str) -> None:
    """`./dc db fork <name>` — a private copy of the fixture for a human to poke at.

    Refuses an existing name rather than handing back a stale copy.
    """
    if name in _RESERVED or name == current_database() or name == E2E_DB_NAME:
        raise SystemExit(f"./dc db fork: {name!r} is not a name you may fork onto.")
    # Nor a name the harness reclaims: a run-id-shaped name would be force-dropped by the next `./dc test`.
    if _RUN_DB_RE.match(name) or _LEGACY_DB_RE.match(name) or name.startswith(TEMPLATE_PREFIX):
        raise SystemExit(
            f"./dc db fork: {name!r} has the shape of a database the test harness owns and reclaims by "
            f"itself — the next `./dc test` would drop it while you were using it. Pick a name that is "
            f"not `{RUN_DB_PREFIX}…` and not `{TEMPLATE_PREFIX}…`."
        )
    # SystemExit, not `_ident`'s ValueError, so a typo is a one-line refusal, not a traceback.
    try:
        _ident(name)
    except ValueError as exc:
        raise SystemExit(f"./dc db fork: {exc}") from None
    with maintenance_connection() as conn:
        if _exists(conn, name):
            raise SystemExit(
                f"./dc db fork: database {name!r} already exists — refusing to reuse it.\n"
                f"  A fork is meant to be a fresh copy of the fixture; handing back an old one would "
                f"be a stale experiment that looks new.\n"
                f"  Drop it first:  ./dc psql -c 'DROP DATABASE \"{name}\"'"
            )
    started = time.monotonic()
    template = clone(name, announce=announce_to_stdout)
    print(
        f"./dc db fork: created {name} from {template} in {time.monotonic() - started:.2f}s\n"
        f"  Connect with:  ./dc psql -d {name}",
        flush=True,
    )


def e2e_create() -> str:
    """`./dc e2e-db up`'s database half. Returns the `DATABASE_URL` the api must be pointed at.

    Drops a leaked copy rather than adopting it (a killed run leaves half-written rows), `WITH (FORCE)`
    since the dead run's api is likely still connected. The URL comes from `database_url_for`, keeping
    the checkout's user, host and port.
    """
    # As `_fork` refuses: this verb force-drops `E2E_DB_NAME`, so a checkout pointed at it is told, not
    # destroyed.
    if current_database() == E2E_DB_NAME:
        raise SystemExit(
            f"fixture: DATABASE_URL names {E2E_DB_NAME!r}, which is the throwaway database this verb "
            "DROPs and recreates. Point this stack at its own database before running the smoke suite."
        )
    started = time.monotonic()
    with maintenance_connection() as conn:
        if _exists(conn, E2E_DB_NAME):
            # Raised, not fallen through: `_drop` reports refusal by returning False, and adopting a
            # leaked copy is the one thing this must not do.
            if not _drop(conn, E2E_DB_NAME, force=True):
                raise SystemExit(
                    f"fixture: {E2E_DB_NAME} is present and could not be dropped, so this run would "
                    "have to adopt an interrupted run's leftovers — which is the one thing it must "
                    "not do. Something is still connected that `WITH (FORCE)` could not terminate."
                )
            print(
                f"fixture: dropped a leaked {E2E_DB_NAME} — an interrupted smoke run left it behind",
                flush=True,
            )
    template = clone(E2E_DB_NAME, announce=announce_to_stdout)
    print(
        f"fixture: created {E2E_DB_NAME} from {template} in {time.monotonic() - started:.2f}s",
        flush=True,
    )
    # Re-base the session clock (see `_REBASE_SESSION_CLOCK`).
    with db_credentials.psycopg_connect(dbname=E2E_DB_NAME, autocommit=True) as conn:
        moved = rebase_session_clock(conn)
    print(f"fixture: re-based {moved} seeded session rows onto this run's clock", flush=True)
    return database_url_for(E2E_DB_NAME)


#: The clone's session clock, re-based onto the run that made it. Seeded `sessions` rows carry absolute
#: timestamps from template build time, while `/sessions` defaults to a 24h window
#: (`apps/web/src/pages/audit/SessionsTab.tsx`), so a day-old template shows no seeded session.
#: Shifted on the clone (owned by this run), never the template (would race cloners); one shift off the
#: newest `started_at`, preserving relative spacing.
_REBASE_SESSION_CLOCK = """
UPDATE sessions AS s SET
    created_at           = s.created_at + d.shift,
    updated_at           = s.updated_at + d.shift,
    started_at           = s.started_at + d.shift,
    ended_at             = s.ended_at + d.shift,
    proc_started_at      = s.proc_started_at + d.shift,
    transcript_leased_at = s.transcript_leased_at + d.shift
FROM (SELECT now() - max(started_at) AS shift FROM sessions) AS d
WHERE d.shift > interval '0'
"""


def rebase_session_clock(conn: psycopg.Connection) -> int:
    """Move `conn`'s seeded `sessions` rows forward so the newest one starts now. Returns rows moved.

    Forward only, so a backwards host clock never ages the fixture; zero moved is ordinary.
    """
    return conn.execute(_REBASE_SESSION_CLOCK).rowcount


def e2e_drop() -> bool:
    """`./dc e2e-db down`'s database half; reports whether anything went.

    Still `WITH (FORCE)`: the old container's backends may not have left yet, and an unforced drop that
    declines would leak silently.
    """
    with maintenance_connection() as conn:
        if not _exists(conn, E2E_DB_NAME):
            print(f"fixture: {E2E_DB_NAME} was already gone; nothing to drop", flush=True)
            return True
        if not _drop(conn, E2E_DB_NAME, force=True):
            return False
    print(f"fixture: dropped {E2E_DB_NAME}", flush=True)
    return True


def main(argv: Optional[list[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    verb = args[0] if args else ""
    if verb == "build":
        _build_here()
    elif verb == "ensure":
        ensure_template(announce=announce_to_stdout)
    elif verb == "fork":
        if len(args) != 2:
            raise SystemExit("usage: python -m app.fixture_db fork <name>")
        _fork(args[1])
    elif verb == "e2e-create":
        # One machine-readable line; `dc` greps this prefix. Everything else is prose.
        url = e2e_create()
        print(f"{E2E_URL_LINE_PREFIX} {url}", flush=True)
        # This can carry a real configured key. `dc` filters both lines; never add an unfiltered
        # reprint or run the verb under `bash -x`.
        print(f"{E2E_KEY_LINE_PREFIX} {template_credential_key()}", flush=True)
        print(f"{E2E_LOCAL_KEY_LINE_PREFIX} {template_local_private_key()}", flush=True)
    elif verb == "e2e-drop":
        if not e2e_drop():
            raise SystemExit(f"fixture: could not drop {E2E_DB_NAME} — it is still there.")
    else:
        raise SystemExit(
            "usage: python -m app.fixture_db {build|ensure|fork <name>|e2e-create|e2e-drop}"
        )


if __name__ == "__main__":
    main()
