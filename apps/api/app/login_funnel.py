"""The single login funnel: the only module that mints a console session cookie (G-02, G-04).

Every first factor — password (`routes/auth.py::login`), Google (`routes/auth.py::google_sign_in`), GitHub
and Microsoft (`routes/login_oauth.py`), and the sign-up doors that finish in a session — resolves a PERSON
(`app/identities.py`), opens one of their memberships (`pick_membership`), and ends in
`complete_first_factor`, which refuses a disabled row, runs `POLICY_HOOKS` (the lockout's hard lock first, then
an unapproved org, then SSO-only orgs), then demands the second factor (PRD D7, `app/mfa.py`) unless the org skips it or the browser is a
trusted device (`mfa.skip_reason`, `mfa.use_trusted_device`): no first factor alone mints a session otherwise, and
a row with no password door (`auth.password_refusal`: a superadmin, unless an operator opened one) may not use one. The pending second factor ends in `complete_second_factor`,
which mints the cookie. A re-mint of an EXISTING session (a password change's new
epoch, a Connect flow's SameSite upgrade, the sliding idle window) goes through `reissue_session`, which keeps
the session's `iat` and `amr` so a re-mint can never extend the absolute cap or launder a factor. An org switch
(`POST /api/auth/switch`) goes through `switch_membership`, which moves the session to another membership of
the same person only once that org's sign-in policy holds for it, keeping `iat` and adding to `amr`; a tab still
on the old org is refused by `app/stale_tab_guard.py`.
`tests/test_login_funnel.py` pins that no other module calls `auth.set_session_cookie`.

The cookie and its expiries are `app/auth.py`'s (`read_session`, `SESSION_IDLE_TIMEOUT`, `SESSION_MAX_AGE`).
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Final, Literal, NamedTuple, Optional
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from app import act_as, auth, edition_features, hipaa, identities, login_lockout, mfa, org_gate
from app.db import IdentitySessionLocal, SessionLocal
from app.request_caller import client_headers
from app.models.identity import AdminUser, UserIdentity
from app.models.orgs import Org
from app.tenancy import seam
from app.tenancy.ids import UNATTRIBUTED_SIGN_IN_ORG_ID
from app.user_activity import record_write

log = logging.getLogger(__name__)
# One JSON object per refused sign-in, the line the `login_failed` metric filter reads (`{ $.event =
# "login_failed" }`); `app/main.py`'s handler writes this logger's messages bare. Its shape is a contract:
# `event`, `method`, `reason`, `ip`, `user_agent`, `forwarded_for`, `identifier_hash` — never a raw identifier.
security_log = logging.getLogger("app.security")

FirstFactor = Literal["password", "google", "github", "microsoft", "saml", "oidc"]

# The `amr` a first factor contributes to a new session's cookie. SAML and OIDC (`app/sso_signin.py`) are IdP doors
# like the others: no MFA attestation, so the console's second factor still applies.
_FACTOR_AMR: dict[str, str] = {
    "password": "pwd", "google": "oauth", "github": "oauth", "microsoft": "oauth", "saml": "oauth", "oidc": "oauth",
}


class LoginOutcome(NamedTuple):
    """What a first factor led to. `session`: the cookie is set on the response. Any other kind: no cookie.
    `reason` is set for `refused` only: one of `disabled`, `locked`, `org_pending`, `org_not_approved`,
    `superadmin_password`, `password_off` (`auth.password_refusal`), `sso_required`, `sso_enforced`, `bad_factor`."""

    kind: Literal["session", "mfa_required", "enrol_required", "refused"]
    reason: Optional[str] = None


#: `(session, request, user, identifier, method, idp_claims) -> LoginOutcome | None`. Run in order after
#: the disabled check; the first non-None outcome is the result (no cookie unless it is `session`, which
#: a hook must not return — the funnel mints). `identifier` is the user's email.
PolicyHook = Callable[
    [Session, Request, Optional[AdminUser], str, FirstFactor, Optional[dict]], Optional[LoginOutcome]
]


def lockout_policy(
    session: Session,
    request: Request,
    user: Optional[AdminUser],
    identifier: str,
    method: FirstFactor,
    idp_claims: Optional[dict],
) -> Optional[LoginOutcome]:
    """The first `POLICY_HOOKS` entry: refuse a sign-in by any door while its identifier is locked from this
    source (`app/login_lockout.py`). The step-up is the password door's alone (`routes/auth.py::login`)."""
    if login_lockout.state(session, identifier, source_ip(request)) != login_lockout.LOCKED:
        return None
    record_login_failure(session, request, user, method=method, reason=login_lockout.LOCKED)
    log.info("login: refused locked method=%s", method)
    return LoginOutcome("refused", login_lockout.LOCKED)


