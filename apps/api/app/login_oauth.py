"""Console sign-in with an OAuth provider (GitHub, Microsoft) — the policy, no routes.

A server-side authorization-code + PKCE redirect: `routes/login_oauth.py` holds the doors, this module the
rules they share —

* the flow cookie (`_LOGIN_FLOW`) carrying state, verifier, provider, the origin the attempt started on and
  a nonce, from `/start` to `/callback` (mechanism: `app/oauth_flow_cookie.py`);
* which origins a sign-in may start and finish on (`console_origins`, `request_origin`): the attempt
  completes on the origin it started from, only if that origin is on the environment's allowlist;
* who a verified provider identity is (`resolve_login`): invite-only, keyed on the provider's STABLE id and
  never on an email, with Google's stale-link rule generalised;
* the closed vocabulary the login page is told (`ERROR_SLUGS`) and the one audit row a resolved sign-in
  writes (`record_sign_in`).

Nothing here logs or stores a code, state, verifier, nonce, token or email.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Literal, Optional, Union
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import forge_oauth, identities
from app.operator_settings import reads as settings_reads
from app.config import get_settings
from app.git_access import APP_KNOBS
from app.models.identity import AdminUser, UserIdentity
from app.oauth_flow_cookie import FlowCookie
from app.user_activity import record_write

log = logging.getLogger("app.login_oauth")

Provider = Literal["github", "microsoft"]

# The flow cookie. Its path is the narrowest prefix every provider's two doors share. `nonce` is Microsoft's
# OIDC nonce; GitHub carries a random, never-checked value so every member stays a required non-empty string.
# `intent` is which door the attempt started on: a sign-in, or the self-service sign-up (`app/signup.py`);
# optional, so an attempt started before it existed finishes as the sign-in it was. `switch_to` is the org an
# org switch's re-authentication signs into (`login_funnel.switch_target`); empty for an ordinary sign-in.
_LOGIN_FLOW = FlowCookie(
    suffix="_login_oauth",
    path="/api/auth/oauth",
    salt="login-oauth-flow",
    # `app`: the GitHub app kind `/start` authorized against (`git_access.APP_KNOBS`), so the callback
    # exchanges with the same one; blank for Microsoft and for a cookie minted before it existed.
    members=("state", "verifier", "provider", "origin", "nonce", "intent", "switch_to", "app"),
    optional=("intent", "switch_to", "app"),
)
INTENT_SIGN_IN = "sign_in"
INTENT_SIGN_UP = "signup"
INTENTS = frozenset({INTENT_SIGN_IN, INTENT_SIGN_UP})
FLOW_COOKIE_NAME = _LOGIN_FLOW.name

# Everything the login page is told, as `?login_error=<slug>`. The page owns the wording; no provider text
# and no exception text ever reaches the redirect.
NO_ACCOUNT = "no_account"
ALREADY_LINKED = "already_linked"
AMBIGUOUS = "ambiguous"
FAILED = "failed"
NOT_CONFIGURED = "not_configured"
# Self-service orgs: the sign-up door when `signup_open` is off, and every door for a rejected org
# (`app/org_gate.py::refuse_unapproved_sign_in`, which may add `login_detail=<the operator's reason>`) or a
# pending one (an application under review: the login page's "Under review" panel, never an error).
SIGNUP_CLOSED = "signup_closed"
ORG_NOT_APPROVED = "org_not_approved"
ORG_PENDING_REVIEW = "org_pending"
# A soft-deleted org (`orgs.deleted_at`): refused on every door until an operator restores it.
ORG_DELETED = "org_deleted"
# The sign-up door's domain join into an org already at its user cap (`app/org_limits.py`).
USER_LIMIT_REACHED = "user_limit_reached"
# A disabled user (`admin_users.disabled_at`), refused by `app/login_funnel.py` on every door.
ACCOUNT_DISABLED = "account_disabled"
# A member of an SSO-only org signing in by anything but its IdP tenant (`app/sso_policy.py`), on every door.
SSO_REQUIRED = "sso_required"
# SAML/OIDC sign-in (`app/org_sso.py`, `app/sso_signin.py`): a member of a Require-SSO org signing in another way;
# a person the org's IdP proved but who holds no membership it may open; an asserted email outside the org's verified
# domains; the org's plan without SSO; an IdP account other than the one the member is linked to (an admin resets
# the link); a first link the IdP's answer cannot prove (an editable email attribute, `sso_signin.bind_subject`); any
# other failure at the IdP or of its answer.
SSO_ENFORCED = "sso_enforced"
SSO_NOT_PROVISIONED = "sso_not_provisioned"
SSO_DOMAIN_MISMATCH = "sso_domain_mismatch"
SSO_NOT_ON_PLAN = "sso_not_on_plan"
SSO_SUBJECT_MISMATCH = "sso_subject_mismatch"
SSO_LINK_UNPROVEN = "sso_link_unproven"
SSO_FAILED = "sso_failed"
# Too many failed sign-ins for this identifier from this source (`app/login_lockout.py`), on every door.
LOCKED = "locked"
# A sign-in from outside the org's IP allowlist (`app/ip_allowlist.py`), on every door.
IP_NOT_ALLOWED = "ip_not_allowed"
ERROR_SLUGS = frozenset(
    {NO_ACCOUNT, ALREADY_LINKED, AMBIGUOUS, FAILED, NOT_CONFIGURED, SIGNUP_CLOSED, ORG_NOT_APPROVED,
     ORG_PENDING_REVIEW, ORG_DELETED, USER_LIMIT_REACHED, ACCOUNT_DISABLED, SSO_REQUIRED, LOCKED, IP_NOT_ALLOWED,
     SSO_ENFORCED, SSO_NOT_PROVISIONED, SSO_DOMAIN_MISMATCH, SSO_NOT_ON_PLAN, SSO_SUBJECT_MISMATCH, SSO_LINK_UNPROVEN,
     SSO_FAILED}
)

# The extra origins a sign-in may run on besides `FRONTEND_URL` (the tailnet console beside the public UI),
# comma-separated. Read at call time.
CONSOLE_ORIGINS_ENV = "CONSOLE_ORIGINS"


@dataclass(frozen=True)
class VerifiedEmail:
    """One email the provider says the account has VERIFIED. Unverified addresses are never built."""

    email_lower: str
    primary: bool = False


@dataclass(frozen=True)
class VerifiedIdentity:
    """A provider identity after the provider proved it: its stable id and its verified emails.

    `subject` is GitHub's numeric user id, or Microsoft's `(tid, oid)` pair."""

    provider: Provider
    subject: Union[int, tuple[UUID, UUID]]
    emails: list[VerifiedEmail] = field(default_factory=list)

    def email_set(self) -> set[str]:
        return {e.email_lower for e in self.emails}

    def primary_email(self) -> Optional[str]:
        return next((e.email_lower for e in self.emails if e.primary), None)

    def named_email(self) -> Optional[str]:
        """The one email a refused sign-in is recorded under: the primary, else the first verified one."""
        return self.primary_email() or next(iter(sorted(self.email_set())), None)


@dataclass(frozen=True)
class LoginRefusal:
    """A sign-in that signs nobody in, as one slug from `ERROR_SLUGS`.

    `user` is the row the refusal is ABOUT when there is one (`already_linked`), so the route can attribute
    its audit row; None for every refusal that resolved to nobody."""

    slug: str
    user: Optional[AdminUser] = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.slug not in ERROR_SLUGS:
            raise ValueError(f"{self.slug!r} is not a member of the closed sign-in error vocabulary")


# ── Origins ───────────────────────────────────────────────────────────────────────────────────────────


def _normalise_origin(raw: str) -> Optional[str]:
    """`scheme://host[:port]`, lower-cased, default port dropped; None for anything that is not one."""
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    host = parts.hostname.lower()
    if ":" in host:  # an IPv6 literal keeps its brackets
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" if port in (None, default) else f"{parts.scheme}://{host}:{port}"


def frontend_origin() -> str:
    """`FRONTEND_URL` as an origin: the fallback every non-allowlisted request resolves to."""
    raw = get_settings().frontend_url
    return _normalise_origin(raw) or raw.rstrip("/")


def console_origins() -> list[str]:
    """Every origin a sign-in may start and finish on: `FRONTEND_URL` plus `CONSOLE_ORIGINS`, deduped."""
    origins = [frontend_origin()]
    for raw in os.environ.get(CONSOLE_ORIGINS_ENV, "").split(","):
        origin = _normalise_origin(raw) if raw.strip() else None
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def _host_port(netloc: str) -> Optional[tuple[str, Optional[int]]]:
    """`(host, port-or-None)` of a `Host` header or an origin's netloc; None when it does not parse."""
    parts = urlsplit(f"//{netloc.strip()}")
    try:
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.username is not None or parts.password is not None:
        return None
    return parts.hostname.lower(), port


