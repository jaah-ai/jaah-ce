"""The database credential, resolved in process from Secrets Manager and re-resolved when Postgres
refuses it.

The RDS master password is AWS-rotated, and an ECS `secrets` env var is read once at task start, so
the fix is a credential source, not a TTL. One `do_connect` listener on the `Engine` class covers every
engine; `psycopg_connect`/`psycopg_connect_async` cover the raw dialers; all share `_attempt` +
`_replacement`. Census tests forbid reading `DATABASE_URL` or opening psycopg directly. Inert without
`DB_SECRET_ARN`; no network I/O at import. Nothing secret is logged or raised (`_Credential.__repr__`
redacts; refusals name keys, never values).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from sqlalchemy import event
from sqlalchemy.engine import Engine

from app import edition_features

_log = logging.getLogger(__name__)

# Written by the deployment's task definition; `DB_SECRET_ARN`'s presence
# puts this module in charge of the credential.
SECRET_ARN_VAR = "DB_SECRET_ARN"
#: The variable carrying the whole url locally, exported so the one caller that sets it
#: (`app/fixture_db.py`) names it through here; `test_db_credentials.py`'s census forbids a second speller.
URL_VAR = "DATABASE_URL"

#: The hosts that count as "this developer's own stack", single-homed for `app/seed_dev_profile.py`
#: and `app/fixture_db.py`, which refuse irreversible work elsewhere. Not a security boundary (a
#: port-forward can sit behind `localhost`); both callers fail closed on an unrecognised or hostless url.
LOCAL_DB_HOSTS: frozenset[str] = frozenset({"postgres", "localhost", "127.0.0.1", "::1"})

HOST_VAR = "DB_HOST"
PORT_VAR = "DB_PORT"
NAME_VAR = "DB_NAME"

# TLS ON THE RDS PATH ONLY, server certificate verified: `verify-full` checks the chain against the RDS CA
# bundle AND the certificate's name against `DB_HOST`, so no hop can impersonate the database. The bundle is
# baked into the api image (sha256-pinned in its Dockerfile) and `DB_SSLROOTCERT` names where; the
# default is that same path, so an image is never one env var away from an unverified session. The compose /
# `DATABASE_URL` path is untouched: the local Postgres speaks no TLS. Fallback, should a CA mismatch ever take
# an environment down: `require` (encrypted, unverified) in `RDS_SSLMODE` — the one constant to change
# (`tls_parameters` then drops `sslrootcert`, which would otherwise keep libpq verifying the chain).
SSLROOTCERT_VAR = "DB_SSLROOTCERT"
RDS_CA_BUNDLE_PATH = "/etc/ssl/certs/rds-global-bundle.pem"
RDS_SSLMODE = "verify-full"

# The two keys an RDS-managed master secret carries (AWS's shape).
_USERNAME_KEY = "username"
_PASSWORD_KEY = "password"

# The local compose stack's URL, used when neither `DB_SECRET_ARN` nor `DATABASE_URL` is set, so a bare
# `import app.db` on a dev box works. Single-homed: every "where is the database" routes through
# `database_url()`.
COMPOSE_DEFAULT_URL = "postgresql+psycopg://cockpit:cockpit@postgres:5432/cockpit"

# SQLSTATEs for a rejected credential: 28P01 `invalid_password`, 28000
# `invalid_authorization_specification`.
#
# Checked first but not sufficient: psycopg (pinned 3.2.13) leaves `sqlstate` None on a connect-phase
# failure — the diagnostic is only libpq's message text — so the text match below is the production path.
# The SQLSTATE check covers mid-session failures and a future psycopg that populates it.
_AUTH_FAILURE_SQLSTATES = frozenset({"28P01", "28000"})

# Lowercased substrings of libpq's auth-failure messages. Deliberately narrow: `fe_sendauth: no
# password supplied` is absent because it means nothing was injected — a wiring bug a fresh credential
# cannot fix, which must surface loudly.
_AUTH_FAILURE_TEXTS = (
    "password authentication failed",
    "authentication failed for user",
)


@dataclass(frozen=True, repr=False)
class _Credential:
    """One resolved database credential, plus the generation that identifies it without reading it.

    `generation` lets the retry path detect replacement without comparing passwords; it increments
    once per successful fetch that changed the credential.

    The `__repr__` override is the leak guard (`repr`, `str`, f-strings, containers). `dataclasses.asdict`,
    `astuple` and pytest's equality rewriting still render fields, so never use them on a real credential.
    """

    username: str
    password: str
    generation: int

    def same_secret_as(self, other: "_Credential") -> bool:
        """Whether `other` carries the same username and password — WITHOUT rendering either.

        `hmac.compare_digest`: no timing signal, and unlike a dataclass `==`, a failing pytest assertion
        cannot print both passwords.
        """
        # Encoded: `hmac.compare_digest` raises `TypeError` on non-ASCII `str`, which here would replace
        # Postgres's real refusal with a `TypeError`.
        return hmac.compare_digest(
            self.username.encode("utf-8"), other.username.encode("utf-8")
        ) and hmac.compare_digest(self.password.encode("utf-8"), other.password.encode("utf-8"))

    def __repr__(self) -> str:
        return (
            f"_Credential(username=<redacted>, password=<redacted>, generation={self.generation})"
        )


# Process-local cache; connections open from handlers, scheduler threads, DBOS's pool and AnyIO, so it
# is locked. Held across the fetch on purpose: a cold-cache storm sends one `GetSecretValue`, since
# waiters then find a newer generation. A failed fetch caches nothing (`_FAILURE_HOLD_SECONDS` bounds
# that); one attempt can take several times `secretsmanager.DEFAULT_TIMEOUT_SECONDS` (httpx per phase).
_lock = threading.Lock()
_cached: _Credential | None = None
_generation = 0

# How long a failed fetch is remembered and re-raised instead of retried, making failure single-flight.
# Short on purpose — a debounce, not a circuit breaker — so a transient blip self-heals on the next
# connection.
_FAILURE_HOLD_SECONDS = 1.0
_failure: tuple[BaseException, float] | None = None

# Bounds the refusal storm (a wrong password or a rotation window under load). `_replaced` records that a
# refusal of this generation was already answered by a real fetch; with an identical re-read not
# advancing the generation, a storm costs one call. A real rotation still recovers on the first refusal.
# `_MIN_FETCH_INTERVAL_SECONDS` stops `_replaced` from latching: one attempt per window gets through.
_MIN_FETCH_INTERVAL_SECONDS = 5.0
_fetched_at = 0.0
_replaced: int | None = None


def reset_cache() -> None:
    """Test seam only — the cache is process-local and is never cleared in production."""
    global _cached, _generation, _failure, _fetched_at, _replaced
    with _lock:
        _cached = None
        _generation = 0
        _failure = None
        _fetched_at = 0.0
        _replaced = None


def secret_arn() -> str:
    """The configured secret's ARN, or `""` when this module is not in charge of the credential."""
    return os.environ.get(SECRET_ARN_VAR, "").strip()