def org_approval_policy(
    session: Session,
    request: Request,
    user: Optional[AdminUser],
    identifier: str,
    method: FirstFactor,
    idp_claims: Optional[dict],
) -> Optional[LoginOutcome]:
    """The backstop behind every door's own org gate (`org_gate.refuse_unapproved_sign_in`): refuse a user
    whose org is pending or rejected (`org_gate.sign_in_refusal`), so a door that forgets the gate still
    mints nothing."""
    code = org_gate.sign_in_refusal(session, user.org_id) if user is not None else None
    if code is None:
        return None
    record_login_failure(session, request, user, method=method, reason=code)
    log.info("login: refused %s method=%s", code, method)
    return LoginOutcome("refused", code)


POLICY_HOOKS: list[PolicyHook] = [lockout_policy, org_approval_policy]

#: `(session, user, method, idp_claims, client_ip) -> bool`: a SIDE-EFFECT-FREE twin of a per-row `POLICY_HOOKS`
#: entry, asked by `pick_membership` of every membership before one is chosen (so a refusal is recorded
#: only for the row the sign-in actually opens). The lockout hook has none: it keys on the identifier,
#: which every membership of one person shares. A module adding a per-row hook adds its twin here.
#: `client_ip` is the sign-in's source address (`source_ip`), None for a door that has no request.
AdmissionCheck = Callable[[Session, AdminUser, FirstFactor, Optional[dict], Optional[str]], bool]
ADMISSION_CHECKS: list[AdmissionCheck] = []

#: `(session, user, client_ip) -> refusal code or None`: what a session minted WITHOUT a first factor at this
#: request must still pass for `user` from `client_ip` — an org switch (`switch_membership`) and a completed
#: second factor (`complete_second_factor`), whose first factor was judged for another org or another request.
#: The IP allowlist registers one (`app/ip_allowlist.py::entry_refusal`).
EntryCheck = Callable[[Session, AdminUser, Optional[str]], Optional[str]]
ENTRY_CHECKS: list[EntryCheck] = []


def entry_refusal(session: Session, user: AdminUser, client_ip: Optional[str]) -> Optional[str]:
    """The first `ENTRY_CHECKS` refusal code for `user` from `client_ip`, or None."""
    return next((code for check in ENTRY_CHECKS if (code := check(session, user, client_ip)) is not None), None)


def _admits(
    session: Session, user: AdminUser, method: FirstFactor, idp_claims: Optional[dict], client_ip: Optional[str]
) -> bool:
    """Whether this membership should be the one a sign-in by this door opens: live, its org approved
    (`org_gate.sign_in_refusal`) and enabled, not a superadmin on the password door, and every
    `ADMISSION_CHECKS` entry agrees. Reads only."""
    if user.is_system or user.disabled_at is not None:
        return False
    if method == "password" and auth.password_refusal(user) is not None:
        return False
    if org_gate.sign_in_refusal(session, user.org_id) is not None:
        return False
    # A disabled org (`orgs.enabled`) is passed over for another membership (R4); alone, it still signs in
    # as it always has — that decision is the org lifecycle's, not this choice's.
    org = session.get(Org, user.org_id)
    if org is not None and not org.enabled:
        return False
    return all(check(session, user, method, idp_claims, client_ip) for check in ADMISSION_CHECKS)


