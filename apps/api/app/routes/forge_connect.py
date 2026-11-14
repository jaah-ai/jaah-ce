"""Connect a forge (GitHub, GitLab, Bitbucket, Azure DevOps) on the shared Connect engine: the forges' hooks
(`CONNECT_HOOKS`), their callback paths, and the one home of whether each
root offers a Connect button at all (`connect_offer`).

The flow is the one-click engine's (`routes/integrations.py`: `POST /api/integrations/{forge}/connect/start`, PKCE,
state, the sealed pending choice, the exchange) and the row `forge_oauth.on_grant`'s. What a forge keeps is here:
the app it runs on (GitHub's App or OAuth App, `git_access.app_config`; the others' `git_access.forge_app_config`,
Entra's tenant included), the callback path its app registered — `/api/git-connect/callback` for GitHub
(`routes/git_connect.py`), `/api/git-connect/{forge}/callback` for the others, both handed to
`integrations.finish_hooked_callback`, so moving onto the engine changed nothing at any forge — Entra's tenant
consent refusal, the owners refresh and GitHub's installation carry-forward, and the landing page: Profile's Git tab
or the create wizard's popup (`git_connect.RETURN_PATHS`), with `?git_connect=` and a slug of `git_connect._ERRORS`.

Paths sit under `/api/git-connect`, which
`app/user_activity.py` and `apps/web/nginx.conf.template` keep out of every log (the callback's query carries a code).
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import ce_caps, edition_features
from app import forge_oauth, forge_repos, forges, git_access, github_api, github_oauth, oauth2_wire
from app.authz.declarations import feature
from app.authz.principal import AsyncCaller, current_caller
from app.db_async import DbDep
from app.integrations.connect import ClientRefusal, ConnectClient, ConnectHooks, PendingConnect
from app.models.identity import AdminUser
from app.oauth_providers import OAuthProvider
from app.routes import git_connect
from app.routes.integrations import HookedCallbackQuery, finish_hooked_callback
from app.tenancy.ids import is_operator_user

log = logging.getLogger("app.forge_connect")

router = APIRouter(tags=["git-connect"])

#: The forges whose callback is `/api/git-connect/{forge}/callback` (GitHub's is `routes/git_connect.py`'s).
CONNECT_FORGES: tuple[str, ...] = tuple(forge for forge in forge_oauth.FORGE_OAUTH if forge != "github")

# Entra's consent and tenant blocks, as they arrive in `error_description` (the redirect's `error` is
# usually a plain `access_denied`, the same as a person declining, AADSTS65004): consent not granted,
# admin consent required, the user not in the tenant, the app not in the tenant, Conditional Access, the
# user not assigned. Matched, never echoed or logged.
_ENTRA_BLOCK = re.compile(r"AADSTS(65001|90094|900941|90099|50020|700016|53003|50105)\b")

_SELF_MANAGED_REASON = (
    "A self-managed GitLab connects with a personal access token only; there is no Connect button for it."
)

_UNCONFIGURED = "{label} connect is not set up. Ask an operator."


def connect_offer(root: str, user: AdminUser) -> tuple[bool, Optional[str]]:
    """Whether `root` offers `user` a working Connect button, and if not, the sentence saying why.

    The single home for Connect availability: the start's refusal and the Git Access page's
    `connect.reason` both read it, so the two can never disagree. The operator plane reads the setting to
    fill; everyone else a sentence for a person. Never opens a secret. An edition without `forge_connect` offers
    none (`_prepare` refuses every forge's start, GitHub's included, so no callback has a flow to finish)."""
    if not edition_features.enabled("forge_connect"):
        return False, ce_caps.NOT_IN_CE
    forge = forges.forge_of_root(root)
    if forge is None or root not in forges.FIXED_ROOTS:
        return False, _SELF_MANAGED_REASON
    error = _app_error(forge)
    if error is None:
        return True, None
    if is_operator_user(user):
        return False, str(error)
    return False, _UNCONFIGURED.format(label=forge_oauth.FORGE_OAUTH[forge].label)


def _app_error(forge: str) -> Optional[git_access.GitAccessError]:
    """Why `forge`'s Connect app cannot run (GitHub's App or OAuth App, either kind), or None when it can."""
    try:
        if forge == "github":
            git_access.app_config(open_client_secret=False)
        else:
            git_access.forge_app_config(forge, open_client_secret=False)
    except git_access.GitAccessError as exc:
        return exc
    return None


def _app_available(provider: OAuthProvider) -> bool:
    """The catalog's `app_configured` for a forge: the same answer `connect_offer` gives."""
    return edition_features.enabled("forge_connect") and _app_error(provider.key) is None


# ── The hooks (`integrations.connect.ConnectHooks`) ──────────────────────────────────────────────────────


def _prepare(pending: PendingConnect, user: AdminUser) -> PendingConnect:
    """The start's forge rules: a forge connection is the person's own (personal, no project, Jaah's app), the page
    to come back to is allow-listed, and GitHub's attempt is pinned to the app kind it authorizes against (a code is
    bound to the client that issued it, so the callback must exchange with the same one). Refusals name what to fix:
    GitHub's missing app is a 503 (an operator setting), another forge's a 409 with `connect_offer`'s sentence; an
    edition without `forge_connect` a 404 for every forge."""
    spec = forge_oauth.FORGE_OAUTH[pending.integration]
    if not edition_features.enabled("forge_connect"):
        raise ce_caps.not_in_edition()
    if pending.owner != "personal" or pending.project_ids:
        raise HTTPException(
            status_code=400, detail=f"A {spec.label} connection is your own: it is personal and attached to no project."
        )
    if pending.byo:
        raise HTTPException(status_code=400, detail=f"{spec.label} connects through Jaah's app only.")
    return_to = git_connect.allowed_return(pending.return_to)
    if spec.forge != "github":
        available, reason = connect_offer(spec.root, user)
        if not available:
            raise HTTPException(status_code=409, detail=reason)
        return replace(pending, return_to=return_to, app="")
    try:
        app = git_access.app_config(kind=git_access.known_app_kind(pending.app), open_client_secret=False)
    except git_access.GitAccessError as exc:
        detail = str(exc) if is_operator_user(user) else _UNCONFIGURED.format(label=spec.label)
        raise HTTPException(status_code=503, detail=detail) from exc
    return replace(pending, return_to=return_to, app=app.kind)


