"""Jaah API keys: every rule. A key is a personal bearer credential of its owner (an Org Owner or Org
Admin), sent as `Authorization: Bearer jaah_…`.

THE SECRET. `jaah_` + 32 base62 random characters + 6 base62 characters of the CRC32 of the random part (43 in all),
so a typo or a stray string is refused before any database read (`parse`). Only its SHA-256 is stored
(`api_key_secrets.token_hash`, the `scim.py` precedent: a 190-bit random secret needs no slow hash) with its last four
characters, shown as `jaah_…<last4>` (`display`). The secret is returned once, by create and rotate, and never
logged, audited or read back.

THE REQUEST. `auth.SessionAuthMiddleware` calls `verify` for a cookie-less request bearing a key (a cookie always
wins). One read joins secret → key → owner; the key is refused (401) when the hash is unknown, the secret is past its
rotation overlap, the key is revoked or expired, or its owner no longer holds it (`owner_refusal`: disabled, or a member
not an Org Owner/Admin of the key's tenant org right now — a demoted owner's keys answer 401 until re-promotion; an
operator's keys live in their own org or in a tenant org they made them in while acting as it). Outside the key's own IP
allowlist → 403 `ip_not_allowed`; the org's allowlist (`app/ip_allowlist.py`) holds too. A passing key becomes
`request.state.api_key` (`ApiKeyContext`) and the request runs as its owner, narrowed by `narrow`: the key's ticked
keys ∩ the owner's own, no Org Admin rank unless the key's `org_admin` toggle is on. An operator's key made while acting
runs as that org's Owner seat (`acting`, `authz.owner_seat`), so it does what the operator's tab acting as the org does;
one in their own org keeps the operator's rights only with every permission and Org administration ticked
(`superadmin`), else it holds just its ticked keys. A key never reaches a `@session_only` route
(`authz/declarations.py`), a stream or a WebSocket.

LIFECYCLE. `routes/api_keys.py` creates, edits, rotates and revokes. Disabling the owner revokes their keys
(`revoke_for_owner`); deleting them deletes the keys after auditing each (`forget_owner`).

THE SWEEP. The hourly `api-keys-hourly` cron (`app/cron_system_activities.py`) runs `sweep`: it reminds each live key's
owner 7 days and 1 day before expiry through the one choke point (`user_notify.deliver`, `important=True`: in-app and
email always, every other channel the owner configured), records one `api_key_expired` row per key that ran out, and
revokes (`owner_disabled`) the keys of an owner disabled by any path the users route did not take. The bell's
`api_key_expiring` item is derived from the reminder stamps (`routes/inbox.py`)."""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
import string
import threading
import time
import zlib
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection, Request

from app import access, edition_features
from app.authz.catalog import PERMISSION_KEYS, UNHONOURED_PERMISSION_KEYS
from app.clock import utcnow
from app.models.api_keys import ApiKey, ApiKeySecret
from app.models.identity import AdminUser
from app.tenancy import seam
from app.tenancy.ids import is_operator_plane, is_tenant_org

if TYPE_CHECKING:
    from app.authz.principal import Principal
    from app.ip_allowlist import Network

# `app.ip_allowlist` is imported where used: it imports `app.login_funnel`, which imports `app.auth`, which imports this.

log = logging.getLogger(__name__)

PREFIX = "jaah_"
_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
_RANDOM_CHARS = 32
_CHECK_CHARS = 6
SECRET_LENGTH = len(PREFIX) + _RANDOM_CHARS + _CHECK_CHARS

#: The keys a `grants_all` key holds: every catalog key something honours, read at request time so the set
#: follows the catalog (D-A1).
GRANTS_ALL_KEYS: frozenset[str] = PERMISSION_KEYS - UNHONOURED_PERMISSION_KEYS

#: How rarely a key's last use is written, and how rarely one refusal of one key is audited.
WRITE_THROTTLE = timedelta(seconds=60)

#: `revoke_reason` values; `owner_removed` names only the audit row of a deleted owner's key (the row is gone).
REVOKED = "revoked"
OWNER_DISABLED = "owner_disabled"
OWNER_REMOVED = "owner_removed"

#: The rotation overlaps a caller may pick: how long the previous secret keeps working.
OVERLAPS: dict[str, Optional[timedelta]] = {
    "none": None, "1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7),
}

#: A key ending within this is `expiring`.
EXPIRING_WITHIN = timedelta(days=7)

#: The 401 a refused key answers, and the 403 code of a `@session_only` route.
INVALID_DETAIL = "invalid_api_key"
NOT_ALLOWED_CODE = "api_key_not_allowed"
NOT_ALLOWED_DETAIL = "An API key cannot do this. Sign in to the console."


# ── The secret ────────────────────────────────────────────────────────────────────────────────────────