def configured() -> bool:
    """Whether the database credential comes from Secrets Manager in this deployment.

    False off ECS and in `database_url_override` mode.
    """
    return secret_arn() != ""


def dsn() -> str:
    """The credential-less SQLAlchemy URL for the configured server — what `database_url()` returns.

    No userinfo: the listener injects the credential at connect time. SQLAlchemy, Alembic and DBOS's
    `is_valid_database_url` all tolerate the omission.

    Raises `RuntimeError` when `DB_SECRET_ARN` is set without its plain fields, and `ValueError` for a
    malformed ARN — a refusal to start, not an engine pointing nowhere. All four fail at the same moment.
    """
    host = os.environ.get(HOST_VAR, "").strip()
    name = os.environ.get(NAME_VAR, "").strip()
    port = os.environ.get(PORT_VAR, "").strip() or "5432"
    missing = [var for var, value in ((HOST_VAR, host), (NAME_VAR, name)) if not value]
    if missing:
        raise RuntimeError(
            f"{SECRET_ARN_VAR} is set, so the database credential is read from Secrets Manager, "
            f"but {' and '.join(missing)} " + ("is" if len(missing) == 1 else "are") + " not set"
        )
    if not port.isdigit():
        raise RuntimeError(f"{PORT_VAR} must be a port number")
    if not edition_features.enabled("aws"):
        raise RuntimeError(f"{SECRET_ARN_VAR} is set, but this edition reads no credential from Secrets Manager")
    # The ARN is validated at boot with its neighbours, not first inside a `do_connect` hook.
    from app.aws import secretsmanager

    secretsmanager.region_of(secret_arn())
    return f"postgresql+psycopg://{host}:{port}/{name}?{urlencode(tls_parameters(), safe='/')}"


