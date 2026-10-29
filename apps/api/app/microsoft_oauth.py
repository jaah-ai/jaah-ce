"""The Microsoft identity platform (Entra ID v2, `common` audience) adapter for console sign-in.

The client pair is the operator-designated Entra app Connect Azure DevOps also uses
(`azure_devops_app_credential_id`, resolved in `routes/login_oauth.py`), never an environment variable.

OIDC on top of the generic wire in `oauth2_wire.py`: authorization code + PKCE, `scope=openid profile
email`, and the identity read from the VERIFIED `id_token` — no MSAL, no Graph `/me`. The access token is
never used, stored or logged.

What makes an identity (`verify_id_token`):

* The signature, against Microsoft's published keys (`_JWKS`), RS256 only, by a key published for the
  token's issuer; `aud` is our client id;
  `exp`/`iat`/`nbf` present and honoured with a 60s leeway; `iss` is exactly the v2 issuer of the token's
  own `tid`, and `tid` is a GUID; `nonce` equals the one this attempt minted (constant time).
* The key is `(tid, oid)`. `sub` is pairwise per app and never used.
* An email counts only when Microsoft vouches for it (the nOAuth defence): the `email` claim AND either a
  personal account (`tid` is the consumer tenant) or `xms_edov` (the tenant verified the email's domain).
  `preferred_username` and `upn` never count. No vouched email → no emails → `resolve_login` refuses.

Every failure raises `MicrosoftOAuthError`; its `code` is safe to log, its text and the token are not.
"""
from __future__ import annotations

import http.client
import logging
from typing import Any, Optional
from uuid import UUID

import jwt

from app import oauth2_wire
from app.entity_shape import optional_uuid
from app.login_oauth import VerifiedEmail, VerifiedIdentity
from app.oauth2_wire import OAuth2Error, TokenGrant
from app.oauth_flow_cookie import same_state

log = logging.getLogger("app.microsoft_oauth")

AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
SCOPE = "openid profile email"

# The tenant every personal Microsoft account's token names. Microsoft owns the address it signs for.
PERSONAL_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"

_LEEWAY_SECONDS = 60
_REQUIRED_CLAIMS = ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "nonce"]

# One process-wide key cache: the keys rotate rarely, and `PyJWKClient` refetches on an unknown `kid`.
_JWKS = jwt.PyJWKClient(JWKS_URL, cache_keys=True, timeout=10)


class MicrosoftOAuthError(OAuth2Error):
    """A sign-in Microsoft refused, or a token that did not verify. Log `code`, never the text."""


def canonical_client_id(raw: str) -> Optional[str]:
    """The Entra app's client id as `aud` carries it, or None when `raw` is not one. Anything that is not a
    GUID (a bad paste, a placeholder, the secret in the wrong field) reads as unusable, so a misconfiguration
    is a 503 on `/start`, never a redirect to an Entra error page."""
    value = raw.strip()
    parsed = optional_uuid(value)
    # The dashed 8-4-4-4-12 spelling only, returned lower-case: Entra's `aud` claim is, and it is compared exactly.
    return str(parsed) if parsed is not None and str(parsed) == value.lower() else None


def authorize_url(*, client_id: str, redirect_uri: str, state: str, challenge: str, nonce: str) -> str:
    return oauth2_wire.authorize_url(
        endpoint=AUTHORIZE_URL,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        challenge=challenge,
        scope=SCOPE,
        # `select_account`: a browser signed in to several Microsoft accounts must choose, never be guessed.
        extra={"response_mode": "query", "nonce": nonce, "prompt": "select_account"},
    )


def exchange_code(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str
) -> TokenGrant:
    return oauth2_wire.exchange_code(
        endpoint=TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
        vendor="Microsoft",
        error_cls=MicrosoftOAuthError,
        send_grant_type=True,
    )


def _guid(value: Any) -> Optional[UUID]:
    """A GUID claim in the dashed 8-4-4-4-12 spelling (either case), or None."""
    if not isinstance(value, str):
        return None
    parsed = optional_uuid(value)
    return parsed if parsed is not None and str(parsed) == value.lower() else None


def _domain_owner_verified(value: Any) -> bool:
    """`xms_edov` as Entra spells "true": a JSON boolean, or 1 / "1" / "true" in some token versions."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in ("1", "true")


def _vouched_email(claims: dict[str, Any], tenant_id: str) -> Optional[str]:
    """The `email` claim, lower-cased, only when Microsoft vouches for it — never `preferred_username`/`upn`."""
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email.strip():
        return None
    if tenant_id == PERSONAL_TENANT_ID or _domain_owner_verified(claims.get("xms_edov")):
        return email.strip().lower()
    return None


def verify_id_token(id_token: str, *, client_id: str, nonce: str) -> VerifiedIdentity:
    """The identity an ID token proves, or `MicrosoftOAuthError`. See this module's header for the rules."""
    try:
        signing_key = _JWKS.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            leeway=_LEEWAY_SECONDS,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWKClientConnectionError as exc:
        log.warning("microsoft-login: signing keys unavailable (%s)", type(exc).__name__)
        raise MicrosoftOAuthError("Microsoft's signing keys could not be read", code="jwks_unavailable") from exc
    except (ValueError, OSError, http.client.HTTPException) as exc:
        # A JWKS body or transport failure `PyJWKClient` does not wrap (non-JSON body, a reset connection).
        log.warning("microsoft-login: signing keys unavailable (%s)", type(exc).__name__)
        raise MicrosoftOAuthError("Microsoft's signing keys could not be read", code="jwks_unavailable") from exc
    except jwt.PyJWTError as exc:  # an unknown `kid` (`PyJWKClientError`) included
        # The class name only: PyJWT's message can quote claim values.
        log.warning("microsoft-login: id token refused (%s)", type(exc).__name__)
        raise MicrosoftOAuthError("The Microsoft ID token did not verify", code="id_token_invalid") from exc

    raw_tid = claims.get("tid")
    tenant_id = _guid(raw_tid)
    object_id = _guid(claims.get("oid"))
    if tenant_id is None or object_id is None:
        log.warning("microsoft-login: id token has no usable tid/oid")
        raise MicrosoftOAuthError("The Microsoft ID token names no account", code="id_token_invalid")
    if claims.get("iss") != f"https://login.microsoftonline.com/{raw_tid}/v2.0":
        log.warning("microsoft-login: id token issuer does not match its tenant")
        raise MicrosoftOAuthError("The Microsoft ID token has the wrong issuer", code="id_token_invalid")
    # Microsoft's multi-tenant rule: a key published with an `issuer` signs only for that issuer.
    key_issuer = getattr(signing_key, "_jwk_data", {}).get("issuer")
    if isinstance(key_issuer, str) and key_issuer.replace("{tenantid}", raw_tid) != claims["iss"]:
        log.warning("microsoft-login: signing key is not published for the token's issuer")
        raise MicrosoftOAuthError("The Microsoft ID token has the wrong issuer", code="id_token_invalid")
    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not same_state(nonce, token_nonce):
        log.warning("microsoft-login: id token nonce does not match the attempt")
        raise MicrosoftOAuthError("The Microsoft ID token is for another attempt", code="nonce_mismatch")

    email = _vouched_email(claims, str(tenant_id))
    return VerifiedIdentity(
        provider="microsoft",
        subject=(tenant_id, object_id),
        emails=[VerifiedEmail(email_lower=email, primary=True)] if email else [],
    )
