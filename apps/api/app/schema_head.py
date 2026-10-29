"""schema_head.py — the single home for deriving this tree's alembic head revision and chain digest.

Both are a cache key: the test harness names its template database `cockpit_fixture_<head>_<digest>`
(`app/fixture_db.py`), so every caller must derive them identically — two spellings that disagree
silently build a second template instead of failing. The head alone is not enough: a revision renumbered
or re-chained beneath an unchanged head would reuse a template missing its migration.

Raises on anything but exactly one head: a forked history has no single answer, so a cache keyed on it
would key on an arbitrary one. `test_alembic_head.py` owns the assertion that every revision descends to that head;
this module owns the derivation. The alembic paths are single-homed in `migrate.py::alembic_config`.
"""
from __future__ import annotations

import hashlib
import os
import sys


def _api_root() -> str:
    """`apps/api` — this file is `apps/api/app/schema_head.py`, so two levels up."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _script_directory():
    """This tree's alembic `ScriptDirectory`.

    `migrate.py` is a top-level module in `apps/api`, importable only with `apps/api` on `sys.path`; a
    host-side tool importing `app.schema_head` need not have it, so it is inserted here, making this work
    from any cwd.
    """
    from alembic.script import ScriptDirectory

    root = _api_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    from migrate import alembic_config

    return ScriptDirectory.from_config(alembic_config())


def chain_digest(script=None) -> str:
    """A sha256 hex digest of every revision in the chain: its id and its file's bytes.

    The bytes carry `down_revision`, so renumbering or re-chaining any revision — or editing one — moves
    the digest even when the head id stays put. `script` defaults to this tree's `ScriptDirectory`.
    """
    script = script or _script_directory()
    digest = hashlib.sha256()
    for rev in sorted(script.walk_revisions(), key=lambda r: r.revision):
        with open(rev.path, "rb") as f:
            body = f.read()
        digest.update(rev.revision.encode() + b"\0" + hashlib.sha256(body).digest())
    return digest.hexdigest()


def chain_key() -> tuple[str, str]:
    """`(current_head(), chain_digest())` from one load of the revision directory."""
    script = _script_directory()
    return _single_head(script), chain_digest(script)


def current_head() -> str:
    """This tree's single alembic head revision id (e.g. `"0248"`)."""
    return _single_head(_script_directory())


def _single_head(script) -> str:
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(
            f"expected exactly one alembic head, found {heads!r}. A forked or empty revision history "
            "has no single answer to 'which schema is this', so nothing may key a cache on it — merge "
            "the heads (or check your `down_revision` chains onto the previous head) first."
        )
    return heads[0]