def _base62(value: int, width: int) -> str:
    out = []
    for _ in range(width):
        value, digit = divmod(value, len(_ALPHABET))
        out.append(_ALPHABET[digit])
    return "".join(reversed(out))


def _checksum(random_part: str) -> str:
    return _base62(zlib.crc32(random_part.encode("ascii")), _CHECK_CHARS)


def mint_secret() -> str:
    random_part = "".join(secrets.choice(_ALPHABET) for _ in range(_RANDOM_CHARS))
    return f"{PREFIX}{random_part}{_checksum(random_part)}"


def parse(token: str) -> bool:
    """Whether `token` is shaped like a key and its checksum holds — decided before any database read."""
    if len(token) != SECRET_LENGTH or not token.startswith(PREFIX):
        return False
    body = token[len(PREFIX):]
    if any(ch not in _ALPHABET for ch in body):
        return False
    return secrets.compare_digest(_checksum(body[:_RANDOM_CHARS]), body[_RANDOM_CHARS:])


def hash_secret(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def display(last4: str) -> str:
    """The non-secret identifier a list shows."""
    return f"{PREFIX}…{last4}"


def bearer(request: Request) -> Optional[str]:
    """The key a request presents in `Authorization: Bearer jaah_…`, or None when it presents none (another scheme or
    another bearer token is not a key, and stays the route's business)."""
    return bearer_value(request.headers.get("authorization", ""))


def bearer_value(authorization: str) -> Optional[str]:
    """`bearer`'s rule over a bare `Authorization` header value."""
    scheme, _, value = (authorization or "").partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value.startswith(PREFIX):
        return None
    return value


# ── The request ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApiKeyContext:
    """A verified key, as `request.state.api_key`: who it is, and what it narrows its owner to."""

    key_id: UUID
    name: str
    owner_id: UUID
    org_id: UUID
    keys: frozenset[str]
    org_admin: bool
    #: An operator's key made while acting as `org_id`: it runs as that org's Owner seat.
    acting: bool = False
    #: An operator's key in their own org with every permission and Org administration: the operator's own rights.
    superadmin: bool = False


@dataclass(frozen=True)
class Refusal:
    """Why a presented key was refused: the status it answers, the audit reason, and the key when it resolved."""

    status: int
    reason: str
    key_id: Optional[UUID] = None
    key_name: Optional[str] = None
    owner_id: Optional[UUID] = None
    org_id: Optional[UUID] = None
    detail: Optional[str] = None


def of(conn: HTTPConnection) -> Optional[ApiKeyContext]:
    """The request's verified key, or None for any other caller."""
    found = getattr(conn.state, "api_key", None)
    return found if isinstance(found, ApiKeyContext) else None


def key_set(row: ApiKey) -> frozenset[str]:
    """The keys a key confers before the owner cap: every honoured key while `grants_all`, else its ticked ones —
    a key the catalog no longer has is dropped."""
    if row.grants_all:
        return GRANTS_ALL_KEYS
    return frozenset(row.permissions or ()) & PERMISSION_KEYS


def networks(row: ApiKey) -> tuple["Network", ...]:
    return tuple(ipaddress.ip_network(str(cidr), strict=False) for cidr in row.ip_allowlist or ())


def _owner_is_admin(session: Session, owner: AdminUser) -> bool:
    """Whether `owner` is an Org Owner/Admin of a tenant org right now (R3)."""
    from app.authz.principal import effective_access

    if not is_tenant_org(owner.org_id):
        return False
    return effective_access(session, owner.id)[1].org_role in (access.ORG_OWNER, access.ORG_ADMIN)


def is_operator(owner: AdminUser) -> bool:
    return is_operator_plane(owner.org_id, owner.is_superadmin)


def owner_refusal(session: Session, owner: Optional[AdminUser], org_id: UUID) -> Optional[str]:
    """Why `owner`'s keys in `org_id` do not work right now (`verify`'s audit reason), or None when they do. An
    operator holds keys in their own org and in any tenant org (made while acting as it); anyone else only in their
    own tenant org, while an Org Owner/Admin there and never a superadmin."""
    if owner is None or owner.disabled_at is not None:
        return "owner"
    if is_operator(owner):
        return None if owner.org_id == org_id or is_tenant_org(org_id) else "owner"
    if owner.org_id != org_id or owner.is_superadmin:
        return "owner"
    return None if _owner_is_admin(session, owner) else "owner_role"


def owner_row(session: Session, owner_id: UUID) -> Optional[AdminUser]:
    """A key's owner by id, on the identity lane: an operator's row lives in their own org, which a tenant org's
    scope does not read."""
    with seam.identity_scope():
        return session.scalar(select(AdminUser).where(AdminUser.id == owner_id))


#: The one read that finds a secret's org: the key is not yet known, so no org scope can apply. It names ONE row by its
#: unique hash and returns only its key and org; everything after it reads bound to that org (`verify`).
_SECRET_BY_HASH = text("SELECT api_key_id, org_id FROM api_key_secrets WHERE token_hash = :token_hash")


def verify(token: str, host: Optional[str]) -> ApiKeyContext | Refusal:
    """Resolve a well-formed key (`parse` passed) for a request from `host`. Blocking, on the identity lane
    (`app/db.py`): the middleware runs it on the thread pool. Touches `last_used_*` at most once per
    `WRITE_THROTTLE`."""
    from app import ip_allowlist
    from app.db import IdentitySessionLocal

    now = utcnow()
    with IdentitySessionLocal() as session:
        hit = session.execute(_SECRET_BY_HASH, {"token_hash": hash_secret(token)}).first()
        if hit is None:
            return Refusal(401, "unknown")
        with seam.bound_to(hit.org_id):
            found = session.execute(
                select(ApiKeySecret, ApiKey)
                .join(ApiKey, ApiKey.id == ApiKeySecret.api_key_id)
                .where(ApiKeySecret.api_key_id == hit.api_key_id, ApiKeySecret.token_hash == hash_secret(token))
            ).first()
            if found is None:
                return Refusal(401, "unknown")
            secret, key = found
            refused = _refusal_of(key)
            if secret.valid_until is not None and secret.valid_until <= now:
                return refused(401, "rotated")
            context = _live_context(session, key, now)
            if isinstance(context, Refusal):
                return context
            allowlist = networks(key)
            if allowlist and not ip_allowlist.allowed(allowlist, host):
                return refused(403, "ip", detail=host)
            if ip_allowlist.refusal(session, key.org_id, host) is not None:
                return refused(403, "org_ip", detail=host)
            last = key.last_used_at
            if last is None or now - last >= WRITE_THROTTLE:
                address = ip_allowlist.parse_address(host)
                _touch(session, key.id, now, str(address) if address is not None else None)
    return context


def _live_context(session: Session, key: ApiKey, now: datetime) -> ApiKeyContext | Refusal:
    """`key` as its request context while it still works (not revoked or expired, its owner still holding it, its
    org still admitting members), else the refusal. The secret and the address are the caller's to judge."""
    refused = _refusal_of(key)
    if key.revoked_at is not None:
        return refused(401, "revoked")
    if key.expires_at is not None and key.expires_at <= now:
        return refused(401, "expired")
    owner = owner_row(session, key.owner_id)
    reason = owner_refusal(session, owner, key.org_id)
    if reason is not None:
        return refused(401, reason)
    assert owner is not None
    if not _org_admits(session, key.org_id):
        return refused(401, "org")
    operator, own = is_operator(owner), owner.org_id == key.org_id
    return ApiKeyContext(
        key_id=key.id, name=key.name, owner_id=owner.id, org_id=key.org_id,
        keys=key_set(key), org_admin=bool(key.org_admin), acting=operator and not own,
        superadmin=operator and own and bool(key.grants_all) and bool(key.org_admin),
    )


def context_of(key_id: UUID, org_id: UUID) -> ApiKeyContext | Refusal:
    """A key verified earlier (a webhook delivery's, `trigger_intake`), as it stands now: `verify` without the secret
    or the address, which were judged when it was presented. Blocking, on the identity lane."""
    from app.db import IdentitySessionLocal

    with IdentitySessionLocal() as session, seam.bound_to(org_id):
        key = session.scalar(select(ApiKey).where(ApiKey.id == key_id))
        if key is None:
            return Refusal(401, "unknown")
        return _live_context(session, key, utcnow())


def _org_admits(session: Session, org_id: UUID) -> bool:
    """Whether the key's org still admits its members: not disabled, soft-deleted, rejected or pending — the
    sign-in doors' rule (`org_gate.sign_in_refusal`), since a key outlives every session those end."""
    from app import org_gate
    from app.models.orgs import Org

    enabled = session.scalar(select(Org.enabled).where(Org.id == org_id))
    return enabled is True and org_gate.sign_in_refusal(session, org_id) is None


def _refusal_of(key: ApiKey):
    def refused(status: int, reason: str, *, detail: Optional[str] = None) -> Refusal:
        return Refusal(status, reason, key.id, key.name, key.owner_id, key.org_id, detail)

    return refused


def _touch(session: Session, key_id: UUID, now: datetime, address: Optional[str]) -> None:
    """Last use, best-effort: a failed write never refuses the request. A machine column, so the
    `set_updated_at` trigger (narrowed to the editable columns) leaves `updated_at` alone."""
    try:
        session.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=now, last_used_ip=address))
        session.commit()
    except Exception:  # noqa: BLE001 — presence is cosmetic; the key already verified
        session.rollback()
        log.warning("api-keys: could not record the last use of key %s", key_id)


