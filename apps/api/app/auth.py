"""Session auth + role helpers.

The cookie value is a signed, timestamped user id (itsdangerous). All /api/*
routes are guarded by SessionAuthMiddleware except the public set (login,
logout, health, /api/public/*).
"""
from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import NamedTuple, Optional
from uuid import UUID

import bcrypt
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import instance_dict
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.routing import Match
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import act_as, api_keys, edition, edition_features, hipaa, node_credentials, request_caller
from app.actors import set_request_actor
# The session clock, in epoch seconds, under this module's own name so a test moves minting and verifying alike.
from app.clock import epoch as _now
from app.db import IdentitySessionLocal, SessionLocal, get_session
from app.dev_bypass import is_local_env
from app.entity_shape import optional_uuid
from app.models.identity import AdminUser
from app.models.orgs import ORG_ACTIVE, Org
from app.tenancy import seam

# Env-overridable so two local stacks on one host can each hold their own session.
COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "session")
# The absolute cap: a session dies this long after the sign-in that minted it, however active (D8).
SESSION_MAX_AGE = 30 * 86400  # seconds
# The sliding idle expiry: a cookie not re-minted within this window is refused. Re-minting is
# `app/login_funnel.py::SessionRefreshMiddleware`'s, at most once per `SESSION_REFRESH_AFTER`.
SESSION_IDLE_TIMEOUT = 7 * 86400  # seconds
SESSION_REFRESH_AFTER = 15 * 60  # seconds
# K5: the 401 detail of a session its org's HIPAA idle rule ended (decided by `session_idled_out`).
SESSION_IDLE_TIMEOUT_DETAIL = "session_idle_timeout"
# Authentication-method references a cookie may carry (`SessionClaims.amr`).
AMR_VALUES = frozenset({"pwd", "oauth", "sso", "otp", "recovery", "device", "skip"})
# The `amr` values that prove a second factor (PRD D7): a cookie carrying none of them is not a session,
# which is what retires every cookie minted before the console demanded one (`read_session`). `device` is a
# trusted device standing in for a code it once saw, `skip` an org the operator exempted (`app/mfa.py`).
SECOND_FACTOR_AMR = frozenset({"otp", "recovery", "sso", "device", "skip"})

PRESENCE_WRITE_THROTTLE = timedelta(seconds=60)

PUBLIC_PATHS = {
    "/api/auth/login", "/api/auth/google", "/api/auth/logout", "/api/health",
    # Console sign-in with GitHub and Microsoft (`routes/login_oauth.py`), exact paths so no sibling rides along.
    "/api/auth/oauth/github/start", "/api/auth/oauth/github/callback",
    "/api/auth/oauth/microsoft/start", "/api/auth/oauth/microsoft/callback",
    # The self-service sign-up (`routes/signup.py`): a visitor with no session yet.
    "/api/auth/signup/google", "/api/auth/signup/pending", "/api/auth/signup/complete", "/api/auth/signup/plans",
    "/api/auth/signup/email/start", "/api/auth/signup/email/resend", "/api/auth/signup/email/verify",
    "/api/auth/signup/email/verify-link", "/api/auth/signup/email/password",
    # The multi-step sign-up's draft doors (`app/signup_draft.py`): the signup cookie is their credential.
    "/api/auth/signup/profile", "/api/auth/signup/plan", "/api/auth/signup/billing", "/api/auth/signup/setup-intent",
    # The second factor (`routes/mfa.py`): authenticated by the pending cookie, not a session.
    "/api/auth/mfa/pending", "/api/auth/mfa/verify", "/api/auth/mfa/enrol/start", "/api/auth/mfa/enrol/confirm",
    # Forgot and reset password (`routes/password_reset.py`): the emailed link is the only credential.
    "/api/auth/password/forgot", "/api/auth/password/reset/check", "/api/auth/password/reset",
}
# The console's signed-out probe: a read carrying this header and NO cookie for what it reads answers 204, not
# 401 — `GET /api/auth/me` here (no session cookie) and `GET /api/auth/signup/pending` (no signup cookie,
# `routes/signup.py`). A first visit to `/login` or `/signup` then logs no failed request. On `/me` a
# session cookie that is present but dead (forged, expired, or ended by a sign-out elsewhere) gets the same 204 and is
# cleared, so the next visit sends none: the console treats both answers alike (signed out). A HIPAA idle
# logoff keeps its 401, whose detail the console turns into the idle notice.
SIGNED_OUT_PROBE_HEADER = "X-Jaah-Signed-Out-Ok"
SIGNED_OUT_PROBE_PATH = "/api/auth/me"

# The Terms gate (PRD D7): a session carrying `terms: "pending"` (`login_funnel._mint`, `signup.terms_pending`) is
# refused 403 `terms_required` on every `/api` path but these — the acceptance itself (`routes/legal_acceptance.py`),
# sign-out, and the console's own identity read.
TERMS_PENDING = "pending"
# `ip_allowlist.refused_response`'s shape: the web client reads `code` (`apps/web/src/api.ts::signalTermsRequired`).
TERMS_REQUIRED = MappingProxyType({"detail": "Accept the Terms of Service to continue.", "code": "terms_required"})
TERMS_PENDING_PATHS = frozenset({"/api/auth/terms", "/api/auth/terms/accept", "/api/auth/logout", SIGNED_OUT_PROBE_PATH})


