"""The per-forge OAuth Connect specs — one frozen `ForgeOAuthSpec` per forge that offers one-click
Connect — and the forges' extras on the shared Connect engine.

The wire (endpoints, scope, PKCE, client auth, `prompt`, the account read) is the forge's
`oauth_providers.PROVIDERS` entry; a spec adds only what a forge needs beyond it: the git host it serves, its
dead-grant codes, what its app row holds, its token `kind` and its Settings section. Every forge's Connect runs on
the one-click engine (`routes/integrations.py`): authorize, PKCE, state and the exchange are the engine's; the app
and the landing page are `routes/forge_connect.py::CONNECT_HOOKS`; the row is this module's `on_grant`: the person's
own forge credential, its installation discovery (GitHub's app kind and the replaced token given back) and the
per-user git pointer (`user_git_connections`). Renewal stays here (`refresh`). Self-managed GitLab, Bitbucket Data
Center and Azure DevOps Server have no entry: they connect with a pasted token only. Azure DevOps' endpoints carry
Entra's `{tenant}`, filled from the app row (`DEFAULT_TENANT` when it names none).
Nothing here logs a token, code, verifier or secret.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NamedTuple, Optional
from uuid import UUID

from app import github_oauth, oauth2_wire, oauth_providers
from app.oauth2_wire import OAuth2Error, TokenGrant
from app.oauth_providers import OAuthProvider

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.integrations.connect import GrantOutcome, PendingConnect
    from app.models.credentials import Credential
    from app.models.identity import AdminUser

log = logging.getLogger("app.forge_oauth")


class ForgeOAuthError(OAuth2Error):
    """A connect or refresh a non-GitHub forge refused; `code` is its `error` member, or None."""


@dataclass(frozen=True)
class ForgeOAuthSpec:
    """One forge's Connect, as data. `forge` is also the credential type the grant is stored as, and the key
    of its `provider` in `oauth_providers.PROVIDERS`, whose wire fields the properties below read out under
    the names callers use.

    `refresh_dead_codes` are the token endpoint's `error` values after which only a reconnect helps
    (`git_access.GitAccessNeedsReconnect`). `app_holds` names what the `app_type` row carries, for the
    not-configured sentence. `token_kind` is the grant's `kind` field where the forge's username table has
    one (`git_access._GIT_LOGINS`). `section` is the Settings > Integrations section holding `app_setting`
    when it is not `label` (Entra's app also serves Microsoft sign-in, so it sits under "Microsoft")."""

    forge: str
    root: str
    refresh_dead_codes: frozenset[str]
    app_holds: str
    token_kind: Optional[str] = None
    section: Optional[str] = None
    provider: OAuthProvider = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", oauth_providers.PROVIDERS[self.forge])

    @property
    def label(self) -> str:
        return self.provider.label

    @property
    def authorize_url(self) -> str:
        return self.provider.authorize_url or ""

    @property
    def token_url(self) -> str:
        return self.provider.token_url or ""

    @property
    def scope(self) -> Optional[str]:
        return self.provider.scope

    @property
    def pkce(self) -> bool:
        return self.provider.pkce

    @property
    def basic_auth(self) -> bool:
        return self.provider.basic_auth

    @property
    def prompt(self) -> Optional[str]:
        return (self.provider.authorize_extra or {}).get("prompt")

    @property
    def account_url(self) -> str:
        return self.provider.account_url or ""

    @property
    def account_field(self) -> str:
        return self.provider.account_field or ""

    @property
    def app_setting(self) -> str:
        return self.provider.app_setting or ""

    @property
    def app_type(self) -> str:
        return self.provider.app_type or ""

    @property
    def settings_section(self) -> str:
        return self.section or self.label


FORGE_OAUTH: dict[str, ForgeOAuthSpec] = {
    # GitHub's Connect and sign-in resolve their app through `git_access.APP_KNOBS` (GitHub App or OAuth App);
    # its provider's `app_setting`/`app_type` name only the OAuth App, for this table's shape.
    "github": ForgeOAuthSpec(
        forge="github", root="github.com",
        refresh_dead_codes=frozenset({github_oauth.REFRESH_TOKEN_DEAD}),
        app_holds="the OAuth App's Client ID and Client secret",
    ),
    "gitlab": ForgeOAuthSpec(
        forge="gitlab", root="gitlab.com",
        refresh_dead_codes=frozenset({oauth2_wire.INVALID_GRANT}),
        app_holds="the application's Application ID and Secret",
    ),
    "bitbucket": ForgeOAuthSpec(
        forge="bitbucket", root="bitbucket.org",
        refresh_dead_codes=frozenset({oauth2_wire.INVALID_GRANT}),
        app_holds="the OAuth consumer's Key and Secret",
        token_kind="oauth",
    ),
    "azure_devops": ForgeOAuthSpec(
        forge="azure_devops", root="dev.azure.com",
        # `interaction_required`: a tenant policy (MFA, Conditional Access) now wants the person present.
        refresh_dead_codes=frozenset({oauth2_wire.INVALID_GRANT, "interaction_required"}),
        app_holds="the Entra app registration's Application (client) ID and a client secret",
        token_kind="oauth",
        section="Microsoft",
    ),
}

#: The Entra tenant when the app row names none: any work or school account (never personal Microsoft
#: accounts, which Azure DevOps' Entra OAuth does not serve).
DEFAULT_TENANT = "organizations"

# A tenant is a GUID, a domain or one of Entra's keywords. A bound, not Entra's rule: it is operator text
# interpolated into a URL path, so `/`, `?` and `#` must not survive.
_TENANT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,98}[A-Za-z0-9])?")


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