def tls_parameters() -> dict[str, str]:
    """The libpq TLS parameters every RDS-path connection carries — see `RDS_SSLMODE`.

    `sslrootcert` only for a verifying mode: libpq treats `require` WITH a root cert as `verify-ca`, so
    the `require` fallback must drop it to stop checking the chain.
    """
    if not RDS_SSLMODE.startswith("verify-"):
        return {"sslmode": RDS_SSLMODE}
    return {
        "sslmode": RDS_SSLMODE,
        "sslrootcert": os.environ.get(SSLROOTCERT_VAR, "").strip() or RDS_CA_BUNDLE_PATH,
    }


def database_url() -> str:
    """The URL every engine in this process is built from — the SINGLE HOME for that decision.

    Three shapes, by deployment:
      * `DB_SECRET_ARN` set (the CP's default RDS deployment) — the credential-less `dsn()` above.
      * `DATABASE_URL` set (local compose, tests, `database_url_override`) — used verbatim.
      * neither — `COMPOSE_DEFAULT_URL`.

    `DB_SECRET_ARN` wins: both set is a misconfiguration, and the rotating credential must be in force.

    `app/db.py::database_url` re-exports this; it lives here because `app/events.py` and `app/config.py`
    need it and cannot import the ORM.
    """
    if configured():
        return dsn()
    return os.environ.get(URL_VAR, COMPOSE_DEFAULT_URL)


def raw_dsn() -> str:
    """`database_url()` as a plain libpq DSN — psycopg has no use for SQLAlchemy's `+psycopg` suffix.

    Only meaningful when this module is not in charge: under `DB_SECRET_ARN` it has no userinfo.
    `psycopg_connect` chooses between this and `libpq_parameters()`.
    """
    return database_url().replace("postgresql+psycopg://", "postgresql://")


def libpq_parameters() -> dict[str, object]:
    """The configured server as libpq connection PARAMETERS — no credential, no DSN to escape.

    Lets `_attempt` credential a raw dialer as it does SQLAlchemy's `cparams`. Parameters because an
    AWS-generated password is not guaranteed safe unescaped in a DSN.
    """
    dsn()  # the validation, in one place — raises RuntimeError/ValueError on a bad configuration
    return {
        "host": os.environ.get(HOST_VAR, "").strip(),
        "port": int(os.environ.get(PORT_VAR, "").strip() or "5432"),
        "dbname": os.environ.get(NAME_VAR, "").strip(),
        **tls_parameters(),
    }


def _parse(secret_string: str) -> tuple[str, str]:
    """The username and password out of an RDS-managed secret's JSON document.

    Raises `ValueError` naming the key that is wrong, never a value.
    """
    try:
        document = json.loads(secret_string)
    except (ValueError, RecursionError):
        raise ValueError("the secret's value is not a JSON document") from None
    if not isinstance(document, dict):
        raise ValueError("the secret's value is not a JSON object")
    values: list[str] = []
    for key in (_USERNAME_KEY, _PASSWORD_KEY):
        value = document.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"the secret has no non-empty {key!r}")
        values.append(value)
    return values[0], values[1]


