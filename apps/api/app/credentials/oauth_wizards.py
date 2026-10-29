"""The control-plane OAuth2 Connect catalogue — which providers the CP runs the authorization-code flow
against, and what each one needs.

`OAUTH_CONNECT_PROVIDERS` runs against an operator-created client stored in the credential row. Endpoints
(public vendor facts) may be pinned here; a client may not. (The node-CLI login wizard this module once also
catalogued retired with its one driver, the Gemini CLI's own login.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from .registry import CREDENTIAL_TYPES


@dataclass(frozen=True)
class ConnectProviderSpec:
    """One control-plane OAuth2 provider: where its flow goes, and what the operator is told.

    `authorize_url`/`token_url` None means read them off the credential row; pinning them means an
    operator cannot be talked into changing them. `authorize_extra` carries non-RFC parameters — for
    Google, `access_type=offline` and `prompt=consent` are what make a refresh token come back.
    `default_scope` None means the operator must state it. `provider_label` None makes the route name
    the authorize URL's host instead.
    """

    slug: str
    action_label: str
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    authorize_extra: Optional[Mapping[str, str]] = None
    default_scope: Optional[str] = None
    provider_label: Optional[str] = None


OAUTH_CONNECT_PROVIDERS: dict[str, ConnectProviderSpec] = {
    # Google's documented endpoints. No default scope: Google scopes are per-API, and a default would
    # silently mis-grant.
    "google": ConnectProviderSpec(
        slug="google",
        action_label="Connect Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        authorize_extra={"access_type": "offline", "prompt": "consent"},
        provider_label="Google",
    ),
    # Heroku's documented OAuth endpoints; `global` is the scope a Platform API token carries.
    "heroku": ConnectProviderSpec(
        slug="heroku",
        action_label="Connect Heroku",
        authorize_url="https://id.heroku.com/oauth/authorize",
        token_url="https://id.heroku.com/oauth/token",
        default_scope="global",
        provider_label="Heroku",
    ),
    # Zapier's partner OAuth endpoints (authorize on the API host, token on zapier.com).
    "zapier": ConnectProviderSpec(
        slug="zapier",
        action_label="Connect Zapier",
        authorize_url="https://api.zapier.com/v2/authorize",
        token_url="https://zapier.com/oauth/token/",
        default_scope="profile",
        provider_label="Zapier",
    ),
    # Pins nothing, for `oauth2_client`: endpoints, scope and label come off the row. No
    # `authorize_extra`: offline access is spelled differently per vendor.
    "generic": ConnectProviderSpec(slug="generic", action_label="Connect"),
}


def connect_for_type(type_slug: str) -> Optional[ConnectProviderSpec]:
    """The control-plane OAuth2 provider that can fill a `type_slug` credential, or None.

    None both for a type with no provider and for an unknown type.
    """
    spec = CREDENTIAL_TYPES.get(type_slug)
    if spec is None or spec.oauth_connect is None:
        return None
    return OAUTH_CONNECT_PROVIDERS.get(spec.oauth_connect)


# Import-time guard: registry `oauth_connect` values must equal `OAUTH_CONNECT_PROVIDERS` keys (equality, not
# subset). An undeclared provider would 500 on every press; an unused one rots. `RuntimeError`, not `assert`, so
# `python -O` keeps it. Additionally, a provider pinning no endpoint needs its types to declare those fields, or the
# flow resolves a blank endpoint at button press.
_CONNECT_DECLARED = {
    spec.oauth_connect for spec in CREDENTIAL_TYPES.values() if spec.oauth_connect is not None
}
if _CONNECT_DECLARED != set(OAUTH_CONNECT_PROVIDERS):
    raise RuntimeError(
        "CREDENTIAL_TYPES[*].oauth_connect and OAUTH_CONNECT_PROVIDERS disagree: "
        f"{sorted(_CONNECT_DECLARED ^ set(OAUTH_CONNECT_PROVIDERS))}"
    )

for _type_slug, _type_spec in CREDENTIAL_TYPES.items():
    if _type_spec.oauth_connect is None:
        continue
    _provider = OAUTH_CONNECT_PROVIDERS[_type_spec.oauth_connect]
    _keys = {field.key for field in _type_spec.fields}
    _needed = {"client_id", "client_secret", "refresh_token"}
    if _provider.authorize_url is None:
        _needed.add("authorize_url")
    if _provider.token_url is None:
        _needed.add("token_url")
    if not _needed <= _keys:
        raise RuntimeError(
            f"{_type_slug}: declares oauth_connect={_type_spec.oauth_connect!r} but has no "
            f"{sorted(_needed - _keys)} field(s) - the flow would have nothing to read or fill"
        )