def pick_membership(
    session: Session,
    identity: UserIdentity,
    method: FirstFactor,
    *,
    idp_claims: Optional[dict] = None,
    request: Optional[Request] = None,
) -> Optional[AdminUser]:
    """WHICH MEMBERSHIP a proven first factor opens, or None when the person holds none (every door then
    refuses `disabled` — removing the last membership deactivates the account, R7; no flag of its own).

    Candidates in order: the identity's `last_active_admin_id` (the org the person last acted as, R4), then
    the rest by `last_seen_at` (most recent first). The first one `_admits` admits is chosen. When none is,
    the FIRST candidate is returned, so the door's own checks and `complete_first_factor` refuse it exactly
    as they refuse a single-membership person today. The system actor's row is never a candidate.
    `request` (a sign-in door's) lets an org's IP allowlist pass its membership over (`app/ip_allowlist.py`)."""
    client_ip = source_ip(request) if request is not None else None
    rows = [row for row in identities.memberships(session, identity.id) if not row.is_system]
    if not rows:
        return None
    last = identity.last_active_admin_id

    def order(row: AdminUser):
        seen = row.last_seen_at.timestamp() if row.last_seen_at is not None else float("-inf")
        return (row.id != last, -seen, row.created_at)

    candidates = sorted(rows, key=order)
    with seam.identity_scope():
        for row in candidates:
            if _admits(session, row, method, idp_claims, client_ip):
                return row
    return candidates[0]


def _mint(
    session: Session,
    response,
    user: AdminUser,
    amr: tuple[str, ...],
    *,
    iat: Optional[int] = None,
) -> None:
    """Set the session cookie for a SIGN-IN of `user` (a new session, or one completed by its second
    factor), carrying both epochs, and remember this membership as the person's last active one (R4).
    Commits that write with whatever the caller staged. An owner whose org an operator submitted for them gets
    the Terms claim (`signup.terms_pending`, PRD D7): the console is gated until they accept."""
    identity = user._loaded_identity()
    if identity is not None:
        identity.last_active_admin_id = user.id
    session.commit()
    terms = None
    # An edition without sign-up has no operator-submitted application to hold its owner at the Terms.
    if edition_features.enabled("signup"):
        from app import signup

        terms = auth.TERMS_PENDING if signup.terms_pending(user.id, user.org_id) else None
    auth.set_session_cookie(
        response, user.id, user.session_epoch or 0, iepoch=auth.identity_epoch(user), iat=iat, amr=amr,
        terms=terms,
    )


def complete_first_factor(
    session: Session,
    request: Request,
    response,
    user: AdminUser,
    method: FirstFactor,
    *,
    idp_claims: Optional[dict] = None,
    second_factor: Optional[str] = None,
) -> LoginOutcome:
    """Finish a verified first factor for `user`: refuse, defer to a policy hook, or set the session cookie
    on `response`. Call only once the factor itself is proven and every door-specific refusal has run.

    `second_factor` is the `amr` of a second factor the same request already proved — the password door's
    step-up TOTP (`routes/auth.py::login`), verified before the password — which completes the sign-in here
    rather than asking for a second code."""
    if method not in _FACTOR_AMR:
        raise ValueError(f"unknown first factor {method!r}")
    if user.is_system:
        # Every door refuses the system actor before this; a second refusal here costs nothing.
        return LoginOutcome("refused", "bad_factor")
    if user.disabled_at is not None:
        record_login_failure(session, request, user, method=method, reason="disabled")
        log.info("login: refused disabled user=%s method=%s", user.id, method)
        return LoginOutcome("refused", "disabled")
    password_refused = auth.password_refusal(user) if method == "password" else None
    if password_refused is not None:
        record_login_failure(session, request, user, method=method, reason=password_refused)
        log.info("login: refused password door=%s user=%s", password_refused, user.id)
        return LoginOutcome("refused", password_refused)
    for hook in POLICY_HOOKS:
        outcome = hook(session, request, user, user.email, method, idp_claims)
        if outcome is not None:
            if outcome.kind == "session":
                raise RuntimeError("a policy hook may not mint a session; return None to let the funnel")
            return outcome
    amr_first = _FACTOR_AMR[method]
    if second_factor is not None:
        # The factor's spent step lands before the session it proves (`_mint` commits first).
        _mint(session, response, user, (amr_first, second_factor))
        return LoginOutcome("session")
    if amr_first == "oauth" and any(attest(session, user, method, idp_claims) for attest in mfa.SSO_ATTESTERS):
        _mint(session, response, user, (amr_first, "sso"))
        return LoginOutcome("session")
    skipped = mfa.skip_reason(session, user)
    if skipped is not None:
        return _mint_without_code(session, response, user, method, amr_first, "skip", None, skipped)
    device = mfa.use_trusted_device(session, user, request.cookies.get(mfa.DEVICE_COOKIE))
    if device is not None:
        return _mint_without_code(session, response, user, method, amr_first, "device", device.label)
    # The second factor is due: no session, a pending cookie whose nonce becomes the user's one live one.
    enrolled = mfa.is_enrolled(session, user)
    nonce = mfa.set_pending_cookie(response, user, method, amr_first)
    mfa.bind_pending_nonce(session, user, nonce)
    session.commit()
    return LoginOutcome("mfa_required" if enrolled else "enrol_required")