def refusal_response(refusal: Refusal, host: Optional[str]):
    """The response a refused key answers: 401 `invalid_api_key`, or 403 `ip_not_allowed` naming the address."""
    from fastapi.responses import JSONResponse

    from app import ip_allowlist

    if refusal.reason == "org_ip":
        return ip_allowlist.refused_response(host)
    if refusal.status == 403:
        return JSONResponse(
            {"detail": f"This API key does not allow requests from {host or 'unknown'}.",
             "code": ip_allowlist.REFUSAL_CODE, "ip": host},
            status_code=403,
        )
    return JSONResponse({"detail": INVALID_DETAIL}, status_code=401)


def key_user(user: AdminUser, context: ApiKeyContext) -> AdminUser:
    """The row a handler is handed for a key's request, matching `narrow`: the acted org's Owner seat for an acting
    key (`act_as.seat_user`), a detached copy without the superadmin flag for an operator key short of
    `context.superadmin` (so `require_operator` refuses it), else `user` itself."""
    from app import act_as

    if context.acting:
        return act_as.seat_user(user, context.org_id)
    if user.is_superadmin and not context.superadmin:
        return act_as.plain_user(user)
    return user


def narrow(principal: "Principal", context: ApiKeyContext) -> "Principal":
    """The owner's principal (an acting key's: the org's Owner seat) cut down to what the key confers: every key set ∩
    the key's, no bindings, and no Org Admin rank unless the key's "Org administration" toggle is on — whose loss also
    hides the org's system projects, as for any non-admin (`Principal.system_project_ids`). A superadmin owner holds
    every key, so their key holds its own ticked ones; it stays superadmin only when `context.superadmin`."""
    from app.authz.principal import _system_project_ids

    keys = context.keys
    held = keys if principal.is_superadmin else principal.permissions & keys
    narrowed = replace(
        principal,
        is_superadmin=context.superadmin,
        acting=context.acting,
        permissions=held,
        org_keys=keys if principal.is_superadmin else principal.org_keys & keys,
        scope_permissions=frozenset(t for t in principal.scope_permissions if t[2] in keys),
        org_role=principal.org_role if context.org_admin else None,
        bindings=(),
        api_key_id=context.key_id,
    )
    if not narrowed.is_org_admin and narrowed.org_id is not None and not narrowed.system_project_ids:
        from app.db import IdentitySessionLocal

        with IdentitySessionLocal() as session:
            hidden = _system_project_ids(session, narrowed.org_id)
        if hidden:
            narrowed = replace(narrowed, system_project_ids=hidden)
    return narrowed


