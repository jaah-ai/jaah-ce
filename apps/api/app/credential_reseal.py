"""Re-seal every `credentials.config` row onto the CURRENT keys after a key rotation (`rotate_all`):

    python -m app.credential_reseal

KEY ROTATION: with the retired keys configured as previous keys (`CREDENTIAL_ENCRYPTION_PREVIOUS_KEYS`,
`CREDENTIAL_KMS_PREVIOUS_KEY_IDS`), `rotate_all` moves each row still on one of them onto the current keys
(`credential_crypto.reseal_to_current_keys`, verified by decrypting before the UPDATE), and the command
then reports how many rows remain on a retired key. A retired key may be removed — and a KMS key
scheduled for deletion — only once that count is ZERO.

Raw SQL over `credentials.config` is what a re-seal IS; this module is the allowed exception in
`tests/test_credential_crypto.py::_ALLOWED_TO_BYPASS`. It is therefore
also the home of the ONE other raw read of the column, `stored_secret_envelope_exists`: an equality
probe that compares a sealed value and never opens or returns it.
"""
from __future__ import annotations

import json
from typing import Callable, Iterator, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.credential_crypto import reseal_to_current_keys, stale_key_fields

# Rows per SELECT; each is UPDATEd individually so a re-run after a failure skips what landed.
_BATCH = 200


def _v2_rows(
    connection: sa.Connection, ids: Optional[Sequence[str]]
) -> Iterator[tuple[str, str, object]]:
    """`(id, type, stored)` for every row (or only `ids`), in keyset-paginated batches by primary key."""
    last_id = None
    wanted = [str(i) for i in ids] if ids is not None else None
    while True:
        rows = connection.execute(
            sa.text(
                "SELECT id, type, config FROM credentials "
                "WHERE (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) "
                "AND (CAST(:only AS uuid[]) IS NULL OR id = ANY(CAST(:only AS uuid[]))) "
                "ORDER BY id LIMIT :limit"
            ),
            {"after": last_id, "only": wanted, "limit": _BATCH},
        ).all()
        if not rows:
            return
        for row_id, type_slug, stored in rows:
            last_id = str(row_id)
            yield last_id, type_slug, json.loads(stored) if isinstance(stored, str) else stored


def rotate_all(
    connection: sa.Connection,
    *,
    commit: Optional[Callable[[], object]] = None,
    ids: Optional[Sequence[str]] = None,
) -> tuple[int, int]:
    """Move every row (or only `ids`) still on a retired key onto the current keys; `(rotated, current)`.

    Each new value is verified by decrypting under the current keys BEFORE its UPDATE
    (`reseal_to_current_keys`), so a row is either moved and readable or left byte-identical. A row no
    key in either ring opens FAILS the run by its id — skipping it would hide a row the application
    already cannot read. `commit`, when given, runs after each row (resumable).
    """
    rotated = 0
    current = 0
    for row_id, type_slug, stored in _v2_rows(connection, ids):
        try:
            resealed = reseal_to_current_keys(stored, credential_id=row_id, type_slug=type_slug)
        except ValueError as exc:
            # The row's id (never a value) beside the field the error already names.
            raise ValueError(f"credential {row_id}: {exc}") from None
        if resealed is None:
            current += 1
            continue
        # Compare-and-swap on the value read: a PATCH or token refresh landing since the SELECT is never
        # reverted to the old content — the run stops by row id and a re-run picks the row up.
        written = connection.execute(
            sa.text(
                "UPDATE credentials SET config = CAST(:new AS jsonb) "
                "WHERE id = CAST(:id AS uuid) AND config = CAST(:old AS jsonb)"
            ),
            {
                "new": json.dumps(resealed, separators=(",", ":")),
                "old": json.dumps(stored, separators=(",", ":")),
                "id": row_id,
            },
        )
        if written.rowcount != 1:
            raise RuntimeError(f"credential {row_id} changed during rotation; re-run credential_reseal")
        rotated += 1
        if commit is not None:
            commit()
    return rotated, current


def rows_on_previous_keys(connection: sa.Connection, *, ids: Optional[Sequence[str]] = None) -> list[str]:
    """The ids of rows (of all, or of `ids`) with any part still on a retired key — what must be empty
    before a retired key is removed. Reads key ids only; no private operation."""
    return [
        row_id
        for row_id, _type, stored in _v2_rows(connection, ids)
        if stale_key_fields(stored, credential_id=row_id)
    ]


def stored_secret_envelope_exists(session: Session, field: str, envelope: dict) -> bool:
    """Whether ANY row stores exactly `envelope` under tier-2 `field` — the Test route's replay check
    (`routes/credentials/guards.py::_refuse_replayed_stored_envelopes`).

    UNSCOPED BY DESIGN (`test_tenancy_bypass_sites.py`, `exact-value-probe`): only an exact stored envelope
    matches, so a hit reveals nothing the caller does not already hold. One parameterised existence query;
    the sealed value is compared in SQL, never read out.
    """
    return session.scalar(
        sa.text(
            "SELECT 1 FROM credentials "
            "WHERE config->'t2'->CAST(:field AS text) = CAST(:envelope AS jsonb) LIMIT 1"
        ),
        {"field": field, "envelope": json.dumps(envelope)},
    ) is not None


def main() -> None:
    from app.db import engine

    with engine.connect() as connection:
        rotated, current = rotate_all(connection, commit=connection.commit)
        print(f"credential_reseal: moved {rotated} row(s) onto the current keys; {current} already current", flush=True)
        remaining = rows_on_previous_keys(connection)
    if remaining:
        # Ids only — never a value.
        print(f"credential_reseal: {len(remaining)} row(s) still on a retired key: {', '.join(remaining)}", flush=True)
        raise SystemExit(1)
    print("credential_reseal: 0 rows on a retired key — previous keys may be retired", flush=True)


if __name__ == "__main__":
    main()
