"""The console's own second factor (G-01, PRD D7): TOTP (RFC 6238) plus single-use recovery codes.

Stdlib only. The secret is sealed under the tier-1 key (`app/credential_crypto.py::seal`), never logged,
never in an event's `data`; recovery codes are stored as hashes and shown once. A TOTP time-step, once
accepted for a user, is never accepted again (`UserMfaFactor.last_used_step`, read under a row lock).
WebAuthn is deferred: TOTP satisfies D7's "TOTP or WebAuthn".

Two things stand in for the code. A TRUSTED DEVICE: after a proven factor the user may trust this
browser (`trust_device`), which skips the code there for the operator's `mfa_remember_days` knob; the
browser holds a random token in `DEVICE_COOKIE`, the server only its hash (`UserTrustedDevice`), so the user
lists and revokes each one. The cookie carries one token per person who trusted that browser: people
sharing a browser, or one person with two accounts, each keep their own trust instead of overwriting it, and a password change or an MFA reset forgets them all (a sign-out does not:
it bumps the epoch, which is why a device is not bound to it). An ORG SKIP: an operator may turn the second factor off for a whole tenant org
(`Org.mfa_skip`, `org_skips`) for recording videos and documentation; never for the operator plane. A USER SKIP: an
operator may exempt one membership, any user's (`AdminUser.mfa_exempt`); `skip_reason` reads both, and honours neither in a HIPAA org.

Between the first factor and the second the browser holds `PENDING_COOKIE` — signed, 10 minutes, naming
the user, the first factor and a nonce. Only the newest nonce is live (`UserMfaFactor.pending_nonce`, set
when the cookie is issued), and `MAX_BAD_CODES` wrong codes kill it and refuse every further code for
`FAILURE_WINDOW`, whatever new sign-ins happen meanwhile. The funnel (`app/login_funnel.py`) decides when a second factor
is due and mints the session once it is proven; the routes are `app/routes/mfa.py`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple, Optional
from urllib.parse import quote, urlencode
from uuid import UUID

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, credential_crypto, hipaa
# The TOTP clock, in epoch seconds, under this module's own name so a test can move time.
from app.clock import epoch as _now
from app.operator_settings import reads as settings_reads
from app.models.identity import AdminUser, UserMfaFactor, UserRecoveryCode, UserTrustedDevice
from app.models.orgs import Org
from app.tenancy.ids import is_tenant_org

PERIOD = 30  # seconds per time-step
DIGITS = 6
# Steps either side of now a code may come from: one, for a phone clock a few seconds off.
SKEW = 1
ISSUER = "Jaah"
RECOVERY_CODE_COUNT = 10
MAX_BAD_CODES = 5
# Bad codes count per USER, not per sign-in: a fresh password login does not reset them until this long
# after the last one, so a stolen password buys MAX_BAD_CODES guesses per window, not per login.
FAILURE_WINDOW = timedelta(minutes=15)

PENDING_COOKIE = "cockpit_mfa_pending"
PENDING_MAX_AGE = 600  # seconds
PENDING_PATH = "/api/auth/mfa"

DEVICE_COOKIE = "cockpit_trusted_device"
# Every sign-in door and the device list sit under it; no other route ever sees the token.
DEVICE_PATH = "/api/auth"
# The cookie holds several tokens joined by `_TOKEN_SEPARATOR` (never in `token_urlsafe`'s alphabet), newest
# first and at most `MAX_DEVICE_TOKENS` — one per person trusting this browser, so a second person's trust
# never evicts the first's. Only tokens whose row is still live are carried over.
_TOKEN_SEPARATOR = "."
MAX_DEVICE_TOKENS = 8
REMEMBER_DAYS_KNOB = "mfa_remember_days"

# Recovery codes: two groups of five from an alphabet without look-alikes; 31^10 ≈ 8e14 per code.
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_CODE_GROUP = 5

#: `(session, user, method, idp_claims) -> bool`: whether an IdP's own MFA is attested for this OAuth
#: sign-in (CH-06's SSO-only orgs plug in here). True mints a session with `amr` `sso`, no console factor.
SsoAttester = Callable[[Session, AdminUser, str, Optional[dict]], bool]
SSO_ATTESTERS: list[SsoAttester] = []


# ── TOTP ──────────────────────────────────────────────────────────────────────────────────────────


def generate_secret() -> str:
    """A fresh 160-bit secret, base32 without padding (what authenticator apps take)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret: str, step: int) -> str:
    """The RFC 6238 / RFC 4226 code for `step` (HMAC-SHA1, dynamic truncation)."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step() -> int:
    return int(_now()) // PERIOD


def matching_step(secret: str, code: str, *, after: Optional[int]) -> Optional[int]:
    """The step within `SKEW` of now whose code is `code` and which is newer than `after`, or None."""
    code = "".join(code.split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    now = current_step()
    for step in range(now - SKEW, now + SKEW + 1):
        if after is not None and step <= after:
            continue
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, email: str) -> str:
    label = quote(f"{ISSUER}:{email}")
    query = urlencode(
        {"secret": secret, "issuer": ISSUER, "algorithm": "SHA1", "digits": DIGITS, "period": PERIOD}
    )
    return f"otpauth://totp/{label}?{query}"


def _aad(user_id: UUID) -> bytes:
    return b"user-mfa-totp|" + str(user_id).encode()


def _seal_secret(user_id: UUID, secret: str) -> dict:
    return credential_crypto.seal({"secret": secret}, aad=_aad(user_id))


def _open_secret(factor: UserMfaFactor) -> str:
    return credential_crypto.unseal(factor.secret_sealed, aad=_aad(factor.key_user_id), label="user_mfa_factors")[
        "secret"
    ]


# ── Rows ──────────────────────────────────────────────────────────────────────────────────────────
# Every row here is the PERSON's (`identity_id`), not one membership's: a factor enrolled through
# one membership challenges and satisfies every other. The tables are platform-plane
# (`app/tenancy/manifest.py`), so no org scope filters them; each read names the caller's own identity,
# taken from the membership row the door or session already resolved. `user_id` records the membership
# that acted (NULL once deleted); `key_user_id` is the membership id the secret's AAD and a recovery code's
# hash were keyed on, set once, so both are read off the ROW.


def _factor(session: Session, user: AdminUser, *, lock: bool = False) -> Optional[UserMfaFactor]:
    stmt = select(UserMfaFactor).where(UserMfaFactor.identity_id == user.identity_id)
    if lock:
        # `populate_existing`: an earlier unlocked read left the row in the identity map, and the locked
        # read must not answer with those stale values (a confirm racing a start would re-key a live factor).
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.scalar(stmt)


def is_enrolled(session: Session, user: AdminUser) -> bool:
    factor = _factor(session, user)
    return factor is not None and factor.confirmed_at is not None


def enrolled_user_ids(session: Session, user_ids: list[UUID]) -> set[UUID]:
    """Which of the memberships `user_ids` belong to a person holding a confirmed factor (one query for a
    list). The memberships are read in the caller's bound scope, so only its own org's rows answer."""
    if not user_ids:
        return set()
    return set(
        session.scalars(
            select(AdminUser.id)
            .join(UserMfaFactor, UserMfaFactor.identity_id == AdminUser.identity_id)
            .where(AdminUser.id.in_(user_ids), UserMfaFactor.confirmed_at.is_not(None))
        )
    )


def _code_hash(user_id: UUID, code: str) -> str:
    return hashlib.sha256(f"{user_id}|{code}".encode()).hexdigest()


def _normalise_recovery(code: str) -> str:
    raw = "".join(ch for ch in code.lower() if ch.isalnum())
    return f"{raw[:_CODE_GROUP]}-{raw[_CODE_GROUP:]}" if len(raw) == 2 * _CODE_GROUP else ""


def _issue_recovery_codes(session: Session, user: AdminUser) -> list[str]:
    """Replace the person's recovery codes with a fresh set and return the plaintext, the only time it exists."""
    for row in session.scalars(select(UserRecoveryCode).where(UserRecoveryCode.identity_id == user.identity_id)):
        session.delete(row)
    session.flush()
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(2 * _CODE_GROUP))
        code = f"{raw[:_CODE_GROUP]}-{raw[_CODE_GROUP:]}"
        codes.append(code)
        session.add(UserRecoveryCode(
            user_id=user.id, identity_id=user.identity_id, key_user_id=user.id,
            code_hash=_code_hash(user.id, code),
        ))
    return codes