# ── Refusals ──────────────────────────────────────────────────────────────────────────────────────────

_REFUSALS_LOCK = threading.Lock()
#: (key id, reason) → monotonic time of the last audited refusal, in this process.
_REFUSALS: dict[tuple[UUID, str], float] = {}
_REFUSALS_PRUNE_AT = 10_000


def _due(key_id: UUID, reason: str) -> bool:
    now = time.monotonic()
    window = WRITE_THROTTLE.total_seconds()
    with _REFUSALS_LOCK:
        last = _REFUSALS.get((key_id, reason))
        if last is not None and now - last < window:
            return False
        if len(_REFUSALS) > _REFUSALS_PRUNE_AT:
            for stale in [k for k, at in _REFUSALS.items() if now - at >= window]:
                _REFUSALS.pop(stale, None)
        _REFUSALS[(key_id, reason)] = now
        return True


def clear_caches() -> None:
    """Forget the refusal throttle (tests)."""
    with _REFUSALS_LOCK:
        _REFUSALS.clear()


def record_refusal(refusal: Refusal, *, client_ip: Optional[str]) -> None:
    """One `api_key_refused` audit row for a refusal of a RESOLVED key, at most once per `WRITE_THROTTLE` per
    (key, reason); an unknown hash writes nothing (it names no key and no org). Own session, committed: the caller is
    about to refuse. Best-effort: a failed write is logged and the refusal still answers."""
    if refusal.key_id is None or refusal.org_id is None or not _due(refusal.key_id, refusal.reason):
        return
    from app.db import SessionLocal
    from app.user_activity import record_write

    try:
        with SessionLocal() as session, seam.bound_to(refusal.org_id):
            owner = owner_row(session, refusal.owner_id) if refusal.owner_id is not None else None
            record_write(
                session, owner, "api_key_refused",
                f"Refused API key {refusal.key_name} ({refusal.reason})",
                data={
                    "api_key_id": refusal.key_id, "api_key_name": refusal.key_name,
                    "reason": refusal.reason, "detail": refusal.detail, "client_ip": client_ip,
                },
                org_id=refusal.org_id, in_org=refusal.org_id,
            )
            session.commit()
    except Exception:  # noqa: BLE001 — see the docstring
        log.exception("api-keys: could not audit a refusal of key %s", refusal.key_id)