def signed_out_headers(clear_session: bool = False) -> dict[str, str]:
    """The probe's 204 headers: never cached (a cache serving it to a signed-in browser would sign that browser
    out), plus the dead session cookie's deletion when `clear_session`."""
    headers = {"Cache-Control": "no-store"}
    if clear_session:
        scratch = Response()
        clear_session_cookie(scratch)
        headers["Set-Cookie"] = scratch.headers["set-cookie"]
    return headers


def signed_out_answer(clear_session: bool = False) -> Response:
    """The probe's 204 (`signed_out_headers`)."""
    return Response(status_code=204, headers=signed_out_headers(clear_session))


def is_signed_out_probe(request: Request, cookie: str) -> bool:
    """A GET asking for the 204 answer (`SIGNED_OUT_PROBE_HEADER`) that carries no `cookie` at all."""
    return (
        request.method == "GET"
        and request.headers.get(SIGNED_OUT_PROBE_HEADER) == "1"
        and not request.cookies.get(cookie)
    )


def is_session_probe(request: Request) -> bool:
    """A GET of `/me` asking for the 204 answer, whatever session cookie it carries: a dead one is cleared."""
    return request.method == "GET" and request.headers.get(SIGNED_OUT_PROBE_HEADER) == "1"


# `/api/node-bootstrap` is exact-matched below, not prefixed, so `/api/node-bootstrapXYZ` cannot pass.
PUBLIC_PREFIXES = ("/api/public/", "/api/agent/", "/api/auth/sso/")
# The community edition's cookieless doors — its first run (`routes/ce_setup.py`, before its one user exists) and
# its attachment volume (`routes/ce_storage.py`, the signed URL is the credential). Waved past only when
# `edition.IS_CE`: the cloud admits neither without a session, and both answer 404 there.
CE_PUBLIC_PATHS = frozenset({"/api/ce/setup"})
CE_PUBLIC_PREFIXES = ("/api/ce/storage/",)

# The one machine-surface path outside `/api/agent/`. Authenticated by a node token in
# `routes/agent_bootstrap.py`, so a session check here would refuse every real caller. Internet-reachable
# by design (`app/node_bootstrap.py::BOOTSTRAP_PATH`). Adding an entry is an auth change: the public/private
# ALB split test fails a member not denied at, or excepted by, the public ALB.
SESSIONLESS_NODE_TOKEN_PATHS = frozenset({"/api/node-bootstrap"})

# Session-gated paths a valid X-Agent-Token may also reach, mapped to the only methods it may use there
# (exact paths, per verb). GET /api/projects serves jaahd's cookie-less project resolvers; POST stays
# session-only, since it would be the one internet-reachable write a leaked node token could make.
NODE_TOKEN_API_PATHS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "/api/projects": frozenset({"GET"}),
    }
)


def _touch_last_seen(session: Session, user: AdminUser) -> None:
    """Presence, throttled — and the account's first use stamps `activated_at`, which is what moves it off
    "invited" on People (`routes/users.py::_invitation_status`)."""
    now = datetime.now(timezone.utc)
    last = user.last_seen_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is not None and (now - last) < PRESENCE_WRITE_THROTTLE and user.activated_at is not None:
        return
    try:
        user.last_seen_at = now
        if user.activated_at is None:
            user.activated_at = now
        session.commit()
    except Exception:
        session.rollback()


# The session secret this repository once published for local stacks. Refused everywhere, local stacks
# included: a key that is in git lets anyone mint a cookie for any admin of a stack that signs with it.
_DEV_SESSION_SECRET = "dev-session-secret-change-me"


def resolve_session_secret() -> str:
    """The session-signing secret, or a refusal to start.

    An unset SESSION_SECRET and one equal to the published `_DEV_SESSION_SECRET` are the same input, refused in
    every environment: `./dc` generates a real one into a local checkout's `.env.local` (`dc_ensure_local_secrets`)."""
    # The community edition's generated secret, in every process that reads one (a no-op in the cloud).
    from app import ce_bootstrap

    ce_bootstrap.ensure_secrets()
    secret = os.environ.get("SESSION_SECRET", "")
    # Bytes, never str: `hmac.compare_digest` raises TypeError on a non-ASCII str.
    if secret and not hmac.compare_digest(secret.encode(), _DEV_SESSION_SECRET.encode()):
        return secret
    cause = "is set to the published dev constant from app/auth.py" if secret else "is unset"
    raise RuntimeError(
        f"SESSION_SECRET must be set to a real value — it {cause}, and a signing key that is in git lets "
        "anyone mint a cookie for any admin. A local stack: run it through ./dc, which generates one into "
        ".env.local. Anywhere else: `openssl rand -hex 32`."
    )


def google_client_id() -> str | None:
    """The Google Sign-In OAuth Client ID for this environment, or None when unset.

    Read at call time. Two readers must agree: POST /api/auth/google uses it as the expected audience,
    and GET /api/public/settings hands it to the browser for the GSI button. Not a secret (it ships in the
    page); verification is against Google's public JWKS.

    Unset is fail-closed: no button, and POST /api/auth/google 503s. Anything not ending in
    `.apps.googleusercontent.com` (a bad paste, a placeholder, the client secret) reads as unset, rather
    than rendering a button wired to a bogus audience."""
    value = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    return value if value.endswith(".apps.googleusercontent.com") else None