def start_enrolment(session: Session, user: AdminUser, nonce: str) -> Optional[str]:
    """A new unconfirmed secret for `user`, bound to the pending `nonce`, or None when the user already
    holds a confirmed factor (a password alone never replaces one) or `nonce` is not the live one."""
    factor = _factor(session, user, lock=True)
    secret = generate_secret()
    if factor is None:
        try:
            # A savepoint: two first starts race on `uq_user_mfa_factors_user_id`, and the loser is refused.
            with session.begin_nested():
                session.add(
                    UserMfaFactor(
                        user_id=user.id, identity_id=user.identity_id, key_user_id=user.id,
                        secret_sealed=_seal_secret(user.id, secret), pending_nonce=nonce, pending_failures=0,
                    )
                )
        except IntegrityError:
            return None
        return secret
    if factor.confirmed_at is not None or not _nonce_live(factor, nonce):
        return None
    # Sealed for the row's own `key_user_id`, which `_open_secret` reads: the enrolment may resume through
    # another membership of the same person.
    factor.secret_sealed = _seal_secret(factor.key_user_id, secret)
    factor.last_used_step = None
    return secret


def _throttled(factor: UserMfaFactor) -> bool:
    """Out of tries: `MAX_BAD_CODES` failures, the last within `FAILURE_WINDOW`."""
    last = factor.last_failure_at
    return (
        factor.pending_failures >= MAX_BAD_CODES
        and last is not None
        and datetime.now(timezone.utc) - last < FAILURE_WINDOW
    )


