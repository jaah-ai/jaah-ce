"""google_id_token.py — read a claim off a Google OAuth `id_token`, as an UNTRUSTED HINT.

A vendor CLI that signs in with Google (Antigravity, `provider_health._antigravity_identity_and_quota`) keeps
the `id_token` Google minted beside its refresh token. Its claims name the account (`email`) and the OAuth
client (`aud`); both are labels and routing hints, decoded and never verified.
"""
from __future__ import annotations

import base64
import json


def id_token_claim(id_token: object, name: str) -> str:
    """The string claim `name` of `id_token`'s payload, or "" — an UNTRUSTED HINT, decoded and never verified.

    Not verified and no network call: JWKS verification buys nothing for a hint read off a document the
    vendor CLI minted. Base64url padding is re-added (`b64decode` rejects it stripped); any malformation is
    "", never an exception on the success path."""
    try:
        payload = str(id_token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        value = claims.get(name)
        return value.strip() if isinstance(value, str) and value.strip() else ""
    except Exception:  # noqa: BLE001 — every malformation is the same non-answer
        return ""
