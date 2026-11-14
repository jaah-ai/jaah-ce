"""The access token an OAuth2 credential row is currently good for: minted, cached, and locked.

Single home of check-cache / exchange (RFC 6749 §6) / write-back, through `oauth_providers.refresh` (each
provider's refresh endpoint and wire quirks). A row with no `client_id`/`client_secret` cannot be refreshed and
answers None (`credentials/registry.py`), unless its provider runs on the deployment's own app
(`OAuthProvider.platform_app`), whose client comes from the operator knob instead.

The refresh runs on its own session and commits itself (a route session never commits on teardown, so a write
there would roll back), under a `FOR UPDATE` lock with a re-read so two callers do not both spend the
refresh token (fatal at a rotating provider). Same shape as `git_access.py::token_for`, without its
durable workflow (`durable/git_token.py`). The cache lives in the row: `access_token` plus
`expiry_date` as epoch milliseconds in a string, the `google` type's format the node reads verbatim.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import http_bounded, oauth2_wire, oauth_providers
from app.clock import epoch_ms_or_none, to_epoch_ms, utcnow
from app.credential_crypto import has_sealed_secret, open_secret
from app.credentials.oauth_wizards import connect_for_type
from app.db import BridgedSession
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


def access_token_for(
    session: Session, credential: Credential, *, margin: timedelta = _MARGIN
) -> Optional[AccessToken]:
    """A live access token for `credential`, minting one if the cache has expired.

    None is always an answer, never an error; every caller then serves the config as it stands. Reasons:
    no OAuth2 provider, no client, no refresh token, a refused exchange, the row changed under the lock,
    lock timeout, or a row-supplied endpoint that is not https or not a public address. Each is logged where it happens. The operator sees a
    refused exchange's real reason via Test (`credentials/testers.py::_refresh_exchange`).

    `session` is the caller's and never written to, but after a mint the row's `config` is expired on
    it, so `routes/agent/task_secrets.py` serving the config beside the token does not hand over the dead one.
    `margin` is how long a cached token must still be live to be served (the refresh cron asks for more,
    `refresh_expiring`). A refusal the vendor will repeat (`invalid_grant`) marks the row Needs reconnect
    (`_mark_needs_reconnect`), so the dead grant is shown, never discovered at runner time.
    """
    provider = connect_for_type(credential.type)
    if provider is not None and provider.discovery is not None:
        return _discovered_access_token(session, credential, provider, margin)
    endpoint = refresh_endpoint(credential)
    if endpoint is None:
        return None
    assert provider is not None  # narrowing: `refresh_endpoint` answered, so the type has a provider
    config = dict(credential.config or {})
    if provider.expiring_grant_ok and is_blank(config.get("refresh_token")):
        # Nothing renews it: serve the token Connect stored while it lives, then none (the row is marked Needs
        # reconnect by `refresh_expiring`).
        return _cached(credential, margin)
    platform_client = oauth_providers.uses_platform_client(provider, config)
    if not platform_client and oauth_providers.row_client_missing(provider, config):
        return None
    if is_blank(config.get("refresh_token")):
        return None

    # Fast path: a live cache costs no lock, session or round trip (one private operation to open it).
    cached = _cached(credential, margin)
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

    platform: Optional[tuple[str, str]] = None
    if platform_client:
        # Resolved before the row lock: it opens the operator credential on its own session, which must not
        # hold a second pooled connection under `FOR UPDATE`.
        platform = oauth_providers.platform_client(provider, consumer="credential_refresh")
        if platform is None:
            log.warning("oauth2 refresh for credential %s skipped: its provider's app is not set up", credential.id)
            return None
    minted = _refreshed(credential.id, endpoint, provider, credential.type, platform, margin)
    if minted is not None:
        # The commit happened on another session (separate identity maps, `expire_on_commit=False`), so expire
        # the column here to force a re-read (`config` is a property over `config_sealed`, the mapped one).
        session.expire(credential, ["config_sealed"])
    return minted


def _discovered_access_token(
    session: Session, credential: Credential, provider: oauth_providers.OAuthProvider, margin: timedelta
) -> Optional[AccessToken]:
    """`access_token_for` for a discovery provider (an MCP server): the token endpoint, the client and the
    `resource` are derived from the row's URL now (`mcp_oauth.grant_for`), never read off the row, so a grant is
    only ever spent where the URL its secrets are sealed to points. A cached token costs no discovery."""
    from app import mcp_oauth
    from app.integrations import catalog

    config = catalog.mcp_config(credential.type, credential.config or {})
    if is_blank(config.get("refresh_token")):
        return None
    cached = _cached(credential, margin)
    if cached is not None:
        return cached
    try:
        grant = mcp_oauth.grant_for(config)
    except mcp_oauth.McpOAuthError:
        log.warning("oauth2 refresh for credential %s skipped: its MCP authorization server was not rediscovered",
                    credential.id)
        return None
    refusal = http_bounded.public_destination_refusal(grant.token_endpoint)
    if refusal:
        log.warning("oauth2 refresh refused: credential %s's token endpoint is not dialled (%s)", credential.id, refusal)
        return None
    minted = _refreshed(
        credential.id, grant.token_endpoint, provider, credential.type,
        (grant.client_id, grant.client_secret) if grant.registered else None, margin,
        extra=grant.extra, basic_auth=grant.basic_auth, registered=grant.registered,
    )
    if minted is not None:
        session.expire(credential, ["config_sealed"])
    return minted


def _refreshed(
    credential_id: Any,
    endpoint: str,
    provider: oauth_providers.OAuthProvider,
    type_slug: str,
    platform: Optional[tuple[str, str]] = None,
    margin: timedelta = _MARGIN,
    *,
    extra: Optional[dict[str, str]] = None,
    basic_auth: Optional[bool] = None,
    registered: bool = False,
) -> Optional[AccessToken]:
    """Take the row lock, re-check, exchange, write and commit, all on this module's own session.

    Takes the credential's ID, not the instance, to avoid lazy-loading across two sessions; every value
    needed is passed in from the fast path. `registered`: `platform` is a discovery provider's own registration
    (`_discovered_access_token`), used whatever the row's client fields say; `extra`/`basic_auth` ride the call.
    """
    with BridgedSession() as own:
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
        fresh = _cached(locked, margin)
        if fresh is not None:
            # A concurrent caller refreshed while we waited; minting again would spend the refresh token and,
            # at a rotating provider, retire the one that caller relies on.
            return fresh

        row_client = not registered and not oauth_providers.uses_platform_client(provider, locked_config)
        # A public client (`OAuthProvider.public_client`, an MCP server's) holds no secret.
        public = provider.public_client and (not row_client or not has_sealed_secret(locked_config, "client_secret"))
        if not has_sealed_secret(locked_config, "refresh_token") or (
            row_client
            and (is_blank(locked_config.get("client_id"))
                 or not (public or has_sealed_secret(locked_config, "client_secret")))
        ):
            # Cleared under the lock by an operator edit.
            return None
        if not row_client:
            # The deployment's app (an operator knob, resolved by the caller before the lock), not the row's client.
            if platform is None:
                return None
            client_id, client_secret = platform
        else:
            client_id = str(locked_config.get("client_id") or "")
            client_secret = ""
        # The sealed fields are opened here, where they are consumed (the one seam,
        # `credential_crypto.open_secret`); `client_id` is plain.
        try:
            refresh_token = open_secret(locked, "refresh_token")
            if row_client and not public:
                client_secret = open_secret(locked, "client_secret")
        except ValueError:
            # A moved, re-pointed or tampered envelope. The field name only; nothing out of the row.
            log.warning(
                "oauth2 refresh for credential %s could not open its refresh token or client secret; "
                "re-enter them", credential_id,
            )
            return None
        if is_blank(refresh_token) or (is_blank(client_secret) and not public):
            # Whitespace-only, which the fast path cannot see through a sealed marker.
            return None

        try:
            grant = oauth_providers.refresh(
                provider,
                endpoint=endpoint,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
                vendor=_vendor(provider),
                extra=extra,
                basic_auth=basic_auth,
            )
        except oauth2_wire.OAuth2Error as exc:
            # `exc.code` only; no message, token, secret or config value reaches the log.
            log.warning(
                "oauth2 refresh failed for credential %s (type=%s code=%s)",
                credential_id,
                type_slug,
                exc.code,
            )
            if exc.code == oauth2_wire.INVALID_GRANT:
                # Revoked, expired or already replaced: no retry fixes it, only a reconnect.
                _mark_needs_reconnect(own, locked, "The vendor no longer honours this connection's grant.")
            return None

        _store(locked, grant)
        # The commit makes the cache real and releases the lock; see the module header.
        own.commit()
        return AccessToken(token=grant.access_token, expires_at=grant.expires_at)


def refresh_endpoint(credential: Credential) -> Optional[str]:
    """Where a refresh for `credential` is POSTed: the provider's own refresh endpoint (Figma's), else
    `token_endpoint`. None if this type has no OAuth2 flow."""
    provider = connect_for_type(credential.type)
    if provider is not None and provider.refresh_url:
        return oauth_providers.endpoint_for(provider, provider.refresh_url, credential.config or {})
    return token_endpoint(credential)


def token_endpoint(credential: Credential) -> Optional[str]:
    """`credential`'s token endpoint (where a refresh goes unless the provider has its own, `refresh_endpoint`),
    or None if this type has no OAuth2 flow.

    Provider first, row second, the same resolution as `testers.py::_refresh_exchange`, exported so the
    two cannot drift. A provider pinning its token URL owns it; otherwise the row's `token_url` is used.

    A row-supplied URL is the author's choice; this function still answers (the tester reports it), and
    `access_token_for` refuses to dial one that fails the destination rule.
    """
    provider = connect_for_type(credential.type)
    if provider is None:
        return None
    if provider.token_url:
        # An origin-scoped provider's is filled from the row's vendor-pinned host (None when it names none).
        return oauth_providers.endpoint_for(provider, provider.token_url, credential.config or {})
    endpoint = (credential.config or {}).get("token_url")
    # Stripped here, once, so the string `access_token_for` checks is the string it dials.
    return endpoint.strip() if isinstance(endpoint, str) and endpoint.strip() else None


def _pins_nothing(credential: Credential) -> bool:
    """Does this credential's provider pin no token endpoint, so the one in use came off the row?

    The same `provider.token_url` test as `routes/credentials/`'s connect guard, so the two agree.
    """
    provider = connect_for_type(credential.type)
    return provider is not None and not provider.token_url


def _vendor(provider: oauth_providers.OAuthProvider) -> str:
    """What the provider is called in an error message: its label, not the row name."""
    return provider.label or "the vendor"


# `expiry_date` has three states, and collapsing any two is a bug:
#   absent/blank - never recorded (the provider sent no `expires_in`): served until rejected, since
#                  refreshing every read would cost an exchange per request.
#   unreadable   - recorded but unparseable: cannot be judged, so it falls through to a refresh,
#                  whose write heals the row. Treated as absent, a stale token would be served forever.
#   an instant   - compared against the margin.
_UNREADABLE = object()


def _mark_needs_reconnect(session: Session, credential: Credential, reason: str) -> None:
    """Set the row's Needs reconnect pair and commit (`session` is `_refreshed`'s own). Cleared by any newly entered
    secret - a Connect, a refresh, a reconnect or a pasted token (`models/credentials.py::Credential.config`)."""
    credential.needs_reconnect_at = utcnow()
    credential.needs_reconnect_reason = reason
    session.commit()
    log.warning("oauth2 refresh for credential %s: grant refused for good; marked needs reconnect", credential.id)


#: How far ahead the refresh cron renews: a grant that died is then found before a runner needs the token (AC1.4).
REFRESH_LOOKAHEAD = timedelta(minutes=15)


@dataclass(frozen=True)
class RefreshReport:
    """One `refresh_expiring` pass: rows renewed, and rows whose renewal failed (a dead grant is also marked)."""

    renewed: int
    failed: int

    @property
    def acted(self) -> bool:
        return bool(self.renewed or self.failed)

    def summary(self) -> str:
        if not self.acted:
            return "no connected OAuth credential expires within 15 minutes"
        return f"renewed {self.renewed} credential(s); {self.failed} failed"


def refresh_expiring(session: Session) -> RefreshReport:
    """The `credential-token-refresh` cron's pass: renew every connected OAuth row whose cached access token runs
    out within `REFRESH_LOOKAHEAD`. A row already marked Needs reconnect is skipped; a failure
    the vendor will repeat marks the row (`_refreshed`). Only the tier-1 config is opened to judge the expiry, and a
    secret only for a row being renewed. One row that cannot be opened or renewed is counted and logged by id, and
    never stops the pass."""
    from sqlalchemy import select

    from app.credentials.oauth_wizards import OAUTH_CONNECT_PROVIDERS
    from app.credentials.registry import CREDENTIAL_TYPES

    types = [slug for slug, spec in CREDENTIAL_TYPES.items()
             if spec.oauth_connect is not None and spec.oauth_connect in OAUTH_CONNECT_PROVIDERS]
    horizon = utcnow() + REFRESH_LOOKAHEAD
    renewed = failed = 0
    rows = session.scalars(
        select(Credential).where(
            Credential.type.in_(types), Credential.needs_reconnect_at.is_(None), Credential.nodevault.is_(False),
        ).order_by(Credential.id)
    ).all()
    for row in rows:
        row_id = row.id
        try:
            config = row.config or {}
            expires_at = _expiry(config.get("expiry_date"))
            if is_blank(config.get("refresh_token")):
                provider = connect_for_type(row.type)
                if (provider is not None and provider.expiring_grant_ok and isinstance(expires_at, datetime)
                        and expires_at <= utcnow()):
                    # Nothing can renew it (LinkedIn's self-serve grants): show the dead grant, never serve it.
                    _mark_needs_reconnect(session, row, "The access token expired and the vendor issues no "
                                          "refresh token - press Connect to reauthorize.")
                    failed += 1
                continue
            if expires_at is None or (isinstance(expires_at, datetime) and expires_at > horizon):
                continue
            token = access_token_for(session, row, margin=REFRESH_LOOKAHEAD)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop every other org's renewal
            log.warning("oauth2 refresh pass: credential %s skipped (%s)", row_id, type(exc).__name__)
            session.rollback()
            failed += 1
            continue
        if token is None:
            failed += 1
        else:
            renewed += 1
    return RefreshReport(renewed, failed)


def _cached(credential: Credential, margin: timedelta = _MARGIN) -> Optional[AccessToken]:
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
        if expires_at - margin <= utcnow():
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
    # A bool, a non-digit string, or a number the platform cannot represent is unreadable, not absent, so it
    # cannot pin its token forever.
    return epoch_ms_or_none(value) or _UNREADABLE


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
        config["expiry_date"] = str(to_epoch_ms(grant.expires_at))
    else:
        config.pop("expiry_date", None)
    credential.config = config