def _nonce_live(factor: UserMfaFactor, nonce: str) -> bool:
    return (
        factor.pending_nonce is not None
        and hmac.compare_digest(factor.pending_nonce, nonce)
        and factor.pending_failures < MAX_BAD_CODES
    )


class Check(NamedTuple):
    """A second-factor attempt's answer. `ok`: proven (`amr` is what it adds). Else `expired` says the
    pending nonce is dead (absent, replaced, or out of tries) rather than merely wrong, and `throttled`
    that it is out of tries (`FAILURE_WINDOW`)."""

    ok: bool
    expired: bool = False
    amr: Optional[str] = None
    throttled: bool = False


def _refused_unchecked(factor: Optional[UserMfaFactor]) -> Check:
    return Check(False, expired=True, throttled=factor is not None and _throttled(factor))


def _failed(factor: UserMfaFactor) -> Check:
    factor.pending_failures += 1
    factor.last_failure_at = datetime.now(timezone.utc)
    if factor.pending_failures >= MAX_BAD_CODES:
        factor.pending_nonce = None
        return Check(False, expired=True, throttled=True)
    return Check(False)


def _succeeded(factor: UserMfaFactor) -> None:
    factor.pending_nonce = None
    factor.pending_failures = 0
    factor.last_failure_at = None