def endpoints(spec: ForgeOAuthSpec, app: ForgeApp) -> tuple[str, str]:
    """`(authorize, token)` endpoints for `app`: the provider's pinned ones, Entra's `{tenant}` filled from the app."""
    return _endpoint(spec.authorize_url, app), _endpoint(spec.token_url, app)


def _endpoint(url: str, app: ForgeApp) -> str:
    return url.format(tenant=app.tenant) if "{tenant}" in url else url


def refresh(spec: ForgeOAuthSpec, app: ForgeApp, refresh_token: str) -> TokenGrant:
    """Spend `refresh_token` for a new grant. GitLab and Entra rotate it, so the returned one must be
    persisted; a response carrying none keeps the one just spent, so the row stays renewable. Entra is
    re-sent its scope, so the renewed token stays for the Azure DevOps resource (Microsoft's v2 guidance)."""
    grant = oauth_providers.refresh(
        spec.provider,
        client_id=app.client_id,
        client_secret=app.client_secret,
        refresh_token=refresh_token,
        endpoint=_endpoint(spec.token_url, app),
        scope=spec.scope if spec.forge == "azure_devops" else None,
        error_cls=ForgeOAuthError,
    )
    return grant if grant.refresh_token else replace(grant, refresh_token=refresh_token)


def fetch_account(spec: ForgeOAuthSpec, access_token: str) -> Optional[str]:
    """Who connected (the provider's account read) under `github_oauth.clean_login`'s bound, or None —
    never a failure of the connect, since the code is already spent."""
    return github_oauth.clean_login(oauth_providers.fetch_account(spec.provider, access_token))


# ── The post-exchange hook (`OAuthProvider.on_grant`) ─────────────────────────────────────────────────────────


