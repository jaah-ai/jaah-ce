"""The per-forge OAuth Connect specs — one frozen `ForgeOAuthSpec` per forge that offers one-click
Connect, and the wire calls the three non-GitHub forges make.

GitHub's entry restates `github_oauth.py`'s constants and its flow keeps its own module and routes
(`routes/git_connect.py`, byte-identical, D-A7). The others go through `routes/forge_connect.py` and
`oauth2_wire.py`. Self-managed GitLab, Bitbucket Data Center and Azure DevOps Server have no entry: they
connect with a pasted token only (D4). Vendor values, checked against the vendors' docs:
  * GitLab.com — PKCE; `api read_user write_repository`; 2 h tokens whose refresh token rotates.
  * Bitbucket Cloud — no PKCE, so state alone binds the callback (O7); no `scope` parameter (scopes live
    on the consumer); the client pair only as HTTP Basic; `/2.0/user` has no `username`, so `nickname`.
  * Azure DevOps — a Microsoft Entra ID v2 app (D5): PKCE, the Azure DevOps resource's `.default` plus
    `offline_access` (no refresh token without it), tenant default `organizations`.
Nothing here logs a token, code, verifier or secret.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import NamedTuple, Optional
from uuid import UUID

import httpx

from app import github_oauth, http_bounded, oauth2_wire
from app.oauth2_wire import OAuth2Error, TokenGrant

log = logging.getLogger("app.forge_oauth")


class ForgeOAuthError(OAuth2Error):
    """A connect or refresh a non-GitHub forge refused; `code` is its `error` member, or None."""


@dataclass(frozen=True)
class ForgeOAuthSpec:
    """One forge's Connect, as data. `forge` is also the credential type the grant is stored as.

    `authorize_url`/`token_url` may carry `{tenant}` (Entra only). `refresh_dead_codes` are the token
    endpoint's `error` values after which only a reconnect helps (`git_access.GitAccessNeedsReconnect`).
    `account_url`/`account_field` name who connected, for the credential's name only. `app_setting` is
    the operator knob designating the `app_type` credential; `app_holds` names what that row carries,
    for the not-configured sentence. `token_kind` is the grant's `kind` field where the forge's username
    table has one (`git_access._GIT_LOGINS`). `prompt` is sent on authorize where the forge honours one,
    so a Connect shows its account picker rather than silently reusing the signed-in account; GitLab and
    Bitbucket document none. `section` is the Settings > Integrations section holding `app_setting` when
    it is not `label` (Entra's app also serves Microsoft sign-in, so it sits under "Microsoft")."""

    forge: str
    label: str
    root: str
    authorize_url: str
    token_url: str
    scope: Optional[str]
    pkce: bool
    refresh_dead_codes: frozenset[str]
    account_url: str
    account_field: str
    app_setting: str
    app_type: str
    app_holds: str
    basic_auth: bool = False
    token_kind: Optional[str] = None
    prompt: Optional[str] = None
    section: Optional[str] = None

    @property
    def settings_section(self) -> str:
        return self.section or self.label


FORGE_OAUTH: dict[str, ForgeOAuthSpec] = {
    "github": ForgeOAuthSpec(
        forge="github", label="GitHub", root="github.com",
        authorize_url=github_oauth.AUTHORIZE_URL, token_url=github_oauth.TOKEN_URL,
        # The classic OAuth App's scope; its authorize is `github_oauth.authorize_url`'s, which sends it. GitHub's
        # Connect and sign-in resolve their app through `git_access.APP_KNOBS` (GitHub App or OAuth App), not
        # `app_setting`/`app_type`, which name only the OAuth App for this table's shape.
        scope=github_oauth.SCOPE, pkce=True, refresh_dead_codes=frozenset({github_oauth.REFRESH_TOKEN_DEAD}),
        account_url=f"{github_oauth.API_URL}/user", account_field="login",
        app_setting="git_app_credential_id", app_type="github_app",
        app_holds="the OAuth App's Client ID and Client secret",
    ),
    "gitlab": ForgeOAuthSpec(
        forge="gitlab", label="GitLab", root="gitlab.com",
        authorize_url="https://gitlab.com/oauth/authorize", token_url="https://gitlab.com/oauth/token",
        scope="api read_user write_repository", pkce=True,
        refresh_dead_codes=frozenset({oauth2_wire.INVALID_GRANT}),
        account_url="https://gitlab.com/api/v4/user", account_field="username",
        app_setting="gitlab_app_credential_id", app_type="gitlab_app",
        app_holds="the application's Application ID and Secret",
    ),
    "bitbucket": ForgeOAuthSpec(
        forge="bitbucket", label="Bitbucket", root="bitbucket.org",
        authorize_url="https://bitbucket.org/site/oauth2/authorize",
        token_url="https://bitbucket.org/site/oauth2/access_token",
        scope=None, pkce=False, refresh_dead_codes=frozenset({oauth2_wire.INVALID_GRANT}),
        account_url="https://api.bitbucket.org/2.0/user", account_field="nickname",
        app_setting="bitbucket_app_credential_id", app_type="bitbucket_app",
        app_holds="the OAuth consumer's Key and Secret",
        basic_auth=True, token_kind="oauth",
    ),
    "azure_devops": ForgeOAuthSpec(
        forge="azure_devops", label="Azure DevOps", root="dev.azure.com",
        authorize_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        scope="499b84ac-1321-427f-aa17-267ca6975798/.default offline_access", pkce=True,
        # `interaction_required`: a tenant policy (MFA, Conditional Access) now wants the person present.
        refresh_dead_codes=frozenset({oauth2_wire.INVALID_GRANT, "interaction_required"}),
        account_url="https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1",
        account_field="emailAddress",
        app_setting="azure_devops_app_credential_id", app_type="azure_devops_app",
        app_holds="the Entra app registration's Application (client) ID and a client secret",
        token_kind="oauth",
        prompt="select_account",
        section="Microsoft",
    ),
}

#: The Entra tenant when the app row names none: any work or school account (never personal Microsoft
#: accounts, which Azure DevOps' Entra OAuth does not serve).
DEFAULT_TENANT = "organizations"

# A tenant is a GUID, a domain or one of Entra's keywords. A bound, not Entra's rule: it is operator text
# interpolated into a URL path, so `/`, `?` and `#` must not survive.
_TENANT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,98}[A-Za-z0-9])?")