def confirm_enrolment(session: Session, user: AdminUser, nonce: str, code: str) -> tuple[Check, list[str]]:
    """Confirm an enrolment in progress with a first code. On success the factor is live, the code's step
    is spent, and a fresh set of recovery codes is returned (plaintext, once)."""
    factor = _factor(session, user, lock=True)
    if factor is None or factor.confirmed_at is not None or not _nonce_live(factor, nonce):
        return _refused_unchecked(factor), []
    step = matching_step(_open_secret(factor), code, after=factor.last_used_step)
    if step is None:
        return _failed(factor), []
    factor.confirmed_at = datetime.now(timezone.utc)
    factor.last_used_step = step
    _succeeded(factor)
    return Check(True, amr="otp"), _issue_recovery_codes(session, user)


def verify(
    session: Session, user: AdminUser, nonce: str, *, code: Optional[str] = None, recovery_code: Optional[str] = None
) -> Check:
    """Check a TOTP code or a recovery code against the user's confirmed factor and the live nonce. A
    TOTP step at or before the last accepted one is refused (replay); a recovery code is spent on use."""
    factor = _factor(session, user, lock=True)
    if factor is None or factor.confirmed_at is None or not _nonce_live(factor, nonce):
        return _refused_unchecked(factor)
    if code is not None:
        step = matching_step(_open_secret(factor), code, after=factor.last_used_step)
        if step is None:
            return _failed(factor)
        factor.last_used_step = step
        amr = "otp"
    else:
        row = _unspent_recovery_code(session, user, _normalise_recovery(recovery_code or ""))
        if row is None:
            return _failed(factor)
        row.used_at = datetime.now(timezone.utc)
        amr = "recovery"
    _succeeded(factor)
    return Check(True, amr=amr)


def _unspent_recovery_code(session: Session, user: AdminUser, normalised: str) -> Optional[UserRecoveryCode]:
    """The person's unspent code `normalised` names, locked, or None. Each code is hashed with the
    membership that was issued it (`UserRecoveryCode.key_user_id`), so the at most `RECOVERY_CODE_COUNT` live
    rows are compared one by one rather than looked up by one membership's hash."""
    if not normalised:
        return None
    rows = session.scalars(
        select(UserRecoveryCode)
        .where(UserRecoveryCode.identity_id == user.identity_id, UserRecoveryCode.used_at.is_(None))
        .order_by(UserRecoveryCode.id)
        .with_for_update()
    )
    return next(
        (row for row in rows if hmac.compare_digest(row.code_hash, _code_hash(row.key_user_id, normalised))), None
    )


def _step_up_throttled(factor: UserMfaFactor) -> bool:
    """`_throttled` for the step-up's own count."""
    last = factor.step_up_last_failure_at
    return (
        factor.step_up_failures >= MAX_BAD_CODES
        and last is not None
        and datetime.now(timezone.utc) - last < FAILURE_WINDOW
    )


def _step_up_failed(factor: UserMfaFactor) -> Check:
    factor.step_up_failures += 1
    factor.step_up_last_failure_at = datetime.now(timezone.utc)
    return Check(False, throttled=factor.step_up_failures >= MAX_BAD_CODES)


def _step_up_succeeded(factor: UserMfaFactor) -> None:
    factor.step_up_failures = 0
    factor.step_up_last_failure_at = None


def _sign_in_failed(factor: UserMfaFactor) -> Check:
    """`_failed` without its `expired`: no pending nonce was checked."""
    return Check(False, throttled=_failed(factor).throttled)


def _sign_in_succeeded(factor: UserMfaFactor) -> None:
    """The sign-in count cleared; the pending nonce is another sign-in's and stays."""
    factor.pending_failures = 0
    factor.last_failure_at = None