def on_grant(session: "Session", provider: OAuthProvider, grant: TokenGrant, pending: "PendingConnect") -> "GrantOutcome":
    """Store a forge Connect's grant as the person's own forge credential and connect it for the forge's root
    (`git_connections.set_connection`, the `user_git_connections` pointer); flushes, the engine commits.

    A reconnect renews the person's own Connect-made row for the same forge account (`own_row_for_root`), whatever it
    is called now; otherwise a new personal row is named for the account. GitHub's grant must come from the app kind
    the attempt ran on (`git_access.minted_by`): a token of the other kind is given back and refused
    (`wrong_app_kind`); a GitHub reconnect gives the replaced token back once the new one is committed."""
    from app import git_access, git_connections
    from app.integrations.connect import GrantOutcome, GrantRefused, grant_context
    from app.routes.git_connect import joined_user
    from app.user_activity import record_write

    spec = FORGE_OAUTH[provider.key]
    # The request's own identity (an acting operator's seat copy included), never one re-read from the pending.
    context = grant_context()
    if context.user.id != pending.user_id:
        raise GrantRefused("wrong_user")
    user, principal = joined_user(session, context.user), context.principal
    account = github_oauth.clean_login(pending.account or None)
    if spec.forge == "github":
        _refuse_wrong_app_kind(pending, grant, context.client)
        connected = git_connections.connect_github(session, user, principal, grant, login=account, via="Connect GitHub")
        return GrantOutcome(
            credential=connected.credential,
            after_commit=lambda: git_connections.revoke_replaced(connected, grant),
        )
    credential = _connected_credential(session, user, principal, spec, account=account)
    # A fresh dict before the grant: a reconnect drops whatever a hand edit left, and `kind` tells the username
    # table this is an OAuth token (`git_access._GIT_LOGINS`).
    credential.config = {"kind": spec.token_kind} if spec.token_kind else {}
    git_access.store_grant(credential, grant)
    git_connections.stamp_account(credential, account)
    # Beside the person's other accounts, and their default when the old default's grant is dead (a legacy row a
    # reconnect could not renew).
    git_connections.set_connection(
        session, user, user, spec.root, credential,
        make_default=git_connections.default_grant_dead(session, user, spec.root),
    )
    record_write(
        session, user, "git_connected",
        f"Connected {spec.label} account {account or '(name unavailable)'} to credential {credential.name}",
        data={"credential_id": credential.id, "login": account, "forge": spec.forge},
    )
    return GrantOutcome(credential=credential)


def _refuse_wrong_app_kind(pending: "PendingConnect", grant: TokenGrant, client: Any) -> None:
    """`GrantRefused("wrong_app_kind")`, after giving the token back with the pair that spent the code (`client`),
    when the designated GitHub app the attempt ran on (`pending.app`, sealed at the start) holds the other kind's
    client pair. Judged against that kind and pair, never a re-read of the setting, which may have changed since."""
    from app import git_access
    from app.integrations.connect import GrantRefused

    if client is None:  # pragma: no cover - the engine always passes the client that spent the code
        raise GrantRefused("bad_request")
    kind = git_access.known_app_kind(pending.app)
    if kind is None:
        # An attempt whose kind was not pinned ran on "the one to use now", as the client did (`CONNECT_HOOKS.client`).
        try:
            kind = git_access.app_config(open_client_secret=False).kind
        except git_access.GitAccessError:
            kind = ""
    if git_access.token_app_kind(grant.access_token) == kind:
        return
    log.warning("forge connect: a token of the wrong kind for the designated %s was issued; token revoked, nothing "
                "stored", kind or "app")
    github_oauth.revoke_user_token(client_id=client.client_id, client_secret=client.client_secret,
                                   access_token=grant.access_token)
    raise GrantRefused("wrong_app_kind")


def _connected_credential(
    session: "Session", user: "AdminUser", principal: Any, spec: ForgeOAuthSpec, *, account: Optional[str],
) -> "Credential":
    """The row this grant belongs on: the caller's own Connect-made row of this forge for the spec's root and
    `account` (`git_connections.own_row_for_root`; a reconnect renews it, whatever it is called now), or a new
    personal row — the GitHub flow's rule (`git_connections.connect_github`)."""
    from app import git_connections
    from app.actors import stamp_actor
    from app.models.credentials import Credential

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
        # Always personal: this is the caller's own token (Profile > Git). The org is named, not left to the scope:
        # the callback is a GET, where an operator reads on the fleet plane, which stamps nothing.
        owner_id=user.id, org_id=user.org_id,
    )
    stamp_actor(row, user, created=True)
    session.add(row)
    session.flush()
    return row


def _description(spec: ForgeOAuthSpec, account: Optional[str]) -> str:
    who = f" as {account}" if account else ""
    return (
        f"Connected through the console's {spec.label} app{who}. The token and refresh token are "
        f"maintained automatically — reconnect from Profile > Git > {spec.label} rather than "
        "editing them."
    )