class _ClockedSigner(TimestampSigner):
    """itsdangerous' timestamp signer reading `_now`, so the idle window is testable with a clock."""

    def get_timestamp(self) -> int:
        return int(_now())


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(resolve_session_secret(), salt="cockpit-session", signer=_ClockedSigner)


class SessionClaims(NamedTuple):
    """A verified session cookie (v2). `iat` is the sign-in that started the session, kept across re-mints
    so the absolute cap holds; `issued_at` is when THIS cookie was signed, which the idle window measures.
    `epoch` is the membership row's `session_epoch`, `iepoch` the identity's (`session_row_is_live`); a
    cookie minted before the identity epoch existed carries none and reads 0. `terms` is `TERMS_PENDING` while the
    person still owes the Terms acceptance (the gate in `SessionAuthMiddleware`), else None; every re-mint carries
    it, and only the acceptance drops it."""

    admin_id: UUID
    epoch: int
    iat: int
    amr: tuple[str, ...]
    issued_at: int
    iepoch: int = 0
    terms: str | None = None


def create_session_token(
    admin_id: UUID,
    epoch: int = 0,
    *,
    iepoch: int = 0,
    iat: int | None = None,
    amr: tuple[str, ...] | list[str] = (),
    terms: str | None = None,
) -> str:
    """A signed v2 session payload `{admin_id, epoch, iepoch, iat, amr}`, plus `terms` when the Terms are owed
    (`SessionClaims.terms`). `iat` defaults to now (a new session)."""
    unknown = set(amr) - AMR_VALUES
    if unknown:
        raise ValueError(f"unknown amr value(s): {sorted(unknown)}")
    if terms not in (None, TERMS_PENDING):
        raise ValueError(f"unknown terms value: {terms!r}")
    # `str(admin_id)`: itsdangerous serializes JSON, and a UUID is not JSON-serializable.
    payload: dict = {
        "admin_id": str(admin_id),
        "epoch": epoch,
        "iepoch": iepoch,
        "iat": int(_now()) if iat is None else int(iat),
        "amr": sorted(set(amr)),
    }
    if terms is not None:
        payload["terms"] = terms
    return _serializer().dumps(payload)


def read_session(token: str | None) -> SessionClaims | None:
    """Decode a session cookie into its claims, or None when it is not a live session.

    Refused: a bad signature; a cookie signed more than `SESSION_IDLE_TIMEOUT` ago (idle); a session whose
    `iat` is more than `SESSION_MAX_AGE` ago (the absolute cap, whatever re-minting happened since); a
    cookie whose `amr` proves no second factor (`SECOND_FACTOR_AMR`) — every v1 cookie (its `amr` is
    empty) and every v2 one minted before the console demanded a second factor.

    The fail-closed admin-id check is the most important line here: an admin id is only a string that
    parses as a uuid. A legacy integer id must never be coerced or looked up, because revision 0105
    derived the new ids from the old integers (`_derive` there), so any coercion could hand one
    operator's session another's row. Any non-uuid string is rejected (`UUID(...)` raises `ValueError`)."""
    if not token:
        return None
    try:
        payload, signed_at = _serializer().loads(
            token, max_age=SESSION_IDLE_TIMEOUT, return_timestamp=True
        )
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    raw_admin_id = payload.get("admin_id")
    if not isinstance(raw_admin_id, str):
        return None
    admin_id = optional_uuid(raw_admin_id)
    if admin_id is None:
        return None
    issued_at = int(signed_at.timestamp())
    raw_iat = payload.get("iat")
    if raw_iat is None:
        iat = issued_at
    elif isinstance(raw_iat, int) and not isinstance(raw_iat, bool) and raw_iat <= issued_at:
        iat = raw_iat
    else:
        # A session cannot start after the cookie carrying it was signed.
        return None
    if _now() - iat > SESSION_MAX_AGE:
        return None
    raw_amr = payload.get("amr", [])
    if not isinstance(raw_amr, list) or not all(isinstance(v, str) and v in AMR_VALUES for v in raw_amr):
        return None
    if not SECOND_FACTOR_AMR.intersection(raw_amr):
        return None
    epoch = payload.get("epoch")
    iepoch = payload.get("iepoch")
    # Fails closed: a signed `terms` this release does not know still gates (the signature proves we wrote it).
    raw_terms = payload.get("terms")
    return SessionClaims(
        admin_id=admin_id,
        epoch=epoch if isinstance(epoch, int) else 0,
        iat=iat,
        amr=tuple(raw_amr),
        issued_at=issued_at,
        # Absent on every cookie minted before revision 0360: 0, the backfilled identity epoch, so it lives.
        iepoch=iepoch if isinstance(iepoch, int) and not isinstance(iepoch, bool) else 0,
        terms=None if raw_terms is None else TERMS_PENDING,
    )


def verify_session(token: str | None) -> tuple[UUID, int] | None:
    """`(admin_id, epoch)` of a live session cookie, or None (`read_session` decides)."""
    claims = read_session(token)
    return (claims.admin_id, claims.epoch) if claims is not None else None