def verify_current_code(
    session: Session, user: AdminUser, code: str, *, allow_recovery: bool = False, sign_in: bool = False,
) -> Check:
    """A fresh TOTP code from the person's confirmed factor, with the replay rule and no pending nonce.

    `sign_in` names which count it spends. True for a code that signs in or proves a factor into a membership (the
    password door's inline code, the org switch): the sign-in's own count (`pending_failures`), so one per-user budget
    covers every door. False for a signed-in user's step-up (Delete account, Compliance Delete, new recovery codes):
    its own count (`step_up_failures`, the same `MAX_BAD_CODES` per `FAILURE_WINDOW`), so wrong codes there never
    throttle sign-in.

    `allow_recovery` also accepts one of the person's recovery codes (spent on use, `amr` `recovery`) for any
    `code` that is not a TOTP's `DIGITS` digits — the org switch's step-up (`login_funnel.switch_membership`).
    The password door's step-up keeps TOTP only."""
    factor = _factor(session, user, lock=True)
    throttled, failed, succeeded = (
        (_throttled, _sign_in_failed, _sign_in_succeeded) if sign_in
        else (_step_up_throttled, _step_up_failed, _step_up_succeeded)
    )
    if factor is None or factor.confirmed_at is None or throttled(factor):
        return Check(False, throttled=factor is not None and throttled(factor))
    typed = (code or "").strip()
    if allow_recovery and not (typed.isdigit() and len(typed) == DIGITS):
        row = _unspent_recovery_code(session, user, _normalise_recovery(typed))
        if row is None:
            return failed(factor)
        row.used_at = datetime.now(timezone.utc)
        succeeded(factor)
        return Check(True, amr="recovery")
    step = matching_step(_open_secret(factor), code, after=factor.last_used_step)
    if step is None:
        return failed(factor)
    factor.last_used_step = step
    succeeded(factor)
    return Check(True, amr="otp")


def bind_pending_nonce(session: Session, user: AdminUser, nonce: str) -> None:
    """Make `nonce` the user's one live pending sign-in (a factor row exists only once enrolment began)."""
    factor = _factor(session, user, lock=True)
    if factor is not None:
        factor.pending_nonce = nonce
        # The count survives a new sign-in until its window has passed (`FAILURE_WINDOW`).
        last = factor.last_failure_at
        if last is None or datetime.now(timezone.utc) - last >= FAILURE_WINDOW:
            factor.pending_failures = 0
            factor.last_failure_at = None


def regenerate_recovery_codes(session: Session, user: AdminUser) -> list[str]:
    """A fresh recovery-code set for an enrolled user (the old set dies). The caller bumps the epoch."""
    return _issue_recovery_codes(session, user)


def reset(session: Session, user: AdminUser) -> bool:
    """Remove the person's factor, recovery codes and trusted devices; they enrol again at next sign-in,
    through whichever membership. Whether a factor was removed. The caller records `mfa_reset` and bumps
    the epoch."""
    removed = False
    for row in session.scalars(select(UserRecoveryCode).where(UserRecoveryCode.identity_id == user.identity_id)):
        session.delete(row)
    for row in session.scalars(select(UserTrustedDevice).where(UserTrustedDevice.identity_id == user.identity_id)):
        session.delete(row)
    factor = _factor(session, user)
    if factor is not None:
        removed = factor.confirmed_at is not None
        session.delete(factor)
    return removed


# ── Trusted devices and the org skip ─────────────────────────────────────────────────────────────


def org_skips(session: Session, user: AdminUser) -> bool:
    """Whether `user`'s org has the second factor turned off (`Org.mfa_skip`). Never for a superadmin or an
    operator-plane org, whatever the column says."""
    if user.is_superadmin or user.org_id is None or not is_tenant_org(user.org_id):
        return False
    org = session.get(Org, user.org_id)
    return org is not None and bool(org.mfa_skip)


#: `skip_reason`'s answers: the user's own exemption, or their org's.
SKIP_USER = "user"
SKIP_ORG = "org"