def _mint_without_code(
    session: Session, response, user: AdminUser, method: FirstFactor, amr_first: str, stand_in: str,
    device_label: Optional[str], skipped: Optional[str] = None,
) -> LoginOutcome:
    """Mint a session whose second factor is a stand-in (`skip` or `device`), recording which: the one
    trace of a sign-in that never saw a code (SOC 2 G-01). Committed with the device's `last_used_at`.
    `skipped` is `mfa.skip_reason`'s answer for a `skip`."""
    if device_label:
        how = f"on trusted {device_label}"
    else:
        how = "with two-factor off for the user" if skipped == mfa.SKIP_USER else "with two-factor off for the org"
    record_write(
        session, user, "mfa_skipped", f"{user.email} signed in without a code {how}",
        data={"method": method, "amr": [amr_first, stand_in], "device": device_label, "skip": skipped},
    )
    _mint(session, response, user, (amr_first, stand_in))
    return LoginOutcome("session")


#: `complete_second_factor`'s refusal when the row no longer holds the pending sign-in.
SECOND_FACTOR_EXPIRED: Final = "expired"


def complete_second_factor(
    session: Session, response, user: AdminUser, pending: mfa.Pending, amr_second: str,
    *, client_ip: Optional[str] = None,
) -> Optional[str]:
    """Mint the session a proven second factor completes, clearing the pending cookie. None once minted;
    otherwise why nothing was: `SECOND_FACTOR_EXPIRED` when the row no longer holds the pending sign-in
    (disabled since, its epoch bumped, or its org not approved — `org_gate.sign_in_refusal`, a pending cookie
    issued before the org gate held), or an `ENTRY_CHECKS` code for `client_ip` (the IP allowlist's
    `ip_not_allowed`: the first factor came from an allowed address, this request did not)."""
    mfa.clear_pending_cookie(response)
    # Re-read: a disable or an epoch bump (the row's or the person's) committed since the pending cookie was
    # checked must win.
    session.refresh(user)
    if user.identity is not None:
        session.refresh(user.identity)
    if user.is_system or not auth.session_row_is_live(user, pending.epoch, pending.iepoch):
        return SECOND_FACTOR_EXPIRED
    if org_gate.sign_in_refusal(session, user.org_id) is not None:
        return SECOND_FACTOR_EXPIRED
    refused = entry_refusal(session, user, client_ip)
    if refused is not None:
        return refused
    _mint(session, response, user, (pending.amr_first, amr_second))
    return None


class SwitchOutcome(NamedTuple):
    """What `switch_membership` decided. `session`: the cookie is re-minted on the response. `reauth_required`:
    no cookie change; `door` is how the target org's policy can be met (`otp`; `enrol` — the person has no
    factor yet, and the pending cookie now set leads to the enrol page; or an IdP: `google`, `microsoft`,
    `github`), and `error` says why a supplied code did not meet it (`bad_factor`, `mfa_throttled`).
    `refused`: `reason` is `not_found`, `org_pending`, `org_not_approved` or an `ENTRY_CHECKS` code
    (`ip_not_allowed`)."""

    kind: Literal["session", "reauth_required", "refused"]
    door: Optional[str] = None
    reason: Optional[str] = None
    error: Optional[str] = None


#: The `amr` values that prove a second factor for ANY org: a code the person typed. Not `skip` (an `mfa_skip`
#: org's own waiver, R10/AC14), `sso` (the session's org's attestation of ITS IdP — another org never made it)
#: or `device` (a trusted browser: a switch re-checks the request's own device cookie instead, `switch_membership`).
_PROVEN_SECOND_FACTOR = frozenset({"otp", "recovery"})


