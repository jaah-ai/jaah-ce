"""The access token an OAuth2 credential row is currently good for: minted, cached, and locked.

Single home of check-cache / exchange (RFC 6749 §6) / write-back. A row with no
`client_id`/`client_secret` cannot be refreshed and answers None (`credentials/registry.py`).

The refresh runs on its own session and commits itself (`db.get_session` never commits, so a write
there would roll back), under a `FOR UPDATE` lock with a re-read so two callers do not both spend the
refresh token (fatal at a rotating provider). Same shape as `git_access.py::token_for`, without its
durable workflow (`durable/git_token.py`). The cache lives in the row: `access_token` plus
`expiry_date` as epoch milliseconds in a string, the `google` type's format the node reads verbatim.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import http_bounded, oauth2_wire
from app.credential_crypto import has_sealed_secret, open_secret
from app.credentials.oauth_wizards import connect_for_type
from app.db import SessionLocal
from app.entity_shape import is_blank
from app.models.credentials import Credential

log = logging.getLogger("oauth2-tokens")

# How long a cached token must still have left to be handed out. Generous because a runner uses it for
# minutes; an early refresh costs one request, a late one a 401 inside someone's task.
_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True)
class AccessToken:
    """A live access token and when it stops being one.

    `expires_at` is None when the provider sent no `expires_in`: a non-expiring token, not an unknown,
    so it is served until the provider rejects it.
    """

    token: str
    expires_at: Optional[datetime]


def access_token_for(session: Session, credential: Credential) -> Optional[AccessToken]:
    """A live access token for `credential`, minting one if the cache has expired.

    None is always an answer, never an error; every caller then serves the config as it stands. Reasons:
    no OAuth2 provider, no client, no refresh token, a refused exchange, the row changed under the lock,
    lock timeout, or a row-supplied endpoint that is not https or not a public address. Each is logged where it happens. The operator sees a
    refused exchange's real reason via Test (`credentials/testers.py::_refresh_exchange`).

    `session` is the caller's and never written to, but after a mint the row's `config` is expired on
    it, so `routes/agent/task_secrets.py` serving the config beside the token does not hand over the dead one.
    """
    endpoint = token_endpoint(credential)
    if endpoint is None:
        return None
    config = dict(credential.config or {})
    if is_blank(config.get("client_id")) or is_blank(config.get("client_secret")):
        return None
    if is_blank(config.get("refresh_token")):
        return None

    # Fast path: a live cache costs no lock, session or round trip (one private operation to open it).
    cached = _cached(credential)
    if cached is not None:
        return cached

    # A row-supplied endpoint is a host its author chose, and refreshing makes the control plane POST the
    # client secret there. So it must pass the one destination rule (`http_bounded`), in every org. The
    # route checks it at connect too; this is the dial itself, reached unattended from
    # `routes/agent/task_secrets.py::get_task_credential`, so it guards the broker regardless of route. After the cache:
    # serving a cached token dials nothing, so a DNS blip must not deny it.
    refusal = _pins_nothing(credential) and http_bounded.public_destination_refusal(endpoint)
    if refusal:
        # No exception, per this function's contract: None means "no token available".
        log.warning(
            "oauth2 refresh refused: credential %s has a Token URL the control plane does not dial (%s)",
            credential.id,
            refusal,
        )
        return None

    minted = _refreshed(credential.id, endpoint, _vendor(credential), credential.type)
    if minted is not None:
        # The commit happened on another session (separate identity maps, `expire_on_commit=False`), so expire
        # the column here to force a re-read (`config` is a property over `config_sealed`, the mapped one).
        session.expire(credential, ["config_sealed"])
    return minted


def _refreshed(
    credential_id: Any, endpoint: str, vendor: str, type_slug: str
) -> Optional[AccessToken]:
    """Take the row lock, re-check, exchange, write and commit, all on this module's own session.

    Takes the credential's ID, not the instance, to avoid lazy-loading across two sessions; every value
    needed is passed in from the fast path.
    """
    with SessionLocal() as own:
        # Bound the lock wait: `FOR UPDATE` waits forever, and a waiter behind a slow vendor would pin a
        # second pooled connection (pool exhaustion has taken the CP down before, `app/db.py`). Ten seconds
        # exceeds a healthy exchange and is under the wire's own bound. `SET LOCAL` scopes it to this
        # transaction. Expiry raises Postgres `55P03`, which SQLAlchemy wraps as `OperationalError`
        # (measured).
        own.execute(text("SET LOCAL lock_timeout = '10s'"))
        # `populate_existing=True` is required: otherwise the identity map answers the `get` and the re-check
        # examines the same stale cache.
        try:
            locked = own.get(
                Credential, credential_id, with_for_update=True, populate_existing=True
            )
        except OperationalError:
            # Another caller holds the lock mid-exchange; its write serves the next reader. None degrades to
            # "no access token, here is the config" rather than a 500.
            log.warning(
                "oauth2 refresh for credential %s gave up waiting for the row lock", credential_id
            )
            return None
        if locked is None:
            # Deleted between the fast-path read and the lock.
            return None
        locked_config = dict(locked.config or {})
        fresh = _cached(locked)
        if fresh is not None:
            # A concurrent caller refreshed while we waited; minting again would spend the refresh token and,
            # at a rotating provider, retire the one that caller relies on.
            return fresh

        client_id = str(locked_config.get("client_id") or "")
        if (
            not client_id
            or not has_sealed_secret(locked_config, "refresh_token")
            or not has_sealed_secret(locked_config, "client_secret")
        ):
            # Cleared under the lock by an operator edit.
            return None
        # The two sealed fields are opened here, where they are consumed (the one seam,
        # `credential_crypto.open_secret`); `client_id` is plain.
        try:
            refresh_token = open_secret(locked, "refresh_token")
            client_secret = open_secret(locked, "client_secret")
        except ValueError:
            # A moved, re-pointed or tampered envelope. The field name only; nothing out of the row.
            log.warning(
                "oauth2 refresh for credential %s could not open its refresh token or client secret; "
                "re-enter them", credential_id,
            )
            return None
        if is_blank(refresh_token) or is_blank(client_secret):
            # Whitespace-only, which the fast path cannot see through a sealed marker.
            return None

        try:
            grant = oauth2_wire.refresh(
                endpoint=endpoint,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
                vendor=vendor,
            )
        except oauth2_wire.OAuth2Error as exc:
            # `exc.code` only; no message, token, secret or config value reaches the log.
            log.warning(
                "oauth2 refresh failed for credential %s (type=%s code=%s)",
                credential_id,
                type_slug,
                exc.code,
            )
            return None

        _store(locked, grant)
        # The commit makes the cache real and releases the lock; see the module header.
        own.commit()
        return AccessToken(token=grant.access_token, expires_at=grant.expires_at)


def token_endpoint(credential: Credential) -> Optional[str]:
    """Where a refresh for `credential` would be POSTed, or None if this type has no OAuth2 flow.

    Provider first, row second, the same resolution as `testers.py::_refresh_exchange`, exported so the
    two cannot drift. A provider pinning its token URL owns it; otherwise the row's `token_url` is used.

    A row-supplied URL is the author's choice; this function still answers (the tester reports it), and
    `access_token_for` refuses to dial one that fails the destination rule.
    """
    provider = connect_for_type(credential.type)
    if provider is None:
        return None
    if provider.token_url:
        return provider.token_url
    endpoint = (credential.config or {}).get("token_url")
    # Stripped here, once, so the string `access_token_for` checks is the string it dials.
    return endpoint.strip() if isinstance(endpoint, str) and endpoint.strip() else None


def _pins_nothing(credential: Credential) -> bool:
    """Does this credential's provider pin no token endpoint, so the one in use came off the row?

    The same `provider.token_url` test as `routes/credentials/`'s connect guard, so the two agree.
    """
    provider = connect_for_type(credential.type)
    return provider is not None and not provider.token_url


def _vendor(credential: Credential) -> str:
    """What the provider is called in an error message: the type label, not the row name."""
    provider = connect_for_type(credential.type)
    if provider is not None and provider.provider_label:
        return provider.provider_label
    return "the vendor"


# `expiry_date` has three states, and collapsing any two is a bug:
#   absent/blank - never recorded (the provider sent no `expires_in`): served until rejected, since
#                  refreshing every read would cost an exchange per request.
#   unreadable   - recorded but unparseable: cannot be judged, so it falls through to a refresh,
#                  whose write heals the row. Treated as absent, a stale token would be served forever.
#   an instant   - compared against the margin.
_UNREADABLE = object()


def _cached(credential: Credential) -> Optional[AccessToken]:
    """The stored access token if it is still live, else None.

    The three-state read of `expiry_date` is `_expiry`'s; see the block above it. Expiry (a plain field)
    is judged FIRST and the sealed `access_token` opened (`credential_crypto.open_secret`, the private
    operation) only for a cache that is still live, so a dead cache costs nothing to reject. A token that
    cannot be opened reads as no cache, so the refresh's write heals the row.
    """
    config = credential.config or {}
    if not has_sealed_secret(config, "access_token"):
        return None
    expires_at = _expiry(config.get("expiry_date"))
    if expires_at is _UNREADABLE:
        return None
    if expires_at is not None:
        assert isinstance(expires_at, datetime)  # narrowing for the type checker
        if expires_at - _MARGIN <= datetime.now(timezone.utc):
            return None
    try:
        token = open_secret(credential, "access_token")
    except ValueError:
        # A moved, re-pointed or tampered envelope; the id only, nothing out of the row.
        log.warning(
            "oauth2 cached access token for credential %s could not be opened; refreshing", credential.id
        )
        return None
    if not token.strip():
        return None
    return AccessToken(token=token, expires_at=expires_at)


def _expiry(value: Any) -> Any:
    """A stored `expiry_date` as an instant, `None` when absent, or `_UNREADABLE` when it is neither.

    Epoch milliseconds (see the module header). Accepts a number or a digit string: a vendor CLI writes a
    JSON number, the registry stores a string. `True` is `_UNREADABLE` because `bool` is an `int`.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):
        return _UNREADABLE
    if isinstance(value, (int, float)):
        millis: float = value
    elif isinstance(value, str) and value.strip().isdigit():
        millis = float(value.strip())
    else:
        return _UNREADABLE
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        # Numeric but unrepresentable: unreadable, not absent, so it cannot pin its token forever.
        return _UNREADABLE


def _store(credential: Credential, grant: oauth2_wire.TokenGrant) -> None:
    """Write a fresh grant onto `credential`: the access token, its expiry, and a rotated refresh token
    if one came back.

    The refresh token is only replaced, never cleared: absent from the response is the normal case (RFC
    6749 §6), unlike `git_access.py::_store_grant`, where absence is meaningful.

    The dict is reassigned, not mutated: JSONB is tracked by identity, so an in-place mutation would
    never emit the UPDATE.
    """
    config = dict(credential.config or {})
    config["access_token"] = grant.access_token
    if grant.refresh_token:
        config["refresh_token"] = grant.refresh_token
    if grant.expires_at:
        config["expiry_date"] = str(int(grant.expires_at.timestamp() * 1000))
    else:
        config.pop("expiry_date", None)
    credential.config = config