def refuse_scope(conn: HTTPConnection, principal: "Principal", detail: str) -> None:
    """Audit a key refused by the guard for a permission it does not carry (`authz/enforce.py`)."""
    context = of(conn)
    if context is None or principal.api_key_id is None:
        return
    record_refusal(
        Refusal(403, "scope", context.key_id, context.name, context.owner_id, context.org_id, detail),
        client_ip=conn.client.host if conn.client else None,
    )


def refuse_key(request: Request) -> None:
    """403 `api_key_not_allowed` when `request` carries a key — for an arm of a route a key may otherwise reach
    (`PATCH /api/users/{id}`'s password and email)."""
    if of(request) is not None:
        raise ApiKeyNotAllowed()


class ApiKeyNotAllowed(HTTPException):
    """A key on a `@session_only` route or arm; `app/main.py` answers it in the `{"detail", "code"}` shape."""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail=NOT_ALLOWED_DETAIL)


def not_allowed_response():
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": NOT_ALLOWED_DETAIL, "code": NOT_ALLOWED_CODE}, status_code=403)


# ── Status ────────────────────────────────────────────────────────────────────────────────────────────


def status(row: ApiKey, *, owner_ok: bool, now: Optional[datetime] = None) -> str:
    """`revoked` > `expired` > `suspended` (owner not an Org Owner/Admin right now) > `expiring` (≤ 7 days) >
    `active`."""
    now = now or utcnow()
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is not None and row.expires_at <= now:
        return "expired"
    if not owner_ok:
        return "suspended"
    if row.expires_at is not None and row.expires_at - now <= EXPIRING_WITHIN:
        return "expiring"
    return "active"


def owner_ok(session: Session, owner: Optional[AdminUser], org_id: UUID) -> bool:
    """Whether the owner's keys in `org_id` work right now, as `verify` decides it."""
    return owner_refusal(session, owner, org_id) is None


def user_row(session: Session, user_id: Optional[UUID]) -> Optional[AdminUser]:
    """A member row by id under the caller's scope (an ORM select, which the seam filters), or None."""
    if user_id is None:
        return None
    return session.scalar(select(AdminUser).where(AdminUser.id == user_id))


# ── Lifecycle ─────────────────────────────────────────────────────────────────────────────────────────


def summary(context: ApiKeyContext) -> tuple[str, Optional[datetime], frozenset[int]]:
    """`(display, expires_at, hidden system project ids)` of a verified key, read bound to its own org on a fresh
    identity-lane session — for `GET /api/auth/me`, an `@always` route that binds nothing. The system projects are
    hidden as `narrow` hides them: only while the key's "Org administration" toggle is off."""
    from app.authz.principal import _system_project_ids
    from app.db import IdentitySessionLocal

    with IdentitySessionLocal() as session, seam.bound_to(context.org_id):
        expires_at = session.scalar(select(ApiKey.expires_at).where(ApiKey.id == context.key_id))
        current = current_secret(session, context.key_id)
        hidden = frozenset() if context.org_admin else _system_project_ids(session, context.org_id)
        return (display(current.last4) if current is not None else PREFIX), expires_at, hidden


def add_secret(session: Session, row: ApiKey) -> str:
    """Stage a new current secret for `row` and return it (once)."""
    token = mint_secret()
    session.add(ApiKeySecret(
        api_key_id=row.id, org_id=row.org_id, token_hash=hash_secret(token), last4=token[-4:],
    ))
    return token


def current_secret(session: Session, key_id: UUID) -> Optional[ApiKeySecret]:
    return session.scalar(
        select(ApiKeySecret).where(ApiKeySecret.api_key_id == key_id, ApiKeySecret.valid_until.is_(None))
        .order_by(ApiKeySecret.created_at.desc()).limit(1)
    )


def rotate(session: Session, row: ApiKey, overlap: str, now: Optional[datetime] = None) -> str:
    """A new secret for `row`, same settings. `none` deletes every older secret; otherwise the current one keeps
    working for the overlap (an older one still overlapping ends no later)."""
    now = now or utcnow()
    window = OVERLAPS[overlap]
    # One rotation at a time: two racing would each leave a current secret.
    session.execute(select(ApiKey.id).where(ApiKey.id == row.id).with_for_update())
    for secret in session.scalars(select(ApiKeySecret).where(ApiKeySecret.api_key_id == row.id)).all():
        if window is None or (secret.valid_until is not None and secret.valid_until <= now):
            session.delete(secret)
        else:
            # Never longer than this rotation's overlap: a shorter one also shortens an older secret's.
            end = now + window
            secret.valid_until = end if secret.valid_until is None else min(secret.valid_until, end)
    session.flush()
    return add_secret(session, row)