def _idp_door(identity: Optional[UserIdentity]) -> str:
    """The IdP door a superadmin (who has no password door, PRD D7) re-authenticates through: a provider the
    person is linked to, else any configured one, in a fixed order; `google` when none is configured."""
    from app import login_oauth

    configured = {
        "google": auth.google_client_id() is not None,
        "microsoft": login_oauth.microsoft_configured(),
        "github": login_oauth.github_configured(),
    }
    linked = {
        "google": identity is not None and identity.google_sub is not None,
        "microsoft": identity is not None and identity.microsoft_tenant_id is not None,
        "github": identity is not None and identity.github_user_id is not None,
    }
    for door in ("google", "microsoft", "github"):
        if configured[door] and linked[door]:
            return door
    return next((door for door in ("google", "microsoft", "github") if configured[door]), "google")


def _first_factor_door(session: Session, target: AdminUser, org: Org, amr: set[str]) -> Optional[str]:
    """The IdP door the target org's first-factor policy demands of this session, or None when it is met.

    An SSO-only org (`app/sso_policy.py`) admits only its IdP from its tenant, and a cookie records neither
    the provider nor the tenant, so switching INTO one always goes through its door; a Require-SSO org
    (`app/org_sso.py`) likewise, through door `sso` (a superadmin row excepted, as at sign-in). A superadmin has no
    password door unless an operator opened one (`auth.password_refusal`), so a session whose first factor was a
    password goes through an IdP whenever the target has no password door."""
    if org.sso_provider is not None:
        return org.sso_provider
    if edition_features.enabled("sso"):
        from app import org_sso

        if not target.is_superadmin and org_sso.enforced(session, org.id):
            return "sso"
    if auth.password_refusal(target) is not None and "oauth" not in amr:
        return _idp_door(target._loaded_identity())
    return None


