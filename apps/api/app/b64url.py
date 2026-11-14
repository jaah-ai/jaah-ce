"""Unpadded url-safe base64 (RFC 4648 §5, `-`/`_`, no `=`), the wire spelling of every signed token the API
mints (`signed_claims`). A stdlib-only leaf so a token's alphabet never couples one module to another's; do not
re-add a local copy."""

import base64


def b64url(raw: bytes) -> str:
    """`raw` as url-safe base64 with the `=` padding stripped."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unb64url(value: str) -> bytes:
    """The bytes behind `b64url`'s text: re-padded to a multiple of four, then decoded."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