# The account read is best effort: the user is waiting on a redirect, and a failure only costs a name.
_ACCOUNT_TIMEOUT_SECONDS = 15.0
_ACCOUNT_MAX_BYTES = 256 * 1024

# The seam tests replace (`http_bounded.client`'s redirect-off, capped-read policy).
_client = http_bounded.client


class ForgeApp(NamedTuple):
    """An operator-designated forge app, read out as plain scalars (`git_access.forge_app_config`)."""

    id: UUID
    name: str
    client_id: str
    client_secret: str
    tenant: str


def valid_tenant(raw: str) -> Optional[str]:
    """`raw` as an Entra tenant to put in a URL, `DEFAULT_TENANT` when blank, or None when malformed."""
    tenant = (raw or "").strip()
    if not tenant:
        return DEFAULT_TENANT
    return tenant if _TENANT.fullmatch(tenant) else None


def _endpoint(url: str, app: ForgeApp) -> str:
    return url.format(tenant=app.tenant) if "{tenant}" in url else url


def authorize_url(
    spec: ForgeOAuthSpec, app: ForgeApp, *, redirect_uri: str, state: str, challenge: str
) -> str:
    """Where the browser goes. `challenge` must be blank exactly when the forge has no PKCE."""
    if bool(challenge) != spec.pkce:
        raise ValueError(f"{spec.forge}: a PKCE challenge is {'required' if spec.pkce else 'not sent'}")
    return oauth2_wire.authorize_url(
        endpoint=_endpoint(spec.authorize_url, app),
        client_id=app.client_id,
        redirect_uri=redirect_uri,
        state=state,
        challenge=challenge,
        scope=spec.scope,
        extra={"prompt": spec.prompt} if spec.prompt else None,
    )


def exchange_code(
    spec: ForgeOAuthSpec, app: ForgeApp, *, code: str, redirect_uri: str, verifier: str
) -> TokenGrant:
    """Trade the callback's code for a grant (`redirect_uri` byte-identical to the authorize one)."""
    return oauth2_wire.exchange_code(
        endpoint=_endpoint(spec.token_url, app),
        client_id=app.client_id,
        client_secret=app.client_secret,
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
        vendor=spec.label,
        error_cls=ForgeOAuthError,
        basic_auth=spec.basic_auth,
    )


def refresh(spec: ForgeOAuthSpec, app: ForgeApp, refresh_token: str) -> TokenGrant:
    """Spend `refresh_token` for a new grant. GitLab and Entra rotate it, so the returned one must be
    persisted; a response carrying none keeps the one just spent, so the row stays renewable. Entra is
    re-sent its scope, so the renewed token stays for the Azure DevOps resource (Microsoft's v2 guidance)."""
    grant = oauth2_wire.refresh(
        endpoint=_endpoint(spec.token_url, app),
        client_id=app.client_id,
        client_secret=app.client_secret,
        refresh_token=refresh_token,
        scope=spec.scope if spec.forge == "azure_devops" else None,
        vendor=spec.label,
        error_cls=ForgeOAuthError,
        basic_auth=spec.basic_auth,
    )
    return grant if grant.refresh_token else replace(grant, refresh_token=refresh_token)


def fetch_account(spec: ForgeOAuthSpec, access_token: str) -> Optional[str]:
    """Who connected (`spec.account_field` of `spec.account_url`), or None — never a failure of the
    connect, since the code is already spent. Only a status or an exception class is logged."""
    try:
        with _client(
            _ACCOUNT_TIMEOUT_SECONDS,
            {"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        ) as client:
            with client.stream("GET", spec.account_url) as response:
                raw, truncated = http_bounded.read_capped(response, _ACCOUNT_MAX_BYTES)
                status = response.status_code
    except httpx.HTTPError as exc:
        # The class only: `str(exc)` renders the request URL.
        log.warning("forge-connect: %s account read failed (%s)", spec.forge, type(exc).__name__)
        return None
    payload = None if truncated else http_bounded.json_or_none(raw)
    if status != 200 or not isinstance(payload, dict):
        log.warning("forge-connect: %s account read answered HTTP %s", spec.forge, status)
        return None
    return github_oauth.clean_login(payload.get(spec.account_field))