def switch_membership(
    session: Session,
    request: Request,
    response,
    claims: auth.SessionClaims,
    target: Optional[AdminUser],
    *,
    code: Optional[str] = None,
) -> SwitchOutcome:
    """Move the browser session `claims` describes to `target`, the same person's membership in another org
    (`POST /api/auth/switch`, R2/R4/R10) — or say why not. The ONE way a session changes org.

    1. `target` must be a live membership of the SAME person as the cookie's live row, else `not_found`.
       `target` IS that row → the session is re-minted as it is (a no-op).
    2. The org gate (`org_gate.sign_in_refusal`; a disabled org reads `org_not_approved`), then `ENTRY_CHECKS`
       for this request's address (the target org's IP allowlist) → `refused`.
    3. The target org's full sign-in policy against what the session PROVED (`claims.amr`): its first factor
       (`_first_factor_door`), then its second — only a typed code proves one across orgs
       (`_PROVEN_SECOND_FACTOR`), so a session that has none must give `code` (a TOTP or a recovery code,
       `mfa.verify_current_code`), unless the target org skips the factor or this request's browser is the
       person's trusted device (`mfa.use_trusted_device` — read off the request's cookie, never off
       the session's `amr`, and recorded `mfa_skipped` as a sign-in on it is). Unmet → `reauth_required`. A person
       with no factor gets door `enrol` and the pending cookie a sign-in's `enrol_required` sets.
    4. Re-mint for `target` with the session's own `iat` (the absolute cap holds) and its `amr` plus what was
       proven here — never less, so nothing is laundered. Remember `target` as the person's last active
       membership (R4), and announce `session_switched` so open streams of THIS session close (R4).

    No audit event (the operator's decision, PRD §5): nothing here records a switch, only the factor traces
    (`mfa_skipped`, `mfa_recovery_used`)."""
    with seam.identity_scope():
        current = session.get(AdminUser, claims.admin_id)
    if (
        current is None
        or not auth.session_row_is_live(current, claims.epoch, claims.iepoch)
        or target is None
        or target.is_system
        or target.disabled_at is not None
        or target.identity_id is None
        or target.identity_id != current.identity_id
    ):
        return SwitchOutcome("refused", reason="not_found")
    if target.id == current.id:
        auth.set_session_cookie(
            response, current.id, current.session_epoch or 0, iepoch=auth.identity_epoch(current),
            iat=claims.iat, amr=claims.amr, terms=claims.terms,
        )
        return SwitchOutcome("session")
    with seam.identity_scope():
        org = session.get(Org, target.org_id)
        if org is None:
            return SwitchOutcome("refused", reason="not_found")
        refusal = org_gate.sign_in_refusal(session, target.org_id)
        if refusal is None and not org.enabled:
            from app.login_oauth import ORG_NOT_APPROVED

            refusal = ORG_NOT_APPROVED
        if refusal is None:
            refusal = entry_refusal(session, target, source_ip(request))
        if refusal is not None:
            return SwitchOutcome("refused", reason=refusal)
        amr = set(claims.amr)
        door = _first_factor_door(session, target, org, amr)
        if door is not None:
            return SwitchOutcome("reauth_required", door=door)
        if not (amr & _PROVEN_SECOND_FACTOR) and mfa.skip_reason(session, target) is None:
            amr_first = next((a for a in ("pwd", "oauth") if a in amr), None)
            if amr_first is not None and not mfa.is_enrolled(session, target):
                # No factor to type: enrol through the sign-in's own pending flow (`complete_first_factor`),
                # whose confirm mints a new sign-in of `target` (its own `iat`) with `amr` (first factor, otp).
                nonce = mfa.set_pending_cookie(response, target, "switch", amr_first)
                mfa.bind_pending_nonce(session, target, nonce)
                session.commit()
                return SwitchOutcome("reauth_required", door="enrol")
            proven = None
            device = None if code else mfa.use_trusted_device(session, target, request.cookies.get(mfa.DEVICE_COOKIE))
            if device is not None:
                # This browser is trusted by the person (`mfa.trust_device`): no code, as at a sign-in, and the same
                # `mfa_skipped` trace a sign-in on it leaves (SOC 2 G-01) — a factor's trace, not a switch event.
                record_write(
                    session, target, "mfa_skipped", f"{target.email} switched in without a code on trusted {device.label}",
                    data={"method": "switch", "amr": [amr_first, "device"], "device": device.label, "skip": None},
                )
                proven = "device"
            elif code:
                check = mfa.verify_current_code(session, target, code, allow_recovery=True, sign_in=True)
                if not check.ok:
                    # The failure count the check staged is kept: it is what throttles guessing.
                    session.commit()
                    return SwitchOutcome(
                        "reauth_required", door="otp", error="mfa_throttled" if check.throttled else "bad_factor"
                    )
                proven = check.amr
                if proven == "recovery":
                    # The factor's trace, as a sign-in leaves it (`routes/mfa.py::mfa_verify`) — not a switch event.
                    record_write(session, target, "mfa_recovery_used", f"{target.email} used a recovery code")
            if proven is None:
                return SwitchOutcome("reauth_required", door="otp")
            amr.add(proven)
    from app import events

    events.emit_session_switched(session, target.identity_id, claims.iat)
    _mint(session, response, target, tuple(sorted(amr)), iat=claims.iat)
    return SwitchOutcome("session")


def switch_target(session: Session, user: AdminUser, org_id: UUID) -> Optional[AdminUser]:
    """The membership an IdP door's org switch (`?switch_to=` / `switch_to`) signs in instead of
    `pick_membership`'s choice: `user`'s person's live row in `org_id` whose org may be signed into, or None
    (the door refuses). The door
    then runs `complete_first_factor` for THAT row, so the target org's whole policy applies (a fresh `iat`)."""
    row = identities.membership_in(session, user.identity_id, org_id)
    if row is None or row.is_system or row.disabled_at is not None:
        return None
    # The org gate `switch_membership` applies: a pending, rejected or disabled org is not switched into.
    with seam.identity_scope():
        org = session.get(Org, org_id)
        if org is None or not org.enabled or org_gate.sign_in_refusal(session, org_id) is not None:
            return None
    return row