def verify_session_token(token: str | None) -> UUID | None:
    result = verify_session(token)
    return result[0] if result is not None else None


def session_cookie_is_secure() -> bool:
    """Whether `set_session_cookie` will set `Secure`, inferred from `FRONTEND_URL`. Shared with the
    boot-time posture check so the two cannot drift."""
    return os.environ.get("FRONTEND_URL", "").startswith("https://")


def warn_if_session_cookie_is_not_secure() -> None:
    """Log at ERROR when a non-local stack is about to serve session cookies without `Secure`.

    A mistyped or http `FRONTEND_URL` silently drops `Secure` on a public origin. A warning, not a
    refusal, by owner decision: the worst case needs an attacker on the network path, and taking the API
    down over a display-URL typo is likelier. A local stack and the community edition are exempt (`http://localhost`
    is correct there)."""
    if is_local_env() or edition.IS_CE or session_cookie_is_secure():
        return
    logging.getLogger(__name__).error(
        "SESSION COOKIE IS NOT MARKED Secure: FRONTEND_URL is %r, which does not start with "
        "'https://', and APP_ENV is %r (not 'local'). Session cookies will be sent over plaintext "
        "HTTP. Set FRONTEND_URL to this deployment's https:// origin.",
        os.environ.get("FRONTEND_URL") or "<unset>",
        os.environ.get("APP_ENV") or "<unset>",
    )


def set_session_cookie(
    response,
    admin_id: UUID,
    epoch: int = 0,
    *,
    iepoch: int,
    iat: int | None = None,
    amr: tuple[str, ...] | list[str] = (),
    terms: str | None = None,
) -> None:
    """Write the session cookie. Called only by `app/login_funnel.py` (`tests/test_login_funnel.py` pins it):
    a sign-in reaches a cookie through the funnel's checks, never around them. `iepoch` is required so a
    mint that forgets the identity epoch fails loudly rather than minting a cookie a sign-out cannot end.

    `samesite="lax"`, not `strict`: the Connect GitHub flow ends in a top-level GET navigation
    back to `/api/git-connect/callback`, which carries no cookie under `strict`. The alternatives (a second
    auth path on the credential-minting route, or relaying the code through the SPA) were worse.

    Cost: `lax` differs only in that a cross-site top-level GET carries the cookie. Mutating verbs are
    still withheld, and the originating page cannot read a navigated GET's response (no CORS).

    Precondition: every cookie-authenticated GET is read-only or carries its own unguessable per-attempt
    binding. The callback is the one mutating GET and is bound by a signed, short-lived flow cookie whose
    `state` and `user_id` are checked before minting. Enforced by
    `tests/test_git_access.py::test_no_cookie_authenticated_get_mutates_state` (holds the allowlist)."""
    secure = session_cookie_is_secure()
    response.set_cookie(
        COOKIE_NAME,
        create_session_token(admin_id, epoch, iepoch=iepoch, iat=iat, amr=amr, terms=terms),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


#: bcrypt reads at most 72 bytes of a password. bcrypt<5 truncated past that silently; bcrypt>=5 raises
#: ValueError instead, which would refuse every stored long-password login and 500 every long-password set.
#: Truncating here keeps the exact bytes every existing hash was sealed over.
_BCRYPT_MAX_BYTES = 72


def _bcrypt_input(password: str) -> bytes:
    return password.encode()[:_BCRYPT_MAX_BYTES]


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_bcrypt_input(password), password_hash.encode())
    except ValueError:
        return False


def hash_password(password: str) -> str:
    rounds = int(os.environ.get("BCRYPT_ROUNDS", "12"))
    return bcrypt.hashpw(_bcrypt_input(password), bcrypt.gensalt(rounds=rounds)).decode()


#: The password rule, single-homed: every door that sets a password (`PUT /api/auth/me`, the email sign-up,
#: the password reset) asks `password_problem`. The ceiling is a sanity bound, not bcrypt's: past 72 bytes
#: `_bcrypt_input` still truncates, as it always has.
PASSWORD_MIN = 8
PASSWORD_MAX = 128
PASSWORD_TOO_SHORT = "password_too_short"
PASSWORD_TOO_LONG = "password_too_long"
PASSWORD_IS_EMAIL = "password_is_email"
#: Each refusal as a sentence a person reads.
PASSWORD_PROBLEM_MESSAGES = MappingProxyType({
    PASSWORD_TOO_SHORT: f"Use at least {PASSWORD_MIN} characters.",
    PASSWORD_TOO_LONG: f"Use at most {PASSWORD_MAX} characters.",
    PASSWORD_IS_EMAIL: "Don't use your email address as your password.",
})


def password_problem(password: str, email: Optional[str]) -> Optional[str]:
    """Why `password` may not be set — one of the `PASSWORD_*` codes — or None when it may.

    Length counts characters (what the form counts), 8 to 128. Equal to the account's email, ignoring case
    and surrounding spaces, is refused."""
    if len(password) < PASSWORD_MIN:
        return PASSWORD_TOO_SHORT
    if len(password) > PASSWORD_MAX:
        return PASSWORD_TOO_LONG
    if email and password.strip().casefold() == email.strip().casefold():
        return PASSWORD_IS_EMAIL
    return None


