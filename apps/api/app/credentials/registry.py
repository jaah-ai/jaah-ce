"""The credential type registry — the single home for what types exist and what fields each has.

Validators, the serializer's secret-stripping and the form schema (`GET /api/credentials/types`) all
derive from here, so adding a type is one edit here and none in TypeScript. `credentials.type` is
validated against `CREDENTIAL_TYPES` app-side (no DB enum), so a new type needs no migration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

FieldKind = Literal["text", "url", "email", "secret"]


@dataclass(frozen=True)
class FieldSpec:
    """One configuration field of one credential type.

    `kind` is also the security classification: `secret` feeds `secret_keys()`, the only list of
    sensitive keys, so a field mis-typed as `text` ships to the browser.
    """

    key: str
    label: str
    kind: FieldKind
    required: bool
    placeholder: Optional[str] = None
    # Pre-filled on a NEW row when its type is chosen (served by `routes/credentials/credential_wire.py::FieldSpecOut`);
    # never applied to a saved row, whose blank is the operator's. Non-secret fields only.
    default: Optional[str] = None
    # Helper text under the input: an instruction, where `placeholder` is only ever an example.
    help: Optional[str] = None


@dataclass(frozen=True)
class KeyTestSpec:
    """A Test that is one GET carrying the key in one header, described as data rather than a function.

    `testers.py::_declared_tester` performs it and `testers.py::_check_tester_registry` checks it at import.
    `accept=None` asks for no container and is not shape-gated (a 2xx is a green). `field_headers` pairs a
    header name with a config key; that header is sent only when its field is non-blank. On a gated 2xx,
    `list_field` names a key that must hold a list (a bare "credentials accepted"), else `identity` lists the
    `_json_field` paths tried for "Connected to {label} {identity_phrase} {identity}." (they are exclusive).
    `blank_key_reason`: for an optional `key_field`, the `unsupported` sentence a blank key gets, sent nowhere.
    `reject_names_vendor`: a 401/403 reads "{label} rejected the {key label} - check it." instead of the
    shared sentence. `bearer_when`: a config key that, when non-blank, marks the key as an OAuth access token,
    sent as `Authorization: Bearer` instead of `header_name` (a header the provider accepts for its own
    tokens only).
    """

    path: str
    key_field: str
    header_name: str = "Authorization"
    value_prefix: str = ""
    accept: Optional[str] = "application/json"
    extra_headers: tuple[tuple[str, str], ...] = ()
    field_headers: tuple[tuple[str, str], ...] = ()
    identity: tuple[tuple[str | int, ...], ...] = ()
    identity_phrase: str = "as"
    list_field: Optional[str] = None
    blank_key_reason: Optional[str] = None
    reject_names_vendor: bool = False
    bearer_when: Optional[str] = None


@dataclass(frozen=True)
class CredentialTypeSpec:
    """One credential type: its label, the fields it asks the operator for, and where it lives.

    `default_base_url`: the host a test reaches when none was chosen, shared by `testers.py::_destination`,
    the host pin and the sealing destination (`sealing_destination`) so they cannot disagree. Served to
    the console as a constant the browser mirrors when it seals — never operator-editable,
    and nothing here imports an HTTP client. It is baked into every tier-2 AAD of a row of this type that
    has no host of its own, so editing it makes those rows' secrets unopenable until re-entered.
    `require_https_base_url`: refuse plaintext Base URLs, enforced identically at test
    (`testers.py::_field_problem`) and save (`routes/credentials/credential_wire.py::_validated_value`).
    `oauth_connect`: the CP's authorization-code provider for the operator's own client
    (`oauth_wizards.py`); additive, fields stay writable.
    `key_test`: a single-GET key check declared as data (`KeyTestSpec`); such a type has no tester function.
    `nodevault_eligible`: whether a row of this type may be a NodeVault credential at all (D7: False for
    every OAuth2 type, since the control plane receives and refreshes its tokens; `nodevault_reason` names any
    other cause). A row is eligible when
    its type is AND nothing on the control plane consumes it (`app/credential_consumers.py`).
    `untestable_reason`: the one sentence why a `testable=False` type has no test (pairing guarded in
    `testers.py`). `base_url_field`: which url field is the destination when several exist.
    `require_one_of`: groups of OPTIONAL keys; a row saves only when at least one group has every key
    non-blank (`routes/credentials/credential_wire.py::_require_declared_fields`). `allowed_hosts`: lowercase hostnames;
    when set, a filled host field must be exactly `https://<one of them>` (`testers.py::_allowed_host_clause`,
    enforced at test and save like `require_https_base_url`). Both are checked at import
    (`testers.py::_check_save_rules`).
    """

    slug: str
    label: str
    fields: tuple[FieldSpec, ...]
    testable: bool
    untestable_reason: Optional[str] = None
    #: A sentence naming the Connect flow as the alternative check, served only to readers offered that flow
    #: (`routes/credentials/credential_wire.py::_untestable_reason_out`).
    untestable_connect_remedy: Optional[str] = None
    key_test: Optional[KeyTestSpec] = None
    default_base_url: Optional[str] = None
    require_https_base_url: bool = False
    oauth_connect: Optional[str] = None
    base_url_field: Optional[str] = None
    nodevault_eligible: bool = True
    #: Why a `nodevault_eligible=False` type is not, where it is not the OAuth2 rule
    #: (`credential_consumers.type_ineligible_reason`): the form shows it beside the NodeVault box.
    nodevault_reason: Optional[str] = None
    require_one_of: tuple[tuple[str, ...], ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    #: The config key holding when this type's token expires (`credentials/health.py::expires_at` reads it):
    #: ISO-8601 or epoch milliseconds. None for a type that records no expiry.
    expiry_field: Optional[str] = None


# Why a forge Connect app (`gitlab_app`, `bitbucket_app`, `azure_devops_app`) has no Test.
_CONNECT_APP_UNTESTABLE = (
    "An OAuth app's client pair can only be checked by a real consent - press Connect under "
    "Profile > Git, which fails at once if the pair is wrong."
)


# When the forge answered a connection test with a revocation (`forges.revoked`, D-A14); set only by
# `git_access` and cleared by a reconnect or a new token. Set, the connection reads "Needs reconnect" and
# no task is given the token. `text`, so an operator can see it and clear it after fixing the grant.
_GRANT_DEAD_AT = FieldSpec(
    "grant_dead_at", "Revoked at", "text", False,
    "Set automatically when the forge reports this token revoked - blank means it is not known to be",
)

# The forge account the token belongs to (the login, username or email the forge names), written by Connect,
# a GitHub sign-in or a pasted token's test (`app/git_connections.py`). It is what tells a person's rows for
# two accounts on one forge apart, so a reconnect renews its own account's row (never by name). `text`, as
# `_GRANT_DEAD_AT`: an operator can see it.
_ACCOUNT = FieldSpec(
    "account", "Account", "text", False,
    "Set automatically when the token is connected or tested - the forge account it belongs to",
)


# The three fields Connect fills on an OAuth-refreshed type (`heroku`, `zapier`, `oauth2_client`). The refresh
# token is optional: the row must be savable before Connect, since it holds the client the flow needs, and a
# pasted token is still accepted. The other two are the CP's own token cache, written by `oauth2_tokens.py` after
# a refresh. Declared because an undeclared key is treated as secret by `secret_keys()`, which would hide the
# expiry. Same split as `google`.
_CONNECT_TOKEN_FIELDS = (
    FieldSpec(
        "refresh_token", "Refresh token", "secret", False,
        "Filled by Connect - paste one only if you already hold it",
    ),
    FieldSpec(
        "access_token", "Access token", "secret", False,
        "Cached from the last refresh - no need to fill this",
    ),
    FieldSpec(
        "expiry_date", "Access token expiry", "text", False,
        "Milliseconds since the epoch, cached from the last refresh",
    ),
)

# Insertion order is dropdown order. `testable=False` is honest for a type no test could truthfully
# check; `testers.py` raises at import if `testable` and tester registration disagree, or a reason is missing.
CREDENTIAL_TYPES: dict[str, CredentialTypeSpec] = {
    # The non-secret status of a `user:pass@host` base URL (see github/gitlab below) applies to every url field.
    "jira": CredentialTypeSpec(
        slug="jira",
        label="Jira",
        fields=(
            FieldSpec("base_url", "Base URL", "url", True, "https://acme.atlassian.net"),
            FieldSpec("email", "Account email", "email", True),
            FieldSpec("api_token", "API token", "secret", True),
            # The Jira project key: `base_url` names a site hosting many projects, so the key completes "which
            # project". Optional: blank makes a site credential many projects share. `text`, since Jira publishes it.
            FieldSpec("project_key", "Project key", "text", False, "OAM"),
        ),
        testable=True,
    ),
    "n8n": CredentialTypeSpec(
        slug="n8n",
        label="N8N",
        fields=(
            # A template for n8n's own cloud, not a real host: the operator replaces YOUR-NAME, or the whole URL
            # for a self-hosted instance.
            FieldSpec("base_url", "Base URL", "url", True, default="https://YOUR-NAME.app.n8n.cloud"),
            FieldSpec("api_key", "API key", "secret", True),
        ),
        testable=True,
        # 401s without a valid key; there is no `/me` route, so no identity. Not shape-gated, as with Jira.
        key_test=KeyTestSpec(
            path="/api/v1/workflows?limit=1", key_field="api_key", header_name="X-N8N-API-KEY", accept=None,
        ),
    ),
    # `tenant_id` is required (Xero's `/connections` needs `Xero-Tenant-Id` for a client-credentials token and
    # filters by it) and `text`: it authorizes nothing, and the operator must see what they typed.
    "xero": CredentialTypeSpec(
        slug="xero",
        label="Xero",
        fields=(
            FieldSpec("client_id", "Client ID", "secret", True),
            FieldSpec("client_secret", "Client secret", "secret", True),
            FieldSpec(
                "tenant_id", "Tenant ID", "text", True, "00000000-0000-0000-0000-000000000000"
            ),
            FieldSpec("scope", "Scope", "text", False),
        ),
        testable=True,
        default_base_url="https://identity.xero.com",
    ),
    # One API address, so no url field (same for HubSpot, Slack and AWS below).
    "clockify": CredentialTypeSpec(
        slug="clockify",
        label="Clockify",
        fields=(FieldSpec("api_key", "API key", "secret", True),),
        testable=True,
        default_base_url="https://api.clockify.me",
        # Probed: a bad key answers `401 {"message":"Api key does not exist","code":4003}`.
        key_test=KeyTestSpec(
            path="/api/v1/user", key_field="api_key", header_name="X-Api-Key", identity=(("name",), ("email",)),
        ),
    ),
    "hubspot": CredentialTypeSpec(
        slug="hubspot",
        label="HubSpot",
        fields=(FieldSpec("access_token", "Access token", "secret", True),),
        testable=True,
        default_base_url="https://api.hubapi.com",
        # Names the portal, which tells sandbox from production tokens; needs only the base `oauth` scope.
        # Token introspection needs a client pair a private-app token lacks. `portalId` is a JSON number.
        key_test=KeyTestSpec(
            path="/account-info/v3/details", key_field="access_token", value_prefix="Bearer",
            identity=(("portalId",),), identity_phrase="- portal",
        ),
    ),
    # `app_token` is the Socket Mode app-level token (`xapp-...`), separate from the bot token; neither can
    # stand in for the other.
    "slack": CredentialTypeSpec(
        slug="slack",
        label="Slack",
        fields=(
            FieldSpec("token", "API token", "secret", True),
            FieldSpec("app_token", "App-level token", "secret", True),
            # The app's OAuth client, read off the operator-plane row the socket dials as: Add to Slack installs THIS
            # app in a tenant's workspace (`routes/slack_connect.py`). Optional: without them no tenant can connect.
            # `client_id` is public in every install link, so `text`.
            FieldSpec("client_id", "Client ID", "text", False, help="Basic Information › App Credentials"),
            FieldSpec("client_secret", "Client secret", "secret", False, help="Basic Information › App Credentials"),
        ),
        testable=True,
        default_base_url="https://slack.com",
    ),
    # The bot the per-user Telegram notifications send as (`app/telegram_bot.py`): BotFather's token, which
    # Telegram takes in the URL path (`/bot<token>/<method>`), so the host is fixed and never an operator's.
    "telegram_bot": CredentialTypeSpec(
        slug="telegram_bot",
        label="Telegram bot",
        fields=(FieldSpec("bot_token", "Bot token", "secret", True, help="From @BotFather."),),
        testable=True,
        default_base_url="https://api.telegram.org",
    ),
    # Optional host with a public default. A `user:pass@host` base URL is deliberately non-secret:
    # `testers.py::_operator_host_field` keys on `kind == "url"` for the SSRF host pin, and a url field is not
    # "leave blank to keep", so redaction would lose the credential on a name-only PATCH.

    # Both set `require_https_base_url` (jira/n8n do not): an org-wide source-control PAT warrants it. A new
    # similarly broad type sets it too.
    # One type for a pasted PAT and a connected GitHub account, so no reader branches on spelling.
    # The OAuth fields are optional because a PAT has none; the OAuth App writes its `gho_` token into `token`
    # (a retired GitHub App's `ghu_` one is dead, `git_access.token_for`). `git_access._needs_refresh` keys on
    # `token_expires_at` (`text`: an operator must see it), so a PAT never refreshes. `refresh_token` is
    # secret. A classic OAuth App token never expires, so it behaves like a PAT.
    "github": CredentialTypeSpec(
        slug="github",
        expiry_field="token_expires_at",
        label="GitHub",
        fields=(
            FieldSpec("token", "Personal access token", "secret", True),
            FieldSpec(
                "base_url", "Base URL", "url", False,
                "Leave blank for github.com - set only for GitHub Enterprise Server, "
                "the instance root, not /api/v3 - must be https://",
            ),
            FieldSpec(
                "refresh_token", "Refresh token", "secret", False,
                "Set automatically by Connect GitHub - leave blank for a personal access token",
            ),
            FieldSpec(
                "token_expires_at", "Token expires at", "text", False,
                "Set automatically by Connect GitHub - ISO-8601, blank means the token never expires",
            ),
            _GRANT_DEAD_AT,
            _ACCOUNT,
        ),
        testable=True,
        default_base_url="https://api.github.com",
        require_https_base_url=True,
    ),
    # The one GitHub OAuth App minting every user's `github` credential, for Connect GitHub and GitHub sign-in
    # alike (`git_app_credential_id`). The type key stays `github_app`: it is stored data, and
    # renaming it needs a migration. `client_id` is public, so `text`; `client_secret` goes only in token POST
    # bodies. The retired GitHub App fields (`slug`, `app_id`, `private_key`) are undeclared, so an old row's
    # values read as secret (`secret_keys`) and a PATCH may delete them. Testable narrowly: `test_github_app`
    # checks the client id is recognised.
    "github_app": CredentialTypeSpec(
        slug="github_app",
        label="GitHub OAuth App (connect and sign-in)",
        fields=(
            FieldSpec("client_id", "Client ID", "text", True, "Ov23li..."),
            FieldSpec("client_secret", "Client secret", "secret", True),
        ),
        testable=True,
        default_base_url="https://github.com",
        # An OAuth app: the control plane runs its consent and holds its tokens (D7).
        nodevault_eligible=False,
        # Read only by the operator-tier knob `git_app_credential_id` (`app/git_access.py` refuses non-operator rows).
    ),
    # The one GitHub App, the default way to connect and sign in with GitHub (`git_github_app_credential_id`,
    # chosen by `git_connect_app`). Its user tokens (`ghu_`) reach only the accounts it is installed
    # on, so `slug` is required: Connect carries a person to `github.com/apps/<slug>/installations/new`, where a
    # non-owner requests an owner's approval. `client_id` and `slug` are public, so `text`. Tested like
    # `github_app`: a GitHub App's client id answers the same device-code recognition.
    "github_install_app": CredentialTypeSpec(
        slug="github_install_app",
        label="GitHub App (connect and sign-in)",
        fields=(
            FieldSpec("client_id", "Client ID", "text", True, "Iv23li..."),
            FieldSpec("client_secret", "Client secret", "secret", True),
            FieldSpec("slug", "App slug", "text", True, "The name in the App's github.com/apps/<slug> URL"),
        ),
        testable=True,
        default_base_url="https://github.com",
        nodevault_eligible=False,
        # Read only by the operator-tier knob `git_github_app_credential_id` (as `github_app`).
    ),
    # The three other forges' Connect apps, one operator-plane row each (`routes/forge_connect.py`,
    # designated by `forge_oauth.FORGE_OAUTH[*].app_setting`); like `github_app`, none authenticates a git
    # remote. Untestable: the only check is a real consent, and pressing Connect is that check.
    "gitlab_app": CredentialTypeSpec(
        slug="gitlab_app",
        label="GitLab application (connect)",
        fields=(
            FieldSpec("client_id", "Application ID", "text", True),
            FieldSpec("client_secret", "Secret", "secret", True),
        ),
        testable=False,
        untestable_reason=_CONNECT_APP_UNTESTABLE,
        default_base_url="https://gitlab.com",
        nodevault_eligible=False,
    ),
    "bitbucket_app": CredentialTypeSpec(
        slug="bitbucket_app",
        label="Bitbucket OAuth consumer (connect)",
        fields=(
            FieldSpec("client_id", "Key", "text", True),
            FieldSpec("client_secret", "Secret", "secret", True),
        ),
        testable=False,
        untestable_reason=_CONNECT_APP_UNTESTABLE,
        default_base_url="https://bitbucket.org",
        nodevault_eligible=False,
    ),
    "azure_devops_app": CredentialTypeSpec(
        slug="azure_devops_app",
        label="Microsoft Entra app (sign-in and Azure DevOps connect)",
        fields=(
            FieldSpec("client_id", "Application (client) ID", "text", True),
            FieldSpec("client_secret", "Client secret", "secret", True),
            FieldSpec(
                "tenant", "Tenant", "text", False,
                "Azure DevOps only: blank for any work or school account, or a tenant ID or domain",
            ),
        ),
        testable=False,
        untestable_reason=_CONNECT_APP_UNTESTABLE,
        default_base_url="https://login.microsoftonline.com",
        nodevault_eligible=False,
    ),
    # The OAuth fields are optional because a PAT has none (Connect GitLab fills them). The
    # git username is `oauth2` for both (`git_access.git_login_for`).
    "gitlab": CredentialTypeSpec(
        slug="gitlab",
        expiry_field="token_expires_at",
        label="GitLab",
        fields=(
            FieldSpec("token", "Personal access token", "secret", True),
            FieldSpec(
                "base_url", "Base URL", "url", False,
                "Leave blank for gitlab.com - the instance root, not /api/v4 - must be https://",
            ),
            FieldSpec(
                "refresh_token", "Refresh token", "secret", False,
                "Set automatically by Connect GitLab - leave blank for a personal access token",
            ),
            FieldSpec(
                "token_expires_at", "Token expires at", "text", False,
                "Set automatically by Connect GitLab - ISO-8601, blank means the token never expires",
            ),
            _GRANT_DEAD_AT,
            _ACCOUNT,
        ),
        testable=True,
        default_base_url="https://gitlab.com",
        require_https_base_url=True,
        # `/api/v4` is served from the instance root on gitlab.com and self-hosted alike. Probed: an invalid
        # token answers `401 {"message":"401 Unauthorized"}`. `PRIVATE-TOKEN` takes access tokens only: a
        # Connect grant (the one row with an expiry) answers 401 there, so it goes as a Bearer.
        key_test=KeyTestSpec(
            path="/api/v4/user", key_field="token", header_name="PRIVATE-TOKEN", identity=(("username",),),
            bearer_when="token_expires_at",
        ),
    ),
    # Bitbucket Cloud. `kind` says how `token` authenticates: `api_token` (pasted, with the Atlassian
    # account email as `username` for the API; git itself uses a fixed username) or `oauth` (Connect
    # Bitbucket, C3). No app-password kind: Atlassian disabled app passwords (D-A10). Fixed host.
    "bitbucket": CredentialTypeSpec(
        slug="bitbucket",
        expiry_field="token_expires_at",
        label="Bitbucket",
        fields=(
            FieldSpec("token", "API token", "secret", True),
            FieldSpec(
                "username", "Atlassian account email", "text", False,
                "The email of the Atlassian account the API token belongs to - required for an API token",
            ),
            FieldSpec("kind", "Token kind", "text", False, "api_token (a pasted API token) or oauth"),
            FieldSpec(
                "refresh_token", "Refresh token", "secret", False,
                "Set automatically by Connect Bitbucket - leave blank for an API token",
            ),
            FieldSpec(
                "token_expires_at", "Token expires at", "text", False,
                "Set automatically by Connect Bitbucket - ISO-8601, blank means the token never expires",
            ),
            _GRANT_DEAD_AT,
            _ACCOUNT,
        ),
        testable=True,
        default_base_url="https://api.bitbucket.org",
    ),
    # Azure DevOps Services. `kind`: `pat` (pasted) or `oauth` (Microsoft Entra, C3). A PAT is tested
    # against one organization it can reach (`test_org`); dev.azure.com has no org-free identity call a
    # PAT may make. Fixed host.
    "azure_devops": CredentialTypeSpec(
        slug="azure_devops",
        expiry_field="token_expires_at",
        label="Azure DevOps",
        fields=(
            FieldSpec("token", "Personal access token", "secret", True),
            FieldSpec("kind", "Token kind", "text", False, "pat (a pasted personal access token) or oauth"),
            FieldSpec(
                "test_org", "Organization to test against", "text", False,
                "An Azure DevOps organization this token can reach, as in dev.azure.com/<organization>",
            ),
            FieldSpec(
                "refresh_token", "Refresh token", "secret", False,
                "Set automatically by Connect Azure DevOps - leave blank for a personal access token",
            ),
            FieldSpec(
                "token_expires_at", "Token expires at", "text", False,
                "Set automatically by Connect Azure DevOps - ISO-8601, blank means the token never expires",
            ),
            _GRANT_DEAD_AT,
            _ACCOUNT,
        ),
        testable=True,
        default_base_url="https://dev.azure.com",
    ),
    # `access_key_id` is `text`: AWS displays it and only the secret authenticates; it is how the operator
    # recognises the row.
    "aws": CredentialTypeSpec(
        slug="aws",
        label="AWS",
        fields=(
            FieldSpec("access_key_id", "Access key ID", "text", True),
            FieldSpec("secret_access_key", "Secret access key", "secret", True),
            # Temporary (STS) credentials only: AWS refuses their key pair without it. Every control-plane
            # signer sends it as `X-Amz-Security-Token` (`sigv4.signed_headers`).
            FieldSpec(
                "session_token", "Session token", "secret", False,
                "Temporary credentials only",
            ),
            # The region a cloud connection on this credential starts from (`routes/cloud_nodes.py`'s
            # options); a connection keeps its own `region`, so this is a suggestion, not a scope. Not
            # read by the signers: the sealing destination stays the global STS host.
            FieldSpec("region", "Region", "text", False, "us-east-1", default="us-east-1"),
        ),
        testable=True,
        default_base_url="https://sts.amazonaws.com",
        # Read only by cloud-node launch (`app/cloud_node_launch.py::AWS_CREDENTIAL_TYPE`).
    ),
    # A Microsoft Entra service principal (client-credentials grant) that signs Your Cloud on Azure
    # (`app/azure/auth.py`). Only the client secret authenticates; the three ids are how the operator recognises
    # the row. The Test mints a token and reads the subscription (`testers.py::test_azure`).
    "azure": CredentialTypeSpec(
        slug="azure",
        label="Azure",
        fields=(
            FieldSpec("tenant_id", "Tenant ID", "text", True, "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                      help="Microsoft Entra ID > Overview, or a domain such as acme.onmicrosoft.com"),
            FieldSpec("client_id", "Client ID", "text", True, "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                      help="The app registration's Application (client) ID"),
            FieldSpec("client_secret", "Client secret", "secret", True,
                      help="The secret's Value, not its Secret ID"),
            FieldSpec("subscription_id", "Subscription ID", "text", True, "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                      help="Grant the app Contributor on this subscription"),
        ),
        testable=True,
        default_base_url="https://login.microsoftonline.com",
    ),
    # A Google Cloud service account's JSON key: Your Cloud on GCP (`app/gcp/`, read only through
    # `app/gcp/auth.py`, which mints its access tokens). The whole key file is the secret; the project is
    # derivable from it, so `project_id` is an optional override. The key never leaves the CP: only a signed
    # assertion goes to Google's token endpoint, the sealing destination below.
    "gcp": CredentialTypeSpec(
        slug="gcp",
        label="GCP",
        fields=(
            FieldSpec(
                "service_account_json", "Service account key", "secret", True,
                '{"type": "service_account", ...}',
                help="Paste the whole JSON key file of the service account nodes launch with",
            ),
            FieldSpec(
                "project_id", "Project ID", "text", False,
                "Blank uses the key's own project",
            ),
        ),
        testable=True,
        default_base_url="https://oauth2.googleapis.com",
    ),
    # Self-hosted headscale (REST v1). Two url fields because two hosts: the CP calls the private API host
    # (`api_url`, the test destination), nodes dial the public coordination host (`login_server`). One row
    # per environment; the API key never leaves the CP (`app/headscale_keys.py`).
    "headscale": CredentialTypeSpec(
        slug="headscale",
        label="Headscale",
        fields=(
            FieldSpec("api_key", "API key", "secret", True),
            FieldSpec("login_server", "Login server URL", "url", True, "https://headscale.example.com"),
            FieldSpec("api_url", "API URL", "url", True, "https://headscale-api.example.com"),
        ),
        testable=True,
        base_url_field="api_url",
        require_https_base_url=True,
        # Read only by cloud-node launch (`app/cloud_node_launch.py::HEADSCALE_CREDENTIAL_TYPE`: the operator-tier
        # knob `cloud_node_tailscale_credential_id`).
    ),
    # A Google refresh token under the operator's own client, filled by `oauth_connect` (per-field comments
    # below). Rows from the retired Gemini CLI login (blank client, `id_token`/`token_type` set) still read.
    # No field is required (the row must be savable before Connect).
    "google": CredentialTypeSpec(
        slug="google",
        expiry_field="expiry_date",
        label="Google",
        fields=(
            # The client the token was minted under. Filled: the operator's own client, so the CP can refresh and
            # `oauth2_tokens.access_token_for` can serve live access tokens (what `oauth_connect` fills). Blank: a
            # row from the retired Gemini CLI login, which the CP cannot refresh. Optional rather than a second
            # type: same fields, and the client and the token it minted are one credential.
            FieldSpec(
                "client_id", "Client ID", "text", False,
                "From your own Google Cloud OAuth client",
            ),
            FieldSpec("client_secret", "Client secret", "secret", False),
            FieldSpec(
                "refresh_token", "Refresh token", "secret", False,
                "Filled by Connect - paste one only if you already hold it",
            ),
            # The same three concepts and words as `oauth2_client`. `id_token` and `token_type` are CLI extras.
            FieldSpec(
                "access_token", "Access token", "secret", False,
                "Cached from the last refresh - no need to fill this",
            ),
            FieldSpec("id_token", "ID token", "secret", False),
            FieldSpec("token_type", "Token type", "text", False, "Bearer"),
            FieldSpec(
                "expiry_date", "Access token expiry", "text", False,
                "Milliseconds since the epoch, cached from the last refresh - blank is accepted",
            ),
            # The field Connect reads. The Google provider pins no default scope (`oauth_wizards.py`), so it is
            # required in practice for a connected row, though optional to save.
            FieldSpec(
                "scope", "Scopes", "text", False,
                "Space-separated - what Connect will ask Google for, e.g. .../auth/youtube.upload",
            ),
            FieldSpec(
                "account_email", "Google account", "email", False,
                "Which account consented - shown here only, never written to disk",
            ),
        ),
        # Testable per row: a row with `client_id`/`client_secret` supports a real refresh exchange (proving
        # client, secret and token together); one without gets a per-row `unsupported` from
        # `testers.test_google` rather than a red. A type-wide `False` would withhold the good check.
        testable=True,
        # Google's token endpoint, which the test refresh reaches (consent happens in the browser at
        # `accounts.google.com`). Also the pin origin; required by the registry's third structural guard.
        default_base_url="https://oauth2.googleapis.com",
        # `oauth_connect` has the CP run the authorization-code flow against this row's client; the fields stay
        # pasteable beside it.
        oauth_connect="google",
        nodevault_eligible=False,
    ),
    # A Claude runner account's key (`agent_providers.CLAUDE.api_key_credential_type`; `app/account_auth.py`).
    # One secret field and no `base_url`: model calls go node to Anthropic directly, never via a
    # configurable host. `default_base_url` is fixed, as guard 3 in `testers.py` requires.
    "anthropic_api_key": CredentialTypeSpec(
        slug="anthropic_api_key",
        label="Anthropic API key",
        fields=(
            FieldSpec("api_key", "API key", "secret", True, "sk-ant-..."),
        ),
        testable=True,
        default_base_url="https://api.anthropic.com",
        # Listing models authenticates and bills nothing, unlike a `POST /v1/messages` probe. `anthropic-version`
        # is required by the API (without it the key is never considered): a pinned wire fact.
        key_test=KeyTestSpec(
            path="/v1/models?limit=1", key_field="api_key", header_name="x-api-key",
            extra_headers=(("anthropic-version", "2023-06-01"),), list_field="data",
        ),
    ),
    # A DeepSeek runner account's key (`agent_providers.DEEPSEEK.api_key_credential_type`), created with the
    # account on `POST /api/accounts`. Fixed host for the same reason as the Anthropic type above.
    "deepseek_api_key": CredentialTypeSpec(
        slug="deepseek_api_key",
        label="DeepSeek API key",
        fields=(
            FieldSpec("api_key", "API key", "secret", True, "sk-..."),
        ),
        testable=True,
        default_base_url="https://api.deepseek.com",
        # The route `POST /api/accounts` probes before it writes a DeepSeek account (`agent_providers.api_key_probe`).
        key_test=KeyTestSpec(path="/models", key_field="api_key", value_prefix="Bearer", list_field="data"),
    ),
    # An OpenAI API key, for the runner accounts of the provider whose `api_key_credential_type` names it
    # (`app/agent_providers.py`). Named for the key's vendor, as the Anthropic type is. Fixed host, likewise.
    "openai_api_key": CredentialTypeSpec(
        slug="openai_api_key",
        label="OpenAI API key",
        fields=(
            FieldSpec("api_key", "API key", "secret", True, "sk-..."),
        ),
        testable=True,
        default_base_url="https://api.openai.com",
        key_test=KeyTestSpec(path="/v1/models", key_field="api_key", value_prefix="Bearer", list_field="data"),
    ),
    # A Google AI Studio key: the type `agent_providers.ANTIGRAVITY.api_key_credential_type` names, and the one
    # the support assistant's embeddings read. Fixed host, likewise.
    "gemini_api_key": CredentialTypeSpec(
        slug="gemini_api_key",
        label="Gemini API key",
        fields=(
            FieldSpec("api_key", "API key", "secret", True, "AIza..."),
        ),
        testable=True,
        default_base_url="https://generativelanguage.googleapis.com",
        # The header Google documents, never the `?key=` query form.
        key_test=KeyTestSpec(path="/v1beta/models", key_field="api_key", header_name="x-goog-api-key", list_field="models"),
    ),
    # The deployment's Stripe account (`app/billing/`, selected by the `billing_stripe_credential_id` knob). Fixed
    # host. A RESTRICTED key (`rk_…`) with only the scopes billing uses is the recommended setup; a full secret
    # key (`sk_…`) also works. The publishable key is the one value the browser receives (Stripe.js), hence
    # `text`; the secret is sealed and never leaves the server. The test refuses keys of mismatched modes.
    "stripe": CredentialTypeSpec(
        slug="stripe",
        label="Stripe",
        fields=(
            FieldSpec("secret_key", "Secret or restricted key", "secret", True, "rk_live_..."),
            FieldSpec("publishable_key", "Publishable key", "text", True, "pk_live_..."),
        ),
        testable=True,
        default_base_url="https://api.stripe.com",
        require_https_base_url=True,
    ),
    # Automation vendors, alphabetical: each is one entry (+ a pinned Connect provider for Heroku and Zapier), tested
    # by its `key_test` alone. Fixed hosts except Make.
    "apify": CredentialTypeSpec(
        slug="apify",
        label="Apify",
        fields=(
            FieldSpec("api_token", "API token", "secret", True),
        ),
        testable=True,
        default_base_url="https://api.apify.com",
        require_https_base_url=True,
        key_test=KeyTestSpec(
            path="/v2/users/me", key_field="api_token", value_prefix="Bearer",
            identity=(("data", "username"),), reject_names_vendor=True,
        ),
    ),
    # A personal key authenticates only beside its user id (`x-auth-key`); an organization key needs none.
    "gumloop": CredentialTypeSpec(
        slug="gumloop",
        label="Gumloop",
        fields=(
            FieldSpec("api_key", "API key", "secret", True),
            FieldSpec("user_id", "User ID", "text", False, help="Needed for a personal API key"),
        ),
        testable=True,
        default_base_url="https://api.gumloop.com",
        require_https_base_url=True,
        key_test=KeyTestSpec(
            path="/api/v1/teams", key_field="api_key", value_prefix="Bearer",
            field_headers=(("x-auth-key", "user_id"),), list_field="teams", reject_names_vendor=True,
        ),
    ),
    # Either a pasted API token or the operator's own OAuth client filled by Connect (`oauth_wizards.py`). Test
    # checks the token only: a Connect-only row gets the neutral blank-key sentence, never a refresh spend.
    "heroku": CredentialTypeSpec(
        slug="heroku",
        expiry_field="expiry_date",
        label="Heroku",
        fields=(
            FieldSpec("api_token", "API token", "secret", False, "Or leave blank and press Connect"),
            FieldSpec("client_id", "Client ID", "text", False),
            FieldSpec("client_secret", "Client secret", "secret", False),
            FieldSpec("scope", "Scopes", "text", False, "global - what Connect asks Heroku for"),
            *_CONNECT_TOKEN_FIELDS,
        ),
        testable=True,
        default_base_url="https://api.heroku.com",
        require_https_base_url=True,
        # Platform API v3 needs its versioned Accept; `/account` names the account email.
        key_test=KeyTestSpec(
            path="/account", key_field="api_token", value_prefix="Bearer",
            accept="application/vnd.heroku+json; version=3", identity=(("email",),),
            blank_key_reason=(
                "No API token on this credential - a Connect-only row has no Test; press Connect, which "
                "fails at once if the client is wrong."
            ),
            reject_names_vendor=True,
        ),
        oauth_connect="heroku",
        nodevault_eligible=False,
        require_one_of=(("api_token",), ("client_id", "client_secret")),
    ),
    # An IFTTT Connect service key; `/v2/me` reads the service and triggers nothing.
    "ifttt": CredentialTypeSpec(
        slug="ifttt",
        label="IFTTT",
        fields=(
            FieldSpec("service_key", "Service key", "secret", True),
        ),
        testable=True,
        default_base_url="https://connect.ifttt.com",
        require_https_base_url=True,
        key_test=KeyTestSpec(
            path="/v2/me", key_field="service_key", header_name="IFTTT-Service-Key",
            identity=(("service_id",),), reject_names_vendor=True,
        ),
    ),
    "lovable": CredentialTypeSpec(
        slug="lovable",
        label="Lovable",
        fields=(
            FieldSpec("api_key", "API key", "secret", True, "lov_..."),
        ),
        testable=True,
        default_base_url="https://api.lovable.dev",
        require_https_base_url=True,
        key_test=KeyTestSpec(
            path="/v1/workspaces?limit=1", key_field="api_key", header_name="Lovable-API-Key",
            list_field="data", reject_names_vendor=True,
        ),
    ),
    # Make serves each account from one zone host, so the zone is the url field (sealed as the destination),
    # pinned to the four make.com zones.
    "make": CredentialTypeSpec(
        slug="make",
        label="Make",
        fields=(
            FieldSpec("base_url", "Zone URL", "url", True, "https://us1.make.com", default="https://us1.make.com"),
            FieldSpec("api_token", "API token", "secret", True),
        ),
        testable=True,
        require_https_base_url=True,
        allowed_hosts=("us1.make.com", "us2.make.com", "eu1.make.com", "eu2.make.com"),
        key_test=KeyTestSpec(
            path="/api/v2/users/me", key_field="api_token", value_prefix="Token",
            identity=(("authUser", "name"), ("authUser", "email")), reject_names_vendor=True,
        ),
    ),
    # `default_base_url` only satisfies the fixed-host guard (it is the sealing destination); nothing calls it.
    "replit": CredentialTypeSpec(
        slug="replit",
        label="Replit",
        fields=(
            FieldSpec("api_key", "API key", "secret", True),
        ),
        testable=False,
        untestable_reason="Replit has no public API to check a key against, so any test here would be a guess.",
        default_base_url="https://replit.com",
    ),
    # An OAuth client (Connect), a Zapier MCP server URL, or a Zap's catch-hook URL: each alone is a usable row.
    # The two URLs are `secret`, not `url`: each embeds the capability itself, so neither is validated as a host
    # nor can become the host field.
    "zapier": CredentialTypeSpec(
        slug="zapier",
        expiry_field="expiry_date",
        label="Zapier",
        fields=(
            FieldSpec("client_id", "Client ID", "text", False),
            FieldSpec("client_secret", "Client secret", "secret", False),
            FieldSpec("scope", "Scopes", "text", False, "profile - what Connect asks Zapier for"),
            *_CONNECT_TOKEN_FIELDS,
            FieldSpec("mcp_server_url", "MCP server URL", "secret", False),
            FieldSpec(
                "webhook_url", "Webhook URL", "secret", False, "https://hooks.zapier.com/hooks/catch/...",
            ),
        ),
        # Zapier's token host: no Test calls its API host.
        default_base_url="https://zapier.com",
        testable=False,
        untestable_reason=(
            "Zapier refresh tokens rotate, so the only possible check - spending one - would retire the token "
            "being tested; and the MCP and webhook URLs would run your Zaps."
        ),
        untestable_connect_remedy="Press Connect instead: it fails immediately if the client is wrong.",
        oauth_connect="zapier",
        nodevault_eligible=False,
        require_one_of=(("client_id", "client_secret"), ("mcp_server_url",), ("webhook_url",)),
    ),
    # An org's OpenID Connect sign-in client (SSO). `issuer` is the host field, so the client secret seals under
    # it (`sealing_destination`) and opens with `credential_crypto.open_secret(row, "client_secret")`. `client_id`
    # is `text` as in `oauth2_client`: it is public in every authorize redirect. `provider` is display only -
    # nothing branches on it, so free text rather than a closed vocabulary.
    "oidc_client": CredentialTypeSpec(
        slug="oidc_client",
        label="OIDC (Okta / Auth0)",
        fields=(
            FieldSpec(
                "issuer", "Issuer", "url", True, "https://your-org.okta.com",
                help="The issuer URL exactly as your provider shows it, including any trailing slash",
            ),
            FieldSpec("client_id", "Client ID", "text", True),
            FieldSpec("client_secret", "Client secret", "secret", True),
            FieldSpec("provider", "Provider", "text", False, "Okta, Auth0 or other", help="For reference only"),
        ),
        testable=True,
        # OIDC requires an https issuer, and the client secret must never cross plaintext.
        require_https_base_url=True,
        # NodeVault-eligible as a type; the SSO consumer (`app/credential_consumers.py`) refuses NodeVault for the
        # rows it names, since the control plane opens them at sign-in.
    ),
    # The keyring system mail's one-click unsubscribe links are signed with (`app/email_unsubscribe.py`), designated by
    # the `email_unsubscribe_credential_id` setting. `previous_secret` keeps links already sent valid across a rotation:
    # move the current key into it, then set a new current one. Both names end in SECRET, so the runner transcript
    # redactor (`apps/jaahd/transcript_redact.py`) covers them. Not the session secret, so neither rotation breaks the other.
    "email_unsubscribe_secret": CredentialTypeSpec(
        slug="email_unsubscribe_secret",
        label="Unsubscribe signing key",
        fields=(
            FieldSpec("current_secret", "Current key", "secret", True, help="32 characters or more: openssl rand -hex 32"),
            FieldSpec(
                "previous_secret", "Previous key", "secret", False,
                help="The key this one replaced, so unsubscribe links already sent keep working",
            ),
        ),
        testable=False,
        untestable_reason="A signing key is checked by nobody else: there is nothing to test it against.",
        # Never called: the key signs links this deployment verifies itself. The origin only binds the seal (the
        # registry's rule that every type names one), so it must never change.
        default_base_url="https://jaah.ai",
        nodevault_eligible=False,
        nodevault_reason="Jaah's servers verify the links this key signs, so it can't use NodeVault",
    ),
    # ── THE GENERIC TYPES ─────────────────────────────────────────────────────────────────────
    # Last in the dropdown so purpose-built types are met first. The two generic types below skip
    # `require_https_base_url` (operator decision): a generic row authorizes one host, and refusing plaintext
    # internal boxes would push credentials into text files. Generic OAuth2 comes first: client, secret and
    # refresh token only work together in one row.
    "oauth2_client": CredentialTypeSpec(
        slug="oauth2_client",
        expiry_field="expiry_date",
        label="OAuth2",
        fields=(
            # Two url fields (see `CredentialTypeSpec.base_url_field`): the authorize endpoint is only rendered into
            # a browser redirect; the token endpoint is the only host contacted, so the pin and test follow `token_url`.
            FieldSpec(
                "authorize_url", "Authorize URL", "url", True,
                "https://accounts.example.com/oauth/authorize",
            ),
            FieldSpec("token_url", "Token URL", "url", True, "https://accounts.example.com/oauth/token"),
            FieldSpec("client_id", "Client ID", "text", True),
            FieldSpec("client_secret", "Client secret", "secret", True),
            FieldSpec(
                "scope", "Scopes", "text", False,
                "Space-separated - what Connect will ask the vendor for",
            ),
            *_CONNECT_TOKEN_FIELDS,
        ),
        # Untestable, deliberately: the only check is spending the refresh token, and at a vendor that rotates
        # refresh tokens (Okta, Auth0, Entra, Salesforce) that retires the token. A `testers.Tester` has no row
        # to store the replacement (by design, Test is not a mutation), so a green would destroy the credential.
        testable=False,
        untestable_reason=(
            "The only possible check is to spend the refresh token on a real grant - and at a vendor "
            "whose refresh tokens rotate that would retire the token being tested and leave this "
            "credential dead."
        ),
        # A separate field because not every reader has Connect: `routes/credentials/credential_wire.py::_connect_out` withholds
        # it from tenants for this provider, and `_untestable_reason_out` serves this only alongside that block.
        untestable_connect_remedy=(
            "Press Connect instead: it fails immediately if the client is wrong."
        ),
        # `default_base_url` stays None because `token_url` is a required url field (third structural guard).
        base_url_field="token_url",
        # Unconditional, unlike the generic types: plaintext OAuth2 exposes the client secret, refresh token and
        # authorization code.
        require_https_base_url=True,
        oauth_connect="generic",
        nodevault_eligible=False,
    ),
    "generic_login": CredentialTypeSpec(
        slug="generic_login",
        label="Generic Login/Pwd",
        # Key `base_url`, label "URL": the key is the registry-wide spelling that
        # `routes/credentials/connection_test.py::_with_stored_secrets` falls back to.
        fields=(
            FieldSpec("base_url", "URL", "url", True, "https://example.com/login"),
            FieldSpec("login", "Login", "text", True),
            FieldSpec("password", "Password", "secret", True),
        ),
        # Untestable: a URL, login and password name no mechanism (form POST with CSRF, SSO, Basic?), and a
        # guessed one would red working credentials.
        testable=False,
        untestable_reason=(
            "A URL, a login and a password name no authentication mechanism - the site may expect an "
            "HTML form post, an SSO redirect or HTTP Basic - so any test here would be a guess that "
            "reds a working credential whenever it guessed wrong."
        ),
    ),
    "generic_api_key": CredentialTypeSpec(
        slug="generic_api_key",
        label="Generic API Key",
        # Header name and value prefix record how the key is sent, so consumers need not guess.
        # Blank header name falls back to `Authorization` (`testers.py::_DEFAULT_API_KEY_HEADER`); blank prefix
        # sends the key alone (defaulting to `Bearer` would make `Authorization: <key>` inexpressible). The
        # prefix is a scheme word and the space is supplied (`_is_transmittable` rejects edge whitespace). Both
        # `text`: they authorize nothing and are the fields most likely to be wrong.
        # Base URL is optional and has no default: a key used by an in-product consumer (reCAPTCHA, Gmail)
        # names no host. Blank, Test refuses (`testers.py::test_generic_api_key`) and the secret seals under
        # `""` (`sealing_destination`); filling it later asks for the key again.
        fields=(
            FieldSpec("base_url", "Base URL", "url", False, "https://api.example.com"),
            FieldSpec("api_key", "API key", "secret", True),
            # Validated at write and test time from one home each: `testers.py::_header_name_clause` and
            # `::_value_prefix_clause`, dispatched by `write_time_problem` for `_validated_value`. Runners build
            # requests from this config verbatim, so an unsendable value must never be stored.
            FieldSpec(
                "header_name", "Header name", "text", False,
                "Authorization - the default when blank",
            ),
            FieldSpec(
                "value_prefix", "Value prefix", "text", False,
                "Bearer - sent as <prefix> <key>; blank sends the key alone",
            ),
        ),
        # Testable with a weak claim: green means the host accepted the request, not that the key is valid
        # (`testers.py::test_generic_api_key`).
        testable=True,
    ),
}


#: The types only one feature reads, `{type: feature}` (`packages/editions/features.toml`): an edition without the
#: feature neither lists nor accepts the type (`routes/credentials/credential_wire.py::in_edition`). Data only: this
#: module imports nothing of the app (the node's redactor loads it by path).
TYPE_FEATURES: dict[str, str] = {
    "slack": "slack", "telegram_bot": "telegram", "headscale": "tailnet", "stripe": "billing",
    "email_unsubscribe_secret": "system_mail",
}


def type_label(slug: str) -> str:
    """Human label for `slug`, falling back to the raw slug for a row whose type left the registry."""
    spec = CREDENTIAL_TYPES.get(slug)
    return spec.label if spec is not None else slug


def field_keys(slug: str) -> tuple[str, ...]:
    """The config keys a row of type `slug` declares, in registry order.

    Schema only, never a row's content, so it is safe to serve to runners, which need a field name for
    `routes/agent_project_settings.py::AgentEnvVarWriteIn.credential_field` without pulling secrets via
    `task.credential_get`. An unknown slug declares nothing (`()`), the safe direction.
    """
    spec = CREDENTIAL_TYPES.get(slug)
    return tuple(field.key for field in spec.fields) if spec is not None else ()


def secret_keys(slug: str, config: Optional[Mapping[str, Any]] = None) -> frozenset[str]:
    """The config keys that must never leave the server for a row of type `slug`.

    Declared `kind == "secret"` fields, plus fail-closed for rows that outlive their spec: an unknown slug
    makes every key in `config` secret, and a key no `FieldSpec` classifies is secret. So `config` is
    needed in practice (`routes/credentials/credential_wire.py::_credential_out` always passes it); None only answers which
    keys a known type declares secret — never use it to decide serialization.
    """
    spec = CREDENTIAL_TYPES.get(slug)
    if spec is None:
        return frozenset(config or ())
    declared = {field.key for field in spec.fields}
    return frozenset(
        [field.key for field in spec.fields if field.kind == "secret"]
        + [key for key in (config or ()) if key not in declared]
    )


def host_field(spec: CredentialTypeSpec) -> Optional[FieldSpec]:
    """The field through which the operator chooses the host, or None for a fixed-host type.

    Classified by `kind == "url"`. A type with more than one url field must name its host in
    `spec.base_url_field` (`oauth2_client`'s authorize endpoint is the browser's, not ours); the
    import-time guard in `testers.py` refuses one that names none. One derivation for the tester's
    destination (`testers.py::_operator_host_field`), the stored-secret host pin, and the sealing
    destination below — any divergence is a hole in the pin.
    """
    if spec.base_url_field is not None:
        for field in spec.fields:
            if field.key == spec.base_url_field:
                return field
        # Unreachable (the guard proves the key exists); falling through avoids a registry typo 500.
    for field in spec.fields:
        if field.kind == "url":
            return field
    return None


def canonical_destination(value: Any) -> str:
    """One spelling of a destination URL for the sealing AAD: stripped, no trailing slash, else `""`.

    MIRRORED in `apps/web/src/lib/credentialSeal.ts::canonicalDestination` — the browser seals under the
    same string, so change one, change both. Known residual: Python's `str.strip` and JS's `trim` differ
    on a few exotic code points (U+FEFF, U+0085, U+001C–U+001F); a URL padded with one seals under a
    destination the server spells differently and must be re-entered, which is the safe direction. Deliberately no host lowercasing or scheme normalisation: the
    fewer rules, the fewer ways two implementations disagree, and the URL is stored as validated
    (`routes/credentials/credential_wire.py::_validated_value` strips it) so this is the stored spelling minus a slash.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        # A url field holds a string or nothing; anything else is a caller bug, refused before it is
        # rendered into an AAD (and never rendered into this message).
        raise ValueError("a destination URL is a string")
    return value.strip().rstrip("/")


def sealing_destination(slug: str, config: Mapping[str, Any]) -> str:
    """The destination a row of type `slug` under `config` binds its secrets to: the operator's host
    field when filled, else the type's `default_base_url`, canonicalised; `""` for a type with neither or
    one the registry no longer knows.

    Part of every tier-2 AAD (`app/credential_crypto.py::tier2_aad`), so a secret sealed for one host
    cannot be opened after the row's URL is pointed elsewhere. Mirrored in `credentialSeal.ts`.
    """
    spec = CREDENTIAL_TYPES.get(slug)
    if spec is None:
        return ""
    field = host_field(spec)
    raw = config.get(field.key) if field is not None else None
    if field is None or raw is None or (isinstance(raw, str) and not raw.strip()):
        return canonical_destination(spec.default_base_url)
    return canonical_destination(raw)