def resolve(*, replacing: int | None = None) -> _Credential:
    """The current database credential, fetching it from Secrets Manager only when it must.

    `replacing` is the generation Postgres just refused: the cache serves anything newer, else a fresh
    `GetSecretValue`. `replacing=None` is the ordinary cached connect path.

    No TTL: Postgres's refusal is what says the credential is stale; a timer would only guess.

    Raises whatever `secretsmanager.get_secret_value` raises, or `ValueError` for a malformed secret;
    never returns a stale credential instead of an error.

    `secretsmanager` is imported here because `app/db.py` imports this module at module level and the
    secretsmanager client pulls in `httpx`, which many `pytest_stage -i` stages do not declare.
    """
    if not edition_features.enabled("aws"):
        # Unreachable: `dsn()` refuses a Secrets Manager credential in an edition without `aws`.
        raise RuntimeError(f"{SECRET_ARN_VAR} is set, but this edition reads no credential from Secrets Manager")
    from app.aws import secretsmanager

    global _cached, _generation, _failure, _fetched_at, _replaced
    with _lock:
        cached = _cached
        if cached is not None and (replacing is None or cached.generation > replacing):
            return cached
        if (
            cached is not None
            and _replaced == replacing
            and time.monotonic() - _fetched_at < _MIN_FETCH_INTERVAL_SECONDS
        ):
            # This generation's refusal was already answered by a fetch returning the same credential; serve the
            # cache so `_replacement` lets the refusal propagate without a second fetch.
            return cached
        held = _failure
        if held is not None:
            error, at = held
            if time.monotonic() - at < _FAILURE_HOLD_SECONDS:
                # A fetch failed a moment ago; re-raise rather than repeat it (see `_FAILURE_HOLD_SECONDS`).
                raise error
            _failure = None
        try:
            username, password = _parse(secretsmanager.get_secret_value(secret_arn()))
        except Exception as error:
            _failure = (error, time.monotonic())
            raise
        _failure = None
        _fetched_at = time.monotonic()
        unchanged = cached is not None and hmac.compare_digest(
            cached.username.encode("utf-8"), username.encode("utf-8")
        ) and hmac.compare_digest(cached.password.encode("utf-8"), password.encode("utf-8"))
        if unchanged:
            # An identical re-read does not advance the generation: a generation identifies a credential, not a
            # fetch. Otherwise every refusal in a storm would look like a new question.
            _replaced = replacing
            return cached
        _generation += 1
        _cached = _Credential(username=username, password=password, generation=_generation)
        _replaced = None
        _log.info(
            "database credential resolved from %s (generation %s)", SECRET_ARN_VAR, _generation
        )
        return _cached


def is_auth_failure(exc: BaseException) -> bool:
    """Whether `exc` is Postgres refusing the credential — as opposed to any other connect failure.

    Narrow on purpose: DNS, closed port, full slots or a wrong dbname must propagate untouched. See
    `_AUTH_FAILURE_SQLSTATES` for why the text match is what fires. `exc.orig` is unwrapped for callers
    holding a SQLAlchemy `OperationalError`.
    """
    candidates = [exc]
    original = getattr(exc, "orig", None)
    if isinstance(original, BaseException):
        candidates.append(original)
    for candidate in candidates:
        for state in (
            getattr(candidate, "sqlstate", None),
            getattr(getattr(candidate, "diag", None), "sqlstate", None),
        ):
            if isinstance(state, str) and state in _AUTH_FAILURE_SQLSTATES:
                return True
        if any(marker in _driver_message(candidate) for marker in _AUTH_FAILURE_TEXTS):
            return True
    return False