#: `password_refusal`'s two answers: the role rule's (PRD D7) and an operator's explicit shut door.
SUPERADMIN_PASSWORD = "superadmin_password"
PASSWORD_OFF = "password_off"


def password_refusal(user: AdminUser) -> Optional[str]:
    """Why `user` has no password door, or None when it has one — the ONE rule every password surface reads
    (sign-in, membership choice, org switch, reset). An operator's `AdminUser.password_login` wins; unset, a
    superadmin has none (PRD D7) and everyone else has one."""
    if user.password_login is True:
        return None
    if user.password_login is False:
        return PASSWORD_OFF
    return SUPERADMIN_PASSWORD if user.is_superadmin else None


def password_reset_eligible(session: Session, user: AdminUser) -> bool:
    """Whether `user` may reset (or first set) a password by email.

    Never the system actor, a row with no password door (`password_refusal`) or a disabled user; and only in
    an approved (`active`) org that is neither SSO-only nor Require-SSO, since such an org's members sign in
    through their IdP alone (`app/sso_policy.py`, `app/org_sso.py`). The one rule both the account-exists email
    and the forgot door read. An edition without single sign-on has no Require-SSO org."""
    if user.is_system or password_refusal(user) is not None or user.disabled_at is not None:
        return False
    org = session.get(Org, user.org_id)
    if org is None or org.lifecycle != ORG_ACTIVE or org.sso_provider is not None:
        return False
    if not edition_features.enabled("sso"):
        return True
    from app import org_sso

    return not org_sso.enforced(session, user.org_id)


class SecurityHeadersMiddleware:
    """Pure ASGI, not `BaseHTTPMiddleware`: that one wraps every request in a `call_next` task and a body-stream
    relay, loop CPU each node poll and every SSE chunk pays. The headers are `setdefault`ed onto
    `http.response.start`, so a route's own value (an attachment's CSP) still wins. Non-http scopes pass
    untouched, as `BaseHTTPMiddleware` passes them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])  # optional in ASGI; MutableHeaders needs the list
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class NodeAuthUnavailable(RuntimeError):
    """The X-Agent-Token could not be checked (a database fault): answered 503, never 401. A 401 tells a
    node its token is revoked, and a node that hears only 401s for long enough stops itself for good
    (`apps/jaahd/token_revocation.py`), so a DB outage must not read as a revocation."""


NODE_AUTH_UNAVAILABLE_DETAIL = "node authentication unavailable"


def _valid_node_token(request: Request) -> bool:
    """Fail-closed check of the request's X-Agent-Token, for the `NODE_TOKEN_API_PATHS` carve-out.

    The rule is `app/node_credentials.py::resolve`'s; this wrapper exists because the middleware needs a
    boolean and runs outside the dependency graph.

    Opens its own short-lived session (middleware has nothing to inherit), on the identity lane
    (`app/db.py`) so a starved request pool cannot refuse a node. Synchronous: the caller runs it via
    `run_in_threadpool` so the DB round trip does not block the event loop.

    A database fault refuses the caller (fails closed) by raising `NodeAuthUnavailable` — a 503, not the
    401 a bad token earns: an unproven caller must not get through, unlike
    `app/routes/node_bus.py::_identity`, where the fact at stake is cosmetic."""
    presented = request.headers.get("X-Agent-Token", "")
    try:
        with IdentitySessionLocal() as session:
            return node_credentials.resolve(session, presented) is not None
    except Exception as exc:  # noqa: BLE001 — see the docstring: an unprovable caller is refused
        logging.getLogger(__name__).warning(
            "node-auth: could not resolve an X-Agent-Token on %s (%s) — refusing with 503",
            request.url.path,
            exc,
        )
        raise NodeAuthUnavailable(str(exc)) from exc


def _ce_public(path: str) -> bool:
    """Is `path` one of the community edition's cookieless doors, in the community edition (`CE_PUBLIC_PATHS`)?"""
    return edition.IS_CE and (path in CE_PUBLIC_PATHS or path.startswith(CE_PUBLIC_PREFIXES))


def _answered_not_in_ce(request: Request) -> bool:
    """Would the community edition's removed-route answer (`app/main.py::_not_in_ce`) serve this request? The first
    route that fully matches decides, as the router's own walk does, so a shared route under a removed prefix
    (`/api/users/{user_id}/git-connections`) still needs its session. That answer is a fixed 404 naming Jaah Cloud
    and reads nothing, so a signed-out caller gets it too, rather than a 401 that hides why (AC-12)."""
    if not edition.IS_CE:
        return False
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match is Match.FULL:
            return getattr(getattr(route, "endpoint", None), "answers_not_in_ce", False)
    return False


