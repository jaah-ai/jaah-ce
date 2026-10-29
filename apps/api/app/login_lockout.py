"""Login lockout (G-14, PRD 5.1 / D9 with the settled deviation): bound password guessing without letting a
stranger lock the owner out.

Two rules over the counted failures in `login_failures` (`models.LoginFailure`), shared by every
control-plane task:

- **Hard lock per (identifier, source IP).** `LOCK_AFTER` failures within `WINDOW` lock that pair for
  `LOCK_FOR` after the last of them. Keyed on the IP too, so an attacker locks only their own source: the
  owner signing in from anywhere else is untouched.
- **Step-up per identifier.** `STEP_UP_AFTER` failures within `WINDOW` from any sources: the password door
  for that identifier refuses unless a valid TOTP code is presented first (`routes/auth.py::login`), so
  distributed guessing stays bounded while the owner, who holds the authenticator, still signs in. Other
  doors are untouched by it.

A refusal the lockout itself answers (`LOCKED`, `STEP_UP_REQUIRED`) is recorded as a `login_failed` but
never counted, so a lock cannot renew itself and lasts exactly `LOCK_FOR`. Everything else
`login_funnel.record_login_failure` records is counted. The identifier is stored only as `identifier_hash`
(an HMAC under the session secret), the same value the `app.security` log line carries.

Nothing here imports the funnel: the funnel calls `record` and registers the lock as its first policy hook.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import resolve_session_secret
from app.login_oauth import ORG_PENDING_REVIEW
from app.models.identity import LoginFailure
from app.clock import utcnow as _now

WINDOW = timedelta(minutes=15)
LOCK_AFTER = 10
LOCK_FOR = timedelta(minutes=15)
STEP_UP_AFTER = 30

LOCKED = "locked"
STEP_UP_REQUIRED = "step_up_required"
# Not failures to count: the lockout's own answers, and an applicant whose org is still under review — they
# proved their identity, and counting it would lock them out of their first sign-in once approved.
_UNCOUNTED = frozenset({LOCKED, STEP_UP_REQUIRED, ORG_PENDING_REVIEW})

State = Optional[Literal["locked", "step_up_required"]]


# The longest identifier kept or hashed: an unknown one is whatever a stranger typed.
IDENTIFIER_MAX = 320


def normalized(identifier: str) -> str:
    """The one comparable form of an identifier, for the lockout's key and the `login_failed` row alike."""
    return identifier.strip().lower()[:IDENTIFIER_MAX]


def identifier_hash(identifier: str) -> str:
    """The keyed hash of the normalised identifier: correlatable across rows and log lines, never reversible
    by hashing a list of candidate emails without the key."""
    key = b"login-identifier:" + resolve_session_secret().encode()
    return hmac.new(key, normalized(identifier).encode(), hashlib.sha256).hexdigest()[:32]


def _locked(session: Session, hashed: str, source_ip: Optional[str], now: datetime) -> bool:
    """Whether any run of `LOCK_AFTER` failures from this source fit in `WINDOW` and its last one is under
    `LOCK_FOR` old. Every run is tested, not only the newest, so a later failure can never lift a lock."""
    if source_ip is None:
        return False
    times = list(
        session.scalars(
            select(LoginFailure.failed_at)
            .where(
                LoginFailure.identifier_hash == hashed,
                LoginFailure.source_ip == source_ip,
                LoginFailure.failed_at > now - WINDOW - LOCK_FOR,
            )
            .order_by(LoginFailure.failed_at)
        )
    )
    return any(
        times[i] - times[i - LOCK_AFTER + 1] <= WINDOW and now < times[i] + LOCK_FOR
        for i in range(LOCK_AFTER - 1, len(times))
    )


def _stepped_up(session: Session, hashed: str, now: datetime) -> bool:
    count = session.scalar(
        select(func.count())
        .select_from(LoginFailure)
        .where(LoginFailure.identifier_hash == hashed, LoginFailure.failed_at > now - WINDOW)
    )
    return (count or 0) >= STEP_UP_AFTER


def state(session: Session, identifier: Optional[str], source_ip: Optional[str]) -> State:
    """`LOCKED` when this identifier is locked from this source, else `STEP_UP_REQUIRED` when its password
    door demands a TOTP, else None. The same answer whether or not the identifier has an account.

    Read before the attempt and counted after it, unserialised: a concurrent burst can overshoot a threshold
    by its width, which the per-IP rate limit bounds. Serialising per identifier (an advisory lock held
    across bcrypt) was rejected — a flood on one email would park a pooled connection per request."""
    if not identifier:
        return None
    hashed, now = identifier_hash(identifier), _now()
    if _locked(session, hashed, source_ip, now):
        return LOCKED
    return STEP_UP_REQUIRED if _stepped_up(session, hashed, now) else None


def record(session: Session, identifier: Optional[str], source_ip: Optional[str], reason: str) -> list[str]:
    """Stage a counted failure (uncommitted: the caller commits it with its activity row). Returns the rules
    this failure ENGAGED — the lock and/or the step-up it just crossed into (one failure can cross both) — so
    the caller records each engagement once."""
    if not identifier or reason in _UNCOUNTED:
        return []
    hashed, now = identifier_hash(identifier), _now()
    was_locked, was_stepped_up = _locked(session, hashed, source_ip, now), _stepped_up(session, hashed, now)
    session.add(LoginFailure(identifier_hash=hashed, source_ip=source_ip, failed_at=now))
    session.flush()
    engaged = []
    if not was_locked and _locked(session, hashed, source_ip, now):
        engaged.append(LOCKED)
    if not was_stepped_up and _stepped_up(session, hashed, now):
        engaged.append(STEP_UP_REQUIRED)
    return engaged
