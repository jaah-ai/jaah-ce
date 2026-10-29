"""Connect GitLab / Bitbucket / Azure DevOps — the per-forge OAuth round trip (D-A6/D-A7),
and the one home of whether each root offers a Connect button at all (`connect_offer`).

`POST /api/git-connect/{forge}/start` and `GET /api/git-connect/{forge}/callback`. The GitHub flow keeps
its own module and its byte-identical legacy paths (`routes/git_connect.py`); `github` here only
aliases that flow's `/start`, since GitHub's registered callback is the legacy path. The mechanism is
the GitHub flow's: a signed per-forge flow cookie (`app/oauth_flow_cookie.py`) carrying state, the PKCE
verifier (none for Bitbucket, O7) and the user id, all checked before the code is spent; failures
redirect to Profile with a slug from the closed `_ERRORS`. The wire is `app/forge_oauth.py`, the app
row `git_access.forge_app_config`. Paths sit under `/api/git-connect`, which `app/user_activity.py` and
`apps/web/nginx.conf.template` already keep out of every log (the callback's query carries a code).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import forge_oauth, forges, git_access, git_connections, login_funnel, oauth2_wire, oauth_flow_cookie
from app.actors import stamp_actor
from app.auth import require_user
from app.authz.declarations import feature
from app.authz.principal import Principal, current_principal
from app.db import get_session
from app.models.credentials import Credential
from app.models.identity import AdminUser
from app.oauth_flow_cookie import FlowCookie
from app.routes import git_connect
from app.tenancy.ids import is_operator_plane
from app.user_activity import record_write

log = logging.getLogger("app.forge_connect")

router = APIRouter(tags=["git-connect"])

#: The forges this module runs a round trip for (GitHub's is `routes/git_connect.py`).
CONNECT_FORGES: tuple[str, ...] = tuple(forge for forge in forge_oauth.FORGE_OAUTH if forge != "github")

# One cookie per forge: its own salt, so an attempt started for one forge cannot be replayed on
# another's callback, and its own path, the narrowest both doors share. Bitbucket carries no verifier.
_FLOWS: dict[str, FlowCookie] = {
    forge: FlowCookie(
        suffix=f"_git_connect_{forge}",
        path=f"/api/git-connect/{forge}",
        salt=f"git-connect-flow-{forge}",
        members=(
            ("state", "verifier", "user_id") if forge_oauth.FORGE_OAUTH[forge].pkce
            else ("state", "user_id")
        ),
    )
    for forge in CONNECT_FORGES
}

# The closed vocabulary of everything the browser is told; no forge text reaches a redirect.
_ERRORS = oauth_flow_cookie.closed_errors({
    "not_configured": "no app for this forge is designated in Settings",
    "expired": "the connect attempt was not finished within ten minutes, or its cookie is gone",
    "state_mismatch": "the callback's state did not match the one this browser started with",
    "wrong_user": "the connect attempt belongs to a different signed-in user",
    "denied": "the authorization was declined at the forge",
    "no_code": "the forge returned no authorization code",
    "exchange_failed": "the forge refused to exchange the authorization code",
    "consent_blocked": (
        "the Microsoft Entra tenant will not let this app sign in until a tenant administrator consents to "
        "it, once for the whole tenant (or the account is outside the app's tenant) — ask one to grant "
        "consent, or paste a personal access token instead"
    ),
})

# Entra's consent and tenant blocks, as they arrive in `error_description` (the redirect's `error` is
# usually a plain `access_denied`, the same as a person declining, AADSTS65004): consent not granted,
# admin consent required, the user not in the tenant, the app not in the tenant, Conditional Access, the
# user not assigned. Matched, never echoed or logged.
_ENTRA_BLOCK = re.compile(r"AADSTS(65001|90094|900941|90099|50020|700016|53003|50105)\b")

_SELF_MANAGED_REASON = (
    "A self-managed GitLab connects with a personal access token only; there is no Connect button for it."
)


def connect_offer(root: str, user: AdminUser) -> tuple[bool, Optional[str]]:
    """Whether `root` offers `user` a working Connect button, and if not, the sentence saying why.

    The single home for Connect availability: `/start`'s 409 and the Git Access page's
    `connect.reason` both read it, so the two can never disagree. The operator plane reads the setting to
    fill; everyone else a sentence for a person, as the GitHub flow's `/start` does. Never opens a secret."""
    forge = forges.forge_of_root(root)
    if forge is None or root not in forges.FIXED_ROOTS:
        return False, _SELF_MANAGED_REASON
    try:
        if forge == "github":
            git_access.app_config(open_client_secret=False)
        else:
            git_access.forge_app_config(forge, open_client_secret=False)
    except git_access.GitAccessError as exc:
        if is_operator_plane(user.org_id, user.is_superadmin):
            return False, str(exc)
        return False, f"{forge_oauth.FORGE_OAUTH[forge].label} connect is not set up. Ask an operator."
    return True, None