class SessionAuthMiddleware:
    """401 for every non-public /api route, except the NODE_TOKEN_API_PATHS carve-out below.

    Pure ASGI, for `SecurityHeadersMiddleware`'s reason: it decides before the app runs and never reads the
    response. `request.state` writes land in `scope["state"]`, which every downstream `request.state` reads.
    A WebSocket scope passes untouched, as it did under `BaseHTTPMiddleware` (`authz` verifies its cookie)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response = await self._refusal(Request(scope, receive))
        if response is not None:
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _refusal(self, request: Request) -> Response | None:
        """The early answer this request gets instead of the app, or None to let it through (identity set)."""
        path = request.url.path
        if (
            path.startswith("/api")
            and path not in PUBLIC_PATHS
            and not any(path.startswith(p) for p in PUBLIC_PREFIXES)
            and path not in SESSIONLESS_NODE_TOKEN_PATHS
            and not _ce_public(path)
        ):
            claims = read_session(request.cookies.get(COOKIE_NAME))
            admin_id: UUID | None = claims.admin_id if claims is not None else None
            if claims is None and path == SIGNED_OUT_PROBE_PATH and is_session_probe(request):
                return signed_out_answer(clear_session=COOKIE_NAME in request.cookies)
            presented_key = api_keys.bearer(request) if COOKIE_NAME not in request.cookies else None
            if presented_key is not None:
                # A Jaah API key (`app/api_keys.py`): only with no session cookie at all — a cookie always wins.
                host = request.client.host if request.client else None
                if not api_keys.parse(presented_key):
                    return JSONResponse({"detail": api_keys.INVALID_DETAIL}, status_code=401)
                verdict = await run_in_threadpool(api_keys.verify, presented_key, host)
                if isinstance(verdict, api_keys.Refusal):
                    await run_in_threadpool(api_keys.record_refusal, verdict, client_ip=host)
                    return api_keys.refusal_response(verdict, host)
                request.state.admin_id = verdict.owner_id
                request.state.api_key = verdict
            elif claims is None:
                # No valid session. A daemon call with its own X-Agent-Token passes only for the carve-out
                # paths and methods, so the route's `require_user_or_agent` can run. Everything else stays 401.
                try:
                    agent_ok = request.method in NODE_TOKEN_API_PATHS.get(path, ()) and await run_in_threadpool(
                        _valid_node_token, request
                    )
                except NodeAuthUnavailable:
                    return JSONResponse({"detail": NODE_AUTH_UNAVAILABLE_DETAIL}, status_code=503)
                if agent_ok:
                    request.state.agent_authenticated = True
                elif not _answered_not_in_ce(request):
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
            else:
                # The Terms gate first (PRD D7): until accepted, the session reaches only the acceptance doors.
                if claims.terms is not None and path not in TERMS_PENDING_PATHS:
                    return JSONResponse(dict(TERMS_REQUIRED), status_code=403)
                # The org's IP allowlist (`app/ip_allowlist.py`), on every cookie session. Node and runner
                # traffic carries no cookie and never reaches it (D-F5); an API key meets it in `api_keys.verify`.
                from app import ip_allowlist

                host = request.client.host if request.client else None
                refused = await run_in_threadpool(ip_allowlist.session_refusal, admin_id, host)
                if refused is not None:
                    return ip_allowlist.refused_response(host)
                request.state.admin_id = admin_id
                # Which sign-in this is, for every activity row the request writes (`app/request_caller.py`).
                request.state.console_session = request_caller.console_session_id(claims.admin_id, claims.iat)
                # Resolved now, while the turn that sent it still holds it (`app/support_agent/assist.py`).
                if edition_features.enabled("support_assistant"):
                    from app.support_agent import assist

                    request.state.assist = assist.resolve_request(request)
        return None


def identity_epoch(user: AdminUser) -> int:
    """The identity epoch of `user`'s person (`UserIdentity.session_epoch`). Fails CLOSED: -1, which no
    cookie carries, when the row names a person that cannot be read here (detached, never loaded)."""
    identity = user._loaded_identity()
    if identity is not None:
        return identity.session_epoch or 0
    return -1 if instance_dict(user).get("identity_id") is not None else 0


def session_row_is_live(user: AdminUser | None, token_epoch: int, token_iepoch: int) -> bool:
    """Whether a verified cookie's row still holds a session: it exists, is not disabled, the cookie's
    `epoch` is the row's current one AND its `iepoch` is the identity's. Two epochs: disabling one
    membership bumps only its row (the person's other orgs keep their sessions), while sign-out and a
    password change bump the identity (every membership, every device). The one predicate every cookie
    check (REST, principal, WebSocket) uses."""
    return (
        user is not None
        and user.disabled_at is None
        and (user.session_epoch or 0) == token_epoch
        and identity_epoch(user) == token_iepoch
    )


def session_idled_out(conn: HTTPConnection | None, session: Session, user: AdminUser, issued_at: int) -> bool:
    """Whether a live row's session has ended for inactivity under its org's HIPAA rule (C5): the row's org
    — the session's CURRENT org, so an org switch takes the new org's rule — is HIPAA ON and the cookie was
    signed more than `hipaa.IDLE_TIMEOUT` ago. An org that is OFF keeps `SESSION_IDLE_TIMEOUT`, which
    `read_session` already enforces.

    A cookie younger than `hipaa.REFRESH_AFTER` costs no read: nothing about it can be due. Otherwise the
    verdict is noted on `conn.state.session_hipaa`, where `login_funnel.SessionRefreshMiddleware` reads the
    org's refresh interval without a second lookup — and where a second check in the same request (the
    authz guard, then `require_user`) reuses it rather than reading the org again."""
    age = _now() - issued_at
    if age < hipaa.REFRESH_AFTER.total_seconds():
        return False
    noted = getattr(conn.state, "session_hipaa_org", None) if conn is not None else None
    if noted is not None and noted[0] == user.org_id:
        hipaa_on = noted[1]
    else:
        hipaa_on = hipaa.org_is_on(session, user.org_id)
    if conn is not None:
        conn.state.session_hipaa = hipaa_on
        conn.state.session_hipaa_org = (user.org_id, hipaa_on)
    return hipaa_on and hipaa.idled_out(age)


def _session_user(request: Request, session: Session) -> AdminUser | None:
    """Resolve the AdminUser for the request's session cookie, or None (absent/bad cookie, unknown user, or
    stale session_epoch). The one home both `require_user` and `require_user_or_agent` use. A session its
    org's HIPAA idle rule ended raises 401 `SESSION_IDLE_TIMEOUT_DETAIL` instead (`session_idled_out`)."""
    claims = read_session(request.cookies.get(COOKIE_NAME))
    if claims is None:
        key = api_keys.of(request) if COOKIE_NAME not in request.cookies else None
        return _api_key_owner(session, key) if key is not None else None
    # On the identity lane (`app/db.py`), never the route session: a dependency runs in its own threadpool
    # call, so a connection the route session checked out here would be HELD while the request queues for
    # its next thread — and a burst of such requests holds every connection while each waits for a thread
    # whose occupant waits for a connection. The row then joins the route session without a
    # query (`merge(load=False)`), so a route that writes the caller's own row still writes it.
    with IdentitySessionLocal() as identity:
        # Inside the identity escape: this read resolves who is asking, and `admin_users` is tenant-owned. On
        # `@always(Tenancy.BOOTSTRAP)` routes no scope is bound, so a filtered lookup would 401 every valid
        # cookie (`app/tenancy/seam.py::identity_scope`). The escape closes on the row: the writes below stamp
        # nothing under `IDENTITY` or unbound alike, and `AdminUser` has no lazy relationship a route reads.
        with seam.identity_scope():
            user = identity.get(AdminUser, claims.admin_id)
        if not session_row_is_live(user, claims.epoch, claims.iepoch):
            return None
        if session_idled_out(request, identity, user, claims.issued_at):
            raise HTTPException(status_code=401, detail=SESSION_IDLE_TIMEOUT_DETAIL)
    user = session.merge(user, load=False)
    # Whose writes this session's flushes are, for `actors.stamp_dirty_task_actors`. Recorded here, the one
    # place a console session resolves to a user; agent-token requests attribute to the system actor.
    set_request_actor(session, user.id)
    # A read Jarvis made for the user is not the user being present.
    if not assisted(request):
        _touch_last_seen(session, user)
    return user


