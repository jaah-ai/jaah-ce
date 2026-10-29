"""Give a fixture login's org a working NodeVault — `./dc fixture-nodevault <email>` (e2e, local dev).

The e2e suite seals credentials under the org's current NodeVault key, and setting one up through the
console needs a passphrase backup and a node to hold the private half. This sets the org's
`nodevault_allowed_override` and, when the org has no `current` key, inserts one: an RSA-4096 public key
whose private half is discarded at once (never written, never printed) — the suite only SEALS, nothing
opens. Idempotent: a second run reuses the current key. Refused unless the process is a throwaway
environment (`app/dev_bypass.py::is_fixture_env`), as `app/fixture_session.py`. Prints
`NODEVAULT_KEY_HASH <hash>` on stdout and nothing else.
"""
from __future__ import annotations

import base64
import sys
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app import identities
from app.credential_keys import nodevault_key_hash
from app.db import SessionLocal
from app.dev_bypass import is_fixture_env
from app.models.credentials import NV_KEY_CURRENT, NodeVaultKey
from app.models.orgs import Org
from app.tenancy import seam


def _public_spki() -> bytes:
    """A fresh RSA-4096 (e = 65537) public key as SPKI DER. The private key never leaves this frame."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    return private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m app.fixture_nodevault <email>", file=sys.stderr)
        return 2
    if not is_fixture_env():
        print("fixture_nodevault: refused — APP_ENV is not local, ci or test", file=sys.stderr)
        return 2
    with seam.identity_scope(), SessionLocal() as session:
        user = identities.first_membership(session, args[0])
        org_id = user.org_id if user is not None else None
    if org_id is None:
        print(f"fixture_nodevault: no user {args[0]!r}", file=sys.stderr)
        return 1
    with seam.bound_to(org_id), SessionLocal() as session:
        session.get(Org, org_id).nodevault_allowed_override = True
        key_hash = session.scalar(select(NodeVaultKey.key_hash).where(
            NodeVaultKey.org_id == org_id, NodeVaultKey.state == NV_KEY_CURRENT
        ))
        if key_hash is None:
            spki = _public_spki()
            key_hash = nodevault_key_hash(spki)
            session.add(NodeVaultKey(
                name=key_hash[:12], org_id=org_id, key_hash=key_hash,
                public_key=base64.b64encode(spki).decode("ascii"), state=NV_KEY_CURRENT, source="generate",
                backup_confirmed_at=datetime.now(timezone.utc),
            ))
        session.commit()
    print(f"NODEVAULT_KEY_HASH {key_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