def revoke(row: ApiKey, by: Optional[AdminUser], reason: str = REVOKED, now: Optional[datetime] = None) -> bool:
    """Stamp `row` revoked; False when it already was (idempotent). Never `updated_by_id`: a revoke by another admin
    is not an edit of the owner's key."""
    if row.revoked_at is not None:
        return False
    row.revoked_at = now or utcnow()
    row.revoked_by_id = by.id if by is not None else None
    row.revoke_reason = reason
    return True


def revoke_for_owner(session: Session, owner: AdminUser, actor: Optional[AdminUser], reason: str = OWNER_DISABLED) -> int:
    """Stage the revocation of every live key `owner` holds, auditing each; how many. Disabling a member ends their
    keys for good (R3), re-enabling them does not bring the keys back."""
    from app.user_activity import record_write

    rows = session.scalars(select(ApiKey).where(ApiKey.owner_id == owner.id, ApiKey.revoked_at.is_(None))).all()
    for row in rows:
        revoke(row, actor, reason)
        record_write(
            session, actor, "api_key_revoked", f"Revoked API key {row.name} of {owner.email} ({reason})",
            data={"api_key_id": row.id, "api_key_name": row.name, "reason": reason, "owner_email": owner.email},
            in_org=row.org_id,
        )
    return len(rows)


def forget_owner(session: Session, owner: AdminUser, actor: Optional[AdminUser]) -> int:
    """Stage what deleting `owner` owes their keys: one `api_key_revoked` row each (reason `owner_removed`, with the
    name and display id, since the row goes), then the keys themselves (secrets cascade) — `ON DELETE RESTRICT` keeps
    a delete that skips this from erasing them unaudited. `routes/users.py::delete_member`. An operator's keys in
    tenant orgs, which the caller's scope may not read, go first on the fleet plane (`_forget_elsewhere`)."""
    gone = _forget_elsewhere(owner, actor) if is_operator(owner) else 0
    return gone + _forget(session, owner, actor)


def _forget_elsewhere(owner: AdminUser, actor: Optional[AdminUser]) -> int:
    """`forget_owner` for an operator's keys in every org, committed on its own fleet session: a later failure of the
    delete leaves those keys gone, each audited."""
    if not edition_features.enabled("multi_node_hosts"):
        return 0  # No operator-plane fleet session (`vnodes`) without Multi-Node Hosts: the one org is all there is.
    from app import vnodes

    with vnodes.fleet_session() as fleet:
        gone = _forget(fleet, owner, actor)
        fleet.commit()
    return gone


def _forget(session: Session, owner: AdminUser, actor: Optional[AdminUser]) -> int:
    from app.user_activity import record_write

    rows = session.scalars(select(ApiKey).where(ApiKey.owner_id == owner.id)).all()
    for row in rows:
        current = current_secret(session, row.id)
        record_write(
            session, actor, "api_key_revoked", f"Removed API key {row.name} with its owner {owner.email}",
            data={
                "api_key_id": row.id, "api_key_name": row.name, "reason": OWNER_REMOVED,
                "owner_email": owner.email, "display": display(current.last4) if current is not None else None,
            },
            in_org=row.org_id,
        )
        session.delete(row)
    if rows:
        session.flush()
    return len(rows)


def validate_permissions(keys: Iterable[str]) -> list[str]:
    """The ticked keys, sorted and de-duplicated; ValueError naming any the catalog does not have."""
    chosen = sorted(set(keys))
    unknown = [key for key in chosen if key not in PERMISSION_KEYS]
    if unknown:
        raise ValueError(f"Unknown permissions: {', '.join(unknown)}.")
    return chosen


def validate_allowlist(entries: Iterable[str]) -> list[str]:
    """Each IP or CIDR as stored (`ip_allowlist.parse_range`: a bare address is /32 or /128); ValueError carries the
    sentence a person is shown."""
    from app import ip_allowlist

    out: list[str] = []
    for entry in entries:
        network = str(ip_allowlist.parse_range(entry))
        if network not in out:
            out.append(network)
    if len(out) > ip_allowlist.MAX_RANGES:
        raise ValueError(f"An API key allows at most {ip_allowlist.MAX_RANGES} addresses or ranges.")
    return out


# ── The sweep ─────────────────────────────────────────────────────────────────────────────────────────

#: The reminders, furthest first: `(window, stamp column)`. A key inside the 1-day window owes no 7-day one.
REMINDERS: tuple[tuple[timedelta, str], ...] = ((timedelta(days=7), "reminded_7d_at"), (timedelta(days=1), "reminded_1d_at"))