def assisted(request: Request) -> bool:
    """Whether Jarvis made this request for its user (`app/support_agent/assist.py`); never in an edition without
    the Help assistant."""
    if not edition_features.enabled("support_assistant"):
        return False
    from app.support_agent import assist

    return assist.of(request) is not None


def _api_key_owner(session: Session, key: "api_keys.ApiKeyContext") -> AdminUser | None:
    """The owner a verified API key (`SessionAuthMiddleware`) acts as, re-read on the identity lane as a cookie's row
    is; None once disabled. No presence write: a script is not the person being present."""
    with IdentitySessionLocal() as identity:
        with seam.identity_scope():
            user = identity.scalar(select(AdminUser).where(AdminUser.id == key.owner_id))
        if user is None or user.disabled_at is not None:
            return None
    user = session.merge(user, load=False)
    set_request_actor(session, user.id)
    return user


def require_user(
    request: Request, session: Session = Depends(get_session)
) -> AdminUser:
    user = _session_user(request, session)
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return _acting_seat(request, session, user)


def require_user_or_signed_out(
    request: Request, session: Session = Depends(get_session)
) -> AdminUser:
    """`require_user` for `GET /api/auth/me`, except that the console's signed-out probe whose cookie decodes but
    holds no session (ended by a sign-out elsewhere, a disabled seat) answers 204 and clears it, as the middleware
    does for a cookie that does not decode (`SIGNED_OUT_PROBE_HEADER`)."""
    user = _session_user(request, session)
    if user is None:
        if is_session_probe(request):
            raise HTTPException(status_code=204, headers=signed_out_headers(clear_session=True))
        raise HTTPException(status_code=401, detail="unauthorized")
    return _acting_seat(request, session, user)


def require_person(user: AdminUser = Depends(require_user)) -> AdminUser:
    """The signed-in person's own row even while their tab acts as an org (`act_as.person`) — for routes about
    the person, not the org: their second factor, their trusted devices."""
    return act_as.person(user)


def _acting_seat(request: Request, session: Session, user: AdminUser) -> AdminUser:
    """`user`, or — when an operator's tab acts as an org (`app/act_as.py`) — that org's Owner seat. The org
    is read on the identity lane, for `_session_user`'s reason."""
    key = api_keys.of(request)
    if key is not None:
        # The header is ignored for a key (R3): the key itself says whose seat it is (`api_keys.narrow`).
        return api_keys.key_user(user, key)
    requested = act_as.requested(request.headers)
    if requested is None:
        return user
    with IdentitySessionLocal() as identity:
        target = act_as.target_org(identity, user, requested)
    return act_as.seat_user(user, target) if target is not None else user


