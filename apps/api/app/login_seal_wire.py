"""The sealed-login wire shapes, as this control plane checks them — the twin of
`apps/jaahd/login_seal.py`'s `is_wire_key`/`is_wire_sealed` and bounds (the packages cannot
cross-import; change one, change both).

The CP relays a per-login public key (`peer_key`, `seal_key`) and RSA-OAEP ciphertext (`sealed_url`,
`sealed_code`) it cannot read, so shape and bound are the only checks it can make: base64 that decodes,
a key under `SEAL_KEY_MAX_CHARS`, a sealed value under `SEALED_MAX_CHARS` and whole 256-byte chunks.
Whether a key is usable RSA is the node's `openssl` verdict (`login_seal_invalid`). One home for both
readers: `routes/commands.py` (the request) and `durable/accounts.py` (the node's reply).
"""
from __future__ import annotations

import base64
import binascii
import re

# A 2048-bit SPKI is 294 DER bytes; the bound leaves room for a longer modulus without admitting a blob.
SEAL_KEY_MAX_CHARS = 1024
# 11 RSA-2048 chunks hold the CP's 2048-character URL bound with room to spare; a code is at most 3.
SEALED_MAX_CHARS = 4096
# One RSA-2048 OAEP ciphertext block.
CHUNK_CIPHERTEXT_BYTES = 256

_B64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")


def _decoded(value: object, max_chars: int) -> bytes | None:
    """`value` decoded, or None unless it is a non-empty base64 string of at most `max_chars` that
    strict-decodes (so bad padding is refused here, not late on the node)."""
    if not isinstance(value, str) or not 0 < len(value) <= max_chars or not _B64_RE.match(value):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def is_wire_key(value: object) -> bool:
    """Whether `value` is a public key this CP will relay: base64 that decodes, bounded."""
    raw = _decoded(value, SEAL_KEY_MAX_CHARS)
    return raw is not None and len(raw) > 0


def is_wire_sealed(value: object) -> bool:
    """Whether `value` is a sealed blob this CP will relay: base64 that decodes, bounded, whole
    256-byte RSA chunks."""
    raw = _decoded(value, SEALED_MAX_CHARS)
    return raw is not None and len(raw) > 0 and len(raw) % CHUNK_CIPHERTEXT_BYTES == 0