def announce_replaced_session(session: Session, request: Request, user: AdminUser) -> None:
    """After a sign-in minted a session for `user` — an IdP door's org switch, or the second factor that
    completes one (`routes/mfa.py`): the browser's PREVIOUS session (the cookie this request carried), when it
    was the same person's, has been replaced — so its open streams close (`events.SESSION_SWITCHED`) as a
    `switch_membership` would close them. Commits the notification."""
    claims = auth.read_session(request.cookies.get(auth.COOKIE_NAME))
    if claims is None or user.identity_id is None:
        return
    with seam.identity_scope():
        previous = session.get(AdminUser, claims.admin_id)
    if previous is None or previous.identity_id != user.identity_id:
        return
    from app import events

    events.emit_session_switched(session, user.identity_id, claims.iat)
    session.commit()


def source_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def record_login_failure(
    session: Session,
    request: Request,
    user: Optional[AdminUser],
    *,
    method: str,
    reason: str,
    identifier: Optional[str] = None,
) -> None:
    """Record a refused sign-in as a `login_failed` activity event, count it toward the lockout
    (`login_lockout.record`), write the `app.security` line, and commit, so the refusal the caller is about
    to raise cannot roll it back. Every refused human sign-in calls this, on every door.

    `user` is the refused row when the door resolved one; otherwise `identifier` is what the attempt named
    (an unknown email, a provider's email), or None when it named nobody (a forged callback). Actorless (a
    refused sign-in authenticated nobody), filed under the refused user's org — `activity_events.org_id` is
    NOT NULL and their org's administrators are who should read it — or `UNATTRIBUTED_SIGN_IN_ORG_ID` when
    there is no user.

    Commits whatever the caller staged with it. On the provider doors that is the identity link the factor
    just verified, kept as the rejected-org refusal keeps it: it is that row's own proven identity, and it
    grants nothing while the row is disabled."""
    if user is not None:
        identifier = user.email
    elif identifier is not None:
        identifier = login_lockout.normalized(identifier) or None
    ip = source_ip(request)
    # The browser and ALB chain it came through (`request_caller.client_headers`).
    caller = client_headers(request)
    org_id = user.org_id if user is not None else UNATTRIBUTED_SIGN_IN_ORG_ID
    record_write(
        session,
        None,
        "login_failed",
        f"Sign-in refused ({reason}) via {method}",
        data={"source_ip": ip, "identifier": identifier, "method": method, "reason": reason, **caller},
        org_id=org_id,
    )
    for engaged in login_lockout.record(session, identifier, ip, reason):
        _record_lock(session, identifier, ip, engaged, org_id)
    session.commit()
    security_log.info(
        json.dumps(
            {
                "event": "login_failed",
                "method": method,
                "reason": reason,
                "ip": ip,
                # Not `client_ip`: `ip` is already the ALB's hop (`source_ip`).
                "user_agent": caller["user_agent"],
                "forwarded_for": caller["forwarded_for"],
                "identifier_hash": login_lockout.identifier_hash(identifier) if identifier else None,
            }
        )
    )


def _record_lock(session: Session, identifier: str, ip: Optional[str], engaged: str, org_id) -> None:
    """The `login_locked` event: the failure just recorded engaged the lock (this source) or the step-up
    (every source). Once per engagement: refusals the lockout answers are not counted, so cannot re-engage."""
    scope = "source_ip" if engaged == login_lockout.LOCKED else "all_sources"
    record_write(
        session,
        None,
        "login_locked",
        f"Sign-in {'locked' if scope == 'source_ip' else 'step-up required'} after repeated failures",
        data={"source_ip": ip, "identifier": identifier, "scope": scope},
        org_id=org_id,
    )


def reissue_session(request: Request, response, user: AdminUser) -> None:
    """Re-mint the caller's own live session for `user` at the row's AND the identity's current epochs,
    keeping the session's `iat`, `amr` and Terms claim. A no-op when the request carries no live session for this user
    (fails closed: nothing is minted that a sign-in did not start)."""
    # The person's own row, never an acting operator's seat copy (`act_as.person`): its epochs are the live ones.
    user = act_as.person(user)
    claims = auth.read_session(request.cookies.get(auth.COOKIE_NAME))
    if claims is None or claims.admin_id != user.id:
        return
    auth.set_session_cookie(
        response, user.id, user.session_epoch or 0, iepoch=auth.identity_epoch(user),
        iat=claims.iat, amr=claims.amr, terms=claims.terms,
    )


