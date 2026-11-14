"""The emailed proof of an address: a 6-digit code and a link token, kept only as HMAC-SHA256 digests keyed by the
session secret, so a leaked table cannot be brute-forced offline over the 10⁶ codes.

Shared by the email sign-up (`app/email_signup.py`) and the typed-in alert addresses (`app/notification_emails.py`):
each owns its rows, budgets and refusals; this module owns how a secret is minted and how it is stored and compared.
Nothing here logs or stores a raw code or token.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from fastapi import HTTPException

from app.auth import resolve_session_secret

CODE_TTL = timedelta(minutes=30)
LINK_TTL = timedelta(hours=24)
MAX_CODE_ATTEMPTS = 5
EMAIL_MAX = 254

# One `@`, something on each side, a dot in the domain, no whitespace: the shape a mailbox can have. The
# mail itself is the real check.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")


def normalized_email(raw: str) -> str:
    """The address as stored (trimmed, lower-cased); 422 when it cannot be a mailbox."""
    email = (raw or "").strip().lower()
    if len(email) > EMAIL_MAX or not _EMAIL_RE.match(email) or not email.isprintable():
        raise HTTPException(status_code=422, detail={"code": "invalid_email"})
    return email


def digest(purpose: str, secret: str) -> str:
    """The stored form of `secret`. `purpose` separates the uses (and, for a code, binds it to its row), so one
    digest says nothing about another."""
    key = resolve_session_secret().encode()
    return hmac.new(key, f"{purpose}:{secret}".encode(), hashlib.sha256).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def matches(stored: str, purpose: str, candidate: str) -> bool:
    """Whether `candidate` (trimmed) is the secret `stored` is the digest of, in constant time."""
    return hmac.compare_digest(stored, digest(purpose, (candidate or "").strip()))