def skip_reason(session: Session, user: AdminUser) -> Optional[str]:
    """Why `user` signs in with no second factor, or None when one is due: an operator's exemption of this
    membership (`AdminUser.mfa_exempt`, any user, a superadmin included) first, then the org's (`org_skips`).
    Neither applies to a membership of a HIPAA org (`hipaa.org_is_on`, C6): its sign-in always needs the factor."""
    if hipaa.org_is_on(session, user.org_id):
        return None
    if user.mfa_exempt:
        return SKIP_USER
    return SKIP_ORG if org_skips(session, user) else None


def remember_days() -> int:
    """How long a trusted device skips the code, the operator's knob."""
    return settings_reads.get_int(REMEMBER_DAYS_KNOB)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _device_live(device: UserTrustedDevice, now: datetime) -> bool:
    return device.expires_at > now


_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_SYSTEMS = (
    ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("CrOS", "ChromeOS"),
    ("Windows", "Windows"), ("Mac OS X", "macOS"), ("Linux", "Linux"),
)


def device_label(user_agent: Optional[str]) -> str:
    """A short name for a browser, from its User-Agent: "Chrome on macOS". Order matters: Edge and Opera
    also say Chrome, Chrome also says Safari, Android also says Linux."""
    ua = user_agent or ""
    browser = next((name for token, name in _BROWSERS if token in ua), "Browser")
    system = next((name for token, name in _SYSTEMS if token in ua), None)
    return f"{browser} on {system}" if system else browser


def _cookie_tokens(cookie: Optional[str]) -> list[str]:
    """The tokens `DEVICE_COOKIE` carries, newest first; a cookie set before per-person trust is one bare token."""
    return [t for t in (cookie or "").split(_TOKEN_SEPARATOR) if t][:MAX_DEVICE_TOKENS]


def _trusted_device(session: Session, user: AdminUser, cookie: Optional[str]) -> Optional[UserTrustedDevice]:
    """The live trusted device one of `cookie`'s tokens names for `user`, or None. Only an enrolled user's
    device counts: a reset factor takes its devices with it, whatever the row says."""
    tokens = _cookie_tokens(cookie)
    if not tokens or not is_enrolled(session, user):
        return None
    now = datetime.now(timezone.utc)
    devices = session.scalars(
        select(UserTrustedDevice).where(
            UserTrustedDevice.identity_id == user.identity_id,
            UserTrustedDevice.token_hash.in_([_token_hash(t) for t in tokens]),
        )
    )
    return next((d for d in devices if _device_live(d, now)), None)


def device_id_for(session: Session, user: AdminUser, cookie: Optional[str]) -> Optional[int]:
    """The id of the live trusted device `cookie` names for `user`, or None (the list's `current`)."""
    device = _trusted_device(session, user, cookie)
    return device.id if device is not None else None


def use_trusted_device(session: Session, user: AdminUser, cookie: Optional[str]) -> Optional[UserTrustedDevice]:
    """The live trusted device `cookie` names for `user`, its `last_used_at` stamped; None when there is none."""
    device = _trusted_device(session, user, cookie)
    if device is not None:
        device.last_used_at = datetime.now(timezone.utc)
    return device