def request_origin(request: Request) -> Optional[str]:
    """The allowlisted origin this request arrived on, or None.

    The `Host` header's host and port are matched against each allowlist entry and the matched ENTRY is
    returned, scheme included: a spoofed `Host` can only ever select an allowlisted origin, and an entry
    whose scheme differs from `FRONTEND_URL`'s (a tailnet console on http) still matches. None also
    covers a proxy that rewrites `Host` (the local vite proxy does)."""
    arrived = _host_port(request.headers.get("host", ""))
    if arrived is None:
        return None
    host, port = arrived
    for origin in console_origins():
        scheme, netloc = origin.split("://", 1)
        entry = _host_port(netloc)
        if entry is None:
            continue
        default = 443 if scheme == "https" else 80
        if entry[0] == host and (entry[1] or default) == (port or default):
            return origin
    return None


def redirect_uri(origin: str, provider: Provider) -> str:
    """The callback URL, absolute: a binding on the code exchange, rebuilt from the COOKIE's origin."""
    return f"{origin}/api/auth/oauth/{provider}/callback"


def start_path(provider: Provider) -> str:
    return f"/api/auth/oauth/{provider}/start"


# ── Configuration ─────────────────────────────────────────────────────────────────────────────────────


def github_configured() -> bool:
    """Whether GitHub sign-in is offered: the operator turned it on (`github_sign_in_enabled`, off by default —
    its catalogue entry says why) AND a GitHub app of either kind is designated (`git_access.APP_KNOBS`).
    Setting reads only: no credential row, no KMS decrypt, since the public settings route and the login
    page's first paint call it. `/start` resolves the app itself."""
    if not settings_reads.get_bool("github_sign_in_enabled"):
        return False
    return any(settings_reads.get_str(knob).strip() != "" for knob, _type, _what in APP_KNOBS.values())