#: The `user_notify` event name (a `preferences.NOTIFICATION_EVENTS` key) and the audit kind of a key that ran out.
REMINDER_EVENT = "api_key_expiry"
EXPIRED_KIND = "api_key_expired"


def console_path(key_id: UUID) -> str:
    return f"/settings/api-keys/{key_id}"


def expiry_phrase(expires_at: datetime, now: datetime, *, with_time: bool = False) -> str:
    """When a key ends, as a person says it: `expires in 1 day (Oct 30)`, `expires in 5 hours (Oct 30)`,
    `expired (Oct 30)`. `with_time` adds the UTC hour, for an email read later and elsewhere."""
    expires_at = expires_at.astimezone(timezone.utc)
    when = f"{expires_at:%b} {expires_at.day}"
    if expires_at.year != now.year:
        when += f", {expires_at.year}"
    if with_time:
        when += f", {expires_at:%H:%M} UTC"
    left = expires_at - now
    if left <= timedelta(0):
        return f"expired ({when})"
    if left < timedelta(hours=1):
        return f"expires in under an hour ({when})"
    if left < timedelta(hours=20):
        hours = int(left / timedelta(hours=1) + 0.5)
        return f"expires in {hours} hour{'s' if hours != 1 else ''} ({when})"
    days = max(1, int(left / timedelta(days=1) + 0.5))
    return f"expires in {days} day{'s' if days != 1 else ''} ({when})"


def reminder_sentence(name: str, expires_at: datetime, now: datetime, *, with_time: bool = False) -> str:
    return f"API key ‘{name}’ {expiry_phrase(expires_at, now, with_time=with_time)}"


@dataclass
class SweepReport:
    reminded: int = 0
    expired: int = 0
    revoked: int = 0
    errors: int = 0
    failed_orgs: list[UUID] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return bool(self.reminded or self.expired or self.revoked or self.errors)

    def summary(self) -> str:
        if not self.acted:
            return "no key to remind, record or revoke"
        return f"{self.reminded} reminded, {self.expired} expired, {self.revoked} revoked, {self.errors} failed"


def sweep(now: Optional[datetime] = None) -> SweepReport:
    """One pass over every org holding an unrevoked key, on the operator plane; each org in its own transaction, so
    one org's fault leaves the rest (retried next pass). Idempotent: every act is stamped on the key it is about."""
    fleet: AbstractContextManager[Session]
    if edition_features.enabled("multi_node_hosts"):
        from app import vnodes

        fleet = vnodes.fleet_session()
    else:
        # Run only by its system cron, which already holds the operator plane (`cron_runtime`).
        from app.db import SessionLocal

        fleet = SessionLocal()
    now = now or utcnow()
    report = SweepReport()
    with fleet as session:
        org_ids = list(session.scalars(select(ApiKey.org_id).where(ApiKey.revoked_at.is_(None)).distinct()).all())
        session.rollback()
        for org_id in org_ids:
            try:
                due = _sweep_org(session, org_id, now, report)
                # The stamps first, then the notices: a fault before this commit re-sends nothing next pass.
                session.commit()
            except Exception:  # noqa: BLE001 — one org must not stall the rest; retried next pass
                session.rollback()
                report.errors += 1
                report.failed_orgs.append(org_id)
                log.exception("api-keys: the sweep of org %s failed", org_id)
                continue
            _send(session, due, report)
    return report


def _send(session: Session, due: list[tuple[AdminUser, dict]], report: SweepReport) -> None:
    """Hand each committed reminder to the one choke point (no channel named), then commit its `notification_sent`
    rows. `deliver` never raises on a send fault; a failed audit commit loses the rows, not the notices."""
    from app import user_notify

    for owner, event in due:
        user_notify.deliver(session, owner, event, event_name=REMINDER_EVENT, important=True)
        report.reminded += 1
    try:
        session.commit()
    except Exception:  # noqa: BLE001 — the notices already went; the next org must still run
        session.rollback()
        report.errors += 1
        log.exception("api-keys: could not record %d reminder(s)", len(due))


def _sweep_org(session: Session, org_id: UUID, now: datetime, report: SweepReport) -> list[tuple[AdminUser, dict]]:
    """The disabled-owner backstop first (a revoked key is owed nothing else), then each key's expiry record or
    reminder stamp; returns the reminders to send once the stamps commit. Rows locked, skipping any a concurrent edit
    or pass holds: the next pass gets them."""
    rows = session.scalars(
        select(ApiKey).where(ApiKey.org_id == org_id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at).with_for_update(skip_locked=True)
    ).all()
    owners = {
        owner.id: owner
        for owner in session.scalars(select(AdminUser).where(AdminUser.id.in_({row.owner_id for row in rows})))
    }
    for owner in owners.values():
        if owner.disabled_at is not None:
            report.revoked += revoke_for_owner(session, owner, None, OWNER_DISABLED)
    due: list[tuple[AdminUser, dict]] = []
    for row in rows:
        owner = owners.get(row.owner_id)
        if row.revoked_at is not None or owner is None or row.expires_at is None:
            continue
        if row.expires_at <= now:
            if row.expiry_recorded_at is None:
                _record_expiry(session, row, owner, now)
                report.expired += 1
        else:
            event = _reminder(session, row, now)
            if event is not None:
                due.append((owner, event))
    return due


