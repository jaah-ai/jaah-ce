"""A credential's stored health facts: the rules behind `credentials.has_stored_secret` and `.expires_at`, and the
two stamps that write `.last_used_at` (`record_use`) and `.last_tested_at`/`.last_test_ok` (`record_test`).

The two facts are derived from a config dict as `Credential.config`'s setter receives it (plaintext secrets or
`SealedSecret` markers) and written beside the sealed column on every config write, so the roster can filter
on them with an indexed predicate instead of opening every row. The setter is the only caller on the write
path; the backfill revision (202610232140) applies the same two functions to each row's opened tier 1.

The stamps are best-effort Core UPDATEs, each in a SAVEPOINT and committed at once, so no row lock outlives the
call and a failure is logged and swallowed: a stamp never fails or changes the answer of the request it rides.
They are machinery, never an edit: revision 202610232200 scopes `trg_credentials_set_updated_at` to every column
but `MACHINERY_COLUMNS`, so a stamp leaves `updated_at` alone.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy import func, or_, text, update
from sqlalchemy.orm import Session
from sqlalchemy.sql import Executable

from app.clock import as_utc
from app.credentials.registry import CREDENTIAL_TYPES, secret_keys
from app.entity_shape import is_blank
from app.models.credentials import Credential

log = logging.getLogger("credential-health")

#: The columns the stamps write: outside the `updated_at` trigger's column list (revision 202610232200), so a
#: stamp is not an edit. `tests/test_credential_health_stamps.py` holds the installed trigger to the rest.
MACHINERY_COLUMNS: tuple[str, ...] = ("last_used_at", "last_tested_at", "last_test_ok")

#: `record_use` writes a row at most this often (R3): every other fetch in the window is a no-op UPDATE.
USE_STAMP_EVERY = timedelta(hours=1)

#: How long a stamp waits on a row lock; ended by the commit right after it (`SET LOCAL`).
_LOCK_WAIT = text("SET LOCAL lock_timeout = '500ms'")


def record_use(session: Session, ids: Iterable[Optional[UUID]]) -> None:
    """Stamp `last_used_at` on the credentials a node or runner just fetched, at most once an hour per row.

    Called only by the node and runner fetch routes (`routes/agent/task_secrets.py`) once the secret is resolved and
    the answer built: "used" means a node fetched it, never that the control plane tested or refreshed it, so
    `credential_crypto.open_secret` does not call it. Commits `session`."""
    wanted = sorted({credential_id for credential_id in ids if credential_id is not None})
    if not wanted:
        return
    column = Credential.__table__.c
    _stamp(session, "use", update(Credential.__table__).where(
        column.id.in_(wanted),
        or_(column.last_used_at.is_(None), column.last_used_at < func.now() - USE_STAMP_EVERY),
    ).values(last_used_at=func.now()))


def record_test(session: Session, credential_id: UUID, ok: bool) -> None:
    """Stamp a saved credential's last connection test (`POST /api/credentials/test`'s saved-row arm only: a test
    of unsaved form values names no row). Commits `session`."""
    _stamp(session, "test", update(Credential.__table__).where(Credential.__table__.c.id == credential_id).values(
        last_tested_at=func.now(), last_test_ok=ok,
    ))


def _stamp(session: Session, kind: str, statement: Executable) -> None:
    """Run one stamp in a SAVEPOINT and commit; log and swallow any failure (the outer work is already done).

    A failure's rollback expires every loaded instance, so a caller reads what it needs off its rows first. A row
    another writer holds locked fails the stamp after `_LOCK_WAIT` rather than stalling the fetch behind it."""
    try:
        with session.begin_nested():
            session.execute(_LOCK_WAIT)
            session.execute(statement)
        session.commit()
    except Exception as exc:  # noqa: BLE001 - best-effort by contract: the request it rides must not fail
        log.warning("credential %s stamp skipped (%s)", kind, type(exc).__name__)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - the connection itself is gone; the session's close handles it
            pass


def has_stored_secret(type_slug: str, config: Mapping[str, Any]) -> bool:
    """Whether any secret key of the row holds a value: what `_credential_out`'s `secrets_set` serves as
    non-empty. `secret_keys` on the config itself fails closed (an unclassified key is a secret), and a
    `SealedSecret` is never blank; `seal_config` drops a blank secret, so the rule reads the same before and
    after sealing."""
    return any(not is_blank(config.get(key)) for key in secret_keys(type_slug, config))


def expires_at(type_slug: str, config: Mapping[str, Any]) -> Optional[datetime]:
    """When the credential stops working, or None when it does not (or it cannot be told).

    A grant its forge reported revoked (`grant_dead_at`) is dead at that instant, or now when the mark is
    unreadable (`git_access` refuses on any mark). A non-blank refresh token renews the grant, so the cached
    access token's expiry is not the credential's. Otherwise the type's declared `expiry_field`, where an
    unreadable value is None: a guess would file the row under Expired. A key the row classifies as secret is
    never read, so no secret's value reaches the stored column."""
    secrets = secret_keys(type_slug, config)
    dead_at = config.get("grant_dead_at")
    if "grant_dead_at" not in secrets and not is_blank(dead_at):
        return _instant(dead_at) or datetime.now(timezone.utc)
    if not is_blank(config.get("refresh_token")):
        return None
    spec = CREDENTIAL_TYPES.get(type_slug)
    if spec is None or spec.expiry_field is None or spec.expiry_field in secrets:
        return None
    return _instant(config.get(spec.expiry_field))


def _instant(value: Any) -> Optional[datetime]:
    """An ISO-8601 string or epoch milliseconds (number or digit string) as an aware UTC instant; None for
    anything else. A naive ISO value is read as UTC (only a hand edit writes one)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit():
            value = int(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed)