def microsoft_configured() -> bool:
    """Whether Microsoft sign-in is offered: the operator turned it on (`microsoft_sign_in_enabled`, off by
    default) AND an Entra app is designated (`azure_devops_app_credential_id`, the one Connect Azure DevOps
    uses). Setting reads only, as `github_configured`; `/start` resolves the app itself."""
    if not settings_reads.get_bool("microsoft_sign_in_enabled"):
        return False
    return settings_reads.get_str(forge_oauth.FORGE_OAUTH["azure_devops"].app_setting).strip() != ""


# ── Resolution ────────────────────────────────────────────────────────────────────────────────────────


def _linked_identity(session: Session, ident: VerifiedIdentity) -> Optional[UserIdentity]:
    if ident.provider == "github":
        return identities.by_github_user_id(session, ident.subject)  # type: ignore[arg-type]
    tenant_id, object_id = ident.subject  # type: ignore[misc]
    return identities.by_microsoft(session, tenant_id, object_id)


def _current_link(person: UserIdentity, provider: Provider):
    if provider == "github":
        return person.github_user_id
    if person.microsoft_tenant_id is None:
        return None
    return (person.microsoft_tenant_id, person.microsoft_object_id)


def _link_fields(ident: VerifiedIdentity) -> dict:
    if ident.provider == "github":
        return {"github_user_id": ident.subject}
    tenant_id, object_id = ident.subject  # type: ignore[misc]
    return {"microsoft_tenant_id": tenant_id, "microsoft_object_id": object_id}


def _cleared_fields(provider: Provider) -> dict:
    if provider == "github":
        return {"github_user_id": None}
    return {"microsoft_tenant_id": None, "microsoft_object_id": None}


def resolve_login(
    session: Session, ident: VerifiedIdentity, *, idp_claims: Optional[dict] = None, request: Optional[Request] = None
) -> Union[AdminUser, LoginRefusal]:
    """The membership this identity signs in as, or why nobody — invite-only, never creates a user.

    1. The PERSON (`app/identities.py`) linked to the provider's stable id counts only while their email is
       still one of the identity's verified emails: otherwise the link is stale, remembered,
       and the email arm decides. A linked system actor is refused outright.
    2. The email arm: non-system people whose email is one of the verified emails. None → `no_account`;
       one → that person; several → the person owning the PRIMARY email, else `ambiguous`.
    3. A chosen person already linked to a different id of this provider → `already_linked`.
    4. A stale link is released (flushed first: the id is unique) and the new one set. Flushed, not
       committed — committed by the sign-in's mint (`login_funnel._mint`) or a refusal's record, the
       audit row after it. A unique race → `already_linked`.
    5. Which of the person's memberships opens: `login_funnel.pick_membership`; none at all →
       `account_disabled` (the last membership was removed).

    The person reads are `app/identities.py`'s (platform plane, no escape needed) and the membership read
    runs under its identity escape (the route is BOOTSTRAP: nothing is bound).
    """
    from app import login_funnel

    emails = ident.email_set()
    linked = _linked_identity(session, ident)
    if linked is not None and identities.is_system(session, linked):
        return LoginRefusal(NO_ACCOUNT)
    if linked is not None and linked.email.lower() in emails:
        return _membership(session, linked, ident, idp_claims, request)
    stale_link = linked

    if not emails:
        return LoginRefusal(NO_ACCOUNT)
    candidates = list(
        session.scalars(select(UserIdentity).where(func.lower(UserIdentity.email).in_(sorted(emails))))
    )
    by_id = {person.id: person for person in candidates if not identities.is_system(session, person)}
    if not by_id:
        return LoginRefusal(NO_ACCOUNT)
    if len(by_id) == 1:
        chosen = next(iter(by_id.values()))
    else:
        primary = ident.primary_email()
        chosen = next((person for person in by_id.values() if person.email.lower() == primary), None)
        if chosen is None:
            return LoginRefusal(AMBIGUOUS)

    current = _current_link(chosen, ident.provider)
    if current is not None and current != ident.subject:
        return LoginRefusal(ALREADY_LINKED, user=login_funnel.pick_membership(session, chosen, ident.provider, request=request))
    if current == ident.subject:
        # Already linked here (the stale arm cannot reach this: it would be `linked`).
        return _membership(session, chosen, ident, idp_claims, request)

    try:
        if stale_link is not None:
            identities.set_fields(session, stale_link, **_cleared_fields(ident.provider))
            session.flush()  # the id is unique: the release must reach the DB before the relink
        identities.set_fields(session, chosen, **_link_fields(ident))
        session.flush()
    except IntegrityError:
        session.rollback()
        log.warning("%s-login: link race on identity=%s — refused as already linked", ident.provider, chosen.id)
        return LoginRefusal(ALREADY_LINKED)
    return _membership(session, chosen, ident, idp_claims, request)