def _record_expiry(session: Session, row: ApiKey, owner: AdminUser, now: datetime) -> None:
    """One `api_key_expired` row per key, ever: the moment it stopped working, which no request records."""
    from app.user_activity import record_write

    assert row.expires_at is not None
    current = current_secret(session, row.id)
    record_write(
        session, None, EXPIRED_KIND, f"API key {row.name} of {owner.email} expired",
        data={
            "api_key_id": row.id, "api_key_name": row.name, "owner_email": owner.email,
            "display": display(current.last4) if current is not None else None,
            "expires_at": row.expires_at.isoformat(),
        },
        org_id=row.org_id,
    )
    row.expiry_recorded_at = now


def _reminder(session: Session, row: ApiKey, now: datetime) -> Optional[dict]:
    """Stamp the reminder this key owes now and return its event, or None: the nearest window it is inside whose
    stamp is unset. A 1-day send stamps the 7-day one too, so a key created with a day left gets one reminder, not
    two."""
    from app import alert_relay, email_notify

    assert row.expires_at is not None
    left = row.expires_at - now
    due = [column for window, column in REMINDERS if left <= window]
    if not due or getattr(row, due[-1]) is not None:
        return None
    for column in due:
        if getattr(row, column) is None:
            setattr(row, column, now)
    current = current_secret(session, row.id)
    shown = display(current.last4) if current is not None else PREFIX
    # The owner's own text, but a Slack leg reads `<!here>` as markup: no angle brackets reach it.
    name = row.name.replace("<", "‹").replace(">", "›")
    return alert_relay.message(
        REMINDER_EVENT,
        f"{reminder_sentence(name, row.expires_at, now, with_time=True)}. Rotate it in Settings > API keys "
        f"before then, or requests that use {shown} start failing.",
        payload={
            "console_url": email_notify.console_path_link(console_path(row.id)),
            "api_key_id": str(row.id), "api_key_name": row.name, "display": shown,
            "expires_at": row.expires_at.isoformat(),
        },
    )


# ── The bell (`routes/inbox.py`, kind `api_key_expiring`) ─────────────────────────────────────────────


def _reminded_at():
    """A key's latest reminder stamp (the 1-day one when both are set)."""
    return func.coalesce(ApiKey.reminded_1d_at, ApiKey.reminded_7d_at)


def reminder_feed(session: Session, org_id: UUID, user_id: UUID, *, limit: int) -> list[tuple[ApiKey, datetime]]:
    """The caller's own unrevoked keys in their active org reminded since they last opened one, newest first, each
    with its reminder's time."""
    reminded = _reminded_at()
    rows = session.execute(
        select(ApiKey, reminded)
        .where(
            ApiKey.org_id == org_id, ApiKey.owner_id == user_id, ApiKey.revoked_at.is_(None), reminded.is_not(None),
            (ApiKey.reminder_seen_at.is_(None)) | (reminded > ApiKey.reminder_seen_at),
        )
        .order_by(reminded.desc())
        .limit(limit)
    ).all()
    return [(row, at) for row, at in rows]


def reminder_wire(session: Session, row: ApiKey, now: Optional[datetime] = None) -> dict[str, str]:
    """The bell's shape: the server's sentence (`text`, relative to now), a second line (`detail`) and `href`."""
    current = current_secret(session, row.id)
    assert row.expires_at is not None, "only an expiring key is reminded"
    return {
        "text": reminder_sentence(row.name, row.expires_at, now or utcnow()),
        "detail": f"API keys · {display(current.last4) if current is not None else PREFIX}",
        "href": console_path(row.id),
    }


def mark_reminders_seen(session: Session, org_id: UUID, user_id: UUID, key_ids: list[UUID]) -> int:
    """Stamp the caller's read of each named key's reminder; anyone else's key is skipped silently. The stamp is the
    reminder's own time, not the clock's: a later reminder stamped by a pass that began before this read still shows.
    A machine column, so `updated_at` stays."""
    if not key_ids:
        return 0
    result = session.execute(
        update(ApiKey)
        .where(ApiKey.org_id == org_id, ApiKey.owner_id == user_id, ApiKey.id.in_(key_ids))
        .values(reminder_seen_at=_reminded_at())
    )
    return result.rowcount or 0