def _spec_or_404(forge: str) -> forge_oauth.ForgeOAuthSpec:
    if forge not in _FLOWS:
        raise HTTPException(status_code=404, detail="no Connect flow for that forge")
    return forge_oauth.FORGE_OAUTH[forge]


def _redirect_uri(forge: str) -> str:
    """The callback URL registered at the forge's app (`git_connect.console_url` says why not the request)."""
    return git_connect.console_url(f"/api/git-connect/{forge}/callback")


def _profile_redirect(forge: str, *, error: Optional[str] = None) -> RedirectResponse:
    """`git_connect.finish_redirect` to Profile, naming the forge, clearing the attempt cookie on every exit."""
    response = git_connect.finish_redirect("/profile", _ERRORS, error=error, forge=forge)
    _FLOWS[forge].clear(response)
    return response


@router.post("/api/git-connect/{forge}/start", response_model=git_connect.GitConnectStartOut)
@feature("credentials", "create", own_credential=True)
def start_forge_connect(
    forge: str,
    request: Request,
    response: Response,
    # GitHub's `return_to` (`git_connect.GitConnectStartIn`); the other forges always return to Profile.
    body: Optional[git_connect.GitConnectStartIn] = Body(default=None),
    user: AdminUser = Depends(require_user),
) -> git_connect.GitConnectStartOut:
    """Begin a connect attempt for `forge`: 409 with `connect_offer`'s sentence when its app is not
    configured, else mint state (and PKCE where the forge has it), stash them, return where to go.
    Self-service and write-free, as the GitHub flow's `/start`."""
    if forge == "github":
        # The alias (D-A7): GitHub's own flow, byte for byte.
        return git_connect.start_git_connect(request, response, body, user)
    spec = _spec_or_404(forge)
    available, reason = connect_offer(spec.root, user)
    if not available:
        raise HTTPException(status_code=409, detail=reason)
    try:
        app = git_access.forge_app_config(forge)
    except git_access.GitAccessError as exc:
        # The secret could not be opened after the offer said usable: the same sentence split.
        detail = (
            str(exc) if is_operator_plane(user.org_id, user.is_superadmin)
            else f"{spec.label} connect is not set up. Ask an operator."
        )
        raise HTTPException(status_code=409, detail=detail) from exc
    state = oauth2_wire.new_state()
    payload: dict[str, object] = {"state": state, "user_id": user.id}
    challenge = ""
    if spec.pkce:
        pkce = oauth2_wire.new_pkce_pair()
        payload["verifier"] = pkce.verifier
        challenge = pkce.challenge
    url = forge_oauth.authorize_url(
        spec, app, redirect_uri=_redirect_uri(forge), state=state, challenge=challenge
    )
    _FLOWS[forge].set(response, payload)
    # As the GitHub flow: upgrades an old `SameSite=Strict` session cookie so the cross-site callback is sent it.
    login_funnel.reissue_session(request, response, user)
    log.info("forge-connect: user=%s started a %s attempt", user.id, forge)
    return git_connect.GitConnectStartOut(authorize_url=url)