def _scrub(exc: BaseException) -> None:
    """Drop psycopg's `FinishedPGconn` from a connect failure, because its `__repr__` PRINTS THE
    PASSWORD.

    On psycopg 3.2.13 every connect-phase `OperationalError` carries `_pgconn`, rendered by
    `repr(exc.__dict__)`/`vars(exc)` though not by `str`/`repr`. The census test
    `test_no_module_renders_an_exception_s_attributes` is kept too: it catches `capture_locals`.

    `object.__setattr__` on the private attribute because `pgconn` is read-only. `str(exc)` and
    `sqlstate` survive, so `is_auth_failure` is unaffected. Silent when the attribute is absent: it must
    never replace the failure it cleans up.
    """
    try:
        if getattr(exc, "_pgconn", None) is not None:
            object.__setattr__(exc, "_pgconn", None)
    except Exception:  # noqa: BLE001 - a cleanup that raised would mask the real failure
        pass


def _driver_message(exc: BaseException) -> str:
    """`exc`'s own message, lowercased, WITHOUT any statement or parameters rendered after it.

    A security boundary: `StatementError.__str__` appends `[SQL: …]` and, without `hide_parameters`,
    `[parameters: …]`, so user-supplied text (a task titled "password authentication failed") could
    trigger the credential-refetch path. Matters for callers holding a wrapped error.
    """
    return str(exc).split("\n[SQL:", 1)[0].lower()


def _targets_the_configured_server(cparams: dict) -> bool:
    """Whether a pending connection is going to the server whose credential this module holds.

    Compares host, and port with 5432 defaulted on both sides. Not `dbname`: the master credential
    works on every database on the instance, and DBOS may open another one.

    `hostaddr` is refused: libpq connects to the address while using `host` for TLS/auth, so it would
    otherwise inject the master credential into a connection to an arbitrary address.
    """
    host = os.environ.get(HOST_VAR, "").strip()
    if cparams.get("host") != host:
        return False
    if int(cparams.get("port") or 5432) != int(os.environ.get(PORT_VAR, "").strip() or 5432):
        return False
    hostaddr = cparams.get("hostaddr")
    if hostaddr is not None and hostaddr != host:
        _log.warning(
            "refusing to inject the database credential: the connection names the configured host "
            "but overrides it with a different hostaddr"
        )
        return False
    return True


# ── The retry policy, in one place for all three dialers ──────────────────────────────────────────
#
# `_connect` (every engine), `psycopg_connect` (cron advisory lock) and `psycopg_connect_async` (LISTEN)
# differ only in how they open a socket; which connections get a credential, what counts as a refusal,
# and when to retry live in the two functions below.


def _attempt(base: dict) -> tuple[dict, _Credential | None]:
    """Connection parameters for one attempt, plus the credential in them (`None` when not ours).

    `None` means connect exactly as if this module did not exist (off ECS, or another server).

    Overwrites any credential already in `base`: honouring a passed one would silently opt that
    connection out of rotation resilience. Overwrites the TLS parameters for the same reason: no caller can
    hand the master credential to an unverified session.
    """
    if not configured() or not _targets_the_configured_server(base):
        return base, None
    credential = resolve()
    return {
        **base,
        **tls_parameters(),
        "user": credential.username,
        "password": credential.password,
    }, credential


def _replacement(credential: _Credential, exc: BaseException) -> _Credential:
    """The credential to retry `exc` with — or `exc` re-raised, when a retry cannot help.

    Exactly one retry, only for an auth failure; a second failure is a wrong credential and surfaces.
    Skipped when the re-read returns the same credential (compared by value, not generation) — the
    "password is simply wrong" case.

    A Secrets Manager failure here is chained to the refusal (`raise … from exc`), so the operator sees
    both facts.
    """
    _scrub(exc)
    if not is_auth_failure(exc):
        raise exc
    # Nothing from this path is logged beyond the username; the rule is uniform so nobody decides case by case.
    _log.warning(
        "postgres refused the database credential (generation %s); re-resolving from %s",
        credential.generation,
        SECRET_ARN_VAR,
    )
    try:
        fresh = resolve(replacing=credential.generation)
    except Exception as failure:
        raise failure from exc
    if fresh.same_secret_as(credential):
        raise exc
    return fresh