def require_user_or_agent(
    request: Request, session: Session = Depends(get_session)
) -> AdminUser | None:
    """Auth for the daemon's cookie-less read surface (NODE_TOKEN_API_PATHS). A valid console session
    returns the AdminUser as require_user does. Otherwise, if the middleware validated an X-Agent-Token
    (`request.state.agent_authenticated`), return None; the route must tolerate a None actor. Anything
    else is 401. Only routes in NODE_TOKEN_API_PATHS may depend on this."""
    user = _session_user(request, session)
    if user is not None:
        return _acting_seat(request, session, user)
    if getattr(request.state, "agent_authenticated", False):
        return None
    raise HTTPException(status_code=401, detail="unauthorized")


def hidden_route() -> HTTPException:
    """The refusal of a route a caller may not know exists: byte-identical to the router's answer for a path
    it does not serve (404, `Not Found`), so a refused superadmin or operator door cannot be told from a
    missing one."""
    return HTTPException(status_code=404)


def require_superadmin(user: AdminUser = Depends(require_user)) -> AdminUser:
    """Gate a route to is_superadmin users only; anyone else reads `hidden_route`."""
    if not user.is_superadmin:
        raise hidden_route()
    return user


#: What `require_operator_field` tells the caller it refuses, one per plane: the door is shared, only the
#: field is the operator's, so it is named rather than hidden. Module constants because tests import them.
#:
#: The tenant arm carries no word of `app/tenancy/copy.py`'s `OPERATOR_CONSOLE_VOCABULARY`; the fleet
#: arm says "fleet-wide" because the operator's own staff are entitled to the subject.
OPERATOR_GATE_TENANT_DETAIL = "This is set by an operator, not by your organization — ask an operator."
OPERATOR_GATE_FLEET_DETAIL = (
    "This is fleet-wide, so only an operator may see or change it — ask an operator for the change "
    "you need."
)


def require_operator(user: AdminUser = Depends(require_user)) -> AdminUser:
    """Gate a handler to the deployment's operator: `is_superadmin` and an operator-plane org
    (`app/tenancy/ids.py::is_operator_plane`).

    For routes whose subject is the fleet but whose declaration is `ORG_SCOPED` (`routes/model_prices.py`,
    `routes/port_grid.py`, `routes/cloud_nodes.py`), which `authz_guard`'s `Tenancy.OPERATOR` check never
    sees. A tenant-org superadmin must not rewrite the fleet's rate card or launch defaults. Anyone else,
    operator staff included, reads `hidden_route`: the door is not advertised."""
    from app.tenancy.ids import is_operator_plane

    if not is_operator_plane(user.org_id, user.is_superadmin):
        raise hidden_route()
    return user


def require_operator_field(user: AdminUser) -> AdminUser:
    """`require_operator` for one operator-only FIELD of a door every org admin uses (`routes/users.py`'s
    sign-in overrides): 403, since the door exists for the caller and hiding it would misreport the row.

    Two refusal sentences keyed on `is_tenant_org` (which reader, not who is admitted): the tenant arm
    names who can act and nothing of the operator's deployment (`app/tenancy/copy.py`'s rule); operator
    staff get the fleet-wide sentence."""
    from app.tenancy.ids import is_operator_plane, is_tenant_org

    if not is_operator_plane(user.org_id, user.is_superadmin):
        detail = (
            OPERATOR_GATE_TENANT_DETAIL
            if is_tenant_org(user.org_id)
            else OPERATOR_GATE_FLEET_DETAIL
        )
        raise HTTPException(status_code=403, detail=detail)
    return user


def require_operator_acting(user: AdminUser = Depends(require_user)) -> AdminUser:
    """`require_operator`, read on the person behind an act-as seat (`act_as.person`): an operator acting as an org
    passes and the handler runs as that org's seat; a tenant admin is refused as `require_operator` refuses them.
    For operator doors the console reaches with a one-request act-as on another org's row (`pool_slots.py`)."""
    require_operator(act_as.person(user))
    return user


def assert_superadmin_floor(session: Session, target: AdminUser | None = None) -> None:
    """Reject (409) a pending change that would drop the number of is_superadmin=true users to zero.

    Call before deleting or demoting exactly one superadmin, in the same transaction. It row-locks the
    superadmin set (`SELECT ... FOR UPDATE`), so two concurrent demotes under READ COMMITTED cannot both
    commit; a locked count of ≤ 1 means the change would leave zero. A disabled superadmin cannot sign
    in, so it does not count (and disabling one is itself a removal this guards); removing a `target` that
    is already disabled therefore removes nothing and is not refused."""
    if target is not None and target.disabled_at is not None:
        return
    locked = session.scalars(
        select(AdminUser.id)
        .where(AdminUser.is_superadmin.is_(True), AdminUser.disabled_at.is_(None))
        .with_for_update()
    ).all()
    if len(locked) <= 1:
        raise HTTPException(
            status_code=409,
            detail="cannot remove the last superadmin",
        )
