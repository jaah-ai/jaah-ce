"""The signed, short-lived cookie that carries one OAuth attempt from the redirect out to the callback.

It holds the CSRF `state`, the PKCE `verifier` and who started the attempt, per browser, with no row to
garbage-collect. The node-CLI wizard (`routes/credentials/`) uses a table instead because its state
names a node, and a client-held pin would be a client-chosen node.

Each flow has its own `FlowCookie` (name suffix, path, salt), so one flow's attempt cannot be read as
another's. `itsdangerous` derives a distinct key per salt, which is what makes sharing one secret across
the session cookie and both flow cookies safe.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any, Mapping, Optional, Sequence

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.auth import COOKIE_NAME as SESSION_COOKIE_NAME
from app.auth import resolve_session_secret, session_cookie_is_secure

# Ten minutes: enough to approve on a vendor's screen, and about a vendor code's own lifetime.
MAX_AGE = 600


class FlowCookie:
    """One flow's cookie: its name, its path, its signing salt, and the members it carries.

    `suffix` is appended to the session cookie's name. `path` must be the narrowest prefix both of the
    flow's doors share, since the cookie carries a user id. `members` are the payload keys, all required
    strings, so `read` can refuse a forged or truncated payload. `optional` members must be present too
    but may be the empty string (a fact the provider did not give, such as a display name). `max_age` is
    the attempt's lifetime in seconds, both the cookie's and the signature's; `MAX_AGE` unless the flow
    outlasts a vendor round trip.
    """

    def __init__(
        self,
        *,
        suffix: str,
        path: str,
        salt: str,
        members: Sequence[str],
        optional: Sequence[str] = (),
        max_age: int = MAX_AGE,
    ) -> None:
        self.name = f"{SESSION_COOKIE_NAME}{suffix}"
        self.max_age = max_age
        self.path = path
        self.salt = salt
        self.members = tuple(members)
        self.optional = frozenset(optional)
        if not self.optional <= set(self.members):
            raise ValueError(f"flow cookie {self.name!r}: optional members must be members")

    def _serializer(self) -> URLSafeTimedSerializer:
        """The signer, built per call (as `auth._serializer` is) so a patched `SESSION_SECRET` applies."""
        return URLSafeTimedSerializer(resolve_session_secret(), salt=self.salt)

    def set(self, response: Response, payload: Mapping[str, Any]) -> None:
        """Carry this attempt's facts to the callback, signed and timestamped.

        Values are stringified (a raw `UUID` is not JSON-serialisable). `secure` comes from
        `auth.session_cookie_is_secure` so it cannot disagree with the session cookie. `httponly`
        because script access to the verifier would complete an intercepted authorization code.
        """
        missing = [key for key in self.members if key not in payload and key not in self.optional]
        if missing:
            # A programming error, not a request problem: the members are declared at construction.
            raise ValueError(f"flow cookie {self.name!r} is missing {missing}")
        response.set_cookie(
            self.name,
            # An absent optional member is written empty, as `read` reads an absent one.
            self._serializer().dumps({key: str(payload.get(key, "")) for key in self.members}),
            max_age=self.max_age,
            httponly=True,
            # The callback is a cross-site top-level navigation, on which `strict` is not sent.
            samesite="lax",
            secure=session_cookie_is_secure(),
            path=self.path,
        )

    def read(self, request: Request) -> Optional[dict[str, str]]:
        """The attempt this browser started, or None when there is nothing valid to read.

        One None for five failures (absent, tampered, expired, not an object, missing a member):
        distinguishing them would be an oracle for whether a forged cookie was well-formed.
        """
        raw = request.cookies.get(self.name)
        if not raw:
            return None
        try:
            payload = self._serializer().loads(raw, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(payload, dict):
            return None
        # An absent optional member reads as empty: a cookie minted before the member existed stays valid.
        fields = {key: payload.get(key, "" if key in self.optional else None) for key in self.members}
        if not all(
            isinstance(value, str) and (value or key in self.optional) for key, value in fields.items()
        ):
            return None
        return fields  # type: ignore[return-value]

    def clear(self, response: Response) -> None:
        """Retire the attempt. The path must match the one it was set with, or `delete_cookie` writes a
        second cookie and leaves the single-use original in the browser."""
        response.delete_cookie(self.name, path=self.path)


# The refusal slugs `verified_attempt` returns. Each flow's closed error dict must contain these and
# pins that at import: a missing slug would be a 500 on a browser mid-redirect.
UNVERIFIED_EXPIRED = "expired"
UNVERIFIED_STATE_MISMATCH = "state_mismatch"
UNVERIFIED_WRONG_USER = "wrong_user"


def closed_errors(errors: dict[str, str]) -> dict[str, str]:
    """A connect flow's closed slug vocabulary, checked to carry every slug `verified_attempt` can return — or
    the flow's redirect would 500 mid-redirect. `RuntimeError`, not `assert` (stripped under `-O`). Every flow
    that redirects a browser on a `verified_attempt` refusal (`routes/git_connect.py`, `routes/forge_connect.py`,
    `routes/credentials/connect.py`) builds its vocabulary through this."""
    missing = {UNVERIFIED_EXPIRED, UNVERIFIED_STATE_MISMATCH, UNVERIFIED_WRONG_USER} - set(errors)
    if missing:
        raise RuntimeError(f"verified_attempt can return slugs the flow's errors do not carry: {sorted(missing)}")
    return errors


def verified_attempt(
    cookie: "FlowCookie",
    request: Request,
    *,
    callback_state: str,
    user_id: Any,
    log: logging.Logger,
    flow_name: str,
) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """The three checks every OAuth callback makes before it spends an authorization code.

    Returns `(attempt, None)` or `(None, slug)`, never raises; where to redirect is the caller's business.
    In order, all before the exchange so a forged or replayed callback mints no token:

    1. The cookie is present, signed and unexpired (`FlowCookie.read`).
    2. Its state equals the query's state, in constant time (login-CSRF defence).
    3. Its user id equals the signed-in user; the one refusal logged with both ids.

    A vendor `error` and a missing `code` stay with the caller, `error` first: a decline carries no
    code. Nothing about the cookie, state, verifier or code is ever logged.
    """
    attempt, refusal = verified_anonymous_attempt(
        cookie, request, callback_state=callback_state, log=log, flow_name=flow_name, user_id=user_id,
    )
    if attempt is None:
        return None, refusal
    if attempt["user_id"] != str(user_id):
        log.warning(
            "%s: attempt for user=%s arrived on the session of user=%s - refused",
            flow_name, attempt["user_id"], user_id,
        )
        return None, UNVERIFIED_WRONG_USER
    return attempt, None


def verified_anonymous_attempt(
    cookie: "FlowCookie",
    request: Request,
    *,
    callback_state: str,
    log: logging.Logger,
    flow_name: str,
    user_id: Any = None,
) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Checks 1 and 2 of `verified_attempt`, for a flow that has no signed-in user yet (console sign-in).

    Same return shape and same never-logged rule. `user_id` only labels the log line when a caller has
    one (`verified_attempt` passes its own); a sign-in has none, and logs `user=-`.
    """
    who = "-" if user_id is None else user_id
    attempt = cookie.read(request)
    if attempt is None:
        log.warning("%s: user=%s callback with no valid attempt cookie", flow_name, who)
        return None, UNVERIFIED_EXPIRED
    if not same_state(attempt["state"], callback_state):
        log.warning("%s: user=%s callback state did not match the attempt", flow_name, who)
        return None, UNVERIFIED_STATE_MISMATCH
    return attempt, None


def same_state(cookie_state: str, callback_state: str) -> bool:
    """Constant-time equality of the two halves of the CSRF state.

    Encoded to bytes first because `hmac.compare_digest` raises `TypeError` on non-ASCII `str`, and
    strictly (not `errors="ignore"`, which is non-injective): a non-encodable state is refused.
    """
    try:
        cookie_bytes = cookie_state.encode("ascii")
        callback_bytes = callback_state.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(cookie_bytes, callback_bytes)