def _connect(dialect, connection_record, cargs, cparams):  # type: ignore[no-untyped-def]
    """The chokepoint for every SQLAlchemy engine: credential the connection, and retry a refused one.

    `None` means connect the default way. Otherwise the connect is made here, the only way to see and
    answer the refusal. `cparams` is mutated in place because SQLAlchemy reuses the dict.
    """
    parameters, credential = _attempt(cparams)
    if credential is None:
        return None
    cparams.update(parameters)
    try:
        return dialect.connect(*cargs, **cparams)
    except Exception as exc:
        # The retry's own failure propagates from here, so it is scrubbed too.
        fresh = _replacement(credential, exc)
        cparams["user"] = fresh.username
        cparams["password"] = fresh.password
        try:
            connection = dialect.connect(*cargs, **cparams)
        except Exception as retried:
            _scrub(retried)
            raise
        _log.info(
            "database credential re-resolved after a refusal (generation %s)", fresh.generation
        )
        return connection


def psycopg_connect(**options: object):  # type: ignore[no-untyped-def]
    """One RAW psycopg connection to this deployment's database, credentialed and retried.

    For dialers the listener cannot reach (`app/cron_runtime.py`'s advisory lock). `options` pass
    through. `psycopg` is imported locally for the reason `resolve` gives.
    """
    import psycopg

    parameters, credential = _attempt(libpq_parameters() if configured() else {})
    if credential is None:
        return psycopg.connect(raw_dsn(), **options)  # type: ignore[arg-type]
    try:
        return psycopg.connect(**parameters, **options)  # type: ignore[arg-type]
    except Exception as exc:
        fresh = _replacement(credential, exc)
        parameters |= {"user": fresh.username, "password": fresh.password}
        try:
            connection = psycopg.connect(**parameters, **options)  # type: ignore[arg-type]
        except Exception as retried:
            _scrub(retried)
            raise
        _log.info(
            "database credential re-resolved after a refusal (generation %s)", fresh.generation
        )
        return connection


async def psycopg_connect_async(**options: object):  # type: ignore[no-untyped-def]
    """`psycopg_connect` for the one dialer that needs an async connection — the LISTEN loop.

    `resolve` blocks the loop for one bounded `GetSecretValue` per connect attempt; accepted on a loop
    otherwise idle waiting for notifications.
    """
    import psycopg

    parameters, credential = _attempt(libpq_parameters() if configured() else {})
    if credential is None:
        return await psycopg.AsyncConnection.connect(raw_dsn(), **options)  # type: ignore[arg-type]
    try:
        return await psycopg.AsyncConnection.connect(**parameters, **options)  # type: ignore[arg-type]
    except Exception as exc:
        fresh = _replacement(credential, exc)
        parameters |= {"user": fresh.username, "password": fresh.password}
        try:
            connection = await psycopg.AsyncConnection.connect(**parameters, **options)  # type: ignore[arg-type]
        except Exception as retried:
            _scrub(retried)
            raise
        _log.info(
            "database credential re-resolved after a refusal (generation %s)", fresh.generation
        )
        return connection


_installed = False


def install() -> None:
    """Register the chokepoint on the `Engine` CLASS — once per process, for every engine.

    Idempotent: a second listener would short-circuit behind the first. Registered unconditionally —
    the listener checks configuration at connect time, so a test can set `DB_SECRET_ARN` and exercise
    the real wiring.
    """
    global _installed
    if _installed:
        return
    event.listen(Engine, "do_connect", _connect)
    _installed = True