def _client(provider: OAuthProvider, pending: PendingConnect) -> ConnectClient:
    """The forge app the attempt runs on, its secret opened: GitHub's chosen kind (with the account picker: without
    it GitHub silently re-approves whichever account is signed in), or the forge's app with Entra's tenant filled."""
    spec = forge_oauth.FORGE_OAUTH[provider.key]
    unconfigured = ClientRefusal("not_configured", 409, _UNCONFIGURED.format(label=spec.label))
    if spec.forge == "github":
        try:
            app = git_access.app_config(kind=git_access.known_app_kind(pending.app))
        except git_access.GitAccessError:
            raise unconfigured from None
        return ConnectClient(
            client_id=app.client_id, client_secret=app.client_secret,
            authorize_extra={"prompt": "select_account"}, accept=github_api.GITHUB_ACCEPT,
        )
    try:
        forge_app = git_access.forge_app_config(spec.forge)
    except git_access.GitAccessError:
        log.warning("forge connect: an attempt on %s found no usable app", spec.forge)
        raise unconfigured from None
    authorize, token = forge_oauth.endpoints(spec, forge_app)
    return ConnectClient(
        client_id=forge_app.client_id, client_secret=forge_app.client_secret,
        authorize_endpoint=authorize, token_endpoint=token,
    )


def _refusal(provider: OAuthProvider, slug: str, text: str) -> str:
    """Entra's tenant blocks (a decline or a refused exchange whose text names one) as `consent_blocked`, whose fix
    is a tenant administrator's consent, not the person's; anything else stays `slug`. `text` is matched only."""
    if provider.key != "azure_devops":
        return slug
    error = text.split(" ", 1)[0]
    blocked = error == "consent_required" or _ENTRA_BLOCK.search(text) is not None
    if blocked:
        log.info("forge connect: azure_devops refused by tenant consent")
    return "consent_blocked" if blocked else slug


def _after_grant(
    session: Session, user: AdminUser, provider: OAuthProvider, grant: oauth2_wire.TokenGrant,
    pending: PendingConnect, credential_id: UUID,
) -> Optional[RedirectResponse]:
    """Once the row is committed: refresh which owners it reaches (`forge_repos.refresh_owners`), then GitHub's
    installation carry-forward — a GitHub App connection reaching no installation of its own goes to the App's
    install page (`git_connect.needs_installation`), with the page to come back to kept for `/setup`."""
    spec = forge_oauth.FORGE_OAUTH[provider.key]
    joined = git_connect.joined_user(session, user)
    forge_repos.refresh_owners(session, joined, spec.root, credential_id)
    # The token says which app minted it (`on_grant` already held it to the attempt's app).
    if spec.forge != "github" or git_access.token_app_kind(grant.access_token) != "github_app":
        return None
    app = git_access.app_config(kind="github_app", open_client_secret=False)
    login = github_oauth.clean_login(pending.account or None)
    if not git_connect.needs_installation(session, app.slug, login, grant.access_token):
        return None
    return git_connect.install_redirect(app.slug, pending.return_to)


def _finish(slug: str, pending: Optional[PendingConnect], error: Optional[str]) -> RedirectResponse:
    """Back to the page the attempt came from (Profile's Git tab by default), saying how it ended and naming the
    forge (GitHub, the page's default, is not named); a slug outside the Git return's vocabulary is a programming
    error, raised."""
    return_to = git_connect.allowed_return(pending.return_to if pending is not None else None)
    tags = {} if slug == "github" else {"forge": slug}
    response = git_connect.return_redirect(return_to, error=error, **tags)
    # An earlier install hop's page must not outlive this attempt (`/setup` reads it).
    git_connect.clear_return(response)
    return response


def _redirect_path(provider: OAuthProvider) -> str:
    """The callback path the forge's app registered: GitHub's predates the per-forge paths."""
    return git_connect.CALLBACK_PATH if provider.key == "github" else f"/api/git-connect/{provider.key}/callback"


CONNECT_HOOKS = ConnectHooks(
    redirect_path=_redirect_path,
    cookie_path=git_connect.FLOW_COOKIE_PATH,
    prepare=_prepare,
    client=_client,
    refusal=_refusal,
    after_grant=_after_grant,
    finish=_finish,
    app_available=_app_available,
)


# ── The routes ─────────────────────────────────────────────────────────────────────────────────────────


@router.get("/api/git-connect/{forge}/callback")
@feature("credentials", "create", own_credential=True)
async def forge_connect_callback(
    forge: str,
    request: Request,
    db: DbDep,
    # `error_description` is Entra's reason text, matched against `_ENTRA_BLOCK`.
    query: HookedCallbackQuery = Depends(HookedCallbackQuery),
    caller: AsyncCaller = Depends(current_caller),
) -> RedirectResponse:
    """GitLab's, Bitbucket's and Azure DevOps' registered redirect target, finished by the shared engine."""
    if forge not in CONNECT_FORGES:
        raise HTTPException(status_code=404, detail="no Connect flow for that forge")

    return await finish_hooked_callback(request, db, slug=forge, **vars(query), user=caller.actor, principal=caller.principal)