@router.get("/api/git-connect/{forge}/callback")
@feature("credentials", "create", own_credential=True)
def forge_connect_callback(
    forge: str,
    request: Request,
    # Unconstrained defaulted `str`s: a pydantic 422 would echo the rejected input, a code.
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    # Entra's reason text; only matched against `_ENTRA_BLOCK`, never echoed or logged.
    error_description: str = Query(default=""),
    user: AdminUser = Depends(require_user),
    principal: Optional[Principal] = Depends(current_principal),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """The forge's redirect target: verify the attempt (cookie, state, user — before the code is spent),
    exchange, store the grant with `git_access.store_grant`, connect it for the forge's root
    (`git_connections.set_connection`), and go back to Profile."""
    spec = _spec_or_404(forge)
    flow, refusal = oauth_flow_cookie.verified_attempt(
        _FLOWS[forge], request,
        callback_state=state, user_id=user.id,
        log=log, flow_name=f"forge-connect:{forge}",
    )
    if refusal is not None:
        return _profile_redirect(forge, error=refusal)
    assert flow is not None  # narrowing: `verified_attempt` returns exactly one of the two
    if error:
        blocked = forge == "azure_devops" and (
            error == "consent_required" or _ENTRA_BLOCK.search(error_description or "") is not None
        )
        log.info("forge-connect: user=%s did not complete %s authorization (blocked=%s)",
                 user.id, forge, blocked)
        return _profile_redirect(forge, error="consent_blocked" if blocked else "denied")
    if not code:
        return _profile_redirect(forge, error="no_code")
    try:
        app = git_access.forge_app_config(forge)
    except git_access.GitAccessError:
        log.warning("forge-connect: user=%s reached the %s callback with no usable app", user.id, forge)
        return _profile_redirect(forge, error="not_configured")
    try:
        grant = forge_oauth.exchange_code(
            spec, app, code=code, redirect_uri=_redirect_uri(forge), verifier=flow.get("verifier", ""),
        )
    except oauth2_wire.OAuth2Error as exc:
        # `exc.code` is safe to log; `str(exc)` may carry the forge's text and reaches nothing.
        log.warning("forge-connect: user=%s %s exchange refused code=%s", user.id, forge, exc.code)
        blocked = forge == "azure_devops" and _ENTRA_BLOCK.search(str(exc)) is not None
        return _profile_redirect(forge, error="consent_blocked" if blocked else "exchange_failed")

    account = forge_oauth.fetch_account(spec, grant.access_token)
    credential = _connected_credential(session, user, principal, spec, account=account)
    # A fresh dict before the grant: a reconnect drops whatever a hand edit left, and `kind` tells the
    # username table this is an OAuth token (`git_access._GIT_LOGINS`).
    credential.config = {"kind": spec.token_kind} if spec.token_kind else {}
    git_access.store_grant(credential, grant)
    git_connections.stamp_account(credential, account)
    git_connections.set_connection(session, user, user, spec.root, credential)
    record_write(
        session, user, "git_connected",
        f"Connected {spec.label} account {account or '(name unavailable)'} to credential {credential.name}",
        data={"credential_id": credential.id, "login": account, "forge": forge},
    )
    session.commit()
    log.info("forge-connect: user=%s connected %s credential id=%s expiring=%s",
             user.id, forge, credential.id, grant.expires_at is not None)
    return _profile_redirect(forge)


def _connected_credential(
    session: Session,
    user: AdminUser,
    principal: Optional[Principal],
    spec: forge_oauth.ForgeOAuthSpec,
    *,
    account: Optional[str],
) -> Credential:
    """The row this grant belongs on: the caller's own Connect-made row of this forge for the spec's root and
    `account` (`git_connections.own_row_for_root`; a reconnect renews it, whatever it is called now), or a new
    personal row — the GitHub flow's rule (`git_connections.connect_github`)."""
    existing = git_connections.own_row_for_root(
        session, user, principal, spec.forge, spec.root, grant=True, account=account
    )
    if existing is not None:
        stamp_actor(existing, user)
        existing.description = _description(spec, account)
        return existing
    name = git_connections.free_name(session, user, git_connections.connection_name(
        spec.forge.replace("_", "-"), account or user.email or str(user.id)
    ))
    row = Credential(
        name=name, type=spec.forge, description=_description(spec, account), config={},
        # Always personal: this is the caller's own token (Profile > Git).
        owner_id=user.id,
        # Named, not left to the scope: the callback is a GET, where an operator reads on the fleet plane
        # (`authz._principal_scope`), which stamps nothing. `git_connect._connected_credential`'s twin.
        org_id=user.org_id,
    )
    stamp_actor(row, user, created=True)
    session.add(row)
    session.flush()
    return row


def _description(spec: forge_oauth.ForgeOAuthSpec, account: Optional[str]) -> str:
    who = f" as {account}" if account else ""
    return (
        f"Connected through the console's {spec.label} app{who}. The token and refresh token are "
        f"maintained automatically — reconnect from Profile > Git > {spec.label} rather than "
        "editing them."
    )