def _membership(
    session: Session, person: UserIdentity, ident: VerifiedIdentity, idp_claims: Optional[dict],
    request: Optional[Request] = None,
) -> Union[AdminUser, LoginRefusal]:
    """`resolve_login`'s step 5: the membership to open, or `account_disabled` when none is left."""
    from app import login_funnel

    user = login_funnel.pick_membership(session, person, ident.provider, idp_claims=idp_claims, request=request)
    return user if user is not None else LoginRefusal(ACCOUNT_DISABLED)


def stamp_email_verified(session: Session, user: AdminUser, proven_email: str) -> None:
    """Record that a sign-in just proved `proven_email` is this person's (`UserIdentity.email_verified_at`).

    One conditional UPDATE, not an attribute write: it stamps only while the identity STILL holds the
    address the provider verified, so an admin changing the email between this sign-in's read and its
    commit cannot leave the new, unproven address stamped. Caller commits."""
    from datetime import datetime, timezone

    from sqlalchemy import func, update
    from sqlalchemy.orm.attributes import set_committed_value

    if user.identity_id is None:
        return
    stamped_at = datetime.now(timezone.utc)
    stamped = session.execute(
        update(UserIdentity)
        .where(
            UserIdentity.id == user.identity_id,
            func.lower(UserIdentity.email) == proven_email.strip().lower(),
            UserIdentity.email_verified_at.is_(None),
        )
        .values(email_verified_at=stamped_at)
        .execution_options(synchronize_session=False)
        .returning(UserIdentity.id)
    ).first()
    if stamped is not None:
        # Mirrored onto the loaded objects so this same response reports it, without marking them dirty;
        # the membership's legacy copy is written too (`models.set_identity_fields`'s dual write).
        identity = user._loaded_identity()
        if identity is not None:
            set_committed_value(identity, "email_verified_at", stamped_at)
        session.execute(
            update(AdminUser.__table__)
            .where(
                AdminUser.__table__.c.id == user.id,
                func.lower(AdminUser.__table__.c.email) == proven_email.strip().lower(),
                AdminUser.__table__.c.email_verified_at.is_(None),
            )
            .values(email_verified_at=stamped_at)
        )
        set_committed_value(user, "_legacy_email_verified_at", stamped_at)


#: `record_sign_in`'s outcome for a provider sign-in the console's second factor must still complete.
MFA_PENDING = "mfa_pending"


def record_sign_in(session: Session, user: AdminUser, provider: Provider, outcome_slug: str) -> None:
    """Stage the ONE attributed audit row for a sign-in that resolved to a user (success, `MFA_PENDING`,
    or `already_linked`). Carries the provider and the outcome only — no code, state, token or email in the
    summary or data. The caller commits it together with any link write.

    A sign-in that resolved to nobody writes no row (there is no one to attribute it to): the route logs
    its slug and provider instead."""
    label = {"github": "GitHub", "microsoft": "Microsoft"}[provider]
    summary = (
        f"Signed in with {label}" if outcome_slug == "ok"
        else f"{label} sign-in awaiting a second factor" if outcome_slug == MFA_PENDING
        else f"{label} sign-in refused ({outcome_slug})"
    )
    record_write(session, user, "sign_in", summary, data={"provider": provider, "outcome": outcome_slug})