def release_terms_gate(request: Request, response, user: AdminUser) -> None:
    """Re-mint the caller's own live session WITHOUT the Terms claim, once the person accepted
    (`routes/legal_acceptance.py`) — the one writer that drops it. Otherwise `reissue_session`: the epochs
    current, `iat` and `amr` kept, and a no-op without a live session of this user's."""
    user = act_as.person(user)
    claims = auth.read_session(request.cookies.get(auth.COOKIE_NAME))
    if claims is None or claims.admin_id != user.id:
        return
    auth.set_session_cookie(
        response, user.id, user.session_epoch or 0, iepoch=auth.identity_epoch(user),
        iat=claims.iat, amr=claims.amr,
    )


def _row_is_live(claims: auth.SessionClaims) -> bool:
    """Whether the cookie's row still holds this session — read only when a re-mint is due (at most once
    per client per refresh interval), so a dead cookie is never refreshed and a slow request cannot
    re-mint a retired epoch over the cookie a password change or a new sign-in just set. A session its
    org's HIPAA idle rule ended is dead too: a public route's 200 must not revive it."""
    # Identity escape: this read resolves who the cookie names (`auth._session_user`'s reason). The identity
    # lane (`app/db.py`): a middleware read beside the route's own session.
    with IdentitySessionLocal() as session:
        with seam.identity_scope():
            row = session.get(AdminUser, claims.admin_id)
            if not auth.session_row_is_live(row, claims.epoch, claims.iepoch):
                return False
        # Outside the escape, which declares exactly what it reads (`orgs` is not tenant-owned).
        return not auth.session_idled_out(None, session, row, claims.issued_at)


# A request no person caused: the console marks a request sent after a quiet spell with no input
# (`apps/web/src/lib/activity.ts`), and the event stream's (re)connects are the browser's own. Neither is
# activity, so neither slides the idle window — else an unattended tab polling every minute never idles out.
ACTIVITY_HEADER = "x-activity"
_NOT_ACTIVITY_PATHS = frozenset({"/api/stream"})


def _is_background(request: Request) -> bool:
    return request.headers.get(ACTIVITY_HEADER) == "background" or request.url.path in _NOT_ACTIVITY_PATHS


def _sets_session_cookie(response) -> bool:
    prefix = f"{auth.COOKIE_NAME}=".encode()
    return any(key == b"set-cookie" and value.startswith(prefix) for key, value in response.raw_headers)


def _refresh_after(request: Request) -> float:
    """The refresh interval of this request's session, in seconds: `hipaa.REFRESH_AFTER` when the route's
    session check found its org HIPAA ON (`auth.session_idled_out`), else `SESSION_REFRESH_AFTER`. A slide
    as slow as the 15-minute HIPAA idle window would sign active people out mid-work."""
    if getattr(request.state, "session_hipaa", False):
        return hipaa.REFRESH_AFTER.total_seconds()
    return auth.SESSION_REFRESH_AFTER


class SessionRefreshMiddleware(BaseHTTPMiddleware):
    """The sliding idle window: re-mint a live cookie signed more than its refresh interval ago
    (`_refresh_after`), keeping its `iat`, `amr` and epoch. Never on every request (at most once per refresh
    interval), never on a refused response (status >= 400), never for a background request
    (`_is_background`), never over a cookie the route itself set or cleared, and never for a row that no
    longer holds the session."""

    async def dispatch(self, request: Request, call_next):
        # Touched first so the state dict exists before the route runs: the route records the session's
        # HIPAA verdict on it, and a dict created downstream would not be the one read here.
        request.state.session_hipaa = False
        response = await call_next(request)
        claims = auth.read_session(request.cookies.get(auth.COOKIE_NAME))
        if (
            claims is None
            or auth._now() - claims.issued_at < _refresh_after(request)
            or response.status_code >= 400
            or _is_background(request)
            or _sets_session_cookie(response)
            # Jarvis reading for the user is not the user being active: no slide.
            or auth.assisted(request)
            or not await run_in_threadpool(_row_is_live, claims)
        ):
            return response
        auth.set_session_cookie(
            response, claims.admin_id, claims.epoch, iepoch=claims.iepoch, iat=claims.iat, amr=claims.amr,
            terms=claims.terms,
        )
        return response
