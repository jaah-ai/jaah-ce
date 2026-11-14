"""The `<base64url body>.<base64url mac>` signed-claims token: a JSON claims dict with an HMAC-SHA256 over its
encoded body. One home for the shape and the mint/open mechanics; the key, its domain separation and what the
claims mean stay with each caller (`console_direct` — the direct-console attribution token;
`trigger_preflight_script` — the preflight git-login grant). Keys must differ per caller, so a token minted by
one never opens under the other."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional

from app.b64url import b64url, unb64url


def _mac(key: bytes, body: str) -> str:
    return b64url(hmac.new(key, body.encode(), hashlib.sha256).digest())


def mint(key: bytes, claims: dict[str, Any]) -> str:
    """`claims` as `<body>.<mac>`: the body is the compact, key-sorted JSON in base64url, the MAC is over it."""
    body = b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_mac(key, body)}"


def open_claims(key: bytes, token: object) -> Optional[dict[str, Any]]:
    """The claims dict behind `token` when its MAC verifies under `key`, else None.

    Fail-closed and never raises: the input is whatever a node put in a frame."""
    if not isinstance(token, str) or "." not in token or not token.isascii():
        # `isascii` matters: `hmac.compare_digest` raises on non-ASCII `str`, and a caller runs in the
        # node bus read loop, where an escape unregisters the node. Signed tokens are always base64url.
        return None
    # The body is base64url, so it never contains "."; the last one separates the MAC.
    body, _, mac = token.rpartition(".")
    if not hmac.compare_digest(mac, _mac(key, body)):
        return None
    try:
        claims = json.loads(unb64url(body))
    except (ValueError, TypeError):
        return None
    return claims if isinstance(claims, dict) else None