def trust_device(
    session: Session, response, user: AdminUser, user_agent: Optional[str], days: int, cookie: Optional[str]
) -> UserTrustedDevice:
    """Trust this browser for `days` (the caller reads `remember_days()` once): a new row, and the cookie
    carrying its token ahead of `cookie`'s (the request's `DEVICE_COOKIE`) still-live tokens of OTHER people,
    so their trust in this browser survives. The user's expired devices are removed while we are here."""
    now = datetime.now(timezone.utc)
    kept_hashes = {_token_hash(t): t for t in _cookie_tokens(cookie)}
    kept: list[tuple[str, datetime]] = []
    if kept_hashes:
        for row in session.scalars(
            select(UserTrustedDevice).where(UserTrustedDevice.token_hash.in_(list(kept_hashes)))
        ):
            # The same person's older token on this browser is superseded by the new one, as it always was.
            if row.identity_id != user.identity_id and _device_live(row, now):
                kept.append((kept_hashes[row.token_hash], row.expires_at))
    for old in session.scalars(select(UserTrustedDevice).where(UserTrustedDevice.identity_id == user.identity_id)):
        if not _device_live(old, now):
            session.delete(old)
    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(days=days)
    device = UserTrustedDevice(
        user_id=user.id, identity_id=user.identity_id, token_hash=_token_hash(token), label=device_label(user_agent),
        expires_at=expires_at,
    )
    session.add(device)
    # Newest first, in the cookie's own order; the cookie lives as long as its longest-lived token.
    order = _cookie_tokens(cookie)
    kept.sort(key=lambda pair: order.index(pair[0]))
    kept = kept[: MAX_DEVICE_TOKENS - 1]
    last = max([expires_at, *(exp for _, exp in kept)])
    response.set_cookie(
        DEVICE_COOKIE, _TOKEN_SEPARATOR.join([token, *(t for t, _ in kept)]),
        max_age=max(1, int((last - now).total_seconds())), httponly=True, samesite="lax",
        secure=auth.session_cookie_is_secure(), path=DEVICE_PATH,
    )
    return device


def trusted_devices(session: Session, user: AdminUser) -> list[UserTrustedDevice]:
    """The person's live trusted devices, newest first."""
    now = datetime.now(timezone.utc)
    rows = session.scalars(
        select(UserTrustedDevice)
        .where(UserTrustedDevice.identity_id == user.identity_id)
        .order_by(UserTrustedDevice.created_at.desc(), UserTrustedDevice.id.desc())
    ).all()
    return [row for row in rows if _device_live(row, now)]


def revoke_trusted_devices(session: Session, user: AdminUser, device_id: Optional[int] = None) -> list[str]:
    """Forget one of the person's devices (`device_id`), or all of them (None). The labels of the live ones
    removed, for the audit row; an unknown or someone else's id removes nothing."""
    now = datetime.now(timezone.utc)
    stmt = select(UserTrustedDevice).where(UserTrustedDevice.identity_id == user.identity_id)
    if device_id is not None:
        stmt = stmt.where(UserTrustedDevice.id == device_id)
    removed = []
    for row in session.scalars(stmt):
        if _device_live(row, now):
            removed.append(row.label)
        session.delete(row)
    return removed


# ── The pending-second-factor cookie ─────────────────────────────────────────────────────────────


class Pending(NamedTuple):
    admin_id: UUID
    method: str
    amr_first: str
    nonce: str
    epoch: int
    # The identity epoch at the first factor (`auth.session_row_is_live`); 0 on a cookie issued before it.
    iepoch: int = 0


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(auth.resolve_session_secret(), salt="cockpit-mfa")


def set_pending_cookie(response, user: AdminUser, method: str, amr_first: str) -> str:
    """Issue the pending cookie for `user` and return its nonce (the caller binds it)."""
    nonce = secrets.token_urlsafe(16)
    token = _serializer().dumps({
        "admin_id": str(user.id), "method": method, "amr_first": amr_first, "nonce": nonce,
        "epoch": user.session_epoch or 0,
        "iepoch": auth.identity_epoch(user),
    })
    response.set_cookie(
        PENDING_COOKIE, token, max_age=PENDING_MAX_AGE, httponly=True, samesite="lax",
        secure=auth.session_cookie_is_secure(), path=PENDING_PATH,
    )
    return nonce


def clear_pending_cookie(response) -> None:
    response.delete_cookie(PENDING_COOKIE, path=PENDING_PATH)


def read_pending(token: Optional[str]) -> Optional[Pending]:
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=PENDING_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    try:
        return Pending(
            admin_id=UUID(payload["admin_id"]), method=str(payload["method"]),
            amr_first=str(payload["amr_first"]), nonce=str(payload["nonce"]), epoch=int(payload["epoch"]),
            iepoch=int(payload.get("iepoch", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None
